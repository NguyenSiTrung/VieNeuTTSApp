# Plan: phase03_corefeat_20260827

**Execution: parallel where file ownership allows** (kickoff decision).
Every task follows the workflow.md order: read patterns/learnings → write
failing tests first (where a Python contract changes) → implement minimal →
run `ruff check`, `ruff format --check`, `pytest` → commit with a
conventional prefix → attach a git note with the task summary → append
learnings. QML files are verified by the offscreen pytest-qt smoke suite
(NFR-3.3), not pixel assertions — QML authoring tasks are not strictly
test-first. Parallel tasks own disjoint files; the controller API is frozen
in Phase 1 so Phase 2 tabs never block on each other.

## Phase 1: Foundations
<!-- execution: parallel -->
<!-- depends: -->

- [x] Task 1: AppController + voice catalog + persistence redirect
  <!-- files: src/vienetts_app/ui/controller.py, src/vienetts_app/core/engine.py, src/vienetts_app/core/models.py, src/vienetts_app/workers/inference_worker.py, src/vienetts_app/app.py, tests/unit/test_controller.py, tests/unit/test_engine.py, tests/unit/test_models.py, tests/unit/test_inference_worker.py, tests/unit/test_app_entry.py -->
  <!-- done 2026-08-27: commit 3504b1e, 143 new tests -->
  <!-- revised 2026-08-27: + models.py, inference_worker.py — voice clone/denoise ops must
       serialize through the engine-owning worker thread (NFR-3.1) and temperature flows
       per request (TTSRequest); see revisions.md #1 -->
  - TDD: `ui/controller.py` QObject owning worker/engine (lazy); voices
    property merging presets (grouped N/C/S) with cloned voices;
    generate/cancel slots; busy/progress/error state from worker signals
    (FR-3.1)
  - Redirect SDK voice persistence: `save_voices(path=app data dir)` so
    `add_voice` never writes into site-packages (FR-3.4, §21); engine
    takes an injectable voices dir
  - Register controller in `app.py` next to the shell bridge (anchor it —
    `setContextProperty` takes no ownership)
  - Fakes everywhere (phase01 pattern): no model load in unit tests

- [x] Task 2: Document import parsers
  <!-- files: src/vienetts_app/core/importers.py, tests/unit/test_importers.py -->
  <!-- done 2026-08-27: commit 4a4a20b, 18 tests + fixtures -->
  - TDD: `core/importers.py` — `.txt`/`.md` native, `.docx` via
    `python-docx`, `.pdf` via `pypdf`; returns extracted plain text;
    unsupported extension / unreadable file → actionable error (FR-3.3)
  - Table-driven unit tests with tiny fixture files committed under
    `tests/fixtures/`

- [x] Task 3: Full-playback wrapper
  <!-- files: src/vienetts_app/ui/playback.py, tests/unit/test_playback.py -->
  <!-- done 2026-08-27: commit 74de022, 23 tests -->
  - TDD: `ui/playback.py` thin wrapper over `QMediaPlayer` for full-file
    playback of exported WAVs (tech-stack §Audio); play/stop/state
    surfaced as properties/slots; player injectable for offscreen tests
    (FR-3.2)

## Phase 2: Tab Implementation
<!-- execution: parallel -->
<!-- depends: phase1 -->

- [x] Task 1: Text tab
  <!-- files: src/vienetts_app/ui/qml/TextTab.qml, src/vienetts_app/app.py, tests/smoke/test_ui_tabs.py, tests/unit/test_app_entry.py -->
  <!-- done 2026-08-27: commit 7243833 -->
  <!-- revised 2026-08-27: + app.py, test_app_entry.py — register PlaybackController as the
       `playback` QML context property (revisions.md #2) -->
  - Multiline vi/en input; grouped voice picker (N/C/S + cloned);
    Generate → progress → done → Play (playback wrapper) + Export WAV
    (48 kHz) via file dialog defaulting to settings output dir; emotion
    cues hint (FR-3.2, AC-1)

- [x] Task 2: Paragraph/File tab
  <!-- done 2026-08-27: commit cec1cba -->
  <!-- files: src/vienetts_app/ui/qml/ParagraphTab.qml, tests/smoke/test_ui_tabs.py -->
  <!-- depends: task1 not required — consumes Phase 1 importers via controller -->
  - Long-text area; Import button (.txt/.md/.docx/.pdf through
    `core/importers.py`); progress bar bound to controller progress;
    Cancel button wired to cooperative cancel (FR-3.3, AC-2)

- [x] Task 3: Cloning tab
  <!-- done 2026-08-27: commit 0904d6b -->
  <!-- files: src/vienetts_app/ui/qml/CloningTab.qml, tests/smoke/test_ui_tabs.py -->
  - Reference-clip loader (mp3/wav, 3–8 s guidance); optional denoise +
    preview (44.1 kHz); consent gate before first clone (persisted
    acknowledgment); name + enroll via controller; enrolled voices appear
    in pickers (FR-3.4, AC-3)

- [x] Task 4: Settings tab
  <!-- done 2026-08-27: commit 6cfb6e5 -->
  <!-- files: src/vienetts_app/ui/qml/SettingsTab.qml, tests/smoke/test_ui_tabs.py -->
  - Backend auto/onnx/torch + detected readout; precision int8/fp32;
    default voice; output dir picker; temperature; theme; apply-on-next-
    init semantics surfaced; invalid selections handled gracefully
    (FR-3.5, AC-4)

## Phase 3: Integration & Close-out
<!-- execution: sequential -->
<!-- depends: phase2 -->

- [ ] Task 1: Offscreen end-to-end smoke suite
  <!-- files: tests/smoke/test_ui_tabs.py, tests/smoke/test_ui_shell.py -->
  - Fake-engine flows through the real QML shell: generate → done →
    export → WAV valid; cancel mid-job; file import → synth; clone flow
    (fake SDK add_voice); settings round-trip incl. apply-on-restart
    (AC-1..AC-5, NFR-3.2/3.3)
  - Subprocess pattern for GUI-object-tree assertions (phase02 pattern);
    `QT_QPA_PLATFORM=offscreen`

- [ ] Task 2: Real-model manual pass, learnings, elevation
  <!-- files: conductor/tracks/phase03_corefeat_20260827/learnings.md -->
  - Linux real-model pass: text generate/play/export; multi-page PDF
    import; clone from clip + restart persistence; settings backend
    switch (AC-1..AC-5)
  - Full gate green (ruff + format + pytest); append learnings; elevate
    reusable patterns to `conductor/patterns.md`; record cross-OS gaps
    for Phase 6
