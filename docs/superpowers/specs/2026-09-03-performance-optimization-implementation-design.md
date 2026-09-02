# Performance, model delivery, and UX optimization, implementation design

**Date:** 2026-09-03
**Status:** approved in chat, pending review of this persisted specification
**Scope:** model onboarding, job scheduling, duration-independent synthesis,
playback, audiobook cache lifecycle, startup, adaptive CPU settings, and
targeted UX corrections
**Supersedes operationally:** `2026-08-28-adaptive-performance-resource-optimization-design.md`

## 1. Purpose

VieNeuTTS already provides fast warm CPU synthesis and keeps inference off the
main thread. This program turns that foundation into a reliable desktop
experience on a clean machine, for long documents, and while audiobook
background work is active.

The implementation has four primary outcomes:

1. A packaged user can obtain and validate the official model without a
   terminal, then synthesize fully offline.
2. Normal synthesis memory and live playback memory do not grow with document
   duration.
3. Every job is identifiable, ordered by user intent, cancellable without
   affecting unrelated work, and terminally resolved exactly once.
4. Audiobook cache hits are correct, storage is bounded, and UI status reflects
   real work rather than optimistic guesses.

## 2. Measured baseline

Measurements were collected on an Apple M4-class host using the CPU ONNX int8
path. They are reference evidence, not cross-platform guarantees.

| Measurement | Result |
| --- | ---: |
| Full-process first frame, audio probe stubbed | 486 ms median |
| Full-process first frame, production audio probe | 542 ms median |
| Warm direct TTFC, 50 / 512 chars | 72 / 117 ms |
| Warm pipeline first sink pull, 50 / 512 chars | 101 / 166 ms |
| Process-cold first sink pull | 5.85-6.55 s median |
| Warm-page-cache model initialization | 1.29-1.48 s |
| Warm pipeline event-loop p95 | 4.0-4.7 ms |
| Real-scenario peak RSS | 0.87-1.22 GiB |

The current frozen bundle is approximately 867 MB before model weights. The
minimal official int8 model set is approximately 327 MB, so embedding it in
every application update would make a roughly 1.2 GB installer.

The warm initialization profile shows where lazy optional sessions can help:

| Session | Initialization time |
| --- | ---: |
| `vieneu_prefill.onnx` | 483 ms |
| `vieneu_decode_step.onnx` | 205 ms |
| `vieneu_acoustic_cached.onnx` | 17 ms |
| full codec decoder | 294 ms |
| streaming codec decoder | 206 ms |
| denoiser | 50 ms |

The UI uses streaming synthesis for normal text and paragraph flows. The full
codec decoder and denoiser are therefore candidates for lazy upstream loading,
after compatibility and benchmark validation.

## 3. Decisions

### 3.1 Model delivery

Use an **in-app downloader** as the primary baseline-model delivery path.

- The initial application download stays smaller and application updates do not
  re-download unchanged model data.
- The official model set is revision-pinned and allowlisted by a committed
  manifest with SHA-256 values, expected sizes, and a model-format version.
- The downloader obtains only the official baseline files into app-managed
  storage. It never treats a mutable branch head as a release model.
- Installation uses a staging directory. A model becomes active only when the
  complete manifest validates.
- After a valid install, the official path runs offline and does not silently
  contact Hugging Face during synthesis.
- A later optional offline model pack calls the same installation and
  validation API. It is not a second cache format.
- Custom model repositories remain an explicit advanced setting. They are not
  silently fetched by the baseline setup flow.

This changes product wording from "offline immediately after application
install" to "offline after one-time model setup." Inference and user content
remain on-device. Model redistribution licensing remains a release gate.

### 3.2 One active engine

Keep one active `TTSEngine` and one inference worker. Multiple CPU engines
would duplicate model memory and compete with ONNX Runtime's internal thread
pools.

Model inference, model lifecycle, voice mutation, and denoise remain
serialized. Small bounded background operations may run concurrently through
the established `ui.bg_ops` pattern.

### 3.3 Artifact-first synthesis

Normal UI jobs write generated audio incrementally to a per-job atomic WAV
artifact. They do not return a duration-sized `numpy.ndarray` to the
controller.

```text
QML / controllers
        |
        v
SynthesisScheduler → one InferenceWorker → IncrementalArtifactWriter
                            |                       |
                            |                       +→ <job>.part.wav
                            |
                            +→ bounded PCM transport → QAudioSink
```

The worker validates each chunk as contiguous mono `float32` at 48 kHz,
appends it to the artifact, advances small metadata, then releases the chunk.

On success it finalizes the WAV header, validates the result, atomically
renames it to the final artifact, and emits metadata:

```python
@dataclass(frozen=True)
class SynthesisArtifact:
    job_id: str
    path: Path
    sample_rate: int
    samples: int
    duration_ms: int
    timeline_path: Path | None
    envelope_path: Path | None
```

A clearly bounded compatibility path may retain short samples in memory only
when an existing public API requires it. It must have an explicit byte limit.

### 3.4 Bounded live playback

The audio transport owns at most two seconds of PCM, 384,000 bytes at 48 kHz
mono float32. It begins only after 100-200 ms of committed audio is available,
then supplies a `QAudioSink` according to available space.

The worker must not create an unbounded Qt queued-signal backlog when
generation is faster than playback. The bounded buffer is thread-safe; the
producer waits cooperatively for capacity and responds to cancellation.

The UI receives coalesced metadata only:

- progress and waveform updates at most 20-30 Hz;
- waveform values paced by audio consumption, not synthesis speed;
- explicit distinction between generating, draining playback, and finalizing.

When no device is available, synthesis still creates an artifact and the UI
uses export-only mode. A device loss does not discard completed generation.

### 3.5 Job model and scheduler

Replace anonymous queue entries and global busy state with immutable jobs:

```python
@dataclass(frozen=True)
class SynthesisJob:
    id: str
    owner: Literal["text", "paragraph", "audiobook", "cloning"]
    kind: Literal["interactive", "requested_chapter", "prefetch", "bulk", "voice_op"]
    priority: int
    request: TTSRequest | VoiceOp | WarmupOp
    artifact_path: Path | None
    cache_fingerprint: str | None
```

Every admitted job emits exactly one terminal state:
`completed`, `cancelled`, `failed`, or `superseded`.

Priority is stable FIFO within each class:

1. foreground text, paragraph, and cloning actions;
2. an audiobook chapter the user explicitly requested;
3. next-chapter pre-render;
4. render-all and other bulk work.

Cancellation targets a job or owner. Queue-wide cancellation is reserved for
shutdown. A cancel-before-dequeue event is terminal immediately. Background
work yields only at an already-safe text-segment boundary, preserving completed
artifact data and avoiding mid-segment output changes.

The QML job state model is:

```text
Idle → Preparing engine → Queued → Prebuffering
    → Generating and playing | Generating to file
    → Playback draining → Finalizing → Completed
    ↘ Cancelling → Cancelled
    ↘ Failed
```

Controllers discard stale events by job ID and may have at most one active
foreground job unless explicitly designed to queue.

### 3.6 Audiobook artifact identity and lifecycle

Each audiobook cache artifact has a manifest. A valid hit requires both an
identity match and structural validation.

Identity includes:

- full EPUB content hash and chapter-text digest;
- voice or cloned-voice fingerprint;
- model revision and SDK version;
- sampling parameters;
- selected backend and precision;
- segmentation and audio-pipeline versions;
- sample rate.

Validation checks the manifest, WAV readability, sample rate, frame count, and
a reasonable duration range. `Path.is_file()` alone is never a cache hit.

Cache lifecycle requirements:

- configurable LRU quota;
- protect playing, queued, and rendering artifacts from eviction;
- calculate a free-space reserve before a render begins;
- do not manage user exports;
- recover or clean orphaned partials at startup;
- surface cache size and quota in Settings.

The initial quota is 10% of available disk space, capped at 20 GB. Writes stop
before crossing the greater of 5 GB or 5% of volume capacity. These defaults
remain benchmark and release-test inputs, not immutable policy.

### 3.7 Startup and adaptive resource policy

The first frame must precede expensive audio enumeration, hardware probing,
model initialization, CUDA checks, and model downloading.

- Audio availability becomes an asynchronous tri-state result, not a property
  getter that probes during QML construction.
- The engine card shows checking, model unavailable, downloading, validating,
  initializing, ready, or failed. It never reports ready before the state is
  known.
- Auto warmup follows a useful intent, such as stable valid text or an EPUB
  import. Performance may preload after first paint; Efficiency stays lazy.
- Performance profiles expose only `Auto`, `Performance`, and `Efficiency`,
  never raw thread counts.

Apple M4-class seed profiles are:

| Profile | Candidate threads | Reason |
| --- | ---: | --- |
| Performance | 4 | fastest measured throughput and TTFC |
| Auto | 2 | about 4% slower than 4, with about 26% less CPU time |
| Efficiency | 1 | real-time capable, with about 44% less CPU time than 4 |

Profiles are keyed by broad hardware class and physical-core count. A local
calibration is optional, explicit, and never delays the first frame.

Offline ONNX/ORT artifact conversion and lazy optional SDK sessions are
separate experiments. They must pass output, initialization, RSS, and
cross-platform compatibility tests before use. No `vieneu` version change,
inference subprocess, dual engine residency, or CUDA microbatching is part of
the initial CPU implementation.

### 3.8 UX corrections

- The first-run model screen shows clear model size, free-space requirement,
  progress, cancellation, failure reason, retry, and offline guidance.
- “Retry” performs a real state refresh; it must not merely hide an error.
- Removing an audiobook requires confirmation that its generated chapter audio
  is removed permanently.
- Compact icon-only navigation gets sighted-user tooltips.
- Long-text word and duration metrics are computed once and debounced instead
  of repeatedly running full regular expressions for each keystroke.
- Audiobook chapter and bulk export copy work runs off the GUI thread and
  reuses in-memory book metadata rather than re-parsing `book.json` per file.
- Existing nested scrolling is validated with real wheel/touch scenarios;
  changes are limited to proven handoff problems.

## 4. Failure handling

| Situation | Required behavior |
| --- | --- |
| no network at setup | explain that setup needs a connection or offline pack; retain no invalid active model |
| interrupted download | preserve safe resumable staging/cache data, report state, allow retry |
| corrupt model file | reject it, show repair/retry, never initialize against it |
| low disk space | block before download/render, show required and available space |
| cancel before dequeue | immediate job-specific terminal cancellation |
| cancel during generation | immediate UI acknowledgement, stop at next SDK-safe boundary |
| stale worker signal | drop by job ID and owner |
| disk full during artifact write | terminate safely, preserve/clean the partial according to manifest policy |
| book switches mid-render | only commit against the original immutable book/chapter target |
| cache corruption | reject and rerender safely |
| audio device loss | continue artifact generation and allow future playback |
| model init failure | show failure state, retain actionable diagnostic, do not claim ready |

## 5. Acceptance criteria

### Product and correctness

1. A clean profile can install the official baseline model through the UI,
   restart the app, and synthesize with network access disabled.
2. No normal packaged user sees a command requiring the repository or Python.
3. Every accepted job receives exactly one terminal result.
4. Cancelling an unstarted job resolves it promptly without leaving a
   controller busy or cancelling an unrelated job.
5. Cache hits never cross an output-affecting configuration change.
6. Book deletion is explicitly confirmed before generated files are removed.

### Performance

| Metric | Target |
| --- | --- |
| warm first visible frame | p95 at or below 500 ms on reference systems |
| UI acknowledgement | visible within 50 ms |
| warm short-text TTFC | p95 at or below 300 ms on reference CPUs |
| warm first sink write | p95 at or below 350 ms |
| audible first audio | p95 at or below 500 ms on built-in/wired output |
| cold first synthesis | at or below 15 s on reference SSD |
| real-time capability | RTF below 1 on minimum supported CPU |
| event loop | p95 at or below 16.7 ms, p99 at or below 33 ms, no normal stall above 100 ms |
| playback | zero underruns in the standard cadence matrix |
| prebuffer | 100-200 ms, tuned per platform |
| live PCM | at most two seconds |
| CPU int8 RSS | normal at or below 1.5 GB, hard limit below 2 GB |
| transport overhead | at most 64 MB above loaded-engine baseline, independent of duration |
| foreground queue delay | at most one safe background segment |
| cancellation acknowledgement | below 100 ms |

### Test and benchmark requirements

Deterministic tests use fake engines and sinks to cover:

- manifest validation, staging, retry, cancellation, and clean-profile model
  setup;
- unique terminal results, stale signals, priority order, cancellation before
  dequeue, and safe background yielding;
- artifact header finalization, atomic promotion, failure cleanup, disk-full
  behavior, and duration-independent buffer limits;
- no device, device loss, prebuffering, backpressure, drain completion, and
  waveform pacing;
- cache fingerprint mismatch, corrupt cache rejection, LRU protection, free
  space reserve, and book-switch races;
- async audio probing, accurate engine states, delete confirmation, tooltips,
  debounced metrics, and background export.

Real-model benchmarks record separate process-cold, page-cache-warm,
model-warm, direct-engine, and production-pipeline figures. They store corpus
hashes and machine characteristics but never source text, voice samples,
generated audio, credentials, or personal paths.

## 6. Delivery boundaries

The implementation is sequenced as:

1. reproducible benchmark corrections and model-manager foundation;
2. job identity, terminal events, targeted cancellation, and truthful status;
3. artifact-first generation and bounded playback;
4. priority scheduling and audiobook cache/export lifecycle;
5. startup/resource presets and targeted UX polish;
6. platform-specific release hardening and optional upstream optimization
   experiments.

The following remain outside the initial implementation:

- multiple simultaneous model workers;
- dual ONNX/CUDA engines in memory;
- GPU support beyond NVIDIA CUDA;
- unvalidated CUDA batching changes;
- cloud telemetry or user-content upload;
- automatic model downloading from arbitrary custom repositories;
- a dedicated inference subprocess.

Introduce a separate inference process only if post-implementation
measurements prove that the one-process worker cannot meet hard memory,
shutdown, backend-switch, or cancellation requirements.

## 7. Existing work reconciliation

This design incorporates rather than duplicates the open issues:

- `VieNeuTTSApp-2ab`: frozen-build developer-only model guidance;
- `VieNeuTTSApp-75v`: very-long synthesis artifact spill;
- `VieNeuTTSApp-n23`: job IDs and stale worker deliveries;
- `VieNeuTTSApp-1v6`: evidence-driven ONNX thread tuning.

`VieNeuTTSApp-d2j` is adjacent reader accessibility work. It may proceed
independently but is not required by this performance architecture.
