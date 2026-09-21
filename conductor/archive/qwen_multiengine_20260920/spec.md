# Track: Qwen Multilingual Multi-Engine TTS Support

**Track ID:** `qwen_multiengine_20260920`
**Type:** Feature · **Priority:** High · **Status:** new

User brief: add the official `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice`
model as another engine because VieNeu is Vietnamese-first, then include the
matching 0.6B Base model for multilingual voice cloning.

## Overview

Add two optional Qwen engine profiles to the existing offline desktop
workflows:

1. `VieNeu` — remains the default, optimized for Vietnamese and existing
   Vietnamese/English code-switching.
2. `Qwen CustomVoice 0.6B` — 10 supported languages and 9 fixed speakers.
3. `Qwen Base 0.6B` — the same multilingual language set with user-created
   voice clones.

The active profile is explicit and global. The app never guesses an engine
from text. Exactly one model owner may be active at a time.

Qwen runs in a dedicated local model-host subprocess backed by an isolated,
app-managed runtime. Existing VieNeu inference remains in the current worker
thread. Both providers feed the same tagged job, artifact, playback, export,
batch, audiobook, subtitle, and Audio Studio contracts.

## Research Summary

- A completed but unmerged prior implementation exists on
  `origin/feature/qwen-support` (`279574d` through `f154725`) with archived
  track `qwen-multiengine_20260908`. It includes capability contracts,
  backend adapters, stateful resampling, CJK segmentation, cache identity,
  Qwen voice storage, setup UI, and extensive fake/opt-in tests.
- That branch split from main at `a52d12f` and predates the current Studio,
  Subtitle, managed-CUDA, Settings, and consolidated-test architecture. It
  also assumes same-process dependencies, exposes ineffective 0.6B
  instructions, and provides setup guidance rather than a fully managed
  cross-platform runtime. Treat it as a reference/salvage source, never a
  wholesale merge or blind cherry-pick.
- The official CustomVoice model card lists Chinese, English, Japanese, Korean,
  German, French, Russian, Portuguese, Spanish, and Italian, plus nine fixed
  speakers. Vietnamese is not supported, so VieNeu remains the Vietnamese
  default: <https://huggingface.co/Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice>.
- The official `qwen-tts` package exposes `generate_custom_voice` and
  `generate_voice_clone`; generation returns `list[np.ndarray], sample_rate`.
  The 0.6B CustomVoice implementation ignores `instruct`, so the product must
  not expose a style control that has no effect:
  <https://github.com/QwenLM/Qwen3-TTS>.
- `non_streaming_mode=False` simulates streaming text input but does not return
  incremental audio from the public wrapper. App-level bounded segmentation is
  therefore required for progress, memory bounds, and artifact-first output.
- The current app pins `transformers==4.57.6`; `qwen-tts==0.1.1` pins
  `transformers==4.57.3` and recommends a fresh isolated environment.
  A subprocess runtime avoids import/native-library contamination and contains
  model-host crashes.
- Official examples are CUDA-first. CPU, Apple MPS, and NVIDIA CUDA support must
  be proven and pinned by a platform spike before integration proceeds.

## Architecture and Data Flow

- Phase 0 produces a commit/file-level salvage matrix for the prior branch.
  Compatible pure contracts and tests are ported deliberately; stale runtime,
  controller, QML, and packaging assumptions are replaced by this spec.
- Introduce an engine capability descriptor covering:
  - engine/profile identity and pinned model revision
  - languages and speakers
  - cloning support and clone requirements
  - supported generation controls
  - source/output sample rates
  - device/runtime availability
  - streaming granularity
- Keep compute device/backend separate from model profile:
  - VieNeu: existing ONNX CPU or managed NVIDIA CUDA behavior
  - Qwen: CPU, Apple MPS, or NVIDIA CUDA
- The Qwen model host:
  - runs from a checksum-pinned isolated runtime
  - accepts length-framed, job-ID-tagged commands over local pipes
  - writes logs only to stderr
  - returns float32 PCM frames and progress through the framed protocol
  - loads only one of CustomVoice or Base
  - never evaluates remote code or pickle model artifacts
- For Qwen synthesis, the app:
  - splits long text into bounded sentence-aware segments
  - generates one segment at a time
  - resamples 24 kHz Qwen output to 48 kHz through one stateful stream
  - appends 48 kHz chunks to the existing incremental artifact writer and
    optional bounded live-preview transport
- Cancellation:
  - queued jobs retain current immediate cancellation semantics
  - active Qwen cancellation requests graceful stop first
  - if generation cannot stop promptly, terminate the model host, discard the
    partial artifact, and lazily restart the host for the next job
- Profile switching:
  - is blocked while synthesis jobs are active or queued
  - shuts down the current owner before loading another
  - never keeps VieNeu and Qwen models resident together

## Functional Requirements

### Engine Selection and Capabilities

- Persist one global active profile: VieNeu, Qwen CustomVoice, or Qwen Base.
- VieNeu remains the default after fresh install and settings migration.
- Surface the active profile and resolved device truthfully.
- Every submitted job snapshots profile, model revision, language,
  speaker/clone, and generation parameters.
- Unsupported controls are hidden or disabled with a reason; values are never
  silently ignored.
- Automatic language routing, mixed per-paragraph profiles, and concurrent
  model residency are not supported.

### Qwen CustomVoice

- Support the official
  `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice` checkpoint.
- Present the model-reported 10-language list, including `Auto`.
- Present the model-reported 9 fixed speakers.
- Do not expose instruction/style prompting because the 0.6B implementation
  ignores `instruct`.
- Reject unsupported language/speaker combinations before job admission.

### Qwen Base and Voice Cloning

- Support the official `Qwen/Qwen3-TTS-12Hz-0.6B-Base` checkpoint.
- Adapt the existing Cloning workflow for engine capabilities.
- Qwen clone enrollment requires a validated local reference clip, a reference
  transcript, and the existing explicit consent acknowledgement.
- Persist app-owned clone metadata, a copied reference artifact, transcript,
  and stable clone ID; do not persist Python pickles or opaque torch objects.
- Rebuild reusable Qwen clone prompts inside the model host after load.
- Keep VieNeu and Qwen clone catalogs separate and show only compatible clones
  for the active profile.
- Removing a clone removes its app-owned metadata/reference copy after
  confirmation.

### Existing Workflows

- Text, Paragraph, Batch, Audiobook, and Subtitle synthesis use the active
  profile and capability-driven language/voice controls.
- Audio Studio accepts artifacts from every profile without changing its
  profile-independent editing/export operations.
- Studio clip provenance records profile, model revision, language, and
  voice/clone ID. Re-synthesis requires the matching active profile and must
  never substitute another engine silently.
- Audiobook, subtitle, and other synthesis cache fingerprints include engine
  profile, model revision, language, voice/clone, and relevant generation
  settings, preventing cross-engine cache reuse.
- Existing exports remain 48 kHz WAV/MP3 regardless of source engine.

### Managed Runtime and Models

- Add an optional Qwen runtime manager for supported release platforms:
  - Windows x64: CPU and NVIDIA CUDA
  - Linux x64: CPU and NVIDIA CUDA
  - macOS Apple Silicon: CPU and MPS
- Pin every runtime wheel by URL, size, and SHA-256.
- Resolve the Qwen/VieNeu dependency conflict through process isolation rather
  than changing the base app's loaded Transformers package.
- Add pinned model manifests for CustomVoice, Base, and shared tokenizer files.
- Support download, resume, cancel, integrity verification, free-space
  preflight, staging-only extraction, atomic promotion, repair, removal, and
  offline-pack import.
- The application remains fully offline after the selected runtime/models are
  installed.
- FlashAttention 2 is an optional CUDA optimization only; SDPA/eager fallback
  must work without it.
- Settings report unsupported hardware, missing runtime, missing model,
  corruption, required storage, installed versions, and actionable recovery.

## Non-Functional Requirements

- The GUI thread never loads models, imports heavyweight Qwen/PyTorch modules,
  performs inference, hashes large downloads, or blocks on subprocess I/O.
- CPU, MPS, and CUDA execution must be validated on their target release
  platforms. CPU may be slow, but the UI remains responsive and communicates
  that limitation before download/use.
- Qwen generation memory is bounded by one text segment plus model/runtime
  state; whole documents are not accumulated in RAM.
- Crashes, malformed frames, protocol-version mismatches, unexpected exits,
  out-of-memory errors, and device failures become tagged actionable errors;
  they do not crash the GUI process or commit partial artifacts.
- Startup remains model-free and does not start the Qwen host until Qwen is
  selected and first needed.
- Existing settings migrate without losing voices, model locations, export
  preferences, or VieNeu behavior.
- New Python core logic maintains at least 80% line coverage.
- Vietnamese and English UI catalogs cover every new user-facing string.

## Acceptance Criteria

1. A fresh or upgraded install defaults to VieNeu with existing behavior
   unchanged.
2. Users can install, repair, and remove the optional Qwen runtime and either
   Qwen model through Settings, including verified offline-pack import.
3. Qwen CustomVoice synthesizes with every reported language and fixed speaker
   accepted by the model API; unsupported values are rejected before launch.
4. Qwen Base can enroll, persist across restart, use, and remove a clone from a
   reference clip plus transcript.
5. Text, Paragraph/Batch, Audiobook, and Subtitle jobs produce playable,
   exportable 48 kHz artifacts through both Qwen profiles.
6. Audio Studio can edit/export Qwen artifacts and preserves truthful
   provenance for any re-synthesis action.
7. Profile switching never leaves two models loaded and cannot corrupt or
   misroute active/queued jobs.
8. Cancelling Qwen generation yields one cancelled terminal, removes partial
   artifacts, and leaves the next job usable even if the host had to restart.
9. Cache fingerprints prevent artifacts from one engine/profile/model revision
   from being reused by another.
10. Runtime/model absence, corruption, unsupported device, subprocess crash,
    OOM, and disk-full paths show actionable localized errors.
11. Real-model smoke validation passes on Windows CPU/CUDA, Linux CPU/CUDA,
    and Apple Silicon CPU/MPS; normal CI uses deterministic fake-host tests and
    requires no model download.
12. `ruff check .`, `ruff format --check .`, and the full `pytest` gate pass,
    excluding only the documented host-specific audio-device failure.

## Out of Scope

- Qwen 1.7B checkpoints
- VoiceDesign and free-form style instructions
- GGUF or third-party quantized runtimes
- Cloud inference or hosted APIs
- Fine-tuning
- Automatic language-to-engine routing
- Per-paragraph/per-cue engine mixing
- Concurrent resident VieNeu and Qwen models
- Redesigning Audio Studio's engine-independent effects
