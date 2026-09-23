# Qwen 0.6B GGUF Engine Implementation Plan

> **For agentic workers:** Use the `executing-plans` skill to implement this
> plan task-by-task. The user approved sequential execution, not parallel
> subagents. Read the specification and inherited learnings first.

**Goal:** Offer official-weight PyTorch or GGUF qwentts.cpp inference for
Qwen3-TTS 0.6B Base and CustomVoice, with Q8_0/Q4_K_M selection.

**Architecture:** Preserve semantic profile IDs and add a validated model
variant contract. Install pinned native libraries and paired GGUF models
separately; bind the C ABI only inside an isolated, lightweight host that
reuses framed IPC, lifecycle controls, and the artifact pipeline.

**Tech stack:** Existing Python/PySide6/QML application, stdlib `ctypes`,
existing NumPy audio processing, qwentts.cpp/GGML native runtime packs,
CMake for maintainer builds, pytest/ruff, and Beads.

**Spec:** [spec.md](./spec.md)

## Global constraints

- Preserve `vieneu`, `qwen_base_0_6b`, and `qwen_custom_0_6b`.
- Official full weights retain CPU/MPS FP32 and CUDA BF16.
- GGUF choices are exactly `Q8_0` and `Q4_K_M`, paired with the matching
  tokenizer quantization; initial GGUF default is `Q8_0`.
- Target Windows x64 CPU/CUDA, Linux x64 CPU/CUDA, macOS arm64 CPU/Metal.
- No Vulkan, 1.7B, VoiceDesign, arbitrary GGUFs, or new precision controls.
- No native library/model loads on GUI startup; no new PyTorch/model
  dependencies in the base bundle or root dependency lock.
- Exactly one resident model owner; no silent cross-format fallback.
- Normal CI is model-free, network-free, and deterministic.
- All phases/tasks execute sequentially. A blocked compatibility cell
  remains blocked; do not mark hardware-dependent evidence complete.
- No per-phase manual verification or user approval gates. Continue after
  automated task checks pass; retain final scripted real-model release
  checks and report genuine compatibility blockers.
- Commit each completed task locally and attach its task summary using
  `git notes add`. Never automatically fetch, pull, push, or Dolt-sync.

## Task execution and gates

Every task below contains red-test cases, a focused command, implementation
steps, and a completion condition. For each code task:

1. Add the named tests first. Run its focused command and confirm failure
   from the missing contract, not an environment/import setup error.
2. Implement the specified behavior, rerun the focused command, and
   refactor only within that task's file ownership.
3. Run the repository gates from the repository root:

   ```bash
   .venv/bin/ruff check .
   .venv/bin/ruff format --check .
   QT_QPA_PLATFORM=offscreen QT_AUDIO_BACKEND=ffmpeg .venv/bin/pytest
   ```

4. Follow `conductor/workflow.md` for the documented device-less real
   QAudioSink failure (`VieNeuTTSApp-3iy`); report it separately rather than
   changing unrelated app code. Investigate any additional failure.
5. Review/stage only that task's files, commit with its supplied message,
   attach a git note naming the task and verification, update its Bead and
   checkbox, and capture new learnings in `learnings.md`.

New test module names below are intentional deliverables. Native ABI
definitions must be generated/transcribed from the exact Phase 1 header,
not invented from examples in this plan.

## File and responsibility map

| Area | Files |
| --- | --- |
| Upstream contract/build evidence | `docs/performance/qwen-gguf-compatibility.md`, `packaging/qwen-gguf-runtime-requirements.json`, `scripts/spike/qwen_gguf_probe.py` |
| Native pack production | `scripts/build_qwen_gguf_runtime.py`, `scripts/lock_qwen_gguf_runtime.py`, `.github/workflows/qwen-gguf-runtime-build.yml` |
| Selection/capabilities | `core/qwen_variants.py`, existing `core/engine_profiles.py`, `core/models.py`, `core/settings.py` |
| Provenance | existing `core/synthesis_context.py`, `core/jobs.py`, `core/studio.py`, `core/subtitle_project.py`, audiobook persistence |
| Runtime installer | `core/qwen_gguf_runtime.py`, `core/qwen_gguf_runtime_manifest.py`, `core/qwen_gguf_runtime_manifests.json` |
| Model installer | `core/qwen_gguf_models.py`, `core/qwen_gguf_model_manifest.py`, `core/qwen_gguf_model_manifests.json`, `scripts/fetch_qwen_gguf_models.py` |
| Native binding/host | `workers/qwen_gguf_abi.py`, `workers/qwen_gguf_host.py`, `core/streaming_resampler.py`, existing `workers/qwen_host.py` |
| Parent lifecycle/protocol | `core/qwen_gguf_engine.py`, existing `core/qwen_engine.py`, `core/qwen_protocol.py`, `workers/inference_worker.py` |
| App/UI | existing `ui/controller.py`, `ui/qml/EngineState.qml`, `ui/qml/SettingsTab.qml`, new shared `ui/qml/components/QwenVariantPicker.qml`, `QwenInstallCards.qml` |
| Packaging/release validation | existing `__main__.py`, `packaging/vienetts-app.spec`, new `scripts/qwen_gguf_release_smoke.py`, `.github/workflows/qwen-gguf-runtime-smoke.yml` |

Paths beginning `core/`, `workers/`, `ui/`, or `__main__.py` above are
relative to `src/vienetts_app/`.

## Phase 1: Prove and pin upstream compatibility

- [x] Task 1.1: Audit the native contract and build a real-model probe

  **Files:** Create `docs/performance/qwen-gguf-compatibility.md`,
  `packaging/qwen-gguf-runtime-requirements.json`,
  `scripts/spike/qwen_gguf_probe.py`,
  `tests/unit/test_qwen_gguf_probe.py`.

  **Consumes:** approved spec, model repository, native source, archived
  Qwen probe conventions. **Produces:** a versioned evidence schema with
  source/GGML commits, ABI version or header hash, model repository commit,
  file identities, platform/backend, profile, quantization, and verdict.

  - [x] Red: fake native calls prove one JSON verdict for success, usage
    error, missing backend, ABI mismatch, invalid speaker, cancellation,
    and empty/non-finite audio. Assert six matrix cells and 24 combinations:

    ```python
    assert len(requirements["cells"]) == 6
    assert {v["quantization"] for v in requirements["variants"]} == {"Q8_0", "Q4_K_M"}
    assert len(requirements["variants"]) == 4
    ```

  - [x] Run `.venv/bin/pytest tests/unit/test_qwen_gguf_probe.py -n 0`.
  - [x] Inspect the pinned header/build configuration and record exact
    struct layouts, symbol signatures, callbacks, buffer lifetimes,
    allocator/free pairs, cancellation mechanism, thread control, and
    device enumeration. Check DLL/dylib/shared-library dependencies and
    OS/CPU deployment floors. Verify Base/CustomVoice semantics, all nine
    speakers, ten language mappings and Auto; preserve unsupported results.
  - [x] Probe both quantizations with reference+transcript Base input and
    named-speaker CustomVoice input on available hardware. Record honest
    status for unrun cells. Collect TTFA, throughput, RSS and GPU memory
    where supported, streaming equivalence, cancel latency and offline use.
    Never log source text or clone recordings.
  - [x] Green: deterministic probe tests pass; exact pins and observed
    limitations are recorded, not guessed. An unavailable build/ABI needed
    for subsequent implementation blocks that path. Commit
    `docs(qwen): pin GGUF runtime compatibility contract`.

- [x] Task 1.2: Produce reproducible native runtime packs

  **Files:** Create `scripts/build_qwen_gguf_runtime.py`,
  `scripts/lock_qwen_gguf_runtime.py`,
  `.github/workflows/qwen-gguf-runtime-build.yml`,
  `tests/unit/test_qwen_gguf_runtime_build.py`; update Phase 1 requirements.

  **Consumes:** exact native/GGML commits and supported build flags from
  1.1. **Produces:** immutable, per-platform backend packs with a library
  inventory, license notices, ABI/build identity, digest and size manifest.
  Artifact publication is a later explicitly authorized release action.

  - [x] Red: command-generation fixtures assert the shared-library build,
    fixed revisions, deployment floors, CPU ISA requirements, and correct
    backend for every target; missing dependencies/notices reject a pack.
  - [x] Run `.venv/bin/pytest tests/unit/test_qwen_gguf_runtime_build.py -n 0`.
  - [x] Build with the pinned `QWEN_SHARED=ON` configuration; inventory all
    redistributable dependencies and loader-relative paths. Keep CUDA
    driver requirements explicit, Metal libraries platform-native, and
    CPU dispatch safe on supported hardware. No downloading build tools
    from the running application.
  - [x] Exercise load/symbol smoke checks in clean target environments.
    Only verified artifacts may be locked for application installation;
    missing target hardware/artifacts remain explicit blockers.
  - [x] Green: reproducible pack metadata and build tests pass. Commit
    `build(qwen): add pinned native runtime pack tooling`.

## Phase 2: Add variant selection and durable identity

- [x] Task 2.1: Define variants and migrate settings

  **Files:** Create `src/vienetts_app/core/qwen_variants.py`,
  `tests/unit/test_qwen_variants.py`; modify `core/engine_profiles.py`,
  `core/models.py`, `core/settings.py`, `tests/unit/test_engine_profiles.py`,
  `tests/unit/test_settings.py`.

  **Interfaces:** `variant_for(profile, model_format="official",
  quantization="") -> QwenVariant`; immutable `QwenVariant` exposes
  `profile`, `model_format`, `quantization`, `engine`, and `devices`.
  Engine values are `pytorch` and `qwentts_cpp`. Official quantization is
  empty; GGUF empty quantization resolves to `Q8_0`. Device availability
  belongs to runtime/hardware readiness, not this static capability table.

  - [x] Red: test the four GGUF variants, two official variants, forbidden
    format/quantization combinations, unchanged profile IDs and precision:

    ```python
    variant = variant_for(QWEN_BASE, model_format="gguf", quantization="Q4_K_M")
    assert (variant.profile, variant.engine) == (QWEN_BASE, "qwentts_cpp")
    assert variant_for(QWEN_CUSTOM).engine == "pytorch"
    assert host_precision("cuda") == ("bfloat16", "sdpa")
    ```

  - [x] Run `.venv/bin/pytest tests/unit/test_qwen_variants.py tests/unit/test_engine_profiles.py tests/unit/test_settings.py -n 0`.
  - [x] Add `qwen_model_format`, `qwen_gguf_quantization`, and
    `qwen_gguf_device` settings; retain existing `qwen_device` for official
    weights. Clamp fields independently and preserve inactive format
    preferences. Resolve compatible engine automatically from format.
    Derive variant capabilities from the existing semantic profile table,
    narrowed by Phase 1 evidence rather than duplicating voice catalogs.
  - [x] Green: legacy/default/corrupt settings and all invalid selections
    have deterministic outcomes. Commit
    `feat(qwen): add model format and quantization selection`.

- [x] Task 2.2: Version job, cache, and Studio provenance

  **Files:** Modify `core/synthesis_context.py`, `core/jobs.py`,
  `core/models.py`, `core/studio.py`, `core/subtitle_project.py` and
  audiobook context serialization consumers identified by a call-site
  search; update `tests/unit/test_synthesis_context.py`,
  `test_studio.py`, `test_subtitle_project.py`, and audiobook tests.

  **Interfaces:** Extend `SynthesisContext` with backward-compatible
  defaults for format, quantization, runtime/model/tokenizer identity and
  resolved device. Retain `context_for`, `context_from_payload`,
  `context_matches`, and `same_engine`; extend their keyword/payload
  contracts rather than introducing an alternate context type.

  - [x] Red: Q8_0 and Q4_K_M contexts differ in fingerprint and
    `same_engine`; official and GGUF never match. Old recorded Qwen
    contexts resolve to official weights, wholly unstamped renders remain
    VieNeu-only, and unknown payload versions cannot become GGUF:

    ```python
    assert q8_context.fingerprint() != q4_context.fingerprint()
    assert not context_matches(q8_context, official_context)
    assert not same_engine(q8_context, q4_context)
    assert context_from_payload(old_qwen_payload).model_format == "official"
    ```

  - [x] Run `.venv/bin/pytest tests/unit/test_synthesis_context.py tests/unit/test_studio.py tests/unit/test_subtitle_project.py -n 0`.
  - [x] Add an explicit payload schema version and immutable resolved
    identities. Decode known legacy payloads conservatively; unknown
    historical runtime identity cannot fabricate an exact cache hit.
    Update all serialization consumers and Studio's restoration payload
    together. Distinguish explicit re-synthesis from exact cache reuse.
  - [x] Green: round-trip, migration and cache-isolation tests pass.
    Commit `feat(qwen): stamp renders with model variant provenance`.

## Phase 3: Install verified native runtimes and GGUF models

- [x] Task 3.1: Add the native runtime manifest and installer

  **Files:** Create `core/qwen_gguf_runtime_manifest.py`,
  `core/qwen_gguf_runtime_manifests.json`, `core/qwen_gguf_runtime.py`,
  `tests/unit/test_qwen_gguf_runtime.py`; modify `core/managed_install.py`
  only for genuinely shared primitives and cover their existing consumers.

  **Interfaces:** `QwenGgufRuntimeManager` exposes `status`,
  `install_online`, `install_from_offline_pack`, `repair`, and `remove`.
  A ready result returns a verified library path, build/ABI identity,
  backend inventory, and declared dependencies. It never loads the library
  to answer status on the GUI thread.

  - [x] Red: tiny archive fixtures cover successful install, checksum/
    size mismatch, wrong platform/ABI, traversal/escaping links, truncated
    downloads, cancellation/resume, insufficient space, failed promotion
    rollback, in-use removal, and incompatible offline packs.
  - [x] Run `.venv/bin/pytest tests/unit/test_qwen_gguf_runtime.py -n 0`.
  - [x] Reuse the verified managed-install lifecycle, not pip/wheel
    semantics for native archives. Restrict extraction and native library
    search paths to the verified pack; compute expanded-space requirements.
    Preserve the last good install until promotion succeeds.
  - [x] Green: valid pack status contains the exact locked identity:

    ```python
    status = manager.status()
    assert status.ready
    assert status.runtime_identity == locked_pack.identity
    assert status.library_path.is_relative_to(manager.root)
    ```

    Commit `feat(qwen): manage verified GGUF native runtimes`.

- [x] Task 3.2: Add paired GGUF model installation

  **Files:** Create `core/qwen_gguf_model_manifest.py`,
  `core/qwen_gguf_model_manifests.json`, `core/qwen_gguf_models.py`,
  `scripts/fetch_qwen_gguf_models.py`,
  `tests/unit/test_qwen_gguf_models.py`,
  `tests/unit/test_fetch_qwen_gguf_models.py`.

  **Interfaces:** `QwenGgufModelManager` provides the same install/status/
  repair/remove operations, keyed by `QwenVariant`. Its verified ready
  result includes `talker_path`, `tokenizer_path`, and `model_identity`.
  The manifest records four talkers and two shared tokenizers.

  - [x] Red: fixtures prove exact pair selection, shared-file reuse,
    independent removal, reference-safe tokenizer cleanup, wrong GGUF
    metadata, wrong hashes/revisions, offline layout rejection, partial
    resume, cancel, free-space refusal, and rollback.
  - [x] Run `.venv/bin/pytest tests/unit/test_qwen_gguf_models.py tests/unit/test_fetch_qwen_gguf_models.py -n 0`.
  - [x] Lock real immutable HF artifacts with per-file digests and sizes.
    Validate GGUF metadata/architecture as well as filenames. Keep the
    managed GGUF tree separate from official weights. Reference shared
    codec files by immutable identity; never delete a codec still used by
    an installed variant. Keep source clones outside model cleanup.
  - [x] Green: both profiles in Q8_0 share one codec while Q4_K_M has its
    own codec; installed status never depends on the PyTorch runtime.
    Commit `feat(qwen): install paired GGUF model variants`.

## Phase 4: Integrate isolated native streaming

- [x] Task 4.1: Bind the native ABI and implement the child host

  **Files:** Create `workers/qwen_gguf_abi.py`,
  `workers/qwen_gguf_host.py`, `core/streaming_resampler.py`,
  `tests/unit/test_qwen_gguf_host.py`; modify `workers/qwen_host.py`,
  `core/qwen_protocol.py`, `tests/unit/test_qwen_host.py`,
  `tests/unit/test_qwen_protocol.py`.

  **Interfaces:** `NativeQwenSession` is an internal binding with explicit
  load, streaming synthesis, reference preparation, cancellation and close
  operations. Its concrete ctypes structures/signatures must match Task
  1.1's header. `QwenGgufHost` speaks the existing `Frame`/`SessionState`
  protocol. Move the existing NumPy-only `StreamingResampler` unchanged
  into the shared module and retain its old import surface.

  - [x] Red: injectable fake ABI verifies callback retention, buffer
    copy-before-return, correct native free functions, bounded frames,
    stdout/log separation, capabilities, unsupported device errors,
    last-sample flush, and cleanup on every error. Chunked resampling
    matches the existing one-shot result.
  - [x] Run `.venv/bin/pytest tests/unit/test_qwen_gguf_host.py tests/unit/test_qwen_host.py tests/unit/test_qwen_protocol.py -n 0`.
  - [x] Version/validate any new protocol fields without weakening
    official-host validation. Map native Metal explicitly. Load only the
    verified library/build; isolate its stdout before initialization so
    native logging cannot corrupt framed IPC. Retain callback objects,
    copy borrowed PCM promptly, bound queues, and propagate exceptions
    outside ctypes callbacks through structured terminal frames.
  - [x] Stream callbacks through one stateful resampler per segment.
    Keep control reading responsive during native inference; use the
    pinned cancel mechanism if supported and let parent escalation handle
    an uninterruptible call. Free native context/reference/audio buffers
    according to the audited ABI, including partial initialization.
  - [x] Green: host modules import without torch or installed native
    libraries; fake ABI tests and official-host regressions pass. Commit
    `feat(qwen): add isolated GGUF streaming host`.

- [x] Task 4.2: Add parent lifecycle, routing, and resource bounds

  **Files:** Create `core/qwen_gguf_engine.py`,
  `tests/unit/qwen_gguf_host_fake.py`,
  `tests/unit/test_qwen_gguf_engine.py`; modify `core/qwen_engine.py`,
  `workers/inference_worker.py`, `ui/controller.py`,
  `tests/unit/test_qwen_engine.py`, and worker/controller tests.

  **Interfaces:** `QwenGgufEngine` implements the existing provider
  `initialize`, `capabilities`, `infer_stream`, `cancel`, and `close`
  surface. Serial segment batching is valid; never advertise native
  parallel batching without evidence. Controller factory selection
  consumes `QwenVariant` and verified install results.

  - [x] Red: a real fake child exercises handshake/load, streamed audio,
    malformed frames, EOF, timeout, cancel before pickup/during load/during
    synthesis, stale frames, idempotent close, restart and reap. Assert:

    ```python
    assert terminals_for(cancelled_job) == ["cancelled"]
    assert pcm_after_terminal(cancelled_job) == []
    assert max_resident_model_owners == 1
    ```

  - [x] Run `.venv/bin/pytest tests/unit/test_qwen_gguf_engine.py tests/unit/test_qwen_engine.py -n 0`.
  - [x] Reuse or narrowly extract engine-neutral transport/lifecycle
    primitives from the official adapter. Preserve SIGPIPE-safe writes,
    Windows pipe-reader ownership, windowless spawn, sanitized offline
    environment, startup-generation guards, and worker-owned teardown.
    GUI cancel only records/signals intent; waits and kill escalation run
    off-thread. Do not duplicate the entire official engine.
  - [x] Bind thread/resource policy to native capabilities, retain
    bounded segments/output and job-boundary RSS recycling, and prevent
    the next owner loading until the old host is reaped. Report OOM or
    unavailable backend without silently changing the selection.
  - [x] Green: cancel/crash/restart scenarios pass and existing official
    cancellation/footprint-governor tests remain green. Commit
    `feat(qwen): route GGUF jobs through a bounded native host`.

- [x] Task 4.3: Integrate Base clones and CustomVoice mappings

  **Files:** Modify `core/voice_profiles.py`, `core/qwen_variants.py`,
  `workers/qwen_gguf_abi.py`, `workers/qwen_gguf_host.py`,
  `tests/unit/test_voice_profiles.py`, `tests/unit/test_qwen_gguf_host.py`.

  **Consumes:** existing enrollment data and audited speaker/language
  mappings. **Produces:** native reference data owned by the host and a
  bounded variant-keyed reference cache; no new persisted opaque objects.

  - [x] Red: a Base enrollment created under official weights is accepted
    by GGUF from the original clip/transcript; CustomVoice rejects clones.
    Test all nine speaker IDs, language/Auto translation, invalid
    selections, resampling to mono 24 kHz, consent/transcript validation,
    and cache invalidation on changed source/transcript/build/quantization.
  - [x] Run `.venv/bin/pytest tests/unit/test_voice_profiles.py tests/unit/test_qwen_gguf_host.py -n 0`.
  - [x] Prepare ABI input from the existing normalized source WAV, not
    PyTorch prompts. Keep clone IDs/profile ownership stable and map
    native speaker casing centrally. Release derived native buffers when
    evicted or the host closes; retain original enrollment files.
  - [x] Green: migration, missing-reference and invalid-capability tests
    pass without exposing ineffective instruction controls. Commit
    `feat(qwen): reuse source clones across Qwen model formats`.

## Phase 5: Expose selection and readiness across the application

- [x] Task 5.1: Wire variant-aware controller state and installation

  **Files:** Modify `ui/controller.py`, `tests/unit/test_controller.py`;
  add `tests/unit/test_qwen_variant_controller.py`. Keep new manifest/
  selection logic in its core modules, not additional copies in controller.

  **Interfaces:** expose `qwenModelFormat`, `qwenGgufQuantization`,
  `qwenEngineLabel`, `qwenVariantOptions`, and `setQwenVariant(format,
  quantization)`. Existing readiness/submission methods resolve the whole
  selected variant. Installation operations capture their target identity.

  - [x] Red: official→GGUF→other quantization→official switches preserve
    independent device preferences, change the engine only when idle, and
    ignore stale readiness/install results. Test unsupported hardware,
    missing runtime/model, busy/queued/cancelling refusal, and no fallback.
  - [x] Run `.venv/bin/pytest tests/unit/test_qwen_variant_controller.py tests/unit/test_controller.py -n 0`.
  - [x] Resolve Auto from the selected engine's validated available
    backends; expose the actual selected device. Reuse injectable
    background runners for install/probe work with generation guards.
    Build one immutable submission context and factory decision per job.
  - [x] Green: status, Generate gating, engine factory and job identity
    agree in every tested state; QML-facing byte counters remain
    `qlonglong`. Commit `feat(qwen): expose variant-aware readiness and installs`.

- [x] Task 5.2: Add shared model-format controls and localized install UI

  **Files:** Create `ui/qml/components/QwenVariantPicker.qml`,
  `ui/qml/components/QwenInstallCards.qml`; modify
  `ui/qml/components/EngineProfilePicker.qml`, `ui/qml/SettingsTab.qml`,
  `ui/qml/EngineState.qml`, `ui/qml/qmldir`, and
  `ui/i18n/vienetts_en.ts`/`.qm`; update `tests/smoke/test_ui_tabs.py`.

  **Interfaces:** shared picker binds only to Task 5.1 properties; install
  cards consume controller status and operation seams. Preserve existing
  official-path objectNames; add stable names for format and quantization.

  - [x] Red: consolidated offscreen scenarios select Base/CustomVoice,
    format, quantization and device; assert Official full weights wording,
    compatible engine readout, GGUF-only quantization visibility, all
    readiness/error/install actions, busy gating and locale changes.
  - [x] Run `.venv/bin/pytest tests/smoke/test_ui_tabs.py -n 0`.
  - [x] Use existing AppCombo/AppCard/EngineState conventions and declare
    delegate requirements on their roots. Extract only affected install
    presentation from the already-large Settings tab. Show per-variant
    and shared storage accurately; never suggest installing PyTorch for
    GGUF. Regenerate the English catalog through the project workflow.
  - [x] Green: source/fake UI scenarios pass, including automated checks
    at 640×420 and larger windows, focus navigation and both locales. Commit
    `feat(ui): add Qwen format and GGUF quantization controls`.

- [ ] Task 5.3: Complete all surface and Studio variant flows

  **Files:** Modify `ui/controller.py`, `ui/audiobook_controller.py`,
  `ui/batch_controller.py`, `ui/subtitle_controller.py`,
  `ui/qml/StudioTab.qml` and shared surface controls only where required;
  update related controller tests and `tests/smoke/test_e2e_flows.py`.

  **Consumes:** the immutable context, shared picker, single-owner provider.
  **Produces:** no new routing contract; existing listener/submission
  seams carry the resolved variant end to end.

  - [ ] Red: one consolidated driver covers Text/audition, Paragraph,
    Batch, Audiobook, Subtitle and Studio with both GGUF quantizations;
    test cache invalidation, exports, preview, clone selection and exact
    Studio switch offers. A missing historical build cannot silently use
    the current one.
  - [ ] Run `.venv/bin/pytest tests/unit/test_audiobook_controller.py tests/unit/test_batch_controller.py tests/unit/test_subtitle_controller.py tests/unit/test_studio_controller.py tests/smoke/test_e2e_flows.py -n 0`.
  - [ ] Route all jobs through the existing listener and submission gate.
    Carry provenance through chapter/cue/batch artifacts and Studio clip
    state. Restore profile+format+quantization explicitly, disarm stale
    re-synthesis offers, and explain unavailable recorded identities.
  - [ ] Green: cross-surface cancellation, variant switch, replay/export,
    and cache reuse tests pass with one worker/model owner. Commit
    `feat(qwen): preserve GGUF provenance across synthesis surfaces`.

## Phase 6: Package, validate, and document release support

- [ ] Task 6.1: Wire frozen host dispatch and package contracts

  **Files:** Modify `src/vienetts_app/__main__.py`,
  `packaging/vienetts-app.spec`, `tests/unit/test_package.py`;
  create `tests/unit/test_qwen_gguf_packaging.py`.

  **Interfaces:** `--qwen-gguf-host` enters the lightweight child before
  GUI initialization, in source and frozen modes. Package the app-owned
  Python host/binding/manifests, not external native packs or model weights.

  - [ ] Red: test dispatch without GUI startup, source/frozen command
    construction, required package data, dependency exclusions, windowless
    Windows startup, and recoverable missing-library/ABI errors.
  - [ ] Run `.venv/bin/pytest tests/unit/test_qwen_gguf_packaging.py tests/unit/test_package.py -n 0`.
  - [ ] Add early dispatch and manifest/host inclusion. Preserve existing
    `--qwen-host` behavior and stdout handling; locate native libraries by
    verified runtime result, never cwd/PATH. Exercise clean frozen builds
    on each supported OS family.
  - [ ] Green: package contract tests pass and actual frozen smoke
    evidence is recorded separately from mocked dispatch. Commit
    `build(qwen): package the isolated GGUF host entry point`.

- [ ] Task 6.2: Gate real-model compatibility and lifecycle evidence

  **Files:** Create `scripts/qwen_gguf_release_smoke.py`,
  `.github/workflows/qwen-gguf-runtime-smoke.yml`,
  `tests/unit/test_qwen_gguf_release_smoke.py`; update
  `docs/performance/qwen-gguf-compatibility.md`.

  **Interfaces:** opt-in runner consumes pre-provisioned verified packs,
  invokes the app's installers/adapter, and emits structured per-cell
  evidence with exact identities, verdicts and resource measurements.

  - [ ] Red: fake runner tests reject missing packs, missing matrix cells,
    wrong identities, omitted profiles/quantizations, empty/non-finite
    audio and unrun results passed off as success. Check workflow matrix
    generation rather than referring to `matrix` in a job-level `if`.
  - [ ] Run `.venv/bin/pytest tests/unit/test_qwen_gguf_release_smoke.py -n 0`.
  - [ ] Implement the 24-combination sweep through the production path.
    Validate clone/fixed-speaker behavior, language mappings, native
    streaming, cancellation/restart, offline operation, repeated jobs,
    bounded RSS and actual device use. Test unsupported backend refusal
    and long-input segmentation. Ordinary CI executes only fake fixtures.
  - [ ] Green: all required real cells pass or remain explicitly blocked;
    never close this task with pending evidence. Commit runner and
    verified evidence incrementally with
    `test(qwen): add GGUF release compatibility gates`.

- [ ] Task 6.3: Finish documentation and acceptance review

  **Files:** Modify `README.md`, `conductor/product.md`,
  `conductor/tech-stack.md`; update this track's learnings, compatibility
  evidence, and plan only after their corresponding work is verified.

  **Consumes:** actual implementation and Task 6.2 evidence.
  **Produces:** installation/troubleshooting documentation and a recorded
  AC-1…AC-12 acceptance review with any blockers still open in Beads.

  - [ ] Check every acceptance criterion against tests and actual evidence.
    Search for obsolete FP16 wording, unsupported platform claims, and
    references to moving download revisions.
  - [ ] Document format/engine/device distinctions, paired quantization,
    runtime/model disk costs, offline imports, license notices, clone
    portability, provenance rules and native-dependency recovery.
    Performance claims must identify the measured device/build/variant.
  - [ ] Run full ruff/format/pytest gates and changed-code coverage.
    Run automated QML scenarios for both locales and all synthesis surfaces. Record
    device-dependent failures separately under the repository policy.
  - [ ] Close implementation Beads only when their work and evidence are
    complete; promote reusable learnings. Commit
    `docs(qwen): document verified GGUF engine support`.

## Acceptance coverage

| Criteria | Tasks |
| --- | --- |
| AC-1, AC-2 | 2.1, 5.1, 5.2 |
| AC-3, AC-4 | 1.2, 3.1, 3.2 |
| AC-5, AC-9 | 1.1, 4.3, 6.2 |
| AC-6, AC-7 | 4.1, 4.2, 6.2 |
| AC-8 | 2.2, 5.3 |
| AC-10 | 5.1, 5.2, 5.3 |
| AC-11 | 1.2, 6.1 |
| AC-12 | 6.2, 6.3 |

## Handoff

Track creation does not implement these tasks. Start with
`/conductor-implement qwen_gguf_engine_20260923`. Resume the first ready
Bead and keep the selected sequential execution order. Do not confuse the
archived official-Qwen probe backlog with this track's native evidence.
