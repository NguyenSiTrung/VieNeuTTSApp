# VieNeuTTS Desktop App — Development Workflow

<!-- refreshed 2026-09-16: test items now 1055 collected / 1054 selected (12 benchmark deselected; v0.1.15 test-suite consolidation folded duplicate micro-tests and smoke scenarios while 100% of assertions were retained); gates otherwise unchanged, but note the real-QAudioSink smoke in tests/unit/test_stream_playback.py is CI-skipped and fails on device-less hosts when it shares a run with other unit files (bead VieNeuTTSApp-3iy) -->
<!-- refreshed 2026-09-14: test items 1036 collected / 1024 selected (12 benchmark deselected; SRT subtitle studio added ~164 tests); gates otherwise unchanged -->

## Testing
 - **Target coverage: 80%** (line) on Python code, measured per change.
 - `pytest` is the gate; run it before any commit.
 - Core logic must be well tested (see `code_styleguides/testing.md`);
   QML glue is smoke-tested outside CI.
- Benchmarks are excluded by default (`-m 'not benchmark'` in pyproject
  addopts) — run explicitly when changing perf-sensitive paths.
- A local `pytest` run on a device-less host is expected to show exactly one
  red test: `test_stream_playback.py::TestRealQtSmoke::test_real_qaudiosink_offscreen_smoke`
  (real `QAudioSink` never drains; CI-skipped, bead `VieNeuTTSApp-3iy`). Judge
  the gate on the rest of the run — do not "fix" it by editing app code, and
  do not treat it as a regression from the current change.
- Merge same-function micro-tests instead of piling parameter rows
  (2026-09-06…10 consolidation: 1029 → 872 items collected / 860 selected,
  12 benchmark cases deselected; 2026-09-14: the SRT subtitle studio took the
  suite to 1036 collected / 1024 selected at that time — merge within the new
  subtitle/align/project suites too; 2026-09-15: the Studio redesign
  consolidation folded duplicate micro-tests and repetitive smoke scenarios
  (100% of assertions retained, full-suite runtime ~30% faster) taking the
  suite to 1055 collected / 1054 selected — keep smoke scenarios
  consolidated per subprocess driver).

## Commits
- Commit **after each task** completes and its tests pass.
- Conventional commit prefix: `feat:`, `fix:`, `chore:`, `refactor:`,
  `test:`, `docs:`.
- Task summary is stored in **git notes** (`git notes add -m "..."`),
  keyed to the task from the plan; the commit message stays concise.

## Remote Sync
- Per-task commits stay **local**. Never `git push`, `git pull`, `git fetch`,
  or `bd dolt push` as part of a task.
- `bd close` / `bd update --notes` for the finished task run normally — task
  tracking is local and ungated. Run `bd dolt push` **once at session end**,
  when the user asks, to persist that state to the remote.
- Push and pull happen only on the user's explicit request.

## Workflow Order (per task)
1. Read `conductor/patterns.md` (project patterns) and the track's
   `learnings.md`.
2. Write/fail tests first (TDD) for the changed contract.
3. Implement minimal code to pass; refactor.
4. Run `ruff check`, `ruff format --check`, `pytest`.
5. Commit; attach a git note with the task summary.
6. Append any new gotcha to `tracks/<id>/learnings.md`.

## Validation Gate
- `ruff check .` and `ruff format --check .` and `pytest` all green.
- Never merge with failing tests, ignored type errors, or `# noqa`
  without a comment.
