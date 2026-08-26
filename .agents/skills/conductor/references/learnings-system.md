# Learnings System Reference

The learnings system captures and consolidates patterns, gotchas, and context discovered during implementation, inspired by [Ralph's progress.txt pattern](https://github.com/snarktank/ralph).

## File Locations

### Project-Level: `conductor/patterns.md`

Consolidated patterns from all tracks. Read at session start to prime context.

```markdown
# Codebase Patterns

Reusable patterns discovered during development. Read this before starting new work.

## Code Conventions
- Use `sql<number>` template for aggregations (from: db_20250101)
- Always use barrel exports in `src/*/index.ts` (from: api_20250102)

## Architecture
- All validation uses Zod schemas in `lib/schemas/` (from: auth_20250103)

## Gotchas
- Don't forget to update `index.ts` barrel exports when adding modules (from: auth_20250103)
- Always use `IF NOT EXISTS` for database migrations (from: db_20250101)

## Testing
- Use `vitest` for unit tests, `playwright` for E2E (from: testing_20250104)

## Context
- Settings panel is in `src/components/settings/SettingsPanel.tsx` (from: settings_20250105)

---
Last refreshed: 2025-01-09
```

### Per-Track: `conductor/tracks/<track_id>/learnings.md`

Append-only log of discoveries during implementation.

```markdown
# Track Learnings: auth_20250109

Patterns, gotchas, and context discovered during implementation.

## Codebase Patterns (Inherited)

- All validation uses Zod schemas in `lib/schemas/`
- Use barrel exports in `src/*/index.ts`

---

## [2025-01-09 14:30] - Phase 1 Task 2: Add auth middleware
Thread: https://ampcode.com/threads/T-xxx
- **Implemented:** JWT validation middleware
- **Files changed:** src/auth/middleware.ts, src/auth/types.ts
- **Commit:** abc1234
- **Learnings:**
  - Patterns: This codebase uses Zod for all validation
  - Gotchas: Must update index.ts barrel exports when adding modules
  - Context: Auth module owns all JWT logic
---

## [2025-01-09 15:45] - REVISION #1
Thread: https://ampcode.com/threads/T-xxx
- **Type:** Plan
- **Trigger:** Discovered need for rate limiting middleware
- **Learning:**
  - Gotcha: Always consider rate limiting for auth endpoints
  - Pattern: Add rate limiting task to any auth-related track
---
```

## Workflow Integration

### On `/conductor-newtrack`

1. Read `conductor/patterns.md` if exists
2. Display: "📚 **Codebase Patterns:** Found X patterns from previous tracks"
3. Check `conductor/archive/` for similar tracks
4. Create `learnings.md` with inherited patterns header

### On `/conductor-implement`

1. **At Start:**
   - Read `conductor/patterns.md`
   - Read `conductor/tracks/<id>/learnings.md`
   - Display pattern count

2. **After Each Task:**
   - Append entry to `learnings.md`
   - Include: timestamp, thread URL, files, commit, learnings

3. **At Phase Completion:**
   - Review learnings from this phase
   - Prompt: "Elevate patterns to project level?"
   - If yes: Append to `conductor/patterns.md`

### On `/conductor-revise`

1. Append revision entry to `learnings.md`
2. Prompt: "This revision reveals a reusable lesson. Add to patterns?"
3. If yes: Append to `conductor/patterns.md`

### On `/conductor-handoff`

1. Read `learnings.md` entries since last handoff
2. Include summarized learnings in handoff document
3. Add thread URL for context retrieval

### On `/conductor-archive`

1. Read `learnings.md` for unextracted patterns
2. Prompt: "Extract patterns before archiving?"
3. Append selected patterns to `conductor/patterns.md`
4. Keep `learnings.md` in archived track folder

### On `/conductor-refresh`

1. Scan all `learnings.md` files in `conductor/tracks/*/` and `conductor/archive/*/`
2. Find patterns mentioned 2+ times across tracks
3. Display consolidation report
4. Update `conductor/patterns.md` with merged patterns

## Templates

Templates are bundled in the skill's `references/` folder:
- [patterns-template.md](patterns-template.md) - Full template for `conductor/patterns.md`
- [learnings-template.md](learnings-template.md) - Full template for track `learnings.md`

When creating files, use these templates as the base structure.

## Pattern Categories

| Category | What to Include |
|----------|-----------------|
| **Code Conventions** | Naming, imports, style patterns |
| **Architecture** | Module structure, API patterns |
| **Gotchas** | Common mistakes, easy-to-forget steps |
| **Testing** | Test frameworks, mocking patterns |
| **Context** | Where things are located, ownership |

## Benefits

1. **Compaction Survival** - Learnings persist in files, not conversation
2. **Cross-Session Memory** - New sessions inherit accumulated knowledge
3. **Pattern Reuse** - Avoid rediscovering the same patterns
4. **Institutional Knowledge** - Project builds knowledge over time
5. **Onboarding** - New developers (human or AI) get context fast
