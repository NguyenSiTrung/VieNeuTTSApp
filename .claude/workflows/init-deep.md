---
description: Deep initialization — generate hierarchical context files throughout the project for agent auto-discovery
---

# /init-deep — Deep Context Initialization

Generate hierarchical context files (like `AGENTS.md` or `CONTEXT.md`) throughout the project that agents automatically read for directory-specific context.

## Steps

1. **Analyze project structure**
   - Run `find . -type d -not -path './.git/*' -not -path './node_modules/*' -not -path './.next/*' -not -path './dist/*' | head -50` to discover directories
   - Identify the depth and breadth of the project

2. **Classify each directory**
   For each significant directory, determine:
   - What code lives here (components, utils, API routes, etc.)
   - Key patterns and conventions used
   - Important files and their roles
   - Relationships to other directories

3. **Generate root context file**
   Create `AGENTS.md` (or `CONTEXT.md`) at the project root:
   ```markdown
   # Project Context
   - Project type, tech stack, key dependencies
   - Architecture overview (brief)
   - Key conventions (naming, file structure, patterns)
   - Entry points and important files
   ```

4. **Generate directory-specific context files**
   For directories with 3+ code files, create context files:
   ```
   project/
   ├── AGENTS.md              ← project-wide context
   ├── src/
   │   ├── AGENTS.md          ← src-specific context
   │   ├── components/
   │   │   └── AGENTS.md      ← component-specific context
   │   └── api/
   │       └── AGENTS.md      ← API-specific context
   ```

5. **Content for each context file should include**:
   - Purpose of this directory
   - Key files and what they do
   - Patterns to follow when adding new code here
   - Dependencies and relationships
   - Testing conventions for this area

6. **Skip directories that don't need context files**:
   - `node_modules/`, `.git/`, `dist/`, `.next/`, `build/`
   - Directories with only 1-2 files (unless they're critical)
   - Auto-generated directories

7. **Verify**
   - Check that context files don't contain sensitive information
   - Ensure they add `.agents.md` or `AGENTS.md` to `.gitignore` if desired
   - Report: "Created N context files across M directories"

## Options

- `--max-depth=N`: Maximum directory depth to traverse (default: 4)
- `--create-new`: Only create new files, don't update existing ones
- `--format=agents|context`: Choose filename format (`AGENTS.md` vs `CONTEXT.md`)

## Notes

- This is a one-time operation that can be re-run to update
- Context files are meant to be committed to the repo
- Agents automatically read the nearest context file when working in a directory
