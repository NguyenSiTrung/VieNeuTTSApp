# Qwen Multilingual Multi-Engine TTS Support Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use `subagent-driven-development`
> (recommended) or `executing-plans` to implement this plan task-by-task. Steps
> use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add isolated, optional Qwen 0.6B CustomVoice and Base profiles to all
existing synthesis workflows while keeping VieNeu as the Vietnamese default.

**Architecture:** VieNeu stays in the existing worker-owned in-process engine.
Qwen runs in one versioned local model-host subprocess backed by separately
managed, checksum-pinned runtime and model installations. Immutable capability
and synthesis-context contracts let both providers reuse the current tagged
job, artifact, playback, export, cache, and Studio paths.

**Tech Stack:** Python 3.10–3.13, PySide6/QML, QThread, local framed IPC,
`qwen-tts`, PyTorch CPU/MPS/CUDA, NumPy float32 PCM, stateful 24→48 kHz
resampling, Hugging Face safetensors, pytest/ruff/PyInstaller.

**Spec:** `conductor/tracks/qwen_multiengine_20260920/spec.md`

## Global Constraints

- TDD per `conductor/workflow.md`: write the failing test, run it to observe the
  expected failure, implement the smallest passing change, run focused tests,
  then run the task gate.
- Per-task gate: `.venv/bin/ruff check .`, `.venv/bin/ruff format --check .`,
  and `.venv/bin/pytest`; the documented device-less `QAudioSink` failure is
  not a regression.
- Commit after every completed task with a conventional prefix and attach a
  task summary through `git notes add`; never push.
- VieNeu remains the fresh-install and migrated-settings default.
- Exactly one large model owner may be resident; profile changes are forbidden
  while jobs are active or queued.
- Qwen runtime/model code stays optional, checksum-pinned, local-only after
  install, and outside the base frozen environment.
- Qwen runtime packages never enter the root `pyproject.toml` or `uv.lock`;
  platform requirements live in a separate managed-runtime lock input.
- Ordinary CI never downloads Qwen weights; deterministic fake-host coverage
  is mandatory.
- Every new user-facing string is localized in Vietnamese and English.

---

## Phase 0: Audit prior work, then prove and lock the Qwen platform contract
<!-- execution: sequential -->
<!-- depends: -->

- [x] Task 0.1: Audit the prior Qwen branch and define the salvage map
  <!-- files: docs/performance/qwen-port-audit.md -->
  - Compare `main...origin/feature/qwen-support` commit by commit and classify
    each Qwen file/test as port unchanged, adapt to current architecture, or
    reject, with an evidence-based reason and target file.
  - Explicitly audit the prior capability/backend contracts, resampler, CJK
    segmentation, cache identity, Qwen adapters, voice store, setup wizard,
    and fake/real-model tests against current jobs, managed installers,
    Studio, Subtitle, QML, and packaging contracts.
  - Record stale assumptions: same-process dependency loading, ineffective
    0.6B instructions, unmanaged package/model setup, and pre-v0.1.16 UI/test
    structure. Do not cherry-pick or implement application code in this task.
  - Gate: every commit from `279574d` through `f154725` and every Qwen-specific
    changed file appears exactly once in the salvage matrix.

- [x] Task 0.2: Build the real-runtime compatibility probe
  <!-- files: scripts/spike/qwen_runtime_probe.py, tests/unit/test_qwen_runtime_probe.py -->
  <!-- depends: task1 -->
  - Create a CLI that emits one JSON result with package/model revisions,
    device, dtype, attention implementation, reported languages/speakers,
    sample rate, TTFR, total time, RTF, peak RSS/VRAM, cancellation result,
    and clean-shutdown result.
  - Exercise CustomVoice generation and Base clone-prompt/generation through
    injectable model loaders; unit tests cover CLI validation and JSON schema
    without downloading weights.
  - Confirm the 0.6B instruction behavior and whether generation exposes any
    incremental audio before returning the final array.
  - Focused gate: `pytest tests/unit/test_qwen_runtime_probe.py -q`.

- [!] Task 0.3: Lock the supported dependency and device matrix
  <!-- files: docs/performance/qwen-runtime-compatibility.md, packaging/qwen-runtime-requirements.json -->
  <!-- depends: task2 -->
  - **Blocked on release hardware (2026-09-20):** the six-cell matrix, resolver
    inputs, wheel-availability evidence and model pins are locked and gated by
    `tests/unit/test_qwen_runtime_requirements.py`, but the two real-runtime
    probe runs per platform cannot be executed on the audit host (Linux arm64,
    no CUDA, no Apple Silicon). Platform evidence is recorded as `pending` and
    the matrix was **not** reduced. Unblock by running the recorded
    `evidence.probeCommand` on each platform and committing the JSON results.
  - Run the probe on Windows CPU/CUDA, Linux CPU/CUDA, and Apple Silicon
    CPU/MPS; record the exact commands and JSON evidence.
  - Record exact per-platform Qwen, PyTorch, torchaudio, Transformers,
    Accelerate, audio, resampler, and attention requirements in
    `packaging/qwen-runtime-requirements.json`; Task 2.2's maintainer lock
    script consumes it, while root `pyproject.toml` and `uv.lock` stay
    unchanged.
  - Stop and revise the spec if a required platform cannot synthesize
    correctly; do not silently reduce the matrix.
  - Focused gate: validate the requirements JSON schema, resolver inputs, and
    probe evidence without installing Qwen into the base environment.

- [ ] Task: Conductor - User Manual Verification 'Audit prior work, then prove and lock the Qwen platform contract' (Protocol in workflow.md)
  <!-- depends: task3 -->

## Phase 1: Add engine-profile, capability, and provenance contracts
<!-- execution: sequential -->
<!-- depends: phase0 -->

- [x] Task 1.1: Define immutable engine capabilities and settings migration
  <!-- files: src/vienetts_app/core/engine_profiles.py, src/vienetts_app/core/models.py, src/vienetts_app/core/settings.py, tests/unit/test_engine_profiles.py, tests/unit/test_models.py, tests/unit/test_settings.py -->
  - Define `EngineProfile`, `EngineCapabilities`, `LanguageOption`, and
    `VoiceOption`, with canonical IDs `vieneu`, `qwen_custom_0_6b`, and
    `qwen_base_0_6b`.
  - Add `Settings.engine_profile` and `Settings.qwen_device`; old JSON loads as
    VieNeu without losing any existing field.
  - Keep `backend` and `precision` scoped to VieNeu.
  - Test every profile capability, invalid value, and old/new settings
    round-trip before implementation.

- [x] Task 1.2: Snapshot synthesis context in every job
  <!-- files: src/vienetts_app/core/synthesis_context.py, src/vienetts_app/core/models.py, src/vienetts_app/core/jobs.py, tests/unit/test_synthesis_context.py, tests/unit/test_models.py, tests/unit/test_jobs.py -->
  <!-- depends: task1 -->
  - Define frozen `SynthesisContext(profile, model_revision, language,
    voice_id, clone_id, generation)` and deterministic
    `fingerprint_payload()`.
  - Extend `TTSRequest`/`SynthesisJob` with validated immutable context; reject
    incompatible speaker/clone/language fields before queue admission.
  - Migrate production callers in later phases; test compatibility defaults,
    identity tagging, immutability, and stable fingerprints now.

- [ ] Task: Conductor - User Manual Verification 'Add engine-profile, capability, and provenance contracts' (Protocol in workflow.md)
  <!-- depends: task2 -->

## Phase 2: Build verified Qwen runtime and model installation
<!-- execution: parallel -->
<!-- depends: phase0 -->

- [x] Task 2.1: Extract shared managed-install primitives
  <!-- files: src/vienetts_app/core/managed_install.py, src/vienetts_app/core/model_manager.py, src/vienetts_app/core/cuda_runtime.py, tests/unit/test_managed_install.py, tests/unit/test_model_manager.py, tests/unit/test_cuda_runtime_manager.py -->
  - Extract HTTPS allowlisting, ranged resume, size/SHA-256 verification,
    free-space checks, staging, atomic promotion, Windows path handling, and
    safe removal without changing existing manager behavior.
  - Prove preservation with existing model/CUDA tests plus focused primitive
    tests for truncation, redirect rejection, corruption, cancellation, and
    atomic failure.

- [x] Task 2.2: Add Qwen runtime manifests and manager
  <!-- files: src/vienetts_app/core/qwen_runtime_manifest.py, src/vienetts_app/core/qwen_runtime.py, scripts/lock_qwen_runtime.py, tests/unit/test_qwen_runtime_manifest.py, tests/unit/test_qwen_runtime_manager.py, tests/unit/test_qwen_runtime_lock.py -->
  <!-- depends: task1 -->
  - Render separate pinned manifests for Windows CPU/CUDA, Linux CPU/CUDA,
    and macOS arm64 CPU/MPS from Phase 0 evidence.
  - Implement inspect/install/resume/cancel/repair/remove/offline import with
    exact platform, Python tag, wheel filename, size, and hash metadata.
  - Test URL policy, manifest drift, interrupted downloads, corrupt wheels,
    rollback, and in-use removal refusal.

- [x] Task 2.3: Add Qwen model manifests and manager
  <!-- files: src/vienetts_app/core/qwen_model_manifest.py, src/vienetts_app/core/qwen_model_manager.py, scripts/fetch_qwen_models.py, tests/unit/test_qwen_model_manifest.py, tests/unit/test_qwen_model_manager.py, tests/unit/test_fetch_qwen_models.py -->
  <!-- depends: task1 -->
  - Pin CustomVoice, Base, and tokenizer repositories, revisions, files,
    sizes, and hashes; reuse verified tokenizer content between profiles.
  - Implement inspect/install/resume/cancel/repair/remove/offline import.
  - Test independent profile lifecycle, shared-file retention, corruption,
    free-space refusal, offline packs, and atomic promotion.

- [ ] Task: Conductor - User Manual Verification 'Build verified Qwen runtime and model installation' (Protocol in workflow.md)
  <!-- depends: task1, task2, task3 -->

## Phase 3: Implement the isolated Qwen model host
<!-- execution: sequential -->
<!-- depends: phase1, phase2 -->

- [x] Task 3.1: Define and test the framed IPC protocol
  <!-- files: src/vienetts_app/core/qwen_protocol.py, tests/unit/test_qwen_protocol.py -->
  - Version and validate job-tagged `hello`, `load`, `capabilities`,
    `synthesize`, `pcm`, `progress`, `cancel`, `terminal`, `error`, and
    `shutdown` frames.
  - Use length-prefixed JSON headers plus bounded binary float32 payloads.
  - Test partial reads, oversized frames, malformed JSON, unknown versions,
    wrong job IDs, invalid transitions, and binary round-trips.

- [x] Task 3.2: Implement the model-host executable
  <!-- files: src/vienetts_app/workers/qwen_host.py, tests/unit/test_qwen_host.py -->
  <!-- depends: task1 -->
  - Load only local verified paths with remote code disabled; implement
    CustomVoice, Base clone prompts/generation, reported capabilities,
    device/dtype/attention selection, and stderr-only structured logging.
  - Keep one stateful 24→48 kHz resampler for the whole job and emit bounded
    48 kHz float32 frames.
  - Test through fake Qwen models: segmentation, speaker/language validation,
    clone prompts, resampler continuity, OOM/device errors, and shutdown.

- [x] Task 3.3: Implement the parent Qwen adapter and lifecycle
  <!-- files: src/vienetts_app/core/qwen_engine.py, tests/unit/test_qwen_engine.py -->
  <!-- depends: task2 -->
  - Expose `initialize()`, `capabilities()`, `infer_stream()`, `cancel()`, and
    `close()` over a shell-free sanitized subprocess.
  - Enforce handshake/version/timeouts, drain stderr, drop stale frames, reap
    every child, and escalate cancellation from request to terminate to kill.
  - Test hangs, crashes, malformed output, OOM/device errors, stale delivery,
    partial PCM, and clean lazy restart.

- [x] Task 3.4: Route Qwen through the existing worker/artifact pipeline
  <!-- files: src/vienetts_app/core/text_segmentation.py, src/vienetts_app/workers/inference_worker.py, src/vienetts_app/core/engine.py, src/vienetts_app/core/artifacts.py, tests/unit/test_text_segmentation.py, tests/unit/test_inference_worker.py, tests/unit/test_engine.py, tests/unit/test_artifacts.py, tests/unit/test_pcm_transport.py -->
  <!-- depends: task3 -->
  - Add `split_text_for_profile(text, language, max_chars)` in
    `core/text_segmentation.py`; it preserves Vietnamese/English behavior,
    handles CJK punctuation without inserting spaces, and bounds every Qwen
    segment before it crosses IPC.
  - Introduce the engine-provider protocol and select the provider from the
    immutable context without switching mid-job.
  - Preserve one terminal per job, partial-artifact cleanup, bounded preview,
    progress, and cancellation semantics.
  - Test VieNeu regression parity and all Qwen terminal/restart paths.

- [x] Task: Conductor - User Manual Verification 'Implement the isolated Qwen model host' (Protocol in workflow.md)
  <!-- depends: task4 -->

## Phase 4: Add safe Qwen voice-clone persistence
<!-- execution: sequential -->
<!-- depends: phase3 -->

- [x] Task 4.1: Create the profile-scoped clone store
  <!-- files: src/vienetts_app/core/voice_profiles.py, tests/unit/test_voice_profiles.py -->
  - Persist stable ID, display name, engine profile, copied reference WAV,
    required transcript, content hash, and timestamps as atomic JSON plus
    app-owned audio; never persist pickle or torch objects.
  - Test duration/codec/transcript validation, restart, collisions,
    corruption, deduplication, removal, and atomic failures.

- [x] Task 4.2: Adapt clone operations to engine capabilities
  <!-- files: src/vienetts_app/core/models.py, src/vienetts_app/workers/inference_worker.py, src/vienetts_app/core/engine.py, src/vienetts_app/core/qwen_engine.py, tests/unit/test_models.py, tests/unit/test_inference_worker.py, tests/unit/test_engine.py, tests/unit/test_qwen_engine.py -->
  <!-- depends: task1 -->
  - Extend `VoiceOp` with profile/context; preserve VieNeu add/remove behavior.
  - For Base, store reference data and build/cache runtime prompts only inside
    the model host; reject CustomVoice cloning with a capability reason.
  - Test add/use/remove, restart, profile isolation, and prompt rebuild.

- [x] Task: Conductor - User Manual Verification 'Add safe Qwen voice-clone persistence' (Protocol in workflow.md)
  <!-- depends: task2 -->

## Phase 5: Integrate profiles across controllers, caches, and Studio
<!-- execution: parallel -->
<!-- depends: phase4 -->

- [x] Task 5.1: Add global profile switching and readiness state
  <!-- files: src/vienetts_app/ui/controller.py, src/vienetts_app/app.py, tests/unit/test_controller.py, tests/unit/test_app_entry.py -->
  - Expose active profile, capabilities, compatible languages/voices/clones,
    resolved device, runtime/model state, and guarded profile switching.
  - Shut down the current owner before activation, reject switching while
    active/queued, and keep startup/model inspection off the GUI thread.
  - Test every state transition, migration, switch refusal, load failure, and
    teardown order.

- [x] Task 5.2: Snapshot profile context in Text, Paragraph, and Batch jobs
  <!-- files: src/vienetts_app/ui/controller.py, src/vienetts_app/ui/batch_controller.py, tests/unit/test_controller.py, tests/unit/test_batch_controller.py -->
  <!-- depends: task1 -->
  - Require compatible language and speaker/clone at every submission and
    persist immutable context on queued batch entries.
  - Test unsupported combinations, settings changes after enqueue,
    cancellation, listener routing, and switch blocking.

- [x] Task 5.3: Make Audiobook and Subtitle caches engine-safe
  <!-- files: src/vienetts_app/core/audiobook.py, src/vienetts_app/ui/audiobook_controller.py, src/vienetts_app/core/subtitle_project.py, src/vienetts_app/ui/subtitle_controller.py, tests/unit/test_audiobook.py, tests/unit/test_audiobook_controller.py, tests/unit/test_subtitle_project.py, tests/unit/test_subtitle_controller.py -->
  <!-- depends: task2 -->
  - Include `SynthesisContext.fingerprint_payload()` in persisted render
    provenance and cache fingerprints; invalidate only incompatible renders.
  - Test profile/revision/language/voice/clone changes and old-project
    migration without unnecessary cache loss.

- [x] Task 5.4: Preserve Studio provenance and truthful re-synthesis
  <!-- files: src/vienetts_app/core/studio.py, src/vienetts_app/core/timeline.py, src/vienetts_app/ui/controller.py, tests/unit/test_studio.py, tests/unit/test_studio_controller.py, tests/unit/test_timeline.py -->
  <!-- depends: task2 -->
  - Persist synthesis context per clip while leaving editing/export
    engine-independent.
  - Require a matching active profile for re-synthesis and expose a switch
    action instead of substituting another provider.
  - Test old projects, imported artifacts, save/load, mismatches, and rollback.

- [x] Task: Conductor - User Manual Verification 'Integrate profiles across controllers, caches, and Studio' (Protocol in workflow.md)
  <!-- depends: task1, task2, task3, task4 -->

## Phase 6: Add capability-aware UI and localization
<!-- execution: parallel -->
<!-- depends: phase5 -->

- [x] Task 6.1: Add shared engine/language controls and Settings management
  <!-- files: src/vienetts_app/ui/qml/components/EngineProfilePicker.qml, src/vienetts_app/ui/qml/components/LanguagePicker.qml, src/vienetts_app/ui/qml/qmldir, src/vienetts_app/ui/qml/SettingsTab.qml, tests/smoke/test_ui_tabs.py -->
  - Add shared profile/language controls and separate Model family, Compute
    device, Qwen runtime, and Qwen model cards.
  - Surface storage, install/import/cancel/repair/remove, unsupported hardware,
    corruption, reload, and CPU performance guidance.
  - Add stable object names and consolidated offscreen smoke scenarios.

- [x] Task 6.2: Integrate active-profile controls into synthesis surfaces
  <!-- files: src/vienetts_app/ui/qml/TextTab.qml, src/vienetts_app/ui/qml/ParagraphTab.qml, src/vienetts_app/ui/qml/AudiobookTab.qml, src/vienetts_app/ui/qml/components/SynthesisBar.qml, src/vienetts_app/ui/qml/components/SubtitleCard.qml, src/vienetts_app/ui/qml/components/VoicePicker.qml -->
  <!-- depends: task1 -->
  - Bind language and compatible voice/clone choices to capabilities across
    Text, Paragraph/Batch, Audiobook, and Subtitle while preserving docked
    actions and 640×420 integrity.
  - Disable unsupported controls with visible reasons and prevent stale
    cross-profile selections.

- [x] Task 6.3: Adapt Cloning and Studio UI
  <!-- files: src/vienetts_app/ui/qml/CloningTab.qml, src/vienetts_app/ui/qml/StudioTab.qml, src/vienetts_app/ui/qml/components/StudioClipRow.qml, tests/unit/test_studio_controller.py -->
  <!-- depends: task1 -->
  - Require transcript for Base, show profile ownership, disable CustomVoice
    cloning, and expose Studio provenance/matching-profile actions.
  - Test enrollment validation, profile filtering, and re-synthesis mismatch.

- [x] Task 6.4: Complete shared QML smoke coverage and Vietnamese/English catalogs
  <!-- files: tests/smoke/test_ui_tabs.py, src/vienetts_app/ui/i18n/vienetts_en.ts, src/vienetts_app/ui/i18n/vienetts_en.qm, tests/unit/test_i18n.py -->
  <!-- depends: task2, task3 -->
  - Consolidate offscreen scenarios for Settings, synthesis workflows,
    Cloning, and Studio after both parallel UI branches land; preserve one
    `QGuiApplication` per subprocess and all new object-name contracts.
  - Translate every new string with disambiguation context, compile the
    catalog, and extend catalog-completeness assertions.

- [x] Task: Conductor - User Manual Verification 'Add capability-aware UI and localization' (Protocol in workflow.md)
  <!-- depends: task1, task2, task3, task4 -->

## Phase 7: Package, validate, and close the track
<!-- execution: parallel -->
<!-- depends: phase6 -->

- [x] Task 7.1: Package the host without optional runtimes/models
  <!-- files: packaging/vienetts-app.spec, .github/workflows/release.yml, tests/unit/test_package.py, tests/unit/test_linux_packaging.py, tests/smoke/test_main_cli.py -->
  - Include lightweight host/protocol code but exclude Qwen/PyTorch/weights.
  - Test frozen host spawning, windowless Windows behavior, paths with
    spaces/non-ASCII, macOS signing coverage, and Linux layout.

- [ ] Task 7.2: Add deterministic fake-host end-to-end coverage
  <!-- files: tests/smoke/test_e2e_flows.py, tests/smoke/test_ui_tabs.py -->
  - In consolidated subprocess scenarios, cover ready install, profile switch,
    CustomVoice synthesis, Base enrollment/synthesis, artifact replay/export,
    Studio guard, cancellation, crash recovery, and shutdown.

- [ ] Task 7.3: Add opt-in real-model release validation
  <!-- files: .github/workflows/qwen-runtime-smoke.yml, scripts/check_smoke_wav.py, docs/performance/qwen-runtime-compatibility.md -->
  <!-- depends: task1, task2 -->
  - Consume pre-provisioned verified packs and validate 48 kHz WAV output on
    Windows CPU/CUDA, Linux CPU/CUDA, and Apple Silicon CPU/MPS.
  - Record TTFR, total time, RTF, peak memory, cancellation latency, and host
    restart; ordinary CI downloads nothing.

- [ ] Task 7.4: Final quality gate and context synchronization
  <!-- files: conductor/product.md, conductor/tech-stack.md, conductor/patterns.md, conductor/tracks.md, conductor/tracks/qwen_multiengine_20260920/learnings.md, conductor/tracks/qwen_multiengine_20260920/metadata.json -->
  <!-- depends: task1, task2, task3 -->
  - Run the full project gates, perform user manual verification, update
    product/stack/patterns/learnings and release-facing setup text, close the
    Beads hierarchy, and mark the track complete.

- [ ] Task: Conductor - User Manual Verification 'Package, validate, and close the track' (Protocol in workflow.md)
  <!-- depends: task1, task2, task3, task4 -->

## Parallel Execution Summary

- Phases 1 and 2 both depend only on Phase 0 and may run concurrently.
- In Phase 2, Tasks 2.2 and 2.3 run concurrently after Task 2.1.
- In Phase 5, Tasks 5.3 and 5.4 run concurrently after central integration;
  their listed ownership is disjoint except `controller.py`, whose Studio
  additions must be serialized by the lead if Task 5.2 still holds it.
- In Phase 6, Tasks 6.2 and 6.3 run concurrently after Task 6.1; Task 6.4 waits
  for both so translation catalogs remain single-owner.
- In Phase 7, Tasks 7.1 and 7.2 run concurrently; Task 7.3 waits for both and
  Task 7.4 waits for all implementation/validation tasks.
- Commits are serialized by the lead to avoid shared-index lock conflicts.
