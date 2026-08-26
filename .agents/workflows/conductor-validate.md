---
description: Validate Conductor project integrity
---

# Conductor Validate

Validate the integrity of this Conductor project.

## 1. Core Files Check

Verify these files exist in `conductor/`:
- `product.md`
- `tech-stack.md`
- `workflow.md`
- `tracks.md`

## 2. Tracks Consistency

For each track in `tracks.md`:
- Verify directory exists: `conductor/tracks/<track_id>/`
- Verify files: `metadata.json`, `spec.md`, `plan.md`
- Validate metadata.json has: track_id, type, status, created_at

## 3. Orphan Detection

- List all directories in `conductor/tracks/`
- Report any not referenced in `tracks.md`

## 4. Status Consistency

- Compare status markers in `tracks.md` with `metadata.json` status field
- Report mismatches

## 5. Plan Integrity

For each `plan.md`:
- Must have at least one phase and task
- Valid markers only: `[ ]`, `[~]`, `[x]`, `[!]`
- Completed tracks should have all tasks completed
- Validate parallel execution annotations if present

## 5a. Parallel Execution Validation

For tracks with parallel execution annotations:
- Check `<!-- execution: parallel -->` is after valid phase heading
- Verify all tasks in parallel phases have `<!-- files: ... -->` annotation
- Detect file conflicts (same file in multiple tasks)
- Verify `<!-- depends: ... -->` references valid task IDs
- If `parallel_state.json` exists, check for stale/orphan workers

## 6. Report

Present summary with:
- ✅ Valid items
- ⚠️ Warnings
- ❌ Errors
- Recommendations for fixes

## 7. Auto-Fix Option

Offer to fix auto-fixable issues:
- Missing metadata fields
- Status mismatches
- Orphan cleanup

---

## 8. BEADS VALIDATION

**PROTOCOL: Include Beads consistency checks.**

1. **Check for Beads CLI:**
   - Run `which bd`
   - **If NOT found:**
     > "⚠️ Beads CLI (`bd`) is not installed. Beads provides persistent task memory across sessions."
     > "A) Continue validation without Beads checks"
     > "B) Stop - I'll install Beads first"
     - If A: Skip this section
     - If B: HALT and wait for user

2. **Verify Beads Integration:**
   - Task status sync between Beads and plan.md
   - No orphan epics/tasks
   - Epic links valid in metadata.json
   - **If any `bd` command fails:**
     > "⚠️ Beads command failed: <error message>"
     > "A) Continue validation without Beads checks"
     > "B) Retry the failed command"
     > "C) Stop - I'll fix the issue first"
     - If A: Skip remaining Beads steps
     - If B: Retry the command
     - If C: HALT and wait for user

3. **Add to Report:** Beads integration section with sync status

4. **Auto-Fix:** Offer to sync status markers between systems
