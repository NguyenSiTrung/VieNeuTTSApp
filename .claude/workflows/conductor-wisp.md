---
description: Create ephemeral exploration track (no audit trail)
argument-hint: [formula_name] [--var key=value]
---

<!-- 
SYSTEM DIRECTIVE: You are an AI agent for the Conductor framework.
CRITICAL: Validate every tool call. If any fails, halt and announce the failure.
-->

# Conductor Wisp

Create ephemeral exploration track: $ARGUMENTS

---

## 1.0 BEADS CHECK

**PROTOCOL: Verify Beads is available.**

1. **Check Beads CLI:** Run `which bd`
   - If NOT found:
     > "⚠️ Beads CLI (`bd`) is required for wisp creation."
     > "Install from: https://github.com/steveyegge/beads"
     - HALT

2. **Check Beads Initialization:**
   - If `.beads/` directory does NOT exist:
     > "⚠️ Beads is not initialized in this project."
     > "Run `bd init` first, or use `/conductor-setup` to set up the project."
     - HALT

---

## 2.0 UNDERSTAND WISPS

**PROTOCOL: Explain ephemeral exploration tracks.**

Wisps are **ephemeral** workflow instances that:
- Live in `.beads-wisp/` (separate from `.beads/`)
- Are **never** synced to git
- Leave no permanent audit trail
- Perfect for exploration, debugging, quick fixes

**Use wisps when:**
- Exploring an approach before committing to it
- Quick debugging session
- Temporary work that shouldn't clutter history
- Patrol cycles, health checks, routine monitoring

**Use persistent tracks (`bd mol pour`) when:**
- Feature development
- Bug fixes that need audit trail
- Work spanning multiple sessions
- Team coordination required

---

## 3.0 CREATE WISP

**PROTOCOL: Create ephemeral exploration track.**

### 3.1 Parse Arguments

**If formula name provided:**
- Use the specified formula
- Extract any `--var key=value` arguments

**If no formula name:**
1. **List Available Formulas:**
   ```bash
   bd formula list --json
   ```

2. **If formulas exist:**
   > "Available formulas for ephemeral exploration:"
   > 
   > | # | Formula | Description |
   > |---|---------|-------------|
   > | 1 | `<name>` | `<desc>` |
   > 
   > "Enter formula number or name, or describe what you want to explore:"

3. **If no formulas:**
   > "No formulas found. Creating ad-hoc exploration wisp."
   > "What do you want to explore? (Describe briefly)"
   - Wait for user input
   - Go to section 3.3 (Ad-hoc Wisp)

### 3.2 Create Wisp from Formula

```bash
bd mol wisp <formula_name> [--var key=value ...] --json
```

**Handle Success:**
- Extract wisp ID from response
- Display:
  > "## Ephemeral Exploration Started"
  > 
  > **Wisp ID:** `<wisp_id>`
  > **Formula:** `<formula_name>`
  > 
  > **Current step:**
  > ```bash
  > bd mol current --json
  > ```
  > 
  > **Remember:** This wisp is ephemeral - it won't be synced to git.
  > 
  > **Navigation:**
  > - `bd mol current` - See current step
  > - `bd close <step-id> --continue` - Complete step, advance to next
  > - `bd mol squash <wisp-id>` - Save summary when done (optional)
  > - `bd mol burn <wisp-id>` - Delete without trace

**Handle Failure:**
- Display error message
- Suggest alternatives

### 3.3 Create Ad-hoc Wisp

If no formula specified and user describes exploration:

1. **Create Quick Exploration Epic:**
   ```bash
   bd create "Exploration: <user_description>" -t epic -p 3 --json
   ```

2. **Create as Wisp:**
   ```bash
   bd mol wisp <epic_id> --json
   ```

3. **Announce:**
   > "## Ad-hoc Exploration Started"
   > 
   > **Wisp ID:** `<wisp_id>`
   > **Topic:** `<description>`
   > 
   > This is an ephemeral exploration. Add tasks as you discover work:
   > ```bash
   > bd create "Found something" -p 2 --deps discovered-from:<wisp_id> --json
   > ```
   > 
   > When done:
   > - `bd mol squash <wisp_id>` - Create digest of findings
   > - `bd mol burn <wisp_id>` - Delete without trace

---

## 4.0 WISP MANAGEMENT

**PROTOCOL: Commands for managing active wisps.**

### List Active Wisps

```bash
bd mol wisp list --json
```

### End Wisp with Digest

When exploration yields useful findings:

```bash
# Create summary of exploration
bd mol squash <wisp_id> --summary "Found: <key findings>" --json
```

This creates a permanent digest summarizing the wisp's work.

### Burn Wisp (No Trace)

When exploration has no archival value:

```bash
bd mol burn <wisp_id> --json
```

This deletes the wisp completely with no trace.

---

## 5.0 TRANSITION TO PERSISTENT TRACK

**PROTOCOL: Convert exploration to permanent track if valuable.**

If exploration reveals work worth tracking:

> "This exploration found valuable work. Would you like to:"
> 
> A) **Convert to persistent track** - Create full Conductor track from findings
> B) **Create follow-up issues** - Add discovered work to existing track
> C) **Squash with digest** - Summarize findings and close wisp
> D) **Burn** - Discard exploration completely

**If A (Convert to track):**
1. Use `/conductor-newtrack` with exploration findings
2. Reference wisp discoveries in spec
3. Burn original wisp after track created

**If B (Create follow-up issues):**
```bash
# For each discovery
bd create "<finding>" -t <type> -p <priority> --deps discovered-from:<parent> --json
```

---

## 6.0 QUICK REFERENCE

> ### Wisp Quick Reference
> 
> | Action | Command |
> |--------|---------|
> | Create from formula | `bd mol wisp <formula>` |
> | Create ad-hoc | `bd create "Explore X" -t epic && bd mol wisp <id>` |
> | See current step | `bd mol current` |
> | Complete step | `bd close <step> --continue` |
> | List wisps | `bd mol wisp list` |
> | Save with summary | `bd mol squash <wisp> --summary "..."` |
> | Delete completely | `bd mol burn <wisp>` |
> | Clean up orphans | `bd mol wisp gc` |
