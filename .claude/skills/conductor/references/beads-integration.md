# Beads Integration Reference

## Overview

Conductor integrates with [Beads](https://github.com/steveyegge/beads) to provide persistent task memory that survives context compaction. This creates a hybrid system:

> **Compatible with Beads v0.56+.** Requires Dolt backend (now the only backend).

- **Conductor**: Human-readable specs and plans
- **Beads**: Agent-optimized task state with dependency tracking

**IMPORTANT: Beads integration is always attempted.** If `bd` CLI is unavailable or fails, the user is prompted to choose whether to continue without persistent task memory. Conductor still functions fully, just without cross-session state.

## CRITICAL: Availability Check

**You MUST run this check before using ANY `bd` command:**

```bash
# Check if Beads is available and enabled
BEADS_AVAILABLE=false
if which bd > /dev/null 2>&1; then
  if [ -f conductor/beads.json ]; then
    if grep -q '"enabled"[[:space:]]*:[[:space:]]*true' conductor/beads.json 2>/dev/null; then
      BEADS_AVAILABLE=true
    fi
  fi
fi

# ONLY use bd commands if BEADS_AVAILABLE is true
# Otherwise, skip all bd commands and use plan.md markers only
```

**If BEADS_AVAILABLE is false:**
- Do NOT run any `bd` commands
- Use `plan.md` markers for all task tracking
- Conductor workflows work identically without Beads

## How It Works (when Beads is enabled)

### Session Protocol (Enhanced - Based on Beads v0.56+ Best Practices)

The recommended session workflow for maximum context preservation:

> **v0.56+:** Beads requires a running Dolt SQL server. Start with `bd dolt start` before using bd commands. The server is typically started once and left running.

1. `bd prime` — Load AI-optimized workflow context (run first!)
2. `bd ready` — Find unblocked work
3. `bd show <id>` — Get full context and audit trail
4. `bd update <id> --status in_progress` — Start work
5. **Add notes as you work** (critical for compaction survival)
6. `bd close <id> --continue` — Complete and auto-advance to next step
7. `bd dolt push` — Push to Dolt remote (always run at session end)

**Key insight:** `bd close --continue` automatically marks the next step as in_progress, reducing commands from 3 to 1.

### Ownership Model

| Component | Conductor Owns | Beads Owns |
|-----------|----------------|------------|
| Specs | `spec.md` (requirements) | — |
| Plans | `plan.md` (task list) | — |
| Task State | Status markers `[ ]` `[~]` `[x]` | Full state + dependencies |
| Memory | Session-bound | Cross-session persistent |

### Track → Epic Mapping

Each Conductor track becomes a Beads epic:

```
conductor/tracks/auth_20250115/
├── spec.md          # Conductor: requirements
├── plan.md          # Conductor: task breakdown
└── metadata.json    # Links to Beads epic ID + task mapping
```

Beads state is stored in the Dolt database (not as files). Access via `bd show <epic_id>`.

### Task ID Mapping

The `metadata.json` contains a `beads_tasks` mapping that links plan tasks to Beads IDs:

```json
{
  "track_id": "auth_20250115",
  "beads_epic": "bd-a3f8",
  "beads_tasks": {
    "phase1": "bd-a3f8.1",
    "phase1_task1": "bd-a3f8.1.1",
    "phase1_task2": "bd-a3f8.1.2",
    "phase2": "bd-a3f8.2",
    "phase2_task1": "bd-a3f8.2.1"
  }
}
```

**Key naming convention:**
- Phase keys: `phase{N}` (1-indexed)
- Task keys: `phase{N}_task{M}` (both 1-indexed)

**Usage during implementation:**
1. Store `beads_enabled=true` flag when Beads is detected
2. Load `beads_tasks` mapping from metadata.json
3. Generate task key from current phase/task index
4. Look up Beads ID from mapping
5. Use Beads ID for `bd update` and `bd close` commands

### Bidirectional Sync

1. **plan.md → Beads**: Tasks created/updated in plan sync to Beads
2. **Beads → plan.md**: Status changes reflect back to plan markers
3. **Conflict resolution**: Beads state is authoritative for status

## What Conductor READS from Beads

| What | Command | Used For |
|------|---------|----------|
| **Ready tasks** | `bd ready --epic <id>` | Task selection (dependency-aware, no blockers) |
| **Epic notes** | `bd show <epic_id>` | Session resume, context recovery after compaction |
| **Task status** | `bd show <task_id>` | Verify current state, check blockers |
| **Blocked info** | `bd show <task_id>` | Understand what's blocking and why |

### Context Recovery Flow (after compaction)

```bash
# 1. Read epic to get last session context
bd show <epic_id>
# Returns notes: "COMPLETED: Phase 1, IN PROGRESS: auth middleware, NEXT: add rate limiting"

# 2. Get ready tasks
bd ready --epic <epic_id>
# Returns: tasks with no blockers, sorted by priority

# 3. Resume with full context even if conversation history is gone
```

## What Conductor WRITES to Beads

| When | Command | Data Written |
|------|---------|--------------|
| **Task start** | `bd update --status in_progress` | Status change |
| **Task complete** | `bd close --reason` | Completion + commit SHA |
| **Task blocked** | `bd update --status blocked` | Block reason |
| **Phase complete** | `bd update --notes` | COMPLETED/IN PROGRESS/NEXT summary |
| **Handoff** | `bd update --notes` | Full session context for recovery |

## Chemistry Patterns (Molecules)

Beads supports workflow templates called "molecules" for multi-step work:

### When to Use Molecules vs Direct Tasks

| Use Case | Approach | Command |
|----------|----------|---------|
| Single task | Direct Beads task | `bd create` + `bd close` |
| Multi-step workflow | Persistent molecule | `bd mol pour <proto>` |
| Ephemeral/patrol work | Wisp (vapor) | `bd mol wisp <proto>` |
| Reusable template | Formula → Proto | `bd cook <formula>` |

### Molecule Navigation

```bash
# Where am I in the molecule?
bd mol current

# Close step and auto-advance to next
bd close <step-id> --continue

# Squash completed molecule to digest
bd mol squash <molecule-id>
```

### Mol vs Wisp Decision Tree

```
Is this work permanent/auditable?
├─ YES → Use `bd mol pour` (persistent, synced to git)
│   └─ Examples: Feature tracks, bug fixes, releases
│
└─ NO → Use `bd mol wisp` (ephemeral, never synced)
    └─ Examples: Patrol cycles, health checks, temp exploration
```

### Molecule Lifecycle

```
Formula (source TOML) ─── "Ice-9"
    │
    ▼ bd cook
Protomolecule (frozen template) ─── Solid
    │
    ├─▶ bd mol pour ──▶ Mol (persistent) ──▶ bd squash ──▶ Digest
    │
    └─▶ bd mol wisp ──▶ Wisp (ephemeral) ──┬▶ bd squash ──▶ Digest
                                           └▶ bd burn ──▶ (gone)
```

## Graph Links Integration

Conductor can leverage Beads graph links for richer track relationships:

| Link Type | Conductor Use Case | Command |
|-----------|-------------------|---------|
| `relates_to` | Link related tracks/tasks | `bd relate <id1> <id2>` |
| `supersedes` | Spec revisions (v1 → v2) | `bd supersede <old> --with <new>` |
| `duplicate_of` | Deduplicate similar tasks | `bd duplicate <dup> --of <canonical>` |
| `discovered_from` | Work discovered during implementation | `bd create --deps discovered-from:<parent>` |

### Track Relationship Example

```bash
# Two tracks are related
bd relate conductor_auth conductor_security

# Spec revision supersedes old version
bd supersede bd-spec-v1 --with bd-spec-v2

# During implementation, find duplicate task
bd duplicate bd-task-dup --of bd-task-canonical
```

## Messaging Integration

When using parallel workers, Beads messaging can coordinate between agents:

```bash
# Coordinator notifies worker
bd mail send worker_1/ -s "Task assigned" -m "Work on bd-task-123"

# Worker reports completion
bd mail reply msg-123 -m "Task completed, commit abc1234"

# Check for worker updates
bd mail inbox
```

> **Note:** Messaging requires a mail delegate (e.g., `gt mail`). If not configured, use `bd update --notes` for coordination instead.

> **Tip:** Use the `decision` issue type (`bd create -t decision`) for architectural decisions made during `/conductor-revise`.

## Configuration

Enable integration via `conductor/beads.json`:

```json
{
  "enabled": true,
  "auto_sync": true,
  "epic_prefix": "conductor",
  "sync_on_implement": true
}
```

| Field | Type | Description |
|-------|------|-------------|
| `enabled` | boolean | Enable/disable Beads integration |
| `auto_sync` | boolean | Sync on every task completion |
| `epic_prefix` | string | Prefix for Beads epic IDs |
| `sync_on_implement` | boolean | Sync before `/conductor-implement` |

## Command Mapping

| Conductor Command | Beads Operations | Description |
|-------------------|------------------|-------------|
| `/conductor-setup` | `bd init` | Initialize Beads in project |
| `/conductor-newtrack` | `bd create` (epic + tasks) | Create epic with linked tasks |
| `/conductor-implement` | `bd ready` → `bd update` → `bd done` | Get next task, track progress, complete |
| `/conductor-status` | `bd ready`, `bd show` | Show available tasks, epic status |
| `/conductor-block` | `bd update --status blocked` | Mark task blocked with reason |
| `/conductor-skip` | `bd update --status skipped` | Skip task with justification |
| `/conductor-archive` | `bd admin compact --auto` | Archive completed epics |

### Example Flow

```bash
# User runs /conductor-implement

# 1. Conductor checks Beads for ready tasks
bd ready --epic auth_20250115

# 2. Agent works on task, updates status
bd update TASK-001 --status in_progress

# 3. On completion
bd close TASK-001 --reason "Implemented JWT validation"

# 4. Conductor updates plan.md markers
[x] Task 1: Implement JWT validation <!-- SHA: abc123 -->
```

## Benefits

### Cross-Session Memory
- Task state persists across context compaction
- Agent resumes exactly where it left off
- No re-reading of entire plan.md needed

### Compaction Survival Notes

When updating task status, use structured notes for recovery after compaction:

```bash
bd update TASK-001 --notes "COMPLETED: JWT validation with RS256
KEY DECISION: RS256 over HS256 for key rotation
IN PROGRESS: Password reset flow
NEXT: Implement rate limiting
BLOCKER: None
DISCOVERED: Found race condition in token refresh (created bd-xyz)"
```

**Notes format (use ALL relevant fields):**
- `COMPLETED:` - What was finished (past tense, specific)
- `KEY DECISION:` - Important choices made (with rationale)
- `IN PROGRESS:` - Current work
- `NEXT:` - Immediate next step (concrete action)
- `BLOCKER:` - What's blocking (if any)
- `DISCOVERED:` - New issues found during work (with beads ID)

**Best practices:**
- Write notes as if explaining to someone with zero context
- Include technical specifics, not vague progress
- Update notes BEFORE session end or handoff
- Use `bd dolt push` after updating notes to ensure persistence

This enables full context recovery after compaction with zero conversation history.

### Discovered-From Tracking

When discovering new work during implementation, link it:

```bash
# Create discovered issue with automatic linking
bd create "Found race condition in token refresh" \
  -t bug -p 2 \
  --deps discovered-from:<current_task_id> \
  --json
```

**Benefits:**
- Builds graph of agent thinking and discovery
- Reconstructs context: "Why was this created?"
- Tracks work trail: "What else was found?"

### Dependency-Aware Selection
- `bd ready` returns only unblocked tasks
- Respects task dependencies defined in plan
- Prevents out-of-order execution

### Audit Trail
- Every status change logged in Beads
- Notes attached to task completions
- Full history survives compaction

## Fallback Behavior (IMPORTANT)

### Decision Flow

```
┌─────────────────────────────────────────────────────────────┐
│ Step 1: Is `bd` command installed?                          │
│   Run: which bd > /dev/null 2>&1                            │
│   NO  → STOP. Do not use any bd commands.                   │
│   YES → Continue to Step 2                                  │
├─────────────────────────────────────────────────────────────┤
│ Step 2: Does conductor/beads.json exist?                    │
│   NO  → STOP. Do not use any bd commands.                   │
│   YES → Continue to Step 3                                  │
├─────────────────────────────────────────────────────────────┤
│ Step 3: Is "enabled": true in conductor/beads.json?         │
│   NO  → STOP. Do not use any bd commands.                   │
│   YES → Use Beads integration                               │
└─────────────────────────────────────────────────────────────┘
```

### Failure Handling

| Scenario | Behavior |
|----------|----------|
| `bd` not installed | Conductor operates normally without Beads |
| `bd` command fails | Log warning, continue with plan.md only |
| Sync conflict | Beads state wins, plan.md updated |
| `.beads/` missing | Auto-initialize on next command |

### Detection Logic

```python
# Pseudo-code for integration check
def should_use_beads():
    if not shutil.which("bd"):
        return False
    if not Path("conductor/beads.json").exists():
        return False
    config = load_json("conductor/beads.json")
    return config.get("enabled", False)
```

## Parallel Execution Integration

When Conductor executes tasks in parallel, Beads provides coordination:

### Worker Assignment Protocol

Each parallel worker claims its task with a unique assignee:

```bash
# Coordinator assigns tasks to workers
bd update <task_id> --status in_progress --assignee worker_1_auth --json

# Worker can query only its assigned tasks
bd ready --assignee worker_1_auth --json
```

### Parallel Worker Best Practices

1. **Claim Before Work:**
   ```bash
   # Worker claims task at start
   bd update <task_id> --status in_progress \
     --assignee <worker_id> \
     --notes "Started: <task_description>" \
     --json
   ```

2. **Use Hash IDs for New Tasks:**
   - Beads hash-based IDs prevent collisions when parallel workers create tasks
   - Each worker can safely run `bd create` without coordination

3. **Discovered Issues:**
   ```bash
   # Worker finds new issue during work
   bd create "Found race condition" \
     -t bug -p 2 \
     --deps discovered-from:<current_task_id> \
     --assignee <worker_id> \
     --json
   ```

4. **Completion with Sync:**
   ```bash
   # Worker completes task
   bd close <task_id> --reason "Completed" --json
   
   # CRITICAL: Force push after parallel work
   bd dolt push
   ```

### Coordinator Protocol

The coordinator manages parallel workers through Beads:

```bash
# 1. Before spawning workers
for task in parallel_tasks:
    bd update <task_id> --status in_progress \
      --assignee worker_<N>_<name> \
      --json

# 2. Workers execute independently
# (Each worker updates its own assigned task)

# 3. After all workers complete
bd dolt push  # Force push all changes
bd ready --epic <epic_id> --json  # Verify all complete
```

### Concurrent Safety

Beads handles concurrent updates safely:

| Scenario | Beads Behavior |
|----------|----------------|
| **Multiple `bd update` simultaneously** | Dolt transactions serialize writes |
| **Same task updated by two workers** | Last writer wins (avoid via assignee) |
| **Parallel `bd create`** | Hash IDs prevent collisions |
| **Rapid status changes** | Batch auto-commit mode reduces commit bloat |

### Notes Format for Parallel Context

Workers should include worker context in notes:

```bash
bd update <task_id> --notes "WORKER: worker_1_auth
TASK: Create auth module
FILES: src/auth/index.ts, src/auth/index.test.ts
STATUS: Completed
COMMIT: abc1234
DURATION: 5 min"
```

### Error Recovery

If a worker fails mid-execution:

```bash
# Coordinator detects worker timeout/failure
bd update <task_id> --status open \
  --notes "Worker failed/timed out. Reassigning." \
  --json

# Clear assignee for retry
bd update <task_id> --assignee "" --json
```

## Quick Reference

```bash
# Check if Beads is available
which bd

# Start Dolt server (required v0.56+)
bd dolt start

# Initialize integration
bd init
echo '{"enabled": true}' > conductor/beads.json

# View current state
bd show --epic conductor_<track_id>

# See what's ready to work on
bd ready

# Push changes to remote
bd dolt push
```
