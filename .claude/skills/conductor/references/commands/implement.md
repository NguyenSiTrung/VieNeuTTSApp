# Conductor Implement

Implement track: $ARGUMENTS

---

## 1.0 SETUP CHECK

**PROTOCOL: Verify Conductor environment is properly set up.**

1. **Check Required Files:** Verify existence of:
   - `conductor/product.md`
   - `conductor/tech-stack.md`
   - `conductor/workflow.md`

2. **Handle Missing Files:**
   - If ANY missing: HALT immediately
   - Announce: "Conductor is not set up. Please run `/conductor-setup` first."
   - Do NOT proceed.

---

## 2.0 TRACK SELECTION

**PROTOCOL: Identify and select the track to be implemented.**

1. **Check for User Input:** Check if track name provided as argument.

2. **Parse Tracks File:** Read `conductor/tracks.md`
   - Split by `---` separator to identify track sections
   - Extract: status (`[ ]`, `[~]`, `[x]`), description, folder link
   - **CRITICAL:** If no track sections found: "The tracks file is empty or malformed." → HALT

3. **Select Track:**

   **If track name provided:**
   - Exact, case-insensitive match against descriptions
   - If unique match: Confirm "I found track '<description>'. Is this correct?"
   - If no match or ambiguous: Inform user, suggest next available track

   **If no track name provided:**
   - Find first track NOT marked `[x]`
   - Announce: "No track name provided. Selecting next incomplete track: '<description>'"
   - If all complete: "No incomplete tracks found. All tasks completed!" → HALT

4. **Check Dependencies:**
   - Read `conductor/tracks/<track_id>/metadata.json`
   - If `depends_on` array is not empty:
     - For each dependency, check status in `conductor/tracks.md`
     - If ANY not `[x]` (completed):
       > "⚠️ This track has incomplete dependencies:"
       > [List blocking tracks]
       > "Do you want to proceed anyway?"
       > A) Yes - Proceed despite incomplete dependencies
       > B) No - Implement dependencies first
     - If B: Suggest `/conductor-implement <first_dependency>`

5. **Handle No Selection:** If no track selected, inform user and await instructions.

---

## 3.0 TRACK IMPLEMENTATION

**PROTOCOL: Execute the selected track.**

1. **Announce Action:** State which track you're beginning to implement.

2. **Update Status to 'In Progress':**
   - In `conductor/tracks.md`, change `## [ ] Track:` to `## [~] Track:` for selected track

3. **Load Track Context:**
   - Identify track folder from tracks file link → get `<track_id>`
   - Read (using absolute paths):
     - `conductor/tracks/<track_id>/plan.md`
     - `conductor/tracks/<track_id>/spec.md`
     - `conductor/workflow.md`
   - **Error Handling:** If any read fails, STOP and inform user

3a. **Load Patterns Context (Ralph-style knowledge priming):**
    - **Read Project Patterns:** If `conductor/patterns.md` exists:
      - Read and announce: "📚 **Codebase Patterns:** Found X patterns from previous tracks"
      - These patterns inform implementation decisions
    - **Read Track Learnings:** If `conductor/tracks/<track_id>/learnings.md` exists:
      - Read to understand prior work on this track
      - Display: "📝 **Track Learnings:** Resuming with context from previous sessions"
    - **Read Previous Track Learnings (optional):** For similar tracks in archive:
      - Scan `conductor/archive/` for tracks with similar names/descriptions
      - Read their `learnings.md` for relevant patterns

3b. **Beads Context:**
    - **Check for Beads CLI:** Run `which bd`
    - **If NOT found:**
      > "⚠️ Beads CLI (`bd`) is not installed. Beads provides persistent task memory across sessions."
      > "A) Continue without Beads integration"
      > "B) Stop - I'll install Beads first"
      - If A: Set `beads_enabled = false`, continue to step 4
      - If B: HALT and wait for user
    - **If found:** Set `beads_enabled = true`
    - **Load Beads Context (if enabled):**
      - **Run `bd prime`** to get AI-optimized workflow context
      - Read `conductor/tracks/<track_id>/metadata.json` for `beads_epic` and `beads_tasks` fields
      - Store `beads_tasks` mapping (maps plan task names to Beads IDs like `"phase1_task1": "bd-a3f8.1.1"`)
      - If `beads_epic` exists:
        - Run `bd ready --epic <beads_epic>` to show tasks with no blockers
        - **If command fails:**
          > "⚠️ Beads command failed: <error message>"
          > "A) Continue without Beads integration"
          > "B) Retry the failed command"
          > "C) Stop - I'll fix the issue first"
          - If A: Set `beads_enabled = false`, continue
          - If B: Retry the command
          - If C: HALT and wait for user
        - Display: "📊 **Beads Status:** X tasks ready, Y blocked"
        - Use Beads ready list to suggest next task

4. **Check and Load Resume State:**

   **Check for:** `conductor/tracks/<track_id>/implement_state.json`

   **If exists:**
   - Read state file
   - Announce: "Resuming implementation from [current_phase] (Phase [current_phase_index + 1]) - Task [current_task_index + 1]"
   - Skip to indicated phase and task within that phase

   **If not exists:**
   - Create initial state:
     ```json
     {
       "current_phase": "",
       "current_phase_index": 0,
       "current_task_index": 0,
       "completed_phases": [],
       "last_updated": "<current_timestamp>",
       "status": "starting"
     }
     ```

5. **Determine Execution Mode and Execute Phases/Tasks:**

   a. **Parse Phase Dependencies and Build Phase Graph:**
      - For each phase in plan.md, check for `<!-- depends: -->` annotation
      - **If NO annotation:** Phase depends on previous phase (sequential, default)
      - **If `<!-- depends: -->` (empty):** Phase has no dependencies (can start immediately)
      - **If `<!-- depends: phase1, phase2 -->`:** Phase waits for listed phases only
      - Build a phase dependency graph to determine which phases can run in parallel

   a2. **Identify Ready Phases:**
      - Find phases with no unmet dependencies (all dependent phases completed)
      - If multiple phases are ready simultaneously, they can run in parallel
      - Process ready phases (may be single or multiple)

   b. **Parse Task Execution Mode (for current phase):**
      - For the current phase, check for `<!-- execution: parallel -->` annotation
      - If found: Go to step 5c (Parallel Task Execution)
      - If not found or `<!-- execution: sequential -->`: Go to step 5d (Sequential Task Execution)

   c. **PARALLEL TASK EXECUTION FLOW:**
   
      **c1. Parse Parallel Task Metadata:**
      - For each task in the phase, extract:
        - `<!-- files: path1, path2 -->` - Files this task owns exclusively
        - `<!-- depends: task1, task2 -->` - Dependencies on other tasks in phase
        - `<!-- parallel-group: groupName -->` - Optional grouping
      
      **c2. Build Dependency Graph:**
      - Identify tasks with no `depends:` annotation (can start immediately)
      - Identify dependent tasks (must wait for dependencies to complete)
      - Create execution order respecting dependencies
      
      **c3. Detect File Conflicts:**
      - Check if any two tasks claim the same file in `files:` annotation
      - If conflicts detected:
        > "⚠️ File conflict detected: [files] claimed by multiple tasks"
        > "A) Make conflicting tasks sequential (recommended)"
        > "B) Continue anyway - I'll handle manually"
        > "C) Stop and revise plan"
        - If A: Remove parallel annotation from conflicting tasks
        - If B: Proceed with warning
        - If C: HALT
      
      **c4. Initialize Parallel State:**
      - Create `conductor/tracks/<track_id>/parallel_state.json`:
        ```json
        {
          "phase": "<phase_name>",
          "execution_mode": "parallel",
          "started_at": "<timestamp>",
          "workers": [],
          "file_locks": {},
          "completed_workers": 0,
          "total_workers": <count>
        }
        ```
      
      **c5. Spawn Parallel Workers:**
      - **If Beads enabled:** Pre-assign all tasks in Beads:
        ```bash
        # For each parallel task, assign to worker
        bd update <beads_task_id> --status in_progress \
          --assignee worker_<N>_<name> \
          --notes "PARALLEL WORKER: Started" \
          --json
        ```
      - For each task with no unmet dependencies, spawn a sub-agent using Task():
        ```
        Task({
          description: "Implement: <task_name>",
          prompt: "
            You are a Conductor sub-agent implementing a single task.
            
            ## Context
            - Track: <track_id>
            - Phase: <phase_name>
            - Task: <task_description>
            - Worker ID: <worker_id>
            - Beads Task ID: <beads_task_id> (if Beads enabled)
            
            ## Files Owned (ONLY modify these files)
            <files_list>
            
            ## Instructions
            1. Follow workflow.md TDD process (Red → Green → Refactor)
            2. ONLY create/modify files in your owned list above
            3. Run tests and ensure >80% coverage
            4. Commit with message: <type>(<scope>): <description>
            5. NEVER run `git push` - all commits stay local
            6. After commit, update parallel_state.json:
               - Find your worker entry by worker_id
               - Set status to 'completed'
               - Set commit_sha to your commit hash
               - Set completed_at to current timestamp
            6. If Beads enabled:
               - bd update <beads_task_id> --notes 'COMPLETED: <description>
                 COMMIT: <sha>
                 FILES CHANGED: <list>' --json
               - bd close <beads_task_id> --continue --reason 'Task completed' --json
               - bd dolt push  # CRITICAL: Force push to remote
            
            ## Spec Context
            <relevant_spec_excerpt>
            
            ## Success Criteria
            - All tests pass
            - Code coverage >80%
            - Only owned files modified
            - Commit created with proper message
            - parallel_state.json updated
            - Beads synced (if enabled)
          "
        })
        ```
      - Record each spawned worker in `parallel_state.json`:
        ```json
        {
          "worker_id": "worker_<task_index>_<sanitized_name>",
          "task": "<task_description>",
          "task_index": <index>,
          "beads_task_id": "<beads_id>",
          "files": ["<file1>", "<file2>"],
          "depends_on": ["<task_id>"],
          "status": "in_progress",
          "started_at": "<timestamp>"
        }
        ```
      - Update `file_locks` with each worker's file ownership
      
      **c6. Monitor Worker Completion:**
      - Periodically read `parallel_state.json` (every 30 seconds)
      - When a worker completes (status = "completed"):
        - Check if any dependent tasks can now start
        - Spawn newly unblocked workers
        - Increment `completed_workers` count
      - Handle worker failures:
        - If worker status = "failed": Log error, ask user for resolution
        - If worker hasn't updated in 60 minutes: Mark as "timed_out"
        - **If Beads enabled:** Clear assignee for retry: `bd update <id> --assignee "" --status open --json`
      
      **c7. Aggregate Results:**
      - Wait until all workers complete
      - **If Beads enabled:** Force push all changes:
        ```bash
        bd dolt push
        bd ready --epic <epic_id> --json  # Verify all complete
        bd update <epic_id> --notes "PARALLEL PHASE COMPLETE: <phase>
        WORKERS: <N> succeeded
        COMMITS: <sha_list>" --json
        ```
      - Update `plan.md`:
        - Mark all parallel tasks as `[x]` complete
        - Append commit SHAs from each worker
      - Delete `parallel_state.json`
      - Check phase graph: are there other ready phases to process?
      - If yes: Go back to step 5a2 to process next ready phase(s)
      - If no more phases: Proceed to step 6 (Finalize Track)

   d. **SEQUENTIAL EXECUTION FLOW:**
   
      **d1. Announce:** "Executing tasks from plan.md following workflow.md procedures."

      **d2. Iterate Through Tasks:** Loop through each task in `plan.md` one by one.

      **d3. For Each Task:**
          - **i. Defer to Workflow:** `workflow.md` is the **single source of truth** for task lifecycle. Follow its "Task Workflow" section for implementation, testing, and committing.
           - **CRITICAL: NEVER run `git push`. All commits stay local. Users decide when to push.**
           - **i-a. Beads Task Start (If Enabled):** After marking task `[~]` in progress:
             - **ONLY if `beads_enabled` is true:**
               - Generate task key from phase index and task index (e.g., `phase1_task1` for first task in first phase)
               - Look up `beads_task_id` from `beads_tasks` mapping in metadata.json using task key
               - If found, run: `bd update <beads_task_id> --status in_progress`
               - **If `bd` command fails:**
                 > "⚠️ Beads command failed: <error message>"
                 > "A) Continue without Beads integration"
                 > "B) Retry the failed command"
                 > "C) Stop - I'll fix the issue first"
                 - If A: Set `beads_enabled = false`, continue
                 - If B: Retry the command
                 - If C: HALT and wait for user
           - **i-b. Beads Task Complete (If Enabled):** After marking task `[x]` complete:
             - **ONLY if `beads_enabled` is true:**
               - Look up `beads_task_id` from `beads_tasks` mapping (same key as i-a)
               - If found:
                 - Add structured completion notes:
                   ```bash
                   bd update <beads_task_id> --notes "COMPLETED: <description>
                   COMMIT: <sha_7chars>
                   FILES CHANGED: <list>
                   KEY DECISION: <if any>"
                   ```
                 - Close with auto-advance: `bd close <beads_task_id> --continue --reason "Task completed"`
                   (The `--continue` flag auto-advances to next step if available)
                 - If discovered work during implementation:
                   `bd create "<issue>" -t bug -p 2 --deps discovered-from:<current_task_id> --json`
               - **If `bd` command fails:**
                 > "⚠️ Beads command failed: <error message>"
                 > "A) Continue without Beads integration"
                 > "B) Retry the failed command"
                 > "C) Stop - I'll fix the issue first"
                 - If A: Set `beads_enabled = false`, continue
                 - If B: Retry the command
                 - If C: HALT and wait for user
      - **ii. Update Implementation State:** After marking task in progress:
        - Set `current_phase` to current phase name
        - Set `current_phase_index` to current phase number (zero-based)
        - Set `current_task_index` to current task number within the phase (zero-based)
        - Set `last_updated` to current timestamp
        - Set `status` to "in_progress"
      - **iii. On Phase Completion:** When all tasks in a phase are complete:
        - Add phase name to `completed_phases` array
        - Reset `current_task_index` to 0
        - **If `beads_enabled` is true:** Update epic notes for compaction survival:
          ```bash
          bd update <epic_id> --notes "COMPLETED: Phase N - <phase_name>
          IN PROGRESS: Phase N+1 - <next_phase>
          NEXT: <first_task_of_next_phase>
          KEY DECISIONS: <major decisions made this phase>"
          ```
        - **Check phase graph for next ready phases:**
          - If other phases now have all dependencies met → Go back to step 5a2
          - If next sequential phase is ready → Process it
          - If all phases complete → Proceed to step 6 (Finalize Track)

      **d4. Handle Blocked Tasks:**
       - If task marked `[!]`:
         > "⚠️ Task is blocked: [reason]"
         > "What would you like to do?"
         > A) Skip this task and continue
         > B) Mark as unblocked and proceed
         > C) Stop implementation here
       - If B: Change `[!]` to `[~]` and proceed

      **d5. Self-Check & Issue Handling:**
      - After implementation, run tests, linting, type checks
      - If issues found, analyze the root cause:
      
      **Issue Analysis Decision Tree:**
      
      | Issue Type | Indicators | Action |
      |------------|------------|--------|
      | **Implementation Bug** | Typo, logic error, missing import, test assertion wrong | Fix directly and continue |
      | **Spec Issue** | Requirement wrong, missing, impossible, edge case not covered | Trigger Revise workflow for spec → update spec.md → log in revisions.md → then fix |
      | **Plan Issue** | Missing task, wrong order, task too big/small, dependency missing | Trigger Revise workflow for plan → update plan.md → log in revisions.md → continue |
      | **Discovered Work** | Bug found, improvement needed, follow-up task | If Beads: `bd create "<issue>" --deps discovered-from:<current_task_id> --json` |
      | **Blocked** | External dependency, need user input, waiting on API | Mark as blocked, suggest `/conductor-block` |
      
      **Agent MUST announce:** "This issue reveals [spec/plan problem | implementation bug | discovered work]. [Triggering revision | Fixing directly | Created follow-up task]."
      
      **For Spec/Plan Issues:**
      1. Create/append to `conductor/tracks/<track_id>/revisions.md` with:
         - Revision number, date, type (Spec/Plan/Both)
         - What triggered the revision
         - Current phase/task when issue occurred
         - Changes made and rationale
      2. Update the relevant document (spec.md or plan.md)
      3. Add "Last Revised" marker at top of updated file
      4. Commit revision before continuing

6. **Finalize Track:**
   - After all tasks complete, update `conductor/tracks.md`: `## [~]` → `## [x]`
   - **Clean Up State:** Delete `conductor/tracks/<track_id>/implement_state.json`
   - **Elevate Patterns:** Prompt for pattern consolidation (see step 5e)
   - Announce track fully complete

---

## 5.1 LEARNINGS CAPTURE (After Each Task)

**PROTOCOL: Record learnings and patterns discovered during implementation (Ralph-style progress tracking).**

After marking each task `[x]` complete, append to `conductor/tracks/<track_id>/learnings.md`:

```markdown
## [YYYY-MM-DD HH:MM] - Phase N Task M: <task_name>
Thread: $AMP_CURRENT_THREAD_ID (if available)
- **Implemented:** <brief description of what was done>
- **Files changed:** <list of files modified/created>
- **Commit:** <sha_7chars>
- **Learnings:**
  - Patterns: <reusable patterns discovered, e.g., "this codebase uses X for Y">
  - Gotchas: <things to watch out for, e.g., "don't forget to update Z when changing W">
  - Context: <useful context, e.g., "the settings panel is in component X">
---
```

**5e. Pattern Elevation (At Phase/Track Completion):**

1. **Review Learnings:** Scan `learnings.md` for reusable patterns
2. **Identify Candidates:** Look for:
   - Patterns mentioned 2+ times
   - Gotchas that apply beyond this track
   - Context that future tracks would benefit from
3. **Prompt for Elevation:**
   > "I found these potentially reusable patterns from this phase/track:"
   > 
   > | Pattern | Occurrences | Elevate to project? |
   > |---------|-------------|---------------------|
   > | "Use Zod for validation" | 3 | ☐ |
   > | "Barrel exports required" | 2 | ☐ |
   > 
   > "Select patterns to add to `conductor/patterns.md` (Enter numbers, or 'all', or 'skip'):"

4. **Update Project Patterns:**
   - If patterns selected, append to `conductor/patterns.md`:
     ```markdown
     - <pattern description> (from: <track_id>, <date>)
     ```
   - If `conductor/patterns.md` doesn't exist, create it with this structure:
     ```markdown
     # Codebase Patterns
     
     Reusable patterns discovered during development. Read this before starting new work.
     
     ---
     
     - <pattern> (from: <track_id>, <date>)
     ```

5. **Suggest AGENTS.md Updates:**
   - If learnings are specific to a module/directory:
     > "These learnings are specific to `src/auth/`. Would you like to update `src/auth/AGENTS.md`?"
     > A) Yes - Add learnings to module AGENTS.md
     > B) No - Keep in track learnings only
   - If A: Create/update the module's AGENTS.md with relevant patterns

---

## 6.0 SYNCHRONIZE PROJECT DOCUMENTATION

**PROTOCOL: Update project-level documentation based on completed track.**

1. **Execution Trigger:** ONLY execute when track reaches `[x]` status. Do NOT execute for other status changes.

2. **Announce:** "Synchronizing project documentation with completed track specifications."

3. **Load Track Specification:** Read `conductor/tracks/<track_id>/spec.md`

4. **Load Project Documents:** Read:
   - `conductor/product.md`
   - `conductor/product-guidelines.md`
   - `conductor/tech-stack.md`

5. **Analyze and Update:**

   **a. Analyze `spec.md`:** Identify new features, functionality changes, or tech stack updates.

   **b. Update `conductor/product.md`:**
   - **Condition:** Determine if completed feature significantly impacts product description
   - **Propose and Confirm:**
     > "Based on the completed track, I propose these updates to `product.md`:"
     > ```diff
     > [Proposed changes in diff format]
     > ```
     > "Do you approve these changes? (yes/no)"
   - **Action:** Only after explicit confirmation, perform edits. Record if changed.

   **c. Update `conductor/tech-stack.md`:**
   - **Condition:** Determine if significant tech stack changes detected
   - **Propose and Confirm:**
     > "Based on the completed track, I propose these updates to `tech-stack.md`:"
     > ```diff
     > [Proposed changes in diff format]
     > ```
     > "Do you approve these changes? (yes/no)"
   - **Action:** Only after explicit confirmation, perform edits. Record if changed.

   **d. Update `conductor/product-guidelines.md` (Strictly Controlled):**
   - **CRITICAL WARNING:** This file defines core identity and communication style. Modify with EXTREME caution.
   - **Condition:** ONLY propose if spec.md explicitly describes branding, voice, tone changes
   - **Propose and Confirm:**
     > "WARNING: The completed track suggests a change to core product guidelines. Please review carefully:"
     > ```diff
     > [Proposed changes in diff format]
     > ```
     > "Do you approve these CRITICAL changes to `product-guidelines.md`? (yes/no)"
   - **Action:** Only after explicit confirmation, perform edits. Record if changed.

6. **Final Report:**
   > "Documentation synchronization complete."
   > - **Changes made to `product.md`:** [description or "No changes needed"]
   > - **Changes made to `tech-stack.md`:** [description or "No changes needed"]
   > - **Changes made to `product-guidelines.md`:** [description or "No changes needed"]

---

## 7.0 TRACK CLEANUP

**PROTOCOL: Offer to archive or delete completed track.**

1. **Execution Trigger:** ONLY execute after track successfully implemented AND documentation sync complete.

2. **Ask for User Choice:**
   > "Track '<track_description>' is now complete. What would you like to do?"
   > A) **Archive** - Move to `conductor/archive/` and remove from tracks file
   > B) **Delete** - Permanently delete folder and remove from tracks file
   > C) **Skip** - Leave in tracks file
   >
   > Please enter A, B, or C.

3. **Handle User Response:**

   **If A (Archive):**
   - Create `conductor/archive/` if not exists
   - Move `conductor/tracks/<track_id>` to `conductor/archive/<track_id>`
   - Remove track section from `conductor/tracks.md`
   - Announce: "Track '<description>' has been successfully archived."

   **If B (Delete):**
   - **CRITICAL WARNING:** Ask final confirmation:
     > "WARNING: This will permanently delete the track folder. This cannot be undone. Are you sure? (yes/no)"
   - If 'yes':
     - Delete `conductor/tracks/<track_id>`
     - Remove track section from `conductor/tracks.md`
     - Announce: "Track '<description>' has been permanently deleted."
   - If 'no':
     - Announce: "Deletion cancelled. Track unchanged."

   **If C (Skip) or other:**
   - Announce: "Completed track will remain in your tracks file."

---

## Status Markers Reference

- `[ ]` - Pending
- `[~]` - In Progress
- `[x]` - Completed
- `[!]` - Blocked
