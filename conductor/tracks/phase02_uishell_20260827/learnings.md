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

## [2026-08-27 10:40] - Phase 4: verification & close-out (be2210f + probes)
- **Verified:**
  - AC-4/NFR-2.3: real `--smoke "Xin chào" --voice Adam` run — exit 0,
    valid 48 kHz WAV (0.80s), full suite 186 green, ruff clean.
  - NFR-2.1: production-default GUI launch 0.15–0.28s; vieneu/onnxruntime/
    transformers/torch NEVER in sys.modules; RSS ~150–205 MB (vs 700+ MB with
    model).
  - AC-1: window `isExposed() == True` on the real Wayland display
    (compositor-mapped), event loop alive 5+s; `vienetts-app` console script
    also launches the GUI.
- **Learnings:**
  - **Wayland blocks QScreen.grabWindow (returns 0×0) without an XDG
    desktop portal**; forcing xcb aborts when the X sockets aren't
    connectable. For "window actually shows" evidence, assert
    `QQuickWindow.isExposed()` + live event loop instead of screenshots.
  - PySide6 teardown noise: after app.quit(), QML bindings re-evaluate while
    the context is being destroyed → harmless "Cannot read property of null"
    TypeErrors on stderr with exit code 0. Don't chase them.
  - `findChildren(QObject, name)` returns generic QObject wrappers — QML
    properties (e.g. StackLayout.currentIndex) must go through
    `.property("name")`, not attribute access.
  - The `--smoke` path prints an HF Hub unauthenticated-request warning when
    the SDK pings for updates even with a warm cache — pre-existing Phase 1
    behavior, not this track's concern.
---

## [2026-08-27 10:20] - Phase 3: Main.qml scaffold + GUI entry (15a0fd6, ebab419)
- **Implemented:** Main.qml (navBar Repeater over bridge.tabs, StackLayout
  tab swap, engineReadout) — offscreen load verified; app.py create_app/
  run_gui; __main__ dispatch (--smoke optional, no args → gui_runner);
  tests/unit/test_app_entry.py (5 tests).
- **Learnings:**
  - **QML aborts (Fatal Python error: Aborted) when a headless
    QCoreApplication exists before QQmlApplicationEngine** — e.g. a
    `--smoke` CLI test in the same pytest process. create_app now raises an
    actionable RuntimeError instead; GUI-object-tree assertions must run in
    a subprocess (pattern: script + `subprocess.run([sys.executable, "-c",
    ...])` + RESULT: json line on stdout).
  - StackLayout instantiates ALL tab children — findChildren sees all four
    tab objectNames at once; use bridge.currentTab (or StackLayout
    currentIndex) for "which is visible", not existence.
  - FR-2.1 superseded the Phase 1 "no args → usage error 2" test (Rev 1,
    revisions.md) — spec change propagated to test_main_cli.py with an
    injected gui_runner.
---

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
