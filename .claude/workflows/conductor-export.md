---
description: Export project summary as markdown
---

# Conductor Export

Generate a comprehensive project summary.

## 1. Gather Information
Read all conductor files:
- product.md, tech-stack.md, workflow.md
- tracks.md and all track specs/plans

## 2. Generate Summary
Create markdown with:
- Product overview
- Tech stack summary
- All tracks (completed, in-progress, pending)
- Statistics

## 3. Save Options
- `conductor/export_YYYYMMDD.md`
- Overwrite README.md
- Print only

## 4. Output
Save and confirm.

---

## 5. BEADS STATISTICS

**PROTOCOL: Include Beads stats in export.**

1. **Check for Beads CLI:**
   - Run `which bd`
   - **If NOT found:**
     > "⚠️ Beads CLI (`bd`) is not installed. Beads provides persistent task memory across sessions."
     > "A) Continue export without Beads statistics"
     > "B) Stop - I'll install Beads first"
     - If A: Skip this section
     - If B: HALT and wait for user

2. **Gather Beads Statistics:**
   - Run `bd stats` and add to export:
     - Total issues, completion rate
     - Task distribution by status
   - **If command fails:**
     > "⚠️ Beads command failed: <error message>"
     > "A) Continue export without Beads statistics"
     > "B) Retry the failed command"
     > "C) Stop - I'll fix the issue first"
     - If A: Skip remaining Beads steps
     - If B: Retry the command
     - If C: HALT and wait for user
