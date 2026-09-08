# Track Learnings: qwen-multiengine_20260908

Patterns, gotchas, and context discovered during implementation.

## Codebase Patterns (Inherited)

- Dev loop uses Python 3.13, `uv`, Ruff, and pytest with offscreen Qt.
- The inference worker is the single owner of the engine; do not create a second worker for new backends.
- Artifact-first synthesis and bounded PCM transport require normalized 48 kHz mono float32 audio.
- Backend-specific behavior belongs behind explicit capabilities and adapters, not repository-string checks in QML/controllers.
- VieNeu's pinned ONNX manifest and installer are specific to the VieNeu layout; Qwen requires a separate model-management path.
- QML properties require real NOTIFY signals for reactive test behavior.
- Full quality gate is `ruff check .`, `ruff format --check .`, and `pytest`.

---

<!-- Learnings from implementation will be appended below -->
## [2026-09-08] - Phase 1 Tasks 1-3: contracts and capability model
- **Implemented:** `core/backends.py` (engine IDs, frozen capabilities, recommend/validate);
  `TTSRequest` + `Settings` engine/language/instruction/voice-source metadata with restore
  clamping; `core/tts_backend.py` (TtsBackend ABC, VieneuBackend adapter, registry/factory).
- **Files changed:** `core/backends.py`, `core/tts_backend.py`, `core/models.py`,
  `core/settings.py`, `tests/unit/test_backends.py`, `test_engine_selection.py`,
  `test_tts_backend.py`, `test_settings.py`
- **Commits:** 279574d, 0cc4c56 (full gate 937 passed + 15 skipped)
- **Learnings:**
  - Patterns: backend-specific behavior lives in `core/backends.py` data + `validate_selection`;
    controllers/QML must never branch on repo strings. `assert_backend_contract` is the
    conformance bar for every new backend (incl. Phase 3 Qwen loaders).
  - Gotchas: `edit` range bodies must exclude untouched lines — two slips dropped
    `silence_p` (models) and `window_x` (test set); `git diff --stat` after each edit catches it.
    `Settings(**data)` restore nukes the whole file on any invalid value, so stale
    engine/language combos need the two-pass clamp in `load_settings`.
  - Context: `TTSEngine.sample_rate` raises before init — contract native rates come from
    capabilities statically, never from a live engine.
---
## [2026-09-08] - Phase 2 Tasks 1-4: audio normalization and shared worker path
- **Implemented:** `core/resample.py` (StreamingResampler + normalize_stream);
  worker `_backend_for` + single normalized inference path; CJK segmentation
  (`_BOUNDARY_RE`, separator tracking, clause-preferring hard-split);
  `model_tag` + `default_model_tag` + chapter `render.json` sidecar/fresh().
- **Files changed:** `core/resample.py`, `workers/inference_worker.py`,
  `core/engine.py`, `core/models.py`, `core/backends.py`, `core/audiobook.py`,
  5 new test files + engine-fake cutover in 5 existing test files
- **Commits:** 52ba03d, 8dfd9a7, 706111f, 888bbff (full gate 982 passed + 15 skipped)
- **Learnings:**
  - Patterns: every engine-layer test fake must mirror the real TTSEngine surface
    (`initialize`/`is_initialized`/`infer_stream_chunked`/`close`) — the worker now
    consumes only that surface via VieneuBackend. Candidate for patterns.md.
  - Gotchas: `edit` range repairs need `git diff` audit — three slips this phase
    (dropped cancel-handler body caused a REAL cancel/discard regression caught by
    the backpressure test; eaten validation lines; duplicated test lines). Prefer
    small ranges + `PUT >N` pure insertions.
  - Gotchas: packer separator belongs to the PREVIOUS unit, not the incoming one;
    CJK terminators must split unconditionally (no spaces in CJK) while ASCII .!?
    keeps the whitespace rule (decimals/abbreviations).
  - Context: chapter WAV cache is file-presence-based; engine-aware freshness lives
    in the new render.json sidecar — controller integration is Phase 5 Task 2.
---
## [2026-09-08] - Phase 3 Tasks 1-4: Qwen runtime and model management
- **Implemented:** `core/qwen_runtime.py` (lazy boundary, diagnostics, consts);
  `core/qwen_backend.py` (CustomVoice/Base adapters, loader); `core/qwen_models.py`
  (HF-cache probe, ensure with progress/preflight/cancel/retry); worker switch
  lifecycle; pyproject `qwen` extra.
- **Files changed:** 3 new core modules + tests, worker, pyproject
- **Commits:** c5796b2, 8940234, f97401b, bd5384d (full gate 1022 passed + 15 skipped)
- **Learnings:**
  - Patterns: official API is `generate_custom_voice(text, language, speaker, instruct)`
    and `generate_voice_clone(text, language, ref_audio, ref_text)` → `(wavs, sr)`;
    language is a full English name — `LANGUAGE_NAMES` maps all 10 codes (sources:
    HF model cards Qwen3-TTS-12Hz-0.6B-{CustomVoice,Base}, qwen3-tts docs).
  - Patterns: injected factories imply their own runtime — `load_qwen_model` skips
    `require_qwen`/torch-dtype when `model_factory` is given; production path keeps
    both with actionable errors.
  - Gotchas: resampler output is `floor((N-1)*dst/src)+1` — assert `abs(n - ideal) <= 1`,
    never exact counts, across any resampled path.
  - Context: Qwen `generate_*` is whole-utterance; the adapter slices into 12k
    native chunks for progress/cancel granularity. `sr` drift raises actionably;
    Phase 5 smoke re-verifies the 24 kHz pin against the real runtime.
  - Context: Base reference enrollment (ref_audio/ref_text into cached backend)
    is Phase 4 work; worker-cached Base without refs fails jobs actionably until then.
---

## [2026-09-08] - Phase 4 Tasks 1-3: voices, mapping, cloning adapter
- **Implemented:** `core/voices.py` (VoiceDescriptor, 9 documented Qwen CustomVoice speakers with native languages, region parsing moved out of controller, language filter + native-first sort); CustomVoice speaker validation in `qwen_backend._require_speaker` (lazy `voices` import, one direction only); `assert_backend_contract` takes probe voice/language; `core/qwen_voices.py` (clip decode + 3-8 s validation, consent gate, transcript requirement, engine-isolated store, atomic part-file writes, corrupt-store errors).
- **Learnings:**
  - Patterns: engine-layer test fakes must mirror the real TTSEngine surface (every method the worker calls); re-derive `voices.json` groupings from `controller._build_voices` (region/Fallback groups), never assume.
  - Context: CustomVoice language default resolves at the `synthesize_stream` call site (`language or "en"` → "English"); `qwen_language_name` raises only for genuinely unknown codes. Backend-level None handling confirmed against the real mapping code.
  - QML voice visibility (hide cloning for CustomVoice, engine-aware catalogs) deferred to Task 4 with the engine-selection UI; Task 2 covers backend mapping + validation only.

## [2026-09-08] - Phase 4 Task 4: engine selection and validation UI
- **Implemented:** `core/engine_selection.py` (pure recommend/validate + ENGINE_LABELS); controller `ttsEngine`/`ttsLanguage`/`voiceInstruction` persisted properties, `recommendedEngine`/`engineRecommendation` (shown, never auto-applied), `cloningSupported`, engine-aware `_build_voices`, `_submit_text_job` validates + carries engine/language/instruction; SettingsTab engine/language/instruction controls; CloningTab capability gate + notice; smoke FakeController mirror extended.
- **Learnings:**
  - Patterns: new controller QProperties need a smoke-fake mirror entry (with NOTIFY signal) or QML bindings evaluate undefined — `clone_visible_after` caught the missing `cloningSupported`.
  - Context: `TTSRequest` already carried engine/language/instruction (Phase 1 Task 2); the gap was purely that `_submit_text_job` never populated them. Worker routing needed no change.
  - QML AppIcon has no "voice" kind — reuse "cloning"/"wave" for engine-voice affordances.
  - `recommend_engine`: vi/empty → VieNeu, Qwen-supported → CustomVoice, unknown → VieNeu fallback (never strands a job).

## [2026-09-08] - Phase 4 Task 5: interactive synthesis, audition, enrollment
- **Implemented:** `TTSRequest.ref_text` (+ cache identity); `VoiceOp` engine/ref_text/consent with Base/CustomVoice rules; worker registry-first Qwen resolution with per-reference Base cache, enrollment/remove branches, `voices_dir` + `qwen_model_factory` seams; controller Base ref resolution at submit/audition, engine-aware audition with engine-keyed cache + job-engine tracking, `enrollBaseVoice` consent-gated slot, engine-tagged add/remove; `register_default_qwen_backends` called from `app.build_gui`; CloningTab transcript field + per-engine button routing.
- **Learnings:**
  - Patterns: `backends.validate_selection` (request-shape, lenient) vs `engine_selection.validate_selection` (job-ready, strict) — two layers, one name each in its module; recommendation lives ONLY in backends (contradictory duplicates fail silently until a test trips).
  - Context: `TTSRequest.ref_audio` was dead (accepted, never read) — the designed seam for Base refs; controller resolves, worker consumes.
  - Edit-tool hazard: stale `#TAG` snapshots remap to wrong lines and silently shred neighbors (audition_state init, request_backend_switch tail, stream-job head, model voice field). After every surprising edit result, `git diff` the file before continuing.
  - QML brace errors surface only in smoke subprocesses ("Expected token }") — run test_ui_tabs after any QML structural edit.
  - Settings revalidation refuses engine switches incompatible with the stored language (zh blocks VieNeu) — correct behavior, tests must change language first.

## [2026-09-08] - Phase 5 Tasks 1-5: surfaces, packaging, gates
- **Implemented:** listener/paragraph/import/SRT/batch/audition all carry explicit engine via `backend_for_request` seam with `kind`-stamped errors; audiobook `chapter_render_fresh` + identity sidecars (`engine`/`voice_source`) with legacy-without-sidecar-stays-fresh and Qwen-without-sidecar-re-renders rules; `qwen_runtime.qwen_install_status` (runtime/torch/models/ready, never imports torch at module load); controller `qwenReadiness` + `refreshQwenReadiness`; SettingsTab Qwen pack card; `scripts/fetch_qwen_models.py` (HF snapshot of both 0.6B repos); `docs/qwen-setup.md`; `tests/smoke/test_qwen_realmodel.py` (env-gated, structural asserts only, skips cleanly).
- **Learnings:**
  - Patterns: optional-dependency touchpoints must degrade on `Exception`, not `ImportError` — a leaked fake `torch` package (empty `__init__.py`, no `cuda` attr) from cuda_runtime_activation's tmp site-packages dirs on sys.path turns `import torch` success + `torch.cuda` AttributeError. Hardened `QwenBackend.close` to match `describe_qwen_device` precedent. The sys.path leak itself belongs to that track's tests — noted, not touched.
  - Context: full-suite xdist scheduling hides/shows cross-file pollution run to run (Phase 4 gate green, Phase 5 gate red on identical files). Reproduce serially (`-n0`) and bisect halves when a test passes alone but fails in suite.
  - Context: untouched-file `M` in git status is a signal, not noise — the audition-cache test update belonged to Phase 4 Task 5 but missed its commit; Task 5 gate caught it.
  - Qwen settings card is status + docs only by design: runtime probe never scans installs beyond the HF hub dir for the two pinned repos, refresh is an explicit slot, and the card fires no downloads.
