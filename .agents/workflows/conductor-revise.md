---
description: Update spec/plan when implementation reveals issues
---

# Conductor Revise

Update specifications and plans when implementation reveals issues, requirements change, or scope adjustments are needed.

## 1. Identify Track
- Find active track (marked `[~]` in tracks.md)
- If no active track, ask which track to revise
- Check for `parallel_state.json` (parallel execution in progress)

## 1a. Parallel Execution Check

If `parallel_state.json` exists:
> "⚠️ This track has parallel workers currently running."
> "Revising the plan may affect in-progress workers."
> 
> "A) Wait for workers to complete, then revise"
> "B) Revise now (affected workers may need restart)"
> "C) Cancel revision"

- If A: Monitor until complete, then proceed
- If B: Note affected tasks in revision, workers operating on changed tasks should abort
- If C: HALT

## 2. Determine Revision Type
Ask what needs revision:
1. **Spec** - Requirements changed or misunderstood
2. **Plan** - Tasks need to be added, removed, or modified
3. **Both** - Significant scope change

## 3. Gather Context
Ask targeted questions about what was discovered and what needs to change.

## 4. Create Revision Record
Append to `conductor/tracks/<track_id>/revisions.md`:
- Revision number, date, type
- What triggered the revision
- Current phase/task when revision occurred
- Changes made (spec and/or plan)
- Rationale and impact

## 5. Update Documents
- Update `spec.md` and/or `plan.md` as needed
- Add "Last Revised" marker at top of updated files
- New tasks: `[ ]`, Removed tasks: `[-] [REMOVED: reason]`
- **Phase-level parallel changes:**
  - Add/modify `<!-- depends: -->` annotations for phase dependencies
  - Add/modify `<!-- depends: phase1, phase2 -->` for specific phase dependencies
  - If removing a phase that others depend on, update dependent phases
- **Task-level parallel changes:**
  - Update `<!-- files: ... -->` and `<!-- depends: ... -->` annotations
  - If parallel phase removed: Mark for sequential execution or update annotations

## 6. Commit
```bash
git add conductor/tracks/<track_id>/
git commit -m "conductor(revise): Update spec/plan for <track_id>"
```

## 7. Announce
Report what was revised and suggest `/conductor-implement` to continue.

---

## 7a. LOG REVISION AS LEARNING

**PROTOCOL: Record revisions as learnings for future tracks (Ralph-style gotcha tracking).**

Revisions are valuable learnings - they indicate gaps in initial understanding.

1. **Append to `learnings.md`:**
   ```markdown
   ## [YYYY-MM-DD HH:MM] - REVISION #N
   Thread: $AMP_CURRENT_THREAD_ID
   - **Type:** Spec/Plan/Both
   - **Trigger:** <what triggered the revision during implementation>
   - **Learning:**
     - Gotcha: <what was missed or misunderstood>
     - Pattern: <how to avoid this in future - e.g., "always verify X before starting Y">
   ---
   ```

2. **Flag for Pattern Elevation:**
   - If revision reveals a pattern applicable to future tracks:
     > "This revision reveals a reusable lesson. Add to project patterns?"
     > - "<lesson description>"
     > (yes/no)
   - If yes: Append to `conductor/patterns.md`

---

## 8. BEADS SYNC

**PROTOCOL: Sync revisions with Beads.**

1. **Check for Beads CLI:**
   - Run `which bd`
   - **If NOT found:**
     > "⚠️ Beads CLI (`bd`) is not installed. Beads provides persistent task memory across sessions."
     > "A) Continue without Beads sync"
     > "B) Stop - I'll install Beads first"
     - If A: Skip this section
     - If B: HALT and wait for user

2. **Sync Task Changes:**
   - NEW tasks: 
     ```bash
     bd create "<task>" --parent <phase_id> \
       --design "<task technical notes>" \
       --acceptance "<task done criteria>" \
       --json
     ```
   - REMOVED tasks: `bd close <task_id> --reason "Removed in revision #N: <reason>"`
   - MODIFIED tasks: `bd update <task_id> --notes "REVISED: <what changed and why>"`
   - **If any `bd` command fails:**
     > "⚠️ Beads command failed: <error message>"
     > "A) Continue without Beads sync"
     > "B) Retry the failed command"
     > "C) Stop - I'll fix the issue first"
     - If A: Skip remaining Beads steps
     - If B: Retry the command
     - If C: HALT and wait for user

3. **Add Revision Note to Epic:**
   ```bash
   bd update <epic_id> --notes "REVISION #N: <summary of changes>
   REASON: <why revision was needed>
   IMPACT: +X tasks added, -Y tasks removed, ~Z modified
   KEY DECISION: <if any major decisions made during revision>"
   ```

4. **Parallel Execution Changes (if parallel annotations modified):**
   
   **Phase-Level Dependencies:**
   - If phase `<!-- depends: -->` annotations changed:
     - For phases with NEW dependencies: `bd dep add <phase_id> <dependency_phase_id>`
     - For phases with REMOVED dependencies: Update epic notes to reflect new structure
     - For phases changed to `<!-- depends: -->` (empty - no dependencies):
       - Remove all phase-level dependencies in Beads for this phase
   
   **Task-Level Parallel Config:**
   - For NEW parallel tasks: Add file ownership notes:
     ```bash
     bd update <new_task_id> --notes "PARALLEL_ENABLED: true
     FILES_OWNED: <comma-separated file list>
     DEPENDS_ON: <task dependencies>" --json
     ```
   - For MODIFIED parallel annotations (files/depends changed):
     ```bash
     bd update <task_id> --notes "REVISED: Parallel config changed
     FILES_OWNED: <new file list>
     DEPENDS_ON: <new dependencies>" --json
     ```
   - If parallel phase changed to sequential (or vice versa):
     - Clear/set `PARALLEL_ENABLED` in task notes
     - Clear assignees if reverting to sequential: `bd update <id> --assignee "" --json`
