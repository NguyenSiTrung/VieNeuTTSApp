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
