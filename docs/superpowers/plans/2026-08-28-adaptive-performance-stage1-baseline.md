# Adaptive Performance Stage 1 Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a reproducible, content-safe benchmark and instrumentation
foundation for the real controller, worker, and audio path without changing
synthesis behavior.

**Architecture:** A disabled-by-default, thread-safe `PerformanceRecorder`
accepts timestamped events and numeric observations from the controller,
worker, and stream transport. Versioned benchmark runners inject an enabled
recorder, sample process resources, execute deterministic fake and real-model
scenarios, and write schema-versioned JSONL for later policy decisions.

**Tech Stack:** Python 3.10 through 3.13, stdlib dataclasses/threading/resource/
ctypes/subprocess/statistics, NumPy, PySide6, Qt event loop, pytest, ruff,
VieNeu 3.3.0.

**Spec:** `docs/superpowers/specs/2026-08-28-adaptive-performance-resource-optimization-design.md`

## Scope

This plan implements only Stage 1, the reproducible baseline. Stages 2 through
6 intentionally remain separate plans because thread counts, buffer sizes,
backend-switch thresholds, and CUDA batches must be selected from this
stage's evidence.

Stage 1 adds observation only. It must not:

- change request ordering or cancellation;
- bound or replace the current waveform retention path;
- alter QAudioSink startup behavior;
- change backend selection;
- preload the model;
- add a runtime dependency;
- persist user text, voice names, file paths, audio, hostnames, serial
  numbers, or hardware UUIDs.

## Global Constraints

- Keep exactly one VieNeu engine instance and one inference worker.
- Keep production instrumentation disabled unless a benchmark or future
  local adaptive policy explicitly enables it.
- Use `time.perf_counter_ns()` for intra-process durations.
- Store event offsets from the trace start, never wall-clock event times.
- Benchmark records identify built-in corpus entries by ID and SHA-256 only.
- Use the repository's existing model-free construction and injectable fake
  patterns.
- Use no network and emit no telemetry outside a user-selected local path.
- Run timing-sensitive deterministic tests serially with `pytest -n 0`.
- Follow TDD and finish each task with focused tests before its commit.
- Commit steps are part of the repository workflow but require explicit user
  authorization when this plan is executed.
- Run the complete quality gate after the final task:
  `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`,
  `QT_QPA_PLATFORM=offscreen .venv/bin/pytest`.
- Do not commit real voice samples, generated audio, model files, usernames,
  hostnames, or machine identifiers.

## File map

### New production files

- `src/vienetts_app/core/performance.py`
  - Typed events, traces, local aggregation, JSON-safe snapshots, and a
    thread-safe disabled mode.

### Modified production files

- `src/vienetts_app/core/models.py`
  - Adds an optional opaque `job_id` to `TTSRequest`.
- `src/vienetts_app/workers/inference_worker.py`
  - Marks dequeue, engine start, first chunk, retention high-water,
    completion, cancellation, and failure.
- `src/vienetts_app/ui/controller.py`
  - Creates correlation IDs, starts traces, marks main-thread receipt and
    terminal state, and injects the recorder into default worker/audio
    objects.
- `src/vienetts_app/ui/stream_playback.py`
  - Observes first buffer append, first sink pull, buffered-byte high-water,
    restarts, and stops without changing transport behavior.

### New benchmark package

- `scripts/__init__.py`
- `scripts/benchmarks/__init__.py`
- `scripts/benchmarks/schema.py`
  - Stable JSONL record schema and environment manifest.
- `scripts/benchmarks/resources.py`
  - Portable current/peak RSS sampling and background sampler.
- `scripts/benchmarks/statistics.py`
  - Median, p90, p95, and median absolute deviation.
- `scripts/benchmarks/corpus.py`
  - Public deterministic benchmark corpus and hashes.
- `scripts/benchmarks/fakes.py`
  - Deterministic engine, rate-limited sink, and event-loop delay probe.
- `scripts/benchmarks/run_once.py`
  - One full controller, worker, stream transport scenario.
- `scripts/benchmarks/run_engine.py`
  - Direct engine-only scenario for comparison with the production path.
- `scripts/benchmarks/run_matrix.py`
  - Fresh-process orchestration and JSONL output.
- `scripts/benchmarks/summarize.py`
  - Human-readable and JSON summary from raw JSONL.
- `scripts/benchmarks/run_startup.py`
  - Process start through QML load, exposure, and first frame.
- `scripts/benchmarks/run_ui.py`
  - Real QML synthesis frame intervals and event-loop delay.

### New tests

- `tests/unit/test_performance.py`
- `tests/unit/test_benchmark_schema.py`
- `tests/unit/test_benchmark_resources.py`
- `tests/unit/test_benchmark_statistics.py`
- `tests/unit/test_benchmark_corpus.py`
- `tests/smoke/test_performance_harness.py`

### Modified tests

- `tests/unit/test_models.py`
- `tests/unit/test_inference_worker.py`
- `tests/unit/test_controller.py`
- `tests/unit/test_stream_playback.py`
- `tests/unit/test_app_entry.py`

### Documentation and evidence

- `docs/performance/README.md`
- `docs/performance/baselines/2026-08-28-apple-m4-macos.jsonl`
- `docs/performance/baselines/2026-08-28-apple-m4-macos-summary.json`
- `README.md`
- `PROJECT_PLAN.md`

---

### Task 1: Thread-safe performance trace model

**Files:**

- Create: `src/vienetts_app/core/performance.py`
- Create: `tests/unit/test_performance.py`

**Interfaces:**

- Produces:
  - `PerformanceEvent(name: str, offset_ns: int, value: int | float | None)`
  - `PerformanceTrace(job_id: str, started_ns: int, tags: dict[str, JSONScalar])`
  - `PerformanceRecorder(enabled=False, clock_ns=time.perf_counter_ns)`
  - `begin(job_id, tags) -> None`
  - `mark(job_id, name, value=None) -> None`
  - `observe_max(job_id, name, value) -> None`
  - `increment(job_id, name, amount=1) -> None`
  - `finish(job_id, outcome) -> None`
  - `snapshot(job_id=None) -> list[dict[str, object]]`
- Consumes: no app or Qt module.

- [ ] **Step 1: Write failing recorder contract tests**

Add tests that pin disabled behavior, event offsets, maxima, counters,
terminal outcome, immutable snapshots, content-safe tags, and thread safety.

```python
from concurrent.futures import ThreadPoolExecutor

import pytest

from vienetts_app.core.performance import PerformanceRecorder


class Clock:
    def __init__(self) -> None:
        self.value = 1_000

    def __call__(self) -> int:
        self.value += 10
        return self.value


def test_trace_uses_offsets_and_aggregates_numeric_values() -> None:
    recorder = PerformanceRecorder(enabled=True, clock_ns=Clock())
    recorder.begin("job-1", {"mode": "stream", "char_count": 42})
    recorder.mark("job-1", "submitted")
    recorder.observe_max("job-1", "retained_audio_bytes", 128)
    recorder.observe_max("job-1", "retained_audio_bytes", 96)
    recorder.increment("job-1", "chunks", 2)
    recorder.finish("job-1", "completed")

    (trace,) = recorder.snapshot("job-1")
    assert trace["job_id"] == "job-1"
    assert trace["tags"] == {"mode": "stream", "char_count": 42}
    assert trace["events"][0]["offset_ns"] >= 0
    assert trace["maxima"] == {"retained_audio_bytes": 128}
    assert trace["counters"] == {"chunks": 2}
    assert trace["outcome"] == "completed"


def test_disabled_recorder_retains_nothing() -> None:
    recorder = PerformanceRecorder(enabled=False)
    recorder.begin("job-1", {"mode": "stream"})
    recorder.mark("job-1", "submitted")
    recorder.finish("job-1", "completed")
    assert recorder.snapshot() == []


def test_tags_reject_content_bearing_keys() -> None:
    recorder = PerformanceRecorder(enabled=True)
    with pytest.raises(ValueError, match="tag key"):
        recorder.begin("job-1", {"text": "private input"})


def test_concurrent_marks_are_not_lost() -> None:
    recorder = PerformanceRecorder(enabled=True)
    recorder.begin("job-1", {"mode": "stream"})
    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: recorder.increment("job-1", "chunks"), range(500)))
    assert recorder.snapshot("job-1")[0]["counters"]["chunks"] == 500
```

The permitted tag keys are exactly:

```python
SAFE_TAG_KEYS = frozenset(
    {
        "backend",
        "char_count",
        "engine",
        "intra_op_threads",
        "max_batch_size",
        "mode",
        "precision",
        "run_kind",
        "scenario_id",
        "sink_kind",
        "streaming",
        "voice_kind",
    }
)
```

- [ ] **Step 2: Run the tests and verify the expected failure**

Run:

```bash
.venv/bin/pytest -n 0 tests/unit/test_performance.py -q
```

Expected: collection fails with
`ModuleNotFoundError: No module named 'vienetts_app.core.performance'`.

- [ ] **Step 3: Implement the recorder**

Use frozen event records, mutable private trace state, one `threading.RLock`,
copy-on-snapshot, and JSON scalar validation. Unknown job IDs are ignored so
disabled or partially instrumented paths cannot break synthesis.

```python
JSONScalar = str | int | float | bool | None


@dataclass(frozen=True)
class PerformanceEvent:
    name: str
    offset_ns: int
    value: int | float | None = None


@dataclass
class _TraceState:
    job_id: str
    started_ns: int
    tags: dict[str, JSONScalar]
    events: list[PerformanceEvent] = field(default_factory=list)
    maxima: dict[str, int | float] = field(default_factory=dict)
    counters: dict[str, int | float] = field(default_factory=dict)
    outcome: str | None = None


class PerformanceRecorder:
    def __init__(
        self,
        *,
        enabled: bool = False,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        self.enabled = bool(enabled)
        self._clock_ns = clock_ns
        self._lock = threading.RLock()
        self._traces: dict[str, _TraceState] = {}

    def begin(self, job_id: str, tags: Mapping[str, JSONScalar]) -> None:
        if not self.enabled:
            return
        if not job_id.strip():
            raise ValueError("job_id must not be blank")
        unknown = set(tags) - SAFE_TAG_KEYS
        if unknown:
            raise ValueError(f"unsafe tag key(s): {sorted(unknown)}")
        with self._lock:
            self._traces[job_id] = _TraceState(
                job_id=job_id,
                started_ns=self._clock_ns(),
                tags=dict(tags),
            )

    def mark(self, job_id: str | None, name: str, value: int | float | None = None) -> None:
        if not self.enabled or job_id is None:
            return
        with self._lock:
            trace = self._traces.get(job_id)
            if trace is None:
                return
            trace.events.append(
                PerformanceEvent(
                    name=name,
                    offset_ns=max(0, self._clock_ns() - trace.started_ns),
                    value=value,
                )
            )
```

Implement `observe_max`, `increment`, `finish`, and `snapshot` with the same
lock. `snapshot` returns new dictionaries and lists so callers cannot mutate
internal state.

- [ ] **Step 4: Run recorder tests**

Run:

```bash
.venv/bin/pytest -n 0 tests/unit/test_performance.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Run lint on the new module**

Run:

```bash
.venv/bin/ruff check src/vienetts_app/core/performance.py tests/unit/test_performance.py
.venv/bin/ruff format --check src/vienetts_app/core/performance.py tests/unit/test_performance.py
```

Expected: both commands exit 0.

- [ ] **Step 6: Commit the recorder**

```bash
git add src/vienetts_app/core/performance.py tests/unit/test_performance.py
git commit -m "feat(perf): add local performance recorder"
```

---

### Task 2: Request correlation and worker boundary metrics

**Files:**

- Modify: `src/vienetts_app/core/models.py`
- Modify: `src/vienetts_app/workers/inference_worker.py`
- Modify: `tests/unit/test_models.py`
- Modify: `tests/unit/test_inference_worker.py`

**Interfaces:**

- Consumes: `PerformanceRecorder` from Task 1.
- Produces:
  - `TTSRequest.job_id: str | None`
  - `InferenceWorker(engine, parent=None, performance_recorder=None)`
  - Worker event names:
    `worker_dequeued`, `engine_call_started`, `worker_first_chunk`,
    `worker_completed`, `worker_cancelled`, `worker_failed`
  - Maxima:
    `retained_chunk_bytes`, `concatenated_audio_bytes`
  - Counter: `chunks_produced`

- [ ] **Step 1: Add failing model validation tests**

```python
def test_optional_job_id_is_opaque_and_non_blank() -> None:
    assert TTSRequest(text="hi").job_id is None
    assert TTSRequest(text="hi", job_id="job-123").job_id == "job-123"
    with pytest.raises(ValueError, match="job_id"):
        TTSRequest(text="hi", job_id=" ")
    with pytest.raises(TypeError, match="job_id"):
        TTSRequest(text="hi", job_id=123)
```

- [ ] **Step 2: Add failing worker instrumentation tests**

Use an enabled recorder and direct `_process` calls to avoid timing noise.

```python
def test_stream_records_worker_boundaries_and_retention() -> None:
    recorder = PerformanceRecorder(enabled=True)
    recorder.begin("job-1", {"mode": "stream", "char_count": 2})
    worker, box = make_worker_with_signals(
        RecordingEngine(chunks_per_stream=2, chunk_delay=0.0),
        performance_recorder=recorder,
    )

    worker._process(TTSRequest(text="hi", mode="stream", job_id="job-1"))

    trace = recorder.snapshot("job-1")[0]
    names = [event["name"] for event in trace["events"]]
    assert names == [
        "worker_dequeued",
        "engine_call_started",
        "worker_first_chunk",
        "worker_completed",
    ]
    assert trace["counters"]["chunks_produced"] == 2
    assert trace["maxima"]["retained_chunk_bytes"] == 2 * 15_360 * 4
    assert trace["maxima"]["concatenated_audio_bytes"] == 2 * 15_360 * 4
    assert box["error"] == []
```

Add equivalent tests for infer completion, cancellation, `TTSEngineError`,
and unexpected exceptions. The recorder must never change emitted Qt signal
payloads.

- [ ] **Step 3: Run focused tests and verify failure**

Run:

```bash
.venv/bin/pytest -n 0 \
  tests/unit/test_models.py::TestTTSRequest \
  tests/unit/test_inference_worker.py -q
```

Expected: tests fail because `job_id`, the constructor argument, and events
do not exist.

- [ ] **Step 4: Add the optional request ID**

Add after `temperature`:

```python
job_id: str | None = None
```

Validate it without requiring UUID syntax:

```python
if self.job_id is not None:
    if not isinstance(self.job_id, str):
        raise TypeError("job_id must be a string or None")
    if not self.job_id.strip():
        raise ValueError("job_id must be a non-empty, non-blank string")
```

- [ ] **Step 5: Instrument worker entry and terminal paths**

Store a disabled recorder when none is supplied:

```python
def __init__(
    self,
    engine: TTSEngine | Any,
    parent: Any | None = None,
    performance_recorder: PerformanceRecorder | None = None,
) -> None:
    super().__init__(parent)
    self.engine = engine
    self._performance = performance_recorder or PerformanceRecorder()
```

At `_process` entry:

```python
job_id = request.job_id if isinstance(request, TTSRequest) else None
self._performance.mark(job_id, "worker_dequeued")
```

Immediately before each `engine.infer`, first `engine.infer_stream`, or
`engine.infer_batch` call:

```python
self._performance.mark(request.job_id, "engine_call_started")
```

In stream mode, use a boolean to mark only the first chunk. Observe retained
bytes after appending, but do not alter the existing list or concatenate
behavior:

```python
retained_bytes = 0
first_chunk = True
for chunk in self.engine.infer_stream(
    segment,
    voice=request.voice,
    temperature=request.temperature,
):
    array = np.asarray(chunk, dtype=np.float32)
    chunks.append(array)
    retained_bytes += int(array.nbytes)
    self._performance.increment(request.job_id, "chunks_produced")
    self._performance.observe_max(
        request.job_id,
        "retained_chunk_bytes",
        retained_bytes,
    )
    if first_chunk:
        self._performance.mark(request.job_id, "worker_first_chunk")
        first_chunk = False
    self.chunk_ready.emit(array)
```

Record the final array size before emitting:

```python
audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
self._performance.observe_max(
    request.job_id,
    "concatenated_audio_bytes",
    int(audio.nbytes),
)
self._performance.mark(request.job_id, "worker_completed")
self.done.emit(audio)
```

Record cancellation once before the existing cancellation error. In
`_process` exception handlers, mark `worker_failed` with no error text or
exception representation.

- [ ] **Step 6: Run focused worker and model tests**

Run:

```bash
.venv/bin/pytest -n 0 \
  tests/unit/test_models.py \
  tests/unit/test_inference_worker.py -q
```

Expected: all tests pass, including all pre-existing signal and cancellation
contracts.

- [ ] **Step 7: Run focused lint**

```bash
.venv/bin/ruff check \
  src/vienetts_app/core/models.py \
  src/vienetts_app/workers/inference_worker.py \
  tests/unit/test_models.py \
  tests/unit/test_inference_worker.py
.venv/bin/ruff format --check \
  src/vienetts_app/core/models.py \
  src/vienetts_app/workers/inference_worker.py \
  tests/unit/test_models.py \
  tests/unit/test_inference_worker.py
```

Expected: both commands exit 0.

- [ ] **Step 8: Commit worker instrumentation**

```bash
git add \
  src/vienetts_app/core/models.py \
  src/vienetts_app/workers/inference_worker.py \
  tests/unit/test_models.py \
  tests/unit/test_inference_worker.py
git commit -m "feat(perf): trace inference worker boundaries"
```

---

### Task 3: Controller and stream transport metrics

**Files:**

- Modify: `src/vienetts_app/ui/controller.py`
- Modify: `src/vienetts_app/ui/stream_playback.py`
- Modify: `tests/unit/test_controller.py`
- Modify: `tests/unit/test_stream_playback.py`

**Interfaces:**

- Consumes:
  - `TTSRequest.job_id`
  - `PerformanceRecorder`
- Produces:
  - `AppController(..., performance_recorder=None)`
  - `StreamPlaybackController(..., performance_recorder=None)`
  - `StreamPlaybackController.begin_trace(job_id) -> None`
  - Controller events:
    `submitted`, `controller_first_chunk`, `controller_done`,
    `cancel_requested`, `controller_error`
  - Audio events:
    `audio_session_started`, `audio_first_buffer_append`,
    `audio_first_sink_pull`, `audio_session_stopped`
  - Audio maxima/counters:
    `audio_buffer_bytes`, `audio_restarts`

- [ ] **Step 1: Add failing controller correlation tests**

Inject an enabled recorder into the existing `Harness`. Assert generated
requests receive unique IDs and that traces contain only numeric or
categorical tags.

```python
def test_generate_stream_starts_content_safe_trace(
    qcoreapp: Any,
    tmp_path: Path,
) -> None:
    recorder = PerformanceRecorder(enabled=True)
    harness = Harness(tmp_path, performance_recorder=recorder)
    harness.controller.generateStream("private words", "Minh Đức")

    (request,) = harness.worker.submitted
    assert request.job_id
    trace = recorder.snapshot(request.job_id)[0]
    assert trace["tags"] == {
        "char_count": 13,
        "mode": "stream",
        "streaming": True,
    }
    serialized = json.dumps(trace, ensure_ascii=False)
    assert "private words" not in serialized
    assert "Minh Đức" not in serialized
```

Add tests for first controller chunk, completion, cancellation, and failure.
Two sequential submissions must receive different IDs.

- [ ] **Step 2: Add failing stream transport tests**

Extend `FakeSink` tests so reading from the QIODevice records the first sink
pull and buffer high-water.

```python
def test_stream_records_first_append_pull_and_high_water(
    qcoreapp: Any,
) -> None:
    recorder = PerformanceRecorder(enabled=True)
    sink = FakeSink()
    controller = StreamPlaybackController(
        sink_factory=lambda _fmt: sink,
        format_factory=FakeFormat,
        performance_recorder=recorder,
    )
    recorder.begin("job-1", {"mode": "stream"})
    controller.begin_trace("job-1")
    controller.start()
    controller.feed(np.zeros(16, dtype=np.float32))
    assert sink.device.readData(64) == bytes(64)
    controller.stop()

    trace = recorder.snapshot("job-1")[0]
    names = [event["name"] for event in trace["events"]]
    assert "audio_first_buffer_append" in names
    assert "audio_first_sink_pull" in names
    assert trace["maxima"]["audio_buffer_bytes"] == 64
```

Add an underrun/restart test asserting `audio_restarts == 1`.

- [ ] **Step 3: Run focused tests and verify failure**

```bash
.venv/bin/pytest -n 0 \
  tests/unit/test_controller.py \
  tests/unit/test_stream_playback.py -q
```

Expected: failures report unsupported recorder arguments, missing
`begin_trace`, and absent IDs/events.

- [ ] **Step 4: Add controller recorder injection and IDs**

Add the constructor argument:

```python
performance_recorder: PerformanceRecorder | None = None,
```

Store:

```python
self._performance = performance_recorder or PerformanceRecorder()
self._active_job_id: str | None = None
self._controller_saw_chunk = False
```

Create private ID and trace helpers:

```python
@staticmethod
def _new_job_id() -> str:
    return uuid.uuid4().hex


def _begin_trace(self, *, job_id: str, text: str, mode: str) -> None:
    self._active_job_id = job_id
    self._controller_saw_chunk = False
    self._performance.begin(
        job_id,
        {
            "char_count": len(text),
            "mode": mode,
            "streaming": mode == "stream",
        },
    )
    self._performance.mark(job_id, "submitted")
```

For each submit path, create an ID, validate the request with that ID, then
begin the trace before `_begin_synthesis()` or worker creation:

```python
job_id = self._new_job_id()
try:
    request = TTSRequest(
        text=text,
        voice=voice or None,
        mode="stream",
        temperature=self._settings.temperature,
        job_id=job_id,
    )
except ValueError as exc:
    self._set_error(f"Invalid request: {exc}")
    return
self._begin_trace(job_id=job_id, text=text, mode="stream")
```

Listener submissions use the same ordering. Do not add source text, voice
ID, file path, or error message to the trace. Stage 1 benchmarks admit one
active controller request at a time because worker signals do not carry IDs
yet. Stage 2 replaces that limitation with fully job-scoped signals.

When creating default dependencies:

```python
self._worker = InferenceWorker(
    self._engine,
    performance_recorder=self._performance,
)
```

and:

```python
player = self._stream_playback_factory()
if hasattr(player, "set_performance_recorder"):
    player.set_performance_recorder(self._performance)
```

Prefer adding `performance_recorder` to the default playback factory closure
over changing existing injected zero-argument factory contracts.

Before starting a stream session, correlate the audio object:

```python
if hasattr(player, "begin_trace"):
    player.begin_trace(self._active_job_id)
player.start()
```

- [ ] **Step 5: Mark main-thread boundaries and terminal state**

On the first `_on_chunk_ready` for the active job:

```python
if not self._controller_saw_chunk:
    self._performance.mark(self._active_job_id, "controller_first_chunk")
    self._controller_saw_chunk = True
```

Before the normal done handling:

```python
self._performance.mark(self._active_job_id, "controller_done")
self._performance.finish(self._active_job_id, "completed")
```

On cancellation use outcome `cancelled`; on all other errors use `failed`.
Never attach the error text.

At the beginning of `cancel()` mark `cancel_requested` for the active ID
before calling the worker. This observes acknowledgement latency without
changing cancellation behavior.

Clear `_active_job_id` only after the existing listener or normal route has
received its terminal callback.

- [ ] **Step 6: Add stream recorder support without behavior changes**

Add:

```python
def set_performance_recorder(self, recorder: PerformanceRecorder) -> None:
    self._performance = recorder


def begin_trace(self, job_id: str | None) -> None:
    self._trace_job_id = job_id
    self._saw_buffer_append = False
```

Let `StreamIODevice` accept an optional first-read callback:

```python
def __init__(
    self,
    parent: QObject | None = None,
    on_first_read: Callable[[], None] | None = None,
) -> None:
    super().__init__(parent)
    self._on_first_read = on_first_read
    self._reported_first_read = False
```

In `readData`, call it only for the first non-empty read:

```python
data = self.take_bytes(int(maxSize))
if data and not self._reported_first_read:
    self._reported_first_read = True
    if self._on_first_read is not None:
        self._on_first_read()
return data
```

Observe `len(io)` after every append. Increment `audio_restarts` only when
the current `_sink_is_stalled()` branch restarts an already started sink.
Mark session start and stop without changing QAudioSink wiring.

- [ ] **Step 7: Run controller and transport tests**

```bash
.venv/bin/pytest -n 0 \
  tests/unit/test_controller.py \
  tests/unit/test_stream_playback.py -q
```

Expected: all tests pass. Existing byte ordering, replay, cancellation, and
sink-call sequences remain unchanged.

- [ ] **Step 8: Run worker/controller integration smoke**

```bash
.venv/bin/pytest -n 0 \
  tests/unit/test_controller.py::test_worker_thread_safety_smoke -q
```

Expected: pass with the real `InferenceWorker` and fake engine/sink.

- [ ] **Step 9: Run focused lint**

```bash
.venv/bin/ruff check \
  src/vienetts_app/ui/controller.py \
  src/vienetts_app/ui/stream_playback.py \
  tests/unit/test_controller.py \
  tests/unit/test_stream_playback.py
.venv/bin/ruff format --check \
  src/vienetts_app/ui/controller.py \
  src/vienetts_app/ui/stream_playback.py \
  tests/unit/test_controller.py \
  tests/unit/test_stream_playback.py
```

Expected: both commands exit 0.

- [ ] **Step 10: Commit UI-path instrumentation**

```bash
git add \
  src/vienetts_app/ui/controller.py \
  src/vienetts_app/ui/stream_playback.py \
  tests/unit/test_controller.py \
  tests/unit/test_stream_playback.py
git commit -m "feat(perf): trace controller and audio transport"
```

---

### Task 4: Portable resource sampling and summary statistics

**Files:**

- Create: `scripts/__init__.py`
- Create: `scripts/benchmarks/__init__.py`
- Create: `scripts/benchmarks/resources.py`
- Create: `scripts/benchmarks/statistics.py`
- Create: `tests/unit/test_benchmark_resources.py`
- Create: `tests/unit/test_benchmark_statistics.py`

**Interfaces:**

- Produces:
  - `ResourceSample(monotonic_ns, current_rss_bytes, peak_rss_bytes, process_cpu_ns)`
  - `current_rss_bytes(pid=None) -> int`
  - `peak_rss_bytes() -> int`
  - `ResourceSampler(interval_seconds=0.1, sample_cuda=False)`
  - `cuda_memory_bytes() -> CudaMemorySample | None`
  - `summarize(values) -> Distribution`
  - `Distribution(count, minimum, median, p90, p95, maximum, mad)`
- Consumes: stdlib only.

- [ ] **Step 1: Write failing parser and statistics tests**

```python
def test_parse_linux_proc_status() -> None:
    text = "Name:\tpython\nVmRSS:\t  12345 kB\nVmHWM:\t  23456 kB\n"
    current, peak = _parse_proc_status(text)
    assert current == 12_345 * 1024
    assert peak == 23_456 * 1024


def test_parse_ps_rss_uses_kib() -> None:
    assert _parse_ps_rss(" 2048\n") == 2 * 1024 * 1024


def test_distribution_uses_nearest_rank_percentiles() -> None:
    result = summarize([1.0, 2.0, 3.0, 4.0, 100.0])
    assert result.count == 5
    assert result.median == 3.0
    assert result.p90 == 100.0
    assert result.p95 == 100.0
    assert result.mad == 1.0
```

Also test empty input raises `ValueError`, a one-value distribution, sampler
start/stop idempotence, preservation of the last and maximum samples,
monotonic process CPU time, CUDA-disabled behavior without importing torch,
and CUDA values through an injected fake probe.

- [ ] **Step 2: Run tests and verify failure**

```bash
.venv/bin/pytest -n 0 \
  tests/unit/test_benchmark_resources.py \
  tests/unit/test_benchmark_statistics.py -q
```

Expected: import errors for missing benchmark modules.

- [ ] **Step 3: Implement current and peak RSS**

Linux current and peak values come from `/proc/<pid>/status`. macOS current
RSS comes from `ps -o rss= -p <pid>` and peak RSS from `resource.getrusage`.
Windows uses `GetProcessMemoryInfo` through `ctypes`.

Use exact byte units at the API boundary:

```python
def _parse_proc_status(text: str) -> tuple[int, int]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, separator, rest = line.partition(":")
        if separator and key in {"VmRSS", "VmHWM"}:
            values[key] = int(rest.split()[0]) * 1024
    if "VmRSS" not in values:
        raise RuntimeError("VmRSS missing from proc status")
    return values["VmRSS"], values.get("VmHWM", values["VmRSS"])


def _parse_ps_rss(text: str) -> int:
    return int(text.strip()) * 1024
```

For Windows define `PROCESS_MEMORY_COUNTERS` with `WorkingSetSize` and
`PeakWorkingSetSize`, open the process with query rights, and always close
the handle in `finally`. Tests mock the platform-specific call boundary
rather than requiring Windows APIs on macOS/Linux.

For unsupported platforms raise `RuntimeError` with the platform name.

- [ ] **Step 4: Implement the resource sampler**

`ResourceSampler` owns one daemon `threading.Thread`, samples current/peak
RSS and `time.process_time_ns()` at the configured interval, and captures
sampling errors as strings in its final result rather than crashing the
benchmark.

```python
with ResourceSampler(interval_seconds=0.1) as sampler:
    run_scenario()
result = sampler.result()
assert result.sample_count >= 1
```

The result contains sample count, first/current/max RSS, peak RSS, process
CPU delta, total CPU utilization (`cpu_seconds / wall_seconds * 100`, which
may exceed 100 on multicore work), utilization normalized by logical CPU
count, and an optional error. It stores no process command line.

When `sample_cuda=True`, lazily import torch inside `cuda_memory_bytes()` and
record allocated, reserved, maximum allocated, and maximum reserved bytes.
Call `torch.cuda.reset_peak_memory_stats()` only after confirming CUDA is
initialized and immediately before the measured workload. Do not call
`empty_cache()` inside measured iterations because it changes allocator
behavior and does not increase memory available to PyTorch.

- [ ] **Step 5: Implement stable statistics**

Use sorted nearest-rank percentiles:

```python
def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]
```

Use `statistics.median` for median and median absolute deviation.

- [ ] **Step 6: Run resource/statistics tests**

```bash
.venv/bin/pytest -n 0 \
  tests/unit/test_benchmark_resources.py \
  tests/unit/test_benchmark_statistics.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Run focused lint**

```bash
.venv/bin/ruff check scripts/benchmarks tests/unit/test_benchmark_resources.py \
  tests/unit/test_benchmark_statistics.py
.venv/bin/ruff format --check scripts/benchmarks \
  tests/unit/test_benchmark_resources.py tests/unit/test_benchmark_statistics.py
```

Expected: both commands exit 0.

- [ ] **Step 8: Commit resource helpers**

```bash
git add \
  scripts/__init__.py \
  scripts/benchmarks/__init__.py \
  scripts/benchmarks/resources.py \
  scripts/benchmarks/statistics.py \
  tests/unit/test_benchmark_resources.py \
  tests/unit/test_benchmark_statistics.py
git commit -m "feat(perf): add portable benchmark metrics"
```

---

### Task 5: Content-safe result schema and deterministic corpus

**Files:**

- Create: `scripts/benchmarks/schema.py`
- Create: `scripts/benchmarks/corpus.py`
- Create: `tests/unit/test_benchmark_schema.py`
- Create: `tests/unit/test_benchmark_corpus.py`

**Interfaces:**

- Produces:
  - `SCHEMA_VERSION = 1`
  - `BenchmarkEnvironment`
  - `BenchmarkScenario`
  - `BenchmarkRecord`
  - `environment_manifest()`
  - `write_jsonl(records, path)`
  - `CORPUS: dict[str, CorpusEntry]`
  - `get_corpus_entry(scenario_id)`
- Consumes: `PerformanceRecorder.snapshot()` output.

- [ ] **Step 1: Write failing schema safety tests**

```python
def test_environment_excludes_identity_fields(monkeypatch) -> None:
    manifest = environment_manifest()
    serialized = json.dumps(manifest.to_dict())
    forbidden = {"hostname", "serial", "hardware_uuid", "username", "home"}
    assert not forbidden.intersection(manifest.to_dict())
    assert str(Path.home()) not in serialized


def test_record_contains_corpus_identity_not_text() -> None:
    entry = get_corpus_entry("vi_50")
    scenario = BenchmarkScenario.from_entry(
        entry,
        backend="onnx",
        precision="int8",
        mode="stream",
    )
    payload = scenario.to_dict()
    assert payload["scenario_id"] == "vi_50"
    assert payload["text_sha256"] == entry.sha256
    assert payload["char_count"] == len(entry.text)
    assert entry.text not in json.dumps(payload, ensure_ascii=False)
```

Add a test that writes two records to JSONL and reads each line as valid JSON.

- [ ] **Step 2: Write failing corpus coverage tests**

Pin these IDs:

```python
EXPECTED_IDS = {
    "vi_20",
    "vi_50",
    "vi_256",
    "vi_512",
    "vi_2000",
    "vi_5000",
    "en_short",
    "code_switch",
    "numbers",
    "emotion",
    "multiline",
    "punctuation_free",
}
```

Assert every entry is nonblank UTF-8, has a stable SHA-256, and matches its
declared character count. Construct longer entries deterministically from
fixed public-domain-like project sentences, not user content.

- [ ] **Step 3: Run tests and verify failure**

```bash
.venv/bin/pytest -n 0 \
  tests/unit/test_benchmark_schema.py \
  tests/unit/test_benchmark_corpus.py -q
```

Expected: missing module import failures.

- [ ] **Step 4: Implement the environment manifest**

Include only:

- schema version;
- UTC run timestamp;
- Python implementation/version;
- OS system/release/version;
- machine architecture;
- logical CPU count;
- total RAM bytes when available;
- a caller-supplied non-identifying hardware class, such as
  `apple-m4-10c-16gb`, `x86-8c-16gb`, or `nvidia-12gb`;
- package versions for `vienetts-app`, `vieneu`, `onnxruntime`, `numpy`,
  `PySide6`, `torch`, and `torchaudio`;
- git commit SHA and dirty boolean;
- explicit benchmark command version.

Do not include environment variables or inspect serial-bearing hardware
reports. The CLI requires `--hardware-class` for real baselines and defaults
to `unspecified` for fake tests. Validate it against
`[a-z0-9][a-z0-9-]{0,63}` and document that it describes capability, never a
unique device. Invoke git with:

```python
subprocess.run(
    ["git", "rev-parse", "HEAD"],
    cwd=repo_root,
    check=True,
    capture_output=True,
    text=True,
)
```

Use a separate `git status --porcelain` call for the dirty flag. A git error
sets both fields to `None` rather than failing a benchmark.

- [ ] **Step 5: Implement corpus entries and schema serialization**

Use frozen dataclasses. Compute hashes in `CorpusEntry.__post_init__` or a
constructor helper and expose no raw text through `to_dict()`.

```python
@dataclass(frozen=True)
class CorpusEntry:
    scenario_id: str
    text: str
    language_class: str

    @property
    def sha256(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()

    def identity(self) -> dict[str, str | int]:
        return {
            "scenario_id": self.scenario_id,
            "text_sha256": self.sha256,
            "char_count": len(self.text),
            "language_class": self.language_class,
        }
```

`BenchmarkRecord.to_dict()` combines environment, scenario identity, trace,
resource result, audio duration, elapsed duration, and derived RTF. Validate
that all durations and byte counts are nonnegative.

- [ ] **Step 6: Run schema/corpus tests**

```bash
.venv/bin/pytest -n 0 \
  tests/unit/test_benchmark_schema.py \
  tests/unit/test_benchmark_corpus.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Run focused lint**

```bash
.venv/bin/ruff check \
  scripts/benchmarks/schema.py \
  scripts/benchmarks/corpus.py \
  tests/unit/test_benchmark_schema.py \
  tests/unit/test_benchmark_corpus.py
.venv/bin/ruff format --check \
  scripts/benchmarks/schema.py \
  scripts/benchmarks/corpus.py \
  tests/unit/test_benchmark_schema.py \
  tests/unit/test_benchmark_corpus.py
```

Expected: both commands exit 0.

- [ ] **Step 8: Commit schema and corpus**

```bash
git add \
  scripts/benchmarks/schema.py \
  scripts/benchmarks/corpus.py \
  tests/unit/test_benchmark_schema.py \
  tests/unit/test_benchmark_corpus.py
git commit -m "feat(perf): define benchmark schema and corpus"
```

---

### Task 6: Deterministic full-pipeline benchmark

**Files:**

- Create: `scripts/benchmarks/fakes.py`
- Create: `scripts/benchmarks/run_once.py`
- Create: `tests/smoke/test_performance_harness.py`

**Interfaces:**

- Consumes:
  - `AppController`
  - real `InferenceWorker`
  - real `StreamPlaybackController`
  - Tasks 1 through 5
- Produces:
  - `DeterministicEngine`
  - `RateLimitedSink`
  - `EventLoopProbe`
  - CLI:
    `python -m scripts.benchmarks.run_once --engine fake --scenario vi_50 --mode stream`

- [ ] **Step 1: Write a failing subprocess smoke test**

```python
def test_fake_pipeline_emits_one_content_safe_record(tmp_path: Path) -> None:
    output = tmp_path / "record.jsonl"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.benchmarks.run_once",
            "--engine",
            "fake",
            "--scenario",
            "vi_50",
            "--mode",
            "stream",
            "--output",
            str(output),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )
    assert proc.returncode == 0, proc.stderr
    payload = json.loads(output.read_text(encoding="utf-8"))
    assert payload["schema_version"] == 1
    assert payload["scenario"]["scenario_id"] == "vi_50"
    assert payload["trace"]["outcome"] == "completed"
    names = [event["name"] for event in payload["trace"]["events"]]
    assert "worker_first_chunk" in names
    assert "controller_first_chunk" in names
    assert "audio_first_buffer_append" in names
    assert "audio_first_sink_pull" in names
    assert payload["resources"]["sample_count"] >= 1
    assert payload["event_loop"]["sample_count"] >= 1
```

Add a second test with a slow sink and enough fake audio to prove
`audio_buffer_bytes` rises while the benchmark still exits. Add an in-flight
cancellation case that requests cancel after the first controller chunk and
asserts `cancel_requested`, `worker_cancelled`, and outcome `cancelled`.

- [ ] **Step 2: Run the smoke test and verify failure**

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -n 0 \
  tests/smoke/test_performance_harness.py -q
```

Expected: subprocess fails because `run_once` does not exist.

- [ ] **Step 3: Implement the deterministic engine**

`DeterministicEngine` implements the existing TTSEngine duck type:

```python
class DeterministicEngine:
    sample_rate = 48_000
    backend = "onnx"

    def __init__(
        self,
        *,
        chunk_samples: int = 4_800,
        chunks_per_segment: int = 4,
        chunk_delay_ms: float = 5.0,
    ) -> None:
        self.chunk_samples = chunk_samples
        self.chunks_per_segment = chunks_per_segment
        self.chunk_delay_ms = chunk_delay_ms

    def infer_stream(self, text: str, voice: str | None = None, **kwargs):
        del text, voice, kwargs
        for index in range(self.chunks_per_segment):
            time.sleep(self.chunk_delay_ms / 1000)
            yield np.full(
                self.chunk_samples,
                ((index % 4) + 1) / 10,
                dtype=np.float32,
            )

    def infer(self, text: str, voice: str | None = None, **kwargs) -> np.ndarray:
        return np.concatenate(list(self.infer_stream(text, voice, **kwargs)))

    def close(self) -> None:
        return None
```

Add `infer_batch` only to satisfy the current engine surface. It maps
`infer` sequentially because the deterministic runner measures plumbing, not
GPU batching.

- [ ] **Step 4: Implement the rate-limited sink**

The sink receives the real `StreamIODevice` and uses a `QTimer` to pull a
fixed number of bytes every 10 ms. It exposes the existing fake contract:
`start(device)`, `stop()`, `state()`, and `stateChanged`.

At 48 kHz mono float32, real-time consumption is 1,920 bytes per 10 ms.
CLI flags allow faster, real-time, and slower-than-real-time rates.

```python
BYTES_PER_SECOND = 48_000 * 4


def bytes_per_tick(rate: float, interval_ms: int = 10) -> int:
    return max(1, round(BYTES_PER_SECOND * rate * interval_ms / 1000))
```

Do not retain consumed bytes. Count total consumed bytes and transition to
`IdleState` only when explicitly instructed by a test scenario.

- [ ] **Step 5: Implement the event-loop probe**

`EventLoopProbe` uses a repeating 10 ms `QTimer`. At each timeout it compares
the actual monotonic interval with the requested interval and records only
numeric delay values:

```python
class EventLoopProbe(QObject):
    def __init__(self, interval_ms: int = 10) -> None:
        super().__init__()
        self._interval_ns = interval_ms * 1_000_000
        self._last_ns: int | None = None
        self.delays_ms: list[float] = []
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._tick)

    def _tick(self) -> None:
        now = time.perf_counter_ns()
        if self._last_ns is not None:
            elapsed = now - self._last_ns
            delay_ns = max(0, elapsed - self._interval_ns)
            self.delays_ms.append(delay_ns / 1_000_000)
        self._last_ns = now
```

The benchmark stores sample count, median, p95, and maximum delay. A missing
sample set is reported as unsupported, never as zero.

- [ ] **Step 6: Implement one production-path run**

`run_once` must:

1. parse explicit arguments;
2. select a built-in corpus entry;
3. create an enabled `PerformanceRecorder`;
4. start `ResourceSampler` and `EventLoopProbe`;
5. build `AppController` with `DeterministicEngine`, the real
   `InferenceWorker`, and real `StreamPlaybackController` wrapping
   `RateLimitedSink`;
6. call `generateStream` or `generate`;
7. pump `QCoreApplication.processEvents()` until terminal state;
8. continue pumping until the fake sink drains or a fixed timeout expires;
9. call `controller.shutdown()` in `finally`;
10. write one `BenchmarkRecord` atomically;
11. print only the output path and summary metrics.

Required CLI defaults:

```text
--engine fake
--scenario vi_50
--mode stream
--backend onnx
--precision int8
--sink fake
--sink-rate 1.0
--cancel-after-first-chunk false
--warmup-iterations 0
--iterations 1
--timeout 30
--output benchmark-record.jsonl
```

Every output is JSONL, including a one-record run. The real-engine and real
QAudioSink branches are accepted by the parser but implemented in Task 7.

- [ ] **Step 7: Run deterministic smoke tests**

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -n 0 \
  tests/smoke/test_performance_harness.py -q
```

Expected: all fake benchmark subprocess tests pass.

- [ ] **Step 8: Run the benchmark manually**

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python \
  -m scripts.benchmarks.run_once \
  --engine fake \
  --scenario vi_512 \
  --mode stream \
  --sink fake \
  --sink-rate 0.5 \
  --output /tmp/vienetts-fake-record.jsonl
```

Expected: exit 0, valid JSON, completed trace, positive elapsed/audio
durations, and `audio_buffer_bytes` above zero.

- [ ] **Step 9: Run focused lint**

```bash
.venv/bin/ruff check \
  scripts/benchmarks/fakes.py \
  scripts/benchmarks/run_once.py \
  tests/smoke/test_performance_harness.py
.venv/bin/ruff format --check \
  scripts/benchmarks/fakes.py \
  scripts/benchmarks/run_once.py \
  tests/smoke/test_performance_harness.py
```

Expected: both commands exit 0.

- [ ] **Step 10: Commit the fake pipeline benchmark**

```bash
git add \
  scripts/benchmarks/fakes.py \
  scripts/benchmarks/run_once.py \
  tests/smoke/test_performance_harness.py
git commit -m "test(perf): benchmark the full streaming pipeline"
```

---

### Task 7: Real-model matrix, startup timing, and summaries

**Files:**

- Modify: `src/vienetts_app/core/engine.py`
- Modify: `scripts/benchmarks/run_once.py`
- Create: `scripts/benchmarks/run_matrix.py`
- Create: `scripts/benchmarks/run_engine.py`
- Create: `scripts/benchmarks/summarize.py`
- Create: `scripts/benchmarks/run_startup.py`
- Create: `scripts/benchmarks/run_ui.py`
- Modify: `tests/smoke/test_performance_harness.py`
- Modify: `tests/unit/test_engine.py`
- Modify: `tests/unit/test_app_entry.py`
- Modify: `src/vienetts_app/app.py`

**Interfaces:**

- Consumes: all earlier benchmark interfaces.
- Produces:
  - real engine mode for `run_once`;
  - direct-engine mode for separating SDK/engine time from app-path time;
  - fake, real, and null audio-sink modes for `run_once`;
  - fresh-process matrix CLI;
  - deterministic summary CLI;
  - startup trace events:
    `process_started`, `qml_loaded`, `window_exposed`, `first_frame_swapped`.
  - QML synthesis frame intervals and event-loop delays from `run_ui`.
  - `TTSEngine.initialize() -> None` for explicit benchmark-only warmup and
    initialization timing.
  - optional `TTSEngine(..., threads=None, max_batch_size=None)` pass-through
    for controlled benchmark sweeps without changing app defaults.

- [ ] **Step 1: Add failing matrix and summary CLI tests**

Run a two-iteration fake matrix and assert two valid JSONL records plus a
summary with count, median, p90, p95, and MAD.

```python
def test_fake_matrix_and_summary(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    summary = tmp_path / "summary.json"
    run_module(
        "scripts.benchmarks.run_matrix",
        "--engine",
        "fake",
        "--scenario",
        "vi_50",
        "--cold-iterations",
        "2",
        "--warm-iterations",
        "0",
        "--output",
        str(raw),
    )
    run_module(
        "scripts.benchmarks.summarize",
        str(raw),
        "--output",
        str(summary),
    )
    lines = raw.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    payload = json.loads(summary.read_text(encoding="utf-8"))
    assert payload["count"] == 2
    assert "ttfc_ms" in payload["distributions"]
```

- [ ] **Step 2: Add failing startup and QML-frame tests**

Inject an enabled recorder or startup callback into `create_app`. Run QML in
a subprocess, connect to the root `QQuickWindow.frameSwapped`, and quit after
the first frame. Assert ordered offsets:

```text
process_started <= qml_loaded <= window_exposed <= first_frame_swapped
```

The test uses `QT_QPA_PLATFORM=offscreen` and accepts exposure/frame absence
as an explicit unsupported result rather than inventing a duration.

Add a fake-engine `run_ui` subprocess case. It must load the real QML shell,
start a text-tab stream through the real controller and worker, and produce
either positive frame samples or an explicit `frame_swaps_supported=false`.
The event-loop probe remains mandatory even when frame swaps are unsupported.

- [ ] **Step 3: Run focused smoke tests and verify failure**

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -n 0 \
  tests/smoke/test_performance_harness.py \
  tests/unit/test_engine.py \
  tests/unit/test_app_entry.py -q
```

Expected: missing matrix, summary, startup modules, and startup callback.

- [ ] **Step 4: Implement real engine mode**

First add and test an explicit idempotent initialization seam:

```python
def initialize(self) -> None:
    """Load the configured VieNeu engine without running synthesis."""
    self._ensure()
```

The new test calls `initialize()` twice against a counting factory and asserts
one factory call. Existing lazy construction remains unchanged because
application code does not call this method.

Add optional constructor parameters:

```python
threads: int | None = None
max_batch_size: int | None = None
```

Reject `threads < 0` and `max_batch_size < 1`. Include each keyword in
`_init_kwargs` only when it is not `None`, so existing application factories
receive exactly the current arguments. Add a counting-factory test asserting
explicit values reach VieNeu unchanged.

For `--engine real`, construct the normal `TTSEngine` with explicit
`--backend` and `--precision`. Reject `backend=auto` in a benchmark so the
record always identifies the engine being measured.

Require:

```text
--backend onnx|torch
--precision int8|fp32
--threads nonnegative-integer
--max-batch-size positive-integer
```

Record requested settings immediately and resolved `engine.backend` only
after the measured job completes. Reading `TTSEngine.backend` calls `_ensure`
and would otherwise preload the model, invalidating the cold-path result. Do
not catch model or CUDA errors as successful runs. Emit a failed record and
nonzero exit code after clean worker shutdown.

For torch runs, enable CUDA resource sampling. Wall-clock submission and
first-chunk metrics remain the user-facing measures. Do not add kernel-only
timing to the threaded production path in Stage 1 because PyTorch default
streams are thread-local.

Add `--sink fake|real|null`:

- `fake` uses `RateLimitedSink` for reproducible transport timing;
- `real` uses the default `QAudioSink` and requires a live supported audio
  environment;
- `null` skips playback and measures export-only/controller overhead.

Record `sink_kind` on every trace. A real sink failure is a failed or
unsupported run, not a successful zero-latency observation.

- [ ] **Step 5: Implement the direct-engine runner**

`run_engine.py` uses the same corpus, resource sampler, schema, and explicit
backend/precision. It constructs `TTSEngine`, times `initialize()`, then calls
`infer` or `infer_stream` directly, consuming stream chunks without the
worker, controller, or sink. An unrecorded warmup group calls `initialize()`
once and synthesizes its warmup corpus before measured jobs.

It records:

```text
run_kind=direct_engine
engine_constructed
engine_initialize_started
engine_initialize_completed
engine_call_started
engine_first_chunk
engine_completed
audio_samples
elapsed_ns
RTF
resources
```

It never presents `engine_first_chunk` as sink or audible TTFA. Add a fake
engine test that proves no controller/audio events appear in this record.
For direct torch runs only, use `torch.cuda.Event` and one explicit
`torch.cuda.synchronize()` after the end event for optional kernel timing;
never time asynchronous CUDA work with unsynchronized `perf_counter` alone.

- [ ] **Step 6: Implement fresh-process matrix orchestration**

`run_matrix` invokes `run_once` as a child process for each cold iteration.
Warm iterations execute multiple jobs in one child only when
`--warm-iterations` is greater than zero.

Required flags:

```text
--engine fake|real
--scenario one-or-more-corpus-ids
--mode stream|infer
--backend onnx|torch
--precision int8|fp32
--threads nonnegative-integer
--max-batch-size positive-integer
--path direct|pipeline
--sink fake|real|null
--hardware-class non-identifying-label
--cold-iterations integer
--warm-iterations integer
--output path.jsonl
```

Default to five cold and twenty warm iterations for real runs, but tests pass
explicit small values. Each cold iteration launches a fresh child and runs
one measured job. Each warm group launches a fresh child, performs one
unrecorded warmup, then records the requested number of jobs against the same
engine. `path=direct` launches `run_engine`; `path=pipeline` launches
`run_once`. Append records atomically after each child finishes so an
interrupted matrix preserves completed samples.

- [ ] **Step 7: Implement summary generation**

Accept one or more JSONL paths, group records by path, scenario, backend,
precision, sink kind, and cold/warm run kind, then derive distributions for:

- process elapsed ms;
- model initialization ms when present;
- TTFC ms;
- controller first-chunk ms;
- first buffer append ms;
- first sink pull ms;
- worker completion ms;
- RTF;
- current RSS delta;
- max current RSS;
- peak RSS;
- retained chunk bytes;
- concatenated audio bytes;
- audio-buffer high-water;
- restart count.
- process CPU seconds and normalized/total CPU utilization;
- event-loop median, p95, and maximum delay;

Missing metrics remain absent with a `missing_count`; they are never replaced
with zero.

- [ ] **Step 8: Add startup timing seam**

Add an optional non-QML `startup_observer: Callable[[str], None] | None` to
`create_app`. Call it after `engine.load` succeeds:

```python
if startup_observer is not None:
    startup_observer("qml_loaded")
```

`run_startup` records process start before importing `vienetts_app.app`,
passes the observer, connects `visibleChanged`/`frameSwapped` on the root
window, and exits after first frame or timeout. The default app path passes
no observer and has no behavior change.

- [ ] **Step 9: Implement the QML synthesis runner**

`run_ui.py` builds the real QML shell with an injected recorder-enabled
`AppController`, uses either the deterministic or real engine, selects the
text tab, and submits a built-in corpus entry. It records:

```text
run_kind=ui_pipeline
window exposure offset
frameSwapped intervals
frames above 16.7 ms
frames above 33.3 ms
maximum frame interval
event-loop median, p95, and maximum delay
normal synthesis trace and resources
```

The runner waits for at least two idle frames before submission so first-load
work is not misclassified as synthesis. It never uses synthetic mouse input
and never serializes editor text. A timeout produces a failed record and
still calls `audiobook.shutdown()` and `controller.shutdown()`.

- [ ] **Step 10: Run smoke and app-entry tests**

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -n 0 \
  tests/smoke/test_performance_harness.py \
  tests/unit/test_engine.py \
  tests/unit/test_app_entry.py -q
```

Expected: all tests pass.

- [ ] **Step 11: Run fake matrix and summary manually**

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python \
  -m scripts.benchmarks.run_matrix \
  --engine fake \
  --scenario vi_50 vi_512 \
  --mode stream \
  --path pipeline \
  --sink fake \
  --hardware-class fake-ci \
  --cold-iterations 2 \
  --warm-iterations 3 \
  --output /tmp/vienetts-fake-matrix.jsonl

.venv/bin/python -m scripts.benchmarks.summarize \
  /tmp/vienetts-fake-matrix.jsonl \
  --output /tmp/vienetts-fake-summary.json
```

Expected: ten raw records and a valid summary for two scenarios.

- [ ] **Step 12: Run a fake QML synthesis benchmark**

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python \
  -m scripts.benchmarks.run_ui \
  --engine fake \
  --scenario vi_512 \
  --hardware-class fake-ci \
  --output /tmp/vienetts-fake-ui.jsonl
```

Expected: one valid record with event-loop samples and either frame interval
samples or the explicit unsupported flag.

- [ ] **Step 13: Run focused lint**

```bash
.venv/bin/ruff check \
  src/vienetts_app/core/engine.py \
  src/vienetts_app/app.py \
  scripts/benchmarks \
  tests/unit/test_app_entry.py \
  tests/smoke/test_performance_harness.py
.venv/bin/ruff format --check \
  src/vienetts_app/core/engine.py \
  src/vienetts_app/app.py \
  scripts/benchmarks \
  tests/unit/test_app_entry.py \
  tests/smoke/test_performance_harness.py
```

Expected: both commands exit 0.

- [ ] **Step 14: Commit real and startup harnesses**

```bash
git add \
  src/vienetts_app/core/engine.py \
  src/vienetts_app/app.py \
  scripts/benchmarks/run_engine.py \
  scripts/benchmarks/run_once.py \
  scripts/benchmarks/run_matrix.py \
  scripts/benchmarks/summarize.py \
  scripts/benchmarks/run_startup.py \
  scripts/benchmarks/run_ui.py \
  tests/unit/test_engine.py \
  tests/unit/test_app_entry.py \
  tests/smoke/test_performance_harness.py
git commit -m "test(perf): add real model and startup matrices"
```

---

### Task 8: Baseline evidence and documentation correction

**Files:**

- Create: `docs/performance/README.md`
- Create after running: `docs/performance/baselines/2026-08-28-apple-m4-macos-direct.jsonl`
- Create after running: `docs/performance/baselines/2026-08-28-apple-m4-macos-pipeline.jsonl`
- Create after running: `docs/performance/baselines/2026-08-28-apple-m4-macos-real-sink.jsonl`
- Create after running: `docs/performance/baselines/2026-08-28-apple-m4-macos-startup.jsonl`
- Create after running: `docs/performance/baselines/2026-08-28-apple-m4-macos-ui.jsonl`
- Create after running: `docs/performance/baselines/2026-08-28-apple-m4-macos-summary.json`
- Modify: `README.md`
- Modify: `PROJECT_PLAN.md`
- Modify: `conductor/patterns.md`

**Interfaces:**

- Consumes: versioned benchmark CLIs from Tasks 6 and 7.
- Produces: reproducible baseline evidence and exact metric terminology.

- [ ] **Step 1: Write the benchmark guide**

Document:

- metric definitions and event pairs;
- cold versus warm process/model/cache meanings;
- direct engine versus production pipeline;
- fake, nightly, weekly, and release-lab commands;
- JSONL schema and safe-data policy;
- supported and unsupported metrics;
- macOS/Linux/Windows RSS unit handling;
- how to compare baselines without using the best-of-N result;
- the requirement to use medians, p90/p95, MAD, and raw samples.

Include these exact distinctions:

```text
TTFC = submitted to worker_first_chunk
controller first chunk = submitted to controller_first_chunk
first transport append = submitted to audio_first_buffer_append
first sink pull = submitted to audio_first_sink_pull
audible TTFA = external loopback only, not inferred from sink pull
```

- [ ] **Step 2: Run the Apple M4 real baseline**

Run from a clean worktree with no unrelated CPU-heavy process:

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/python \
  -m scripts.benchmarks.run_matrix \
  --engine real \
  --scenario vi_50 en_short code_switch vi_512 vi_5000 \
  --mode stream \
  --path direct \
  --sink null \
  --backend onnx \
  --precision int8 \
  --threads 0 \
  --max-batch-size 1 \
  --hardware-class apple-m4-10c-16gb \
  --cold-iterations 5 \
  --warm-iterations 20 \
  --output docs/performance/baselines/2026-08-28-apple-m4-macos-direct.jsonl

QT_QPA_PLATFORM=offscreen .venv/bin/python \
  -m scripts.benchmarks.run_matrix \
  --engine real \
  --scenario vi_50 en_short code_switch vi_512 vi_5000 \
  --mode stream \
  --path pipeline \
  --sink fake \
  --backend onnx \
  --precision int8 \
  --threads 0 \
  --max-batch-size 1 \
  --hardware-class apple-m4-10c-16gb \
  --cold-iterations 5 \
  --warm-iterations 20 \
  --output docs/performance/baselines/2026-08-28-apple-m4-macos-pipeline.jsonl

env -u QT_QPA_PLATFORM .venv/bin/python \
  -m scripts.benchmarks.run_matrix \
  --engine real \
  --scenario vi_50 code_switch \
  --mode stream \
  --path pipeline \
  --sink real \
  --backend onnx \
  --precision int8 \
  --threads 0 \
  --max-batch-size 1 \
  --hardware-class apple-m4-10c-16gb \
  --cold-iterations 3 \
  --warm-iterations 10 \
  --output docs/performance/baselines/2026-08-28-apple-m4-macos-real-sink.jsonl

env -u QT_QPA_PLATFORM .venv/bin/python \
  -m scripts.benchmarks.run_startup \
  --iterations 10 \
  --hardware-class apple-m4-10c-16gb \
  --output docs/performance/baselines/2026-08-28-apple-m4-macos-startup.jsonl

env -u QT_QPA_PLATFORM .venv/bin/python \
  -m scripts.benchmarks.run_ui \
  --engine real \
  --scenario vi_50 vi_5000 \
  --backend onnx \
  --precision int8 \
  --threads 0 \
  --max-batch-size 1 \
  --iterations 5 \
  --hardware-class apple-m4-10c-16gb \
  --output docs/performance/baselines/2026-08-28-apple-m4-macos-ui.jsonl

.venv/bin/python -m scripts.benchmarks.summarize \
  docs/performance/baselines/2026-08-28-apple-m4-macos-direct.jsonl \
  docs/performance/baselines/2026-08-28-apple-m4-macos-pipeline.jsonl \
  docs/performance/baselines/2026-08-28-apple-m4-macos-real-sink.jsonl \
  docs/performance/baselines/2026-08-28-apple-m4-macos-startup.jsonl \
  docs/performance/baselines/2026-08-28-apple-m4-macos-ui.jsonl \
  --output docs/performance/baselines/2026-08-28-apple-m4-macos-summary.json
```

This baseline is descriptive, not a universal gate. If the model or cache is
missing, do not create synthetic evidence. Record the blocking command and
leave model-dependent files absent. If no usable real audio sink exists,
record that run as unsupported and keep the direct, fake-sink pipeline, and
startup baselines.

- [ ] **Step 3: Validate baseline safety**

Run:

```bash
rg -n \
  "$USER|$HOME|Hardware UUID|Serial Number|private words|Minh Đức" \
  docs/performance/baselines || true
```

Expected: no output. Then inspect scenario IDs, hashes, package versions,
machine class, and metrics manually.

- [ ] **Step 4: Correct README performance wording**

Replace any statement that describes the historical 99 to 102 ms direct SDK
yield as audible playback. Use:

```markdown
- Streaming synthesis with a historical direct-engine first-chunk observation
  of about 100 ms on one Apple M4. End-to-end controller, audio-device, and
  cross-platform results are tracked separately in
  [docs/performance](docs/performance/README.md).
```

If the new baseline exists, add its measured production-path median and p95
only by linking to the committed summary. Do not copy a best-of-N result into
the README.

- [ ] **Step 5: Correct project-plan evidence labels**

Keep historical values, but label:

- 99 to 102 ms as preloaded direct SDK TTFC;
- 1.12 GB as direct segmented engine RSS with chunks discarded;
- 2.5 GB as a one-host direct non-stream observation;
- audible TTFA, cross-platform utilization, and production transport RSS as
  unverified until their named suites run.

Link the approved design, Stage 1 plan, benchmark guide, and baseline summary.

- [ ] **Step 6: Capture reusable benchmark patterns**

Append a concise `conductor/patterns.md` entry covering:

- timing events use monotonic offsets;
- current and peak RSS are both required;
- benchmark records never contain user content;
- timing suites run serially;
- direct engine and production path claims must not be conflated;
- raw samples and environment manifests are part of the evidence.

- [ ] **Step 7: Run documentation and evidence checks**

```bash
rg -n "first audio|first-audio|99.102|1.12 GB|2.5 GB" \
  README.md PROJECT_PLAN.md docs/performance \
  docs/superpowers/specs/2026-08-28-adaptive-performance-resource-optimization-design.md

git diff --check
```

Expected: every claim is qualified by path and measurement definition;
`git diff --check` exits 0.

- [ ] **Step 8: Run focused benchmark tests**

```bash
QT_QPA_PLATFORM=offscreen .venv/bin/pytest -n 0 \
  tests/unit/test_performance.py \
  tests/unit/test_benchmark_schema.py \
  tests/unit/test_benchmark_resources.py \
  tests/unit/test_benchmark_statistics.py \
  tests/unit/test_benchmark_corpus.py \
  tests/smoke/test_performance_harness.py -q
```

Expected: all tests pass.

- [ ] **Step 9: Run complete quality gates**

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
QT_QPA_PLATFORM=offscreen .venv/bin/pytest
```

Expected: all commands exit 0. Report exact test count and elapsed time.

- [ ] **Step 10: Review the intended Stage 1 diff**

```bash
git status --short
git diff --stat
git diff -- \
  src/vienetts_app/core/performance.py \
  src/vienetts_app/core/models.py \
  src/vienetts_app/workers/inference_worker.py \
  src/vienetts_app/ui/controller.py \
  src/vienetts_app/ui/stream_playback.py \
  src/vienetts_app/app.py \
  scripts/benchmarks \
  tests \
  docs/performance \
  README.md \
  PROJECT_PLAN.md \
  conductor/patterns.md
```

Expected: no model files, generated WAVs, user content, secrets, unrelated
changes, or unplanned dependencies.

- [ ] **Step 11: Commit evidence and documentation**

```bash
git add \
  docs/performance \
  README.md \
  PROJECT_PLAN.md \
  conductor/patterns.md
git commit -m "docs(perf): record production baseline methodology"
```

## Stage 1 completion gate

Do not plan fixed production tuning values until all statements below are
true:

1. Fake full-pipeline records include worker, controller, buffer append, and
   sink pull events.
2. Raw records cannot serialize corpus text, voice names, paths, hostnames,
   usernames, serials, or UUIDs.
3. Current and peak RSS units are tested on each supported OS in CI or the
   release lab.
4. Real ONNX-int8 runs produce raw samples plus median, p90, p95, and MAD.
5. Direct engine and full-pipeline numbers are reported separately.
6. The recorder is disabled by default and has no observable synthesis
   behavior change.
7. Full repository quality gates pass.

## Follow-up plan boundaries

After Stage 1 evidence is reviewed, write and approve these separate plans in
order:

1. `adaptive-performance-stage2-job-correctness`
   - Job IDs on every signal, immutable ownership, targeted cancellation,
     terminal-event guarantees, backend-resolution corrections, async
     startup probes.
2. `adaptive-performance-stage3-bounded-streaming`
   - Incremental artifact writer, cross-platform growing-file contract,
     fixed transport high-water, QAudioSink prebuffer/state/drain behavior,
     export-only flow.
3. `adaptive-performance-stage4-priority-and-io`
   - Priority scheduler, segment-boundary yielding, foreground/background
     QoS, async EPUB/import/export/persistence.
4. `adaptive-performance-stage5-adaptive-presets-and-cache`
   - Auto/Performance/Efficiency, ONNX profile selection, intent warmup,
     cache fingerprints, validation, free-space reserve, and LRU.
5. `adaptive-performance-stage6-cuda-bulk`
   - True multi-text requests, batch 1/2/4/8/16 evidence, VRAM headroom,
     fallback, backend-switch break-even, and release hardware matrix.

Each follow-up plan must cite the exact Stage 1 raw records and summaries that
justify its numeric defaults.
