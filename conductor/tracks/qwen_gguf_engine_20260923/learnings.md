# Track Learnings: qwen_gguf_engine_20260923

## Codebase patterns inherited

Sources: `conductor/patterns.md` and
`conductor/archive/qwen_multiengine_20260920/learnings.md`.

- Preserve one capability table and one submission gate. Model format and
  runtime are not new semantic voice profiles.
- One worker owns one resident engine. Attach listener controllers for
  Batch/Audiobook/Subtitle instead of creating another worker.
- Verified installs stage, verify every file, and atomically promote.
  Model/runtime installs remain separate from the base dependency lock.
- Immutable context travels with jobs, artifacts, caches and Studio clips.
  Missing identity is not permission to reuse an arbitrary engine's audio.
- Base clones persist source audio, transcript and consent, not tensors.
- Artifact-first audio and bounded preview avoid duration-sized buffers.
- Existing `StreamingResampler` is NumPy-only and stateful; share it rather
  than creating another subtly different 24→48 kHz conversion.
- GUI callbacks must not wait for host cancellation/reaping. Preserve
  cancel-before-pickup guards and drop stale frames after terminal delivery.
- Parent writes must remain SIGPIPE-safe. On Windows, pipe readers own
  closure and are joined before streams close.
- Windows process liveness uses `core/processes.py`, never `os.kill(pid, 0)`.
- Keep stdout exclusively for protocol frames, including native-library
  logging. Diagnostics must not contain user text or reference content.
- Use queued background results with selection-generation guards.
- QML byte counts use `qlonglong`; delegates declare required properties
  on their root; shared controls preserve tested objectNames.
- Consolidate fake-host e2e scenarios per subprocess. Ordinary CI never
  downloads weights; real release evidence is an independent obligation.
- Full gates precede local task commits; attach git notes. No automatic
  fetch/pull/push or Dolt remote sync.

## Planning research (2026-09-23)

- The requested model repository contains paired talker/tokenizer GGUFs
  with Q8_0 and Q4_K_M for both 0.6B profiles. Tokenizer sharing must be by
  immutable artifact identity and quantization, not filename alone.
- The model card swaps Base/CustomVoice use-case descriptions in one
  table. The runtime README explicitly uses Base for reference cloning
  and CustomVoice for named speakers, matching the app. Verify the pinned
  code and models before claiming compatibility.
- The current runtime README advertises a C ABI (`qt_*`), shared-library
  builds (`QWEN_SHARED=ON`), streaming callbacks and reference extraction.
  HEAD documentation is not a pinned ABI or verified Windows/macOS pack.
- The README's `qwen.h` example does not establish the header's repository
  location: a direct root-level lookup returned 404. Phase 1 must inspect
  the pinned source tree, not invent ctypes layouts from prose examples.
- Runtime documentation mentions more languages than the app's current
  ten-language table. This track does not expand languages based on that
  claim; it validates mappings and represents unsupported ones honestly.
- The user approved retaining actual official precision (CPU/MPS FP32,
  CUDA BF16) and labeling the format Official full weights.
- The user explicitly excluded per-phase manual verification. Automated
  task gates and scripted real-model release checks remain required;
  do not insert Conductor manual-verification checkpoints.
- Native Metal is not PyTorch MPS. Selection, protocol, diagnostics, and
  readiness must distinguish backend identity even on the same device.
- Recent local commits fix GUI-blocking cancellation and host footprint
  growth. These are regression constraints for native integration, not
  optional future optimizations.

## Implementation discoveries

### Task 1.1 — native contract audit + real-model probe (2026-09-23)

- `src/qwen.h` is the real ABI home (a root-level `qwen.h` 404s). ABI 5/5,
  C99 structs, `extern "C"`; only `QT_API` symbols export. Shared lib needs
  `-DQWEN_SHARED=ON` → `libqwen.so`/`qwen.dll`/`libqwen.dylib`.
- **Backend discovery is the sharp edge.** With `GGML_BACKEND_DL=ON`,
  `qt_init` → `ggml_backend_load_all()` searches compile-time
  `GGML_BACKEND_DIR`, the *host executable's* dir, then *cwd* — never the
  `libqwen.so` dir. `dlopen` from another dir fails with
  `backend_init failed (no GGML backend available)`. The GGUF host subprocess
  must spawn with `cwd=<pack dir>` (recorded under `backendDiscovery` in the
  requirements JSON; verified on linux-x64-cpu).
- Linux pack = `libqwen.so` + SONAME'd `libggml.so.0(.23.0)` +
  `libggml-base.so.0(.23.0)` + 13 ISA-variant `libggml-cpu-*.so` modules.
  `libgomp.so.1` (OpenMP) is a NEEDED dep of `libggml-base` and every cpu
  module — must be shipped or floored per platform.
- Cancellation is better than the header suggested: `qt_cancel_cb` polls
  during prefill AND per decode step; a 300 ms cancel inside ~20 s of base
  prefill still ended `cancelled` with 0.2 ms abort latency. `on_chunk`
  returning false is equivalent.
- Streaming emits mono float32 @ 24 kHz on the library's internal worker
  thread; chunks are 1920-sample (80 ms) units, width doubles to 8 frames,
  EOS flushes the remainder. Copy before returning from the callback.
- Base vs CustomVoice tables differ on the real models: `custom_voice`
  carries 12 language rows (10 + beijing/sichuan dialects) and 9 speakers;
  `base` carries 10 language rows and 0 speakers. Take capabilities from
  `qt_language_name`/`qt_speaker_name`, never a hardcoded table.
- `qt_extract_voice_ref` on a 17.3 s 22.05 kHz clip: 2.4–2.6 s one-time,
  returns 1024-dim spk emb + 215-frame × 16-codebook latents, reusable per
  clone source.
- CPU throughput is below realtime on this host (18-core x86_64):
  CustomVoice RTF ≈ 2.6 both quants; Base RTF ≈ 6.8–7.1 (ref-latent prefill
  makes TTFA ≈ 20 s). Peak RSS 2.6–4.3 GB. Settings needs a CPU-throughput
  warning; this is a UX input for Phase 5.
- `qwentts.cpp` repo ships `examples/freeman.wav` + `freeman.txt` —
  canonical Base reference clip for probes.
- Probe runs `qt_synthesize` twice when `--check-cancel`: one measured pass,
  one cancelled pass — keeps timing metrics and cancel evidence independent.
- HF LFS resume (`curl -C -`) can interleave with a concurrent fresh `-o`
  download and produce an oversized corrupt file — always SHA-256 verify
  against the manifest (all six files verified at the pinned revision).

### Task 1.2 — reproducible native runtime packs (2026-09-23)

- `libqwen.so` builds with an **absolute build-tree RUNPATH** — staging must
  rewrite it (`patchelf --set-rpath '$ORIGIN'` on Linux, `install_name_tool
  -add_rpath @loader_path` on macOS) or the pack cannot find its ggml deps
  once relocated. Verified: `ldd` from a foreign cwd resolves every dep from
  the pack dir.
- **Deployment floor = build host's toolchain, not the runner label.** The
  local Ubuntu 24.04 build needs GLIBC_2.38 — a bare `ubuntu:22.04` container
  rejects it. `stage_pack` now measures the real floor from the pack binaries
  (`glibc_floor` → `BUILD-INFO.measuredGlibcFloor`) and CI builds on
  ubuntu-22.04 so the shipped floor is 2.35.
- `libgomp.so.1` is bundled, not floored: `stage_pack` copies the build
  host's `libgomp.so.1.0.0` + symlink and `gcc-*-base/copyright` (GCC Runtime
  Library Exception) into `licenses/`. A bare ubuntu:24.04 container with no
  gcc loads the pack and runs `qt_version` cleanly.
- Pack = flat dir: `libqwen` + SONAME'd ggml/ggml-base + 13 cpu-variant
  modules + bundled gomp + `licenses/` + `BUILD-INFO.json`. Symlinks are
  preserved through staging and recorded as `{path,target}` links (not
  digested) in the lock manifest.
- `lock_qwen_gguf_runtime.py` writes `packaging/qwen-gguf-pack-manifests.json`
  keyed by cell — a cell appears only after a real pack builds and verifies;
  `--check` re-verifies digests/sizes/symlinks for drift.
- Clean-env evidence: `docker run ubuntu:24.04` + mounted pack → 0
  unresolved NEEDED deps, `qt_version` prints `0cbde9b (2026-09-22)`, and a
  full `qwen_gguf_probe` against the *staged pack* synthesized audio
  (verdict pass, backend CPU).
- The opt-in build workflow mirrors qwen-runtime-smoke.yml: plan job shrinks
  the matrix from a `cells` dispatch input so GPU cells never queue on
  offline runners; artifact upload only, publication deferred.

## Track-creation validation (2026-09-23)

- Epic: `VieNeuTTSApp-ysl8`; six phases and 15 implementation tasks remain
  open. Parent relationships, sequential blockers, metadata mappings,
  acceptance coverage, relative links, and whitespace were validated.
- `ruff check .` passed. `ruff format --check .` failed on the unchanged
  `src/vienetts_app/core/text_metrics.py` regular-expression formatting.
- Full offscreen/ffmpeg pytest: 1,844 passed, two failed in 45.02 seconds
  with 14 xdist workers. `TestFrameLoop::test_fatal_error_stops_the_host`
  crashed its worker; the CLI import-check test expected exit 1 but got
  exit 0 with `ok=true`. These are observations, not diagnosed causes.
- Baseline follow-up: `VieNeuTTSApp-55ln`. No application or test files were
  changed. Planning files remain uncommitted because repository gates
  failed; no gate was bypassed and no remote synchronization was run.

## Task 2.1 — variant selection (2026-09-23)

- `qwen_variants.py` is pure data: frozen `QwenVariant` carries profile /
  model_format / quantization / engine / devices; `capabilities` delegates
  to `get_capabilities(profile)` so voice/language catalogs are never
  duplicated. `is_qwen_profile()` raises `EngineProfileError` on unknown
  ids — wrap it into `VariantError` so the module has one failure type.
- Engine-scoped device vocabularies differ by *name*, not just backend:
  official speaks `mps`, the native host speaks `metal`. Two separate
  settings fields (`qwen_device` vs `qwen_gguf_device`) keep each engine's
  remembered device — never share one field and translate.
- `resolve_variant()` must NOT pass the stored `qwen_gguf_quantization`
  into an official variant: it is an inactive preference. Passing it made
  `Settings(engine_profile=QWEN_BASE)` raise `VariantError` because
  official rejects any quantization. Gate it on format == gguf.
- Each new settings field clamps independently in `_clamp_engine_fields`;
  dropping a corrupt field pops it so the dataclass default applies —
  matches the existing `qwen_device` pattern. Old files without the fields
  load as official/Q8_0/auto with every legacy field intact.
- `test_saved_file_is_json_with_all_fields` asserts an exact key set —
  adding Settings fields requires updating it.

## Task 2.2 — provenance in SynthesisContext (2026-09-23)

- Variant identity lives as flat `""`-defaulted fields on SynthesisContext
  (`model_format`, `quantization`, `engine`, `resolved_device`,
  `runtime_identity`, `model_identity`, `tokenizer_identity`). Constructor
  normalization derives the Qwen triple via `variant_for`, so contradictory
  engine/format pairs are impossible to construct.
- Payload versioning is sparse: the `contextVersion: 2` block is emitted
  only when variant fields carry data (GGUF always; official only when a
  device/identity is stamped). VieNeu and unstamped-official contexts keep
  byte-identical v1 payloads → zero fingerprint invalidation for existing
  renders, and legacy Qwen payloads decode→normalize→re-serialize to the
  same v1 bytes (they ARE official renders — cache continuity is truthful).
- `context_from_payload` treats absent `contextVersion` as v1 and refuses
  any unknown version → an undecodable payload becomes "unknown identity",
  never a guessed GGUF. Variant keys on a v1 payload are ignored — no
  smuggling a format into a legacy record.
- `resolved_device`: "auto" is a selection, not a resolution → normalizes
  to "". Stamping a concrete device makes the context v2 — intended.
- `same_engine` = (profile, model_format, quantization, engine): a quant
  or format switch is a different engine for render-slot purposes.
  `context_matches` keeps full-payload equality including identities.
- Two gates refuse unservable engines: `submission_context_for` resolves
  the variant and refuses non-PyTorch engines with a localized error, and
  `EngineProviders.provider_for` refuses `context.engine not in
  ("", "pytorch")` — a GGUF context that bypassed the gate can never be
  silently served by the PyTorch provider.
- `jobs.py`/`models.py`/`studio.py` needed no code: the context object is
  opaque to them. The serialization seams are exactly audiobook
  `state.json` (`renders` map) and subtitle `project.json` (`context`).
- Follow-up for Phase 5: Studio's regen refusal names only the profile —
  a same-profile/different-format mismatch deserves a variant-aware label.

## Task 3.1 — Native runtime manifest + installer

- `QwenGgufRuntimePack` is the install contract per cell: `files[]`
  (path/size/sha256), `links[]` (SONAME chains), upstream pins, ABI, backends,
  declared dependencies, deployment floor. `identity` is content-addressed —
  upstream commit + ggml commit + cell + sha256 of the file/link table — so a
  toolchain change with the same pins still changes provenance.
- Manifest loader is as strict as the wheel manifest's: safe POSIX-relative
  paths only, 40-hex commits, links must resolve through the declared chain to
  a real file (two-pass: collect link names, then validate targets, then walk
  chains for cycles). Download URLs must be HTTPS from `PACK_DOWNLOAD_HOSTS`.
- `QwenGgufRuntimeManager` mirrors `QwenRuntimeManager` but stages plain
  files instead of wheel archives — `download_wheel_archive` is reused via a
  `RuntimeWheel` adapter (it's just name+url+size+sha), `promoted_install`
  handles rollback. No changes to `managed_install.py` were needed.
- Active dir is cell-scoped (`root/<cell>/<format>`): a CUDA host keeps the
  CPU pack too. `_install_guard` refuses packs for other platforms before any
  disk work — a Windows pack verifies byte-perfect on Linux but can't load.
- `install_from_offline_pack` accepts a directory OR a zip/tar archive (what
  a CI artifact download produces). Archive links are never trusted — only
  regular members are extracted, links are always recreated from the lock.
- `status()` verifies the whole tree (digests + link targets) and never
  loads the library — safe on the GUI thread. `verify_install` is the
  Phase 4 seam for a host-level load check.
- Empty `downloads` is truthful: `install_online` fails with "no published
  runtime pack" until publication is authorized.

## Task 3.2 — paired GGUF model installation

- `qwen_gguf_model_manifest.py` pins every file three ways: path, size+SHA-256,
  *and* the GGUF KV metadata it must declare (architecture, file_type,
  model_type, tokenizer_type, model_size, num_code_groups). A file whose
  bytes match but whose header lies can never satisfy the contract.
- `parse_gguf_metadata` decodes only the KV section via a `read(n)` seam and
  stops early once every wanted key is seen — verification touches the header
  of a 1 GB file, never tensor data. Bounds on KV count, string and array
  sizes reject hostile or truncated headers.
- The GGUF root is `<root>/<variant_key>/<talker>` plus `<root>/shared/<codec>`:
  one tokenizer per quantization serves both profiles. `remove(drop_shared)`
  deletes a codec only when no surviving `install.json` references its SHA —
  reference safety is keyed by content identity, not by which variant was
  installed last.
- `install_offline` sources copy only: `<src>/<key>/<talker>` +
  `<src>/shared/<codec>` with a strict allowlist — an unexpected file rejects
  the pack rather than being silently skipped. A user clone is never consumed.
- The downloader seam is `(url, target)` pointing at the pinned HF resolve
  URL; the default wraps `hf_hub_download` (redirects/resume/partials) so no
  PyTorch or qt dependency ever enters the model path. `DownloadCancelled`
  propagates through the seam to a clean `unavailable`, leaving staged
  partials for the next attempt to resume.
- `hf_hub_download(local_dir=...)` returns the canonical path; with
  `local_dir_use_symlinks=False` the staged file IS the destination, so the
  seam needs no copy step.
- The fetch script verifies each requirements pin (file/size/sha256/blobId)
  against the live `?blobs=true` listing — `blobId` is a top-level sibling
  field, `lfs.sha256` the digest. `.gitattributes` is listed without LFS
  metadata, so the listing parser stays lenient and `_verified_blob` enforces
  digests only for the six pinned files.
- Real-data smoke: the actual 1.28 GB base-Q8_0 pair installs via
  `install_offline` in ~3 s, tampers are detected, and repair re-verifies.

## Task 4.1 — Bind the native ABI and implement the child host

- `NativeQwenSession` (workers/qwen_gguf_abi.py) binds the audited ABI v5
  surface with `ctypes`. Callback objects (chunk/cancel/log) are retained for
  the whole native call; borrowed PCM buffers are copied inside the callback
  before it returns; exceptions can never cross a ctypes boundary — the relay
  captures them, returns a stop verdict, and the caller re-raises after
  `qt_synthesize` returns. `qt_audio_free`/`qt_voice_ref_free` are NULL-safe
  and run on every path including early validation exits.
- `StreamingResampler` moved verbatim to `core/streaming_resampler.py`;
  `qwen_host` re-imports it so the old import surface is unchanged. Chunked
  push output is bit-identical to a one-shot resample.
- `qwen_protocol` gained `format`/`quantization`/`runtimeDir`/`talkerPath`/
  `codecPath` for `format="gguf"` loads only — absent `format` keeps the
  official path byte-for-byte. Protocol stays v1; GGUF devices are
  `cpu`/`cuda`/`metal` (app `mps` is a parent-side translation, never on the
  wire).
- The GGUF host isolates stdout before `qt_init` — the real library chatters
  on load — and maps `device` to `GGML_BACKEND` (cpu→CPU, cuda→CUDA0,
  metal→Metal). A non-gguf load reaching it is a parent routing bug and
  fails loudly.
- Serve-loop ordering: the dispatch loop must emit `capabilities` for a
  pending load before accepting the next `synthesize`, or `SessionState`
  (correctly) rejects a job on an unloaded session.
- Real verification: `NativeQwenSession` + `QwenGgufHost` + a real
  subprocess round-trip all pass against the locked linux-x64-cpu pack and
  real CustomVoice Q4_K_M weights (39 bounded 48 kHz frames, `ok` terminal,
  stdout carries only frames).
- Known flake (pre-existing, filed separately): the serve-harness daemon
  reader can segfault an xdist worker at interpreter teardown; the file
  passes standalone. Not introduced by this task.

## Task 4.2 — Add parent lifecycle, routing, and resource bounds

- `QwenGgufEngine` subclasses `QwenEngine` after a narrow extraction:
  `_init_transport` holds the engine-neutral lifecycle state (pipes, inbox,
  session, generation guards, timeouts, footprint hooks) and three host-kind
  hooks — `_host_command`, `_host_environment`, `_spawn_cwd`, `_load_frame` —
  are the only overrides. SIGPIPE-safe writes, Windows pipe-reader ownership,
  windowless spawn, the sanitized offline environment, RSS recycling, and
  cancel escalation are inherited verbatim; the 98-test official suite is
  unchanged in behavior.
- The GGUF spawn uses `cwd=<runtime pack>`: ggml discovers backend modules
  relative to the process working directory, so the pack dir is the child's
  cwd rather than a PYTHONPATH entry (the pack carries no Python).
- `EngineProviders.provider_for` now matches `context.engine` to
  `provider.engine` (from the engine's `engine_id`): a GGUF context can never
  be silently served by PyTorch, nor an official context by qwentts.cpp.
  Context-less legacy jobs still hit the default provider.
- The controller resolves the selected `QwenVariant` in `_build_qwen_engine`:
  `gguf` routes to `_build_qwen_gguf_engine`, which resolves the native
  device from `qwen_gguf_device` (independent of `qwen_device`, `mps`→`metal`),
  picks the host cell, and gates on the Phase 3 runtime+model install states
  before constructing the engine with verified paths only.
- `submission_context_for` now stamps GGUF contexts instead of refusing the
  format — install gaps surface at engine build, the same layer the official
  variant reports them, and the engine match is the structural guard.
- Frozen builds re-dispatch with `--qwen-gguf-host`, handled in `__main__`
  before any GUI import or stdio redirection.
- A real scripted child (`tests/unit/qwen_gguf_host_fake.py`, derived from
  the official fake with the GGUF load contract + emit logging injected)
  exercises crash/EOF, malformed frames, timeouts, all three cancel windows,
  stale frames, restart and reap — including `terminals_for(job) ==
  ["cancelled"]`, no PCM after terminal, and one resident model owner.
- `infer_stream_many` needs no override: the host's `synthesize_batch`
  already runs one `qt_synthesize` per segment — serial batching in one
  protocol job, and nothing advertises parallel native batching.

## Task 4.3 — clone reuse and native speaker/language mapping

- `core.qwen_variants.NATIVE_SPEAKER_IDS` derives the app→native speaker map
  from the pinned `QWEN_SPEAKERS` table (`voice_id.lower()`): `Uncle_Fu` →
  `uncle_fu`, `Ono_Anna` → `ono_anna`. One source of truth — the real
  CustomVoice GGUF reports exactly these nine lowercase names.
- `NativeQwenSession.get_supported_languages` now reports `"auto"` first:
  the codec table lists only concrete languages, but `qt_synthesize` accepts
  `auto` — without it, shared capability narrowing would hide the app's Auto
  option for GGUF while the official path kept it.
- The GGUF host's `_prepare` now validates `voice_id` for BOTH profiles (a
  preset speaker on Base was previously unvalidated), rejects `instruct` on
  either profile (upstream honours it for custom_voice while the PyTorch
  checkpoint ignores it — a loud `unsupported_selection` keeps the product
  contract format-independent), and rejects `voicePrompt`/`refText`/
  `useVoiceRef` on CustomVoice.
- Base always extracts a native `VoiceRefData` once per
  (path, source-sha256, transcript, talker, codec, quantization, build) —
  `useVoiceRef` is accepted on the wire but the extracted path is now the
  only path; `ref_audio_24k` would re-run the speaker encoder per segment.
- Both derived caches are bounded `OrderedDict` LRU (8 voice refs, 4 PCM
  clips); eviction and `close()` release them explicitly (`refs_released`),
  and the source file is re-hashed per lookup so a rewritten clip at the
  same path can never reuse stale latents.
- `CloneStore.prompt_for` now verifies the stored file still decodes to its
  enrollment `content_hash` (payload+rate recipe, container-metadata-proof)
  — a replaced or undecodable reference is an actionable "re-enroll" error,
  not a silently wrong voice.
- Real-model smoke: `extract_voice_ref` on the locked base-Q4_K_M pair
  returns a (1024,) embedding + (16,25) codes; a 24 kHz noise clip cloned
  into 8 streamed chunks. Speaker encoder absent on custom_voice, present
  on base — matching `EXPECTED_MODEL_TYPE` enforcement.

## Task 5.1 — variant-aware controller state and installation

- The device preference is per-format and must stay so: `qwen_device` speaks
  PyTorch names (`mps`), `qwen_gguf_device` speaks ggml names (`metal`).
  `setQwenDevice` writes the field of the SELECTED format; a format switch
  never rewrites the inactive side. `_qwen_device_pref()` centralizes the
  pick — the controller had a `_qwen_device` cache that could drift from
  settings; it is gone, the settings field is the single source.
- Model row keys are the operation identity. `_qwen_model_manager(key)`
  dispatches on the key itself: `base`/`customvoice` → official checkpoint
  manager, `{profile}-{quantization}` → the GGUF manager for that exact
  variant. The current selection never overrides an explicit key — a row
  action always addresses the install it names. `parse_variant_key` +
  `variant_key_for` in the model-manifest module are the shared keyspace
  helpers.
- `_qwen_model_removal_flags`: `in_use` is pinned by the live engine's
  `engine_id` (a built PyTorch engine does NOT pin GGUF installs and vice
  versa); `drop_shared`/`remove_shared` follows the format's shared tree —
  the GGUF codec is per-quantization, so `*-Q8_0` keeps its codec until no
  other `*-Q8_0` variant is installed.
- `installable_devices()` (runtime-manifest helper) answers "what could
  auto pick" — a cell must exist in the matrix AND ship a manifest. On
  linux-x64 only the cpu cell is published, so `auto` + NVIDIA hardware
  resolves `cpu`, never a CUDA cell that cannot be installed.
- `setQwenVariant` bumps `_qwen_generation` BEFORE `shutdown()`: the
  teardown's pool drain can land a still-current inspection mid-call, and
  it must be stale by the time it publishes. Same lesson as
  `_start_qwen_inspection`'s generation claim.
- `_qwen_operation == "inspect"` is NOT a variant-switch blocker — the new
  inspection supersedes it (generation guard drops the stale result).
  Busy/queued/cancelling (`_profile_switch_blockers`) plus any non-inspect
  operation ARE blockers.
- PySide6 `QMetaProperty.name()` returns `str`, not `bytes` — tests that
  reflect over controller properties must handle both.
- lupdate's same-text heuristic fills a moved string's translation but
  keeps `type="unfinished"` — the fix is to clear the attribute after
  confirming the source is identical, not to retype the translation.
  Recovering translations for extracted strings works via `git show
  HEAD:.../vienetts_en.ts` keyed on `<source>`.
- `FolderDialog` lives in `QtQuick.Dialogs`, not `QtQuick.Controls` —
  extracted components that own dialogs need the import even when the
  host file already imported it.
- `RemoveConfirmDialog` extracted to a file component must not reference
  the host's `root.width`; `Overlay.overlay.width` is the same window
  bound and keeps the dialog self-contained.
- A blank quantization argument to `setQwenVariant` means "remembered
  GGUF choice": the persisted `qwen_gguf_quantization` wins over the
  Q8_0 default, which applies only when nothing is stored (first pick).
