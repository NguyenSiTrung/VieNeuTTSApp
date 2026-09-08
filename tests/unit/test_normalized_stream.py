"""Normalized backend chunks through artifact + transport (Phase 2 Task 2)."""

import time
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication  # noqa: E402

from vienetts_app.core.artifacts import SynthesisArtifact, validate_wav_artifact  # noqa: E402
from vienetts_app.core.jobs import JobTerminal, SynthesisJob  # noqa: E402
from vienetts_app.core.models import TTSRequest  # noqa: E402
from vienetts_app.core.pcm_transport import BoundedPcmTransport  # noqa: E402
from vienetts_app.core.resample import normalize_stream  # noqa: E402
from vienetts_app.core.tts_backend import TtsBackend, register_backend  # noqa: E402
from vienetts_app.workers.inference_worker import InferenceWorker  # noqa: E402


def sine(n: int, rate: int) -> np.ndarray:
    t = np.arange(n, dtype=np.float64) / rate
    return (0.5 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.float32)


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


class TestNormalizeStream:
    def test_passthrough_preserves_audio(self) -> None:
        chunks = [sine(15360, 48000), sine(23040, 48000)]
        out = list(normalize_stream(iter(chunks), 48000))
        assert len(out) == 2
        np.testing.assert_array_equal(out[0], chunks[0])
        assert all(c.dtype == np.float32 and c.ndim == 1 for c in out)

    def test_upsample_length_bound(self) -> None:
        chunks = [sine(12000, 24000), sine(12000, 24000)]
        out = list(normalize_stream(iter(chunks), 24000))
        assert abs(sum(len(c) for c in out) - 48000) <= 1

    def test_incremental_first_output_before_exhaustion(self) -> None:
        seen_requests = []

        def source() -> Iterator[np.ndarray]:
            for _ in range(10):
                seen_requests.append(1)
                yield sine(12000, 24000)

        stream = normalize_stream(source(), 24000)
        first = next(stream)
        assert first.size > 0
        assert len(seen_requests) < 10

    def test_skips_empty_chunks(self) -> None:
        chunks = [np.zeros(0, dtype=np.float32), sine(12000, 24000)]
        out = list(normalize_stream(iter(chunks), 24000))
        assert all(c.size > 0 for c in out)
        assert abs(sum(len(c) for c in out) - 24000) <= 1

    def test_non_finite_raises(self) -> None:
        bad = sine(100, 24000)
        bad[5] = np.inf
        with pytest.raises(ValueError, match="finite"):
            list(normalize_stream(iter([bad]), 24000))

    def test_bad_rate_raises(self) -> None:
        with pytest.raises(ValueError, match="src_rate"):
            list(normalize_stream(iter([sine(10, 24000)]), 0))


class FakeQwen24k(TtsBackend):
    """24 kHz backend standing in for the Phase 3 Qwen loaders."""

    engine_id = "qwen_base"
    native_sample_rate = 24000

    def __init__(self) -> None:
        self._open = False

    def initialize(self) -> None:
        self._open = True

    @property
    def is_initialized(self) -> bool:
        return self._open

    def synthesize_stream(
        self,
        text: str,
        *,
        voice: str | None = None,
        language: str | None = None,
        instruction: str | None = None,
        temperature: float | None = None,
    ) -> Iterator[np.ndarray]:
        del voice, language, instruction, temperature
        assert text.strip()
        yield sine(12000, 24000)
        yield sine(12000, 24000)

    def close(self) -> None:
        self._open = False


class RecordingEngine:
    """TTSEngine-shaped duck type for the VieNeu path."""

    def __init__(self) -> None:
        self.requests: list[str] = []

    def initialize(self) -> None:
        pass

    @property
    def is_initialized(self) -> bool:
        return True

    def infer_stream(self, text, voice=None, **kw):
        self.requests.append(text)
        yield sine(15360, 48000)

    def infer_stream_chunked(self, text, voice=None, temperature=None, max_chars=None):
        yield from self.infer_stream(text, voice=voice, temperature=temperature)

    def close(self) -> None:
        pass


def make_job(
    job_id: str, request: TTSRequest, artifact_path: Path, transport: BoundedPcmTransport | None
) -> SynthesisJob:
    return SynthesisJob(
        id=job_id,
        owner="text",  # type: ignore[arg-type]
        kind="interactive",  # type: ignore[arg-type]
        priority=0,
        request=request,
        artifact_path=artifact_path,
        live_transport=transport,
    )


@pytest.fixture
def worker_factory(qcoreapp, tmp_path: Path):
    workers: list[InferenceWorker] = []
    terminals: list[JobTerminal] = []

    def make(engine: Any) -> InferenceWorker:
        worker = InferenceWorker(engine)
        worker.terminal.connect(terminals.append)
        worker.start()
        workers.append(worker)
        return worker

    yield make, terminals, tmp_path
    for worker in workers:
        worker.stop()


class TestWorkerNormalizedPath:
    def test_qwen_24k_job_writes_48k_artifact(self, worker_factory) -> None:
        make, terminals, tmp_path = worker_factory
        register_backend("qwen_base", lambda **kw: FakeQwen24k())
        try:
            transport = BoundedPcmTransport()
            request = TTSRequest(
                text="hello world",
                engine="qwen_base",
                language="en",
                voice="MyClone",
                ref_audio="/refs/myclone.wav",
                ref_text="hello world",
            )
            path = tmp_path / "qwen.wav"
            job = make_job("b" * 32, request, path, transport)
            worker = make(RecordingEngine())
            assert worker.submit(job) is True
            assert wait_until(lambda: any(t.job_id == job.id for t in terminals))
            (terminal,) = [t for t in terminals if t.job_id == job.id]
            assert terminal.state == "completed"
            assert isinstance(terminal.value, SynthesisArtifact)
            frames, rate = validate_wav_artifact(path)
            assert rate == 48000
            assert frames == terminal.value.samples
            assert frames > 0
            assert transport.available_bytes() > 0
        finally:
            register_backend("qwen_base", None)

    def test_unregistered_qwen_fails_actionably(self, worker_factory) -> None:
        make, terminals, tmp_path = worker_factory
        register_backend("qwen_base", None)
        request = TTSRequest(
            text="hello",
            engine="qwen_base",
            language="en",
            voice="MyClone",
            ref_audio="/refs/myclone.wav",
            ref_text="hello",
        )
        job = make_job("c" * 32, request, tmp_path / "missing.wav", None)
        worker = make(RecordingEngine())
        assert worker.submit(job) is True
        assert wait_until(lambda: any(t.job_id == job.id for t in terminals))
        (terminal,) = [t for t in terminals if t.job_id == job.id]
        assert terminal.state == "failed"
        assert "no backend registered" in (terminal.error or "")

    def test_vieneu_path_still_completes(self, worker_factory) -> None:
        make, terminals, tmp_path = worker_factory
        request = TTSRequest(text="Xin chào", engine="vieneu", language="vi")
        path = tmp_path / "vi.wav"
        job = make_job("d" * 32, request, path, None)
        worker = make(RecordingEngine())
        assert worker.submit(job) is True
        assert wait_until(lambda: any(t.job_id == job.id for t in terminals))
        (terminal,) = [t for t in terminals if t.job_id == job.id]
        assert terminal.state == "completed"
        frames, rate = validate_wav_artifact(path)
        assert (frames, rate) == (15360, 48000)
