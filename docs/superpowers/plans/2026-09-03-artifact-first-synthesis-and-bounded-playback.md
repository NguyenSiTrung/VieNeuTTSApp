# Artifact-First Synthesis and Bounded Playback Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make normal synthesis duration-independent in memory by incrementally
writing a validated WAV artifact and sending live audio through a bounded
two-second PCM transport.

**Architecture:** The inference worker validates each stream chunk once,
writes it to `<job>.part.wav`, optionally appends it to a
`BoundedPcmTransport`, and emits only small tagged metadata. Successful
completion closes the WAV header, validates its structural metadata, then
atomically promotes it to `<job>.wav` and emits `SynthesisArtifact`. Playback
pulls the shared locked transport through a QIODevice; the producer blocks
cooperatively when it reaches the 384,000-byte cap, eliminating an unbounded
Qt signal/audio backlog.

**Tech Stack:** Python stdlib filesystem/threading primitives, NumPy,
SoundFile, PySide6 `QIODevice`/`QAudioSink`/`QMediaPlayer`, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-03-performance-optimization-implementation-design.md`

**Dependencies:** Implement after
`2026-09-03-job-contract-and-terminal-events.md`. It changes
`JobTerminal.value` for successful synthesis from a NumPy buffer to
`SynthesisArtifact`; Phase 4 consumes that artifact as the audiobook cache
input and must not reintroduce full-buffer handoff.

## Global Constraints

- Keep one `TTSEngine` and one `InferenceWorker`; do not introduce a rendering
  subprocess or a second model instance.
- Normal synthesis uses `infer_stream` segmentation and never builds a list,
  grown array, `np.concatenate`, or duration-sized controller array.
- Worker chunks must be contiguous, one-dimensional, finite `float32`, mono,
  and 48 kHz. Reject malformed chunks before writing or enqueueing playback.
- Each active job writes first to a private same-directory
  `<job_id>.part.wav`; only a closed and validated file may become
  `<job_id>.wav` through `os.replace`.
- A failed or cancelled normal synthesis removes its partial artifact. An
  existing valid artifact is never overwritten until a replacement is
  complete.
- `SynthesisArtifact` contains path, sample rate, sample count, duration,
  and optional sidecar paths, but never the PCM array itself.
- Live PCM transport capacity is exactly at most two seconds:
  `48_000 * 1 * 4 * 2 = 384_000` bytes.
- Prebuffer between 100 and 200 ms before starting a live `QAudioSink`; use
  150 ms (28,800 bytes) as the initial fixed setting.
- Producer backpressure must re-check targeted cancellation and terminal
  state while waiting. It cannot busy spin or block the GUI thread.
- No raw PCM moves in queued Qt signals after this phase. Worker-to-controller
  events carry only job ID, sample count, progress, state, artifact metadata,
  and coalesced level values.
- Live playback is optional. No device, sink creation failure, or device loss
  must leave artifact generation and export usable.
- Keep interactive artifacts outside user-selected export directories. Clean
  the prior unprotected interactive artifact only after playback releases it;
  never delete a user export.
- Read/compute a complete artifact waveform off the GUI thread with
  `compute_waveform_envelope_from_wav`; never decode the whole file in the
  controller.
- Preserve Windows-safe close-before-replace ordering. A file player must be
  stopped before deleting its temporary/current managed artifact.
- Use fake SoundFile writers, fake sink devices, and fake engines in unit
  tests. The long-artifact tests must prove bounds through counters, not
  allocate a multi-gigabyte fixture.
- Before each commit, run the task’s focused tests. Before Phase completion,
  run:
  `./.venv/bin/ruff check .`,
  `./.venv/bin/ruff format --check .`, and
  `QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest`.

## File Map

### New production files

- `src/vienetts_app/core/artifacts.py`
  - `SynthesisArtifact`, streaming WAV writer, structural validation, managed
    interactive artifact allocation, atomic promotion, and cleanup.
- `src/vienetts_app/core/pcm_transport.py`
  - Thread-safe bounded PCM byte transport with cooperative producer waiting,
    consumer reads, close/cancel wakeup, and metrics.

### Modified production files

- `src/vienetts_app/core/jobs.py`
  - Converts `JobChunk` from raw samples to small chunk metadata, and
    optionally carries a live `BoundedPcmTransport` reference only for the
    admitted job’s lifetime.
- `src/vienetts_app/workers/inference_worker.py`
  - Uses `IncrementalArtifactWriter`, transfers chunks to the bounded
    transport, emits coalesced metadata, and returns artifacts in terminals.
- `src/vienetts_app/ui/stream_playback.py`
  - Replaces main-thread unbounded bytearray ownership with a
    `BoundedPcmTransport`-backed QIODevice and prebuffer/drain state.
- `src/vienetts_app/ui/controller.py`
  - Holds an artifact reference instead of `_audio`, drives artifact playback
    and file-copy export, schedules waveform sidecar work in `bg_ops`.
- `src/vienetts_app/ui/playback.py`
  - Adds an explicit managed-artifact release callback after stop/end/error so
    Windows cleanup cannot race an open player.
- `src/vienetts_app/ui/qml/TextTab.qml`
  - Binds replay/export to artifact availability and labels state accurately.
- `src/vienetts_app/ui/qml/ParagraphTab.qml`
  - Uses the same artifact availability contract.
- `src/vienetts_app/ui/audiobook_controller.py`
  - Accepts an artifact terminal result without a full buffer and forwards it
    to the cache lifecycle introduced in Phase 4.
- `src/vienetts_app/ui/chapter_persist.py`
  - Is retired only after Phase 4 supplies a file-to-cache promotion path; do
    not delete it in this phase.

### New tests

- `tests/unit/test_artifacts.py`
- `tests/unit/test_pcm_transport.py`

### Modified tests

- `tests/unit/test_inference_worker.py`
- `tests/unit/test_stream_playback.py`
- `tests/unit/test_controller.py`
- `tests/unit/test_playback.py`
- `tests/unit/test_audio.py`
- `tests/unit/test_audiobook_controller.py`
- `tests/smoke/test_ui_tabs.py`

---

### Task 1: Build atomic incremental WAV artifacts

**Files:**

- Create: `src/vienetts_app/core/artifacts.py`
- Create: `tests/unit/test_artifacts.py`
- Modify: `src/vienetts_app/core/audio.py`
- Modify: `tests/unit/test_audio.py`

**Interfaces:**

- Consumes:
  - `DEFAULT_SAMPLE_RATE = 48_000` from `core.audio`.
  - `soundfile.SoundFile` through a small injected writer factory in tests.
- Produces:

```python
@dataclass(frozen=True)
class SynthesisArtifact:
    job_id: str
    path: Path
    sample_rate: int
    samples: int
    duration_ms: int
    timeline_path: Path | None = None
    envelope_path: Path | None = None

class ArtifactWriteError(RuntimeError): ...

class IncrementalArtifactWriter:
    def __init__(self, job_id: str, destination: Path, *,
                 sample_rate: int = 48_000,
                 writer_factory: Callable[..., Any] | None = None,
                 validate: Callable[[Path], tuple[int, int]] | None = None) -> None: ...
    @property
    def part_path(self) -> Path: ...
    @property
    def samples_written(self) -> int: ...
    def append(self, samples: np.ndarray) -> int: ...
    def finalize(self) -> SynthesisArtifact: ...
    def abort(self) -> None: ...

class InteractiveArtifactStore:
    def __init__(self, root: Path) -> None: ...
    def allocate(self, job_id: str) -> Path: ...
    def protect(self, artifact: SynthesisArtifact) -> None: ...
    def release(self, artifact: SynthesisArtifact) -> None: ...
    def remove_if_unprotected(self, artifact: SynthesisArtifact) -> bool: ...
    def cleanup_orphaned_parts(self) -> int: ...
```

- `validate_wav_artifact(path) -> tuple[frames, sample_rate]` uses SoundFile
  metadata only. It rejects a missing, zero-frame, multichannel,
  non-48-kHz, unreadable, or truncated artifact.

- [ ] **Step 1: Write failing artifact writer tests**

```python
import numpy as np
import pytest

from vienetts_app.core.artifacts import (
    ArtifactWriteError,
    IncrementalArtifactWriter,
    validate_wav_artifact,
)


def test_writer_promotes_only_after_close_and_validation(tmp_path) -> None:
    destination = tmp_path / "jobs" / "abc.wav"
    writer = IncrementalArtifactWriter("abc", destination)
    writer.append(np.full(480, 0.25, dtype=np.float32))
    writer.append(np.full(960, -0.5, dtype=np.float32))

    artifact = writer.finalize()

    assert artifact.path == destination
    assert not writer.part_path.exists()
    assert destination.exists()
    assert (artifact.samples, artifact.sample_rate, artifact.duration_ms) == (1440, 48_000, 30)
    assert validate_wav_artifact(destination) == (1440, 48_000)


def test_write_failure_deletes_partial_and_never_promotes(tmp_path) -> None:
    writer = IncrementalArtifactWriter(
        "abc",
        tmp_path / "abc.wav",
        writer_factory=FailingWriterFactory(fail_after_writes=1),
    )
    writer.append(np.ones(10, dtype=np.float32))

    with pytest.raises(ArtifactWriteError, match="write"):
        writer.append(np.ones(10, dtype=np.float32))

    writer.abort()
    assert not writer.part_path.exists()
    assert not (tmp_path / "abc.wav").exists()
```

Complete the writer matrix with these assertions:

- a non-contiguous `float64` mono slice is appended as contiguous `float32`;
- chunks containing `NaN` or `Inf` raise `ArtifactWriteError` and do not
  promote the destination;
- `finalize()` with zero written samples raises `ArtifactWriteError`;
- a preexisting malformed `<job>.part.wav`, a writer close failure, and
  post-close structural validation failure each leave no final `.wav`;
- calling `abort()` twice leaves both `.part.wav` and destination absent;
- a recording fake writer receives the original sequence of chunk lengths
  (`480`, then `960`), never one concatenated `1440`-sample array.

- [ ] **Step 2: Run focused tests to verify they fail**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_artifacts.py tests/unit/test_audio.py -v`

Expected: FAIL because artifact classes and validation helper do not exist.

- [ ] **Step 3: Implement chunk validation and incremental writer**

Use this checked append boundary:

```python
def _normalize_chunk(value: object) -> np.ndarray:
    samples = np.asarray(value)
    if samples.ndim != 1:
        raise ArtifactWriteError("audio chunk must be mono")
    if samples.size == 0:
        return np.empty(0, dtype=np.float32)
    if not np.isfinite(samples).all():
        raise ArtifactWriteError("audio chunk contains non-finite samples")
    return np.ascontiguousarray(samples, dtype=np.float32)
```

Create the parent directory, open `<stem>.part.wav` with
`soundfile.SoundFile(..., mode="w", samplerate=48_000, channels=1,
subtype="FLOAT", format="WAV")`, and retain only the writer handle plus an
integer sample counter. `append` calls `handle.write(chunk)` and increments
the counter after a successful write. It neither aggregates chunks nor emits
raw PCM.

`finalize` must:

1. reject `samples_written == 0`;
2. close the SoundFile handle, even if validation later fails;
3. call `validate_wav_artifact(part_path)`;
4. check returned frames equals `samples_written`;
5. call `os.replace(part_path, destination)` on the same volume;
6. create and return a `SynthesisArtifact`.

On every exception after construction, close the writer under
`contextlib.suppress`, delete the part path under `contextlib.suppress`, and
raise `ArtifactWriteError` without embedding user text or a full path in the
message.

`InteractiveArtifactStore.allocate()` uses
`<data_dir>/artifacts/interactive/<job_id>.wav` and rejects an ID containing
path separators. It does not sweep completed files in `allocate()`. Its
protection reference count prevents cleanup of the current artifact during a
file-player session.

- [ ] **Step 4: Run focused artifact tests**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_artifacts.py tests/unit/test_audio.py -v`

Expected: PASS, with an actual short WAV structurally verified through
SoundFile.

- [ ] **Step 5: Commit atomic artifact writing**

```bash
git add src/vienetts_app/core/artifacts.py src/vienetts_app/core/audio.py \
  tests/unit/test_artifacts.py tests/unit/test_audio.py
git commit -m "feat(audio): write synthesis artifacts incrementally"
git notes add -m "Phase 3 Task 1: added validated atomic WAV artifact writer."
```

### Task 2: Implement cooperative bounded PCM transport

**Files:**

- Create: `src/vienetts_app/core/pcm_transport.py`
- Create: `tests/unit/test_pcm_transport.py`
- Modify: `src/vienetts_app/ui/stream_playback.py`
- Modify: `tests/unit/test_stream_playback.py`

**Interfaces:**

- Produces:

```python
PCM_BYTES_PER_SECOND = 48_000 * 4
MAX_PCM_BYTES = PCM_BYTES_PER_SECOND * 2
PREBUFFER_BYTES = PCM_BYTES_PER_SECOND * 150 // 1000

class TransportClosed(RuntimeError): ...

class BoundedPcmTransport:
    def __init__(self, capacity_bytes: int = MAX_PCM_BYTES) -> None: ...
    def put(self, payload: memoryview, *,
            cancelled: Callable[[], bool] = lambda: False) -> None: ...
    def take(self, maximum_bytes: int) -> bytes: ...
    def available_bytes(self) -> int: ...
    def close(self, *, discard: bool) -> None: ...
    def wait_for_prebuffer(self, minimum_bytes: int,
                           timeout_seconds: float = 0.0) -> bool: ...
```

- `StreamPlaybackController` changes to:

```python
def start(self, transport: BoundedPcmTransport, job_id: str) -> None: ...
def notify_transport_available(self) -> None: ...
def begin_drain(self) -> None: ...
def stop(self, *, discard: bool = True) -> None: ...
@property
def buffered_drain_ms(self) -> int: ...
```

- `TransportIODevice.readData(max_size)` calls `transport.take(max_size)`;
  it owns no audio bytearray and retains no raw PCM.

- [ ] **Step 1: Write failing bounded transport tests**

```python
import threading

import pytest

from vienetts_app.core.pcm_transport import BoundedPcmTransport, TransportClosed


def test_transport_never_exceeds_its_capacity() -> None:
    transport = BoundedPcmTransport(capacity_bytes=8)
    transport.put(memoryview(b"12345678"))

    cancelled = threading.Event()
    blocked = threading.Event()

    def producer() -> None:
        blocked.set()
        with pytest.raises(TransportClosed):
            transport.put(memoryview(b"9"), cancelled=cancelled.is_set)

    thread = threading.Thread(target=producer)
    thread.start()
    assert blocked.wait(timeout=1)
    assert transport.available_bytes() == 8
    cancelled.set()
    transport.close(discard=True)
    thread.join(timeout=1)
    assert not thread.is_alive()


def test_take_wakes_blocked_producer_without_losing_order() -> None:
    transport = BoundedPcmTransport(capacity_bytes=4)
    transport.put(memoryview(b"abcd"))
    complete = threading.Event()

    thread = threading.Thread(
        target=lambda: (transport.put(memoryview(b"ef")), complete.set())
    )
    thread.start()
    assert transport.take(2) == b"ab"
    assert complete.wait(timeout=1)
    assert transport.take(10) == b"cdef"
    thread.join(timeout=1)
```

Complete the transport and playback matrix with these assertions:

- capacities `0` and `-1` raise `ValueError`;
- writing `b"abcdef"` into a four-byte transport and draining concurrently
  returns `b"abcdef"` in that order;
- `close(discard=False)` allows the queued payload to drain, then `take()`
  raises `TransportClosed`;
- `ready_for_prebuffer()` is false at `PREBUFFER_BYTES - 1` and true at
  exactly `PREBUFFER_BYTES`;
- a producer exits with `TransportClosed` when its cancellation callback
  becomes true even if `close()` was never called;
- a high-water-mark assertion proves `max_buffered_bytes <= capacity_bytes`;
- a fake sink remains stopped before `PREBUFFER_BYTES`, starts exactly once
  after `notify_transport_available()`, and receives the transport bytes in
  order.

- [ ] **Step 2: Run focused tests to verify they fail**

Run:
`QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 tests/unit/test_pcm_transport.py tests/unit/test_stream_playback.py -v`

Expected: FAIL because the transport and prebuffer API do not exist.

- [ ] **Step 3: Implement the condition-protected transport**

Use a `bytearray` with read offset protected by
`threading.Condition(threading.Lock())`. `put` slices large input up to
available capacity and waits with a short timeout (for example 50 ms) between
checks:

```python
while remaining:
    with self._condition:
        while not self._closed and self._available() >= self._capacity:
            if cancelled():
                raise TransportClosed("producer cancelled")
            self._condition.wait(timeout=0.05)
        if self._closed:
            raise TransportClosed("transport closed")
        room = self._capacity - self._available()
        n = min(room, len(remaining))
        self._buffer.extend(remaining[:n])
        remaining = remaining[n:]
        self._max_available = max(self._max_available, self._available())
        self._condition.notify_all()
```

`take` removes no more than requested bytes, advances the offset, compacts
only after the dead prefix exceeds half the buffer, and `notify_all()` wakes
producers. `close(discard=True)` clears immediately; `close(discard=False)`
allows `take` to drain before it returns `b""`.

No worker thread calls a `QObject` or emits `readyRead` directly. The
controller schedules a 20 ms GUI-thread `QTimer` while an active transport
exists. Each tick calls `notify_transport_available`, which tests
prebuffer/availability and emits the QIODevice `readyRead` from its owning
thread. This bounded timer wakeup replaces one queued Qt raw-audio event per
chunk.

- [ ] **Step 4: Refactor stream playback around the transport**

Retain the lazy QtMultimedia factories and the fake sink seam. Replace
`StreamIODevice._buffer` with:

```python
class TransportIODevice(QIODevice):
    def __init__(self, transport: BoundedPcmTransport, parent=None) -> None:
        super().__init__(parent)
        self._transport = transport
        self.open(QIODevice.OpenModeFlag.ReadOnly)

    def readData(self, max_size: int) -> bytes:  # noqa: N802
        return self._transport.take(max(0, int(max_size)))
```

`StreamPlaybackController.start(transport, job_id)` creates the I/O device
but delays `_start_sink()` until `available_bytes() >= PREBUFFER_BYTES`.
`notify_transport_available()` starts/restarts the sink only after the
prebuffer threshold, emits at most one coalesced level update per 50 ms, and
signals `readyRead` while data exists. `begin_drain()` closes with
`discard=False`; when both the transport and sink buffer report empty, it
sets the state to drained and emits `finished`.

`stop(discard=True)` closes the transport, stops the sink, stops all timers,
and drops bytes immediately. `buffered_drain_ms` combines the transport
available bytes and the sink’s known local buffer estimate only when
available; it never claims a finished state merely because the worker ended.

- [ ] **Step 5: Run focused transport and playback tests**

Run:
`QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 tests/unit/test_pcm_transport.py tests/unit/test_stream_playback.py -v`

Expected: PASS. Assert `max_available_bytes <= MAX_PCM_BYTES` in the
long-producer fake test.

- [ ] **Step 6: Commit bounded playback transport**

```bash
git add src/vienetts_app/core/pcm_transport.py \
  src/vienetts_app/ui/stream_playback.py \
  tests/unit/test_pcm_transport.py tests/unit/test_stream_playback.py
git commit -m "feat(playback): bound live PCM transport"
git notes add -m "Phase 3 Task 2: replaced unbounded queued PCM with two-second backpressure transport."
```

### Task 3: Make the worker produce artifacts, not full arrays

**Files:**

- Modify: `src/vienetts_app/core/jobs.py`
- Modify: `src/vienetts_app/workers/inference_worker.py`
- Modify: `src/vienetts_app/core/performance.py`
- Modify: `tests/unit/test_inference_worker.py`
- Modify: `tests/unit/test_jobs.py`

**Interfaces:**

- Consumes:
  - `IncrementalArtifactWriter`, `SynthesisArtifact`,
    `InteractiveArtifactStore`.
  - `BoundedPcmTransport`.
  - Phase 2 tagged job contract.
- Produces:

```python
@dataclass(frozen=True)
class JobChunk:
    job_id: str
    sample_count: int
    peak: float

@dataclass(frozen=True)
class SynthesisJob:
    # Existing Phase 2 fields...
    artifact_path: Path | None
    live_transport: BoundedPcmTransport | None = field(default=None, compare=False, repr=False)

class InferenceWorker(QThread):
    def submit(self, job: SynthesisJob | WarmupOp) -> bool: ...
```

- Successful TTS terminal:
  `JobTerminal(job_id=..., owner=..., state="completed",
  value=SynthesisArtifact(...))`.
- Job chunk metadata emits at most 20 Hz per job, plus a final segment
  metadata event. It never carries a NumPy array.
- Jobs that represent a TTS request must have an `artifact_path` before
  admission. Voice operations do not require one.

- [ ] **Step 1: Write failing worker artifact/backpressure tests**

```python
def test_stream_terminal_contains_artifact_not_numpy_array(harness, tmp_path) -> None:
    job = make_stream_job(
        "a" * 32,
        artifact_path=tmp_path / "a.wav",
        chunks=[np.full(480, 0.25, dtype=np.float32)] * 3,
    )

    harness.worker.submit(job)

    terminal = harness.wait_terminal(job.id)
    assert terminal.state == "completed"
    assert isinstance(terminal.value, SynthesisArtifact)
    assert terminal.value.samples == 1440
    assert terminal.value.path.is_file()


def test_worker_never_concatenates_duration_sized_audio(monkeypatch, harness, tmp_path) -> None:
    monkeypatch.setattr(np, "concatenate", lambda *_args, **_kwargs: pytest.fail("forbidden"))
    job = make_stream_job("a" * 32, artifact_path=tmp_path / "a.wav", chunks=repeat_chunks(400))

    harness.worker.submit(job)

    assert harness.wait_terminal(job.id).state == "completed"


def test_cancelling_while_transport_is_full_terminalizes_once(harness, tmp_path) -> None:
    transport = BoundedPcmTransport(capacity_bytes=16)
    job = make_stream_job("a" * 32, artifact_path=tmp_path / "a.wav", transport=transport)
    harness.worker.submit(job)
    harness.engine.wait_until_second_chunk()

    assert harness.worker.cancel_job(job.id)
    assert harness.wait_terminal(job.id).state == "cancelled"
    assert not (tmp_path / "a.wav").exists()
```

Complete the worker matrix with these assertions:

1. an invalid chunk emits one `failed` terminal and leaves no final artifact;
2. injected writer failure emits one `failed` terminal and closes/discards
   the transport;
3. 400 small chunks preserve the exact aggregate sample count and duration;
4. `JobChunk(job_id, samples=np.zeros(1))` raises `TypeError`;
5. a job with no live transport completes and yields an artifact;
6. a rapid fake clock yields at most 20 metadata events per elapsed second;
7. a TTS `SynthesisJob` whose `artifact_path` is `None` is rejected before
   engine invocation.

- [ ] **Step 2: Run focused worker tests to verify they fail**

Run:
`QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 tests/unit/test_jobs.py tests/unit/test_inference_worker.py -v`

Expected: FAIL because successful terminals still contain arrays and stream
accumulation is present.

- [ ] **Step 3: Implement artifact-first stream processing**

Create each writer inside the worker just before the first engine call. Delete
the `_ACCUM_INITIAL_SAMPLES` constant and the full-buffer infer/stream paths.
For normal text synthesis, controllers now submit a stream-mode request even
for the non-live “generate” action, so every TTS path shares this loop:

```python
def _process_tts(self, job: SynthesisJob, request: TTSRequest) -> None:
    assert job.artifact_path is not None
    writer = IncrementalArtifactWriter(job.id, job.artifact_path)
    try:
        for segment_index, segment in enumerate(split_text_for_streaming(request.text)):
            self._check_active_cancel(job)
            for raw_chunk in self.engine.infer_stream(
                segment, voice=request.voice, temperature=request.temperature
            ):
                self._check_active_cancel(job)
                chunk = _normalize_chunk(raw_chunk)
                writer.append(chunk)
                if job.live_transport is not None:
                    job.live_transport.put(
                        memoryview(chunk.astype("<f4", copy=False)).cast("B"),
                        cancelled=lambda: self._is_cancelled(job.id),
                    )
                self._emit_chunk_metadata(job.id, chunk)
            self.progress.emit(JobProgress(job.id, segment_index + 1, total, "generating"))
        artifact = writer.finalize()
    except _JobCancelled:
        writer.abort()
        self._close_transport(job, discard=True)
        self._terminalize(job, "cancelled")
    except (ArtifactWriteError, TransportClosed, TTSEngineError) as exc:
        writer.abort()
        self._close_transport(job, discard=True)
        self._terminalize(job, "failed", error=self._safe_error(exc))
    else:
        self._close_transport(job, discard=False)
        self._terminalize(job, "completed", value=artifact)
```

Use a private `_JobCancelled` exception rather than emitting a terminal in
several nested loop branches. `_terminalize` remains the only terminal event
source.

`_emit_chunk_metadata` calculates peak from the normalized chunk and emits
when `time.monotonic_ns() - last_emit_ns >= 50_000_000`; it keeps a pending
sample count so timeline consumers retain exact totals even when visual
updates coalesce. It explicitly emits remaining metadata before the
segment-progress tick and before terminalization.

Move all `np.asarray(audio)`/`np.concatenate` handoff code out of the worker.
The performance trace records `artifact_samples`,
`artifact_bytes_on_disk`, and `transport_max_bytes`; it no longer records
`concatenated_audio_bytes` or `retained_chunk_bytes`.

- [ ] **Step 4: Run focused worker and contract tests**

Run:
`QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 tests/unit/test_jobs.py tests/unit/test_inference_worker.py -v`

Expected: PASS. Search the worker source and confirm no
`np.concatenate(`, `parts: list`, or growing audio buffer remains.

- [ ] **Step 5: Commit artifact-producing worker**

```bash
git add src/vienetts_app/core/jobs.py src/vienetts_app/workers/inference_worker.py \
  src/vienetts_app/core/performance.py tests/unit/test_jobs.py \
  tests/unit/test_inference_worker.py
git commit -m "feat(worker): return atomic synthesis artifacts"
git notes add -m "Phase 3 Task 3: removed duration-sized worker audio accumulation."
```

### Task 4: Migrate controller replay/export and artifact playback

**Files:**

- Modify: `src/vienetts_app/ui/controller.py`
- Modify: `src/vienetts_app/ui/playback.py`
- Modify: `src/vienetts_app/ui/stream_playback.py`
- Modify: `src/vienetts_app/ui/qml/TextTab.qml`
- Modify: `src/vienetts_app/ui/qml/ParagraphTab.qml`
- Modify: `tests/unit/test_controller.py`
- Modify: `tests/unit/test_playback.py`
- Modify: `tests/smoke/test_ui_tabs.py`

**Interfaces:**

- Consumes: `SynthesisArtifact`, `InteractiveArtifactStore`,
  `BoundedPcmTransport`, tagged terminal events.
- Produces QML properties:

```python
@Property(bool, notify=hasArtifactChanged)
def hasArtifact(self) -> bool: ...

@Property(str, notify=artifactPathChanged)
def artifactPath(self) -> str: ...

@Property(str, notify=playbackStateChanged)
def playbackState(self) -> str:  # "prebuffering"|"generating"|"draining"|"idle"
```

- Compatibility: keep `hasAudio` as a forwarding property for one release,
  returning `hasArtifact`; remove all controller `_audio: np.ndarray` state.
- `exportWav(path) -> bool` starts an asynchronous `shutil.copyfile` from the
  current artifact; it does not reencode PCM.
- `replay()` plays the current managed artifact via the attached
  `PlaybackController`, not a RAM buffer or temporary rewrite.
- `PlaybackController.play(path, on_released: Callable[[], None] | None = None)`
  calls `on_released` exactly once after stop/end/error and before the
  controller cleans an artifact.

- [ ] **Step 1: Write failing controller artifact tests**

```python
def test_completed_artifact_enables_export_without_held_numpy_audio(harness, tmp_path) -> None:
    artifact = make_artifact(tmp_path / "artifacts" / "job.wav", samples=480)
    harness.controller.generateStream("hello", "")
    job = harness.worker.submitted[-1]

    harness.worker.terminal.emit(completed(job.id, artifact))

    assert harness.controller.hasArtifact is True
    assert harness.controller.hasAudio is True
    assert not hasattr(harness.controller, "_audio")
    assert harness.controller.exportWav(str(tmp_path / "user.wav"))
    assert (tmp_path / "user.wav").is_file()


def test_replay_protects_artifact_until_file_player_releases_it(harness, tmp_path) -> None:
    artifact = harness.complete_interactive_artifact(tmp_path)
    harness.controller.replay()

    harness.controller.generate("replacement", "")
    assert artifact.path.exists()

    harness.file_playback.finished.emit()
    harness.controller.release_retired_artifacts()
    assert not artifact.path.exists()
```

Complete the controller ownership matrix with these assertions:

- an unavailable audio device still leaves `hasArtifact` true after a valid
  terminal;
- injected waveform computation failure leaves the committed artifact
  replayable/exportable;
- cancelling the foreground job closes only its transport;
- a queued waveform callback for artifact A cannot overwrite the envelope
  after artifact B becomes current;
- a failed `shutil.copyfile` leaves the managed source artifact intact.

- [ ] **Step 2: Run focused controller tests to verify they fail**

Run:

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 \
  tests/unit/test_controller.py \
  tests/unit/test_playback.py \
  tests/smoke/test_ui_tabs.py -v
```

Expected: FAIL because controller completion expects a NumPy array.

- [ ] **Step 3: Implement managed artifact ownership**

Replace controller fields with:

```python
self._artifact_store = InteractiveArtifactStore(self._data_dir / "artifacts")
self._current_artifact: SynthesisArtifact | None = None
self._retired_artifacts: set[SynthesisArtifact] = set()
self._active_live_transport: BoundedPcmTransport | None = None
self._live_playback_job_id: str | None = None
self._playback_state = "idle"
```

On accepting a foreground TTS job, allocate its artifact path before
`worker.submit`, create a `BoundedPcmTransport` only for the user-selected
live streaming action, and start a lightweight GUI timer that calls
`stream_playback.notify_transport_available()`. Do not create a sink on the
non-live action.

On a matching successful terminal, verify `isinstance(event.value,
SynthesisArtifact)` and its `job_id`, make it the current artifact, mark
`hasArtifact`, and enqueue `compute_waveform_envelope_from_wav` through
`_run_bg`. Its callback must first verify that the same `artifact.job_id` is
still current before updating `waveformEnvelope`.

On a matching failure/cancel, stop only the active transport/session and
leave a prior completed artifact available. Do not blank the previous
artifact at new job submission; mark a local `generating` state separately so
users retain an explicit prior export/replay choice until replacement arrives.

`replay()` calls `self._file_playback.play(str(artifact.path),
on_released=lambda: self._release_artifact_after_playback(artifact))`.
If the wrapper does not yet support callbacks, add a per-play ownership field
and invoke the same release callback from each end/stop/error route. Never
unlink an artifact immediately after `play()`.

`exportWav` copies bytes in `_run_bg`:

```python
def work() -> tuple[str, str]:
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(artifact.path, target)
        return str(target), ""
    except OSError as exc:
        return "", self.tr("Xuất WAV thất bại: {}").format(exc)
```

Guard source existence, retain the artifact on error, and keep the existing
completion signal semantics.

- [ ] **Step 4: Update QML state and tooltip behavior**

Use `controller.hasArtifact` instead of a full-buffer implication for export
and replay buttons. Keep `hasAudio` temporarily only for existing callers
outside QML. The live meter is visible for `"prebuffering"` and
`"generating"`; the completed waveform is visible for a current artifact
after its envelope callback succeeds. Add translated labels for:

- `Đệm âm thanh…` (prebuffering),
- `Đang tạo và phát` (generating),
- `Đang phát phần còn lại…` (draining),
- export-only messaging when `audioAvailable` is false.

Maintain current `objectName` values and add
`artifactPlaybackState` for the new status line.

- [ ] **Step 5: Run focused controller, playback, and QML tests**

Run:

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 \
  tests/unit/test_controller.py \
  tests/unit/test_playback.py \
  tests/smoke/test_ui_tabs.py -v
```

Expected: PASS, including export-only generation and safe delayed cleanup.

- [ ] **Step 6: Commit controller artifact migration**

```bash
git add src/vienetts_app/ui/controller.py src/vienetts_app/ui/playback.py \
  src/vienetts_app/ui/stream_playback.py \
  src/vienetts_app/ui/qml/TextTab.qml src/vienetts_app/ui/qml/ParagraphTab.qml \
  tests/unit/test_controller.py tests/unit/test_playback.py tests/smoke/test_ui_tabs.py
git commit -m "feat(ui): replay and export committed artifacts"
git notes add -m "Phase 3 Task 4: removed controller full-audio ownership."
```

### Task 5: Convert audiobook terminals and verify duration-independent flow

**Files:**

- Modify: `src/vienetts_app/ui/audiobook_controller.py`
- Modify: `src/vienetts_app/ui/chapter_persist.py`
- Modify: `tests/unit/test_audiobook_controller.py`
- Modify: `tests/unit/test_inference_worker.py`
- Modify: `docs/performance/README.md`
- Modify: `PROJECT_PLAN.md`

**Interfaces:**

- Consumes: matching audiobook `JobTerminal(value=SynthesisArtifact)`.
- Produces:

```python
def on_synthesis_terminal(self, event: JobTerminal) -> None:
    # Matching completed artifact is staged for cache promotion in Phase 4.
```

- Transitional behavior: the existing `PersistExecutor` accepts an artifact
  path after verifying its metadata and copies/promotes it to the current
  chapter cache without `read_wav()` or a full NumPy array:

```python
def submit_artifact(
    self, library: AudiobookLibrary, book_id: str, index: int,
    artifact: SynthesisArtifact, snapshot: RenderSnapshot,
) -> None: ...
```

- [ ] **Step 1: Write failing artifact-based audiobook tests**

```python
def test_audiobook_persists_matching_artifact_without_full_audio(book_harness, tmp_path) -> None:
    book_harness.begin_render(index=0)
    job_id = book_harness.audiobook._render_job_id
    artifact = make_artifact(tmp_path / "job.wav", samples=48_000)

    book_harness.app.emit_terminal(completed(job_id, artifact))

    assert book_harness.library.chapter_wav_path(book_harness.book_id, 0).is_file()
    assert artifact.path.exists() is False  # moved/copied only after validated cache promotion


def test_audiobook_rejects_artifact_for_stale_job(book_harness, tmp_path) -> None:
    artifact = make_artifact(tmp_path / "stale.wav", samples=480)

    book_harness.audiobook.on_synthesis_terminal(completed("stale-id", artifact))

    assert not book_harness.library.chapter_wav_path(book_harness.book_id, 0).exists()
```

Complete the audiobook terminal matrix with these assertions:

- a completed terminal whose value is not a structurally valid
  `SynthesisArtifact` does not create a chapter WAV;
- matching failed and cancelled terminals leave no `ch_NNNN.wav` or
  `ch_NNNN.part.wav`;
- switching books before the old job terminal arrives cannot write into the
  newly selected book;
- accumulated `JobChunk.sample_count` values become the exact timeline
  sample total without storing raw PCM.

- [ ] **Step 2: Run focused audiobook tests to verify they fail**

Run:
`QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 tests/unit/test_audiobook_controller.py -v`

Expected: FAIL because the controller expects raw `audio`.

- [ ] **Step 3: Implement artifact path persistence**

Replace `on_synthesis_done(audio)` and `on_synthesis_error(message)` with
one terminal handler. For a completed matching event, require a
`SynthesisArtifact`, ensure its sample rate is 48 kHz and it validates, take
the existing immutable `RenderSnapshot`, and call
`persist.submit_artifact(...)`. The background job must copy to a
chapter-local `.part.wav`, validate its length, then `os.replace` it to
`ch_NNNN.wav`; only then may it delete/release the original managed artifact.
Existing timeline and envelope work runs from the chapter WAV path using
block-wise readers.

For failed/cancelled matching terminals, preserve existing Vietnamese status
semantics, but clear only `_render_job_id` belonging to the event. Never
detach a global listener, because Phase 2 eliminated it.

- [ ] **Step 4: Run focused artifact-based audiobook tests**

Run:
`QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 tests/unit/test_audiobook_controller.py -v`

Expected: PASS without `read_wav()` in the render completion path.

- [ ] **Step 5: Run Phase 3 quality gate**

Run:

```bash
./.venv/bin/ruff check .
./.venv/bin/ruff format --check .
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest
```

Expected: all commands exit 0.

- [ ] **Step 6: Document and commit Phase 3**

Document the artifact lifecycle, two-second PCM limit, export-only fallback,
and benchmark counters `artifact_bytes_on_disk` and
`transport_max_bytes`. Do not claim a real-time target without a measured
benchmark record.

```bash
git add src/vienetts_app/ui/audiobook_controller.py \
  src/vienetts_app/ui/chapter_persist.py \
  tests/unit/test_audiobook_controller.py tests/unit/test_inference_worker.py \
  docs/performance/README.md PROJECT_PLAN.md
git commit -m "feat(audiobook): persist streamed render artifacts"
git notes add -m "Phase 3 Task 5: completed artifact-first path across text and audiobook flows."
```
