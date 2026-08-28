# Adaptive performance and resource optimization, design

Date: 2026-08-28

Status: approved in chat, awaiting review of the persisted specification

Scope: cross-platform CPU and NVIDIA optimization, streaming, audiobook,
resource efficiency, and performance UX

Related issue: `VieNeuTTSApp-8jm`

## Executive summary

VieNeuTTS App should optimize for useful audio generated per second and per
watt while meeting hard limits for first-audio latency, UI responsiveness,
memory, cancellation, and playback stability. Maximum CPU or GPU utilization
is not itself a goal because unrestricted saturation can increase audio
underruns, event-loop latency, memory pressure, power draw, and total job time.

The selected design keeps exactly one active VieNeu engine instance, adds a
priority-aware job scheduler, writes generated audio incrementally to an
atomic artifact, bounds the live playback transport, and provides three
high-level presets:

- **Auto**, the default, chooses the fastest measured configuration that
  satisfies UX, memory, and audio-stability limits.
- **Performance** favors throughput and may preload the model or use CUDA
  microbatching for bulk work.
- **Efficiency** reduces thread count and speculative work while preserving
  real-time synthesis.

The implementation must begin with production-path instrumentation. Existing
performance observations are useful directional evidence, but they are not
yet reproducible cross-platform product benchmarks.

## Problem

The current app already keeps inference off the main thread and uses
segmented SDK streaming. It also measured approximately 100 ms to the first
SDK chunk and approximately 1.12 GB RSS on one Apple M4 development machine.
However, the full application pipeline still has several structural limits.

1. `AppController` passes raw `backend="auto"` to VieNeu instead of enforcing
   the documented workload policy. On a compatible NVIDIA system this may
   choose PyTorch for short interactive streaming even when ONNX would provide
   better latency or efficiency.
2. `InferenceWorker._process_stream()` retains every audio chunk and then
   allocates a second full waveform with `np.concatenate`.
3. `StreamPlaybackController.feed()` converts each chunk to bytes and appends
   it to an unbounded `bytearray`. Removing consumed bytes from the front
   repeatedly shifts the remaining buffer.
4. The worker queue has no job IDs, ownership, priorities, or targeted
   cancellation. Foreground work, audiobook pre-render, render-all, denoise,
   and voice mutation share one anonymous FIFO.
5. Worker completion and audible playback completion are treated as one UI
   state even though buffered audio may still be draining.
6. Audiobook chapter audio crosses to the main thread as one complete NumPy
   array and is encoded synchronously there.
7. The current worker "batch" path submits a list containing only one text,
   so it cannot use VieNeu's CUDA static batching.
8. Hardware detection synchronously imports or probes expensive components
   during startup and does not retain all data needed to prove CUDA usability.
9. Long-running cache, import, export, and persistence operations can block
   the GUI thread.

These issues limit resource utilization in two ways: useful parallel work is
not overlapped where it is safe, while memory and event traffic grow where
they should remain bounded.

## Evidence and interpretation

### Existing observations

| Observation | Actual measurement | Limitation |
|---|---|---|
| 99 to 102 ms first audio | First yielded SDK chunk after a preloaded ONNX-int8 engine, three runs, one short Vietnamese sentence on an Apple M4 | Excludes controller setup, worker queueing, Qt signal delivery, sink prebuffering, output-device latency, and cold model load |
| Approximately 1.12 GB streaming RSS | Direct `TTSEngine.infer_stream_chunked()` run on approximately 5,120 characters with yielded chunks discarded | Does not execute the production worker, which retains and concatenates all chunks |
| Approximately 2.5 GB non-stream RSS | Direct long-text SDK `infer()` observation on the same M4 | One OS, CPU, language family, and process path |
| Cross-platform throughput | Not measured | Windows, Ubuntu, Intel macOS, and NVIDIA remain uncharacterized |

Sources:

- `conductor/archive/phase04_streaming_20260827/learnings.md`
- `docs/spike-report.md`
- `scripts/spike/phase0_perf.py`
- `scripts/spike/phase0_rss_current.py`
- `src/vienetts_app/workers/inference_worker.py`

### Required wording

Until production-path benchmarks exist, documentation must distinguish:

- time to first chunk, TTFC;
- time to first sink write;
- time to first audible sample, TTFA;
- cold process with warm disk/model cache;
- cold model initialization;
- warm synthesis;
- direct engine measurements;
- full controller, worker, audio, and UI measurements.

No design claim may promise a percentage speedup before a reproducible
baseline establishes it.

## Goals

1. Bound application transport memory independently of text or audio duration.
2. Preserve responsive UI and stable audio while using available compute.
3. Prioritize user-initiated synthesis over speculative background work.
4. Use ONNX threads and CUDA batches according to measured device capability.
5. Improve cold and perceived startup without loading large runtimes
   unnecessarily.
6. Make cancellation targeted, race-free, and visibly immediate.
7. Make audiobook caches correct across voice, text, model, and sampling
   changes.
8. Produce repeatable performance evidence across supported platforms.
9. Keep all synthesis, metrics, and adaptation fully offline.

## Non-goals

- Reimplementing VieNeu or its neural networks.
- Adding AMD ROCm, Apple MPS, or Intel GPU execution without upstream SDK
  support.
- Running multiple CPU VieNeu engines merely to occupy more cores.
- Keeping ONNX and CUDA engines loaded simultaneously by default.
- Cloud telemetry or uploading benchmark data.
- Promising identical sampled audio across CUDA batch sizes.
- Adding compressed audio export as part of this work.

## Architecture decision

### Selected approach

Use an adaptive scheduler and a duration-independent artifact pipeline while
retaining one active VieNeu engine instance.

```text
QML and controllers
        |
        | typed jobs with ID, owner, priority, and preset
        v
SynthesisScheduler
        |
        | one active engine operation
        v
InferenceExecutor and VieNeu
        |
        +----> IncrementalArtifactWriter ----> atomic WAV/cache artifact
        |
        +----> bounded playback cursor ------> QAudioSink
        |
        +----> coalesced metadata -----------> progress and waveform UI
```

Safe non-model I/O may execute concurrently through a small bounded pool.
Model inference, voice mutation, and denoise remain serialized.

### Rejected alternatives

#### Patch-only

Local buffer limits and thread controls would provide quick gains but would
not solve job ownership, backend lifecycle, hard cancellation, stale events,
or long-term output memory.

#### Dual ONNX and CUDA engines

Keeping two models loaded could improve simultaneous interactive and bulk
performance on NVIDIA workstations, but it duplicates RAM and VRAM, increases
power use and packaging complexity, and offers no benefit to most supported
machines. It may be reconsidered only after single-engine switching is
measured and fails foreground-latency targets.

## Domain model

### Performance preset

```python
PerformancePreset = Literal["auto", "performance", "efficiency"]
```

Precision remains a separate user decision:

```python
QualityPreset = Literal["standard_int8", "maximum_quality_fp32"]
```

PyTorch execution reports its actual dtype separately because ONNX precision
labels do not accurately describe CUDA execution.

### Execution plan

```python
@dataclass(frozen=True)
class ExecutionPlan:
    backend: Literal["onnx", "torch"]
    precision: Literal["int8", "fp32"]
    intra_op_threads: int
    max_batch_size: int
    preload: bool
    prebuffer_ms: int
    background_render: bool
    memory_budget_bytes: int
    vram_headroom_bytes: int | None
```

An execution plan is immutable for one engine lifecycle. Auto mode may select
a different plan between jobs, but it must not continuously reconfigure a
live ONNX session.

### Synthesis job

```python
@dataclass(frozen=True)
class SynthesisJob:
    id: UUID
    owner: Literal["text", "paragraph", "audiobook", "cloning"]
    kind: Literal["interactive", "requested_chapter", "prefetch", "bulk", "voice_op"]
    priority: int
    request: TTSRequest
    artifact_path: Path
    cache_fingerprint: str | None
```

Every event carries the job ID. Every accepted job receives exactly one
terminal result:

```python
JobTerminal = Literal["completed", "cancelled", "failed", "superseded"]
```

Controllers ignore stale events whose job ID no longer matches their active
job. A result remains bound to its immutable owner, book ID, and chapter.

### Text segment

```python
@dataclass(frozen=True)
class TextSegment:
    text: str
    source_start: int
    source_end: int
    boundary_before: Literal["none", "sentence", "paragraph"]
    boundary_after: Literal["none", "sentence", "paragraph"]
```

The current splitter folds whitespace and can erase paragraph semantics.
Segment metadata must preserve intended silence, timeline offsets, and source
mapping while still enforcing the memory-safe character cap.

### Artifact result

```python
@dataclass(frozen=True)
class SynthesisArtifact:
    path: Path
    sample_rate: int
    samples: int
    duration_ms: int
    timeline_path: Path | None
    envelope_path: Path | None
```

Normal jobs return artifact metadata rather than a duration-sized NumPy
array. A small in-memory compatibility path may remain for short test or API
requests, but it must have an explicit size threshold.

## Scheduler

### Priority order

1. Foreground text, paragraph, and cloning requests.
2. Audiobook chapter explicitly requested for playback.
3. Next-chapter pre-render.
4. Render-all or other bulk background work.

Background synthesis may yield at a safe segment boundary. Completed segments
remain committed to the partial artifact, so foreground preemption does not
discard prior work.

### Queue guarantees

- One active engine operation.
- Stable FIFO ordering within the same priority.
- No anonymous global busy state.
- No queue-wide cancellation.
- Cancel-before-dequeue produces an immediate terminal cancellation event.
- A controller may have at most one foreground job unless it explicitly
  supports a queue.
- Shutdown stops admission, terminates queued jobs, requests active-job
  cancellation, and cleans or records partial artifacts.
- Retired engines must not remain unaccounted for after a shutdown timeout.

### Parallelism boundary

Serialized:

- VieNeu inference;
- voice enrollment and removal;
- denoise;
- model initialization, teardown, and backend changes.

Bounded parallel work:

- EPUB hashing and parsing;
- cache validation;
- JSON persistence;
- artifact copying and export;
- cache eviction;
- non-content performance sampling.

CUDA bulk only:

- true microbatches of similarly sized chunks or texts;
- one voice and compatible sampling settings per batch;
- immediate artifact write and release after each result group.

CPU `infer_batch` is sequential in VieNeu 3.3.0. More app inference threads
would compete with ONNX's internal pools and duplicate model memory rather
than provide reliable throughput.

## Incremental artifact pipeline

For each generated chunk:

1. Validate a one-dimensional float32 mono array at 48 kHz.
2. Convert at most once to the canonical contiguous representation.
3. Append to `<job>.part.wav` through a streaming writer.
4. Flush enough state for the playback cursor to observe committed frames.
5. Update committed sample count, timeline, and envelope metadata.
6. Emit a small coalesced metadata event rather than retaining the waveform.
7. Release the chunk before requesting the next segment when the SDK allows.

On success:

1. finalize the WAV header;
2. flush and close;
3. validate frame count and sample rate;
4. atomically rename the artifact;
5. atomically commit cache or job state;
6. emit `SynthesisArtifact`.

On cancellation or failure, retain only a resumable partial explicitly
recorded by the job manifest. Otherwise remove the partial file. Existing
final artifacts are never overwritten until the new file is valid.

The growing `.part.wav` reader/writer contract must be proven on macOS,
Windows, and Linux before replacing the current path. It must not depend on
renaming an open file where Windows semantics differ.

## Audio pipeline

Use controlled `QAudioSink` push mode with an application-owned pump.

1. Configure the audio buffer before `start()`.
2. Wait for approximately 100 to 200 ms of committed audio before starting.
3. Write no more than `bytesFree()` from the artifact playback cursor.
4. Keep at most one to two seconds of PCM in live memory.
5. Monitor `stateChanged`, errors, processed time, and underruns.
6. Refill on a bounded cadence without emitting one UI event per SDK chunk.
7. Keep playback active after synthesis completes until the final frame
   drains.

At 48 kHz, mono, float32, two seconds of live PCM is 384,000 bytes.

Waveform animation follows consumed playback time. It does not follow
generation speed, which may be many times faster than real time. Hidden tabs
must not process waveform updates. UI-facing envelope and progress updates
are capped at approximately 20 to 30 Hz.

If no audio device is available, the app skips sink creation and generates
the artifact without showing an audio error. If the device disappears,
synthesis continues and playback can resume from the committed artifact.

Qt requires sink buffer sizing before `start()` and supports raw QIODevice
streaming. The implementation must follow the supported state and error
transitions documented by Qt:
<https://doc.qt.io/qt-6/qaudiosink.html>.

## Adaptive resource policy

### Non-NVIDIA systems

Apple Silicon, Intel macOS, AMD/iGPU systems, and machines without usable
CUDA use ONNX int8 by default.

- **Auto:** benchmark-derived thread count that meets all UX limits.
- **Performance:** highest validated throughput configuration.
- **Efficiency:** lower thread count, no aggressive pre-render, lazy model
  loading.
- **Maximum quality:** explicit ONNX fp32 request, separate from the
  performance preset.

### NVIDIA systems

Auto mode keeps one backend loaded:

1. Interactive use begins with ONNX unless device profiling proves CUDA is
   better for that hardware and workload.
2. For sustained bulk work, estimate whether expected CUDA savings exceed
   engine-switch and model-load cost.
3. If worthwhile and memory is sufficient, yield safely, unload ONNX, and
   load CUDA.
4. Keep CUDA loaded for the active bulk session and serve urgent jobs through
   that engine.
5. Return to ONNX only after an idle period or an explicit mode change.

Performance mode may preload CUDA when the user selects it. Efficiency mode
does not switch for speculative work.

### Thread and batch candidates

Reference hardware benchmarking evaluates:

```text
ONNX intra-op threads: 1, 2, 4, 6, 8
CUDA microbatch size:  1, 2, 4, 8, 16
```

ONNX Runtime supports explicit thread counts, affinity, bounded spinning, and
shared pools. VieNeu already exposes a `threads` constructor argument and
currently disables spinning, which is a reasonable efficiency default. The
app must tune this interface rather than creating more model workers.

Official reference:
<https://onnxruntime.ai/docs/performance/tune-performance/threading.html>.

VieNeu's CUDA code currently uses `torch.no_grad`. `torch.inference_mode`
could reduce additional autograd overhead if upstream compatibility and
output equivalence are verified. This is an upstream SDK opportunity, not a
required app patch.

Official reference:
<https://docs.pytorch.org/docs/2.8/generated/torch.autograd.grad_mode.inference_mode.html>.

### Eligibility and selection

```text
eligible configuration =
    first-audio latency within target
    AND UI event delay within target
    AND no normal-playback underruns
    AND memory within budget
    AND output contract and quality checks pass

Auto =
    highest median throughput among eligible configurations,
    with efficiency as the tie-breaker

Performance =
    highest throughput eligible configuration

Efficiency =
    lowest measured CPU-seconds or joules per audio minute
    while RTF remains below 1
```

Shipped profiles are keyed by broad hardware class and physical-core count,
not exact marketing model. A short local calibration may refine them, but it
must run only with user intent and must not delay the first frame.

## Startup policy

- Render the first QML frame before importing PyTorch, initializing CUDA, or
  performing potentially slow multimedia enumeration.
- Probe hardware and audio availability asynchronously and update the
  Settings readout when ready.
- Cache preset voice metadata parsing once per process.
- Lazy-create heavy hidden tab content, particularly the audiobook library.
- Auto mode warms the model on intent, such as valid text settling or EPUB
  import completion.
- Performance mode may preload immediately.
- Efficiency mode remains lazy.
- A changed backend or precision applies through an explicit engine
  lifecycle transition, never through two accidentally live workers.

Hardware detection must:

- preserve and require `torch.cuda.is_available()`;
- never infer usable CUDA from `nvidia-smi` alone;
- report actual torch dtype;
- defer heavy torch probing;
- resolve the real text length and workload type;
- pass an explicit backend to VieNeu instead of raw `"auto"`.

## Audiobook pipeline and cache

### Cache fingerprint

The cache key includes:

- full EPUB content hash;
- chapter text digest;
- voice or cloned-voice fingerprint;
- model and SDK version;
- sampling parameters;
- precision and backend;
- segmentation and audio-pipeline version;
- sample rate.

A hit validates WAV structure, sample rate, frame count, expected duration
range, and manifest identity. `Path.is_file()` alone is insufficient.

### Cache lifecycle

- Use a configurable LRU quota.
- Protect currently playing, queued, and rendering files.
- Show predicted and current cache storage in Settings.
- Never manage or evict user exports as cache.
- Validate free space before rendering.
- Recover or clean orphaned partial files at startup.

Float32 48 kHz mono WAV consumes approximately 691 MB per hour. The proposed
default quota is 10 percent of currently free disk space, capped at 20 GB.
New cache writes stop before they would breach a reserve of the greater of
5 GB or 5 percent of volume capacity. Release testing may revise these
defaults, and users may choose a smaller quota.

### Render quality of service

- Requested chapter outranks next-chapter pre-render.
- Next-chapter pre-render outranks render-all.
- Foreground text or cloning preempts all background chapter work at the next
  safe segment boundary.
- CPU render-all remains sequential.
- CUDA render-all may use length-bucketed microbatches under a total predicted
  audio-duration and VRAM cap.
- Import, export, state persistence, and cache validation run outside the GUI
  thread.

## UX state model

Replace one global `busy` state with job-scoped states:

```text
Idle
Preparing engine
Queued
Prebuffering
Generating and playing
Generating to file
Playback draining
Finalizing file
Paused for foreground request
Cancelling
Completed
Cancelled
Failed
```

Requirements:

- New jobs reset progress before the next frame.
- Engine loading, queue wait, synthesis, playback drain, and finalization are
  distinct.
- Cancel acknowledgement is immediate even when SDK work stops only at a
  safe boundary.
- Background audiobook work never disables foreground text controls.
- Progress is weighted by normalized text or predicted audio duration, not
  equal segment count.
- ETA uses warm rolling medians and excludes one-time engine load.
- Render-all reports completed, failed, cancelled, and remaining chapters.
- Settings show selected preset, resolved engine, precision/dtype, and a
  concise reason.
- Low-level thread and batch controls remain internal.

## Failure and recovery

| Failure | Required behavior |
|---|---|
| CUDA initialization fails in Auto | Fall back once to ONNX and explain the resolved change |
| Manual CUDA mode fails | Keep the preference, fail clearly, and offer ONNX |
| Out of memory | Stop the job, release transient buffers, preserve only recorded resumable segments |
| Disk full | Stop before additional inference, protect final artifacts, and clean or mark the partial |
| Cancel before dequeue | Emit immediate terminal cancellation |
| Cancel during segment | Acknowledge immediately, stop at the next SDK-safe boundary |
| Stale signal | Ignore by job ID |
| Book changes during render | Complete only against the immutable original target |
| Audio device disappears | Continue artifact generation and allow later resume |
| Corrupt cache | Reject the hit and rerender safely |
| App shutdown | Stop admission, terminate queued jobs, clean/finalize partials, then release the engine |
| Engine teardown hangs | Use process termination only after process isolation is introduced |

## Local telemetry

Operational metrics contain no source text, voice samples, or audio content:

- process start to first visible frame;
- submission to dequeue;
- TTFC;
- first sink write;
- audible TTFA when loopback measurement exists;
- RTF, characters per second, and chapters per hour;
- current and peak RSS;
- CUDA memory allocated and reserved;
- CPU time, utilization, and core distribution;
- artifact write throughput;
- audio-buffer occupancy and underrun count;
- event-loop heartbeat delay;
- cancellation acknowledgement and stop latency;
- backend load and switch time.

Metrics remain local. Benchmark export is an explicit user or developer
action.

## Acceptance budgets

Budgets apply to named reference hardware. Unsupported or highly variable
devices report results without claiming universal compliance.

| Metric | Target |
|---|---|
| Warm first visible frame | p95 at or below 500 ms on reference systems |
| UI acknowledgement | click or cancel reflected within 50 ms |
| Warm short-text TTFC | p95 at or below 300 ms on reference CPUs |
| Warm first sink write | p95 at or below 350 ms |
| Audible first audio | p95 at or below 500 ms on built-in or wired output |
| Cold first synthesis | at or below the existing 15 s SSD target |
| Real-time capability | RTF below 1 on minimum supported CPU |
| Auto throughput | no more than 10 percent median regression from the best eligible profile |
| Event loop | p95 at or below 16.7 ms, p99 at or below 33 ms, no normal app stall above 100 ms |
| Playback | zero underruns under standard cadence |
| Prebuffer | 100 to 200 ms, tuned per platform |
| Live PCM | at most two seconds |
| CPU-int8 RSS | normal target at or below 1.5 GB, hard limit below 2 GB |
| Transport overhead | at most 64 MB above loaded-engine baseline regardless of duration |
| Foreground queue delay | at most one safe background segment |
| Cancellation acknowledgement | below 100 ms |
| Cache identity | no reuse across output-affecting input changes |
| Disk management | preserve the configured free-space reserve and stop safely before exhausting it |
| CUDA | remain below configured VRAM headroom and shrink batch before OOM |

Power acceptance uses CPU-seconds per audio minute everywhere and joules per
audio minute where supported. Efficiency mode must preserve RTF below 1.

## Benchmark strategy

### Per-change deterministic suite

Run serially with fake engine and sink:

- timestamp submit, dequeue, first chunk, feed, drain, finalization, and
  cancellation;
- simulate a fast producer and slow consumer;
- assert transport high-water marks and terminal-event uniqueness;
- simulate underrun, no device, device loss, disk full, cache corruption,
  stale completion, switch-book races, and cancel-before-dequeue;
- assert main-thread heartbeat while importing, exporting, and finalizing;
- store machine-readable result JSON.

### Nightly real-model suite

Use stable self-hosted runners where possible.

Corpus dimensions:

- 20, 50, 256, 512, 2,000, 5,000, and 60,000 characters;
- Vietnamese, English, code-switching, numbers, emotion cues, multiline, and
  punctuation-free text;
- multiple regional preset voices;
- one deterministic cloned-voice fixture.

Execution:

- five process-cold runs;
- one warmup followed by twenty warm runs;
- direct engine and production controller/worker/audio paths;
- ONNX thread sweep on CPU hardware;
- batch sweep on CUDA hardware.

Report median, p90, p95, median absolute deviation, raw samples, environment,
and corpus hash.

### Weekly stress suite

- full 60,000-character audiobook chapter;
- 200,000-character imported document;
- repeated short and long alternation to expose arena growth;
- playback plus pre-render plus urgent foreground synthesis;
- constrained memory and low-disk scenarios;
- crash or injected failure at every artifact commit boundary;
- CUDA batch 1, 2, 4, 8, and 16 with VRAM and quality checks.

### Release hardware lab

Minimum matrix:

- Apple Silicon macOS;
- Intel macOS;
- Windows Intel;
- Windows AMD;
- Ubuntu x64;
- supported NVIDIA on Windows or Ubuntu;
- built-in or wired audio;
- Bluetooth audio;
- no output device.

Record OS/build, dependency lock, CPU/GPU, physical and logical cores, RAM,
storage, power mode, thermal state, commit SHA, and model/corpus hashes.

## Staged delivery

### Stage 1, reproducible baseline

Add versioned production-path benchmark harnesses and telemetry. Re-measure
all current claims and correct documentation language.

### Stage 2, correctness foundation

Add job IDs, immutable ownership, one terminal event, targeted cancellation,
explicit backend resolution, and deferred hardware/audio probing.

### Stage 3, bounded artifact and audio pipeline

Stream to atomic artifacts, bound playback transport, monitor real sink
states, distinguish synthesis from drain completion, and fix export-only
behavior.

### Stage 4, scheduler and responsive I/O

Add priority and safe segment yielding. Move EPUB, cache, export, and
persistence work off the GUI thread.

### Stage 5, adaptive presets and cache lifecycle

Ship Auto, Performance, and Efficiency; apply benchmark-derived ONNX profiles;
add cache fingerprints, integrity validation, quota, LRU, and intent-driven
warmup.

### Stage 6, CUDA bulk acceleration and hardening

Add true microbatch requests, VRAM-aware adaptation, and the cross-platform
release matrix. Add process isolation only if measurements show that
single-process backend switching, hard cancellation, or memory reclamation
cannot meet the accepted budgets.

Estimated engineering investment:

- two to three engineering weeks through bounded scheduling;
- four to six engineering weeks for the full CPU/CUDA and cross-platform
  program, plus hardware-lab availability.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Incremental artifact playback behaves differently across OSes | Establish a contract test on all three platforms before migration |
| Preemption changes pauses or prosody | Preserve segment boundary metadata and compare real output continuity |
| CUDA batching changes sampled output | Treat output as quality-equivalent, not bit-identical; use bounded quality checks |
| Backend switching costs exceed savings | Require measured break-even prediction and idle hysteresis |
| Automatic tuning becomes unstable | Change plans only between jobs and use broad shipped profiles |
| Cache fingerprint invalidates old audio | Treat legacy cache as a versioned compatibility case and rerender explicitly |
| I/O pool contends with inference | Keep it small, lower priority, and benchmark event-loop and audio stability |
| Process isolation increases complexity | Defer it until objective escalation conditions are met |

## Escalation conditions

Introduce a dedicated inference subprocess only if at least one condition is
verified after Stages 1 through 5:

1. ONNX arena or retired-engine memory cannot return below the hard budget.
2. Backend switching cannot reliably release RAM or VRAM.
3. Hard cancellation is a product requirement that cooperative SDK
   cancellation cannot meet.
4. A hung native inference call can prevent clean application shutdown.

Consider simultaneous ONNX and CUDA engines only for a separate workstation
profile if measured backend-switch delay causes foreground latency to fail and
the target machine has enough RAM and VRAM for both with explicit user opt-in.

## Testing requirements

All implementation follows repository TDD:

1. add a failing observable-contract test;
2. implement the smallest passing behavior;
3. refactor;
4. run focused tests;
5. run `ruff check .`, `ruff format --check .`, and `pytest`;
6. record real-model evidence separately from deterministic tests.

Required new coverage includes:

- controller and listener races;
- two submissions and stale completion;
- cancel before dequeue and during every stage;
- priority and preemption order;
- duration-independent memory transport;
- sink prebuffer, state, underrun, and drain behavior;
- export-only generation;
- growing artifact validity and cross-platform access;
- cache fingerprint and corrupt-cache recovery;
- disk-full and crash consistency;
- asynchronous startup probes;
- actual engine constructor arguments for every preset and workload;
- CUDA batch order, limits, fallback, and per-item failure;
- real audio continuity across preserved segment boundaries.

## Definition of done

The optimization program is complete when:

1. production-path benchmarks are reproducible from the repository;
2. documentation accurately labels cold, warm, TTFC, sink-write, and audible
   measurements;
3. output transport memory is duration-independent and below its budget;
4. all jobs are identified, prioritized, targeted, and terminally resolved;
5. foreground synthesis can interrupt background audiobook work safely;
6. Auto, Performance, and Efficiency meet their defined selection rules;
7. ONNX thread profiles are validated across reference CPU classes;
8. CUDA microbatching is validated on supported NVIDIA systems;
9. cache results cannot cross output-affecting configuration changes;
10. startup, playback, memory, cancellation, UI, and power budgets pass on the
    release hardware matrix;
11. repository quality gates are green;
12. no network or content-bearing telemetry was introduced.
