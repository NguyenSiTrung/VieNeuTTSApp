---
description: Sync context docs with current codebase state
---

# Conductor Refresh

Sync conductor context documentation with the current codebase state.

## 1. Verify Setup

Check conductor/ exists with core files. If not, suggest `/conductor-setup`.

## 2. Determine Scope

If no argument, ask:
- `all` - Full refresh
- `tech` - tech-stack.md only
- `product` - product.md only
- `workflow` - workflow.md only
- `track [id]` - Specific track

## 3. Analyze Drift

Compare current codebase against docs:
- **Tech:** Scan package.json, requirements.txt, etc. for new/removed deps
- **Product:** Check completed tracks not reflected in product.md
- **Workflow:** Check CI/CD changes, new tooling

## 4. Present Drift Report

Show what's changed since last setup/refresh.

## 5. Confirm Updates

Ask user to approve changes.

## 6. Apply Updates

- Create backups (*.md.bak)
- Update files with new information
- Add refresh marker to top of files

## 7. Update State

Create/update `conductor/refresh_state.json` with timestamp and changes.

## 8. Commit

```bash
git add conductor/
git commit -m "conductor(refresh): Sync context with codebase"
```

---

## 8a. CONSOLIDATE LEARNINGS (Pattern Flywheel)

**PROTOCOL: Aggregate and consolidate learnings across all tracks into project patterns.**

1. **Scan All Track Learnings:**
   - Read `learnings.md` from all tracks in `conductor/tracks/*/`
   - Read `learnings.md` from all archived tracks in `conductor/archive/*/`

2. **Extract Pattern Candidates:**
   - Find patterns mentioned 2+ times across different tracks
   - Find gotchas that reoccur
   - Find context that applies project-wide

3. **Present Consolidation Report:**
   > "## Learnings Consolidation Report"
   > 
   > **Patterns found across tracks:**
   > | Pattern | Tracks | Already in patterns.md? |
   > |---------|--------|-------------------------|
   > | "Use Zod for validation" | auth, api, users | ❌ No |
   > | "Barrel exports required" | auth, config | ✅ Yes |
   > 
   > **New patterns to add:** [list]
   > **Duplicate patterns to merge:** [list]
   > 
   > "Would you like to update `conductor/patterns.md`? (yes/no)"

4. **Update `conductor/patterns.md`:**
   - Add new patterns with source attribution
   - Merge duplicates
   - Remove outdated patterns (if confirmed)
   - Format:
     ```markdown
     - <pattern description> (from: <track_ids>, <date>)
     ```

5. **Create `conductor/patterns.md` if Missing:**
   ```markdown
   # Codebase Patterns
   
   Reusable patterns discovered during development. Read this before starting new work.
   
   ## Code Conventions
   - <patterns related to code style>
   
   ## Architecture
   - <patterns related to architecture decisions>
   
   ## Gotchas
   - <common mistakes to avoid>
   
   ## Testing
   - <patterns for testing approaches>
   
   ---
   Last refreshed: <timestamp>
   ```

---

## 9. BEADS DRIFT CHECK

**PROTOCOL: Include Beads status in drift analysis.**

1. **Check for Beads CLI:**
   - Run `which bd`
   - **If NOT found:**
     > "⚠️ Beads CLI (`bd`) is not installed. Beads provides persistent task memory across sessions."
     > "A) Continue refresh without Beads drift check"
     > "B) Stop - I'll install Beads first"
     - If A: Skip this section
     - If B: HALT and wait for user

2. **Analyze Beads vs Conductor Drift:**
   - Tasks done in Beads but `[ ]` in plan.md
   - Tasks `[x]` in plan.md but open in Beads
   - Orphaned Beads tasks
   - **If any `bd` command fails:**
     > "⚠️ Beads command failed: <error message>"
     > "A) Continue refresh without Beads drift check"
     > "B) Retry the failed command"
     > "C) Stop - I'll fix the issue first"
     - If A: Skip remaining Beads steps
     - If B: Retry the command
     - If C: HALT and wait for user

3. **Offer Sync Options:**
   > A) Sync Beads → Conductor (trust Beads)
   > B) Sync Conductor → Beads (trust plan.md)
   > C) Skip
