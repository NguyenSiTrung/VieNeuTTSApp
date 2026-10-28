# Track Learnings: llm_provider_hardening_20261028

Patterns, gotchas, and context discovered during implementation.

## Codebase Patterns (Inherited)

📚 **222 patterns** available in `conductor/patterns.md`, plus learnings from `dub_pacing_alignment_20261026`, `asr_hardening_20261021` and `subtitles_ux_overhaul_20261023`. Most relevant here:

- **Quality gate per task**: `ruff check .` + `ruff format --check .` + `pytest` must all pass before committing (the format gate is `ruff format --check`, not `ruff check`); commit with a conventional prefix + `git notes add` task summary.
- **Settings load is all-or-nothing** (`Settings(**data)`): every new field clamps INDIVIDUALLY in `core/settings.py` and a bad value is DROPPED to its dataclass default — never widen the whole-file fallback. Python `bool` subclasses `int`: reject `isinstance(v, bool)` before `isinstance(v, int)`.
- **`_set_setting` signal table**: every new AI setting key must be listed in `AppController._set_setting`'s `(key, signal)` table (`aiSettingsChanged`) or QML bindings never refresh after a setter write (asr_hardening gotcha). Single-field settings writes are load-modify-save.
- **Staged render invalidation**: cue edits / ASR / diarization / LLM re-runs on a rendered project go through `_PendingInvalidation`; tests that mutate a rendered project must confirm it. The glossary (FR-6.3) must NOT go through it — it never changes audio.
- **Sparse fingerprint-payload evolution**: emit a JSON key only when its data is present, and keep new project fields out of `render_fingerprint` unless they change audio; pin the pre-change digest with a golden test.
- **`notes` out-param**: `asr.transcribe_media_to_cues(..., notes=[...])` is the existing pattern for non-fatal, user-facing summaries — mirror it in `refine_cues_with_llm`.
- **bg ops in tests**: controllers take an injectable `bg_runner`; tests use `run_sync` (or `DeferredRunner` for ordering). Every test building a controller after a slot goes async needs `bg_runner=run_sync`.
- **Network tests**: local `http.server.HTTPServer` on port 0 in a daemon thread (see `test_llm_refine.py::test_llm_refine_modes_and_connection_probe`), or the module-level `_post_chat_completion` seam; never real providers, never real backoff sleeps.
- **QML fakes**: smoke fake controllers need real NOTIFY `@Property` members (plain attributes read as `undefined`); list props use `@Property("QVariantList")`. Add new scenarios inside existing consolidated smoke groups — each QML instantiation costs ≥ 1 s.
- **Shared controls**: new text inputs use `AppTextField` (component-owned `disabledReason` / `Accessible.name`; custom skins mirror `control.text`), numeric inputs `AppNumberField`; keep existing objectNames stable.
- **i18n**: any `qsTr` / `self.tr` change → `scripts/update_i18n.sh`, translate the new `unfinished` entries in `src/vienetts_app/ui/i18n/vienetts_en.ts`, re-run the script to compile `.qm`, 0 unfinished.
- **Testing diet**: don't add default/getter-setter round-trip micro-tests; keep regression guards, error paths, security, cancel/ordering and cross-component contracts; merge same-function scenarios into existing tests.

## Audit evidence (2026-10-28, pre-implementation)

Reproduced against `_parse_refined_batch` at `8e4ce55`:
- missing index 2 → `[(1,'A'), (2,'C'), (3,'C')]` (positional fallback copies cue 3 into cue 2)
- `"speaker": null` → speaker `'None'`
- `<think>The array has [3] items…</think>[…]` → "Extra data" JSON error; JSON + `Note: … [names] …` → same
- per-cue payload 227 chars vs 124 with only `index/maxChars/speaker/text` (−45%)

External facts relied on: GPT-5 / o-series reject non-default `temperature` (HTTP 400 `unsupported_value`); Ollama's default context is 4k on < 24 GiB VRAM and its `/v1` endpoint ignores `num_ctx`; OpenAI `/audio/transcriptions` caps uploads at 25 MB.

---

<!-- Learnings from implementation will be appended below -->

## 2026-10-28 - Phase 1 Task 1.1: Shared LLM transport
- Provider errors must redact the configured API key even when the provider echoes it in its response body.
- Beads on this host does not support `bd ready --epic`; use `bd ready` and inspect the tracked issue IDs.
- Full gate on linux-aarch64: 1590 passed, 5 skipped, 6 Qwen/GGUF manifest failures matching the documented unsupported x64 host baseline.

## 2026-10-28 - Phase 1 Task 1.2: Settings validation and permissions
- Settings files are created exclusively with POSIX mode 0600 even under a permissive umask; existing files are tightened on load.
- Validate each new field independently on load and never include credential values in validation warnings.
- Full gate: 1591 passed, 5 skipped and the same 6 documented ARM64 baseline failures.
