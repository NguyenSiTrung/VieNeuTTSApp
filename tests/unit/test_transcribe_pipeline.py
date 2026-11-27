"""Tests for core/transcribe_pipeline.py: ASR plus speaker labels, concurrent with Nemotron."""

from __future__ import annotations

import os
import threading
import time
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from vienetts_app.core import transcribe_pipeline as tp
from vienetts_app.core.ai_models import DIARIZATION_GGUF_MODELS, WHISPER_GGUF_MODELS
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


def _install_stub_clis(data_dir: Path) -> tuple[Path, dict[str, Path]]:
    """whisper-cli and audiocpp_cli stubs, run through ``ai_models.cli_command``.

    Each logs the thread count it was given. audiocpp_cli marks that it started; while
    the returned flag file exists, whisper-cli waits for that mark (up to WAIT_S) and
    fails without it, so a passing run proves Nemotron started before ASR finished.
    """
    flag, started = data_dir / "require_overlap", data_dir / "nemotron_started"
    logs = {"whisper": data_dir / "whisper.log", "nemotron": data_dir / "nemotron.log"}

    def install(rel_dir: str, name: str, body: str) -> None:
        bin_dir = data_dir / rel_dir
        bin_dir.mkdir(parents=True, exist_ok=True)
        cli = bin_dir / (f"{name}.exe" if os.name == "nt" else name)
        cli.write_text(
            "#!/usr/bin/env python3\nimport json, sys, time\nfrom pathlib import Path\n"
            f"args = sys.argv[1:]\nflag, started = Path({flag.as_posix()!r}), "
            f"Path({started.as_posix()!r})\n" + body,
            encoding="utf-8",
        )
        cli.chmod(0o755)

    install(
        "runtimes/whisper/bin",
        "whisper-cli",
        f"deadline = time.monotonic() + {WAIT_S}\n"
        "while flag.exists() and not started.exists():\n"
        "    if time.monotonic() > deadline:\n"
        "        sys.exit('audiocpp_cli never started')\n"
        "    time.sleep(0.005)\n"
        f"with open({logs['whisper'].as_posix()!r}, 'a') as fh:\n"
        "    fh.write(args[args.index('-t') + 1] + '\\n')\n"
        "Path(args[args.index('-of') + 1] + '.srt').write_text(\n"
        "    '1\\n00:00:00,000 --> 00:00:01,500\\nXin chào.\\n\\n'\n"
        "    '2\\n00:00:02,000 --> 00:00:03,500\\nHôm nay trời đẹp.\\n\\n', encoding='utf-8'\n"
        ")\n",
    )
    install(
        "runtimes/audiocpp/bin",
        "audiocpp_cli",
        "started.touch()\n"
        f"with open({logs['nemotron'].as_posix()!r}, 'a') as fh:\n"
        "    fh.write(args[args.index('--threads') + 1] + '\\n')\n"
        "turns = [{'start_sample': 0, 'end_sample': 28800, 'speaker_id': 'speaker_0'},\n"
        "         {'start_sample': 28800, 'end_sample': 64000, 'speaker_id': 'speaker_1'}]\n"
        "Path(args[args.index('--turns-out') + 1]).write_text(json.dumps(turns))\n",
    )
    for folder, spec in (
        ("whisper", WHISPER_GGUF_MODELS["base"]),
        ("diarization", DIARIZATION_GGUF_MODELS["nemotron-3-diarization-q8_0"]),
    ):
        model_dir = data_dir / "models" / folder
        model_dir.mkdir(parents=True, exist_ok=True)
        with (model_dir / spec.filename).open("wb") as fh:  # sparse, at the catalog size
            fh.truncate(int(spec.size_bytes))
    return flag, logs


def test_default_runners_run_the_whisper_and_nemotron_clis_side_by_side(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    media = _speech_wav(tmp_path / "talk.wav")
    flag, logs = _install_stub_clis(tmp_path)
    asr_cfg = AsrConfig(backend="whisper_cpp", model="base", language="vi", threads=4)
    diar = DiarizationConfig(backend="nemotron_gguf", device="cpu")

    def threads(name: str) -> list[str]:
        return logs[name].read_text(encoding="utf-8").splitlines()

    # parallel=True: audiocpp_cli starts while whisper-cli is still running, and each
    # gets half of the 4-thread budget on its argv.
    flag.touch()
    result = tp.run_transcription(
        media, asr_cfg, diar, work_dir=tmp_path / "par", data_dir=tmp_path, parallel=True
    )
    assert threads("whisper") == ["2"] and threads("nemotron") == ["2"]
    assert [(c.text, c.speaker) for c in result.cues] == [
        ("Xin chào.", "SPEAKER_01"),
        ("Hôm nay trời đẹp.", "SPEAKER_02"),
    ]
    assert result.turns == [RttmTurn(0, 1800, "SPEAKER_01"), RttmTurn(1800, 4000, "SPEAKER_02")]

    # Sequential: the same result, with ASR on the whole budget.
    flag.unlink()
    sequential = tp.run_transcription(
        media, asr_cfg, diar, work_dir=tmp_path / "seq", data_dir=tmp_path
    )
    assert sequential == result and threads("whisper") == ["2", "4"]

    # The acoustic backend goes through diarize_cues; its failure keeps the ASR cues.
    def failing_diarize(*_args: Any, **_kwargs: Any) -> list[Any]:
        raise RuntimeError("onnx exploded")

    monkeypatch.setattr(tp, "diarize_cues", failing_diarize)
    result = tp.run_transcription(
        media,
        asr_cfg,
        DiarizationConfig(backend="acoustic"),
        work_dir=tmp_path / "seq",
        data_dir=tmp_path,
        parallel=True,
    )
    assert [c.speaker for c in result.cues] == ["", ""]
    assert result.notes == ["Không phân tách được người nói: onnx exploded"]
    assert len(threads("nemotron")) == 2  # one Nemotron run per work dir


def test_run_transcription_refines_speech_bounds_at_most_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from vienetts_app.core import asr, diarization, onnx_audio
    from vienetts_app.core.ai_models import AUDIO_MODELS, SILERO_VAD_MODEL_ID

    sr = 16_000
    # 0.25s silence + 3.1s speech + 0.65s silence so VAD bounds tighten strictly inside [0, 3600].
    t = np.arange(int(sr * 3.1), dtype=np.float32) / sr
    samples = np.concatenate(
        [
            np.zeros(int(sr * 0.25), dtype=np.float32),
            0.3 * np.sin(2 * np.pi * 220 * t),
            np.zeros(int(sr * 0.65), dtype=np.float32),
        ]
    )
    media = write_wav_file(samples, tmp_path / "talk.wav", sr)

    real_refine = diarization.refine_cues_speech_bounds
    calls: list[tuple[int, bool]] = []

    def spy_refine(cues, wav_path=None, **kwargs):
        calls.append((len(cues), kwargs.get("turns") is not None))
        return real_refine(cues, wav_path, **kwargs)

    monkeypatch.setattr(diarization, "refine_cues_speech_bounds", spy_refine)
    monkeypatch.setattr(asr, "refine_cues_speech_bounds", spy_refine)

    # 1. Nemotron turns with mid-segment speaker split (both sequential and parallel):
    # refine_cues_speech_bounds must run ONCE (not 3 times across ASR, split, and assign).
    for parallel in (False, True):
        calls.clear()
        stubs = _Stubs(overlap=parallel)
        res = tp.run_transcription(
            media,
            AsrConfig(),
            DiarizationConfig(backend="nemotron_gguf"),
            work_dir=tmp_path / f"cache_nem_{parallel}",
            parallel=parallel,
            runners=stubs.runners(),
        )
        assert len(calls) == 1, (parallel, calls)
        assert calls[0] == (2, True)
        assert len(res.cues) == 2
        assert res.cues[0].speech_start_ms is not None and res.cues[0].speech_start_ms >= 200

    # 2. ASR-only (diar_cfg=None): refines exactly once.
    calls.clear()
    stubs = _Stubs()
    res_asr_only = tp.run_transcription(
        media,
        AsrConfig(),
        None,
        work_dir=tmp_path / "cache_asr_only",
        runners=stubs.runners(),
    )
    assert len(calls) == 1, calls
    assert res_asr_only.cues[0].speech_start_ms is not None
    assert res_asr_only.cues[0].speech_start_ms >= 200

    # 3. Acoustic diarization using default diarize_cues: refines exactly once.
    calls.clear()
    stubs = _Stubs()
    res_acoustic = tp.run_transcription(
        media,
        AsrConfig(),
        DiarizationConfig(backend="acoustic"),
        work_dir=tmp_path / "cache_acoustic",
        runners=tp.PipelineRunners(transcribe=stubs.transcribe),
    )
    assert len(calls) == 1, calls
    assert res_acoustic.cues[0].speaker == "SPEAKER_01"
    assert res_acoustic.cues[0].speech_start_ms is not None
    assert res_acoustic.cues[0].speech_start_ms >= 200

    # 4. Diarization fails -> kept ASR cues are still refined once.
    calls.clear()
    stubs = _Stubs()
    stubs.turns = DiarizationError("runtime down")
    res_failed = tp.run_transcription(
        media,
        AsrConfig(),
        DiarizationConfig(backend="nemotron_gguf"),
        work_dir=tmp_path / "cache_fail",
        runners=stubs.runners(),
    )
    assert len(calls) == 1, calls
    assert res_failed.cues[0].speech_start_ms is not None
    assert res_failed.cues[0].speech_start_ms >= 200

    # 5. With Silero VAD installed, only one pass of Silero frames executes across the 2 split cues.
    silero_spec = AUDIO_MODELS[SILERO_VAD_MODEL_ID]
    silero_path = tmp_path / "models" / silero_spec.subdir / silero_spec.filename
    silero_path.parent.mkdir(parents=True, exist_ok=True)
    with silero_path.open("wb") as fh:
        fh.truncate(silero_spec.size_bytes)

    class _CountingSilero:
        def __init__(self) -> None:
            self.calls = 0

        def run(self, _outputs: object, feeds: dict[str, np.ndarray]) -> list[np.ndarray]:
            self.calls += 1
            frame = feeds["input"][0, 64:]
            prob = 0.9 if float(np.sqrt(np.mean(frame * frame))) > 0.1 else 0.05
            return [np.array([[prob]], dtype=np.float32), feeds["state"]]

    silero = _CountingSilero()
    monkeypatch.setattr(onnx_audio, "default_session_factory", lambda _path, _threads: silero)
    calls.clear()
    stubs = _Stubs()
    tp.run_transcription(
        media,
        AsrConfig(),
        DiarizationConfig(backend="nemotron_gguf"),
        work_dir=tmp_path / "cache_silero",
        data_dir=tmp_path,
        runners=stubs.runners(),
    )
    assert len(calls) == 1, calls
    # 2 split cues cover 1.6s + 1.6s = 3.2s -> ~100 frames (512 samples/frame @ 16kHz),
    # never 3x (~300+ frames).
    assert 50 <= silero.calls <= 130, silero.calls
