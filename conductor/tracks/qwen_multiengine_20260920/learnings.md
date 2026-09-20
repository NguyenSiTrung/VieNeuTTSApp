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


## [2026-09-21] - Phase 3 Task 3.3: the parent Qwen adapter and lifecycle
- **Implemented:** `core/qwen_engine.py` — the app-side owner of the isolated host: spawn,
  handshake, load, stream, cancel, close. `initialize()`/`capabilities()`/`infer_stream()`/
  `cancel()`/`close()` are the whole public surface.
- **Files changed:** src/vienetts_app/core/qwen_engine.py, tests/unit/test_qwen_engine.py
- **Commits:** `c348855`
- **Learnings:**
  - Patterns: the reader thread must own `SessionState.accept()` (not the caller), because the
    calling thread is blocked inside a generator waiting for the next frame; the queue is the
    hand-off. Keeping `accept()` on the reader is what makes "a late frame for a settled job"
    a `StaleFrameError` drop instead of a corrupted next job.
  - Patterns: a per-spawn **generation counter** is the cheapest guard against the nastiest
    cross-generation bug — an orphaned generator that times out after its host died would
    otherwise `_abort_host()` and kill the *fresh* host a later `initialize()` just started.
    `_publish_closed`/`_mark_dead`/`_abort_host` all no-op when the generation moved on.
  - Patterns: `cancel()` must remember requests for jobs that have not reached the host yet
    (`_cancel_requests`, bounded like the host's retired-id registry) — the UI can cancel a job
    the worker has not submitted. The stream consumes that memory right after sending
    `synthesize`, so a pre-start cancel still ends as `QwenEngineCancelled`.
  - Gotchas: (1) `close()` must join the reader/stderr threads *and* close the pipes before
    dropping `self._process`, otherwise `_close_pipes()` silently early-returns and the pipes
    are only reclaimed by the GC; (2) `_wait_settled` has to break out when the host is no
    longer ready, or a cancel on a host that already crashed waits the full cancel timeout for
    a terminal that can never arrive; (3) `sys.exit()` inside a *thread* only ends that thread —
    the fake host needs `os._exit()` to model a real crash, which is exactly what the adapter's
    "partial PCM then raise" path must survive.
  - Gotchas: a crash's last stderr line is the most useful diagnostic, but the reader can see
    stdout EOF before the stderr drain thread appends it. The reader therefore joins the drain
    thread (bounded 0.5 s) on the clean-EOF path before publishing, which makes the
    "boom: the host died mid-job" text deterministic in the error message instead of a race.
  - Patterns (testing): a scripted stand-in host driven by a mode argument, using the *real*
    `qwen_protocol` module and logging every received frame as JSON lines to `$FAKE_HOST_LOG`,
    makes the whole lifecycle deterministic and torch-free: hangs, crash, OOM (fatal + exit),
    garbage bytes, protocol violation, stale delivery, SIGTERM-ignoring host, and process
    liveness (`os.kill(pid, 0)`) assertions. The stand-in must call `SESSION.accept(frame)` in
    its loop, otherwise its own `record_sent()` rejects the `pcm` frames it is supposed to emit.
  - Verification: full gate green (1357 passed, 1 documented device-less Qt audio smoke
    failure); 39 new tests, 96% line coverage on the new module (the 17 uncovered lines are
    platform/defensive guards: Windows `CREATE_NO_WINDOW`, generation guards, unreachable
    `None` checks).


## [2026-09-21] - Phase 3 Task 3.4: Qwen routed through the worker/artifact pipeline
- **Implemented:** two slices. (1) `core/text_segmentation.py` takes the pure segmentation
  rules out of `core/engine.py` (which re-exports the same names) and adds
  `split_text_for_profile(text, language, max_chars)`, `segment_limit_for(profile)` and the
  CJK boundary rule. (2) The engine-provider seam: `EngineProvider` / `VieNeuProvider` /
  `EngineProviders` / `EngineProviderError` in `core/engine.py`, `QwenEngineProvider` (plus
  `ClonePrompt`) in `core/qwen_engine.py`, and routing in `workers/inference_worker.py`.
- **Files changed:** src/vienetts_app/core/text_segmentation.py (new),
  src/vienetts_app/core/engine.py, src/vienetts_app/core/qwen_engine.py,
  src/vienetts_app/workers/inference_worker.py, tests/unit/qwen_host_fake.py (new shared
  scripted host), tests/unit/test_text_segmentation.py (new), tests/unit/test_engine.py,
  tests/unit/test_qwen_engine.py, tests/unit/test_inference_worker.py
- **Commits:** `c62759b`, `6356be5`
- **Learnings:**
  - Gotchas (segmentation): the space-only sentence regex requires *trailing whitespace*, so a
    Chinese paragraph used to be ONE unit that `_pack_units` then hard-split mid-sentence at
    the cap. The CJK rule (。！？； close a unit with no following space) fixes that; joining
    packed units with the separator the boundary actually consumed (`""` for CJK, the source's
    own space for Korean/English) is what keeps spacing byte-faithful instead of inventing a
    space between two Chinese sentences.
  - Gotchas (segmentation): `split_text_for_streaming`/`split_text_for_profile` PACK
    consecutive units greedily up to `max_chars` and return `[text]` when the whole text
    already fits — a two-sentence Chinese paragraph is ONE segment. Tests must assert
    "segments reassemble to the source" and "every segment ≤ the profile cap", never "one
    segment per sentence".
  - Patterns (routing): resolve the provider ONCE per job from `job.context` and *before*
    `IncrementalArtifactWriter` exists — that ordering is what makes an unroutable profile
    fail with an actionable `EngineProviderError` and leave no `<stem>.part.wav` behind.
    `EngineProviders` freezes `by_profile` into a `MappingProxyType`; a profile switch builds a
    NEW set, and a test that swaps `worker._providers` mid-job proves the running job keeps the
    provider it started with.
  - Gotchas (cancel): the provider's `cancel()` must be called OUTSIDE `_active_lock`. A Qwen
    cancel can block while the host settles the job, and the worker thread needs that same lock
    to reach its next chunk boundary. A provider exception raised while `_is_aborted()` is the
    job's *cancellation* (`_JobCancelled`), never a synthesis failure — that is exactly how
    "the host was terminated to stop the job" settles as a `cancelled` terminal.
  - Gotchas (Qwen provider): the host settles an id per terminal, so re-using one protocol job
    id for every segment makes the second segment's frames look stale; each segment gets
    `<worker job>:<n>` from a process-wide counter. And `QwenEngine.infer_stream` takes *Python*
    parameter names (`voice_prompt`, `ref_text`) — passing the protocol's camelCase fields
    (`voicePrompt`) fails silently at the adapter boundary.
  - Gotchas (locks): `QwenEngineProvider.cancel()` holds a non-reentrant `threading.Lock` while
    `_remember_pending()` re-acquires it — a straight deadlock, caught only by the
    "cancel lands before the first segment" test. Any private helper called with the lock held
    must document that contract.
  - Patterns (testing): the scripted fake host moved to `tests/unit/qwen_host_fake.py` so the
    worker tests can drive the *real* adapter end to end (valid WAV artifact, graceful cancel
    keeps the host alive, OOM fails the job and the next job restarts the host) without torch.
  - Verification: ruff check + format clean; full gate `1424 passed` with the one documented
    device-less Qt audio smoke deselected (`VieNeuTTSApp-3iy`). New tests: 29 segmentation,
    13 provider-seam, 13 Qwen-provider, 12 worker-integration.


## [2026-09-21] - Phase 4 Task 4.1: the profile-scoped clone store
- **Implemented:** `core/voice_profiles.py` — one app-owned catalog of enrolled clones per engine
  profile (`CloneStore`, `CloneProfile`, `CloneStoreError`), plus the `prompt_for()` bridge that
  hands the Qwen model host a `ClonePrompt` (reference clip + transcript).
- **Files changed:** src/vienetts_app/core/voice_profiles.py (new, 280 stmts),
  tests/unit/test_voice_profiles.py (new, 44 tests)
- **Commits:** `0dd67f4`
- **Learnings:**
  - Patterns: put the spec's clone rules in the store, not in the callers. `clone_requirements`
    from `engine_profiles` capabilities drive transcript/consent enforcement (Base needs both,
    VieNeu needs no transcript, CustomVoice is refused outright), so no UI path can enroll a
    clone that the engine cannot rebuild — the same capability table the job validator already
    uses stays the single source of truth.
  - Patterns: dedup on `(profile, sha256(source bytes), transcript)`. The hash is of the *user's*
    file, so a re-encode never hides a duplicate, while a different transcript is legitimately a
    different clone (the prompt text differs). The reference copy is still per-clone, so removing
    one clone can never break another that happens to share the same clip.
  - Gotchas: `audio.write_wav_file` requires 1-D mono *and* infers the container from the file
    extension, so (a) stereo references must be downmixed with `read_wav` + `mean(axis=1)` before
    writing, and (b) the temp file for the atomic publish must keep a `.wav` suffix — a
    `.<name>.<uuid>.tmp` temp makes soundfile raise "No format specified".
  - Gotchas: a corrupt index must never be silently replaced. It is moved aside as
    `clones.json.corrupt` (metadata recoverable by hand), invalid *entries* are dropped with a
    warning, and reference audio is never deleted as a side effect of loading. Index paths are
    always re-resolved inside the store root, so a hand-edited absolute/escaping `reference`
    cannot point the model host at an arbitrary file.
  - Patterns (atomicity): reference copy first, index second; if the index write fails the copy is
    deleted, so there is no file for an unindexed clone. Removal is the mirror image: the index is
    committed before the audio is unlinked, so a failed unlink leaves a harmless orphan rather
    than a dangling entry. Both directions are covered by monkeypatched `os.replace`.
  - Gotchas: `os.replace` on a *globally* monkeypatched `os.replace` breaks the reference publish
    before the index write is ever reached; failure tests must key the failure on the destination
    (index vs reference) or they test the wrong branch.
  - Verification: ruff check + format clean; full gate `1468 passed` with the documented
    device-less Qt audio smoke deselected; 100% line coverage of the new module (a dead
    `except CloneStoreError` guard inside `_write_reference` was removed rather than left
    untested).
