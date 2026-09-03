"""InferenceWorker: tagged exactly-once terminals, targeted cancel (Phase 2 Task 2)."""

import threading
import time
from typing import Any

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication  # noqa: E402

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
from vienetts_app.workers.inference_worker import InferenceWorker  # noqa: E402


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
) -> SynthesisJob:
    return SynthesisJob(
        id=job_id,
        owner=owner,  # type: ignore[arg-type]
        kind=kind,  # type: ignore[arg-type]
        priority=0,
        request=TTSRequest(text=text, mode=mode, job_id=job_id),  # type: ignore[arg-type]
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

    def infer(self, text, voice=None, temperature=None, **kw) -> np.ndarray:
        self._rec(text)
        self.started.set()
        assert self.release.wait(timeout=10), "gate was never released"
        return np.zeros(100, dtype=np.float32)


class FailingEngine(RecordingEngine):
    def infer(self, text, voice=None, temperature=None, **kw) -> np.ndarray:
        self._rec(text)
        raise TTSEngineError("boom")

    def infer_stream(self, text, voice=None, **kw):
        self._rec(text)
        raise TTSEngineError("boom")
        yield  # pragma: no cover - make this a generator


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
    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.progresses: list[Any] = []
        self.chunks: list[Any] = []
        self.terminals: list[JobTerminal] = []
        self.results: list[Any] = []
        self.errors: list[str] = []
        self.voice_ops: list[Any] = []
        self.worker = InferenceWorker(engine)
        self.worker.progress.connect(self.progresses.append)
        self.worker.chunk_ready.connect(self.chunks.append)
        self.worker.terminal.connect(self.terminals.append)
        self.worker.done.connect(self.results.append)
        self.worker.error.connect(self.errors.append)
        self.worker.voice_op_done.connect(self.voice_ops.append)
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
def harness(qcoreapp):
    # qcoreapp: cross-thread delivery needs the session event loop; without
    # it wait_until only sleeps and queued slots never fire.
    created: list[WorkerHarness] = []

    def make(engine: Any) -> WorkerHarness:
        h = WorkerHarness(engine)
        created.append(h)
        return h

    yield make
    for h in created:
        h.worker.stop()


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
    assert len(h.tagged_chunks()) == 5
    assert all(p.job_id == job.id for p in h.tagged_progresses())
    assert all(c.job_id == job.id for c in h.tagged_chunks())
    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "completed"
    assert terminal.value.shape == (5 * 15_360,)


def test_infer_multi_segment_reports_segment_progress(harness) -> None:
    text = "Xin chào. " * 200
    segments = split_text_for_streaming(text)
    assert len(segments) > 1
    h = harness(RecordingEngine())
    job = make_job("b" * 32, text=text, mode="infer")
    h.worker.submit(job)

    assert h.wait_terminal(job.id)
    assert h.engine.requests == segments
    assert h.errors == []
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
    h = harness(RecordingEngine(chunks_per_stream=1000, chunk_delay=0.002))
    first = make_job("a" * 32, text="first", mode="stream")
    second = make_job("b" * 32, text="second", mode="infer")
    h.worker.submit(first)
    h.worker.submit(second)
    assert wait_until(lambda: len(h.tagged_chunks()) >= 2, timeout=5.0)

    assert h.worker.cancel_job(first.id) is True

    assert h.wait_terminal(first.id)
    assert h.wait_terminal(second.id)
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
    assert h.errors == []
    assert [t.state for t in h.terminals_for(job.id)] == ["completed"]
    assert engine.requests == ["hello"]


def test_warmup_failure_is_silent(harness) -> None:
    h = harness(FailingInitEngine())
    job = make_job("b" * 32, mode="infer")
    h.worker.submit(WarmupOp())
    h.worker.submit(job)

    assert h.wait_terminal(job.id)
    assert h.errors == []
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


def test_batch_job_terminal_carries_audio_list(harness) -> None:
    h = harness(RecordingEngine())
    job = make_job("a" * 32, mode="batch")
    h.worker.submit(job)

    assert h.wait_terminal(job.id)
    (terminal,) = h.terminals_for(job.id)
    assert terminal.state == "completed"
    assert isinstance(terminal.value, list)
    assert len(terminal.value) == 1


# ── legacy submission adapter (transitional; Task 3 removes callers) ──────


def test_legacy_tts_request_still_drives_done_signal(harness) -> None:
    h = harness(RecordingEngine(chunks_per_stream=1, chunk_delay=0.0))
    assert h.worker.submit(TTSRequest(text="hello")) is True

    assert wait_until(lambda: len(h.results) == 1, timeout=10.0)
    assert h.results[0].dtype == np.float32
    assert len(h.terminals) == 1
    assert h.terminals[0].state == "completed"


def test_legacy_stream_request_drives_chunks_and_done(harness) -> None:
    h = harness(RecordingEngine(chunks_per_stream=5, chunk_delay=0.0))
    h.worker.submit(TTSRequest(text="stream me", mode="stream"))

    assert wait_until(lambda: len(h.results) == 1, timeout=10.0)
    raw = [c for c in h.chunks if isinstance(c, np.ndarray)]
    assert len(raw) == 5  # legacy views; tagged JobChunk copies share the list
    assert h.results[0].shape == (5 * 15_360,)


def test_legacy_voice_op_drives_voice_op_done(harness) -> None:
    h = harness(RecordingEngine())
    h.worker.submit(VoiceOp(op="remove", name="Doomed"))

    assert wait_until(lambda: len(h.voice_ops) == 1, timeout=10.0)
    assert h.voice_ops[0] == {"op": "remove", "name": "Doomed"}


def test_legacy_cancel_drops_queue_and_errors_in_flight(harness) -> None:
    h = harness(RecordingEngine(chunks_per_stream=1000, chunk_delay=0.002))
    h.worker.submit(TTSRequest(text="doomed", mode="stream"))
    assert wait_until(lambda: len(h.chunks) >= 2, timeout=5.0)
    h.worker.cancel()
    assert wait_until(lambda: len(h.errors) == 1, timeout=5.0)
    assert "cancel" in h.errors[0].lower()

    h.worker.submit(TTSRequest(text="after cancel"))
    assert wait_until(lambda: len(h.results) == 1, timeout=10.0)
    assert h.engine.requests[-1] == "after cancel"


def test_legacy_engine_error_message_preserved(harness) -> None:
    h = harness(FailingEngine())
    h.worker.submit(TTSRequest(text="hello"))

    assert wait_until(lambda: len(h.errors) == 1, timeout=10.0)
    assert h.errors == ["boom"]
    assert h.terminals[0].state == "failed"
