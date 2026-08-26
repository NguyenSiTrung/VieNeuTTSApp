# VieNeuTTS Desktop App — Development Workflow

## Testing
- **Target coverage: 80%** (line) on Python code, measured per change.
- `pytest` is the gate; run it before any commit.
- Core logic must be well tested (see `code_styleguides/testing.md`);
  QML glue is smoke-tested outside CI.

## Commits
- Commit **after each task** completes and its tests pass.
- Conventional commit prefix: `feat:`, `fix:`, `chore:`, `refactor:`,
  `test:`, `docs:`.
- Task summary is stored in **git notes** (`git notes add -m "..."`),
  keyed to the task from the plan; the commit message stays concise.

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
