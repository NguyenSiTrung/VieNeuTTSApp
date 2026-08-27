# Track Learnings: phase03_corefeat_20260827

Patterns, gotchas, and context discovered during implementation.

## Codebase Patterns (Inherited)

📚 Full set: `conductor/patterns.md` (28 patterns from phase01_core +
phase02_uishell). Most load-bearing for this track:

- **Dev loop / gates**: `uv venv --python 3.13 .venv`, install
  `-e ".[dev]"`, gates via `.venv/bin/{ruff,pytest}`;
  `QT_QPA_PLATFORM=offscreen` for pytest; ruff + format + pytest all green
  before every commit (conventional prefix + `git notes add` summary).
- **Engine seam**: `Vieneu(...)` is a factory function; exactly one
  `TTSEngine` per `InferenceWorker`; requests serialize through the
  worker queue; cancel is cooperative between stream chunks. Worker
  signals: `progress`, `chunk_ready`, `done`, `error`.
- **Voice keys**: `list_voices()` → `[(label, voice_id)]`; the
  **voice_id** (element [1]) is the key for `infer(voice=...)`; labels
  encode gender·region·style for N/C/S grouping.
- **`add_voice` persists into site-packages by default** — this track
  must redirect via `save_voices(path=<app data dir>)` (FR-3.4, §21).
- **Sample rates differ**: `infer`/`infer_stream` 48 kHz; `denoise`
  **44.1 kHz** — denoise preview playback must not assume 48 kHz.
- **Qt threading**: cross-thread signals are queued to the receiver
  thread — tests pump `QCoreApplication.processEvents()` while waiting;
  coverage.py cannot trace QThread-run code, keep logic in directly
  callable methods.
- **QML integration**: anchor context-property objects on the engine
  (`engine._controller = controller`) or GC collects them; QML files use
  same-directory `import "."` for the Theme singleton; read QML-declared
  properties via `.property("name")` from `findChildren` wrappers.
- **GUI tests in pytest**: ONE QGuiApplication per process; GUI-object-
  tree assertions run in a subprocess (`sys.executable -c` + `RESULT:`
  json stdout line); `colorScheme()` Unknown under offscreen → treat as
  dark.
- **Testing fakes**: `TTSEngine(factory=lambda **kw: FakeVieneu())`,
  injectable detector/player/settings — no model loads in unit tests.

---

<!-- Learnings from implementation will be appended below -->

## [2026-08-27 11:00] - Phase 1 Tasks 1–3 (parallel)
- **Implemented:** AppController (worker/engine owner, voice catalog, generate/cancel/export, voice ops, settings seam, consent gate); document importers (.txt/.md/.docx/.pdf); PlaybackController (QMediaPlayer wrapper).
- **Files changed:** ui/controller.py, core/engine.py, core/models.py, workers/inference_worker.py, app.py, core/importers.py, ui/playback.py + 6 test files + fixtures.
- **Commits:** 3504b1e, 4a4a20b, 74de022
- **Learnings:**
  - Patterns: SDK `save_voices(path)` writes ALL voices (presets+cloned) but there is NO custom-path load API — persistence = save to `<app data>/voices/voices.json` + merge-back into `tts._preset_voices` at `_ensure()`; `add_voice` must always be `save=False`. Voice ops serialize through the worker queue as `VoiceOp` dataclasses (keeps the one-thread-owns-engine invariant).
  - Patterns: preset catalog is readable model-free from `Path(vieneu.__file__).parent/assets/voices_v3_turbo.json` (20 presets; descriptions `gender · region · style` → region is the middle `·` token).
  - Gotchas: `app.aboutToQuit.connect(controller.shutdown)` binds the method AT CONNECT TIME — post-connect monkeypatches never fire; wrap before connecting in tests.
  - Gotchas: `QAudioOutput` construction deadlocks under pytest fd capture (pipewire devicemonitor probe) — set `QT_AUDIO_BACKEND=ffmpeg` in tests needing the real player; `QMediaPlayer` has no `resume()` (use `play()`); enum str is `"PlaybackState.PlayingState"` — map via `.name` or split.
  - Gotchas: pypdf 6.16 corrupt-file exception types vary by failure mode — catch broad + chain; python-docx raises `PackageNotFoundError`; `extract_text()` may return `None` (coerce `or ""`).
  - Gotchas: `tests/fixtures/` is NOT ruff-excluded — fixture generator scripts must pass lint/format.
  - Context: theme setting lives in settings.json `theme` field; both bridge (`save_theme`) and controller settings seam write the same field — Settings tab should write via `bridge.themePreference` for live switching.
  - Process: parallel sub-agents authoring code with main-session commit serialization avoided git index races and red-test interference; each agent ran only scoped tests, orchestrator ran the full gate (337 passed).
---
