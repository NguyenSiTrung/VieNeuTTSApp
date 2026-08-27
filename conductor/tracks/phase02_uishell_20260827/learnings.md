# Track Learnings: phase02_uishell_20260827

Patterns, gotchas, and context discovered during implementation.

## Codebase Patterns (Inherited)

From `conductor/patterns.md` (project-level) — full list there; the ones
load-bearing for this track:

- Dev loop: `uv venv --python 3.13 .venv` (SDK caps at 3.13; system python
  may be newer), install `-e ".[dev]"`, gates via `.venv/bin/{ruff,pytest}`.
- Quality gate per task: `ruff check .` + `ruff format --check .` + `pytest`
  all green before committing; conventional prefix + `git notes add` task
  summary.
- Ruff excludes `.agents/`, `conductor/`, `*.md` — tooling/planning dirs are
  not app code.

## Seeded from phase01_core_20260827 (direct predecessor)

- **Qt cross-thread signals are queued** to the receiver thread: tests and
  headless code must pump `QCoreApplication.processEvents()` while waiting,
  or callbacks never fire. Applies to every pytest-qt test in this track.
- **coverage.py cannot trace PySide6 QThread-run code** (C++ threads) — keep
  logic in directly callable methods and test those synchronously. The UI
  bridge must therefore be a plain QObject with testable slots/properties,
  not logic hidden inside QML.
- **Testing style:** injectable fakes everywhere (`engine_factory=...`,
  `detect_hardware(probe=...)`). This track's analog: bridge/theme take
  injectable settings store + detector, so unit tests never touch the real
  model or hardware.
- `python -m vienetts_app --smoke "Xin chào" --voice Adam -o out.wav` is the
  CI smoke entry — the GUI entry must not disturb its argv contract
  (`--smoke` currently `required=True`, so the no-args path is free for the
  GUI).
- Engine readout comes from the detector's capability view — display only;
  nothing in this track may instantiate `TTSEngine` (NFR-2.1).

---

<!-- Learnings from implementation will be appended below -->

## [2026-08-27 10:03] - Phase 2 (parallel): theme manager + QML tokens/tabs + shell bridge
- **Implemented (3 workers, disjoint files):**
  - `ui/theme.py` (b0f321f): resolve_theme/save_theme/load_theme +
    qt_system_theme (styleHints colorScheme; never raises)
  - `ui/qml/` (afe3687): Theme.qml singleton tokens (colors/spacing/typography,
    dark default), qmldir, 4 placeholder tabs with objectName markers
    (textTab/paragraphTab/cloningTab/settingsTab)
  - `ui/bridge.py` (7dac5e6): ShellBridge QObject — currentTab + setCurrentTab
    Slot, tabs list for Repeater, themePreference↔effectiveTheme with
    persistence, engineNote from detector (no engine init), refreshSystemTheme
- **Gates after aggregation:** 176 tests green, ruff clean
- **Learnings:**
  - QML tabs import the Theme singleton via same-directory `import "."` —
    Main.qml must live in the same qml/ dir (bare absolute-path imports are
    rejected by the engine).
  - QML singleton must be registered in qmldir AND the engine needs the qml
    dir on its import path for `import "."` resolution offscreen.
  - Parallel workers sharing a git index: commits hit index.lock occasionally
    — retry-after-5s instruction worked; keep workers off `git add -A`.
  - Qt ColorScheme: Light→light, Dark→dark, Unknown→dark (safe default;
    offscreen platform reports Unknown).
---

## [2026-08-27 09:45] - Phase 1 Task 1: Recreate dev environment and verify Qt offscreen support
- **Implemented:** `uv venv --python 3.13 .venv` + `-e ".[dev]"`; verified
  PySide6 import, `QT_QPA_PLATFORM=offscreen` QGuiApplication constructs
  (platformName=offscreen), baseline 137 tests green, ruff check/format clean.
- **Files changed:** none (env-only task per plan) + conductor state
- **Commit:** (bookkeeping only)
- **Learnings:**
  - PySide6 **6.11.2** installed with zero friction via uv; QtQml
    (QQmlApplicationEngine) imports fine offscreen.
  - uv pulled cpython 3.13.14 automatically — no system-python pinning needed.
  - Baseline `pytest` runs in <1s (all fakes, no model loads) — safe to run
    the full suite per task.
---
