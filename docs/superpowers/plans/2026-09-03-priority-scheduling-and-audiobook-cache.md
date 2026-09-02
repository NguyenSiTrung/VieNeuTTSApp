# Priority Scheduling and Audiobook Cache Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use
> superpowers:subagent-driven-development (recommended) or
> superpowers:executing-plans to implement this plan task-by-task. Steps use
> checkbox (`- [ ]`) syntax for tracking.

**Goal:** Schedule user intent ahead of background audiobook work, make
audiobook cache reuse configuration-safe, bound its storage, and move book
export/deletion work into truthful asynchronous flows.

**Architecture:** Replace the Phase 2 FIFO selection policy with a stable
priority scheduler and preserve a suspended background job’s worker-owned
artifact writer across safe segment-boundary yields. `AudiobookCache` derives a
cryptographic identity from the book/chapter/configuration inputs, validates
every hit structurally, and owns LRU quota/reserve eviction. The audiobook
controller admits requested, prefetch, and bulk jobs directly to the scheduler,
then asynchronously promotes matching completed artifacts into cache.

**Tech Stack:** Python dataclasses/hashlib/json/pathlib/shutil/threading,
SoundFile metadata, PySide6/QML, pytest, ruff.

**Spec:** `docs/superpowers/specs/2026-09-03-performance-optimization-implementation-design.md`

**Dependencies:** Implement after
`2026-09-03-artifact-first-synthesis-and-bounded-playback.md`. It extends the
same `SynthesisJob`, artifact, and tagged terminal interfaces. It must not
restore raw audio handoff, global queue cancellation, or `Path.is_file()` cache
acceptance.

## Global Constraints

- Keep exactly one engine and one inference worker. The scheduler orders work;
  it never runs inference itself.
- Stable class order is: foreground text/paragraph/cloning/voice work,
  explicitly requested audiobook chapter, next-chapter prefetch, then
  render-all bulk work. FIFO order is stable inside a class.
- A higher-priority job can preempt background work only after the current
  SDK-safe text segment completes. It never interrupts an SDK call or discards
  already committed partial WAV data.
- Each admitted job retains its single job ID across yield/resume and emits
  exactly one terminal result.
- A targeted cancellation works for active, suspended, and pending jobs, and
  only affects the requested job or owner.
- A valid audiobook cache hit requires an identity manifest and structural WAV
  validation, not just a file existence check.
- Cache identity includes: full EPUB content hash; chapter-text SHA-256;
  preset/cloned voice fingerprint; official model revision or an explicit
  immutable custom model fingerprint; `vieneu` SDK version; backend;
  precision; temperature/sampling values; segmentation version;
  audio-pipeline version; and 48 kHz sample rate.
- If an output-affecting custom model source has no immutable revision, cache
  reuse is disabled rather than guessed. It may still write a new artifact.
- Cache artifacts live only under the app audiobook workspace. Do not count,
  evict, alter, or infer ownership of user-selected exported files.
- Auto quota is 10% of the cache volume, capped at 20 GiB. A render must keep
  a reserve equal to `max(5 GiB, 5% of total volume)` after both estimated
  output and current cache usage.
- Protect playing, queued, rendering, and atomic-promotion artifacts from LRU
  eviction. A failed eviction/preflight blocks a render before inference.
- Cache stores normal WAV content and its manifest/timeline/envelope sidecars
  atomically. Startup removes only orphan `.part` files that cannot belong to
  an active process.
- All book export copies and cache metadata scans that can scale with a book
  run through the existing `bg_ops` seam. QML receives status/progress
  metadata, not per-file sync work.
- Book removal is a two-step QML operation. Backend deletion runs only after
  the user confirms the exact pending book ID.
- Do not log source EPUB path, chapter text, cloned voice content, generated
  audio, or full cache paths in benchmark traces or user-visible errors.
- Before each commit, run the task’s focused tests. Before Phase completion,
  run:
  `./.venv/bin/ruff check .`,
  `./.venv/bin/ruff format --check .`, and
  `QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest`.

## File Map

### New production files

- `src/vienetts_app/core/audiobook_cache.py`
  - Cache identity/manifest types, hit validation, safe artifact promotion,
    sidecar naming, LRU accounting, reserve preflight, protection, and
    orphan-part recovery.
- `src/vienetts_app/workers/priority_scheduler.py`
  - Stable priority selection, queued/suspended runtime bookkeeping, and
    safe-boundary preemption queries. It replaces the queue implementation
    introduced in Phase 2.

### Modified production files

- `src/vienetts_app/core/models.py`
  - Adds validated cache-quota preference.
- `src/vienetts_app/core/settings.py`
  - Persists cache preference with existing atomic settings writes.
- `src/vienetts_app/core/engine.py`
  - Exposes a privacy-safe immutable model/voice cache fingerprint.
- `src/vienetts_app/core/audiobook.py`
  - Delegates chapter audio cache checks and transitions to
    `AudiobookCache`, retains book metadata/progress APIs, and never performs
    destructive work in a read accessor.
- `src/vienetts_app/core/artifacts.py`
  - Adds a cache-owned artifact release path after successful promotion.
- `src/vienetts_app/workers/inference_worker.py`
  - Uses `PriorityScheduler`; yields background job runtimes at segment
    boundaries and resumes their open artifact writers safely.
- `src/vienetts_app/ui/controller.py`
  - Maps job kinds to priority classes and forwards `cancel_job`.
- `src/vienetts_app/ui/audiobook_controller.py`
  - Handles cached hits, submitted job IDs, async promotion/export, quota
    state, and confirmation-based removal.
- `src/vienetts_app/ui/qml/AudiobookTab.qml`
  - Adds cache quota/usage UI, asynchronous export status, and a destructive
    book-removal confirmation dialog.
- `src/vienetts_app/ui/qml/SettingsTab.qml`
  - Shows cache usage/quota and lets the user select automatic or bounded
    cache capacity.

### New tests

- `tests/unit/test_priority_scheduler.py`
- `tests/unit/test_audiobook_cache.py`

### Modified tests

- `tests/unit/test_inference_worker.py`
- `tests/unit/test_engine.py`
- `tests/unit/test_models.py`
- `tests/unit/test_settings.py`
- `tests/unit/test_audiobook.py`
- `tests/unit/test_audiobook_controller.py`
- `tests/unit/test_controller.py`
- `tests/smoke/test_ui_tabs.py`

---

### Task 1: Implement a stable priority scheduler with safe-boundary yields

**Files:**

- Create: `src/vienetts_app/workers/priority_scheduler.py`
- Create: `tests/unit/test_priority_scheduler.py`
- Modify: `src/vienetts_app/workers/job_queue.py`
- Modify: `src/vienetts_app/workers/inference_worker.py`
- Modify: `tests/unit/test_inference_worker.py`

**Interfaces:**

- Consumes: Phase 2 `SynthesisJob`, `JobOwner`, and `JobKind`.
- Produces:

```python
PRIORITY_FOREGROUND = 0
PRIORITY_REQUESTED_CHAPTER = 10
PRIORITY_PREFETCH = 20
PRIORITY_BULK = 30

def priority_for(kind: JobKind, owner: JobOwner) -> int: ...

@dataclass
class JobRuntime:
    job: SynthesisJob
    segments: tuple[str, ...]
    next_segment_index: int
    writer: IncrementalArtifactWriter | None
    submitted_sequence: int

class PriorityScheduler:
    def submit(self, job: SynthesisJob) -> bool: ...
    def take(self, timeout_seconds: float) -> JobRuntime | None: ...
    def suspend(self, runtime: JobRuntime) -> None: ...
    def has_higher_priority_than(self, priority: int) -> bool: ...
    def cancel_job(self, job_id: str) -> JobRuntime | None: ...
    def cancel_owner(self, owner: JobOwner) -> tuple[JobRuntime, ...]: ...
    def cancel_all(self) -> tuple[JobRuntime, ...]: ...
    def wake(self) -> None: ...
```

- `PriorityScheduler.take` returns the lowest `(priority, submitted_sequence)`
  pair. A suspended runtime keeps its original sequence; it cannot leap ahead
  of an already queued job with the same class.
- `InferenceWorker` adds:

```python
def cancel_job(self, job_id: str) -> bool: ...
def cancel_owner(self, owner: JobOwner) -> int: ...
```

- A runtime only yields when `job.kind in {"prefetch", "bulk"}` and the
  scheduler contains a lower numeric priority. It checks immediately after a
  complete segment and emits `JobProgress(..., stage="queued")` before
  suspension.

- [ ] **Step 1: Write failing scheduler ordering tests**

```python
def test_scheduler_orders_classes_and_preserves_fifo_within_each() -> None:
    scheduler = PriorityScheduler()
    bulk_first = runtime(job("a" * 32, owner="audiobook", kind="bulk"))
    text = runtime(job("b" * 32, owner="text", kind="interactive"))
    requested = runtime(job("c" * 32, owner="audiobook", kind="requested_chapter"))
    prefetch = runtime(job("d" * 32, owner="audiobook", kind="prefetch"))
    bulk_second = runtime(job("e" * 32, owner="audiobook", kind="bulk"))
    for item in (bulk_first, text, requested, prefetch, bulk_second):
        scheduler.submit(item.job)

    assert [scheduler.take(0).job.id for _ in range(5)] == [
        text.job.id,
        requested.job.id,
        prefetch.job.id,
        bulk_first.job.id,
        bulk_second.job.id,
    ]


def test_suspended_bulk_keeps_its_fifo_position_after_foreground_preemption() -> None:
    scheduler = PriorityScheduler()
    bulk = runtime(job("a" * 32, owner="audiobook", kind="bulk"))
    later_bulk = runtime(job("b" * 32, owner="audiobook", kind="bulk"))
    scheduler.submit(bulk.job)
    scheduler.submit(later_bulk.job)
    running = scheduler.take(0)
    scheduler.submit(job("c" * 32, owner="text", kind="interactive"))

    assert scheduler.has_higher_priority_than(running.job.priority)
    scheduler.suspend(running)
    assert scheduler.take(0).job.id == "c" * 32
    assert scheduler.take(0).job.id == "a" * 32
```

Complete the scheduler matrix with these assertions:

- `cancel_job` returns and removes the exact pending runtime and the exact
  suspended runtime, while subsequent `take(0)` never returns either;
- `cancel_owner("audiobook")` returns only audiobook runtimes and preserves
  text/cloning jobs for `take`;
- a second `submit` of the same ID returns `False` and leaves the original
  sequence unchanged;
- a thread blocked in `take(5)` returns `None` promptly after `wake()`;
- `priority_for("voice_op", "cloning")`,
  `priority_for("interactive", "paragraph")`, and text interactive work
  each equal `PRIORITY_FOREGROUND`.

- [ ] **Step 2: Run focused tests to verify they fail**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_priority_scheduler.py -v`

Expected: FAIL because the scheduler module does not exist.

- [ ] **Step 3: Implement priority selection**

Use a single `threading.Condition` and an `OrderedDict`/heap combination
protected by the same lock. Keep jobs keyed by ID to remove pending or
suspended work exactly:

```python
def _key(runtime: JobRuntime) -> tuple[int, int]:
    return runtime.job.priority, runtime.submitted_sequence

def take(self, timeout_seconds: float) -> JobRuntime | None:
    with self._condition:
        if not self._runtimes:
            self._condition.wait(timeout=max(0.0, timeout_seconds))
        if not self._runtimes:
            return None
        job_id = min(self._runtimes, key=lambda key: _key(self._runtimes[key]))
        return self._runtimes.pop(job_id)
```

The initial `submit(job)` creates a `JobRuntime` with immutable segments,
`next_segment_index=0`, writer constructed only once it first runs, and a
monotonic scheduler sequence. `suspend(runtime)` re-adds the same runtime
without changing its sequence or terminal history. Do not use a `PriorityQueue`
whose stale heap entries make cancellation hard to reason about.

- [ ] **Step 4: Migrate the worker’s segment loop**

Move writer creation and segmentation into a runtime initialization helper:

```python
def _run_runtime_until_pause_or_terminal(self, runtime: JobRuntime) -> None:
    job = runtime.job
    if runtime.writer is None:
        runtime.writer = self._create_artifact_writer(job)
    while runtime.next_segment_index < len(runtime.segments):
        self._raise_if_cancelled(job.id)
        segment = runtime.segments[runtime.next_segment_index]
        self._synthesize_one_segment(runtime, segment)
        runtime.next_segment_index += 1
        self.progress.emit(JobProgress(job.id, runtime.next_segment_index,
                                       len(runtime.segments), "generating"))
        if (
            job.kind in {"prefetch", "bulk"}
            and self._scheduler.has_higher_priority_than(job.priority)
        ):
            self.progress.emit(JobProgress(job.id, runtime.next_segment_index,
                                           len(runtime.segments), "queued"))
            self._scheduler.suspend(runtime)
            return
    self._finalize_runtime(runtime)
```

Use the same active-job cancellation event across a resumed runtime. A
background terminal is emitted only by `_finalize_runtime`, error handling, or
cancellation. `cancel_job` must call `runtime.writer.abort()` before emitting
a terminal when `PriorityScheduler.cancel_job` removes a suspended/pending
runtime **and the writer was created**. An active job still observes its own
event at a chunk or segment boundary.

- [ ] **Step 5: Write and run worker safe-boundary tests**

```python
def test_bulk_yields_after_one_complete_segment_for_queued_text_job(harness, tmp_path) -> None:
    bulk = make_segmented_job("a" * 32, "bulk", tmp_path / "bulk.wav", segments=3)
    text = make_segmented_job("b" * 32, "interactive", tmp_path / "text.wav", segments=1)
    harness.worker.submit(bulk)
    harness.engine.wait_for_completed_segments(1)
    harness.worker.submit(text)

    assert harness.wait_terminal(text.id).state == "completed"
    assert harness.engine.completed_segments_for(bulk.id) >= 1
    assert harness.wait_terminal(bulk.id).state == "completed"
```

Run:

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 \
  tests/unit/test_priority_scheduler.py \
  tests/unit/test_inference_worker.py -v
```

Expected: PASS. The fake engine call ledger shows a complete first bulk
segment, then text, then remaining bulk segments.

- [ ] **Step 6: Commit scheduler behavior**

```bash
git add src/vienetts_app/workers/priority_scheduler.py \
  src/vienetts_app/workers/job_queue.py \
  src/vienetts_app/workers/inference_worker.py \
  tests/unit/test_priority_scheduler.py tests/unit/test_inference_worker.py
git commit -m "feat(scheduler): prioritize foreground synthesis"
git notes add -m "Phase 4 Task 1: added stable priorities and safe boundary background yielding."
```

### Task 2: Define cache identity and validate cache artifacts

**Files:**

- Create: `src/vienetts_app/core/audiobook_cache.py`
- Create: `tests/unit/test_audiobook_cache.py`
- Modify: `src/vienetts_app/core/engine.py`
- Modify: `src/vienetts_app/core/audiobook.py`
- Modify: `tests/unit/test_engine.py`
- Modify: `tests/unit/test_audiobook.py`

**Interfaces:**

- Produces:

```python
CACHE_SCHEMA_VERSION = 1
SEGMENTATION_VERSION = "v1"
AUDIO_PIPELINE_VERSION = "artifact-v1"

@dataclass(frozen=True)
class CacheIdentity:
    digest: str
    payload: dict[str, str | int | float]

@dataclass(frozen=True)
class CacheHit:
    artifact: SynthesisArtifact
    identity: CacheIdentity
    accessed_at: str

@dataclass(frozen=True)
class CacheRenderMetadata:
    text: str
    segments: tuple[str, ...]
    segment_samples: tuple[int, ...]

class CacheMiss(Exception): ...
class CachePreflightError(RuntimeError): ...

class AudiobookCache:
    def identity_for(self, *, book_hash: str, chapter_text: str,
                     voice_fingerprint: str, model_fingerprint: str | None,
                     backend: str, precision: str, temperature: float,
                     sample_rate: int = 48_000) -> CacheIdentity | None: ...
    def lookup(self, book_id: str, index: int, identity: CacheIdentity) -> CacheHit | None: ...
    def promote(self, book_id: str, index: int, artifact: SynthesisArtifact,
                identity: CacheIdentity, metadata: CacheRenderMetadata) -> SynthesisArtifact: ...
    def staging_artifact_path(self, book_id: str, index: int, job_id: str) -> Path: ...
    def invalidate(self, book_id: str, index: int) -> None: ...
    def recover_orphaned_parts(self) -> int: ...
```

- `TTSEngine.cache_model_fingerprint() -> str | None` returns:
  - `official:<revision>:<sdk-version>` for a ready official model;
  - `custom:<repo>:<immutable-revision>:<sdk-version>` only when a custom
    immutable revision is explicitly available;
  - `None` for mutable/unknown sources, disabling cache reuse.
- `TTSEngine.cache_voice_fingerprint(voice: str | None) -> str` returns a
  stable preset identifier or SHA-256 of canonical cloned-voice configuration
  plus its source-file digest.

- [ ] **Step 1: Write failing identity and validation tests**

```python
def test_identity_changes_for_every_output_affecting_input(cache) -> None:
    base = cache.identity_for(
        book_hash="a" * 64,
        chapter_text="Nội dung chương.",
        voice_fingerprint="preset:Adam",
        model_fingerprint="official:pin:3.3.0",
        backend="onnx",
        precision="int8",
        temperature=0.4,
    )

    assert base is not None
    assert base.digest != cache.identity_for(
        book_hash="a" * 64, chapter_text="Nội dung đã đổi.",
        voice_fingerprint="preset:Adam", model_fingerprint="official:pin:3.3.0",
        backend="onnx", precision="int8", temperature=0.4,
    ).digest
    assert base.digest != cache.identity_for(
        book_hash="a" * 64, chapter_text="Nội dung chương.",
        voice_fingerprint="preset:Adam", model_fingerprint="official:other:3.3.0",
        backend="onnx", precision="int8", temperature=0.4,
    ).digest


def test_lookup_rejects_wav_without_matching_manifest(cache, book) -> None:
    path = cache.chapter_path(book.id, 0)
    write_short_valid_wav(path)

    assert cache.lookup(book.id, 0, identity()) is None


def test_lookup_rejects_wrong_sample_rate_and_reconciles_status(cache, book) -> None:
    cache.write_manifest_only_for_test(book.id, 0, identity())
    write_wav(path=cache.chapter_path(book.id, 0), sample_rate=44_100)

    assert cache.lookup(book.id, 0, identity()) is None
```

Complete the identity and validation matrix with these assertions:

- changing voice, backend, precision, temperature, segmentation version,
  audio-pipeline version, or SDK version produces a cache miss;
- `model_fingerprint is None` yields no identity;
- corrupt manifest JSON, corrupt WAV, mismatched frame count, and mismatched
  duration each produce a miss;
- a valid lookup advances its access timestamp through one atomic sidecar
  replacement;
- successful promotion writes a manifest whose digest equals the identity and
  writes the timeline and envelope sidecars beside the committed WAV.

- [ ] **Step 2: Run focused cache tests to verify they fail**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_audiobook_cache.py tests/unit/test_engine.py tests/unit/test_audiobook.py -v`

Expected: FAIL because cache identity and manifest APIs do not exist.

- [ ] **Step 3: Implement canonical identity and hit validation**

Build the digest from canonical JSON with sorted keys and no raw source text:

```python
def _digest_payload(payload: dict[str, str | int | float]) -> str:
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()

payload = {
    "schema": CACHE_SCHEMA_VERSION,
    "book_sha256": book_hash,
    "chapter_sha256": hashlib.sha256(chapter_text.encode("utf-8")).hexdigest(),
    "voice": voice_fingerprint,
    "model": model_fingerprint,
    "sdk": sdk_version,
    "backend": backend,
    "precision": precision,
    "temperature": temperature,
    "segmentation": SEGMENTATION_VERSION,
    "pipeline": AUDIO_PIPELINE_VERSION,
    "sample_rate": sample_rate,
}
```

Do not place chapter text, voice clip paths, EPUB paths, or arbitrary source
metadata in the JSON. Store `identity.digest`, a copy of this privacy-safe
payload, WAV frame count, sample rate, byte size, created/accessed UTC times,
and cache schema in `ch_NNNN.cache.json`.

`lookup` performs all checks before returning:

1. parse and schema-check cache manifest;
2. exact identity digest/payload match;
3. validate WAV readability, mono/48 kHz, frame count, and nonzero reasonable
   duration with `validate_wav_artifact`;
4. require matching stored frames/bytes;
5. require timeline/envelope sidecars only when manifest says they were
   generated, otherwise permit their absence as cosmetic degradation;
6. atomically refresh only `accessed_at`.

On any failure, return `None` and call `invalidate` to remove only corrupt
managed cache artifacts plus their manifest/sidecars. Do not touch exports.

- [ ] **Step 4: Implement engine configuration fingerprints**

Make official model fingerprint use the Phase 1 `ManagedModelLocation`
revision, not a raw local pathname. For cloned voices, read the existing
voice registry through the current engine helper, canonicalize the relevant
voice record, and include SHA-256s for any app-managed referenced files. A
missing referenced clone file raises a normal actionable engine error when
used and makes cache identity unavailable. Preset voices use an ID-only
fingerprint because the immutable official model revision is already in the
model component.

- [ ] **Step 5: Run focused cache, engine, and library tests**

Run:
`./.venv/bin/pytest -n 0 tests/unit/test_audiobook_cache.py tests/unit/test_engine.py tests/unit/test_audiobook.py -v`

Expected: PASS. Confirm a file with no manifest never appears as cache-ready.

- [ ] **Step 6: Commit cache correctness**

```bash
git add src/vienetts_app/core/audiobook_cache.py src/vienetts_app/core/engine.py \
  src/vienetts_app/core/audiobook.py tests/unit/test_audiobook_cache.py \
  tests/unit/test_engine.py tests/unit/test_audiobook.py
git commit -m "feat(cache): validate audiobook artifacts by identity"
git notes add -m "Phase 4 Task 2: added identity-safe cache hits and structural validation."
```

### Task 3: Add quota, reserve, protection, and LRU eviction

**Files:**

- Modify: `src/vienetts_app/core/audiobook_cache.py`
- Modify: `src/vienetts_app/core/models.py`
- Modify: `src/vienetts_app/core/settings.py`
- Modify: `src/vienetts_app/core/audiobook.py`
- Modify: `tests/unit/test_audiobook_cache.py`
- Modify: `tests/unit/test_models.py`
- Modify: `tests/unit/test_settings.py`
- Modify: `tests/unit/test_audiobook.py`

**Interfaces:**

- Produces:

```python
GIB = 1024**3
MAX_AUTO_CACHE_BYTES = 20 * GIB
MIN_FREE_RESERVE_BYTES = 5 * GIB

@dataclass(frozen=True)
class CacheCapacity:
    usage_bytes: int
    quota_bytes: int
    reserve_bytes: int
    free_bytes: int

class AudiobookCache:
    def capacity(self, configured_quota_bytes: int | None) -> CacheCapacity: ...
    def estimate_required_bytes(self, chapter_text: str) -> int: ...
    def preflight_render(self, *, estimated_bytes: int,
                         configured_quota_bytes: int | None) -> CacheCapacity: ...
    def protect(self, key: tuple[str, int]) -> None: ...
    def release(self, key: tuple[str, int]) -> None: ...
    def evict_to_fit(self, required_bytes: int,
                     configured_quota_bytes: int | None) -> CacheCapacity: ...
```

- `Settings.audiobook_cache_quota_bytes: int | None = None`, where `None`
  means automatic. Any explicit value must be from 1 GiB through 20 GiB.
- `AudiobookLibrary` exposes:

```python
def cache_capacity(self, configured_quota_bytes: int | None) -> CacheCapacity: ...
def preflight_chapter_render(self, book_id: str, index: int, text: str,
                             configured_quota_bytes: int | None) -> CacheCapacity: ...
```

- [ ] **Step 1: Write failing capacity and eviction tests**

```python
def test_auto_quota_is_ten_percent_capped_at_twenty_gib(cache, monkeypatch) -> None:
    monkeypatch.setattr(cache, "_disk_usage", lambda _path: disk(total=500 * GIB, free=200 * GIB))

    capacity = cache.capacity(configured_quota_bytes=None)

    assert capacity.quota_bytes == 20 * GIB
    assert capacity.reserve_bytes == 25 * GIB


def test_lru_evicts_oldest_unprotected_cache_only(cache, book) -> None:
    metadata = CacheRenderMetadata("chapter", ("chapter",), (48_000,))
    old = cache.promote(book.id, 0, artifact_for(cache, "old"), identity_for("old"), metadata)
    new = cache.promote(book.id, 1, artifact_for(cache, "new"), identity_for("new"), metadata)
    cache.set_accessed_at_for_test(book.id, 0, "2020-01-01T00:00:00+00:00")
    cache.set_accessed_at_for_test(book.id, 1, "2021-01-01T00:00:00+00:00")
    cache.protect((book.id, 1))

    cache.evict_to_fit(required_bytes=size_of(old), configured_quota_bytes=size_of(new))

    assert not old.path.exists()
    assert new.path.exists()


def test_preflight_refuses_render_when_reserve_cannot_be_preserved(cache, monkeypatch) -> None:
    monkeypatch.setattr(cache, "_disk_usage", lambda _path: disk(total=100 * GIB, free=4 * GIB))

    with pytest.raises(CachePreflightError, match="free space"):
        cache.preflight_render(estimated_bytes=1, configured_quota_bytes=None)
```

Complete the capacity matrix with these assertions:

- entries protected for playback, queueing, or rendering survive eviction;
- evicting a chapter removes its WAV, manifest, waveform sidecar, and no
  unrelated user-export path outside the cache root;
- settings reject explicit quotas below `1 * GIB` or above `20 * GIB`;
- preflight evicts eligible unprotected entries before raising
  `CachePreflightError`;
- equal access timestamps evict by lexicographic `(book_id, chapter_index)`;
- startup recovery removes orphan `*.part.wav` files and matching partial
  sidecars while retaining committed artifacts.

- [ ] **Step 2: Run focused quota tests to verify they fail**

Run:

```bash
./.venv/bin/pytest -n 0 \
  tests/unit/test_audiobook_cache.py \
  tests/unit/test_models.py \
  tests/unit/test_settings.py \
  tests/unit/test_audiobook.py -v
```

Expected: FAIL because capacity/preflight/protection APIs are absent.

- [ ] **Step 3: Implement capacity and preflight policy**

Use `shutil.disk_usage(self.root)` through an injected `_disk_usage` seam.
Calculate:

```python
auto_quota = min(int(usage.total * 0.10), MAX_AUTO_CACHE_BYTES)
quota = configured_quota_bytes if configured_quota_bytes is not None else auto_quota
reserve = max(MIN_FREE_RESERVE_BYTES, int(usage.total * 0.05))
```

Implement `estimate_required_bytes` without assuming model output:

```python
ESTIMATED_WAV_BYTES_PER_CHAR = 6 * 1024
MIN_RENDER_RESERVATION_BYTES = 16 * 1024 * 1024

def estimate_required_bytes(self, chapter_text: str) -> int:
    return max(MIN_RENDER_RESERVATION_BYTES, len(chapter_text) * ESTIMATED_WAV_BYTES_PER_CHAR)
```

Preflight first calls `evict_to_fit(estimated_bytes, quota)`, then rejects if
either:

```python
usage_after_eviction + estimated_bytes > quota
free_bytes - estimated_bytes < reserve
```

The exception says required additional space and available space in
human-readable sizes, but not the full local path.

`evict_to_fit` scans only valid cache manifests under book directories,
sorts unprotected entries by `(accessed_at, book_id, index)`, deletes via
same-directory rename-to-`.deleting` followed by recursive unlink, and checks
capacity after each deletion. If no unprotected candidate remains, let
`preflight_render` report the block. It does not use `shutil.rmtree` over a
path outside the known library root.

- [ ] **Step 4: Implement settings validation and library delegation**

Add `audiobook_cache_quota_bytes` to `Settings`; validate `None` or an
integer (bool rejected) satisfying:

```python
1 * GIB <= quota <= MAX_AUTO_CACHE_BYTES
```

Expose this setting through AppController only after the cache API is ready.
Update `AudiobookLibrary.remove_book` to call cache’s confined delete helper
and drop protection bookkeeping. On app startup, call
`library.recover_cache()` after model-free controller construction and after
the QML shell is visible, not while `create_app` constructs objects.

- [ ] **Step 5: Run focused capacity suite**

Run:

```bash
./.venv/bin/pytest -n 0 \
  tests/unit/test_audiobook_cache.py \
  tests/unit/test_models.py \
  tests/unit/test_settings.py \
  tests/unit/test_audiobook.py -v
```

Expected: PASS. The tests must calculate quota against injected disk sizes,
not the developer machine.

- [ ] **Step 6: Commit bounded cache lifecycle**

```bash
git add src/vienetts_app/core/audiobook_cache.py src/vienetts_app/core/models.py \
  src/vienetts_app/core/settings.py src/vienetts_app/core/audiobook.py \
  tests/unit/test_audiobook_cache.py tests/unit/test_models.py \
  tests/unit/test_settings.py tests/unit/test_audiobook.py
git commit -m "feat(cache): bound audiobook storage and reserve disk"
git notes add -m "Phase 4 Task 3: added LRU cache quota, protection, and free-space preflight."
```

### Task 4: Admit audiobook job classes and promote verified artifacts

**Files:**

- Modify: `src/vienetts_app/ui/controller.py`
- Modify: `src/vienetts_app/ui/audiobook_controller.py`
- Modify: `src/vienetts_app/core/audiobook.py`
- Modify: `tests/unit/test_controller.py`
- Modify: `tests/unit/test_audiobook_controller.py`

**Interfaces:**

- Consumes:
  - `PriorityScheduler` behavior through the worker.
  - `AudiobookCache.lookup`, `preflight_render`, `protect`, `release`, and
  `promote`.
  - Phase 3 `SynthesisArtifact`.
- Produces:

```python
class AppController(QObject):
    def prepare_stream_for_listener(
        self, text: str, voice: str | None, *,
        kind: Literal["requested_chapter", "prefetch", "bulk"],
        artifact_path_for_job: Callable[[str], Path],
        cache_fingerprint: str | None = None,
    ) -> SynthesisJob: ...
    def submit_prepared_listener_job(
        self, job: SynthesisJob, listener: Any
    ) -> bool: ...
    def cancel_job(self, job_id: str) -> bool: ...
    def cache_quota_preference(self) -> int | None: ...

class AudiobookController(QObject):
    @Property(int, notify=cacheUsageBytesChanged)
    def cacheUsageBytes(self) -> int: ...
    @Property(int, notify=cacheQuotaBytesChanged)
    def cacheQuotaBytes(self) -> int: ...
    @Property(str, notify=cacheStatusChanged)
    def cacheStatus(self) -> str: ...
```

- Audiobook state tracks:

```python
@dataclass(frozen=True)
class RenderTarget:
    book_id: str
    chapter_index: int
    kind: Literal["requested_chapter", "prefetch", "bulk"]
    identity: CacheIdentity | None
    metadata: CacheRenderMetadata

self._render_jobs: dict[str, RenderTarget]
self._requested_by_chapter: dict[int, str]
self._cache_protected_keys: set[tuple[str, int]]
```

- `RenderTarget` freezes book ID, chapter index, kind, cache identity, and
  timeline capture. It is never recreated from the mutable selected book.

- [ ] **Step 1: Write failing priority/cache integration tests**

```python
def test_play_requested_chapter_precedes_queued_bulk_render(book_harness) -> None:
    book_harness.open_sample()
    book_harness.audiobook.renderAllPending()
    bulk_ids = list(book_harness.audiobook._render_jobs)

    book_harness.audiobook.playChapter(2)

    requested_id = book_harness.audiobook._requested_by_chapter[2]
    assert book_harness.worker.job(requested_id).kind == "requested_chapter"
    assert book_harness.worker.job(requested_id).priority < book_harness.worker.job(bulk_ids[0]).priority


def test_valid_identity_hit_plays_without_worker_submission(book_harness) -> None:
    book_harness.open_sample()
    book_harness.seed_valid_cache(index=0)
    before = len(book_harness.worker.submitted)

    book_harness.audiobook.playChapter(0)

    assert len(book_harness.worker.submitted) == before
    assert book_harness.audiobook.playerState == "playing"


def test_preflight_failure_does_not_admit_or_mark_rendering(book_harness) -> None:
    book_harness.cache.raise_preflight = True
    book_harness.open_sample()

    book_harness.audiobook.renderChapter(0)

    assert book_harness.audiobook.renderingIndex == -1
    assert book_harness.worker.submitted == []
```

Complete the controller/cache matrix with these assertions:

- `renderAllPending()` submits every eligible chapter with kind `"bulk"`;
- finishing playback for chapter `i` admits only `i + 1` with kind
  `"prefetch"`;
- a matching terminal promotes only its `RenderTarget.book_id` and
  `RenderTarget.chapter_index`, even after selection changes;
- an injected promotion failure marks that target failed and leaves no ready
  chapter artifact;
- cancellation releases the target’s cache protection key exactly once;
- a text terminal completes normally while audiobook bulk jobs remain
  queued in the fake scheduler.

- [ ] **Step 2: Run focused integration tests to verify they fail**

Run:

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 \
  tests/unit/test_controller.py \
  tests/unit/test_audiobook_controller.py -v
```

Expected: FAIL because the controller does not accept job classes/artifact
paths and cache checks use only `has_chapter_audio`.

- [ ] **Step 3: Implement cache-aware admission**

At every requested render/play, derive engine and voice fingerprints, then
ask `cache.identity_for`. If identity is unavailable, set a visual
“rendering without reusable cache” status and submit an artifact job but
never accept an old artifact as a hit.

For a usable identity:

```python
hit = self._cache.lookup(book_id, index, identity)
if hit is not None:
    self._cache.protect((book_id, index))
    self._play_cached_hit(hit)
    return
capacity = self._cache.preflight_render(
    estimated_bytes=self._cache.estimate_required_bytes(chapter.text),
    configured_quota_bytes=self._app.cache_quota_preference(),
)
```

The concrete prepared-job contract is:

```python
@dataclass(frozen=True)
class PreparedListenerJob:
    job: SynthesisJob
    target: RenderTarget

def prepare_audiobook_job(
    self,
    *,
    book_id: str,
    chapter_index: int,
    chapter_text: str,
    voice: str | None,
    kind: Literal["requested_chapter", "prefetch", "bulk"],
    identity: CacheIdentity | None,
    metadata: CacheRenderMetadata,
) -> PreparedListenerJob: ...

def submit_prepared_listener_job(
    self, prepared: PreparedListenerJob
) -> bool: ...
```

Implement the preparation and submit paths exactly as follows:

```python
def prepare_audiobook_job(
    self, *, book_id, chapter_index, chapter_text, voice, kind, identity, metadata
) -> PreparedListenerJob:
    job = self._app.prepare_stream_for_listener(
        chapter_text,
        voice,
        kind=kind,
        artifact_path_for_job=lambda job_id: self._cache.staging_artifact_path(
            book_id, chapter_index, job_id
        ),
        cache_fingerprint=identity.digest if identity is not None else None,
    )
    return PreparedListenerJob(
        job=job,
        target=RenderTarget(book_id, chapter_index, kind, identity, metadata),
    )

def submit_prepared_listener_job(self, prepared: PreparedListenerJob) -> bool:
    self._render_jobs[prepared.job.id] = prepared.target
    if self._app.submit_prepared_listener_job(prepared.job, self):
        return True
    self._render_jobs.pop(prepared.job.id, None)
    return False
```

`AppController.prepare_stream_for_listener` creates the immutable job ID
before it invokes `artifact_path_for_job(job.id)`, then returns the job with
that path. `AppController.submit_prepared_listener_job` registers the listener
and submits exactly once, removing the listener if admission returns `False`.
This replaces Phase 2 `submit_stream_for_listener`; it avoids guessing an
artifact path from an as-yet-unknown ID and avoids a listener registry leak if
worker admission fails.

```python
def prepare_stream_for_listener(
    self, text, voice, *, kind, artifact_path_for_job, cache_fingerprint=None
) -> SynthesisJob:
    request = TTSRequest(
        text=text,
        voice=voice or None,
        mode="stream",
        temperature=self._settings.temperature,
    )
    base = new_synthesis_job("audiobook", kind, request)
    return replace(
        base,
        artifact_path=artifact_path_for_job(base.id),
        cache_fingerprint=cache_fingerprint,
    )

def submit_prepared_listener_job(self, job: SynthesisJob, listener: Any) -> bool:
    worker = self._ensure_worker()
    self._listener_by_job_id[job.id] = listener
    if worker.submit(job):
        self._performance.begin(
            job.id, {"char_count": len(job.request.text), "mode": "stream"}
        )
        self._performance.mark(job.id, "submitted")
        return True
    self._listener_by_job_id.pop(job.id, None)
    return False
```

On a matching completed `SynthesisArtifact`, enqueue cache promotion through
the single persist/background executor. The promotion job validates artifact
identity and WAV metadata, commits to `<book>/ch_NNNN.wav`, writes the cache
manifest and sidecars atomically, and only then emits ready. It releases
worker staging artifact and cache protection on all terminal/promotion paths.

On playback start, protect the chapter key; release it in the wrapped
PlaybackController end/stop/error callback. This ensures LRU cannot remove a
file being read on Windows.

- [ ] **Step 4: Implement continuous queue intent correctly**

`renderAllPending` prepares all currently pending chapters as `bulk` jobs
after independently passing each preflight, stopping as soon as a preflight
block is reported. `playChapter(i)` submits `requested_chapter`; when
playback begins, it may prepare only `i + 1` as `prefetch`. Do not create a
second prefetch if that chapter already has a request/bulk job in
`_render_jobs`.

For a duplicate user click, retain the existing job and set it as the
play-after-complete target. For a cache hit, remove any queued background
job for the same chapter through targeted cancellation and play the
validated cache. Statuses distinguish `queued`, `rendering`, `ready`, and
`failed`; QML keeps its existing `rendering` display only for the active
worker runtime.

- [ ] **Step 5: Run focused integration suite**

Run:

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 \
  tests/unit/test_controller.py \
  tests/unit/test_audiobook_controller.py -v
```

Expected: PASS. Inspect fake scheduler call order to prove requested work
overtakes bulk at a safe boundary.

- [ ] **Step 6: Commit scheduler/cache controller integration**

```bash
git add src/vienetts_app/ui/controller.py src/vienetts_app/ui/audiobook_controller.py \
  src/vienetts_app/core/audiobook.py tests/unit/test_controller.py \
  tests/unit/test_audiobook_controller.py
git commit -m "feat(audiobook): schedule cache-aware chapter renders"
git notes add -m "Phase 4 Task 4: connected job priorities to validated chapter cache lifecycle."
```

### Task 5: Move book export and deletion to safe, asynchronous UX

**Files:**

- Modify: `src/vienetts_app/ui/audiobook_controller.py`
- Modify: `src/vienetts_app/ui/qml/AudiobookTab.qml`
- Modify: `src/vienetts_app/ui/qml/SettingsTab.qml`
- Modify: `tests/unit/test_audiobook_controller.py`
- Modify: `tests/smoke/test_ui_tabs.py`
- Modify: `README.md`
- Modify: `PROJECT_PLAN.md`

**Interfaces:**

- Produces QML properties/signals:

```python
exportingChanged = Signal()
exportProgressChanged = Signal()
exportFinished = Signal(str, int, bool)  # destination, files, success
pendingRemovalBookIdChanged = Signal()
cacheUsageBytesChanged = Signal()
cacheQuotaBytesChanged = Signal()
cacheStatusChanged = Signal()

@Property(bool, notify=exportingChanged)
def exporting(self) -> bool: ...
@Property(float, notify=exportProgressChanged)
def exportProgress(self) -> float: ...
@Property(str, notify=pendingRemovalBookIdChanged)
def pendingRemovalBookId(self) -> str: ...

@Slot(int, str, result=bool)
def exportChapter(self, index: int, dest_dir: str) -> bool: ...
@Slot(str, result=bool)
def exportAllReady(self, dest_dir: str) -> bool: ...
@Slot(str)
def requestRemoveBook(self, book_id: str) -> None: ...
@Slot(str)
def confirmRemoveBook(self, book_id: str) -> None: ...
@Slot()
def cancelRemoveBook(self) -> None: ...
```

- [ ] **Step 1: Write failing asynchronous UI/controller tests**

```python
def test_export_all_returns_promptly_and_reports_progress(book_harness, tmp_path) -> None:
    book_harness.seed_ready_chapters(2)

    assert book_harness.audiobook.exportAllReady(str(tmp_path / "exports")) is True
    assert book_harness.audiobook.exporting is True

    book_harness.complete_background_export()
    assert book_harness.audiobook.exporting is False
    assert book_harness.audiobook.exportProgress == 1.0


def test_book_removal_requires_matching_confirmation(book_harness) -> None:
    book_harness.open_sample()
    book_id = book_harness.audiobook.currentBookId

    book_harness.audiobook.requestRemoveBook(book_id)
    book_harness.audiobook.confirmRemoveBook("other")
    assert book_harness.audiobook.currentBookId == book_id

    book_harness.audiobook.confirmRemoveBook(book_id)
    assert book_harness.audiobook.currentBookId == ""
```

Add QML smoke assertions for object names `removeBookDialog`,
`confirmRemoveBookButton`, `cancelRemoveBookButton`, `cacheUsageLabel`,
`cacheQuotaControl`, `audiobookExportProgress`, and for confirm-only removal
behavior. Add a test that export copies use a frozen `BookState`/chapter
metadata snapshot rather than calling `load_book` in the background loop.

- [ ] **Step 2: Run focused UX tests to verify they fail**

Run:

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 \
  tests/unit/test_audiobook_controller.py \
  tests/smoke/test_ui_tabs.py -v
```

Expected: FAIL because exports are synchronous and removeBook deletes at the
first click.

- [ ] **Step 3: Implement background export**

At acceptance, snapshot only ready chapter `(index, title, wav_path)` tuples
from current in-memory book state, validate that each path belongs under the
cache root, set `exporting=True`, and submit one background operation:

```python
def work() -> tuple[int, str]:
    completed = 0
    for index, title, source in snapshot:
        target = make_export_target(dest_dir, index, title)
        shutil.copyfile(source, target)
        completed += 1
        self._export_progress_signal.emit(completed / len(snapshot))
    return completed, ""
```

Marshal progress to the GUI thread through a Qt signal, cap visual updates at
20 Hz, and reset state on success/failure. The task rejects a second export
while one is active. A source that disappears after snapshot causes a
truthful failure result without deleting any cache or user export. Do not
reuse `AudiobookLibrary.export_chapter` if it reparses `book.json` on each
copy; refactor its naming helper into pure code used by both paths.

- [ ] **Step 4: Implement confirmation and cache status**

Replace QML’s direct `audiobook.removeBook(...)` click with
`requestRemoveBook(...)`. It sets only `pendingRemovalBookId`; no file is
removed. The QML dialog reads the selected book title/count, uses destructive
Vietnamese copy that says generated chapter audio is permanently removed, and
calls `confirmRemoveBook(pendingRemovalBookId)` only from its affirmative
button. Escape/outside close invokes `cancelRemoveBook`.

`confirmRemoveBook` compares exact IDs, cancels only jobs owned by that
book’s render targets, stops/release playback protection, waits for no active
promotion for that book, then calls the confined cache/library delete helper.
It clears the pending ID regardless of outcome and surfaces a short error if
an OS deletion fails.

Surface `cacheUsageBytes`, resolved quota, and status on the audiobook page
and Settings. The Settings capacity choice uses values `auto`, `5`, `10`,
and `20` GiB and writes only the validated corresponding setting. It does not
run eviction on every QML binding; explicit quota change schedules the
preflight/eviction operation in background.

- [ ] **Step 5: Run focused UX tests**

Run:

```bash
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest -n 0 \
  tests/unit/test_audiobook_controller.py \
  tests/smoke/test_ui_tabs.py -v
```

Expected: PASS, including one click opening a confirmation without deleting
files and a background export status transition.

- [ ] **Step 6: Run complete quality gate and commit**

Run:

```bash
./.venv/bin/ruff check .
./.venv/bin/ruff format --check .
QT_QPA_PLATFORM=offscreen ./.venv/bin/pytest
```

Expected: all commands exit 0.

Document stable priority classes, cache identity/reuse limits, quota defaults,
reserve behavior, export-only user ownership, and book-removal confirmation.

```bash
git add src/vienetts_app/ui/audiobook_controller.py \
  src/vienetts_app/ui/qml/AudiobookTab.qml src/vienetts_app/ui/qml/SettingsTab.qml \
  tests/unit/test_audiobook_controller.py tests/smoke/test_ui_tabs.py \
  README.md PROJECT_PLAN.md
git commit -m "feat(audiobook): add cache controls and safe exports"
git notes add -m "Phase 4 Task 5: completed priority UX, cache visibility, async export, and destructive confirmation."
```
