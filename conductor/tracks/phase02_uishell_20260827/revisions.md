# Revisions: phase02_uishell_20260827

## Rev 1 — 2026-08-27 (Plan)

- **Type:** Plan (files-ownership footnote)
- **Trigger:** Phase 3 Task 2 (GUI bootstrap and entry point) must change
  `tests/smoke/test_main_cli.py::test_missing_smoke_flag_is_usage_error`,
  which pinned the Phase 1 contract "no args → argparse usage error (2)".
  FR-2.1 explicitly supersedes it: no args now launches the GUI; the
  `--smoke` exit-code contract (0/1 + blank-text usage error) is unchanged
  (AC-4).
- **Phase/Task when raised:** Phase 3, Task 2.
- **Changes:** Updated the test to assert GUI routing with an injected
  `gui_runner`; full no-args coverage lives in `tests/unit/test_app_entry.py`.
  Plan's `files:` list for phase3_task2 amended to include
  `tests/smoke/test_main_cli.py`.
- **Rationale:** Spec FR-2.1 is the authority; the old test encoded a
  Phase-1-only behavior the spec was always going to replace.
