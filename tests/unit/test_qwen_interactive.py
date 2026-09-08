"""Qwen interactive synthesis + Base enrollment through the worker (Phase 4 Task 5)."""

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication  # noqa: E402

from vienetts_app.core.artifacts import validate_wav_artifact  # noqa: E402
from vienetts_app.core.backends import BackendCapabilityError  # noqa: E402
from vienetts_app.core.jobs import JobTerminal, SynthesisJob  # noqa: E402
from vienetts_app.core.models import TTSRequest, VoiceOp  # noqa: E402
from vienetts_app.core.qwen_voices import list_reference_voices, load_reference_voice  # noqa: E402
from vienetts_app.core.tts_backend import TtsBackend, register_backend  # noqa: E402
from vienetts_app.workers.inference_worker import InferenceWorker  # noqa: E402


def sine(n: int, rate: int) -> np.ndarray:
    t = np.arange(n, dtype=np.float64) / rate
    return (0.5 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.float32)


def write_clip(path: Path, seconds: float = 4.0, rate: int = 44100) -> None:
    import soundfile as sf

    t = np.arange(int(seconds * rate), dtype=np.float64) / rate
    sf.write(str(path), (0.4 * np.sin(2.0 * np.pi * 220.0 * t)).astype(np.float32), rate)


def wait_until(cond, timeout: float = 10.0) -> bool:
    app = QCoreApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        if app is not None:
            app.processEvents()
        time.sleep(0.01)
    return False


class RecordingQwenBackend(TtsBackend):
    """Registry-level Qwen stand-in; records synthesize kwargs per engine."""

    def __init__(self, engine_id: str) -> None:
        self.engine_id = engine_id
        self.native_sample_rate = 24000
        self.calls: list[dict[str, Any]] = []
        self.closed = False

    def initialize(self) -> None:
        pass

    @property
    def is_initialized(self) -> bool:
        return True

    def synthesize_stream(
        self,
        text: str,
        *,
        voice: str | None = None,
        language: str | None = None,
        instruction: str | None = None,
        temperature: float | None = None,
    ) -> Iterator[np.ndarray]:
        self.calls.append(
            {"text": text, "voice": voice, "language": language, "instruction": instruction}
        )
        yield sine(12000, 24000)

    def close(self) -> None:
        self.closed = True


class FakeVieneu:
    def initialize(self) -> None:
        pass

    @property
    def is_initialized(self) -> bool:
        return True

    def close(self) -> None:
        pass


def make_job(job_id: str, request: Any, artifact_path: Path | None) -> SynthesisJob:
    return SynthesisJob(
        id=job_id,
        owner="text",  # type: ignore[arg-type]
        kind="interactive",  # type: ignore[arg-type]
        priority=0,
        request=request,
        artifact_path=artifact_path,
        live_transport=None,
    )


@pytest.fixture
def rig(qcoreapp, tmp_path: Path):
    workers: list[InferenceWorker] = []
    terminals: list[JobTerminal] = []

    def make(**kwargs: Any) -> InferenceWorker:
        worker = InferenceWorker(FakeVieneu(), voices_dir=tmp_path / "voices", **kwargs)
        worker.terminal.connect(terminals.append)
        worker.start()
        workers.append(worker)
        return worker

    yield make, terminals, tmp_path
    for worker in workers:
        worker.stop()
    register_backend("qwen_customvoice", None)
    register_backend("qwen_base", None)


def run(rig, worker: InferenceWorker, terminals, job: SynthesisJob) -> JobTerminal:
    assert worker.submit(job) is True
    assert wait_until(lambda: any(t.job_id == job.id for t in terminals))
    (terminal,) = [t for t in terminals if t.job_id == job.id]
    return terminal


class TestCustomVoiceJob:
    def test_forwards_speaker_language_instruction(self, rig) -> None:
        make, terminals, tmp_path = rig
        seen: list[RecordingQwenBackend] = []
        register_backend(
            "qwen_customvoice",
            lambda **kw: seen.append(RecordingQwenBackend("qwen_customvoice")) or seen[-1],
        )
        request = TTSRequest(
            text="hello world",
            engine="qwen_customvoice",
            language="en",
            voice="Ryan",
            instruction="cheerful",
        )
        path = tmp_path / "cv.wav"
        terminal = run(rig, make(), terminals, make_job("b" * 32, request, path))
        assert terminal.state == "completed"
        (backend,) = seen
        assert backend.calls[0]["voice"] == "Ryan"
        assert backend.calls[0]["language"] == "en"
        assert backend.calls[0]["instruction"] == "cheerful"
        frames, rate = validate_wav_artifact(path)
        assert (rate, frames > 0) == (48000, True)


class TestBaseEnrollment:
    def test_enroll_then_synthesize(self, rig) -> None:
        make, terminals, tmp_path = rig
        voices = tmp_path / "voices"
        clip = tmp_path / "ref.wav"
        write_clip(clip)
        register_backend("qwen_base", lambda **kw: RecordingQwenBackend("qwen_base"))
        worker = make()
        enrolled = VoiceOp(
            op="add",
            name="MyClone",
            clip_path=str(clip),
            engine="qwen_base",
            ref_text="xin chào các bạn",
            consent=True,
        )
        terminal = run(rig, worker, terminals, make_job("e" * 32, enrolled, None))
        assert terminal.state == "completed"
        assert list_reference_voices(voices) == ["MyClone"]

        clip_path, transcript = load_reference_voice(voices, "MyClone")
        request = TTSRequest(
            text="hello again",
            engine="qwen_base",
            language="en",
            voice="MyClone",
            ref_audio=str(clip_path),
            ref_text=transcript,
        )
        path = tmp_path / "base.wav"
        terminal = run(rig, worker, terminals, make_job("f" * 32, request, path))
        assert terminal.state == "completed"
        frames, rate = validate_wav_artifact(path)
        assert (rate, frames > 0) == (48000, True)

    def test_enroll_without_consent_fails_actionably(self, rig) -> None:
        with pytest.raises(ValueError, match="[Cc]onsent"):
            VoiceOp(
                op="add",
                name="Nope",
                clip_path="/tmp/ref.wav",
                engine="qwen_base",
                ref_text="hi",
                consent=False,
            )

    def test_enroll_without_transcript_fails_actionably(self, rig) -> None:
        with pytest.raises(ValueError, match="ref_text"):
            VoiceOp(
                op="add",
                name="Nope",
                clip_path="/tmp/ref.wav",
                engine="qwen_base",
                consent=True,
            )

    def test_remove_reference(self, rig) -> None:
        make, terminals, tmp_path = rig
        voices = tmp_path / "voices"
        clip = tmp_path / "ref.wav"
        write_clip(clip)
        worker = make()
        enrolled = VoiceOp(
            op="add",
            name="Temp",
            clip_path=str(clip),
            engine="qwen_base",
            ref_text="hi there",
            consent=True,
        )
        assert run(rig, worker, terminals, make_job("e" * 32, enrolled, None)).state == "completed"
        removed = VoiceOp(op="remove", name="Temp", engine="qwen_base")
        assert run(rig, worker, terminals, make_job("f" * 32, removed, None)).state == "completed"
        assert list_reference_voices(voices) == []

    def test_missing_store_fails_actionably(self, rig) -> None:
        make, terminals, tmp_path = rig
        worker = InferenceWorker(FakeVieneu())  # no voices_dir
        worker.terminal.connect(terminals.append)
        worker.start()
        try:
            op = VoiceOp(
                op="add",
                name="Lost",
                clip_path=str(tmp_path / "ref.wav"),
                engine="qwen_base",
                ref_text="hi",
                consent=True,
            )
            terminal = run(rig, worker, terminals, make_job("e" * 32, op, None))
            assert terminal.state == "failed"
            assert "not configured" in (terminal.error or "")
        finally:
            worker.stop()


class TestBaseJobGuards:
    def test_job_without_refs_fails_with_enrollment_hint(self, rig) -> None:
        make, terminals, tmp_path = rig
        register_backend("qwen_base", lambda **kw: RecordingQwenBackend("qwen_base"))
        request = TTSRequest(text="hello", engine="qwen_base", language="en")
        terminal = run(rig, make(), terminals, make_job("c" * 32, request, tmp_path / "x.wav"))
        assert terminal.state == "failed"
        assert "enrolled reference" in (terminal.error or "")

    def test_per_reference_backends_cached(self, rig) -> None:
        make, terminals, tmp_path = rig
        made: list[RecordingQwenBackend] = []
        register_backend(
            "qwen_base", lambda **kw: made.append(RecordingQwenBackend("qwen_base")) or made[-1]
        )
        worker = make()
        first = worker._base_backend_for(ref_audio="/r/one.wav", ref_text="one")  # noqa: SLF001
        second = worker._base_backend_for(ref_audio="/r/one.wav", ref_text="one")  # noqa: SLF001
        other = worker._base_backend_for(ref_audio="/r/two.wav", ref_text="two")  # noqa: SLF001
        assert first is second
        assert other is not first
        assert len(made) == 2

    def test_voiceless_backend_probe_rejected(self, rig) -> None:
        make, _terminals, _tmp = rig
        worker = make()
        with pytest.raises(BackendCapabilityError, match="[Ee]nrolled reference"):
            worker._base_backend_for(ref_audio=None, ref_text=None)  # noqa: SLF001
