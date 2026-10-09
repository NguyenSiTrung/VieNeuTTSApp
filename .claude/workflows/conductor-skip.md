---
description: Skip the current task with a reason
---

# Conductor Skip

Skip the current in-progress task.

## 1. Find Current Task
- Find in-progress track in `tracks.md`
- Find in-progress task in that track's `plan.md`

## 2. Get Reason
Ask why skipping:
- Will complete later
- No longer needed
- Blocked by external factor
- Other

## 3. Update Plan
- If "no longer needed": Mark as `[x] (SKIPPED)`
- Otherwise: Reset to `[ ]` with skip comment
- Mark next pending task as `[~]`

## 4. Update State
Update `implement_state.json` with new task index.

## 5. Announce
Confirm skip and show next task.

---

## 6. BEADS SYNC

**PROTOCOL: Sync skip action with Beads.**

1. **Check for Beads CLI:**
   - Run `which bd`
   - **If NOT found:**
     > "⚠️ Beads CLI (`bd`) is not installed. Beads provides persistent task memory across sessions."
     > "A) Continue without Beads sync"
     > "B) Stop - I'll install Beads first"
     - If A: Skip this section
     - If B: HALT and wait for user

2. **Sync Skip Actions:**
   - "No longer needed": `bd close <task_id> --reason "Skipped: <reason>"`
   - "Will complete later": 
     ```bash
     bd update <task_id> --status open
     bd update <task_id> --notes "SKIPPED: <reason>. Will complete later."
     ```
   - "Blocked": `bd update <task_id> --status blocked --notes "SKIPPED: <reason>"`
   - **If any `bd` command fails:**
     > "⚠️ Beads command failed: <error message>"
     > "A) Continue without Beads sync"
     > "B) Retry the failed command"
     > "C) Stop - I'll fix the issue first"
     - If A: Skip remaining Beads steps
     - If B: Retry the command
     - If C: HALT and wait for user

3. **Update next task:** `bd update <next_task_id> --status in_progress --assignee conductor`
