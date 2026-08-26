# Python Style Guide

> Applies to all `.py` in the repo. Based on PEP 8, enforced by `ruff`.

## Formatting & Linting
- `ruff` for lint + format; do not mix with `black`/`flake8`.
- 88-char line length; single quotes preferred; import sorting via ruff.
- `ruff check .` and `ruff format --check .` must pass in CI.

## Type Hints
- Annotate all public functions and dataclass fields.
- Prefer `collections.abc` / `typing` modern forms
  (`list[str]`, `dict[str, int]`, `X | None`) for 3.10+.
- Use `# type: ignore[code]` with a comment explaining why. Never blanket
  ignore.

## Dataclasses & Models
- Use `@dataclass(frozen=True)` for immutable value objects
  (`EngineInfo`, `TTSRequest`, `TTSProgress`) and `@dataclass` for mutable
  state (`Settings`).
- Keep models free of logic; place logic in services/controllers.

## Concurrency
- Never mutate shared state from the UI thread.
- Worker/engine objects owned by exactly one thread; serialize requests
  through a queue. Document ownership in the module docstring.

## Naming
- `snake_case` functions/vars, `PascalCase` classes, `SCREAMING_SNAKE`
  constants. Private helpers prefixed `_`.

## Imports
- Stdlib, third-party, first-party group order; one import per line from
  `from`, and avoid wildcard imports.
