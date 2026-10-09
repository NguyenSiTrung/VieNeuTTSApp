# Conductor Directory Structure

When you see this structure, the project uses Conductor:

```
conductor/
├── product.md              # Product vision, users, goals
├── product-guidelines.md   # Brand/style guidelines (optional)
├── tech-stack.md           # Technology choices
├── workflow.md             # Development standards (TDD, commits, coverage)
├── tracks.md               # Master track list with status markers
├── patterns.md             # Consolidated learnings from all tracks (Ralph-style)
├── setup_state.json        # Setup progress tracking
├── refresh_state.json      # Context refresh tracking (created by /conductor-refresh)
├── beads.json              # Beads integration config (created by setup)
├── code_styleguides/       # Language-specific style guides
├── archive/                # Archived completed tracks
├── exports/                # Exported summaries
└── tracks/
    └── <track_id>/         # Format: shortname_YYYYMMDD
        ├── metadata.json   # Track type, status, dates
        ├── spec.md         # Requirements and acceptance criteria
        ├── plan.md         # Phased task list with status
        ├── learnings.md    # Patterns/gotchas discovered (Ralph-style)
        ├── implement_state.json  # Phase-aware implementation resume state (if in progress)
        ├── handoff_*.md    # Section handoff documents (if any)
        ├── blockers.md     # Block history log (if any)
        ├── skipped.md      # Skipped tasks log (if any)
        └── revisions.md    # Revision history log (if any)
```

## Status Markers

Throughout conductor files:

| Marker | Meaning |
|--------|---------|
| `[ ]` | Pending/New |
| `[~]` | In Progress |
| `[x]` | Completed (often followed by 7-char commit SHA) |
| `[!]` | Blocked (followed by reason in brackets) |
| `[-]` | Skipped (followed by reason) |

## State Files Reference

| File | Purpose |
|------|---------|
| `conductor/setup_state.json` | Track setup progress for resume |
| `conductor/product.md` | Product vision, users, goals |
| `conductor/tech-stack.md` | Technology choices |
| `conductor/workflow.md` | Development workflow (TDD, commits) |
| `conductor/tracks.md` | Master track list with status |
| `conductor/patterns.md` | Consolidated learnings from all tracks |
| `conductor/tracks/<id>/metadata.json` | Track metadata |
| `conductor/tracks/<id>/spec.md` | Requirements |
| `conductor/tracks/<id>/plan.md` | Phased task list |
| `conductor/tracks/<id>/learnings.md` | Patterns/gotchas discovered during implementation |
| `conductor/tracks/<id>/implement_state.json` | Phase-aware implementation resume state |
| `conductor/tracks/<id>/parallel_state.json` | Parallel worker state (for parallel phases) |
| `conductor/tracks/<id>/handoff_*.md` | Section handoff documents |
| `conductor/tracks/<id>/blockers.md` | Block history log |
| `conductor/tracks/<id>/skipped.md` | Skipped tasks log |
| `conductor/tracks/<id>/revisions.md` | Revision history log |
| `conductor/refresh_state.json` | Context refresh tracking |
| `conductor/beads.json` | Beads integration config |
| `conductor/archive/` | Archived completed tracks |
| `conductor/exports/` | Exported summaries |

## Parallel Execution Annotations

Plan.md can include annotations for parallel task execution:

| Annotation | Location | Purpose |
|------------|----------|---------|
| `<!-- execution: parallel -->` | After phase heading | Enable parallel execution for phase |
| `<!-- files: path1, path2 -->` | After task line | Exclusive file ownership |
| `<!-- depends: task1, task2 -->` | After task line | Task dependencies within phase |
| `<!-- parallel-group: name -->` | After task line | Optional grouping |

## Gemini CLI Compatibility

Projects set up with Gemini CLI's Conductor extension use identical structure.
The only differences are command syntax:

| Gemini CLI | Claude Code | Purpose |
|------------|-------------|---------|
| `/conductor:setup` | `/conductor-setup` | Initialize project with context files and first track |
| `/conductor:newTrack` | `/conductor-newtrack` | Create feature/bug track with spec and plan |
| `/conductor:implement` | `/conductor-implement` | Execute tasks from track's plan (TDD workflow) |
| `/conductor:status` | `/conductor-status` | Display progress overview |
| `/conductor:revert` | `/conductor-revert` | Git-aware revert of tracks, phases, or tasks |
| `/conductor:validate` | `/conductor-validate` | Validate project integrity and fix issues |
| `/conductor:block` | `/conductor-block` | Mark task as blocked with reason |
| `/conductor:skip` | `/conductor-skip` | Skip current task with justification |
| `/conductor:revise` | `/conductor-revise` | Update spec/plan when implementation reveals issues |
| `/conductor:archive` | `/conductor-archive` | Archive completed tracks |
| `/conductor:export` | `/conductor-export` | Generate project summary export |
| `/conductor:handoff` | `/conductor-handoff` | Create context handoff for section transfer |
| `/conductor:refresh` | `/conductor-refresh` | Sync context docs with current codebase state |
| `/conductor:formula` | `/conductor-formula` | List and manage track templates (Beads formulas) |
| `/conductor:wisp` | `/conductor-wisp` | Create ephemeral exploration track (no audit trail) |
| `/conductor:distill` | `/conductor-distill` | Extract reusable template from completed track |

Files, workflows, and state management are fully compatible.
