"""InferenceWorker: serialized queue on one QThread, cooperative cancel, signals."""

import threading
import time
from typing import Any

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication  # noqa: E402

from vienetts_app.core.engine import (  # noqa: E402
    TTSEngine,
    TTSEngineError,
    split_text_for_streaming,
)
from vienetts_app.core.models import TTSProgress, TTSRequest, VoiceOp  # noqa: E402
from vienetts_app.workers import inference_worker as iw  # noqa: E402
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
        self.last_infer_kwargs = {"voice": voice, "temperature": temperature}
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

    # -- voice management seam (TTSEngine surface, FR-3.4) -------------------

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

    def test_cancel_then_resubmit_does_not_resurrect_cancelled_job(self, harness) -> None:
        # Regression: submit() used to clear the cancel flag at ENQUEUE time,
        # so cancel(A) + submit(B) let A finish and emit a stale done on top
        # of B's state (busy flipped false, A's audio shown). The clear now
        # happens at the start of _process() in the worker thread: A must
        # still die with CANCELLED even though B was queued right after.
        h = harness(RecordingEngine(chunks_per_stream=1000, chunk_delay=0.002))
        h.worker.submit(TTSRequest(text="doomed", mode="stream"))
        assert h.wait_chunks(2)
        h.worker.cancel()
        h.worker.submit(TTSRequest(text="after cancel"))  # must NOT un-cancel A
        assert wait_until(lambda: len(h.errors) == 1)
        assert "cancel" in h.errors[0].lower()
        assert wait_until(lambda: len(h.results) == 1)  # B completes normally
        time.sleep(0.1)  # let any (wrong) late A audio arrive
        assert len(h.results) == 1  # exactly B's — A never emitted done
        assert h.engine.requests[-1] == "after cancel"

    def test_stop_interrupts_stream_promptly(self, harness) -> None:
        # Regression: the stream loop only checked _cancel, so a quit during
        # a long render kept synthesizing after stop() gave up waiting — the
        # engine was then closed under the live thread (native crash risk).
        h = harness(RecordingEngine(chunks_per_stream=1000, chunk_delay=0.005))
        h.worker.submit(TTSRequest(text="long render", mode="stream"))
        assert h.wait_chunks(3)
        started = time.monotonic()
        assert h.worker.stop() is True  # far under the 5 s wait budget
        assert time.monotonic() - started < 4.0
        assert h.worker.isFinished()


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


class TestTemperatureFlow:
    def _worker_with(self, engine) -> tuple[InferenceWorker, dict[str, list]]:
        worker = InferenceWorker(engine)
        box: dict[str, list] = {"progress": [], "done": [], "error": []}
        worker.progress.connect(box["progress"].append)
        worker.done.connect(box["done"].append)
        worker.error.connect(box["error"].append)
        return worker, box

    def test_temperature_reaches_engine_infer_direct(self) -> None:
        engine = RecordingEngine()
        worker, _box = self._worker_with(engine)
        worker._process(TTSRequest(text="hi", temperature=0.9))
        assert engine.last_infer_kwargs == {"voice": None, "temperature": 0.9}

    def test_none_temperature_reaches_engine_infer_direct(self) -> None:
        engine = RecordingEngine()
        worker, _box = self._worker_with(engine)
        worker._process(TTSRequest(text="hi"))
        assert engine.last_infer_kwargs == {"voice": None, "temperature": None}

    def test_temperature_flows_through_threaded_queue(self, harness) -> None:
        h = harness(RecordingEngine())
        h.worker.submit(TTSRequest(text="warm", temperature=1.25))
        assert h.wait_done()
        assert h.engine.last_infer_kwargs == {"voice": None, "temperature": 1.25}


class TestVoiceOpDispatch:
    """VoiceOp jobs ride the same queue and report via voice_op_done."""

    def _voice_worker(self, engine) -> tuple[InferenceWorker, dict[str, list]]:
        worker = InferenceWorker(engine)
        box: dict[str, list] = {"progress": [], "done": [], "error": [], "voice_op": []}
        worker.progress.connect(box["progress"].append)
        worker.done.connect(box["done"].append)
        worker.error.connect(box["error"].append)
        worker.voice_op_done.connect(box["voice_op"].append)
        return worker, box

    def test_add_voice_dispatch_direct(self) -> None:
        engine = RecordingEngine()
        worker, box = self._voice_worker(engine)
        worker._process(VoiceOp(op="add", name="MyVoice", clip_path="/r.wav", denoise=False))
        assert box["error"] == []
        assert box["done"] == []  # voice ops never emit `done` (no audio produced)
        assert engine.voice_calls == [
            (
                "add_voice",
                {"name": "MyVoice", "ref_clip": "/r.wav", "denoise": False, "save": False},
            )
        ]
        assert engine.persisted_count == 1  # persist after add, never save=True
        assert box["voice_op"] == [{"op": "add", "name": "MyVoice"}]

    def test_remove_voice_dispatch_direct(self) -> None:
        engine = RecordingEngine()
        worker, box = self._voice_worker(engine)
        worker._process(VoiceOp(op="remove", name="MyVoice"))
        assert box["error"] == []
        assert engine.voice_calls == [("remove_voice", {"name": "MyVoice", "save": False})]
        assert engine.persisted_count == 1
        assert box["voice_op"] == [{"op": "remove", "name": "MyVoice"}]

    def test_denoise_dispatch_direct(self) -> None:
        engine = RecordingEngine()
        worker, box = self._voice_worker(engine)
        worker._process(VoiceOp(op="denoise", clip_path="/c.wav"))
        assert box["error"] == []
        assert engine.voice_calls == [("denoise", {"clip_path": "/c.wav"})]
        assert engine.persisted_count == 0  # denoise never persists voices
        (payload,) = box["voice_op"]
        assert payload["op"] == "denoise"
        assert payload["sample_rate"] == 44_100
        assert payload["audio"].dtype == np.float32

    def test_add_voice_through_threaded_queue(self, harness) -> None:
        h = harness(RecordingEngine())
        voice_ops: list[Any] = []
        h.worker.voice_op_done.connect(voice_ops.append)
        h.worker.submit(VoiceOp(op="add", name="Clone", clip_path="/r.wav"))
        assert wait_until(lambda: len(voice_ops) == 1)
        assert voice_ops[0] == {"op": "add", "name": "Clone"}

    def test_voice_op_error_flows_through_error_signal(self) -> None:
        class ExplodingEngine(RecordingEngine):
            def add_voice(self, name, ref_clip, *, denoise=True, save=False) -> str:
                raise TTSEngineError(" enrollment failed: bad clip")

        engine = ExplodingEngine()
        worker, box = self._voice_worker(engine)
        worker._process(VoiceOp(op="add", name="X", clip_path="/r.wav"))
        assert box["voice_op"] == []
        assert len(box["error"]) == 1
        assert "enrollment" in box["error"][0]

    def test_mixed_queue_serializes_in_order(self, harness) -> None:
        h = harness(RecordingEngine())
        h.worker.submit(TTSRequest(text="one"))
        h.worker.submit(VoiceOp(op="add", name="Clone", clip_path="/r.wav"))
        h.worker.submit(TTSRequest(text="two"))
        assert wait_until(lambda: len(h.results) == 2 and h.engine.voice_calls != [], timeout=10.0)
        assert h.engine.requests == ["one", "two"]
        assert h.errors == []

    """Synchronous _process calls (main thread) — coverage for logic that
    QThread runs in C++-created threads (untraceable by coverage.py)."""

    def _worker_with(self, engine) -> tuple[InferenceWorker, dict[str, list]]:
        worker = InferenceWorker(engine)
        box: dict[str, list] = {"progress": [], "chunks": [], "done": [], "error": []}
        worker.progress.connect(box["progress"].append)
        worker.chunk_ready.connect(box["chunks"].append)
        worker.done.connect(box["done"].append)
        worker.error.connect(box["error"].append)
        return worker, box

    def test_infer_direct(self) -> None:
        worker, box = self._worker_with(RecordingEngine())
        worker._process(TTSRequest(text="hi"))
        assert box["error"] == []
        assert box["done"][0].shape == (48_000,)
        assert [p.stage for p in box["progress"]] == ["init", "synthesizing", "synthesizing"]

    def test_stream_direct(self) -> None:
        worker, box = self._worker_with(RecordingEngine(chunks_per_stream=3, chunk_delay=0.0))
        worker._process(TTSRequest(text="hi", mode="stream"))
        assert len(box["chunks"]) == 3
        assert box["done"][0].shape == (3 * 15_360,)

    def test_stream_cancel_between_chunks_direct(self) -> None:
        class SlowEngine(RecordingEngine):
            def infer_stream(self, text, voice=None, **kw):
                yield np.zeros(15_360, dtype=np.float32)
                self.midpoint.set()
                yield np.zeros(15_360, dtype=np.float32)

        engine = SlowEngine(chunks_per_stream=2, chunk_delay=0.0)
        engine.midpoint = threading.Event()

        worker, box = self._worker_with(engine)

        # Simulate cancel arriving while the second chunk is being produced.
        class CancelOnSecond:
            def __init__(self, w: InferenceWorker) -> None:
                self.n = 0
                self.w = w

            def __call__(self, chunk: object) -> None:
                self.n += 1
                if self.n == 1:
                    self.w._cancel.set()

        worker.chunk_ready.connect(CancelOnSecond(worker))
        worker._process(TTSRequest(text="hi", mode="stream"))
        assert len(box["chunks"]) == 1  # second chunk never emitted
        assert "cancel" in box["error"][0].lower()

    def test_batch_direct(self) -> None:
        worker, box = self._worker_with(RecordingEngine())
        worker._process(TTSRequest(text="hi", mode="batch"))
        assert isinstance(box["done"][0], list)
        assert box["done"][0][0].shape == (1000,)

    def test_engine_error_direct(self) -> None:
        class Boom(RecordingEngine):
            def infer(self, text, voice=None, **kw):
                raise TTSEngineError("nope")

        worker, box = self._worker_with(Boom())
        worker._process(TTSRequest(text="hi"))
        assert box["error"] == ["nope"]

    def test_unexpected_error_direct(self) -> None:
        class Bang(RecordingEngine):
            def infer(self, text, voice=None, **kw):
                raise RuntimeError("surprise")

        worker, box = self._worker_with(Bang())
        worker._process(TTSRequest(text="hi"))
        assert len(box["error"]) == 1
        assert "surprise" in box["error"][0]

    def test_check_cancelled_clear_case(self) -> None:
        worker, box = self._worker_with(RecordingEngine())
        assert worker._check_cancelled() is False
        worker._cancel.set()
        assert worker._check_cancelled() is True
        assert "cancel" in box["error"][0].lower()

    def test_check_cancelled_honors_stop_flag(self) -> None:
        # shutdown() must silence the in-flight request too, not just cancels.
        worker, box = self._worker_with(RecordingEngine())
        worker._stop.set()
        assert worker._check_cancelled() is True
        assert "cancel" in box["error"][0].lower()

    def test_stream_loop_treats_stop_as_cancel(self) -> None:
        worker, box = self._worker_with(RecordingEngine(chunks_per_stream=50))
        worker._stop.set()  # quit lands before the job even starts its 2nd chunk
        worker._process(TTSRequest(text="hi", mode="stream"))
        assert len(box["chunks"]) <= 1
        assert "cancel" in box["error"][0].lower()
        assert box["done"] == []


class TaggedStreamEngine(RecordingEngine):
    """infer_stream records segment texts; yields deterministic tagged chunks.

    The three chunks per call carry values 1.0/2.0/3.0 so tests can verify
    per-segment ordering and reconstruct the expected concatenated audio.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.stream_texts: list[str] = []

    def infer_stream(self, text, voice=None, **kw):
        self._rec(text)
        self.stream_texts.append(text)
        for i in range(3):
            yield np.full(16, float(i + 1), dtype=np.float32)


def tagged_chunks_for_segment(_segment: str) -> list[np.ndarray]:
    # Mirrors TaggedStreamEngine.infer_stream: three 16-sample chunks whose
    # values encode the position within the segment.
    return [np.full(16, float(i + 1), dtype=np.float32) for i in range(3)]


class TestSegmentedStreamDispatch:
    """Chunked stream dispatch (FR-4.6d): segment-aware progress + cancel.

    The worker consumes segmentation from core.engine's pure helper directly,
    so ANY engine with the plain ``infer_stream`` duck type keeps working
    unchanged (RecordingEngine below has no segmentation attributes).
    """

    def _long_text(self) -> str:
        # ~20 complete Vietnamese sentences ≈ 1.5k chars → several segments
        # under DEFAULT_MAX_CHARS=512; boundaries land between sentences.
        return " ".join(
            f"Câu thứ {i} chứa những từ tiếng Việt có dấu để chiếm chỗ đều đặn." for i in range(20)
        )

    def test_multi_segment_concatenation_matches_manual_per_segment(self, harness) -> None:
        h = harness(TaggedStreamEngine())
        text = self._long_text()
        expected_segments = split_text_for_streaming(text)
        assert len(expected_segments) >= 2

        h.worker.submit(TTSRequest(text=text, mode="stream"))
        assert h.wait_done()

        assert h.errors == []
        assert h.engine.stream_texts == expected_segments
        expected_audio = np.concatenate(
            [chunk for segment in expected_segments for chunk in tagged_chunks_for_segment(segment)]
        )
        assert np.array_equal(h.results[0], expected_audio)

    def test_chunk_ready_fires_per_chunk_across_segments(self, harness) -> None:
        h = harness(TaggedStreamEngine())
        text = self._long_text()
        n_segments = len(split_text_for_streaming(text))
        h.worker.submit(TTSRequest(text=text, mode="stream"))
        assert h.wait_done()
        assert len(h.chunks) == 3 * n_segments

    def test_progress_counts_completed_segments(self, harness) -> None:
        h = harness(TaggedStreamEngine())
        text = self._long_text()
        n_segments = len(split_text_for_streaming(text))
        h.worker.submit(TTSRequest(text=text, mode="stream"))
        assert h.wait_done()

        synthesizing = [p for p in h.progresses if p.stage == "synthesizing"]
        assert all(p.done <= p.total for p in synthesizing)
        assert (synthesizing[0].done, synthesizing[0].total) == (0, n_segments)
        assert (synthesizing[-1].done, synthesizing[-1].total) == (n_segments, n_segments)
        dones = [p.done for p in synthesizing]
        assert dones == sorted(dones)  # monotonic

    def test_cancel_checked_at_segment_boundaries_direct(self) -> None:
        class BoundaryCancelHook:
            # Fires synchronously: direct _process runs on this thread.
            def __init__(self, worker: InferenceWorker) -> None:
                self.worker = worker

            def __call__(self, progress: object) -> None:
                if getattr(progress, "done", 0) >= 1:
                    self.worker._cancel.set()

        worker, box = make_worker_with_signals(TaggedStreamEngine())
        original = iw.split_text_for_streaming
        iw.split_text_for_streaming = lambda text, max_chars=None: ["đoạn một", "đoạn hai"]
        try:
            worker.progress.connect(BoundaryCancelHook(worker))
            worker._process(TTSRequest(text="văn bản bất kỳ dài hơn", mode="stream"))
        finally:
            iw.split_text_for_streaming = original

        assert worker.engine.stream_texts == ["đoạn một"]  # segment 2 never started
        assert "cancel" in box["error"][0].lower()
        assert box["done"] == []

    def test_bare_engine_with_only_infer_stream_still_works(self, qcoreapp) -> None:
        class BareStreamEngine:
            """Minimal duck-typed engine: no segmentation capability."""

            def infer_stream(self, text, voice=None, **kw):
                yield np.full(8, 0.5, dtype=np.float32)

        engine = BareStreamEngine()
        box: dict[str, list] = {"chunks": [], "done": [], "error": []}
        worker = InferenceWorker(engine)
        worker.chunk_ready.connect(box["chunks"].append)
        worker.done.connect(box["done"].append)
        worker.error.connect(box["error"].append)
        try:
            worker.start()
            worker.submit(TTSRequest(text="hello world", mode="stream"))
            assert wait_until(lambda: bool(box["done"]), timeout=10.0)
            assert box["error"] == []
            assert box["done"][0].shape == (8,)
        finally:
            worker.stop()


def make_worker_with_signals(engine):
    worker = InferenceWorker(engine)
    box: dict[str, list] = {"progress": [], "chunks": [], "done": [], "error": []}
    worker.progress.connect(box["progress"].append)
    worker.chunk_ready.connect(box["chunks"].append)
    worker.done.connect(box["done"].append)
    worker.error.connect(box["error"].append)
    return worker, box
