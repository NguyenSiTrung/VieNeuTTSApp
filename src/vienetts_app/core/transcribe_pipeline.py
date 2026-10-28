"""Transcription pipeline: one 16 kHz prep, Whisper ASR, then speaker labels.

``run_transcription`` is the worker-thread body behind "Nhận diện phụ đề" in the
subtitle controller. It converts the media once (``audio_prep.ensure_source_16k``)
and feeds that WAV to both runners:

* sequentially (the default) it is the controller's original flow — ASR, then
  speaker labels — with the same notes and the same failure handling;
* with ``parallel=True`` the Nemotron/audio.cpp turn runner works alongside ASR,
  each on half of the thread budget, and the cues are labelled from its turns
  once both finish. The acoustic fallback needs the cues, so it always runs after
  ASR.

Speaker labelling never costs the transcription: a diarization failure or cancel
keeps the ASR cues and adds a note. A Whisper segment spoken by two people is cut
at the word nearest the speaker change (``diarization.split_segments_at_turns``).
"""

from __future__ import annotations

import contextlib
import logging
import tempfile
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from vienetts_app.core.asr import (
    AsrCancelled,
    AsrConfig,
    AsrError,
    RawSegment,
    default_asr_threads,
    segments_to_cues,
    transcribe_segments,
)
from vienetts_app.core.audio_prep import AudioPrepCancelled, AudioPrepError, ensure_source_16k
from vienetts_app.core.diarization import (
    DiarizationCancelled,
    DiarizationConfig,
    DiarizationError,
    RttmTurn,
    apply_speaker_ceiling,
    assign_speakers_from_turns,
    diarize_cues,
    extract_speaker_turns,
    split_segments_at_turns,
)
from vienetts_app.core.subtitles import Cue

logger = logging.getLogger(__name__)

#: Share of the progress bar for ASR when speaker labels follow (as before).
ASR_PROGRESS_SHARE = 0.7

ASR_TEXT = "Đang nhận diện giọng nói (ASR)…"
DIAR_TEXT = "Đang phân tách người nói…"
DONE_TEXT = "Hoàn tất nhận diện giọng nói."
DIAR_CANCELLED_NOTE = "Đã hủy phân tách người nói; phụ đề vẫn được giữ nguyên."
DIAR_FAILED_NOTE = "Không phân tách được người nói: {error}"
NEMOTRON_UNAVAILABLE_NOTE = "Không dùng được mô hình Nemotron-3: {error}"
ACOUSTIC_FALLBACK_NOTE = (
    "Nhãn người nói được tạo bằng phương pháp dự phòng (âm học), "
    "độ chính xác thấp hơn mô hình Nemotron-3."
)

ProgressFn = Callable[[float, str], None]
#: ``transcribe(wav, asr_cfg, *, work_dir, data_dir, progress_cb, cancel_check, notes)``
#: returns the filtered raw segments (with word timings) — ``asr.transcribe_segments``.
TranscribeRunner = Callable[..., list[RawSegment]]
#: ``extract_turns(wav, diar_cfg, *, work_dir, data_dir, progress_cb, cancel_check,
#: notes)`` returns the Nemotron speaker turns, raising ``DiarizationError`` when the
#: runtime or model is unavailable and ``DiarizationCancelled`` on cancel.
TurnsRunner = Callable[..., list[RttmTurn]]
#: ``diarize(wav, cues, diar_cfg, *, work_dir, data_dir, progress_cb, cancel_check,
#: notes)`` labels finished cues — ``diarization.diarize_cues``.
DiarizeRunner = Callable[..., list[Cue]]


@dataclass(frozen=True)
class PipelineRunners:
    """The runners ``run_transcription`` calls; ``None`` picks the default.

    The defaults are ``asr.transcribe_segments``, ``diarization.extract_speaker_turns``
    (the Nemotron CLI) and ``diarization.diarize_cues`` (the acoustic backend and the
    ``auto`` fallback).
    """

    transcribe: TranscribeRunner | None = None
    extract_turns: TurnsRunner | None = None
    diarize: DiarizeRunner | None = None


@dataclass(frozen=True)
class TranscriptionResult:
    cues: list[Cue]
    #: User-facing notes: ASR notes first, then the speaker-label notes.
    notes: list[str]
    #: Nemotron turns the cues were labelled from; ``None`` when speaker labels came
    #: from ``diarize`` (or were not produced).
    turns: list[RttmTurn] | None = None

    @property
    def speakers(self) -> list[str]:
        """Speaker labels in first-appearance order."""
        return list(dict.fromkeys(c.speaker for c in self.cues if c.speaker))


def split_thread_budget(threads: int) -> int:
    """Threads for each of two concurrent runners: half the budget, at least 1.

    ``threads <= 0`` means automatic, so the budget is ``default_asr_threads()``.
    """
    budget = threads if threads > 0 else default_asr_threads()
    return max(1, budget // 2)


class _Progress:
    """Combine ASR and diarization progress into one bar that never moves back."""

    def __init__(self, progress: ProgressFn | None, *, diarize: bool, parallel: bool) -> None:
        self._progress = progress
        self._diarize = diarize
        self._parallel = parallel
        self._lock = threading.Lock()
        self._asr = 0.0
        self._diar = 0.0
        self._last = 0.0

    def _combined(self, *, diar_phase: bool) -> float:
        if not self._diarize:
            return self._asr
        if self._parallel:
            return ASR_PROGRESS_SHARE * self._asr + (1 - ASR_PROGRESS_SHARE) * self._diar
        if diar_phase:
            return ASR_PROGRESS_SHARE + (1 - ASR_PROGRESS_SHARE) * self._diar
        return ASR_PROGRESS_SHARE * self._asr

    def _emit(self, value: float, text: str) -> None:
        # The callback runs under the lock too, so two runners can never deliver
        # their values out of order.
        with self._lock:
            value = min(1.0, max(self._last, value))
            self._last = value
            if self._progress is not None:
                self._progress(value, text)

    def asr(self, value: float, text: str = "") -> None:
        with self._lock:
            self._asr = max(self._asr, value)
            combined = self._combined(diar_phase=False)
        self._emit(combined, text or ASR_TEXT)

    def diar(self, value: float) -> None:
        with self._lock:
            self._diar = max(self._diar, value)
            combined = self._combined(diar_phase=True)
        self._emit(combined, DIAR_TEXT)

    def done(self) -> None:
        self._emit(1.0, DONE_TEXT)


def _diar_backend(config: DiarizationConfig) -> str:
    backend = (config.backend or "auto").strip()
    return "nemotron_gguf" if backend == "nemo" else backend  # legacy setting value


def run_transcription(
    media: str | Path,
    asr_cfg: AsrConfig | None = None,
    diar_cfg: DiarizationConfig | None = None,
    *,
    work_dir: Path | None = None,
    data_dir: Path | None = None,
    parallel: bool = False,
    progress: ProgressFn | None = None,
    cancel_check: Callable[[], bool] | None = None,
    runners: PipelineRunners | None = None,
) -> TranscriptionResult:
    """Transcribe ``media`` and, with a ``diar_cfg``, label the cues by speaker.

    ``work_dir`` is the media cache (the shared ``source_16k.wav`` and the ASR and
    diarization caches); without one a temp dir is used and removed. Raises
    ``AsrCancelled``/``AsrError`` as ``asr.transcribe_media_to_cues`` does; speaker
    labelling problems become notes on a result that keeps the ASR cues.
    """
    asr_cfg = asr_cfg or AsrConfig()
    picked = runners or PipelineRunners()
    transcribe = picked.transcribe or transcribe_segments
    extract_turns = picked.extract_turns or extract_speaker_turns
    diarize = picked.diarize or diarize_cues

    def cancelled() -> bool:
        return cancel_check is not None and bool(cancel_check())

    if cancelled():
        raise AsrCancelled("Đã hủy nhận diện giọng nói.")
    src = Path(media)
    if not src.is_file():
        raise AsrError(f"Không tìm thấy tệp phương tiện: {src.name}")

    backend = _diar_backend(diar_cfg) if diar_cfg is not None else ""
    # The acoustic backend clusters the finished cues, so only Nemotron turns can
    # run alongside ASR.
    turns_runner = extract_turns if backend in ("auto", "nemotron_gguf") else None
    concurrent = parallel and turns_runner is not None
    bar = _Progress(progress, diarize=diar_cfg is not None, parallel=concurrent)
    if concurrent and diar_cfg is not None:
        half = split_thread_budget(asr_cfg.threads)
        asr_cfg = replace(asr_cfg, threads=half)
        diar_cfg = replace(diar_cfg, threads=half)

    temp_dir: tempfile.TemporaryDirectory[str] | None = None
    if work_dir is None:
        temp_dir = tempfile.TemporaryDirectory(prefix="vienetts_transcribe_")
    cache_dir = work_dir if work_dir is not None else Path(temp_dir.name)  # type: ignore[union-attr]
    try:
        wav = _prepare(src, cache_dir, data_dir, bar, cancel_check)
        asr_notes: list[str] = []
        diar_notes: list[str] = []
        stop = threading.Event()

        def run_asr() -> tuple[list[RawSegment], list[Cue]]:
            segments = transcribe(
                wav,
                asr_cfg,
                work_dir=cache_dir,
                data_dir=data_dir,
                progress_cb=bar.asr,
                cancel_check=cancel_check,
                notes=asr_notes,
            )
            return segments, segments_to_cues(segments, wav, data_dir=data_dir)

        def run_turns() -> list[RttmTurn]:
            if turns_runner is None or diar_cfg is None:
                return []
            return turns_runner(
                wav,
                diar_cfg,
                work_dir=cache_dir,
                data_dir=data_dir,
                progress_cb=bar.diar,
                cancel_check=lambda: stop.is_set() or cancelled(),
                notes=diar_notes,
            )

        turns_future: Future[list[RttmTurn]] | None = None
        if concurrent:
            with ThreadPoolExecutor(max_workers=1, thread_name_prefix="diarize") as pool:
                turns_future = pool.submit(run_turns)
                try:
                    segments, cues = run_asr()
                except BaseException:
                    stop.set()  # leaving the block waits for the turn runner to stop
                    raise
        else:
            segments, cues = run_asr()

        result = TranscriptionResult(cues, asr_notes)
        if diar_cfg is not None:
            result = _label_speakers(
                wav,
                segments,
                cues,
                diar_cfg,
                backend=backend,
                turns_future=turns_future,
                run_turns=run_turns if turns_runner is not None else None,
                diarize=diarize,
                work_dir=cache_dir,
                data_dir=data_dir,
                bar=bar,
                cancelled=cancelled,
                cancel_check=cancel_check,
                asr_notes=asr_notes,
                diar_notes=diar_notes,
            )
        bar.done()
        return result
    finally:
        if temp_dir is not None:
            with contextlib.suppress(OSError):
                temp_dir.cleanup()


def _prepare(
    src: Path,
    cache_dir: Path,
    data_dir: Path | None,
    bar: _Progress,
    cancel_check: Callable[[], bool] | None,
) -> Path:
    """The single 16 kHz conversion both runners read (never two in parallel)."""
    try:
        return ensure_source_16k(
            src,
            cache_dir,
            data_dir=data_dir,
            cancel_check=cancel_check,
            progress_cb=lambda f: bar.asr(0.05 + 0.05 * f, "Đang trích xuất âm thanh…"),
        )
    except AudioPrepCancelled as exc:
        raise AsrCancelled("Đã hủy nhận diện giọng nói.") from exc
    except AudioPrepError as exc:
        raise AsrError(str(exc)) from exc


def _label_speakers(
    wav: Path,
    segments: Sequence[RawSegment],
    cues: list[Cue],
    diar_cfg: DiarizationConfig,
    *,
    backend: str,
    turns_future: Future[list[RttmTurn]] | None,
    run_turns: Callable[[], list[RttmTurn]] | None,
    diarize: DiarizeRunner,
    work_dir: Path,
    data_dir: Path | None,
    bar: _Progress,
    cancelled: Callable[[], bool],
    cancel_check: Callable[[], bool] | None,
    asr_notes: list[str],
    diar_notes: list[str],
) -> TranscriptionResult:
    """Label ``cues`` by speaker; any failure keeps them unlabelled with a note."""

    def kept(note: str) -> TranscriptionResult:
        return TranscriptionResult(cues, [*asr_notes, *diar_notes, note])

    def run_diarize(config: DiarizationConfig) -> list[Cue]:
        return diarize(
            wav,
            cues,
            config,
            work_dir=work_dir,
            data_dir=data_dir,
            progress_cb=bar.diar,
            cancel_check=cancel_check,
            notes=diar_notes,
        )

    try:
        if turns_future is None and cancelled():
            raise DiarizationCancelled("Đã hủy phân tách người nói.")
        if run_turns is None:
            return TranscriptionResult(run_diarize(diar_cfg), [*asr_notes, *diar_notes])
        try:
            turns = turns_future.result() if turns_future is not None else run_turns()
        except DiarizationCancelled:
            raise
        except DiarizationError as exc:
            if backend != "auto":
                raise
            # Same chain as diarize_cues(auto): Nemotron unavailable -> acoustic.
            logger.warning("Nemotron-3 diarization unavailable, using acoustic: %s", exc)
            diar_notes.append(NEMOTRON_UNAVAILABLE_NOTE.format(error=exc))
            if cancelled():
                raise DiarizationCancelled("Đã hủy phân tách người nói.") from exc
            labelled = run_diarize(replace(diar_cfg, backend="acoustic"))
            diar_notes.append(ACOUSTIC_FALLBACK_NOTE)
            return TranscriptionResult(labelled, [*asr_notes, *diar_notes])
        return TranscriptionResult(
            _cues_from_turns(segments, cues, turns, wav, diar_cfg, data_dir),
            [*asr_notes, *diar_notes],
            list(turns),
        )
    except DiarizationCancelled:
        return kept(DIAR_CANCELLED_NOTE)
    except Exception as exc:  # noqa: BLE001 - the ASR cues must survive
        logger.warning("Diarization failed during transcribe: %s", exc)
        return kept(DIAR_FAILED_NOTE.format(error=exc))


def _cues_from_turns(
    segments: Sequence[RawSegment],
    cues: list[Cue],
    turns: Sequence[RttmTurn],
    wav: Path,
    diar_cfg: DiarizationConfig,
    data_dir: Path | None,
) -> list[Cue]:
    """Split segments that span a speaker change, then label cues by max overlap."""
    split: list[Any] = split_segments_at_turns(segments, turns)
    if len(split) != len(segments):
        cues = segments_to_cues(split, wav, data_dir=data_dir)
    labelled = assign_speakers_from_turns(cues, turns, wav_path=wav, data_dir=data_dir)
    return apply_speaker_ceiling(labelled, diar_cfg.max_speakers)
