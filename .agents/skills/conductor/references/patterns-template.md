# Codebase Patterns

Reusable patterns discovered during development. **Read this before starting new work.**

This file is the project's institutional knowledge - learnings extracted from completed tracks that help future development.

---

## Code Conventions

<!-- Patterns related to code style, naming, imports -->

- *Example: Use `sql<number>` template for aggregations (from: db_20250101)*
- *Example: Always use barrel exports in `src/*/index.ts` (from: api_20250102)*

## Architecture

<!-- Patterns related to architecture decisions, module structure -->

- *Example: All validation uses Zod schemas in `lib/schemas/` (from: auth_20250103)*
- *Example: API routes follow `/api/v1/<resource>/<action>` pattern (from: api_20250102)*

## Gotchas

<!-- Common mistakes to avoid, things that are easy to forget -->

- *Example: Don't forget to update `index.ts` barrel exports when adding new modules (from: auth_20250103)*
- *Example: Always use `IF NOT EXISTS` for database migrations (from: db_20250101)*
- *Example: Run `pnpm build` before testing - types aren't auto-updated (from: setup_20250101)*

## Testing

<!-- Patterns for testing approaches -->

- *Example: Use `vitest` for unit tests, `playwright` for E2E (from: testing_20250104)*
- *Example: Mock external APIs in `__mocks__/` directory (from: api_20250102)*

## Context

<!-- Useful context about where things are located -->

- *Example: Settings panel is in `src/components/settings/SettingsPanel.tsx` (from: settings_20250105)*
- *Example: Auth logic lives in `src/lib/auth/`, UI in `src/components/auth/` (from: auth_20250103)*

---

Last refreshed: YYYY-MM-DD
