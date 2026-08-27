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
