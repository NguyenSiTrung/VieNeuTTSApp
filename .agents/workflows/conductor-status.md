---
description: Display current Conductor project progress
---

# Conductor Status

Show the current status of this Conductor project.

## 1. Check Setup

If `conductor/tracks.md` doesn't exist, tell user to run `/conductor-setup` first.

## 2. Read State

- Read `conductor/tracks.md`
- List all track directories: `conductor/tracks/*/`
- Read each `conductor/tracks/<track_id>/plan.md`

## 3. Calculate Progress

For each track:
- Read `metadata.json` to get priority and depends_on
- Count total tasks (lines with `- [ ]`, `- [~]`, `- [x]`)
- Count completed `[x]`
- Count in-progress `[~]`
- Count pending `[ ]`
- Calculate percentage: (completed / total) * 100
- Check if blocked (has incomplete dependencies)

## 4. Present Summary

Format the output like this:

```
## Conductor Status

**Active Track:** [track name] ([completed]/[total] tasks - [percent]%)
**Overall Status:** In Progress | Complete | No Active Tracks

### Priority Grouping

Group tracks by priority (read from metadata.json):
- 🔴 Critical
- 🟠 High  
- 🟡 Medium
- 🟢 Low

### Tracks by Priority

**🔴 Critical**
- [~] auth_20241215 - User Authentication (30%)

**🟠 High**
- [ ] payments_20241216 - Payment Integration 🔒
  ⤷ Depends on: auth_20241215

**🟡 Medium**
- [x] setup_20241214 - Project Setup (100%)

**🟢 Low**
- [ ] docs_20241217 - Documentation

### Blocked Tracks

Show 🔒 for tracks with incomplete dependencies.
Show dependency chain: "Depends on: [track_ids]"

### Blockers
List all tasks marked with `[!]`:
- Task name (track_id): Reason

### Current Task
[The task marked with [~] in the active track's plan.md]

### Next Action
[The next task marked with [ ] in the active track's plan.md]

### Recent Completions
[Last 3 tasks marked [x] with their commit SHAs]
```

## 5a. Parallel Execution Status

If any track has `parallel_state.json`:

```
### Parallel Execution (Phase: [phase_name])

**Status:** Running | Waiting for workers | Complete

**Workers:**
- 🟢 worker_1_auth: Completed (commit: abc1234)
- 🔵 worker_2_config: In Progress (45 min)
- ⚪ worker_3_utils: Pending (depends on worker_1)

**File Locks:**
- src/auth/index.ts → worker_1_auth
- src/config/index.ts → worker_2_config

**Progress:** 1/3 workers complete
```

## 6. Suggestions

Based on status:
- If no tracks: "Run `/conductor-newtrack` to create your first track"
- If track in progress: "Run `/conductor-implement` to continue"
- If all complete: "All tracks complete! Run `/conductor-newtrack` for new work"

---

## 7. BEADS STATUS

**PROTOCOL: Show Beads task status.**

1. **Check for Beads CLI:**
   - Run `which bd`
   - **If NOT found:**
     > "⚠️ Beads CLI (`bd`) is not installed. Beads provides persistent task memory across sessions."
     > "A) Continue without Beads status"
     > "B) Stop - I'll install Beads first"
     - If A: Skip this section
     - If B: HALT and wait for user

2. **Gather Beads Status:**
   - Run `bd ready --json` to get tasks with no blockers
   - **If command fails:**
     > "⚠️ Beads command failed: <error message>"
     > "A) Continue without Beads status"
     > "B) Retry the failed command"
     > "C) Stop - I'll fix the issue first"
     - If A: Skip remaining Beads steps
     - If B: Retry the command
     - If C: HALT and wait for user
   - For active track with `beads_epic` in metadata:
     - Run `bd show <epic_id> --json` to get full context
     - Read epic notes for last session context (COMPLETED, IN PROGRESS, NEXT)
     - Read design field for technical approach
     - Read acceptance field for completion criteria

3. **Present Beads Status:**
   ```
   ### Beads Task Status

   **Last Session Context (from epic notes):**
   - COMPLETED: <from notes>
   - IN PROGRESS: <from notes>
   - NEXT: <from notes>
   - KEY DECISIONS: <from notes>

   **Ready to Work (no blockers):**
   - bd-a3f8.1.2 P1 "Implement auth middleware"
   - bd-a3f8.2.1 P2 "Write API tests"

   **In Progress:**
   - bd-a3f8.1.1 [active] "Setup database schema"

   **Blocked:**
   - bd-a3f8.3.1 ⤷ Waiting on: bd-a3f8.2.1

   **Discovered During Work:**
   - bd-xyz (discovered-from: bd-a3f8.1.1) "Found race condition"
   ```

4. **Dependency Graph (if complex):**
   ```
   ### Dependency Graph
   bd-a3f8 (Epic: auth_20241226)
   ├── bd-a3f8.1 ✓ Phase 1
   │   ├── bd-a3f8.1.1 ✓
   │   └── bd-a3f8.1.2 ~
   └── bd-a3f8.2 ○ Phase 2 (blocked)
   ```

---

## 8. NEXT ACTIONS

**Molecule Status (if using formula):**
   ```bash
   bd mol current  # Molecule step status
   ```
