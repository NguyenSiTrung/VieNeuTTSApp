# Qwen 0.6B GGUF engine support

## Overview

Add Serveurperso's GGUF conversions of Qwen3-TTS 12Hz 0.6B Base and
CustomVoice through an isolated, app-managed `qwentts.cpp` runtime. Users
choose the model profile first, then its format, an optional GGUF
quantization, the compatible engine, and a supported compute device.

The existing official-weight PyTorch path remains available and unchanged.
This track adds a second implementation of the same Qwen model profiles,
not four more top-level engine profiles.

**Approved decisions (2026-09-23):**

- Target CPU, NVIDIA CUDA, and Apple Metal. Exclude Vulkan.
- Label the existing option **Official full weights**, not FP16.
  Preserve `host_precision`: CPU/MPS FP32 and CUDA BF16.
- Offer **GGUF Q8_0** and **GGUF Q4_K_M** for both 0.6B profiles.
- Use an isolated managed host, not native inference in the GUI process.
- Execute the implementation plan sequentially.
- No per-phase manual verification or approval gates. Use automated task
  gates and final scripted real-model release checks.
- Medium priority; no prerequisite track; no time estimate.

## Current integration points

- `core/engine_profiles.py` defines the existing semantic profiles,
  languages, nine CustomVoice speakers, and Base enrollment requirements.
- `core/synthesis_context.py` carries immutable job/cache/Studio identity.
- `core/qwen_engine.py`, `core/qwen_protocol.py`, and
  `workers/qwen_host.py` implement the isolated official-weight path.
- `core/qwen_runtime.py`, `core/qwen_model_manager.py`, and
  `core/managed_install.py` provide verified install patterns.
- `ui/controller.py`, `EngineState.qml`, and `EngineProfilePicker.qml`
  provide global selection and readiness across synthesis surfaces.
- `core/voice_profiles.py` already persists source audio, transcript, and
  consent rather than opaque engine objects.

## Functional requirements

### FR-1: Model-first selection

1. Preserve the existing IDs `vieneu`, `qwen_base_0_6b`, and
   `qwen_custom_0_6b`.
2. For either Qwen profile, offer **Official full weights** and **GGUF**.
3. GGUF exposes exactly `Q8_0` and `Q4_K_M`. Default to `Q8_0` the first
   time GGUF is selected; remember the user's subsequent selection.
4. Show a compatible-engine selector/readout: official weights resolve to
   **PyTorch**, GGUF resolves to **qwentts.cpp**. With only one compatible
   engine, show it as the selected, non-editable choice. Never permit
   PyTorch+GGUF or qwentts.cpp+official weights.
5. Device choices depend on the engine, platform, installed runtime, and
   hardware. Display **Metal** for the native backend, not PyTorch's
   **MPS**. Auto chooses only a validated available device in the selected
   format; it never changes profile, format, or quantization.
6. Preserve current official-weight precision and device behavior. Display
   actual precision where diagnostics expose it; do not promise FP16.
7. Refuse selection changes while synthesis is running, queued, or
   cancelling. Async checks/install completions cannot publish readiness
   for an obsolete selection. An install targets the variant captured
   when it began, not whichever option is selected when it finishes.
8. Migrate old Qwen settings to official weights without changing voices,
   devices, or unrelated preferences. Fresh installs still default to
   VieNeu. Persist format, GGUF quantization, and separate engine-device
   preferences; corrupt new fields clamp individually.

### FR-2: Model and runtime installation

1. Support these talkers from `Serveurperso/Qwen3-TTS-GGUF`:
   - `qwen-talker-0.6b-base-Q8_0.gguf`
   - `qwen-talker-0.6b-base-Q4_K_M.gguf`
   - `qwen-talker-0.6b-customvoice-Q8_0.gguf`
   - `qwen-talker-0.6b-customvoice-Q4_K_M.gguf`
2. Pair each talker with the same-quantization
   `qwen-tokenizer-12hz-{Q8_0|Q4_K_M}.gguf`. Do not expose mixed talker/codec
   quantization in this track. Share a tokenizer across Base/CustomVoice
   only when revision, size, and digest identify the same file.
3. Pin the repository revision, exact filenames, byte sizes, SHA-256
   digests, and compatible native runtime build. Never install from a
   moving `main`/`HEAD` URL at runtime.
4. Distribute verified native library/backend packs separately from models
   and the frozen application. Install/download, cancel, retry, repair,
   remove, inspect storage, and import a matching offline pack.
5. Reuse staging, resume, free-space checks, verification, atomic promotion,
   rollback, Windows path/lock handling, and in-use removal guards.
   Reject traversal, escaping links, incompatible pack identities, and
   incomplete or corrupt packs before readiness becomes true.
6. Keep official and GGUF installs independently usable. Removing one
   variant cannot remove another variant's files, a referenced shared
   tokenizer, or enrolled clone recordings.
7. GGUF requires no PyTorch runtime, pip invocation, user compiler, Docker,
   arbitrary executable path, or manually discovered Python environment.
   Maintainers build packs; the application only installs verified packs.

### FR-3: Isolated native inference

1. Use a thin app-owned host process binding the pinned qwentts.cpp C ABI.
   In source mode it uses the app interpreter; frozen builds re-dispatch
   the application through `--qwen-gguf-host`. Native libraries load only
   in that child, from the verified runtime directory.
2. Reuse the framed, bounded, job-tagged IPC contract and parent lifecycle
   discipline. Negotiate native capabilities explicitly; do not pretend
   Metal is a PyTorch MPS device or advertise native batch support without
   evidence. Unsupported protocol/build combinations fail actionably.
3. Preserve exactly one resident model owner across VieNeu, official Qwen,
   and GGUF. Reuse a loaded native context across bounded segments rather
   than starting a CLI/model load for every segment.
4. Feed real native audio callbacks through a stateful, per-segment
   24 kHz-to-48 kHz mono float32 resampler into the existing artifact-first
   writer and bounded preview transport. Do not buffer the whole document.
   Keep native logs off the IPC stdout channel.
5. Keep the GUI cancellation path nonblocking. Cancel-before-pickup,
   cancellation during load/inference, late audio, host crash, and shutdown
   settle each admitted job once. Escalate cancel to terminate/kill/reap
   outside the GUI thread when the native API cannot stop promptly.
6. After cancellation or crash, the next job may lazily restart a clean
   host. Never complete or autoplay a cancelled generation.
7. Bound CPU threads, queued output, segment sizes, and reference caches;
   retain the existing job-boundary footprint-governor behavior where
   applicable without imposing PyTorch-specific knobs on GGML.
8. Explicit unsupported devices, OOM, missing native dependencies, bad
   model metadata, and ABI mismatch produce recovery guidance. No silent
   cross-format fallback, hidden model download, or runtime network access.

### FR-4: Voices and capability mapping

1. Base remains the clone profile: reference audio, matching transcript,
   and consent are required, including for GGUF.
2. CustomVoice remains the fixed-speaker profile. Map existing speaker IDs
   to native IDs explicitly, including casing (`Uncle_Fu` → `uncle_fu`).
3. Preserve the existing ten Qwen languages plus Auto as the intended
   capability set. Verify each mapping against the pinned native runtime.
   An unsupported mapping is rejected with a reason, never guessed.
   Do not add languages based solely on the current upstream README.
4. Do not add instruction/style controls for the 0.6B variants.
5. Reuse Base enrollment source recordings/transcripts across formats.
   Normalize native reference input to the ABI's required mono 24 kHz.
   Rebuild engine-specific prompts/latents; never pass PyTorch objects to
   the native runtime. Any derived cache includes source-content hash,
   transcript, model/tokenizer identity, quantization, and native build.
6. Verify Base/CustomVoice semantics against native code and real-model
   output before enabling them. The model card's mode table conflicts
   with the runtime README; filenames alone are not compatibility evidence.

### FR-5: Provenance and all synthesis surfaces

1. Snapshot profile, format, quantization, engine/runtime build identity,
   pinned model/tokenizer identity, resolved device, language, voice/clone,
   and generation settings when admitting a job.
2. Carry that identity into artifacts, audiobook/subtitle cache keys,
   batch jobs, and Studio clips. Different format/quantization/runtime
   identities cannot share a cached render.
3. Migrate old known Qwen provenance as official-weight provenance, never
   as GGUF. Unknown new schemas become cache misses; wholly unstamped
   legacy renders retain the existing VieNeu-only compatibility rule.
4. Studio displays the recorded model/engine/quantization, guards
   re-synthesis against a variant mismatch, and offers an explicit switch
   to the recorded selection. Unavailable recorded revisions/builds are
   explained, not silently replaced.
5. Cover Text, Paragraph, multi-file Batch, Audiobook, Subtitle, Audio
   Studio, voice audition, playback, and WAV/MP3 export. Native batch
   operations may serialize bounded segments in one resident host.
6. Keep selection/readiness in one Python contract and the shared
   `EngineState` derivation. Add reusable QML controls, not per-tab copies.
   Retain existing objectName contracts and Vietnamese/English localization.

## Non-functional requirements

- **Platform target:** Windows x64 CPU/CUDA, Linux x64 CPU/CUDA, and
  macOS Apple Silicon CPU/Metal, six matrix cells. Do not claim support
  until the corresponding native pack and real-model evidence pass.
- **Release gate:** both profiles × both quantizations × all six cells,
  24 primary synthesis combinations, plus lifecycle and offline checks.
  Missing release hardware remains an explicit blocker, not a passed test.
- **Security/privacy:** no inference listener port, shell-built commands,
  unverified native library discovery, user-text logging, or reference-audio
  telemetry. Native code is third-party code and must remain pinned and
  isolated. Preserve MIT/Apache-2.0 notices in redistributed packs.
- **Startup/dependencies:** no model load/native ABI import on GUI startup;
  no Qwen PyTorch stack or native model weights added to `pyproject.toml`,
  `uv.lock`, or the frozen base bundle.
- **Performance:** measure load time, first audio, real-time factor,
  current/peak RSS, and GPU memory where available. Do not copy upstream
  leaderboard claims into app guarantees. Preserve responsive cancellation,
  bounded preview transport, and single-owner operation.
- **Testing:** ordinary CI uses deterministic fake hosts and tiny
  installation fixtures, with no real weights or network. Real-model
  checks are explicit, opt-in, and use the application's own install/host
  path. Target at least 80% line coverage on changed Python logic.
  No phase requires manual user verification or approval to proceed.
- **UI:** responsive down to the existing 640×420 minimum; truthful active
  variant, storage, runtime readiness, device, and error states.

## Acceptance criteria

- **AC-1:** Both Qwen profiles offer official weights, GGUF Q8_0, and GGUF
  Q4_K_M; only compatible engines/devices can be selected.
- **AC-2:** Old settings preserve the official path and device precision;
  new settings round-trip without resetting unrelated preferences.
- **AC-3:** All four talkers and two shared tokenizers have pinned manifests,
  correct pairing, independent lifecycle, and verified offline installs.
- **AC-4:** Native runtime packs install/repair/remove safely without
  PyTorch, compilers, arbitrary executable discovery, or GUI-thread loads.
- **AC-5:** Base cloning and all nine CustomVoice speakers work for both
  quantizations with validated language/Auto mappings.
- **AC-6:** Real native streaming reaches 48 kHz artifacts and bounded
  preview correctly; segment boundaries do not reset resampler state
  within a segment or lose its final samples.
- **AC-7:** Cancellation, crashes, switching, and shutdown leave no
  overlapping model owners, stale audio, blocked GUI, or orphan host.
- **AC-8:** Variant-aware provenance prevents cross-format/quantization
  cache reuse and makes Studio restoration/re-synthesis explicit.
- **AC-9:** Existing Base clones work across formats from their stored
  source data; derived data never crosses incompatible runtimes.
- **AC-10:** All synthesis surfaces and localized UI pass consolidated
  fake-host scenarios; VieNeu and official Qwen behavior does not regress.
- **AC-11:** Frozen source/packaging paths locate the pinned native host
  assets correctly and classify missing native dependencies actionably.
- **AC-12:** The 24-combination real-model matrix, resource/cancel/offline
  checks, license inventory, documentation, and repository quality gates
  pass before advertising the complete target matrix as supported.

## Out of scope

1.7B checkpoints, VoiceDesign, Vulkan/ROCm/DirectML, Intel macOS, generic
user-supplied GGUFs, custom executable/library paths, mixed codec/talker
quantization, GGUF F32/BF16, new FP16 precision controls, local model
conversion, HTTP inference servers, engine auto-selection from input text,
and unrelated UI or controller refactoring.

## Research and evidence boundary

Read on 2026-09-23:

- Models: https://huggingface.co/Serveurperso/Qwen3-TTS-GGUF
- Runtime: https://github.com/ServeurpersoCom/qwentts.cpp
- Runtime README:
  https://raw.githubusercontent.com/ServeurpersoCom/qwentts.cpp/HEAD/README.md

The model card advertises both 0.6B modes, Q8_0/Q4_K_M, paired GGUFs,
and CPU/CUDA/Metal. The runtime README describes C-linkage `qt_*` APIs,
`QWEN_SHARED=ON`, streaming callbacks, reference extraction, Base cloning,
and CustomVoice named speakers. These are planning evidence, not verified
app compatibility or immutable revision pins.

Phase 1 must resolve exact source/submodule commits, header location,
ABI ownership/cancel semantics, model digests, Windows/macOS build details,
backend mappings, and redistribution dependencies. A failed probe blocks
the affected support claim; it does not authorize a different engine or
reduced scope without user approval.

The archived `qwen_multiengine_20260920` track supplies reusable patterns.
Its outstanding official-runtime real-device checks are not completed by
this track and must not be relabeled as GGUF evidence.
