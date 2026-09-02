# Job Contract and Terminal Events Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make every admitted inference operation identifiable, targetably
cancellable, and exactly-once terminal, so controllers can safely reject stale
worker delivery.

**Architecture:** Add immutable job/event value objects and a lock-protected
FIFO admission queue to the existing one-worker architecture. The worker emits
only tagged `JobProgress`, `JobChunk`, and `JobTerminal` values; a
terminal-deduplication gate owns the exactly-once invariant. `AppController`
routes events by job ID instead of a mutable global listener, and the
audiobook controller registers only its own returned job ID.

**Tech Stack:** Python dataclasses, `threading.Condition`, PySide6 signals,
NumPy, pytest with QCoreApplication event pumping, ruff.

**Spec:** `docs/superpowers/specs/2026-09-03-performance-optimization-implementation-design.md`

**Dependencies:** Implement after
`2026-09-03-model-onboarding-and-benchmark-foundation.md`. Phase 3 replaces
the temporary `JobTerminal.value` audio payload with a
`SynthesisArtifact`; no controller or QML code may depend on a raw untagged
worker signal after this phase.

## Global Constraints

- Preserve exactly one `TTSEngine` and one `InferenceWorker`.
- Never create an engine or initialize a model while constructing either
  controller.
- A `SynthesisJob` is immutable and has a globally unique nonblank ID.
- Every successfully admitted `SynthesisJob` emits precisely one
  `JobTerminal` with state `completed`, `cancelled`, `failed`, or
  `superseded`, including a job cancelled before it reaches the engine.
- Progress, chunks, and terminals carry a job ID. Receivers drop an event
  whose ID is not currently owned by that receiver.
- `cancel_job(job_id)` affects only that job. `cancel_owner(owner)` affects
  only jobs belonging to that owner. Only `stop()` may cancel the full queue.
- An in-flight cancellation remains cooperative at already-safe segment/chunk
  boundaries. It must not clear the cancel state of another job.
- Preserve current FIFO selection in this phase. Phase 4 changes only the
  queue selection policy to stable priority order.
- Preserve existing `TTSRequest.job_id` data during migration for external
  callers, but make `SynthesisJob.id` authoritative and require both IDs to
  match when the nested request supplies one.
- Maintain a single engine thread owner. Queue data structures and
  cancellation bookkeeping are thread-safe; controllers never touch engine
  state from a Qt callback.
- Use fake engines, fake workers, and fake listeners in deterministic tests.
  Tests do not load an SDK model or sleep for timing assertions.
- `WarmupOp` remains a non-job best-effort internal command. It does not
  participate in the user-visible terminal event contract.
- Run timing-sensitive worker tests with `-n 0`.
- Before each commit, run the task’s focused tests. Before Phase completion,
  run:
  `./.venv/bin/ruff check .`,
  `./.venv/bin/ruff format --check .`, and
  `QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest`.

## File Map

### New production files

- `src/vienetts_app/core/jobs.py`
  - Immutable job, tagged event, terminal state, and job-construction
    validation. Contains no Qt or worker threading code.
- `src/vienetts_app/workers/job_queue.py`
  - Thread-safe FIFO pending-job queue, targeted pending cancellation, and
    owner-filtered cancellation. Its API intentionally leaves priority
    selection as a replaceable implementation detail for Phase 4.

### Modified production files

- `src/vienetts_app/core/models.py`
  - Makes nested operation payloads compatible with `SynthesisJob`.
- `src/vienetts_app/core/performance.py`
  - Associates performance events with the authoritative job ID and makes
    duplicate finish calls idempotent.
- `src/vienetts_app/workers/inference_worker.py`
  - Admits `SynthesisJob`, emits tagged events, supports job/owner
    cancellation, and has a single terminalization gate.
- `src/vienetts_app/ui/controller.py`
  - Replaces global `busy`/attachment-based routing with foreground-owned job
    tracking and a job-ID listener registry.
- `src/vienetts_app/ui/audiobook_controller.py`
  - Records the returned audiobook render job ID and ignores unrelated/stale
    terminal notifications.
- `src/vienetts_app/ui/qml/Main.qml`
  - Binds current action availability to the controller’s foreground job
    state, not the worker’s global queue state.

### New tests

- `tests/unit/test_jobs.py`
- `tests/unit/test_job_queue.py`

### Modified tests

- `tests/unit/test_inference_worker.py`
- `tests/unit/test_controller.py`
- `tests/unit/test_audiobook_controller.py`
- `tests/unit/test_performance.py`
- `tests/smoke/test_ui_shell.py`

---

### Task 1: Define immutable jobs and tagged worker events

**Files:**

- Create: `src/vienetts_app/core/jobs.py`
- Create: `tests/unit/test_jobs.py`
- Modify: `src/vienetts_app/core/models.py`
- Modify: `tests/unit/test_models.py`

**Interfaces:**

- Consumes:
  - `TTSRequest`, `VoiceOp`, and `WarmupOp` from
    `vienetts_app.core.models`.
- Produces:

```python
JobOwner = Literal["text", "paragraph", "audiobook", "cloning"]
JobKind = Literal[
    "interactive", "requested_chapter", "prefetch", "bulk", "voice_op"
]
JobTerminalState = Literal["completed", "cancelled", "failed", "superseded"]
JobRequest = TTSRequest | VoiceOp

@dataclass(frozen=True)
class SynthesisJob:
    id: str
    owner: JobOwner
    kind: JobKind
    priority: int
    request: JobRequest
    artifact_path: Path | None = None
    cache_fingerprint: str | None = None

@dataclass(frozen=True)
class JobProgress:
    job_id: str
    done: int
    total: int
    stage: str

@dataclass(frozen=True)
class JobChunk:
    job_id: str
    samples: np.ndarray

@dataclass(frozen=True)
class JobTerminal:
    job_id: str
    owner: JobOwner
    state: JobTerminalState
    value: object | None = None
    error: str = ""
```

- `new_synthesis_job(owner, kind, request, *, priority=0,
  artifact_path=None, cache_fingerprint=None) -> SynthesisJob`
- `SynthesisJob` validates a UUID-hex-shaped nonblank ID, a valid owner/kind,
  nonnegative integer priority, a supported nested operation, and matching
  nested `TTSRequest.job_id` when present.
- `JobTerminal` enforces:
  - completed has no `error`;
  - cancelled and superseded have no `error`;
  - failed has a nonblank `error`;
  - a terminal ID is nonblank.

- [ ] **Step 1: Write failing data-contract tests**

```python
import pytest

from vienetts_app.core.jobs import (
    JobTerminal,
    SynthesisJob,
    new_synthesis_job,
)
from vienetts_app.core.models import TTSRequest


def test_factory_copies_its_id_into_a_tts_request() -> None:
    job = new_synthesis_job(
        "text",
        "interactive",
        TTSRequest(text="Xin chào", mode="stream"),
    )

    assert job.id
    assert isinstance(job.request, TTSRequest)
    assert job.request.job_id == job.id


def test_job_rejects_mismatched_nested_request_id() -> None:
    with pytest.raises(ValueError, match="must match"):
        SynthesisJob(
            id="a" * 32,
            owner="text",
            kind="interactive",
            priority=0,
            request=TTSRequest(text="Xin chào", job_id="b" * 32),
        )


def test_terminal_rejects_a_failed_result_without_error() -> None:
    with pytest.raises(ValueError, match="failed"):
        JobTerminal(job_id="a" * 32, owner="text", state="failed")
```

Complete the data-contract matrix with these assertions:

- assigning `job.priority = 1` raises `FrozenInstanceError`;
- owner `"unknown"`, kind `"unknown"`, and priority `-1` each raise
  `ValueError`;
- a `VoiceOp` produces a valid immutable job without a nested `job_id`;
- `completed`, `cancelled`, and `superseded` terminals reject a nonempty
  `error`, while a failed terminal accepts a nonempty error;
- `SynthesisJob(..., artifact_path="out.wav")` exposes
  `Path("out.wav")`, not a raw string.

- [ ] **Step 2: Run focused tests to verify they fail**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_jobs.py tests/unit/test_models.py -v`

Expected: FAIL because the jobs module and factory do not exist.

- [ ] **Step 3: Implement the value-object contract**

Use `uuid.uuid4().hex` only in `new_synthesis_job`; the dataclass itself
never manufactures IDs so tests and scheduler callers may provide a known
value. Copy a nested request safely with `dataclasses.replace`:

```python
def new_synthesis_job(
    owner: JobOwner,
    kind: JobKind,
    request: JobRequest,
    *,
    priority: int = 0,
    artifact_path: Path | None = None,
    cache_fingerprint: str | None = None,
) -> SynthesisJob:
    job_id = uuid.uuid4().hex
    if isinstance(request, TTSRequest):
        if request.job_id not in (None, job_id):
            raise ValueError("TTSRequest.job_id must match the enclosing SynthesisJob id")
        request = replace(request, job_id=job_id)
    return SynthesisJob(
        id=job_id,
        owner=owner,
        kind=kind,
        priority=priority,
        request=request,
        artifact_path=artifact_path,
        cache_fingerprint=cache_fingerprint,
    )
```

Keep `TTSRequest.job_id` as an optional compatibility field in this phase.
Do not add a `job_id` to `VoiceOp`; the enclosing job identifies it.

- [ ] **Step 4: Run focused data-contract tests**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_jobs.py tests/unit/test_models.py -v`

Expected: PASS.

- [ ] **Step 5: Commit the event contract**

```bash
git add src/vienetts_app/core/jobs.py src/vienetts_app/core/models.py \
  tests/unit/test_jobs.py tests/unit/test_models.py
git commit -m "feat(jobs): define tagged inference job contract"
git notes add -m "Phase 2 Task 1: added immutable jobs and terminal event invariants."
```

### Task 2: Add a cancellable FIFO admission queue and terminal gate

**Files:**

- Create: `src/vienetts_app/workers/job_queue.py`
- Create: `tests/unit/test_job_queue.py`
- Modify: `src/vienetts_app/workers/inference_worker.py`
- Modify: `tests/unit/test_inference_worker.py`

**Interfaces:**

- Consumes: `SynthesisJob` from `core.jobs`.
- Produces:

```python
class FifoJobQueue:
    def put(self, job: SynthesisJob) -> None: ...
    def take(self, timeout_seconds: float) -> SynthesisJob | None: ...
    def cancel(self, job_id: str) -> SynthesisJob | None: ...
    def cancel_owner(self, owner: JobOwner) -> tuple[SynthesisJob, ...]: ...
    def cancel_all(self) -> tuple[SynthesisJob, ...]: ...
    def wake(self) -> None: ...

class InferenceWorker(QThread):
    progress = Signal(object)  # JobProgress
    chunk_ready = Signal(object)  # JobChunk
    terminal = Signal(object)  # JobTerminal
    voice_op_done = Signal(object)  # retained temporary compatibility signal

    def submit(self, job: SynthesisJob | WarmupOp) -> bool: ...
    def cancel_job(self, job_id: str) -> bool: ...
    def cancel_owner(self, owner: JobOwner) -> int: ...
    def stop(self) -> bool: ...
```

- Worker terminal behavior:
  - `submit(SynthesisJob)` returns `False` only after `stop()` begins; a
    returned `True` job gets one terminal event.
  - `cancel_job` returns `True` for a queued or active known job, `False`
    otherwise.
  - queued cancellation emits `JobTerminal(state="cancelled")` immediately
    after the queue removes that job.
  - an active cancellation sets only that active job’s cancel event; it emits
    its terminal at the next safe boundary.
  - exception, cancellation, and success all pass through
    `_terminalize(job, state, value=None, error="")`.
  - `_terminalize` is lock-protected and returns `False` if another path has
    already terminalized the job.

- [ ] **Step 1: Write failing queue and worker tests**

```python
def test_cancel_queued_job_is_immediate_and_does_not_run() -> None:
    queue = FifoJobQueue()
    first = make_job("a" * 32, text="first")
    second = make_job("b" * 32, text="second")
    queue.put(first)
    queue.put(second)

    assert queue.cancel(second.id) == second
    assert queue.take(0) == first
    assert queue.take(0) is None


def test_worker_emits_one_tagged_terminal_for_queued_cancellation(harness) -> None:
    h = harness(BlockFirstEngine())
    first = make_job("a" * 32, text="first")
    second = make_job("b" * 32, text="second")
    h.worker.submit(first)
    h.worker.submit(second)
    h.engine.wait_until_started()

    assert h.worker.cancel_job(second.id) is True

    assert h.wait_terminal(second.id)
    terminal = h.terminals_for(second.id)[0]
    assert terminal.state == "cancelled"
    assert h.engine.requests == ["first"]
```

Add worker tests proving:

1. each emitted `JobProgress` and `JobChunk` has the correct `job_id`;
2. active cancellation of job A does not cancel queued job B;
3. a success path cannot produce a subsequent failed/cancelled terminal;
4. an engine exception produces one failed terminal with its error;
5. shutdown terminalizes every still-admitted pending job once;
6. a rejected `submit` after stop emits no event because it was never
   admitted;
7. `cancel_owner("audiobook")` leaves text and cloning jobs in FIFO order.

- [ ] **Step 2: Run focused queue and worker tests to verify they fail**

Run:
`QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 tests/unit/test_job_queue.py tests/unit/test_inference_worker.py -v`

Expected: FAIL because the FIFO queue, tagged signals, and targeted
cancellation API do not exist.

- [ ] **Step 3: Implement `FifoJobQueue`**

Use an `OrderedDict[str, SynthesisJob]` and one
`threading.Condition(threading.Lock())`; that allows an O(1) exact pending-job
removal without touching `queue.Queue` private state:

```python
class FifoJobQueue:
    def __init__(self) -> None:
        self._items: OrderedDict[str, SynthesisJob] = OrderedDict()
        self._condition = threading.Condition()

    def put(self, job: SynthesisJob) -> None:
        with self._condition:
            if job.id in self._items:
                raise ValueError(f"job already queued: {job.id}")
            self._items[job.id] = job
            self._condition.notify()

    def take(self, timeout_seconds: float) -> SynthesisJob | None:
        with self._condition:
            if not self._items:
                self._condition.wait(timeout=max(0.0, timeout_seconds))
            if not self._items:
                return None
            _, job = self._items.popitem(last=False)
            return job

    def cancel(self, job_id: str) -> SynthesisJob | None:
        with self._condition:
            return self._items.pop(job_id, None)
```

Implement the owner/all variants under the same condition and call
`notify_all()` after removal so `stop()` does not wait for the polling period.
Do not mutate a `SynthesisJob` after enqueue.

- [ ] **Step 4: Migrate `InferenceWorker` to canonical tagged signals**

Replace the anonymous done/error paths with one event stream:

```python
def _terminalize(
    self,
    job: SynthesisJob,
    state: JobTerminalState,
    *,
    value: object | None = None,
    error: str = "",
) -> bool:
    with self._terminal_lock:
        if job.id in self._terminal_ids:
            return False
        self._terminal_ids.add(job.id)
    self._performance.finish(
        job.id,
        "completed" if state == "completed" else state,
    )
    self.terminal.emit(
        JobTerminal(job_id=job.id, owner=job.owner, state=state, value=value, error=error)
    )
    return True
```

Use a fresh `threading.Event` stored in `_active_cancel` only while that
specific `SynthesisJob` is running. `cancel_job()` first tries
`self._jobs.cancel(job_id)`: when it returns a job, terminalize that removed
job synchronously. Otherwise, under `_active_lock`, set the event only if
`_active_job.id == job_id`. Never clear this event in `submit()`.

Convert the worker’s synthesis paths:

```python
self.progress.emit(JobProgress(job.id, done=0, total=total, stage="synthesizing"))
self.chunk_ready.emit(JobChunk(job.id, array.copy()))
...
self._terminalize(job, "completed", value=audio)
```

Use a bounded safe copy for the transitional chunk event. Phase 3 removes
cross-thread raw PCM transport, so do not optimize this temporary copy at the
expense of a producer-buffer lifetime race.

For `VoiceOp`, emit one `JobTerminal(completed, value={"op": ...})` rather
than `voice_op_done`; retain `voice_op_done` only as an adapter for a
one-release migration and ensure it does not drive controllers. For
`WarmupOp`, preserve silent behavior and do not admit a `SynthesisJob`.

On worker `stop()`, set the stopping flag, terminalize `cancel_all()` results,
set the active job’s event, wake the queue, and then wait. The active method
calls `_terminalize(..., "cancelled")` after its current safe boundary.

- [ ] **Step 5: Run focused queue and worker tests**

Run:
`QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 tests/unit/test_job_queue.py tests/unit/test_inference_worker.py -v`

Expected: PASS. The terminal collection must contain exactly one entry per
admitted test job.

- [ ] **Step 6: Commit cancellation and terminalization**

```bash
git add src/vienetts_app/workers/job_queue.py \
  src/vienetts_app/workers/inference_worker.py \
  tests/unit/test_job_queue.py tests/unit/test_inference_worker.py
git commit -m "feat(worker): tag jobs and support targeted cancellation"
git notes add -m "Phase 2 Task 2: migrated worker delivery to tagged exactly-once terminals."
```

### Task 3: Route controller and audiobook callbacks by job ID

**Files:**

- Modify: `src/vienetts_app/ui/controller.py`
- Modify: `src/vienetts_app/ui/audiobook_controller.py`
- Modify: `tests/unit/test_controller.py`
- Modify: `tests/unit/test_audiobook_controller.py`

**Interfaces:**

- Consumes:
  - `JobChunk`, `JobProgress`, `JobTerminal`, `SynthesisJob`,
    `new_synthesis_job`.
  - Worker `progress`, `chunk_ready`, and `terminal` signals.
- Produces on `AppController`:

```python
@Property(str, notify=foregroundJobIdChanged)
def foregroundJobId(self) -> str: ...

@Property(str, notify=foregroundJobStateChanged)
def foregroundJobState(self) -> str: ...

@Slot()
def cancel(self) -> None: ...

def submit_stream_for_listener(
    self, text: str, voice: str | None, listener: Any, *,
    kind: Literal["requested_chapter", "prefetch", "bulk"] = "requested_chapter",
) -> str | None: ...
```

- `foregroundJobState` is `idle`, `queued`, `generating`, `cancel_requested`,
  `completed`, `cancelled`, or `failed`. It is scoped to the interactive
  text/paragraph/cloning owner, not the worker’s entire queue.
- `submit_stream_for_listener` returns a nonblank job ID only after the
  listener is registered and the worker admits the job; it returns `None`
  without registering a listener on validation/admission failure.
- Each listener callback takes an event object:

```python
listener.on_synthesis_progress(event: JobProgress)
listener.on_synthesis_chunk(event: JobChunk)
listener.on_synthesis_terminal(event: JobTerminal)
```

- `AudiobookController._render_job_id: str | None` identifies its one active
  render. It commits a result only when `event.job_id == _render_job_id` and
  its immutable saved book/chapter snapshot still matches.

- [ ] **Step 1: Write failing controller stale-routing tests**

```python
def test_controller_discards_stale_terminal_for_a_previous_job(harness) -> None:
    harness.controller.generateStream("first", "")
    first = harness.worker.submitted[-1]
    harness.worker.terminal.emit(completed(first.id, samples(4)))

    harness.controller.generateStream("second", "")
    second = harness.worker.submitted[-1]
    harness.worker.terminal.emit(completed(first.id, samples(8)))

    assert harness.controller.foregroundJobId == second.id
    assert harness.controller.busy is True
    assert harness.controller.hasAudio is False


def test_cancel_targets_only_the_foreground_job(harness) -> None:
    harness.controller.generate("one", "")
    first = harness.worker.submitted[-1]
    harness.controller.cancel()

    assert harness.worker.cancelled_job_ids == [first.id]


def test_audiobook_ignores_foreign_terminal_after_book_switch(book_harness) -> None:
    book_harness.open_two_books_and_begin_first_render()
    first_job_id = book_harness.app.submitted_listener_job_ids[-1]
    book_harness.audiobook.selectBook(book_harness.other_book_id)

    book_harness.app.emit_terminal(completed(first_job_id, samples(48_000)))

    assert not book_harness.library.has_chapter_audio(book_harness.other_book_id, 0)
```

Complete the routing matrix with these assertions:

- emitting a text job event never invokes an audiobook listener;
- terminalizing one audiobook job removes only that job’s listener mapping;
- queued audiobook work leaves the text action’s `busy` state false;
- `cancelRender()` calls `cancel_job` with its current `_render_job_id`;
- a failed terminal updates an error only when its ID matches the selected
  chapter’s immutable render snapshot.

- [ ] **Step 2: Run focused controller tests to verify they fail**

Run:

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 \
  tests/unit/test_controller.py \
  tests/unit/test_audiobook_controller.py -v
```

Expected: FAIL because fake worker events are untagged and listener attachment
is global.

- [ ] **Step 3: Implement `AppController` job ownership**

Replace `_active_job_id`, `_synthesis_listener`, and
`_controller_saw_chunk` with:

```python
self._foreground_job_id: str | None = None
self._foreground_job_state = "idle"
self._listener_by_job_id: dict[str, Any] = {}
self._chunk_seen_by_job_id: set[str] = set()
```

Build a text job through one helper:

```python
def _submit_text_job(self, request: TTSRequest, *, owner: JobOwner = "text") -> str | None:
    job = new_synthesis_job(owner, "interactive", request)
    worker = self._ensure_worker()
    if not worker.submit(job):
        self._set_error(self.tr("Không thể thêm tác vụ vì ứng dụng đang đóng."))
        return None
    self._foreground_job_id = job.id
    self._set_foreground_job_state("queued")
    self._set_busy(True)
    self._performance.begin(job.id, {"char_count": len(request.text), "mode": request.mode})
    self._performance.mark(job.id, "submitted")
    return job.id
```

When the fake/real worker’s tagged `JobProgress` arrives, only change
interactive UI progress if `event.job_id == _foreground_job_id`; otherwise
delegate only when `event.job_id` exists in `_listener_by_job_id`. The same
rule applies to `JobChunk` and `JobTerminal`.

On a foreground terminal:

1. first confirm the ID equals `_foreground_job_id`;
2. set `_foreground_job_id` to `None` before calling Qt-visible completion
   behavior;
3. update only the matching state/audio/error;
4. clear busy for that foreground owner;
5. retain the terminal state long enough for the QML completion/error
   notification, then reset it on the next accepted foreground job.

`cancel()` reads `_foreground_job_id` once, changes only its state to
`cancel_requested`, calls `worker.cancel_job(job_id)`, and stops only that
job’s stream sink. It never invokes `worker.cancel()` or erases the whole
queue.

Implement `submit_stream_for_listener` as a one-step registration/admission
sequence:

```python
def submit_stream_for_listener(self, text, voice, listener, *, kind="requested_chapter"):
    request = TTSRequest(text=text, voice=voice or None, mode="stream",
                         temperature=self._settings.temperature)
    job = new_synthesis_job("audiobook", kind, request)
    worker = self._ensure_worker()
    self._listener_by_job_id[job.id] = listener
    if not worker.submit(job):
        self._listener_by_job_id.pop(job.id, None)
        return None
    self._performance.begin(job.id, {"char_count": len(text), "mode": "stream"})
    self._performance.mark(job.id, "submitted")
    return job.id
```

`_on_terminal` pops the listener before invoking
`listener.on_synthesis_terminal(event)` so a reentrant submit cannot receive
the completed job’s late events.

- [ ] **Step 4: Implement audiobook ID ownership**

Replace `attach_synthesis_listener`, `detach_synthesis_listener`, and
`submit_stream_for_listener(text, voice) -> bool` usage. In the audio-book
controller, retain the existing immutable render snapshot then submit:

```python
job_id = self._app_controller.submit_stream_for_listener(
    chapter.text,
    voice,
    self,
    kind="requested_chapter",
)
if job_id is None:
    self._mark_render_submission_failed(snapshot)
    return
self._render_job_id = job_id
```

Every listener handler starts with:

```python
if event.job_id != self._render_job_id:
    return
```

`cancelRender()` invokes `self._app_controller.cancel_job(self._render_job_id)`
through a new forwarding method, does not cancel text/cloning work, and leaves
the cache’s already atomically saved artifacts intact. Clear
`_render_job_id` only after the matching terminal handler completed the
snapshot validation and status transition.

- [ ] **Step 5: Run focused controller and audiobook tests**

Run:

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 \
  tests/unit/test_controller.py \
  tests/unit/test_audiobook_controller.py -v
```

Expected: PASS. Verify fake worker terminals use `JobTerminal`, rather than
calling legacy `done`/`error` signals.

- [ ] **Step 6: Commit ID-based controller routing**

```bash
git add src/vienetts_app/ui/controller.py src/vienetts_app/ui/audiobook_controller.py \
  tests/unit/test_controller.py tests/unit/test_audiobook_controller.py
git commit -m "feat(ui): route inference events by job identity"
git notes add -m "Phase 2 Task 3: replaced global listener routing with job-ID ownership."
```

### Task 4: Complete fake migration, trace invariants, and UI status handoff

**Files:**

- Modify: `src/vienetts_app/core/performance.py`
- Modify: `tests/unit/test_performance.py`
- Modify: `tests/unit/test_controller.py`
- Modify: `tests/unit/test_inference_worker.py`
- Modify: `tests/smoke/test_ui_shell.py`
- Modify: `src/vienetts_app/ui/qml/Main.qml`
- Modify: `README.md`
- Modify: `PROJECT_PLAN.md`

**Interfaces:**

- Consumes all Task 1–3 public interfaces.
- Produces:
  - performance trace completion that is idempotent per `job_id`;
  - shell object names `foregroundJobStatus` and `cancelForegroundButton`;
  - no active production controller connection to legacy untagged
    `done`/`error` worker signals.

- [ ] **Step 1: Write failing idempotent trace and shell-state tests**

```python
def test_trace_finish_is_idempotent_for_one_job() -> None:
    recorder = PerformanceRecorder(enabled=True)
    recorder.begin("a" * 32, {"mode": "stream"})

    recorder.finish("a" * 32, "cancelled")
    recorder.finish("a" * 32, "failed")

    (trace,) = recorder.snapshot("a" * 32)
    assert trace["outcome"] == "cancelled"


def test_shell_shows_foreground_job_not_global_queue_state(shell) -> None:
    shell.controller.set_foreground_state_for_test("queued")
    shell.audiobook_queue_has_background_job = True

    assert shell.find("foregroundJobStatus").property("visible") is True
    assert shell.find("cancelForegroundButton").property("enabled") is True
```

Add a source-level migration test that the canonical
`AppController._connect_worker` attaches `worker.terminal` and never connects
worker `done` or `error`.

- [ ] **Step 2: Run focused migration tests to verify they fail**

Run:

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 \
  tests/unit/test_performance.py \
  tests/unit/test_controller.py \
  tests/smoke/test_ui_shell.py -v
```

Expected: FAIL because trace finish is currently overwritable and QML has no
foreground job status component.

- [ ] **Step 3: Implement final invariants and transition UI**

In `PerformanceRecorder.finish`, return immediately if an existing trace
already has a non-`None` outcome:

```python
def finish(self, job_id: str | None, outcome: str) -> None:
    trace = self._trace_for(job_id)
    if trace is None or trace.outcome is not None:
        return
    trace.outcome = outcome
    trace.events.append({"name": "finished", "at_ns": time.perf_counter_ns()})
```

Adapt every test fake `FakeWorker` to expose:

```python
progress = Signal(object)
chunk_ready = Signal(object)
terminal = Signal(object)

def submit(self, job: SynthesisJob | WarmupOp) -> bool:
    self.submitted.append(job)
    return True

def cancel_job(self, job_id: str) -> bool:
    self.cancelled_job_ids.append(job_id)
    return True
```

Do not preserve fake `done` or `error` signals beyond tests explicitly
covering the one-release worker adapter. Eliminate any production
`worker.done.connect` or `worker.error.connect` use.

In `Main.qml`, add a compact, translated foreground status line beneath the
existing action area. Map the controller’s state to copy such as “Đang chờ
xử lý”, “Đang tạo âm thanh”, and “Đang hủy”. Its cancel button calls
`controller.cancel()` and is enabled only for `queued` or `generating`.
Background audiobook work must not make this line claim that the text
foreground action is busy.

- [ ] **Step 4: Run the Phase 2 focused suite**

Run:

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 \
  tests/unit/test_jobs.py \
  tests/unit/test_job_queue.py \
  tests/unit/test_inference_worker.py \
  tests/unit/test_controller.py \
  tests/unit/test_audiobook_controller.py \
  tests/unit/test_performance.py \
  tests/smoke/test_ui_shell.py -v
```

Expected: PASS. Inspect the terminal event collectors and verify no admitted
job appears zero or twice.

- [ ] **Step 5: Update documentation**

Document that normal actions use a foreground scoped state, background
audiobook jobs have their own owner, and cancelling a text action does not
drop queued audiobook work. Do not promise priority scheduling until Phase 4
is implemented.

- [ ] **Step 6: Run complete quality gate**

Run:

```bash
./.venv/bin/ruff check .
./.venv/bin/ruff format --check .
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest
```

Expected: all commands exit 0.

- [ ] **Step 7: Commit the Phase 2 migration**

```bash
git add src/vienetts_app/core/performance.py src/vienetts_app/ui/qml/Main.qml \
  tests/unit/test_performance.py tests/unit/test_controller.py \
  tests/unit/test_inference_worker.py tests/smoke/test_ui_shell.py \
  README.md PROJECT_PLAN.md
git commit -m "fix(jobs): enforce one terminal event per request"
git notes add -m "Phase 2 Task 4: completed fake migration, trace deduplication, and foreground job status."
```
