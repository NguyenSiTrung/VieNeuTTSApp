# Plan: phase02_uishell_20260827

**Execution: parallel where file ownership allows** (kickoff decision).
Every task follows the workflow.md order: read patterns/learnings → write
failing tests first (where a Python contract changes) → implement minimal →
run `ruff check`, `ruff format --check`, `pytest` → commit with a
conventional prefix → attach a git note with the task summary → append
learnings. QML files are verified by the offscreen pytest-qt suite
(NFR-2.2), not pixel assertions — QML authoring tasks are not strictly
test-first.

## Phase 1: Environment Bootstrap
<!-- execution: sequential -->

- [x] Task 1: Recreate dev environment and verify Qt offscreen support
  <!-- files: (env only — no repo files) -->
  - `uv venv --python 3.13 .venv`; install `-e ".[dev]"` (phase01 pattern)
  - Verify PySide6 imports, `QT_QPA_PLATFORM=offscreen` QApplication
    constructs, baseline 137 tests green, ruff clean
  - Record PySide6 version + install friction in learnings

## Phase 2: UI Core — Theme & Placeholders
<!-- execution: parallel -->
<!-- depends: phase1 -->

- [x] Task 1: Theme manager (`ui/theme.py`) with unit tests — b0f321f
  <!-- files: src/vienetts_app/ui/__init__.py, src/vienetts_app/ui/theme.py, tests/unit/test_theme.py -->
  - TDD: dark/light/system → effective-theme resolution; system follows Qt
    palette; persistence via core/settings.py round-trip; invalid value →
    dark fallback

- [x] Task 2: QML design tokens + placeholder tabs — afe3687
  <!-- files: src/vienetts_app/ui/qml/Theme.qml, src/vienetts_app/ui/qml/TextTab.qml, src/vienetts_app/ui/qml/ParagraphTab.qml, src/vienetts_app/ui/qml/CloningTab.qml, src/vienetts_app/ui/qml/SettingsTab.qml -->
  - Theme.qml singleton tokens (colors/spacing/typography), dark default
    (FR-2.4)
  - Four placeholder tabs, static content + objectName markers for offscreen
    tests

- [x] Task 3: Shell bridge (`ui/bridge.py`) with unit tests — 7dac5e6
  <!-- files: src/vienetts_app/ui/bridge.py, tests/unit/test_bridge.py -->
  <!-- depends: task1 -->
  - TDD: QObject with currentTab/theme/engineNote properties + change
    signals; theme set → persist + re-emit effective theme; engineNote from
    detector capability view (no engine init)
  - Injectable detector/settings fakes (phase01 testing pattern)

## Phase 3: QML Shell Assembly & GUI Entry
<!-- execution: sequential -->
<!-- depends: phase2 -->

- [x] Task 1: Main.qml navigation scaffold — 15a0fd6
  <!-- files: src/vienetts_app/ui/qml/Main.qml -->
  - Window + nav between the four tabs bound to bridge.currentTab;
    Theme.qml tokens; engine readout visible (FR-2.7)

- [ ] Task 2: GUI bootstrap and entry point
  <!-- files: src/vienetts_app/app.py, src/vienetts_app/__main__.py, tests/unit/test_app_entry.py, tests/smoke/test_main_cli.py (Rev 1: old no-args usage-error test superseded by FR-2.1) -->
  - app.py: QApplication + QQmlApplicationEngine, register bridge, load
    Main.qml
  - __main__: no args → GUI; `--smoke` contract unchanged (AC-4)
  - TDD: argv dispatch unit test (GUI vs smoke routing, faked engines)

## Phase 4: Shell Verification & Close-out
<!-- execution: sequential -->
<!-- depends: phase3 -->

- [ ] Task 1: Offscreen pytest-qt smoke suite
  <!-- files: tests/smoke/test_ui_shell.py -->
  - Launch offscreen; assert window + four tabs; navigate via bridge; theme
    toggle → live change + persistence (AC-2, AC-3, AC-5)

- [ ] Task 2: Full gate, live launch, and learnings
  <!-- files: conductor/tracks/phase02_uishell_20260827/learnings.md -->
  - ruff + pytest all green (NFR-2.3); real `python -m vienetts_app` launch,
    confirm no model load at startup (NFR-2.1, AC-1); `--smoke` regression
    (AC-4)
  - Append learnings; elevate reusable patterns to conductor/patterns.md
