---
description: Extract reusable template from completed track
argument-hint: <track_id> [--as <template_name>]
---

<!-- 
SYSTEM DIRECTIVE: You are an AI agent for the Conductor framework.
CRITICAL: Validate every tool call. If any fails, halt and announce the failure.
-->

# Conductor Distill

Extract reusable template from track: $ARGUMENTS

---

## 1.0 BEADS CHECK

**PROTOCOL: Verify Beads is available for template extraction.**

1. **Check Beads CLI:** Run `which bd`
   - If NOT found:
     > "⚠️ Beads CLI (`bd`) is required for template extraction."
     > "Install from: https://github.com/steveyegge/beads"
     - HALT

2. **Check Beads Initialization:**
   - If `.beads/` directory does NOT exist:
     > "⚠️ Beads is not initialized in this project."
     > "Run `bd init` first, or use `/conductor-setup` to set up the project."
     - HALT

---

## 2.0 TRACK SELECTION

**PROTOCOL: Identify track to extract as template.**

### 2.1 Parse Arguments

**If track_id provided:**
- Validate track exists in `conductor/tracks/<track_id>/`
- Load `conductor/tracks/<track_id>/metadata.json`

**If no track_id:**
1. **List Completed Tracks:**
   - Read `conductor/tracks.md`
   - Find tracks marked `[x]` (completed)
   
2. **If completed tracks found:**
   > "Select a completed track to extract as template:"
   > 
   > | # | Track ID | Description |
   > |---|----------|-------------|
   > | 1 | `<id>` | `<description>` |
   > 
   > "Enter track number or ID:"

3. **If no completed tracks:**
   > "⚠️ No completed tracks found."
   > "Complete a track using `/conductor-implement` first, then extract it as a template."
   - HALT

### 2.2 Validate Track

1. **Check Track Status:**
   - If track is NOT marked `[x]` in `conductor/tracks.md`:
     > "⚠️ Track '<track_id>' is not completed."
     > "Current status: `<status>`"
     > 
     > "Would you like to:"
     > A) Extract anyway (may have incomplete patterns)
     > B) Complete the track first with `/conductor-implement`
     - If B: HALT

2. **Check Beads Epic:**
   - Read `conductor/tracks/<track_id>/metadata.json`
   - Extract `beads_epic` ID
   - If no Beads epic linked:
     > "⚠️ This track has no Beads integration."
     > "Templates are extracted from Beads epics."
     - HALT

---

## 3.0 TEMPLATE EXTRACTION

**PROTOCOL: Extract template from track's Beads epic.**

### 3.1 Determine Template Name

**If `--as <name>` provided:**
- Use specified name

**Otherwise:**
- Derive from track description
- Convert to kebab-case: "Add User Authentication" → "add-user-authentication"
- Ask for confirmation:
  > "Suggested template name: `<derived_name>`"
  > "Press Enter to accept, or type a different name:"

### 3.2 Analyze Track for Variables

Read the following files and identify patterns that should become template variables:

1. **`conductor/tracks/<track_id>/spec.md`:**
   - Look for specific names, versions, dates that could be parameterized
   - Example: "OAuth 2.0" → `{{auth_protocol}}`

2. **`conductor/tracks/<track_id>/plan.md`:**
   - Look for specific file paths, module names
   - Example: "src/auth/" → `{{module_path}}`

3. **Propose Variables:**
   > "I found the following values that could become template variables:"
   > 
   > | Value | Suggested Variable | Occurrences |
   > |-------|-------------------|-------------|
   > | `auth` | `{{module_name}}` | 5 |
   > | `JWT` | `{{token_type}}` | 3 |
   > 
   > "Would you like to add, remove, or modify any variables? (Enter to accept)"

### 3.3 Extract Template

```bash
bd mol distill <beads_epic_id> \
  --as "<template_name>" \
  --var <value1>=<var1> \
  --var <value2>=<var2> \
  --json
```

**Handle Success:**
- Extract proto ID from response
- Announce:
  > "## Template Extracted Successfully"
  > 
  > **Template Name:** `<template_name>`
  > **Proto ID:** `<proto_id>`
  > **Variables:** `<var_list>`
  > 
  > **Usage:**
  > ```bash
  > # Create persistent track from template
  > bd mol pour <template_name> --var module_name=payments
  > 
  > # Create ephemeral exploration
  > bd mol wisp <template_name> --var module_name=cache
  > /conductor-wisp <template_name> --var module_name=cache
  > ```
  > 
  > **View template:**
  > ```bash
  > /conductor-formula show <template_name>
  > bd mol show <template_name>
  > ```

**Handle Failure:**
- Display error message
- Suggest manual extraction steps

---

## 4.0 REGISTER WITH CONDUCTOR (Optional)

**PROTOCOL: Create Conductor-specific template metadata.**

1. **Create Template Directory:**
   - Create `conductor/templates/<template_name>/` if it doesn't exist

2. **Create Template Metadata:**
   ```json
   {
     "name": "<template_name>",
     "beads_proto": "<proto_id>",
     "source_track": "<track_id>",
     "variables": {
       "module_name": {
         "description": "Name of the module",
         "default": null,
         "required": true
       }
     },
     "created_at": "<timestamp>",
     "created_from": "<track_description>"
   }
   ```

3. **Copy Template Spec:**
   - Copy `conductor/tracks/<track_id>/spec.md` to `conductor/templates/<template_name>/spec.template.md`
   - Replace specific values with `{{variable}}` placeholders

4. **Copy Template Plan:**
   - Copy `conductor/tracks/<track_id>/plan.md` to `conductor/templates/<template_name>/plan.template.md`
   - Replace specific values with `{{variable}}` placeholders

5. **Announce:**
   > "Template also registered in Conductor at `conductor/templates/<template_name>/`"

---

## 5.0 CLEANUP

**PROTOCOL: Offer to archive source track after extraction.**

> "The source track '<track_id>' has been used to create a template."
> "Would you like to archive it?"
> 
> A) **Archive** - Move to `conductor/archive/`
> B) **Keep** - Leave track in place
> C) **Delete** - Permanently remove track (template already extracted)

**If A (Archive):**
- Run `/conductor-archive <track_id>` workflow

**If C (Delete):**
- Confirm: "Are you sure? The template has been extracted, but the original track will be permanently deleted. (yes/no)"
- If yes: Delete `conductor/tracks/<track_id>/`

---

## 6.0 TEMPLATE USAGE GUIDE

> ### Using Your Template
> 
> **List all templates:**
> ```bash
> /conductor-formula list
> bd formula list
> ```
> 
> **View template structure:**
> ```bash
> /conductor-formula show <template_name>
> ```
> 
> **Create new track from template:**
> ```bash
> # Persistent (auditable, for features/bugs)
> bd mol pour <template_name> --var module_name=payments
> 
> # Ephemeral (exploration, no audit trail)
> bd mol wisp <template_name> --var module_name=cache
> /conductor-wisp <template_name> --var module_name=cache
> ```
> 
> **Best Practices:**
> 1. Extract templates from successful, well-structured tracks
> 2. Use descriptive variable names (`{{module_name}}` not `{{x}}`)
> 3. Include default values where sensible
> 4. Document variable purposes in template metadata
