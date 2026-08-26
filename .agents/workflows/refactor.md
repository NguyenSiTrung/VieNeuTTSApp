---
description: Intelligent refactoring with pre/post verification, safe symbol renames, and TDD validation. Ensures zero regressions.
---

# /refactor — Intelligent Refactoring Workflow

Structured refactoring with full analysis, safe transformations, and verification at every step.

## Steps

1. **Understand the refactoring target**
   - What needs to be refactored? (file, module, pattern, architecture)
   - What's the scope? (single file, module, project-wide)
   - What's the strategy? (safe/conservative vs aggressive)
   - What behavior must be preserved?

2. **Pre-refactor analysis**
   - Map the codebase area being refactored
   - Find all references to symbols being changed
   - Identify dependent files and modules
   - Check for existing tests covering the code
   - Run current tests and capture baseline results

3. **Create refactoring plan**
   - List specific transformations to apply
   - Order by dependency (leaf changes first)
   - Identify risk points and rollback strategies
   - Define verification steps for each transformation

4. **Execute transformations (SEQUENTIAL)**
   For each transformation:
   
   a. **Apply the change**
      - Use safe rename operations where possible
      - Prefer AST-aware transformations over text find-replace
      - Make ONE logical change at a time
   
   b. **Verify immediately**
      - Run linting/diagnostics on changed files
      - Run affected tests
      - Check for type errors
      - If ANY verification fails → fix or rollback before proceeding
   
   c. **Commit checkpoint** (if using git)
      - Atomic commit for each verified transformation
      - Descriptive message: "refactor: [what changed]"

5. **Post-refactor verification**
   - Run the FULL test suite
   - Compare results to baseline from step 2
   - Run linting on all changed files
   - Verify no behavior changes (same inputs → same outputs)

6. **Report**
   ```
   ## Refactoring Complete
   - **Target**: [what was refactored]
   - **Transformations**: N changes applied
   - **Files modified**: [list]
   - **Tests**: [pass/fail status, comparison to baseline]
   - **Regressions**: [none / list of issues]
   ```

## Rules

- **NEVER mix refactoring with feature changes**
- **NEVER mix formatting with logic changes**
- **Verify after EACH change**, not just at the end
- **Preserve behavior** — refactoring changes structure, not semantics
- **Rollback on failure** — don't accumulate broken changes
- If pre-existing test failures exist, document them but don't fix during refactoring

## Safety Checks

| Before | After |
|---|---|
| Tests baseline recorded | Tests match baseline |
| All references mapped | All references updated |
| Dependent files identified | Dependent files verified |
| Rollback strategy defined | Rollback unnecessary (or applied) |
