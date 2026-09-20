# Track Learnings: qwen_multiengine_20260920

Patterns, gotchas, and context discovered during implementation.

## Codebase Patterns (Inherited)

`conductor/patterns.md` contains 113 inherited project patterns. The patterns
most relevant to this track are:

- Quality gate per task: `.venv/bin/ruff check .`, `.venv/bin/ruff format
  --check .`, and `.venv/bin/pytest`; commit with a conventional prefix and a
  git-note task summary.
- Engine ownership is single-owner: one model owner serializes requests, and
  model/audio stacks stay off the GUI/startup path.
- Immutable job IDs and tagged terminals are the routing contract; stale
  deliveries are dropped and every admitted job settles exactly once.
- Artifact-first synthesis writes `<job>.part.wav` and atomically promotes;
  bounded live preview carries PCM without duration-sized accumulation.
- Managed installs are checksum-pinned, staging-only, resumable, and offline
  after install; QML-facing byte counts use `qlonglong`.
- Cache fingerprints include every input that changes rendered audio; corrupt
  sidecars degrade to cache misses rather than crashes.
- Context properties require Python lifetime anchors; heavyweight one-shot
  work uses injectable background runners with stale-result guards.
- QML smoke drivers share one `QGuiApplication`, use real NOTIFY properties on
  fakes, and consolidate scenarios per subprocess.
- Repeater/ComboBox delegates declare required `modelData`/`index` properties
  on the delegate root.
- Parallel workers own disjoint files; commits and shared UI/i18n copy remain
  serialized by the lead.

---

<!-- Learnings from implementation will be appended below -->
## [2026-09-20] - Phase 0 Tasks 0.1-0.3: audit, probe, matrix
- **Implemented:** `docs/performance/qwen-port-audit.md` (26-commit / 70-file salvage
  matrix); `scripts/spike/qwen_runtime_probe.py` + 15 tests; `packaging/qwen-runtime-requirements.json`
  + `docs/performance/qwen-runtime-compatibility.md` + 7 gating tests.
- **Files changed:** docs/performance/qwen-port-audit.md, scripts/spike/qwen_runtime_probe.py,
  tests/unit/test_qwen_runtime_probe.py, packaging/qwen-runtime-requirements.json,
  tests/unit/test_qwen_runtime_requirements.py, docs/performance/qwen-runtime-compatibility.md
- **Commits:** a13cee1, 01de651, 5cc57a5
- **Learnings:**
  - Patterns: a "one JSON result" CLI contract means usage errors must NOT go through
    argparse `choices` (that exits before the JSON contract) — validate in code and print
    the error payload. Round latency metrics to 3 decimals or sub-millisecond fakes round
    to 0.0 and break `> 0` assertions.
  - Gotchas: `peak_rss_mb` units differ by platform (macOS bytes vs Linux KB) — reuse the
    `sys.platform` branch; `ru_maxrss` is process-wide, so tests must inject it.
  - Context: real evidence gathered without a Qwen device — torch/torchaudio 2.8.0 wheels
    exist for cp310-cp313 on win_amd64, manylinux_2_28_x86_64 (cpu+cu128) and macOS
    `macosx_11_0_arm64` (PyPI only, no `+cpu` variant); `qwen-tts==0.1.1` hard-pins
    transformers==4.57.3 and pulls gradio/librosa/onnxruntime/sox; model repos pinned at
    `85e237c1…` (CustomVoice) and `5d839924…` (Base) with 683 MB of shared tokenizer files.
  - Blocked: the six real-device probe runs need release hardware; the matrix was locked
    with `evidence.status = pending` and a test now refuses a silent reduction.

## [2026-09-20] - Phase 1 Tasks 1.1 + 1.2: engine profiles and synthesis context
- **Implemented:** `core/engine_profiles.py` (EngineCapabilities/LanguageOption/VoiceOption,
  `validate_selection`, `model_tag`), `core/synthesis_context.py` (GenerationSettings,
  SynthesisContext, `fingerprint_payload()`/`fingerprint()`, `context_for`),
  `Settings.engine_profile`/`qwen_device` with per-field clamping, `TTSRequest.context`
  with contradiction checks, `SynthesisJob.context` + `new_synthesis_job(context=...)`.
- **Files changed:** core/engine_profiles.py, core/synthesis_context.py, core/models.py,
  core/settings.py, core/jobs.py + 5 test files
- **Commit:** 88feee2 (Tasks 1.1 + 1.2 in one commit)
- **Learnings:**
  - Patterns: generation bounds (`temperature`/`speed`/`silence_p`) now live in
    `synthesis_context` and `core.models` imports them — one source of truth, no drift
    between Settings validation and context validation.
  - Gotchas: settings restore is all-or-nothing (`Settings(**data)`), so a *stale engine
    field* would wipe every other setting — clamp the engine fields individually in
    `load_settings` instead of widening the fallback.
  - Gotchas: `model_tag` for Qwen returns the raw pinned revision (the profile is recorded
    separately); an `profile@revision` compound tag would duplicate identity in fingerprints.
  - Context: Tasks 1.1 and 1.2 cannot be split into two green commits — `core.models`
    imports both modules, so the pair lands as one commit and both plan checkboxes move
    together.

## [2026-09-20] - Phase 2 Tasks 2.1 + 2.2: shared install primitives, Qwen runtime manifests
- **Implemented:** `core/managed_install.py` gained the shared wheel-archive primitives
  (`RuntimeWheel`, `wheel_member_destination`, `validate_wheel_layout`,
  `extract_wheel_archive`, `download_wheel_archive`) with `CudaRuntimeManager` and
  `ModelManager` delegating to them; `core/qwen_runtime_manifest.py` (validated,
  data-driven manifests), `core/qwen_runtime.py` (staged/resumable/repairable/offline
  installer), `scripts/lock_qwen_runtime.py` (maintainer re-lock), and
  `src/vienetts_app/core/qwen_runtime_manifests.json` (87-102 pinned wheels per platform).
- **Files changed:** src/vienetts_app/core/managed_install.py, cuda_runtime.py,
  cuda_runtime_manifest.py, qwen_runtime_manifest.py, qwen_runtime.py,
  qwen_runtime_manifests.json, scripts/lock_qwen_runtime.py,
  tests/unit/test_qwen_runtime_manifest.py, test_qwen_runtime_manager.py,
  test_qwen_runtime_lock.py
- **Commits:** 6b8845a (2.1), <2.2 commit>
- **Learnings:**
  - Patterns: manifest data for a full closure belongs in a generated JSON *inside* the
    package (loaded next to the module, bundled in the Phase 6 packaging task) rather than
    in 4000 lines of Python; the loader returns no manifests at all when the data file is
    missing or invalid, so a frozen build degrades to "runtime unavailable" instead of
    importing unverified wheels. Tests are the thing that makes the shipped data file
    fail loudly.
  - Patterns: the maintainer lock resolves cross-platform with
    `uv pip compile --generate-hashes --python-platform <target>` (metadata only, ~5 s per
    platform, no wheel downloads) and then re-derives URL+size per artifact, accepting a
    file only when its SHA-256 is in the resolver's digest set. PyPI JSON supplies
    size+digest without downloads; the PyTorch index HTML supplies `#sha256=` fragments but
    links point at the `download-r2` CDN, so the href is re-hosted on the index host and
    the digest is what actually pins the artifact.
  - Gotchas: (1) `pip`/`uv` cannot express "install this for macOS arm64 without an OS
    version" — scipy/torch publish several `macosx_1x_0_arm64` wheels, so the lock prefers
    the *lowest* deployment target (widest compatibility) and rejects free-threaded
    `cp3XXt` ABIs outright; (2) `sox` (declared by qwen-tts) and `brotli` (on macOS) ship
    sdists only — the runtime is wheel-only, so they are recorded in `sdistOnly` with a
    reason instead of silently disappearing (Task 3.2 must confirm the host never imports
    them); (3) `uv` needs `--index-strategy unsafe-best-match` to see `+cpu`/`+cu128` local
    versions across the PyTorch and PyPI indexes.
  - Context: pinned closure sizes are 430 MB (linux cpu), 4.16 GB (linux cu128), 273 MB
    (macOS arm64), 830 MB (windows cpu), 3.67 GB (windows cu128); the installer preflights
    2x the wheel bytes because archives stay on disk while they are expanded.
  - Verification: full gate green (1208 passed, 1 documented device-less Qt audio smoke
    failure); 93 CUDA/model/managed-install tests still pass after the primitive extraction.

## [2026-09-20] - Phase 2 Task 2.3: Qwen model manifests and shared profile installs
- **Implemented:** `core/qwen_model_manifest.py` (validated CustomVoice/Base profiles with a
  shared tokenizer set), `core/qwen_model_manager.py` (staging-only per-profile installer with
  shared-file reuse, repair, offline packs, in-use refusal), `scripts/fetch_qwen_models.py`
  (maintainer lock), and `src/vienetts_app/core/qwen_model_manifests.json`.
- **Files changed:** src/vienetts_app/core/qwen_model_manifest.py, qwen_model_manager.py,
  qwen_model_manifests.json, scripts/fetch_qwen_models.py, tests/unit/test_qwen_model_manifest.py,
  test_qwen_model_manager.py, test_fetch_qwen_models.py, packaging/qwen-runtime-requirements.json
- **Commits:** <2.3 commit>
- **Learnings:**
  - Patterns: the HF API (`/api/models/<repo>/revision/<rev>?blobs=true`) publishes sizes for
    every file and SHA-256 only for LFS blobs; the lock hashes the handful of small non-LFS
    files (config/vocab/merges, ~9 MB per re-lock) so every pin is digest-backed. A file is
    *shared* only when path, size and digest all match between the two revisions — that rule
    reproduces the Phase 0 tokenizer list automatically (plus `generation_config.json`, which
    Phase 0 had missed).
  - Gotchas: (1) the Phase 0 aggregate `sharedBytes` (683,056,438) and repo totals were wrong
    because the API listing used there omitted `.gitattributes`/README accounting; the
    per-file manifest is now authoritative and the JSON carries corrected figures plus
    `bytesExcludingGitMetadata`; (2) `.gitattributes` is recorded as excluded with a reason
    rather than dropped silently; (3) a shared file already promoted to `<root>/shared` must be
    checked *before* the staging path, otherwise the second profile re-downloads 686 MB.
  - Context: per-profile pins are 1.81 GB (CustomVoice) and 1.83 GB (Base) of own weights plus
    686 MB shared; the installer reserves 256 MB of headroom because the previous install
    survives until promotion succeeds.
  - Verification: full gate green (1231 passed, 1 documented device-less Qt audio smoke
    failure); 23 new tests cover independent profile lifecycle, shared-file retention,
    corruption/repair, free-space refusal, offline packs and promotion rollback.

## [2026-09-20] - Phase 3 Task 3.1: framed IPC protocol
- **Implemented:** `core/qwen_protocol.py` — versioned, job-tagged frames
  (`hello`/`load`/`capabilities`/`synthesize`/`pcm`/`progress`/`cancel`/`terminal`/`error`/
  `shutdown`) with `header_len|header|payload_len|payload` framing, bounded float32 PCM
  payloads, per-type field validation, and `SessionState` transition checks for both ends.
- **Files changed:** src/vienetts_app/core/qwen_protocol.py, tests/unit/test_qwen_protocol.py
- **Commits:** <3.1 commit>
- **Learnings:**
  - Patterns: only `pcm` frames may carry a payload (checked before field validation), so a
    confused peer cannot smuggle bytes through a control frame; `EndOfStream` at a frame
    boundary is distinguished from `ProtocolError` mid-frame, which is what lets the parent
    adapter treat a clean host exit differently from a truncated pipe.
  - Gotchas: transitions need *two* directions — `accept()` for received frames and
    `record_sent()` for outgoing ones, otherwise a parent session cannot know that its own
    `synthesize` opened the job and would reject the host's first `pcm` frame as "not
    running". `StaleFrameError` (a `ProtocolError` subclass) is the signal Task 3.3 drops
    silently instead of failing the job.
  - Verification: full gate green (1253 passed, 1 documented device-less Qt audio smoke
    failure); 22 new tests cover partial reads, oversized declarations, malformed JSON,
    unknown versions, job-tag rules, payload rules, binary round-trips, and both roles'
    transition violations.

## [2026-09-20] - Phase 3 Task 3.2: the isolated Qwen model host
- **Implemented:** `workers/qwen_host.py` — the child half of the framed protocol: merged
  load view (hard link → symlink → copy), offline local-only loader, model-reported
  capabilities, CustomVoice/Base generation with a cached clone prompt, one stateful
  24→48 kHz resampler per job, bounded `pcm`/`progress` emission, job-keyed cancellation,
  JSON logs on stderr only, and a reader thread so `cancel` is visible during generation.
- **Files changed:** src/vienetts_app/workers/qwen_host.py, tests/unit/test_qwen_host.py
- **Commits:** <3.2 commit>
- **Learnings:**
  - Patterns: `Qwen3TTSModel.from_pretrained` resolves the text tokenizer, the generation
    config **and** the hardcoded `speech_tokenizer/` subfolder from one path, and there is no
    argument for a second directory — so the host merges the managed trees into a link view
    (`<model root>/.load/<profile>`) instead of copying 686 MB. Hard links keep it free and
    same-filesystem by construction; the view is rebuilt idempotently and removed on close.
  - Patterns: cancellation needs a second thread. The main thread is inside a blocking
    `generate_*` call, so a reader thread does `SessionState.accept()` (keeping transition
    violations synchronous) and records cancel ids; the job loop polls a job-keyed set
    between resample chunks. Cancel *for a settled job* surfaces as `StaleFrameError` and is
    logged and dropped, which is exactly the 3.1 contract.
  - Gotchas: (1) `MAX_HEADER_BYTES` is 65536, so a declared length of exactly 65536 is
    *valid* and the reader blocks waiting for the rest of the frame — the "oversized frame"
    test needs 65537; (2) emitting `final` on the last pcm frame needs a one-chunk lookahead
    when streaming resampled audio, otherwise the flag can only be set by buffering the whole
    segment; (3) `_validate_audio` guarantees ≥1 sample and the resampler always returns ≥1
    sample for ≥1 input sample, so an "empty segment" fallback is unreachable — do not carry
    defensive branches that cannot be exercised.
  - Gotchas: the engine-profile validator is the single source of truth for messages: for
    Base the host passes the *reference clip path* as `clone_id`, which reuses the shared
    "needs an enrolled clone" wording for a missing clip and keeps the "use VieNeu for
    Vietnamese" hint identical to the app's.
  - Context: a device/OOM failure is fatal by design (settle the job `failed`, emit
    `fatal: true`, exit 1) so the parent restarts a clean interpreter rather than retrying on
    a poisoned accelerator context; protocol violations exit 2; peer close and `shutdown`
    exit 0. `qwen_tts`/`torch` are imported only inside the loader, so the app and the suite
    stay torch-free (verified: neither package is installed in the dev venv).
  - Verification: full gate green (1318 passed, 1 documented device-less Qt audio smoke
    failure); 65 new tests cover the resampler (ported, chunked == one-shot), the load view
    and its link fallbacks, capability narrowing/fallback, load validation and swap, every
    synthesize terminal path, and the frame loop end to end over real pipes (handshake, load,
    synthesize, cancel, stale cancel, protocol violation, malformed bytes, peer close, fatal
    exit); 97% line coverage on the new module.
  - Housekeeping: `metadata.json`'s `beads_tasks` mapping was off by one phase (phase1→nqx.2,
    phase3→nqx.4) and lacked phase 0; it now matches the real hierarchy (phase0→nqx.2 …
    phase7→nqx.9). Phase 3 tasks are `nqx.5.x`.

