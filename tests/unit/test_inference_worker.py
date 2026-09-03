"""InferenceWorker: artifact-first terminals and targeted cancellation."""

import threading
import time
from contextvars import ContextVar
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication  # noqa: E402

import vienetts_app.workers.inference_worker as worker_module  # noqa: E402
from vienetts_app.core.artifacts import ArtifactWriteError, SynthesisArtifact  # noqa: E402
from vienetts_app.core.engine import (  # noqa: E402
    TTSEngineError,
    split_text_for_streaming,
)
from vienetts_app.core.jobs import (  # noqa: E402
    JobChunk,
    JobProgress,
    JobTerminal,
    SynthesisJob,
)
from vienetts_app.core.models import TTSRequest, VoiceOp, WarmupOp  # noqa: E402
from vienetts_app.core.pcm_transport import BoundedPcmTransport, TransportClosed  # noqa: E402
from vienetts_app.core.performance import PerformanceRecorder  # noqa: E402
from vienetts_app.workers.inference_worker import InferenceWorker  # noqa: E402

_ARTIFACT_ROOT: ContextVar[Path | None] = ContextVar("artifact_root", default=None)
_DEFAULT_ARTIFACT_PATH = object()


def wait_until(cond, timeout: float = 5.0, interval: float = 0.01) -> bool:
    # Cross-thread signals are queued to the main thread; pump the event loop
    # while polling or callbacks never fire outside a running app.
    app = QCoreApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        if app is not None:
            app.processEvents()
        time.sleep(interval)
    return False


def make_job(
    job_id: str,
    text: str = "hello",
    owner: str = "text",
    kind: str = "interactive",
    mode: str = "stream",
    artifact_path: Path | None | object = _DEFAULT_ARTIFACT_PATH,
    transport: BoundedPcmTransport | None = None,
) -> SynthesisJob:
    if artifact_path is _DEFAULT_ARTIFACT_PATH:
        root = _ARTIFACT_ROOT.get()
        assert root is not None
        artifact_path = root / f"{job_id}.wav"
    assert artifact_path is None or isinstance(artifact_path, Path)
    return SynthesisJob(
        id=job_id,
        owner=owner,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        priority=0,
        request=TTSRequest(text=text, mode=mode, job_id=job_id),  # type: ignore[arg-type]
        artifact_path=artifact_path,
        live_transport=transport,
    )


class RecordingEngine:
    """Stands in for TTSEngine; records the thread every call runs on."""

    def __init__(self, chunks_per_stream: int = 50, chunk_delay: float = 0.005) -> None:
        self.call_threads: list[int] = []
        self.requests: list[str] = []
        self.chunks_per_stream = chunks_per_stream
        self.chunk_delay = chunk_delay
        self.sample_rate = 48_000
        self.backend = "onnx"
        self.voice_calls: list[tuple[str, dict[str, Any]]] = []
        self.persisted_count = 0

    @property
    def single_thread(self) -> bool:
        return len(set(self.call_threads)) == 1

    def _rec(self, text: str) -> None:
        self.call_threads.append(threading.get_ident())
        self.requests.append(text)

    def infer(self, text, voice=None, temperature=None, **kw) -> np.ndarray:
        self._rec(text)
        return np.zeros(48_000, dtype=np.float32)

    def infer_stream(self, text, voice=None, **kw):
        self._rec(text)
        for i in range(self.chunks_per_stream):
            self.call_threads.append(threading.get_ident())
            time.sleep(self.chunk_delay)
            yield np.full(15_360, 0.1 * (i + 1), dtype=np.float32)

    def infer_batch(self, texts, voice=None, **kw) -> list[np.ndarray]:
        for t in texts:
            self._rec(t)
        return [np.zeros(1000, dtype=np.float32) for _ in texts]

    def add_voice(self, name, ref_clip, *, denoise=True, save=False) -> str:
        self.voice_calls.append(
            (
                "add_voice",
                {"name": name, "ref_clip": str(ref_clip), "denoise": denoise, "save": save},
            )
        )
        return name

    def remove_voice(self, name, *, save=False) -> None:
        self.voice_calls.append(("remove_voice", {"name": name, "save": save}))

    def denoise(self, clip_path, out_path=None, max_seconds=None):
        self.voice_calls.append(("denoise", {"clip_path": str(clip_path)}))
        return np.full(44_100, 0.25, dtype=np.float32), 44_100

    def persist_voices(self):
        self.persisted_count += 1

    def close(self) -> None:
        pass


class GateEngine(RecordingEngine):
    """Blocks the first job inside the engine call until released."""

    def __init__(self) -> None:
        super().__init__(chunks_per_stream=1, chunk_delay=0.0)
        self.started = threading.Event()
        self.release = threading.Event()

    def wait_until_started(self, timeout: float = 5.0) -> bool:
        app = QCoreApplication.instance()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.started.is_set():
                return True
            if app is not None:
                app.processEvents()
            time.sleep(0.01)
        return False

    def infer_stream(self, text, voice=None, temperature=None, **kw):
        self._rec(text)
        self.started.set()
        assert self.release.wait(timeout=10), "gate was never released"
        yield np.zeros(100, dtype=np.float32)


class FailingEngine(RecordingEngine):
    def infer(self, text, voice=None, temperature=None, **kw) -> np.ndarray:
        self._rec(text)
        raise TTSEngineError("boom")

    def infer_stream(self, text, voice=None, **kw):
        self._rec(text)
        raise TTSEngineError("boom")
        yield  # pragma: no cover - make this a generator


class StreamOnlyEngine(RecordingEngine):
    """Fails if a tagged TTS job uses a full-array engine entry point."""

    def infer(self, *args, **kwargs) -> np.ndarray:
        raise AssertionError("tagged TTS jobs must use infer_stream")

    def infer_batch(self, *args, **kwargs) -> list[np.ndarray]:
        raise AssertionError("tagged TTS jobs must use infer_stream")


class BackpressureEngine(RecordingEngine):
    """Fills the transport with its first chunk, then blocks on its second."""

    def __init__(self) -> None:
        super().__init__(chunks_per_stream=0, chunk_delay=0.0)
        self.second_chunk_started = threading.Event()

    def infer_stream(self, text, voice=None, **kw):
        self._rec(text)
        yield np.ones(4, dtype=np.float32)
        self.second_chunk_started.set()
        yield np.ones(4, dtype=np.float32)


class MalformedChunkEngine(RecordingEngine):
    def infer_stream(self, text, voice=None, **kw):
        self._rec(text)
        yield np.ones((2, 2), dtype=np.float32)


class EmptyChunkEngine(RecordingEngine):
    def infer_stream(self, text, voice=None, **kw):
        self._rec(text)
        yield np.array([], dtype=np.float32)


class InitializingEngine(RecordingEngine):
    def __init__(self) -> None:
        super().__init__(chunks_per_stream=1, chunk_delay=0.0)
        self.initialized = 0

    def initialize(self) -> None:
        self.initialized += 1


class FailingInitEngine(RecordingEngine):
    def initialize(self) -> None:
        raise TTSEngineError("weights missing")


class WorkerHarness:
    def __init__(
        self, engine: Any, performance_recorder: PerformanceRecorder | None = None
    ) -> None:
        self.engine = engine
        self.progresses: list[Any] = []
        self.chunks: list[Any] = []
        self.terminals: list[JobTerminal] = []
        self.worker = InferenceWorker(engine, performance_recorder=performance_recorder)
        self.worker.progress.connect(self.progresses.append)
        self.worker.chunk_ready.connect(self.chunks.append)
        self.worker.terminal.connect(self.terminals.append)
        self.worker.start()

    def wait_terminal(self, job_id: str, timeout: float = 10.0) -> bool:
        return wait_until(lambda: any(t.job_id == job_id for t in self.terminals), timeout)

    def terminals_for(self, job_id: str) -> list[JobTerminal]:
        return [t for t in self.terminals if t.job_id == job_id]

    def tagged_progresses(self) -> list[JobProgress]:
        return [p for p in self.progresses if isinstance(p, JobProgress)]

    def tagged_chunks(self) -> list[JobChunk]:
        return [c for c in self.chunks if isinstance(c, JobChunk)]


@pytest.fixture
def harness(qcoreapp, tmp_path: Path):
    # qcoreapp: cross-thread delivery needs the session event loop; without
    # it wait_until only sleeps and queued slots never fire.
    created: list[WorkerHarness] = []
    token = _ARTIFACT_ROOT.set(tmp_path / "artifacts")

    def make(engine: Any, performance_recorder: PerformanceRecorder | None = None) -> WorkerHarness:
        h = WorkerHarness(engine, performance_recorder=performance_recorder)
        created.append(h)
        return h

    yield make
    for h in created:
        h.worker.stop()
    _ARTIFACT_ROOT.reset(token)


# ── admission and ordering ────────────────────────────────────────────────


def test_jobs_complete_in_fifo_order_with_one_terminal_each(harness) -> None:
    h = harness(RecordingEngine(chunks_per_stream=1, chunk_delay=0.0))
    jobs = [make_job(f"{n:032x}", text=text, mode="infer") for n, text in enumerate("abc", 1)]
    for job in jobs:
        assert h.worker.submit(job) is True

    assert all(h.wait_terminal(job.id) for job in jobs)
    assert [t.job_id for t in h.terminals] == [job.id for job in jobs]
    assert all(t.state == "completed" for t in h.terminals)
    assert h.engine.requests == ["a", "b", "c"]
    assert h.engine.single_thread


def test_stream_progress_and_chunks_carry_the_job_id(harness) -> None:
    h = harness(RecordingEngine(chunks_per_stream=5, chunk_delay=0.0))
    job = make_job("a" * 32, mode="stream")
    h.worker.submit(job)

    assert h.wait_terminal(job.id)
    assert h.tagged_progresses(), "expected tagged progress events"
    assert all(p.job_id == job.id for p in h.tagged_progresses())
    assert all(c.job_id == job.id for c in h.tagged_chunks())
    assert sum(c.sample_count for c in h.tagged_chunks()) == 5 * 15_360
    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "completed"
    assert isinstance(terminal.value, SynthesisArtifact)
    assert terminal.value.samples == 5 * 15_360


def test_stream_job_returns_committed_artifact_when_path_supplied(harness, tmp_path: Path) -> None:
    h = harness(RecordingEngine(chunks_per_stream=1, chunk_delay=0.0))
    destination = tmp_path / "stream.wav"
    job = make_job("1" * 32, artifact_path=destination)

    assert h.worker.submit(job) is True
    assert h.wait_terminal(job.id)

    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "completed"
    assert isinstance(terminal.value, SynthesisArtifact)
    assert terminal.value.path == destination
    assert terminal.value.samples == 15_360
    assert destination.is_file()


def test_stream_job_applies_silence_p_between_segments(harness, tmp_path: Path) -> None:
    text = "A" * 300 + ". " + "B" * 300 + "."
    h = harness(RecordingEngine(chunks_per_stream=1, chunk_delay=0.0))
    destination = tmp_path / "silence_test.wav"
    request = TTSRequest(text=text, mode="stream", job_id="2" * 32, silence_p=0.1)
    job = SynthesisJob(
        id="2" * 32,
        owner="text",
        kind="interactive",
        priority=0,
        request=request,
        artifact_path=destination,
    )
    assert h.worker.submit(job) is True
    assert h.wait_terminal(job.id)

    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "completed"
    assert isinstance(terminal.value, SynthesisArtifact)
    expected_samples = 2 * 15_360 + int(48_000 * 0.1)
    assert terminal.value.samples == expected_samples


def test_stream_job_applies_speed_stretch(harness, tmp_path: Path) -> None:
    text = "Single sentence."
    h = harness(RecordingEngine(chunks_per_stream=1, chunk_delay=0.0))
    destination = tmp_path / "speed_test.wav"
    request = TTSRequest(text=text, mode="stream", job_id="3" * 32, speed=1.5)
    job = SynthesisJob(
        id="3" * 32,
        owner="text",
        kind="interactive",
        priority=0,
        request=request,
        artifact_path=destination,
    )
    assert h.worker.submit(job) is True
    assert h.wait_terminal(job.id)

    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "completed"
    assert isinstance(terminal.value, SynthesisArtifact)
    assert abs(terminal.value.samples - 10_240) < 150


def test_tts_job_without_artifact_path_is_rejected_before_engine_invocation(harness) -> None:
    engine = RecordingEngine(chunks_per_stream=1, chunk_delay=0.0)
    h = harness(engine)
    job = make_job("2" * 32, artifact_path=None)

    assert h.worker.submit(job) is False
    assert engine.requests == []
    assert h.terminals == []


def test_non_stream_tts_job_uses_artifact_streaming_path(harness, tmp_path: Path) -> None:
    h = harness(StreamOnlyEngine(chunks_per_stream=1, chunk_delay=0.0))
    job = make_job("3" * 32, mode="infer", artifact_path=tmp_path / "infer.wav")

    assert h.worker.submit(job) is True
    assert h.wait_terminal(job.id)

    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "completed"
    assert isinstance(terminal.value, SynthesisArtifact)
    assert terminal.value.path.is_file()


def test_stream_chunk_metadata_is_coalesced_and_final_total_is_exact(
    harness, tmp_path: Path
) -> None:
    h = harness(RecordingEngine(chunks_per_stream=5, chunk_delay=0.0))
    h.worker._monotonic_ns = lambda: 0
    job = make_job("4" * 32, artifact_path=tmp_path / "rate-limited.wav")

    assert h.worker.submit(job) is True
    assert h.wait_terminal(job.id)

    chunks = h.tagged_chunks()
    assert [chunk.sample_count for chunk in chunks] == [15_360, 61_440]
    assert [chunk.peak for chunk in chunks] == pytest.approx([0.1, 0.5])


def test_cancelling_while_transport_is_full_terminalizes_once(harness, tmp_path: Path) -> None:
    engine = BackpressureEngine()
    recorder = PerformanceRecorder(enabled=True)
    h = harness(engine, performance_recorder=recorder)
    transport = BoundedPcmTransport(capacity_bytes=16)
    destination = tmp_path / "cancelled.wav"
    job = make_job("5" * 32, artifact_path=destination, transport=transport)
    recorder.begin(job.id, {"mode": "stream", "streaming": True})

    assert h.worker.submit(job) is True
    assert engine.second_chunk_started.wait(timeout=1)
    assert transport.available_bytes() == 16

    assert h.worker.cancel_job(job.id) is True
    assert h.wait_terminal(job.id)

    assert [terminal.state for terminal in h.terminals_for(job.id)] == ["cancelled"]
    assert [event["name"] for event in recorder.snapshot(job.id)[0]["events"]].count(
        "worker_cancelled"
    ) == 1
    assert not destination.exists()
    assert transport.available_bytes() == 0


def test_malformed_stream_chunk_fails_without_artifact(harness, tmp_path: Path) -> None:
    h = harness(MalformedChunkEngine(chunks_per_stream=0, chunk_delay=0.0))
    destination = tmp_path / "malformed.wav"
    job = make_job("6" * 32, artifact_path=destination)

    assert h.worker.submit(job) is True
    assert h.wait_terminal(job.id)

    assert [terminal.state for terminal in h.terminals_for(job.id)] == ["failed"]
    assert not destination.exists()
    assert not destination.with_name("malformed.part.wav").exists()


def test_writer_failure_terminalizes_once_and_discards_transport(
    harness, tmp_path: Path, monkeypatch
) -> None:
    original_writer = worker_module.IncrementalArtifactWriter

    class FailingSecondAppendWriter(original_writer):
        def __init__(self, *args, **kwargs) -> None:
            super().__init__(*args, **kwargs)
            self._append_count = 0

        def append(self, samples: object) -> int:
            self._append_count += 1
            if self._append_count == 2:
                raise ArtifactWriteError("injected writer failure")
            return super().append(samples)

    monkeypatch.setattr(worker_module, "IncrementalArtifactWriter", FailingSecondAppendWriter)
    h = harness(RecordingEngine(chunks_per_stream=2, chunk_delay=0.0))
    transport = BoundedPcmTransport(capacity_bytes=100_000)
    destination = tmp_path / "writer-failure.wav"
    job = make_job("8" * 32, artifact_path=destination, transport=transport)

    assert h.worker.submit(job) is True
    assert h.wait_terminal(job.id)

    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "failed"
    assert terminal.error == "injected writer failure"
    assert not destination.exists()
    assert not destination.with_name("writer-failure.part.wav").exists()
    assert transport.available_bytes() == 0
    with pytest.raises(TransportClosed):
        transport.take(1)


def test_worker_never_concatenates_long_stream_audio(harness, tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        np,
        "concatenate",
        lambda *_args, **_kwargs: pytest.fail("worker must not concatenate stream audio"),
    )
    h = harness(RecordingEngine(chunks_per_stream=400, chunk_delay=0.0))
    job = make_job("9" * 32, artifact_path=tmp_path / "long.wav")

    assert h.worker.submit(job) is True
    assert h.wait_terminal(job.id)

    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "completed"
    assert isinstance(terminal.value, SynthesisArtifact)
    assert terminal.value.samples == 400 * 15_360
    assert terminal.value.duration_ms == int(400 * 15_360 * 1000 / 48_000)


def test_stream_artifact_records_disk_and_transport_bounds(harness, tmp_path: Path) -> None:
    recorder = PerformanceRecorder(enabled=True)
    h = harness(
        RecordingEngine(chunks_per_stream=2, chunk_delay=0.0),
        performance_recorder=recorder,
    )
    transport = BoundedPcmTransport(capacity_bytes=200_000)
    destination = tmp_path / "metrics.wav"
    job = make_job("7" * 32, artifact_path=destination, transport=transport)
    recorder.begin(job.id, {"mode": "stream", "streaming": True})

    assert h.worker.submit(job) is True
    assert h.wait_terminal(job.id)

    (trace,) = recorder.snapshot(job.id)
    names = [event["name"] for event in trace["events"]]
    maxima = trace["maxima"]
    assert names.index("worker_first_chunk") < names.index("worker_completed")
    assert names.count("worker_first_chunk") == 1
    assert names.count("audio_first_buffer_append") == 1
    assert names.count("worker_completed") == 1
    assert maxima["artifact_samples"] == 30_720
    assert maxima["artifact_bytes_on_disk"] == destination.stat().st_size
    assert maxima["transport_max_bytes"] == 122_880


def test_empty_stream_chunk_does_not_record_transport_append(harness, tmp_path: Path) -> None:
    recorder = PerformanceRecorder(enabled=True)
    h = harness(EmptyChunkEngine(), performance_recorder=recorder)
    transport = BoundedPcmTransport(capacity_bytes=16)
    job = make_job("e" * 32, artifact_path=tmp_path / "empty.wav", transport=transport)
    recorder.begin(job.id, {"mode": "stream", "streaming": True})

    assert h.worker.submit(job) is True
    assert h.wait_terminal(job.id)

    (trace,) = recorder.snapshot(job.id)
    names = [event["name"] for event in trace["events"]]
    assert "audio_first_buffer_append" not in names


def test_infer_multi_segment_reports_segment_progress(harness) -> None:
    text = "Xin chào. " * 200
    segments = split_text_for_streaming(text)
    assert len(segments) > 1
    h = harness(RecordingEngine())
    job = make_job("b" * 32, text=text, mode="infer")
    h.worker.submit(job)

    assert h.wait_terminal(job.id)
    assert h.engine.requests == segments
    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "completed"


# ── targeted cancellation and exactly-once terminals ──────────────────────


def test_worker_emits_one_tagged_terminal_for_queued_cancellation(harness) -> None:
    h = harness(GateEngine())
    first = make_job("a" * 32, text="first", mode="infer")
    second = make_job("b" * 32, text="second", mode="infer")
    h.worker.submit(first)
    h.worker.submit(second)
    assert h.engine.wait_until_started()

    assert h.worker.cancel_job(second.id) is True

    assert h.wait_terminal(second.id)
    (terminal,) = h.terminals_for(second.id)
    assert terminal.state == "cancelled"
    assert terminal.error == ""
    assert h.engine.requests == ["first"]
    h.engine.release.set()
    assert h.wait_terminal(first.id)
    assert h.terminals_for(first.id)[0].state == "completed"


def test_active_cancellation_does_not_cancel_queued_job(harness) -> None:
    h = harness(RecordingEngine(chunks_per_stream=100, chunk_delay=0.002))
    first = make_job("a" * 32, text="first", mode="stream")
    second = make_job("b" * 32, text="second", mode="infer")
    h.worker.submit(first)
    h.worker.submit(second)
    assert wait_until(lambda: len(h.tagged_chunks()) >= 2, timeout=5.0)

    assert h.worker.cancel_job(first.id) is True

    assert h.wait_terminal(first.id, timeout=15.0)
    assert h.wait_terminal(second.id, timeout=15.0)
    assert [t.state for t in h.terminals_for(first.id)] == ["cancelled"]
    assert [t.state for t in h.terminals_for(second.id)] == ["completed"]
    assert "second" in h.engine.requests


def test_completed_job_cannot_terminalize_twice(harness) -> None:
    h = harness(RecordingEngine(chunks_per_stream=1, chunk_delay=0.0))
    job = make_job("a" * 32, mode="infer")
    h.worker.submit(job)
    assert h.wait_terminal(job.id)

    assert h.worker.cancel_job(job.id) is False

    app = QCoreApplication.instance()
    for _ in range(5):
        if app is not None:
            app.processEvents()
        time.sleep(0.01)
    assert len(h.terminals_for(job.id)) == 1


def test_engine_exception_produces_one_failed_terminal(harness) -> None:
    h = harness(FailingEngine())
    job = make_job("c" * 32, mode="infer")
    h.worker.submit(job)

    assert h.wait_terminal(job.id)
    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "failed"
    assert "boom" in terminal.error


def test_stop_terminalizes_pending_jobs_exactly_once(harness) -> None:
    h = harness(RecordingEngine(chunks_per_stream=1000, chunk_delay=0.002))
    first = make_job("a" * 32, text="first", mode="stream")
    second = make_job("b" * 32, text="second", mode="infer")
    third = make_job("c" * 32, text="third", mode="infer")
    for job in (first, second, third):
        h.worker.submit(job)
    assert wait_until(lambda: len(h.tagged_chunks()) >= 1, timeout=5.0)

    assert h.worker.stop() is True

    assert wait_until(lambda: len(h.terminals) >= 3, timeout=10.0)
    for job in (first, second, third):
        terminals = h.terminals_for(job.id)
        assert [t.state for t in terminals] == ["cancelled"], job.id


def test_submit_after_stop_is_rejected_without_event(harness) -> None:
    h = harness(RecordingEngine())
    assert h.worker.stop() is True

    assert h.worker.submit(make_job("d" * 32)) is False

    app = QCoreApplication.instance()
    for _ in range(5):
        if app is not None:
            app.processEvents()
        time.sleep(0.01)
    assert h.terminals == []


def test_cancel_unknown_job_returns_false(harness) -> None:
    h = harness(RecordingEngine())
    assert h.worker.cancel_job("e" * 32) is False


def test_cancel_owner_leaves_other_owners_in_fifo_order(harness) -> None:
    h = harness(GateEngine())
    text = make_job("a" * 32, text="text", owner="text", mode="infer")
    book_a = make_job("b" * 32, text="book a", owner="audiobook", mode="infer")
    cloning = make_job("c" * 32, text="cloning", owner="cloning", mode="infer")
    book_b = make_job("d" * 32, text="book b", owner="audiobook", mode="infer")
    for job in (text, book_a, cloning, book_b):
        h.worker.submit(job)
    assert h.engine.wait_until_started()

    assert h.worker.cancel_owner("audiobook") == 2

    for job in (book_a, book_b):
        assert h.wait_terminal(job.id)
        assert [t.state for t in h.terminals_for(job.id)] == ["cancelled"]
    h.engine.release.set()
    assert h.wait_terminal(text.id)
    assert h.wait_terminal(cloning.id)
    assert [t.state for t in h.terminals_for(text.id)] == ["completed"]
    assert [t.state for t in h.terminals_for(cloning.id)] == ["completed"]
    assert h.engine.requests == ["text", "cloning"]


# ── warmup, voice ops, batch (migrated coverage) ──────────────────────────


def test_warmup_is_silent_and_preserves_order(harness) -> None:
    engine = InitializingEngine()
    h = harness(engine)
    job = make_job("a" * 32, mode="infer")
    h.worker.submit(WarmupOp())
    h.worker.submit(job)

    assert h.wait_terminal(job.id)
    assert engine.initialized == 1
    assert len(h.terminals) == 1  # warmup itself emitted nothing
    assert [t.state for t in h.terminals_for(job.id)] == ["completed"]
    assert engine.requests == ["hello"]


def test_warmup_failure_is_silent(harness) -> None:
    h = harness(FailingInitEngine())
    job = make_job("b" * 32, mode="infer")
    h.worker.submit(WarmupOp())
    h.worker.submit(job)

    assert h.wait_terminal(job.id)
    assert [t.state for t in h.terminals_for(job.id)] == ["completed"]


def test_voice_op_job_emits_completed_terminal_with_op_value(harness) -> None:
    h = harness(RecordingEngine())
    job = SynthesisJob(
        id="f" * 32,
        owner="cloning",
        kind="voice_op",
        priority=0,
        request=VoiceOp(op="remove", name="Doomed"),
    )
    h.worker.submit(job)

    assert h.wait_terminal(job.id)
    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "completed"
    assert terminal.value == {"op": "remove", "name": "Doomed"}
    assert h.engine.voice_calls == [("remove_voice", {"name": "Doomed", "save": False})]


def test_batch_mode_job_terminal_carries_artifact(harness) -> None:
    h = harness(RecordingEngine())
    job = make_job("a" * 32, mode="batch")
    h.worker.submit(job)

    assert h.wait_terminal(job.id)
    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "completed"
    assert isinstance(terminal.value, SynthesisArtifact)
    assert terminal.value.path.is_file()


@pytest.mark.parametrize(
    "payload",
    [
        TTSRequest(text="untagged"),
        VoiceOp(op="remove", name="Doomed"),
    ],
)
def test_untagged_worker_payload_is_rejected_without_signals(harness, payload: object) -> None:
    h = harness(RecordingEngine())

    assert h.worker.submit(payload) is False  # type: ignore[arg-type]
    assert h.engine.requests == []
    assert h.terminals == []
