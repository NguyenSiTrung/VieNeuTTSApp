---
description: Create context handoff for transferring implementation to next section/session
---

# Conductor Handoff

Create a comprehensive context handoff document when you need to transfer implementation progress to a new section or session. Essential for large tracks that span multiple AI context windows.

## 1. Identify Active Track
- Find track marked `[~]` in `conductor/tracks.md`
- If no active track, ask user to specify or halt
- Load `spec.md`, `plan.md`, and `implement_state.json`
- Check for `parallel_state.json` (indicates parallel execution in progress)

## 1a. Parallel Execution Check

If `parallel_state.json` exists:
> "⚠️ This track has parallel workers currently running:"
> [List workers and their status]
> 
> "A) Wait for workers to complete before handoff"
> "B) Create handoff anyway (workers will continue)"
> "C) Cancel handoff"

- If A: Monitor `parallel_state.json` until all complete, then proceed
- If B: Include parallel state in handoff document
- If C: HALT

## 2. Gather Handoff Context

**Progress Analysis:**
- Count completed `[x]`, in-progress `[~]`, pending `[ ]` tasks
- Calculate overall percentage
- Identify current phase and task position

**Recent Changes:**
```bash
git log --oneline -10
git diff --name-only HEAD~5
```

**Unresolved Issues:**
- Check for `[!]` blocked markers in plan.md
- Read `blockers.md` if exists
- Ask user for any pending decisions or important context

## 3. Update Implementation State

Update `conductor/tracks/<track_id>/implement_state.json` with section tracking:
```json
{
  "current_phase": "...",
  "current_phase_index": 1,
  "current_task_index": 3,
  "completed_phases": ["Phase 1"],
  "section_count": 2,
  "last_handoff": "<ISO timestamp>",
  "handoff_history": [
    {
      "section": 1,
      "timestamp": "...",
      "phase_at_handoff": "...",
      "task_at_handoff": 5,
      "handoff_file": "handoff_<timestamp>.md"
    }
  ],
  "status": "handed_off"
}
```

## 4. Create Handoff Document

Create `conductor/tracks/<track_id>/handoff_<YYYYMMDD_HHMMSS>.md` with:

- **Header:** Track info, section number, timestamp, link to previous handoff
- **Thread URL:** Current Amp thread URL ($AMP_CURRENT_THREAD_ID) for context retrieval
- **Progress Summary:** Overall %, current phase/task, completed/remaining tasks
- **Parallel Execution State:** (if applicable) Worker status, file locks, pending workers
- **Key Implementation Decisions:** Important choices made during this section
- **Code Changes Summary:** Files modified, new files, recent commits
- **Learnings Extracted:** Key patterns/gotchas from `learnings.md` this section
- **Unresolved Issues:** Blockers, pending decisions, questions
- **Context for Next Section:** Critical info, architecture notes, testing status
- **Next Steps:** Immediate tasks, upcoming phase work
- **Resume Instructions:** Commands and specific actions to continue

### 4a. Extract Learnings for Handoff

Before creating handoff document:
1. **Read `learnings.md`:** Load `conductor/tracks/<track_id>/learnings.md`
2. **Extract Recent Learnings:** Filter entries from current section (since last handoff)
3. **Summarize for Handoff:**
   - Key patterns discovered
   - Gotchas encountered
   - Context that next session needs
4. **Include in Handoff Document:**
   ```markdown
   ## Learnings from This Section
   
   ### Patterns Discovered
   - <pattern 1>
   - <pattern 2>
   
   ### Gotchas Encountered
   - <gotcha 1>
   
   ### Context for Next Session
   - <context 1>
   ```

## 5. Commit Handoff

```bash
git add conductor/tracks/<track_id>/
git commit -m "conductor(handoff): Create section <N> handoff for <track_id>

Progress: <X>% complete
Phase: <current_phase>
Next: <next_task_brief>"
```

## 6. Present Summary

Display:
- Handoff document location
- Resume command (`/conductor-implement <track_id>`)
- Next action to take
- Options: End session, Continue, View full document

## When to Use

- Before ending a long implementation session
- When context window is getting full
- At phase boundaries with significant remaining work
- When transferring work to a different session/agent
- After 5+ tasks completed without a checkpoint

---

## 9. BEADS HANDOFF SYNC

**PROTOCOL: Save handoff context to Beads for compaction-proof resumability.**

1. **Check for Beads CLI:**
   - Run `which bd`
   - **If NOT found:**
     > "⚠️ Beads CLI (`bd`) is not installed. Beads provides persistent task memory across sessions."
     > "A) Continue with markdown handoff only"
     > "B) Stop - I'll install Beads first"
     - If A: Skip this section
     - If B: HALT and wait for user

2. **Save Context to Beads with Full Structure:**
   ```bash
   bd update <epic_id> --notes "COMPLETED: Tasks 1-N (<progress>% of track)
   KEY DECISIONS: [list major decisions from this section]
   IN PROGRESS: <current_task>
   NEXT: <next_task>
   BLOCKER: <if any>
   DISCOVERED: <new issues found, with beads IDs>
   HANDOFF: Section <N> saved at conductor/tracks/<track_id>/handoff_<timestamp>.md"
   ```
   - **If command fails:**
     > "⚠️ Beads command failed: <error message>"
     > "A) Continue with markdown handoff only"
     > "B) Retry the failed command"
     > "C) Stop - I'll fix the issue first"
     - If A: Skip remaining Beads steps
     - If B: Retry the command
     - If C: HALT and wait for user

3. **Parallel Workers Handoff (if parallel_state.json exists):**
   - For each active parallel worker, save Beads context:
     ```bash
     bd update <worker_beads_task_id> --notes "HANDOFF: Worker <id> state saved
     STATUS: <in_progress|completed|pending>
     FILES_OWNED: <exclusive files>
     PROGRESS: <description of work done>
     ASSIGNEE: <worker_id>" --json
     ```
   - Include parallel state summary in epic notes:
     ```bash
     bd update <epic_id> --notes "...
     PARALLEL_STATE: <N> workers, <M> completed, <K> in progress
     WORKERS: <list of worker_ids and their status>" --json
     ```

4. **Format for Compaction Recovery:**
   Notes should be self-contained - no conversation context assumed.
   Include technical specifics, not vague progress.

5. **Force Sync to Remote:**
   ```bash
   bd dolt push  # Ensures changes reach remote immediately
   ```

**Benefit:** Beads notes survive context compaction, enabling seamless session resume.
