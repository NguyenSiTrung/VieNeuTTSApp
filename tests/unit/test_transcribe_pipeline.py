"""Tests for core/transcribe_pipeline.py: ASR plus speaker labels, concurrent with Nemotron."""

from __future__ import annotations

import sys
import threading
import time
import types
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from vienetts_app.core import asr
from vienetts_app.core import transcribe_pipeline as tp
from vienetts_app.core.asr import AsrCancelled, AsrConfig, AsrError, RawSegment
from vienetts_app.core.audio import write_wav_file
from vienetts_app.core.diarization import (
    DiarizationCancelled,
    DiarizationConfig,
    DiarizationError,
    RttmTurn,
)

WAIT_S = 5.0  # upper bound for event waits; a passing run never reaches it

# One Whisper segment spoken by two people: SPEAKER_01 says the first four words.
WORDS = (
    (0, 400, "Xin"),
    (400, 800, "chào"),
    (800, 1200, "các"),
    (1200, 1600, "bạn"),
    (2000, 2400, "hôm"),
    (2400, 2800, "nay"),
    (2800, 3200, "trời"),
    (3200, 3600, "đẹp"),
)
SEGMENT = RawSegment(0, 3600, "Xin chào các bạn hôm nay trời đẹp", words=WORDS)
TURNS = [RttmTurn(0, 1800, "SPEAKER_01"), RttmTurn(1800, 4000, "SPEAKER_02")]
FALLBACK_NOTE = (
    "Nhãn người nói được tạo bằng phương pháp dự phòng (âm học), "
    "độ chính xác thấp hơn mô hình Nemotron-3."
)


def _speech_wav(path: Path, rate: int = 16_000) -> Path:
    t = np.arange(4 * rate, dtype=np.float32) / rate
    return write_wav_file(0.3 * np.sin(2 * np.pi * 220 * t), path, rate)


def _wait_until(condition: Any) -> bool:
    """Poll ``condition`` (a cancel_check) until it holds; False after WAIT_S."""
    tick = threading.Event()
    deadline = time.monotonic() + WAIT_S
    while not condition():
        if time.monotonic() > deadline:
            return False
        tick.wait(0.005)
    return True


class _Stubs:
    """Event-driven ASR / Nemotron / acoustic runners that record what they saw."""

    def __init__(self, *, overlap: bool = False) -> None:
        self.overlap = overlap
        self.turns_started = threading.Event()
        self.seen: dict[str, Any] = {}
        self.order: list[str] = []
        self.turns: list[RttmTurn] | Exception = list(TURNS)

    def transcribe(self, wav, config, *, progress_cb, notes, **_kwargs):
        self.seen["asr"] = (wav, config.threads)
        if self.overlap:  # Nemotron must start while ASR is still running
            assert self.turns_started.wait(WAIT_S)
        progress_cb(0.5, "ASR")
        progress_cb(0.2, "ASR fallback restarts lower")
        notes.append("asr note")
        self.order.append("asr")
        return [SEGMENT]

    def extract_turns(self, wav, config, *, progress_cb, notes, **_kwargs):
        self.seen["turns"] = (wav, config.threads)
        self.order.append("turns")  # before the event, so the order is deterministic
        self.turns_started.set()
        progress_cb(0.9)
        if isinstance(self.turns, Exception):
            raise self.turns
        return list(self.turns)

    def diarize(self, media, cues, config, *, progress_cb, **_kwargs):
        self.seen["diarize"] = (media, config.backend, [c.text for c in cues])
        self.order.append("diarize")
        progress_cb(1.0)
        return [replace(c, speaker="SPEAKER_07") for c in cues]

    def runners(self) -> tp.PipelineRunners:
        return tp.PipelineRunners(
            transcribe=self.transcribe, extract_turns=self.extract_turns, diarize=self.diarize
        )


def test_parallel_overlaps_runners_splits_threads_and_equals_sequential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = _speech_wav(tmp_path / "talk.wav", rate=22_050)
    work = tmp_path / "cache"
    prepared: list[Path] = []
    prepare = tp.ensure_source_16k

    def spy_prepare(src: Path, *args: Any, **kwargs: Any) -> Path:
        prepared.append(Path(src))
        return prepare(src, *args, **kwargs)

    monkeypatch.setattr(tp, "ensure_source_16k", spy_prepare)
    monkeypatch.setattr(tp, "default_asr_threads", lambda: 6)
    stubs = _Stubs(overlap=True)
    progress: list[float] = []
    result = tp.run_transcription(
        media,
        AsrConfig(),
        DiarizationConfig(),
        work_dir=work,
        parallel=True,
        progress=lambda value, _text: progress.append(value),
        runners=stubs.runners(),
    )
    # The media is converted once and both runners read that WAV, each on half the
    # thread budget (default_asr_threads() when threads=0).
    shared = work / "source_16k.wav"
    assert prepared == [media]
    assert stubs.seen["asr"] == (shared, 3) and stubs.seen["turns"] == (shared, 3)
    # The segment spanning a speaker change is split at the word boundary.
    assert [(c.text, c.speaker) for c in result.cues] == [
        ("Xin chào các bạn", "SPEAKER_01"),
        ("hôm nay trời đẹp", "SPEAKER_02"),
    ]
    assert result.turns == TURNS and result.speakers == ["SPEAKER_01", "SPEAKER_02"]
    assert result.notes == ["asr note"]
    # Combined progress never moves back, though ASR's own progress did.
    assert progress == sorted(progress) and progress[-1] == 1.0

    # The sequential path gives the same result; each runner keeps its own threads.
    sequential = _Stubs()
    progress.clear()
    assert (
        tp.run_transcription(
            media,
            AsrConfig(),
            DiarizationConfig(),
            work_dir=work,
            progress=lambda value, _text: progress.append(value),
            runners=sequential.runners(),
        )
        == result
    )
    assert sequential.order == ["asr", "turns"]
    assert sequential.seen["asr"][1] == sequential.seen["turns"][1] == 0
    assert progress == sorted(progress) and progress[-1] == 1.0

    # threads // 2, at least 1.
    for threads, half in ((9, 4), (1, 1)):
        stubs = _Stubs(overlap=True)
        tp.run_transcription(
            media,
            AsrConfig(threads=threads),
            DiarizationConfig(backend="nemotron_gguf"),
            work_dir=work,
            parallel=True,
            runners=stubs.runners(),
        )
        assert stubs.seen["asr"][1] == stubs.seen["turns"][1] == half
    assert len(prepared) == 4


def test_diarization_failures_keep_asr_cues_and_acoustic_runs_after_asr(tmp_path: Path) -> None:
    media = _speech_wav(tmp_path / "talk.wav")

    def run(stubs: _Stubs, diar: DiarizationConfig | None, asr_cfg: AsrConfig | None = None):
        return tp.run_transcription(
            media,
            asr_cfg,
            diar,
            work_dir=tmp_path / "cache",
            parallel=True,
            runners=stubs.runners(),
        )

    # Nemotron chosen explicitly and failing: the ASR cues survive, with a note.
    stubs = _Stubs(overlap=True)
    stubs.turns = DiarizationError("runtime crashed")
    result = run(stubs, DiarizationConfig(backend="nemotron_gguf"))
    assert [(c.text, c.speaker) for c in result.cues] == [(SEGMENT.text, "")]
    assert result.notes == ["asr note", "Không phân tách được người nói: runtime crashed"]
    assert result.turns is None

    # Auto with Nemotron unavailable: the acoustic fallback runs once ASR is done.
    stubs = _Stubs(overlap=True)
    stubs.turns = DiarizationError("chưa cài audio.cpp")
    result = run(stubs, DiarizationConfig(backend="auto"))
    assert stubs.order == ["turns", "asr", "diarize"]
    assert stubs.seen["diarize"] == (tmp_path / "talk.wav", "acoustic", [SEGMENT.text])
    assert [c.speaker for c in result.cues] == ["SPEAKER_07"]
    assert result.notes == [
        "asr note",
        "Không dùng được mô hình Nemotron-3: chưa cài audio.cpp",
        FALLBACK_NOTE,
    ]

    # The acoustic backend needs the cues, so it never runs concurrently and the
    # thread budget is left alone.
    stubs = _Stubs()
    result = run(stubs, DiarizationConfig(backend="acoustic"), AsrConfig(threads=8))
    assert stubs.order == ["asr", "diarize"] and "turns" not in stubs.seen
    assert stubs.seen["asr"][1] == 8 and stubs.seen["diarize"][1] == "acoustic"

    # Any other diarization failure keeps the ASR cues too.
    stubs = _Stubs()
    stubs.diarize = lambda *_a, **_k: (_ for _ in ()).throw(RuntimeError("onnx exploded"))
    result = run(stubs, DiarizationConfig(backend="acoustic"))
    assert [c.speaker for c in result.cues] == [""]
    assert result.notes == ["asr note", "Không phân tách được người nói: onnx exploded"]

    # Without a diarization config only ASR runs.
    stubs = _Stubs()
    result = run(stubs, None)
    assert stubs.order == ["asr"] and result.notes == ["asr note"] and result.turns is None


def test_cancel_stops_both_runners_and_an_asr_failure_stops_nemotron(tmp_path: Path) -> None:
    media = _speech_wav(tmp_path / "talk.wav")
    cancel = threading.Event()
    stopped: list[str] = []
    stubs = _Stubs(overlap=True)

    def cancelled_asr(wav, config, *, cancel_check, **_kwargs):
        assert stubs.turns_started.wait(WAIT_S)
        cancel.set()  # the user cancels while both run
        assert cancel_check()
        raise AsrCancelled("Đã hủy nhận diện giọng nói.")

    def turns_until_cancelled(wav, config, *, cancel_check, **_kwargs):
        stubs.turns_started.set()
        assert _wait_until(cancel_check)
        stopped.append("turns")
        raise DiarizationCancelled("Đã hủy phân tách người nói.")

    runners = tp.PipelineRunners(
        transcribe=cancelled_asr, extract_turns=turns_until_cancelled, diarize=stubs.diarize
    )
    with pytest.raises(AsrCancelled):
        tp.run_transcription(
            media,
            AsrConfig(),
            DiarizationConfig(),
            parallel=True,
            cancel_check=cancel.is_set,
            runners=runners,
        )
    assert stopped == ["turns"]  # returned only once Nemotron had stopped

    # A real ASR failure stops Nemotron through its cancel_check, then surfaces.
    stubs = _Stubs(overlap=True)

    def failing_asr(wav, config, **_kwargs):
        assert stubs.turns_started.wait(WAIT_S)
        raise AsrError("whisper.cpp thất bại: GPU init error")

    runners = replace(runners, transcribe=failing_asr)
    with pytest.raises(AsrError, match="GPU init error"):
        tp.run_transcription(
            media, AsrConfig(), DiarizationConfig(), parallel=True, runners=runners
        )
    assert stopped == ["turns", "turns"]

    # A cancel once ASR has finished keeps its cues, as the sequential path does.
    cancel.clear()
    stubs = _Stubs(overlap=True)

    def asr_then_cancel(wav, config, **kwargs):
        segments = stubs.transcribe(wav, config, **kwargs)
        cancel.set()
        return segments

    runners = replace(runners, transcribe=asr_then_cancel)
    result = tp.run_transcription(
        media,
        AsrConfig(),
        DiarizationConfig(),
        parallel=True,
        cancel_check=cancel.is_set,
        runners=runners,
    )
    assert [c.text for c in result.cues] == [SEGMENT.text]
    assert result.notes == [
        "asr note",
        "Đã hủy phân tách người nói; phụ đề vẫn được giữ nguyên.",
    ]


def test_default_runners_match_the_controller_flow(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = _speech_wav(tmp_path / "talk.wav")

    class _Word:
        def __init__(self, start_ms: int, end_ms: int, word: str) -> None:
            self.start, self.end, self.word = start_ms / 1000, end_ms / 1000, word

    class _Seg:
        start, end, text = 0.0, 3.6, SEGMENT.text
        words = [_Word(*w) for w in WORDS]

    class _Model:
        def __init__(self, model_size: str, **kwargs: object) -> None:
            pass

        def transcribe(self, path: str, **kwargs: object):
            return iter([_Seg()]), types.SimpleNamespace(duration=4.0, language="vi")

    fake_mod = types.ModuleType("faster_whisper")
    fake_mod.WhisperModel = _Model  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "faster_whisper", fake_mod)
    monkeypatch.setattr(asr, "_FASTER_WHISPER_CACHE", {})
    seen: list[tuple[Path, str, int, Path | None]] = []

    def fake_diarize(media_path, cues, config, *, work_dir, data_dir, notes, **_kwargs):
        seen.append((Path(media_path), config.backend, config.threads, data_dir))
        if config.backend == "acoustic":
            raise RuntimeError("onnx exploded")
        notes.append("diar note")
        return [replace(c, speaker="SPEAKER_01") for c in cues]

    monkeypatch.setattr(tp, "diarize_cues", fake_diarize)
    asr_cfg = AsrConfig(backend="faster_whisper")
    # No Nemotron turn runner yet: parallel=True runs the controller's sequential flow
    # — the real ASR, then diarize_cues on the same WAV with the configs as given.
    result = tp.run_transcription(
        media,
        asr_cfg,
        DiarizationConfig(),
        work_dir=tmp_path / "cache",
        data_dir=tmp_path,
        parallel=True,
    )
    assert seen == [(media, "auto", 0, tmp_path)]
    assert [(c.text, c.speaker) for c in result.cues] == [(SEGMENT.text, "SPEAKER_01")]
    assert result.notes == ["Ngôn ngữ nhận diện được: vi.", "diar note"]
    assert result.turns is None
    result = tp.run_transcription(
        media, asr_cfg, DiarizationConfig(backend="acoustic"), work_dir=tmp_path / "cache"
    )
    assert [c.speaker for c in result.cues] == [""]
    assert result.notes[-1] == "Không phân tách được người nói: onnx exploded"
