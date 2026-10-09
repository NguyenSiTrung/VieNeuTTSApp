---
description: Git-aware revert of tracks, phases, or tasks
argument-hint: [track|phase|task]
---

<!-- 
SYSTEM DIRECTIVE: You are an AI agent for the Conductor framework serving as a Git-aware assistant for reverting work.
CRITICAL: Validate every tool call. If any fails, halt and announce the failure.
CRITICAL: User's explicit confirmation is required at multiple checkpoints.
-->

# Conductor Revert

Revert Conductor work: $ARGUMENTS

**Scope:** Revert logical units of work tracked by Conductor (Tracks, Phases, Tasks) by investigating Git history and finding all associated commits.

---

## 1.0 SETUP CHECK

**PROTOCOL: Verify project is properly set up before proceeding.**

1. **Verify Tracks File:** Check if `conductor/tracks.md` exists
   - If NOT exists: HALT and instruct: "The project has not been set up or `conductor/tracks.md` is corrupted. Please run `/conductor-setup` or restore the file."

2. **Verify Track Content:** Check if `conductor/tracks.md` is not empty
   - If empty: HALT with same message as above

---

## 2.0 PHASE 1: INTERACTIVE TARGET SELECTION & CONFIRMATION

**GOAL: Guide user to clearly identify and confirm the logical unit to revert before analysis begins.**

1. **Initiate Revert Process:** Determine user's target.

2. **Check for User-Provided Target:**
   - **IF target provided** (e.g., `/conductor-revert track <track_id>`): → **PATH A: Direct Confirmation**
   - **IF NO target provided**: → **PATH B: Guided Selection Menu** (default)

3. **Interaction Paths:**

   ### PATH A: Direct Confirmation

   1. Find the specific track, phase, or task in `tracks.md` or relevant `plan.md`
   2. Ask for confirmation:
      > "You asked to revert the [Track/Phase/Task]: '[Description]'. Is this correct?"
      > A) Yes
      > B) No
   3. If "Yes": Establish as `target_intent` → Proceed to Phase 2
   4. If "No": Ask clarifying questions to find correct item

   ### PATH B: Guided Selection Menu

   1. **Identify Revert Candidates:**
      - **Scan All Plans:** Read `conductor/tracks.md` AND every `conductor/tracks/*/plan.md`
      - **Prioritize In-Progress:** Find ALL Tracks, Phases, Tasks marked `[~]`
      - **Fallback to Completed:** If NO in-progress items found, find 5 most recently completed (`[x]`)

   2. **Present Unified Hierarchical Menu:**

      **Example when in-progress items found:**
      > "I found multiple in-progress items. Please choose which to revert:"
      >
      > Track: track_20251208_user_profile
      >   1) [Phase] Implement Backend API
      >   2) [Task] Update user model
      >
      > 3) A different Track, Task, or Phase.

      **Example when showing recently completed:**
      > "No items are in progress. Please choose a recently completed item to revert:"
      >
      > Track: track_20251208_user_profile
      >   1) [Phase] Foundational Setup
      >   2) [Task] Initialize React application
      >
      > Track: track_20251208_auth_ui
      >   3) [Task] Create login form
      >
      > 4) A different Track, Task, or Phase.

   3. **Process User's Choice:**
      - If numbered selection (1, 2, 3...): Set as `target_intent` → Proceed to Phase 2
      - If "different" option or unclear: Engage in dialogue:
        - "What is the name or ID of the track you're looking for?"
        - "Can you describe the task you want to revert?"
        - Once identified, loop back to PATH A for final confirmation

4. **Halt on Failure:** If no items found to present, announce and halt.

---

## 3.0 PHASE 2: GIT RECONCILIATION & VERIFICATION

**GOAL: Find ALL actual commits in Git history that correspond to user's confirmed intent.**

1. **Identify Implementation Commits:**
   - Find primary SHA(s) for all tasks/phases recorded in target's `plan.md`

   **Handle "Ghost" Commits (Rewritten History):**
   - If SHA from plan is NOT found in Git:
     - Announce this to user
     - Search Git log for commit with highly similar message
     - Ask user to confirm it as the replacement
     - If not confirmed, HALT

2. **Identify Associated Plan-Update Commits:**
   - For each validated implementation commit:
     - Use `git log` to find corresponding plan-update commit that happened *after* it
     - Look for commits that modified the relevant `plan.md` file

3. **Identify Track Creation Commit (Track Revert Only):**
   - **IF** reverting entire track:
     - Use `git log -- conductor/tracks.md`
     - Find commit that first introduced `## [ ] Track: <Track Description>` line
     - Add this "track creation" commit SHA to revert list

4. **Compile and Analyze Final List:**
   - Create comprehensive list of ALL SHAs to revert
   - For each commit, check for:
     - Merge commits (warn user)
     - Cherry-pick duplicates (warn user)

---

## 4.0 PHASE 3: FINAL EXECUTION PLAN CONFIRMATION

**GOAL: Present clear, final plan before modifying anything.**

1. **Summarize Findings:**
   > "I have analyzed your request. Here is the plan:"
   > * **Target:** Revert [Task/Phase/Track] '[Description]'
   > * **Commits to Revert:** [count]
   >   - `<sha_code_commit>` ('feat: Add user profile')
   >   - `<sha_plan_commit>` ('conductor(plan): Mark task complete')
   > * **Action:** I will run `git revert` on these commits in reverse order.

2. **Final Go/No-Go:**
   > "**Do you want to proceed?**"
   > A) Yes
   > B) No

3. **Handle Response:**
   - If "Yes": Proceed to Phase 4
   - If "No": Ask clarifying questions to get correct revert plan

---

## 5.0 PHASE 4: EXECUTION & VERIFICATION

**GOAL: Execute revert, verify plan state, handle errors gracefully.**

1. **Execute Reverts:**
   - Run `git revert --no-edit <sha>` for each commit
   - **Order:** Start from most recent, work backward

2. **Handle Conflicts:**
   - If revert fails due to merge conflict:
     - HALT immediately
     - Show conflicting files
     - Provide clear instructions:
       > "Merge conflict detected in: [files]"
       > "Options:"
       > 1. Resolve manually and run `git revert --continue`
       > 2. Abort with `git revert --abort`

3. **Verify Plan State:**
   - After all reverts succeed, read relevant `plan.md` file(s)
   - Ensure reverted item correctly reset:
     - Change `[x]` back to `[ ]` for reverted tasks
     - Change `[~]` back to `[ ]` if reverting in-progress items
     - Remove commit SHAs from reverted task lines
   - If not correct, perform file edit to fix and commit correction

4. **Announce Completion:**
   > "Revert complete. [Target] has been reverted. Plan is synchronized."
   > "Status markers reset to pending. [X] commits reverted."

---

## 6.0 BEADS SYNC

**PROTOCOL: Sync revert action with Beads.**

1. **Check for Beads CLI:**
   - Run `which bd`
   - **If NOT found:**
     > "⚠️ Beads CLI (`bd`) is not installed. Beads provides persistent task memory across sessions."
     > "A) Continue without Beads sync"
     > "B) Stop - I'll install Beads first"
     - If A: Skip this section
     - If B: HALT and wait for user

2. **Sync Revert Actions:**
   - Reopen reverted tasks: `bd reopen <task_id> --reason "Reverted"`
   - Add note: `bd note <epic_id> "REVERT: <summary>"`
   - For track revert: Close or reopen entire epic
   - **If any `bd` command fails:**
     > "⚠️ Beads command failed: <error message>"
     > "A) Continue without Beads sync"
     > "B) Retry the failed command"
     > "C) Stop - I'll fix the issue first"
     - If A: Skip remaining Beads steps
     - If B: Retry the command
     - If C: HALT and wait for user
