"""InferenceWorker: artifact-first terminals and targeted cancellation."""

import threading
import time
from collections.abc import Iterator
from contextvars import ContextVar
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from tests.unit import qwen_host_fake as host_fake

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication  # noqa: E402

import vienetts_app.workers.inference_worker as worker_module  # noqa: E402
from vienetts_app.core.artifacts import (  # noqa: E402
    ArtifactWriteError,
    SynthesisArtifact,
    validate_wav_artifact,
)
from vienetts_app.core.engine import (  # noqa: E402
    EngineProviders,
    TTSEngineError,
    VieNeuProvider,
    split_text_for_streaming,
)
from vienetts_app.core.engine_profiles import QWEN_BASE, QWEN_CUSTOM, VIENEU  # noqa: E402
from vienetts_app.core.jobs import (  # noqa: E402
    JobChunk,
    JobProgress,
    JobTerminal,
    SynthesisJob,
)
from vienetts_app.core.models import TTSRequest, VoiceOp, WarmupOp  # noqa: E402
from vienetts_app.core.pcm_transport import BoundedPcmTransport, TransportClosed  # noqa: E402
from vienetts_app.core.performance import PerformanceRecorder  # noqa: E402
from vienetts_app.core.qwen_engine import (  # noqa: E402
    QwenEngine,
    QwenEngineCancelled,
    QwenEngineProvider,
)
from vienetts_app.core.qwen_protocol import MAX_TEXT_CHARS  # noqa: E402
from vienetts_app.core.synthesis_context import (  # noqa: E402
    SynthesisContext,
    context_for,
)
from vienetts_app.core.text_segmentation import (  # noqa: E402
    segment_limit_for,
    split_text_for_profile,
)
from vienetts_app.core.voice_profiles import CloneStore  # noqa: E402
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
    context: SynthesisContext | None = None,
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
        request=TTSRequest(text=text, mode=mode, job_id=job_id, context=context),  # type: ignore[arg-type]
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
        self,
        engine: Any,
        performance_recorder: PerformanceRecorder | None = None,
        providers: EngineProviders | None = None,
    ) -> None:
        self.engine = engine
        self.progresses: list[Any] = []
        self.chunks: list[Any] = []
        self.terminals: list[JobTerminal] = []
        self.worker = InferenceWorker(
            engine, performance_recorder=performance_recorder, providers=providers
        )
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

    def make(
        engine: Any,
        performance_recorder: PerformanceRecorder | None = None,
        providers: EngineProviders | None = None,
    ) -> WorkerHarness:
        h = WorkerHarness(engine, performance_recorder=performance_recorder, providers=providers)
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


@pytest.mark.parametrize(
    ("text", "request_kwargs", "expected_samples", "tolerance"),
    [
        pytest.param(
            "A" * 300 + ". " + "B" * 300 + ".",
            {"silence_p": 0.1},
            2 * 15_360 + int(48_000 * 0.1),
            0,
            id="silence-p-between-segments",
        ),
        pytest.param(
            "Single sentence.",
            {"speed": 1.5},
            10_240,
            150,
            id="speed-stretch",
        ),
    ],
)
def test_stream_job_applies_request_options(
    harness,
    tmp_path: Path,
    text: str,
    request_kwargs: dict,
    expected_samples: int,
    tolerance: int,
) -> None:
    h = harness(RecordingEngine(chunks_per_stream=1, chunk_delay=0.0))
    request = TTSRequest(text=text, mode="stream", job_id="2" * 32, **request_kwargs)
    job = SynthesisJob(
        id="2" * 32,
        owner="text",
        kind="interactive",
        priority=0,
        request=request,
        artifact_path=tmp_path / "stream_options.wav",
    )
    assert h.worker.submit(job) is True
    assert h.wait_terminal(job.id)

    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "completed"
    assert isinstance(terminal.value, SynthesisArtifact)
    assert abs(terminal.value.samples - expected_samples) <= tolerance


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
    h = harness(RecordingEngine(chunks_per_stream=2, chunk_delay=0.0))
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
        time.sleep(0.001)
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
        time.sleep(0.001)
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


def test_warmup_is_silent_and_does_not_block_jobs(harness) -> None:
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

    h = harness(FailingInitEngine())
    job = make_job("b" * 32, mode="infer")
    h.worker.submit(WarmupOp())
    h.worker.submit(job)

    assert h.wait_terminal(job.id)
    assert [t.state for t in h.terminals_for(job.id)] == ["completed"]


def test_non_tts_job_kinds_emit_completed_terminals(harness) -> None:
    h = harness(RecordingEngine())
    voice_job = SynthesisJob(
        id="f" * 32,
        owner="cloning",
        kind="voice_op",
        priority=0,
        request=VoiceOp(op="remove", name="Doomed"),
    )
    h.worker.submit(voice_job)

    assert h.wait_terminal(voice_job.id)
    (terminal,) = h.terminals_for(voice_job.id)
    assert terminal.state == "completed"
    assert terminal.value == {"op": "remove", "name": "Doomed"}
    assert h.engine.voice_calls == [("remove_voice", {"name": "Doomed", "save": False})]

    batch_job = make_job("a" * 32, mode="batch")
    h.worker.submit(batch_job)

    assert h.wait_terminal(batch_job.id)
    (terminal,) = h.terminals_for(batch_job.id)
    assert terminal.state == "completed"
    assert isinstance(terminal.value, SynthesisArtifact)
    assert terminal.value.path.is_file()


@pytest.mark.parametrize(
    "payload",
    [
        pytest.param(TTSRequest(text="untagged"), id="untagged-tts-request"),
        pytest.param(VoiceOp(op="remove", name="Doomed"), id="untagged-voice-op"),
        pytest.param(make_job("2" * 32, artifact_path=None), id="tts-job-without-artifact-path"),
    ],
)
def test_invalid_worker_payload_is_rejected_without_signals(harness, payload: object) -> None:
    h = harness(RecordingEngine())

    assert h.worker.submit(payload) is False  # type: ignore[arg-type]
    assert h.engine.requests == []
    assert h.terminals == []


def test_cancel_tracking_aborts_dequeued_job(harness) -> None:
    h = harness(RecordingEngine(chunks_per_stream=5, chunk_delay=0.01))
    job = make_job("9" * 32)
    with h.worker._cancel_lock:
        h.worker._cancel_requested_ids.add(job.id)
    h.worker.submit(job)
    assert h.wait_terminal(job.id)
    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "cancelled"


def test_speed_path_stretches_per_chunk_without_concatenation(
    harness, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        np,
        "concatenate",
        lambda *_args, **_kwargs: pytest.fail("speed path must not concatenate"),
    )
    h = harness(RecordingEngine(chunks_per_stream=3, chunk_delay=0.0))
    request = TTSRequest(text="Single sentence.", mode="stream", job_id="a" * 32, speed=1.5)
    job = SynthesisJob(
        id="a" * 32,
        owner="text",
        kind="interactive",
        priority=0,
        request=request,
        artifact_path=tmp_path / "speedy.wav",
    )
    assert h.worker.submit(job) is True
    assert h.wait_terminal(job.id)

    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "completed"
    assert isinstance(terminal.value, SynthesisArtifact)
    # 3 chunks x 15_360 samples at 1.5x ~ 30_720, tolerant to WSOLA framing.
    assert abs(terminal.value.samples - 30_720) <= 3 * 150


def test_retire_registries_stay_bounded(harness) -> None:
    h = harness(RecordingEngine())
    worker = h.worker
    for n in range(5_000):
        assert worker.cancel_job(f"{n:032x}") is False
    assert len(worker._cancel_requested_ids) <= worker_module._RETIRED_ID_RETAIN

    for n in range(5_000):
        assert worker._terminalize(make_job(f"{(n + 10_000):032x}"), "cancelled") is True
    assert len(worker._terminal_ids) <= worker_module._RETIRED_ID_RETAIN


# ── engine providers: the Qwen profile through the worker pipeline ────────


def qwen_providers(provider: Any) -> EngineProviders:
    """A Qwen-only set: no VieNeu provider, so a stray VieNeu job fails loudly."""
    return EngineProviders(by_profile={QWEN_CUSTOM: provider}, default=provider)


def qwen_context(language: str = "zh", voice_id: str = "Vivian") -> SynthesisContext:
    return context_for(QWEN_CUSTOM, language=language, voice_id=voice_id)


def part_path_of(job: SynthesisJob) -> Path:
    assert job.artifact_path is not None
    return job.artifact_path.parent / (job.artifact_path.stem + ".part.wav")


class QwenProviderDouble:
    """Stands in for QwenEngineProvider: records segments, honours cancels."""

    def __init__(
        self,
        *,
        chunks_per_segment: int = 2,
        block: bool = False,
        profile: str = QWEN_CUSTOM,
    ) -> None:
        self.profile = profile
        self.chunks_per_segment = chunks_per_segment
        self.segments: list[tuple[str, SynthesisContext | None, str]] = []
        self.cancels: list[str] = []
        self.voice_ops: list[VoiceOp] = []
        self.cancelled = False
        self.initialized = 0
        self.closed = 0
        self.started = threading.Event()
        self._block = block
        self._released = threading.Event()

    @property
    def is_initialized(self) -> bool:
        return bool(self.initialized)

    def initialize(self) -> None:
        self.initialized += 1

    def infer_stream(self, text, *, context=None, voice=None, temperature=None, job_id=""):
        self.segments.append((text, context, job_id))
        self.started.set()
        if self._block:
            # A generation only the provider itself can interrupt: the worker's
            # per-job event cannot break into a running model call.
            assert self._released.wait(timeout=10), "the provider was never released"
            if self.cancelled:
                raise QwenEngineCancelled("generation cancelled")
        for index in range(self.chunks_per_segment):
            yield np.full(1024, 0.1 * (index + 1), dtype=np.float32)

    def cancel(self, job_id: str) -> bool:
        self.cancels.append(job_id)
        self.cancelled = True
        self._released.set()
        return True

    def voice_op(self, op: VoiceOp) -> dict[str, Any]:
        self.voice_ops.append(op)
        return {"op": op.op, "name": op.name, "cloneId": f"{self.profile}-clone"}

    def release(self) -> None:
        """Unblock a blocked generation without recording a cancel (test helper)."""
        self._released.set()

    def close(self) -> None:
        self.closed += 1


class SlowCancelProvider(QwenProviderDouble):
    """A provider whose in-engine cancel blocks until the test releases it.

    Stands for the real Qwen engine's cancel: it asks the host to stop and
    then waits for the running (uninterruptible) generate to settle, which
    for a long text can take minutes.
    """

    def __init__(self) -> None:
        super().__init__(block=True)
        self.cancel_entered = threading.Event()
        self._cancel_gate = threading.Event()

    def cancel(self, job_id: str) -> bool:
        self.cancel_entered.set()
        assert self._cancel_gate.wait(timeout=10), "the provider cancel was never released"
        return super().cancel(job_id)

    def release_cancel(self) -> None:
        """Let the blocked provider cancel finish (test helper)."""
        self._cancel_gate.set()


@pytest.fixture
def qwen_engines() -> Iterator[list[QwenEngine]]:
    created: list[QwenEngine] = []
    yield created
    for engine in created:
        engine.close()


def real_qwen_provider(engines: list[QwenEngine], tmp_path: Path, mode: str) -> QwenEngineProvider:
    engine = host_fake.engine_for(tmp_path, mode)
    engines.append(engine)
    return QwenEngineProvider(engine)


class BatchingProviderDouble(QwenProviderDouble):
    """A provider that batches whole segment lists (the export fast path)."""

    def __init__(self, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self.batch_calls: list[list[str]] = []

    def infer_stream_segments(
        self, texts, *, context=None, voice=None, temperature=None, job_id=""
    ):
        batch = [str(text) for text in texts]
        self.batch_calls.append(batch)
        self.started.set()
        for index, text in enumerate(batch):
            self.segments.append((text, context, job_id))
            for chunk_index in range(self.chunks_per_segment):
                yield index, np.full(1024, 0.1 * (chunk_index + 1), dtype=np.float32)


def test_export_jobs_synthesize_segments_in_batches(harness) -> None:
    provider = BatchingProviderDouble(chunks_per_segment=2)
    h = harness(None, providers=qwen_providers(provider))
    # Well past the 512-char segment cap: several segments in one export job.
    text = "".join(f"这是第{index}个句子。" for index in range(100))
    job = make_job("b" * 32, text=text, context=qwen_context())

    assert h.worker.submit(job) is True
    assert h.wait_terminal(job.id)

    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "completed"
    assert provider.batch_calls, "an export job must go through the batcher"
    batched = [text for batch in provider.batch_calls for text in batch]
    single = [text for text, _context, _job_id in provider.segments]
    assert batched == single
    assert "".join(single) == text, "batching must preserve segment order end to end"
    assert terminal.value.samples == len(single) * 2 * 1024
    assert validate_wav_artifact(terminal.value.path) == (terminal.value.samples, 48_000)


def test_live_streaming_jobs_stay_one_segment_at_a_time(harness) -> None:
    provider = BatchingProviderDouble(chunks_per_segment=1)
    h = harness(None, providers=qwen_providers(provider))
    transport = BoundedPcmTransport(capacity_bytes=200_000)
    job = make_job("c" * 32, text="你好。" * 200, context=qwen_context(), transport=transport)

    assert h.worker.submit(job) is True
    assert h.wait_terminal(job.id)

    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "completed"
    assert provider.batch_calls == [], "a live stream must never batch segments"
    assert len(provider.segments) > 1, "the job must still be segmented"


def test_a_qwen_job_is_served_by_its_own_provider(harness) -> None:
    vieneu = RecordingEngine(chunks_per_stream=1, chunk_delay=0.0)
    vieneu_provider = VieNeuProvider(vieneu)
    provider = QwenProviderDouble(chunks_per_segment=3)
    h = harness(
        vieneu,
        providers=EngineProviders(
            by_profile={QWEN_CUSTOM: provider, VIENEU: vieneu_provider},
            default=vieneu_provider,
        ),
    )
    text = "你好。" * 200  # > one segment at the profile cap
    job = make_job("a" * 32, text=text, context=qwen_context())

    assert h.worker.submit(job) is True
    assert h.wait_terminal(job.id)

    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "completed"
    assert isinstance(terminal.value, SynthesisArtifact)
    # Every segment of the job goes to the Qwen provider, tagged with this
    # worker's job id (the provider owns the per-segment ids the host settles).
    assert len(provider.segments) > 1
    assert [job_id for _text, _context, job_id in provider.segments] == [job.id] * len(
        provider.segments
    )
    assert all(context is job.request.context for _text, context, _job_id in provider.segments)
    assert "".join(segment for segment, _context, _job_id in provider.segments) == text
    assert terminal.value.samples == len(provider.segments) * 3 * 1024
    assert validate_wav_artifact(terminal.value.path) == (terminal.value.samples, 48_000)
    assert vieneu.requests == [], "a Qwen job must not touch the VieNeu engine"


def test_a_qwen_job_without_a_registered_provider_fails_cleanly(harness) -> None:
    h = harness(RecordingEngine())
    job = make_job("a" * 32, text="你好。", context=qwen_context())

    assert h.worker.submit(job) is True
    assert h.wait_terminal(job.id)

    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "failed"
    assert "not available in this worker" in terminal.error
    assert not job.artifact_path.exists()
    assert not part_path_of(job).exists(), "no writer may be created for an unroutable job"
    assert h.engine.requests == []


def test_qwen_segments_are_sentence_bounded_and_never_split_mid_sentence(harness) -> None:
    provider = QwenProviderDouble(chunks_per_segment=1)
    h = harness(None, providers=qwen_providers(provider))
    long_text = "".join(f"这是第{index}个句子。" for index in range(200))
    job = make_job("a" * 32, text=long_text, context=qwen_context())

    assert h.worker.submit(job) is True
    assert h.wait_terminal(job.id)

    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "completed"
    segments = [text for text, _context, _job_id in provider.segments]
    limit = segment_limit_for(QWEN_CUSTOM)
    assert len(segments) == len(split_text_for_profile(long_text, "zh", limit)) > 1
    assert all(len(segment) <= limit <= MAX_TEXT_CHARS for segment in segments)
    assert all(segment.endswith("。") for segment in segments)
    assert all(" " not in segment for segment in segments), "no spaces may be inserted"
    assert "".join(segments) == long_text


def test_cancel_job_reaches_a_running_qwen_generation(harness) -> None:
    provider = QwenProviderDouble(block=True)
    h = harness(None, providers=qwen_providers(provider))
    job = make_job("a" * 32, text="你好。", context=qwen_context())

    assert h.worker.submit(job) is True
    assert wait_until(provider.started.is_set), "the generation never started"
    assert h.worker.cancel_job(job.id) is True
    assert h.wait_terminal(job.id)

    (terminal,) = h.terminals_for(job.id)
    # An engine torn down to stop a job is a cancellation, never a failure.
    assert terminal.state == "cancelled"
    assert provider.cancels == [job.id]
    assert not job.artifact_path.exists()
    assert not part_path_of(job).exists()


def test_cancel_job_does_not_block_the_calling_thread(harness) -> None:
    # The real QwenEngine.cancel waits for the model host to settle the job
    # (an in-flight generate is uninterruptible; the grace is 300 s), and
    # cancel_job runs on the GUI thread (controller.cancel is a QML slot).
    # The provider stop must therefore happen off the caller's thread, or the
    # whole window freezes behind a "Canceling…" label (spec: "Main thread
    # never blocks").
    provider = SlowCancelProvider()
    h = harness(None, providers=qwen_providers(provider))
    job = make_job("a" * 32, text="你好。", context=qwen_context())

    assert h.worker.submit(job) is True
    assert wait_until(provider.started.is_set), "the generation never started"

    started_at = time.monotonic()
    assert h.worker.cancel_job(job.id) is True
    elapsed = time.monotonic() - started_at
    assert elapsed < 1.0, f"cancel_job blocked the caller for {elapsed:.1f}s"

    # The dispatched stop still reaches the engine and settles the job.
    assert wait_until(provider.cancel_entered.is_set), "the provider cancel never started"
    provider.release_cancel()
    assert h.wait_terminal(job.id)
    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "cancelled"
    assert provider.cancels == [job.id]


def test_stop_reaches_a_running_qwen_generation(harness) -> None:
    provider = QwenProviderDouble(block=True)
    h = harness(None, providers=qwen_providers(provider))
    job = make_job("a" * 32, text="你好。", context=qwen_context())

    assert h.worker.submit(job) is True
    assert wait_until(provider.started.is_set), "the generation never started"
    assert h.worker.stop() is True

    assert provider.cancels == [job.id]
    assert not job.artifact_path.exists()


def test_a_running_job_keeps_the_provider_it_resolved(harness) -> None:
    first = QwenProviderDouble(chunks_per_segment=1, block=True)
    second = QwenProviderDouble(chunks_per_segment=1)
    h = harness(None, providers=qwen_providers(first))
    job = make_job("a" * 32, text="你好。" * 200, context=qwen_context())

    assert h.worker.submit(job) is True
    assert wait_until(first.started.is_set), "the generation never started"
    # A profile switch builds a new provider set for the NEXT job: the running
    # job must keep the provider it resolved before its first segment.
    h.worker._providers = qwen_providers(second)
    first.release()
    assert h.wait_terminal(job.id)

    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "completed"
    assert len(first.segments) > 1
    assert second.segments == []
    assert second.initialized == 0


def test_warmup_initializes_only_the_default_provider(harness) -> None:
    active = QwenProviderDouble(chunks_per_segment=1)
    inactive = QwenProviderDouble(chunks_per_segment=1, profile=QWEN_BASE)
    h = harness(
        None,
        providers=EngineProviders(
            by_profile={QWEN_CUSTOM: active, QWEN_BASE: inactive}, default=active
        ),
    )
    job = make_job("a" * 32, text="你好。", context=qwen_context())
    h.worker.submit(WarmupOp())
    h.worker.submit(job)

    assert h.wait_terminal(job.id)
    assert active.initialized == 1
    assert inactive.initialized == 0, "only the active profile is prewarmed"
    assert inactive.segments == []


def test_a_vieneu_voice_op_needs_the_vieneu_engine(harness) -> None:
    vieneu = VieNeuProvider(None)
    h = harness(None, providers=EngineProviders(by_profile={VIENEU: vieneu}, default=vieneu))
    voice_job = SynthesisJob(
        id="f" * 32,
        owner="cloning",
        kind="voice_op",
        priority=0,
        request=VoiceOp(op="remove", name="Doomed", profile=VIENEU),
    )

    assert h.worker.submit(voice_job) is True
    assert h.wait_terminal(voice_job.id)

    (terminal,) = h.terminals_for(voice_job.id)
    assert terminal.state == "failed"
    assert "no VieNeu-TTS engine" in terminal.error


def test_a_voice_op_routes_to_its_own_profile_provider(harness) -> None:
    vieneu = RecordingEngine()
    base = QwenProviderDouble(profile=QWEN_BASE)
    h = harness(
        vieneu,
        providers=EngineProviders(
            by_profile={VIENEU: VieNeuProvider(vieneu), QWEN_BASE: base},
            default=VieNeuProvider(vieneu),
        ),
    )
    vieneu_job = SynthesisJob(
        id="f" * 32,
        owner="cloning",
        kind="voice_op",
        priority=0,
        request=VoiceOp(op="remove", name="Gone", profile=VIENEU),
    )
    base_job = SynthesisJob(
        id="e" * 32,
        owner="cloning",
        kind="voice_op",
        priority=0,
        request=VoiceOp(op="remove", name="Doomed", profile=QWEN_BASE),
    )

    assert h.worker.submit(vieneu_job) is True
    assert h.worker.submit(base_job) is True
    assert h.wait_terminal(vieneu_job.id) and h.wait_terminal(base_job.id)

    assert h.engine.voice_calls == [("remove_voice", {"name": "Gone", "save": False})]
    assert [op.name for op in base.voice_ops] == ["Doomed"]
    assert [t.state for t in h.terminals] == ["completed", "completed"]
    assert h.terminals[1].value == {
        "op": "remove",
        "name": "Doomed",
        "cloneId": "qwen_base_0_6b-clone",
    }


def test_a_real_qwen_provider_writes_a_valid_artifact(
    harness, tmp_path: Path, qwen_engines
) -> None:
    provider = real_qwen_provider(qwen_engines, tmp_path, "ok")
    h = harness(None, providers=qwen_providers(provider))
    job = make_job("a" * 32, text="你好。世界。", context=qwen_context())

    assert h.worker.submit(job) is True
    assert h.wait_terminal(job.id)

    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "completed"
    assert isinstance(terminal.value, SynthesisArtifact)
    # The scripted host streams two 12000-frame pcm chunks per segment.
    assert terminal.value.samples == 24_000
    assert validate_wav_artifact(terminal.value.path) == (24_000, 48_000)
    frames = host_fake.received(tmp_path, "synthesize")
    assert [entry["fields"]["text"] for entry in frames] == ["你好。世界。"]
    # The frame carries the APP code — the host maps it to the model's own
    # language name ("zh" → "Chinese") and validates it first.
    assert frames[0]["fields"]["language"] == "zh"
    assert frames[0]["fields"]["speaker"] == "Vivian"


def test_a_real_qwen_cancel_settles_cancelled_and_keeps_the_host_alive(
    harness, tmp_path: Path, qwen_engines
) -> None:
    provider = real_qwen_provider(qwen_engines, tmp_path, "graceful_cancel")
    engine = provider._engine  # type: ignore[attr-defined]
    h = harness(None, providers=qwen_providers(provider))
    job = make_job("a" * 32, text="你好。", context=qwen_context())

    assert h.worker.submit(job) is True
    assert wait_until(lambda: bool(host_fake.received(tmp_path, "synthesize"))), (
        "the host never received the segment"
    )
    assert h.worker.cancel_job(job.id) is True
    assert h.wait_terminal(job.id)

    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "cancelled"
    assert not job.artifact_path.exists()
    assert not part_path_of(job).exists()
    assert engine.is_initialized is True, "a user cancel must not kill the host"
    assert host_fake.pid_alive(host_fake.host_pid(tmp_path)) is True


def test_a_fatal_qwen_error_fails_the_job_and_the_next_job_restarts_the_host(
    harness, tmp_path: Path, qwen_engines
) -> None:
    provider = real_qwen_provider(qwen_engines, tmp_path, "oom")
    engine = provider._engine  # type: ignore[attr-defined]
    h = harness(None, providers=qwen_providers(provider))

    for attempt in range(2):
        job = make_job(f"{attempt:032x}", text="你好。", context=qwen_context())
        assert h.worker.submit(job) is True
        assert h.wait_terminal(job.id)

        (terminal,) = h.terminals_for(job.id)
        assert terminal.state == "failed"
        assert "CUDA out of memory" in terminal.error
        assert not job.artifact_path.exists()
        assert not part_path_of(job).exists()
        assert engine.is_initialized is False, "the OOM host is not reused"

    starts = [entry for entry in host_fake.host_log(tmp_path) if entry["event"] == "start"]
    assert len(starts) == 2, "the second job must get a fresh host"


# ── voice ops: profile-scoped clone enrollment (Task 4.2) ─────────────────


def make_reference_clip(path: Path, *, seconds: float = 3.0) -> Path:
    from vienetts_app.core.audio import write_wav_file

    return write_wav_file(np.full(int(seconds * 24_000), 0.2, dtype=np.float32), path, 24_000)


def base_providers(provider) -> EngineProviders:
    return EngineProviders(by_profile={QWEN_BASE: provider}, default=provider)


def voice_job(job_id: str, op: VoiceOp) -> SynthesisJob:
    return SynthesisJob(id=job_id, owner="cloning", kind="voice_op", priority=0, request=op)


def test_a_qwen_voice_op_enrolls_a_clone_in_the_store(
    harness, tmp_path: Path, qwen_engines
) -> None:
    store = CloneStore(tmp_path / "clones", now=lambda: datetime(2026, 9, 21, tzinfo=UTC))
    provider = QwenEngineProvider(
        host_fake.engine_for(tmp_path, "ok", profile=QWEN_BASE), clone_store=store
    )
    qwen_engines.append(provider._engine)  # type: ignore[attr-defined]
    h = harness(None, providers=base_providers(provider))
    clip = make_reference_clip(tmp_path / "ref.wav")
    job = voice_job(
        "f" * 32,
        VoiceOp(
            op="add",
            name="Ngọc Anh",
            clip_path=str(clip),
            transcript="Xin chào.",
            consent=True,
            profile=QWEN_BASE,
        ),
    )

    assert h.worker.submit(job) is True
    assert h.wait_terminal(job.id)

    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "completed"
    assert terminal.value == {
        "op": "add",
        "name": "Ngọc Anh",
        "cloneId": terminal.value["cloneId"],  # type: ignore[index]
        "profile": QWEN_BASE,
    }
    clone = store.get(terminal.value["cloneId"])  # type: ignore[index]
    assert clone.name == "Ngọc Anh"
    assert clone.transcript == "Xin chào."
    assert clone.reference_path.is_file()

    remove = voice_job("e" * 32, VoiceOp(op="remove", name="Ngọc Anh", profile=QWEN_BASE))
    assert h.worker.submit(remove) is True
    assert h.wait_terminal(remove.id)
    (removed,) = h.terminals_for(remove.id)
    assert removed.state == "completed"
    assert removed.value == {
        "op": "remove",
        "name": "Ngọc Anh",
        "cloneId": clone.clone_id,
        "profile": QWEN_BASE,
    }
    assert store.list() == ()
    assert not clone.reference_path.exists()


def test_a_qwen_voice_op_without_a_store_fails_with_a_reason(
    harness, tmp_path: Path, qwen_engines
) -> None:
    engine = host_fake.engine_for(tmp_path, "ok", profile=QWEN_BASE)
    qwen_engines.append(engine)
    provider = QwenEngineProvider(engine)  # no clone store wired
    h = harness(None, providers=base_providers(provider))
    job = voice_job("f" * 32, VoiceOp(op="remove", name="Doomed", profile=QWEN_BASE))

    assert h.worker.submit(job) is True
    assert h.wait_terminal(job.id)

    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "failed"
    assert "no clone store configured" in terminal.error


def test_a_customvoice_voice_op_is_rejected_with_the_capability_reason(
    harness, tmp_path: Path, qwen_engines
) -> None:
    engine = host_fake.engine_for(tmp_path, "ok", profile=QWEN_CUSTOM)
    qwen_engines.append(engine)
    provider = QwenEngineProvider(engine, clone_store=CloneStore(tmp_path / "clones"))
    h = harness(
        None, providers=EngineProviders(by_profile={QWEN_CUSTOM: provider}, default=provider)
    )
    job = voice_job("f" * 32, VoiceOp(op="remove", name="Doomed", profile=QWEN_CUSTOM))

    assert h.worker.submit(job) is True
    assert h.wait_terminal(job.id)

    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "failed"
    assert "uses fixed speakers and cannot enroll clones" in terminal.error


def test_an_enrolled_clone_synthesizes_after_a_restart(
    harness, tmp_path: Path, qwen_engines
) -> None:
    store = CloneStore(tmp_path / "clones")
    provider = QwenEngineProvider(
        host_fake.engine_for(tmp_path, "ok", profile=QWEN_BASE), clone_store=store
    )
    qwen_engines.append(provider._engine)  # type: ignore[attr-defined]
    h = harness(None, providers=base_providers(provider))
    clip = make_reference_clip(tmp_path / "ref.wav")
    enroll_job = voice_job(
        "f" * 32,
        VoiceOp(
            op="add",
            name="Ngọc Anh",
            clip_path=str(clip),
            transcript="Xin chào.",
            consent=True,
            profile=QWEN_BASE,
        ),
    )
    assert h.worker.submit(enroll_job) is True
    assert h.wait_terminal(enroll_job.id)
    (enrolled,) = h.terminals_for(enroll_job.id)
    clone_id = enrolled.value["cloneId"]  # type: ignore[index]
    reference = store.get(clone_id).reference_path

    # A restart: fresh store, fresh host, same app-owned reference copy.
    restarted_store = CloneStore(tmp_path / "clones")
    restarted = QwenEngineProvider(
        host_fake.engine_for(tmp_path, "ok", profile=QWEN_BASE), clone_store=restarted_store
    )
    qwen_engines.append(restarted._engine)  # type: ignore[attr-defined]
    h2 = harness(None, providers=base_providers(restarted))
    job = make_job(
        "a" * 32,
        text="你好。",
        context=context_for(QWEN_BASE, language="zh", clone_id=clone_id),
    )

    assert h2.worker.submit(job) is True
    assert h2.wait_terminal(job.id)

    (terminal,) = h2.terminals_for(job.id)
    assert terminal.state == "completed"
    frames = host_fake.received(tmp_path, "synthesize")
    assert [entry["fields"]["voicePrompt"] for entry in frames] == [str(reference)]
    assert [entry["fields"]["refText"] for entry in frames] == ["Xin chào."]
    assert "speaker" not in frames[0]["fields"]


# ── pending-work probe (Phase 5 Task 5.1) ─────────────────────────────────


def test_has_pending_work_covers_queued_and_active_jobs(harness) -> None:
    h = harness(GateEngine())
    assert h.worker.has_pending_work() is False
    running = make_job("a" * 32, text="running", mode="stream")
    queued = make_job("b" * 32, text="queued", mode="infer")
    assert h.worker.submit(running) is True
    assert h.worker.submit(queued) is True

    # Both the active job and the still-queued one count: a profile switch
    # must never tear an engine down under admitted work.
    assert h.worker.has_pending_work() is True
    assert h.engine.wait_until_started()
    assert h.worker.has_pending_work() is True

    assert h.worker.cancel_job(queued.id) is True
    h.engine.release.set()
    assert h.wait_terminal(running.id)
    assert h.wait_terminal(queued.id)
    assert h.worker.has_pending_work() is False


def test_has_pending_work_ignores_warmups(harness) -> None:
    h = harness(RecordingEngine())
    assert h.worker.submit(WarmupOp()) is True

    # A warmup is silent engine preparation, not user work — it must not block
    # a profile switch (the incoming profile redoes it anyway).
    assert h.worker.has_pending_work() is False


def test_has_pending_work_covers_the_dequeued_window(harness, monkeypatch) -> None:
    """A job out of the queue but not yet marked active still blocks a switch."""
    h = harness(RecordingEngine(chunks_per_stream=1, chunk_delay=0.0))
    job = make_job("c" * 32, text="dequeued", mode="infer")
    original = h.worker._process  # noqa: SLF001 - the window under test
    dequeued = threading.Event()
    release = threading.Event()

    def gated(item: Any) -> None:
        dequeued.set()
        assert release.wait(timeout=5), "gated _process was never released"
        original(item)

    monkeypatch.setattr(h.worker, "_process", gated)
    assert h.worker.submit(job) is True
    assert dequeued.wait(timeout=5)

    # take() returned the job, _process has not installed _active_job yet, and
    # the queue is empty: the probe must still report admitted work.
    assert h.worker.has_pending_work() is True
    release.set()
    assert h.wait_terminal(job.id)
    assert h.worker.has_pending_work() is False


# ── thread priority ───────────────────────────────────────────────────────


def test_the_worker_thread_runs_below_normal_priority(qcoreapp) -> None:
    """In-process engines compute on this thread inside the app itself: it must
    yield to the GUI (the Qwen host lowers its own process separately)."""
    worker = InferenceWorker(RecordingEngine())
    seen: list[Any] = []
    worker.setPriority = seen.append  # type: ignore[method-assign]
    worker._lower_priority()
    assert seen == [worker_module.QThread.Priority.LowPriority]
