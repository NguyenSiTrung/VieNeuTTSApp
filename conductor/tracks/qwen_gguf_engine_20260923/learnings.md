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
