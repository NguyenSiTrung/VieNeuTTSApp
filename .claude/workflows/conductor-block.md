---
description: Mark a task as blocked with a reason
---

# Conductor Block

Mark a task as blocked.

## 1. Identify Task
- If argument provided, find that task
- Otherwise show in-progress tasks for selection

## 2. Get Reason
Ask for the blocking reason.

## 3. Update Plan
Change `[~]` to `[!]` and append `[BLOCKED: reason]`

## 4. Beads Sync

**PROTOCOL: Sync block action with Beads.**

1. **Check for Beads CLI:**
   - Run `which bd`
   - **If NOT found:**
     > "⚠️ Beads CLI (`bd`) is not installed. Beads provides persistent task memory across sessions."
     > "A) Continue without Beads sync"
     > "B) Stop - I'll install Beads first"
     - If A: Skip to step 5
     - If B: HALT and wait for user

2. **Sync Block Action (if task has `beads_task_id` in track metadata):**
   - Run:
     ```bash
     bd update <task_id> --status blocked
     bd update <task_id> --notes "BLOCKED: <reason>
     CATEGORY: <External/Technical/Resource>
     WAITING FOR: <what needs to happen to unblock>
     DISCOVERED: <if blocking issue is new, create with discovered-from>"
     ```
   - If blocker is another task, create dependency: `bd dep add <blocked_task> <blocker_task>`
   - **If any `bd` command fails:**
     > "⚠️ Beads command failed: <error message>"
     > "A) Continue without Beads sync"
     > "B) Retry the failed command"
     > "C) Stop - I'll fix the issue first"
     - If A: Skip remaining Beads steps
     - If B: Retry the command
     - If C: HALT and wait for user

## 5. Confirm
Announce task is blocked.
