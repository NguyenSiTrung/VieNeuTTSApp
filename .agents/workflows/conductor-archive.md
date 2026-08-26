---
description: Archive completed tracks
---

# Conductor Archive

Archive completed tracks to clean up the project.

## 1. Find Completed Tracks
List all `[x]` tracks from `tracks.md`.

## 2. Select Tracks
- Archive all completed
- Or select specific ones
- Or cancel

## 3. Archive Process
For each selected track:
1. Create `conductor/archive/` if needed
2. **Check for stale parallel state**: If `parallel_state.json` exists, warn and clean up
3. **Extract learnings before archiving** (see step 3a)
4. Move track folder to archive
5. Remove from `tracks.md`
6. Add archive comment

### 3a. Extract Learnings Before Archiving

**PROTOCOL: Preserve learnings from completed tracks (Ralph-style knowledge capture).**

1. **Read Track Learnings:** Load `conductor/tracks/<track_id>/learnings.md`

2. **Identify Patterns to Elevate:**
   - Find patterns not yet in `conductor/patterns.md`
   - Find gotchas with general applicability
   
3. **Present Extraction Summary:**
   > "## Learnings from `<track_id>`"
   > 
   > **Patterns to preserve:**
   > | Pattern | Elevate to project? |
   > |---------|---------------------|
   > | "Use X for Y" | ☐ |
   > | "Don't forget Z" | ☐ |
   > 
   > "Select patterns to add to `conductor/patterns.md` (Enter numbers, 'all', or 'skip'):"

4. **Update Project Patterns:**
   - Append selected patterns to `conductor/patterns.md`:
     ```markdown
     - <pattern> (from: <track_id>, archived <date>)
     ```

5. **Preserve Learnings in Archive:**
   - Keep `learnings.md` in the archived track folder
   - Future tracks can reference via `/conductor-newtrack`

## 4. Commit
Commit the archive operation.

---

## 5. BEADS COMPACTION

**PROTOCOL: Compact Beads history for archived tracks.**

1. **Check for Beads CLI:**
   - Run `which bd`
   - **If NOT found:**
     > "⚠️ Beads CLI (`bd`) is not installed. Beads provides persistent task memory across sessions."
     > "A) Continue archive without Beads compaction"
     > "B) Stop - I'll install Beads first"
     - If A: Skip this section
     - If B: HALT and wait for user

2. **Clear Stale Parallel Assignees:**
   - For each archived track, check if any Beads tasks have assignees set (from parallel execution):
     ```bash
     # Clear any leftover parallel worker assignees
     bd update <task_id> --assignee "" --json
     ```
   - This prevents orphaned worker assignments

3. **Compact Archived Track Epics:**
   - For each archived track with `beads_epic` in metadata:
     - Run `bd admin compact --auto <epic_id>`
   - **If any `bd` command fails:**
     > "⚠️ Beads command failed: <error message>"
     > "A) Continue archive without Beads compaction"
     > "B) Retry the failed command"
     > "C) Stop - I'll fix the issue first"
     - If A: Skip remaining Beads steps
     - If B: Retry the command
     - If C: HALT and wait for user
   - Announce: "Compacted Beads history for archived tracks."

4. **Offer Project-Wide Compaction:**
   > "Would you like to compact all completed Beads tasks?"
   > A) Yes - Compact all completed tasks project-wide
   > B) No - Only compact archived tracks
   
   If A: Run `bd admin compact --auto --all`
