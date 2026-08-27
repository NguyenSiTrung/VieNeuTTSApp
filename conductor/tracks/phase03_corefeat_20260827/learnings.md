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

## [2026-08-27 12:10] - Phase 2 Tasks 1–4 (sequential — shared test file)
- **Implemented:** all four tabs wired to controller/playback/bridge: Text
  (editor, grouped picker, generate/progress/cancel, play+export), Paragraph
  (long text, file import, progress), Cloning (consent gate, clip, denoise
  preview, enrollment, cloned list), Settings (backend/precision +
  needsRestart, default voice, output dir, temperature, theme).
- **Commits:** 7243833, cec1cba, 0904d6b, 6cfb6e5
- **Learnings:**
  - Patterns: theme is dual-written (bridge.themePreference for the live
    switch + controller.theme mirror — both hit the same settings.json
    field); tabs read group labels from controller.voices with the
    "▸ group / — voice" flat-model idiom; every tab exposes a tested seam
    function (importPath/selectClip/setOutputDir) as the FileDialog
    onAccepted entry point.
  - Gotchas (QML test driving): Repeater delegates are incubated — visual
    parent only, invisible to findChildren(QObject); walk childItems().
    Button.click() works via QMetaObject.invokeMethod; ComboBox.activate()
    is QML-side only — emit the bound `activated` signal instead
    (Q_ARG(int,...) invoke fails). SpinBox `text` unreadable from C++ —
    read displayText. Qt6 FileDialog has no `folder` (use currentFolder).
    QML function args are QVariant in the metaobject.
  - Bugs fixed in flight: controller exportWav @Slot(str, bool) →
    result=bool (overload breaks QML one-arg calls); importDocument seam
    added (@Slot(str, result=str), errors → errorText + "").
  - Process: sub-agent dispatch was cancelled twice mid-track (platform
    instability) — the Cloning tab was finished by the orchestrator
    directly from the partial working tree; diagnose-before-fix paid off
    (the QML was correct; the driver lookup was wrong).
---

## [2026-08-27 12:30] - Phase 3 Tasks 1–2 (integration + real-model pass)
- **Implemented:** e2e offscreen smoke suite (fake at the SDK layer only —
  the whole controller/worker/engine/QML/playback stack is production code);
  real-model GUI pass driving the real app.
- **Commits:** 819af33 (e2e suite)
- **Real-model evidence (15/15 PASS, offscreen, ONNX CPU int8):** AC-1
  generate+export 48 kHz 2.24 s; AC-2 PDF import (44 chars) → progress+cancel
  clean → synth done; AC-3 clone enrolled from a synthesized 2 s ref,
  voices.json in app data, clone-listed, synth-with-clone 2.88 s, survives
  restart in the catalog WITHOUT engine init; AC-4 backend persisted across
  restart. HF_TOKEN-less warning appears but the seeded cache needs no
  network (offline validation was Phase 1).
- **Learnings:**
  - Patterns: e2e fake placement — below the controller (FakeVieneu
    implementing the spike §0 surface), never beside it; wait_for() pumping
    processEvents + QThread.msleep is the offscreen async idiom.
  - Gotchas: fake players must implement the FULL duck-typed signal contract
    (playbackStateChanged/mediaStatusChanged/errorOccurred) — the wrapper
    connects them at construction; `__file__`-relative fixture paths break
    in `python -c` drivers — resolve from `vienetts_app.__file__`.
  - Context: real CPU int8 synth of a ~45-char sentence ≈ 3–8 s wall clock
    in the offscreen driver (model load ~30 s first request) — e2e timeouts
    sized accordingly.
---
