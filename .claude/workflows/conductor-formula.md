---
description: List and manage track workflow templates (Beads formulas)
argument-hint: [list|show <name>]
---

<!-- 
SYSTEM DIRECTIVE: You are an AI agent for the Conductor framework.
CRITICAL: Validate every tool call. If any fails, halt and announce the failure.
-->

# Conductor Formula

Manage track workflow templates: $ARGUMENTS

---

## 1.0 BEADS CHECK

**PROTOCOL: Verify Beads is available for formula management.**

1. **Check Beads CLI:** Run `which bd`
   - If NOT found:
     > "⚠️ Beads CLI (`bd`) is required for formula management."
     > "Install from: https://github.com/steveyegge/beads"
     - HALT

2. **Check Beads Initialization:**
   - If `.beads/` directory does NOT exist:
     > "⚠️ Beads is not initialized in this project."
     > "Run `bd init` first, or use `/conductor-setup` to set up the project."
     - HALT

---

## 2.0 PARSE SUBCOMMAND

**PROTOCOL: Determine action from arguments.**

1. **Parse $ARGUMENTS:**

   **If empty or "list":**
   - Go to section 3.0 (List Formulas)

   **If "show <name>":**
   - Extract formula name
   - Go to section 4.0 (Show Formula Details)

   **If "create":**
   - Announce: "To create a new formula, use `/conductor-distill <track_id>` to extract a template from a completed track."
   - HALT

   **Otherwise:**
   - Announce available subcommands:
     > "Usage: /conductor-formula [list|show <name>]"
     > 
     > **Subcommands:**
     > - `list` - List all available formulas (default)
     > - `show <name>` - Show formula structure and variables
     > 
     > **Related commands:**
     > - `/conductor-distill <track_id>` - Create formula from completed track
     > - `/conductor-wisp` - Create ephemeral exploration track
   - HALT

---

## 3.0 LIST FORMULAS

**PROTOCOL: Display available workflow templates.**

1. **Query Beads:**
   ```bash
   bd formula list --json
   ```

2. **Handle Empty Result:**
   - If no formulas found:
     > "No formulas found in this project."
     > 
     > **To create a formula:**
     > 1. Complete a track using `/conductor-implement`
     > 2. Run `/conductor-distill <track_id>` to extract a reusable template
     > 
     > **Or use Beads directly:**
     > ```bash
     > bd mol distill <epic-id> --as "Template Name"
     > ```

3. **Display Results:**
   > "## Available Formulas (Track Templates)"
   > 
   > | Formula | Description | Variables |
   > |---------|-------------|-----------|
   > | `<name>` | `<description>` | `<var_list>` |
   > 
   > **Usage:**
   > - `/conductor-formula show <name>` - View formula details
   > - `bd mol pour <name>` - Create persistent track from formula
   > - `bd mol wisp <name>` - Create ephemeral exploration from formula
   > - `/conductor-wisp <name>` - Quick ephemeral track

---

## 4.0 SHOW FORMULA DETAILS

**PROTOCOL: Display formula structure and required variables.**

1. **Query Beads:**
   ```bash
   bd mol show <formula_name> --json
   ```

2. **Handle Not Found:**
   - If formula not found:
     > "Formula '<name>' not found."
     > "Run `/conductor-formula list` to see available formulas."
   - HALT

3. **Display Structure:**
   > "## Formula: <name>"
   > 
   > **Description:** <description>
   > 
   > ### Variables
   > | Variable | Default | Description |
   > |----------|---------|-------------|
   > | `{{var1}}` | `<default>` | `<desc>` |
   > 
   > ### Phases
   > ```
   > <phase_tree>
   > ```
   > 
   > ### Usage Examples
   > ```bash
   > # Create persistent track (auditable, synced to git)
   > bd mol pour <name> --var key=value
   > 
   > # Create ephemeral exploration (no audit trail)
   > bd mol wisp <name> --var key=value
   > /conductor-wisp <name> --var key=value
   > ```

---

## 5.0 CONDUCTOR INTEGRATION NOTES

**PROTOCOL: Explain how formulas work with Conductor.**

After listing or showing formulas, include:

> ### How Formulas Work with Conductor
> 
> Beads formulas are reusable workflow templates. When you complete a track, you can extract it as a formula for reuse.
> 
> | Beads Concept | Conductor Mapping |
> |---------------|-------------------|
> | **Formula** | Track template (reusable pattern) |
> | **Proto** | Frozen template (ready to use) |
> | **Mol** | Feature track (persistent, auditable) |
> | **Wisp** | Quick exploration (ephemeral, no clutter) |
> 
> **Workflow:**
> 1. Create and complete a track: `/conductor-newtrack` → `/conductor-implement`
> 2. Extract as formula: `/conductor-distill <track_id>`
> 3. Reuse for new work: `bd mol pour <formula>` or `/conductor-wisp <formula>`
