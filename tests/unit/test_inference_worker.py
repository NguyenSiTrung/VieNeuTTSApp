"""InferenceWorker: serialized queue on one QThread, cooperative cancel, signals."""

import threading
import time
from typing import Any

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication  # noqa: E402

from vienetts_app.core.engine import TTSEngine, TTSEngineError  # noqa: E402
from vienetts_app.core.models import TTSProgress, TTSRequest  # noqa: E402
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


class RecordingEngine:
    """Stands in for TTSEngine; records the thread every call runs on."""

    def __init__(self, chunks_per_stream: int = 50, chunk_delay: float = 0.005) -> None:
        self.call_threads: list[int] = []
        self.requests: list[str] = []
        self.chunks_per_stream = chunks_per_stream
        self.chunk_delay = chunk_delay
        self.sample_rate = 48_000
        self.backend = "onnx"

    @property
    def single_thread(self) -> bool:
        return len(set(self.call_threads)) == 1

    def _rec(self, text: str) -> None:
        self.call_threads.append(threading.get_ident())
        self.requests.append(text)

    def infer(self, text, voice=None, **kw) -> np.ndarray:
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

    def close(self) -> None:
        pass


@pytest.fixture()
def qcoreapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


class WorkerHarness:
    def __init__(self, engine: Any) -> None:
        self.engine = engine
        self.progresses: list[TTSProgress] = []
        self.chunks: list[np.ndarray] = []
        self.results: list[Any] = []
        self.errors: list[str] = []
        self.worker = InferenceWorker(engine)
        self.worker.progress.connect(self.progresses.append)
        self.worker.chunk_ready.connect(self.chunks.append)
        self.worker.done.connect(self.results.append)
        self.worker.error.connect(self.errors.append)
        self.worker.start()
        self._finalizers.append(self._shutdown)

    _finalizers: list[Any] = []

    def _shutdown(self) -> None:
        self.worker.stop()

    def wait_done(self, count: int = 1, timeout: float = 10.0) -> bool:
        return wait_until(lambda: len(self.results) + len(self.errors) >= count, timeout)

    def wait_chunks(self, count: int, timeout: float = 10.0) -> bool:
        return wait_until(lambda: len(self.chunks) >= count, timeout)


@pytest.fixture()
def harness(qcoreapp):
    WorkerHarness._finalizers = []
    yield WorkerHarness
    for finalize in WorkerHarness._finalizers:
        finalize()
    WorkerHarness._finalizers = []


class TestQueueSerialization:
    def test_requests_processed_in_order_on_one_thread(self, harness) -> None:
        h = harness(RecordingEngine())
        for text in ("one", "two", "three"):
            h.worker.submit(TTSRequest(text=text))
        assert h.wait_done(3)
        assert h.engine.requests == ["one", "two", "three"]
        assert h.engine.single_thread
        assert threading.get_ident() not in set(h.engine.call_threads)

    def test_done_carries_audio(self, harness) -> None:
        h = harness(RecordingEngine())
        h.worker.submit(TTSRequest(text="hello"))
        assert h.wait_done()
        assert h.errors == []
        assert h.results[0].dtype == np.float32
        assert len(h.results[0]) == 48_000

    def test_progress_stages_for_infer(self, harness) -> None:
        h = harness(RecordingEngine())
        h.worker.submit(TTSRequest(text="hello"))
        assert h.wait_done()
        stages = [p.stage for p in h.progresses]
        assert stages[0] == "init"
        assert "synthesizing" in stages
        last = h.progresses[-1]
        assert (last.done, last.total) == (1, 1)


class TestStreaming:
    def test_chunk_ready_per_chunk_and_concatenated_done(self, harness) -> None:
        h = harness(RecordingEngine(chunks_per_stream=5, chunk_delay=0.0))
        h.worker.submit(TTSRequest(text="stream me", mode="stream"))
        assert h.wait_done()
        assert len(h.chunks) == 5
        assert h.results[0].shape == (5 * 15_360,)
        assert h.results[0][15_360] == pytest.approx(0.2)  # chunk 2 value


class TestCooperativeCancel:
    def test_cancel_stops_between_chunks(self, harness) -> None:
        h = harness(RecordingEngine(chunks_per_stream=1000, chunk_delay=0.005))
        h.worker.submit(TTSRequest(text="long stream", mode="stream"))
        assert h.wait_chunks(3)
        h.worker.cancel()
        emitted_before_cancel = len(h.chunks)
        assert wait_until(lambda: len(h.errors) == 1, timeout=5.0)
        time.sleep(0.1)  # allow any (wrong) extra chunks to arrive
        assert len(h.chunks) < 1000
        assert "cancel" in h.errors[0].lower()
        assert len(h.chunks) >= emitted_before_cancel - 1  # at most the in-flight chunk
        assert threading.get_ident() not in set(h.engine.call_threads)

    def test_cancelled_worker_accepts_new_work(self, harness) -> None:
        h = harness(RecordingEngine(chunks_per_stream=1000, chunk_delay=0.002))
        h.worker.submit(TTSRequest(text="doomed", mode="stream"))
        assert h.wait_chunks(2)
        h.worker.cancel()
        assert wait_until(lambda: len(h.errors) == 1)
        h.worker.submit(TTSRequest(text="after cancel"))
        assert wait_until(lambda: len(h.results) == 1)
        assert h.engine.requests[-1] == "after cancel"


class TestErrorPropagation:
    def test_engine_error_emits_error_signal(self, harness) -> None:
        class ExplodingEngine(RecordingEngine):
            def infer(self, text, voice=None, **kw):
                raise TTSEngineError("Voice 'Nope' not found")

        h = harness(ExplodingEngine())
        h.worker.submit(TTSRequest(text="hi", voice="Nope"))
        assert wait_until(lambda: len(h.errors) == 1)
        assert "Nope" in h.errors[0]
        assert h.results == []

    def test_worker_survives_error(self, harness) -> None:
        class OnceExplodingEngine(RecordingEngine):
            exploded = False

            def infer(self, text, voice=None, **kw):
                if not OnceExplodingEngine.exploded:
                    OnceExplodingEngine.exploded = True
                    raise TTSEngineError("boom")
                return super().infer(text, voice, **kw)

        h = harness(OnceExplodingEngine())
        h.worker.submit(TTSRequest(text="first"))
        assert wait_until(lambda: len(h.errors) == 1)
        h.worker.submit(TTSRequest(text="second"))
        assert wait_until(lambda: len(h.results) == 1)
        assert len(h.results) == 1


def test_worker_owns_engine_type() -> None:
    # The worker takes the engine it is given; TTSEngine is the expected type
    # (fake engines are for tests only).
    worker = InferenceWorker(TTSEngine(factory=lambda **kw: None))
    assert worker.engine is not None


def test_accepts_tts_engine(harness) -> None:
    h = harness(TTSEngine(factory=lambda **kw: RecordingEngine()))
    h.worker.submit(TTSRequest(text="via real wrapper"))
    assert h.wait_done()
    assert h.results[0].dtype == np.float32
