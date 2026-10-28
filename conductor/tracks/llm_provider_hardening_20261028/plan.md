# Implementation Plan: OpenAI-Compatible LLM & Whisper API Provider Hardening (`llm_provider_hardening_20261028`)

## Overview

4-phase TDD implementation across a new `core/llm_client.py`, `core/llm_refine.py`, `core/asr.py`, `core/models.py`, `core/settings.py`, `core/subtitle_project.py`, `ui/controller.py`, `ui/subtitle_controller.py`, `SettingsModels.qml`, `SubtitlePreparePanel.qml`, the i18n catalog and `README.md`. Verification is automated only (`ruff check .`, `ruff format --check .`, `pytest`, plus `scripts/update_i18n.sh` for string changes), with no manual verification gates. Commit after each task with a `git notes add` summary.

Network tests use local `http.server` mocks (pattern: `tests/unit/test_llm_refine.py::test_llm_refine_modes_and_connection_probe`) or the `_post_chat_completion` / `_sleep` seams — never real providers or real backoff sleeps.

---

## Phase 1: Transport & Settings Foundation
<!-- execution: parallel -->
<!-- depends: -->

- [x] **Task 1.1: `core/llm_client.py` — OpenAI-compatible transport with error taxonomy, retries and capability downgrade**
  <!-- files: src/vienetts_app/core/llm_client.py, src/vienetts_app/core/llm_refine.py, tests/unit/test_llm_client.py, tests/unit/test_llm_refine.py -->
  - Red tests (`tests/unit/test_llm_client.py`, mock `HTTPServer`):
    - `LlmRefineError` kind/status/transient for 401, 403, 404, 400, 422, 429, 500, 503, connection refused, timeout; message contains the provider's `error.message` from a JSON body and a trimmed raw body for non-JSON; the API key never appears in any message.
    - 503 → 503 → 200 succeeds with `max_retries=2` and `_sleep` called with 1 s then 2 s; `Retry-After: 2` is honoured, and 120 is capped to 30; `cancel_check` turning true during backoff raises `kind="cancelled"`.
    - A 400 mentioning `temperature` triggers one retry without `temperature`; the next call to the same `(base_url, model)` omits it up front, while a different model still sends it. The same holds for `response_format` when `json_mode=True`. `reset_capability_cache()` clears the cache.
    - `ChatResult.finish_reason` reflects `choices[0].finish_reason` (empty when absent); a non-OpenAI envelope raises `kind="format"`.
  - Green implementation:
    - Move `LlmConfig` (+ `max_retries=2`, `concurrency=0`, `batch_size=25`, `timeout_s=120.0`) and `LlmRefineError` into `llm_client.py`; add `ChatResult`, `http_error_detail`, `post_chat_completion`, the lock-guarded capability cache and the `_sleep` seam.
    - `llm_refine.py` re-exports `LlmConfig` / `LlmRefineError`; `_post_chat_completion` becomes a thin seam over `post_chat_completion` returning `ChatResult`; `probe_llm_connection` uses `max_retries=0`. Update existing `test_llm_refine.py` fakes to the new seam signature `(config, messages, *, temperature=0.2, json_mode=False, cancel_check=None) -> ChatResult` without changing their assertions.

- [x] **Task 1.2: Settings fields, per-field clamping and owner-only `settings.json`**
  <!-- files: src/vienetts_app/core/models.py, src/vienetts_app/core/settings.py, tests/unit/test_settings.py -->
  - Red tests:
    - `Settings(...)` rejects out-of-range or wrong-type values for `llm_concurrency` `[0, 8]`, `llm_timeout_s` `[15, 600]`, `llm_batch_size` `[5, 60]` (bool-as-int included) and non-string `asr_api_*` / `llm_model_*` values. Fold these into the existing settings validation test rather than adding default/round-trip micro-tests (zc8p low-value criteria).
    - `load_settings` with one invalid new field (e.g. `llm_batch_size: 999`, `asr_api_key: 5`) keeps every other field and drops only that one to its default; log output never contains a key value.
    - POSIX only (`skipif os.name == "nt"`): after `save_settings` the file mode is `0o600`; a pre-existing `0o644` file is tightened to `0o600` by `load_settings`.
  - Green implementation:
    - Add the fields plus `__post_init__` validation in `core/models.py`, and per-field clamps in `core/settings.py` that never log string values.
    - In `save_settings`, create the temp file via `os.open(..., 0o600)` on POSIX; in `load_settings`, best-effort `os.chmod(path, 0o600)` with `OSError` suppressed.

---

## Phase 2: Refinement Engine Correctness & Throughput
<!-- execution: sequential -->
<!-- depends: phase1 -->

- [x] **Task 2.1: Robust reply parsing, speaker safety, slim payload and the JSON request envelope**
  <!-- files: src/vienetts_app/core/llm_refine.py, tests/unit/test_llm_refine.py -->
  - Red tests:
    - The parser accepts `{"cues": [...]}`, a bare array, markdown-fenced JSON, `<think>The array has [3] items</think>` + JSON, and JSON followed by `Note: I kept [names] unchanged.`
    - A reply missing index 2 leaves cue 2 untouched and reports index 2 as missing (no `(2, 'C')` copy). An index-less reply of equal length maps positionally; an index-less reply of a different length matches nothing.
    - `"speaker": null` / missing / `""` keep the original speaker. In `refine_asr`, `"SPEAKER_99"` (not among the input labels) is rejected while a swap to an existing label is accepted. In `translate_dub` / `fit_timing`, speaker changes are ignored. Empty `text` keeps the original.
    - Captured request: cue items have exactly `{index, speaker, text}` (`refine_asr`) or `{index, speaker, text, maxChars}` (translate/fit); `maxChars` equals the current speech-duration + slack budget. Rewrite `test_refine_cues_with_llm_uses_speech_duration_and_post_cue_slack` to assert that the timing keys are absent and the budget values are unchanged.
    - The envelope carries `allowedSpeakers` (refine only), `contextBefore` (≤ 3) / `contextAfter` (≤ 2) taken from source cues around the batch, and `glossary` when non-empty (> 2000 chars truncated + note). Empty keys are omitted. `json_mode=True` is passed to the seam.
    - The `translate_dub` system prompt names `"English"` for `en` and `"Japanese"` for `ja`, falls back to `"Vietnamese"` for `""` / `auto`, and passes unknown codes through.
  - Green implementation:
    - Add `_strip_reasoning`, a `raw_decode` candidate scanner, and `_parse_reply(raw, batch, *, task, allowed_speakers) -> tuple[list[Cue], list[int]]` to replace `_parse_refined_batch`.
    - Add `LANGUAGE_NAMES`, the new system prompts (context is read-only; reply shape `{"cues": [...]}`), and `_build_request(batch, all_cues, start, *, task, ...)`.

- [x] **Task 2.2: Batch failure isolation, truncation bisection, missing-cue follow-up, `fit_timing` verification and concurrency**
  <!-- files: src/vienetts_app/core/llm_refine.py, tests/unit/test_llm_refine.py -->
  - Red tests (via the `_post_chat_completion` seam unless noted):
    - 3 batches where batch 2 always raises a transient `server` error: the result has batches 1 and 3 refined and batch 2 original, and `notes` gets a "1/3 lô" summary. If every batch fails, the last error is raised. An `auth` error on batch 1 raises immediately and no further batches are requested.
    - `finish_reason="length"` on a 10-cue batch triggers two 5-cue requests; a 5-cue batch that is still unparseable is retried once, then fails.
    - A reply missing 2 indices triggers exactly one follow-up containing only those 2 cues; cues still missing keep their original text and a note counts them.
    - `fit_timing`: a batch whose cues are all within `maxChars` makes no request; an over-budget reply (> `ceil(maxChars × 1.1)`) triggers one corrective request including `currentChars`; the shortest candidate not longer than the original wins; cues still over budget are counted in a note.
    - Concurrency (mock `HTTPServer` with a barrier/latency): `concurrency=3` with 6 batches has ≥ 2 requests in flight at once, and results arrive in input order. Auto concurrency is 1 for `http://localhost:11434/v1`, `http://192.168.1.5:8000/v1` and `http://box.local/v1`, and 3 for `https://api.openai.com/v1`. Cancelling mid-run raises `kind="cancelled"` and starts no new requests.
    - `batch_size=None` uses `config.batch_size`.
  - Green implementation:
    - Restructure `refine_cues_with_llm` around `_run_batch` (bisect / follow-up / fit verification), ordered `ThreadPoolExecutor` assembly with `cancel_futures=True` on cancel or fatal errors, `_auto_concurrency(base_url)` via `urllib.parse` + `ipaddress`, the `notes` out-param and the `glossary` kwarg.

---

## Phase 3: Cloud Whisper ASR
<!-- execution: sequential -->
<!-- depends: phase1 -->

- [ ] **Task 3.1: ASR API model, readable errors, cache key and auto-mode gating**
  <!-- files: src/vienetts_app/core/asr.py, tests/unit/test_asr.py -->
  - Red tests:
    - `backend="openai_api"` with `model="base"`, `api_model=""` sends the form field `model=whisper-1`; `api_model="whisper-large-v3-turbo"` is sent verbatim.
    - An HTTP 401 with a JSON error body raises `AsrError` containing the provider message and never the key.
    - Auto mode with `api_base_url=""` never hits `/audio/transcriptions` even when both local runners are missing; with a non-empty URL it is tried last (existing ordering).
    - `_asr_cache_key` is unchanged when `api_base_url` is empty and includes URL + `api_model` when set.
  - Green implementation:
    - Add `AsrConfig.api_model`; use `http_error_detail` from `core/llm_client.py`; gate the auto-mode runner on a non-empty `api_base_url`; append sparse cache-key fields.

- [ ] **Task 3.2: Silence-aware chunked upload for long audio**
  <!-- files: src/vienetts_app/core/asr.py, tests/unit/test_asr.py -->
  <!-- depends: task1 -->
  - Red tests (synthetic 16 kHz WAVs written with soundfile; mock `HTTPServer` echoing `verbose_json` per chunk):
    - A 25-minute WAV (use a small patched `API_CHUNK_SECONDS` to keep the test fast, asserting the same ratios) makes 3 requests, each body < 25 MB at the real constant. Each cut lands on an injected silent region inside the 15 s window before the boundary. Returned segment and word timestamps are offset by each chunk's start.
    - The second and later requests carry `prompt` equal to the previous chunk's trailing ≤ 200 characters.
    - A transient 503 on chunk 2 is retried once and succeeds; cancelling after chunk 1 raises `AsrCancelled`; no temporary chunk files remain on success, failure, or cancel.
    - Audio ≤ `API_CHUNK_SECONDS` keeps the single-request path (existing tests unchanged).
  - Green implementation:
    - Add `API_CHUNK_SECONDS = 600` and `_plan_api_chunks(wav_path) -> list[tuple[int, int]]` (quietest 200 ms RMS frame via `read_wav_range`).
    - Add a per-chunk temp PCM16 write, an upload loop with per-chunk timeout `max(120, 0.5 × chunk_s)`, timestamp offsetting, and `finally` cleanup.

---

## Phase 4: Controller Wiring, Project Glossary, UI & Docs
<!-- execution: parallel -->
<!-- depends: phase2, phase3 -->

- [ ] **Task 4.1: AppController properties, per-task/ASR config resolution, project glossary and refinement notes**
  <!-- files: src/vienetts_app/core/subtitle_project.py, src/vienetts_app/ui/controller.py, src/vienetts_app/ui/subtitle_controller.py, tests/unit/test_subtitle_project.py, tests/unit/test_subtitle_controller.py, tests/unit/test_controller.py -->
  - Red tests:
    - `SubtitleProject.llm_glossary` round-trips as `"llmGlossary"` only when non-empty and is capped at 4000 chars; `render_fingerprint` digests are unchanged with and without a glossary (golden pin).
    - The AppController properties `asrApiBaseUrl`, `asrApiKey`, `asrApiModel`, `llmModelRefine`, `llmModelTranslate`, `llmModelFit`, `llmConcurrency`, `llmTimeoutS`, `llmBatchSize` persist and emit `aiSettingsChanged`.
    - `_llm_config("translate_dub")` uses `llmModelTranslate` when set and `llmModel` otherwise (same for refine/fit), and carries the timeout/concurrency/batch settings.
    - `_asr_config()` resolves as follows:
      - ASR URL set + ASR key empty → empty key (the LLM key is never sent to the ASR host).
      - ASR URL empty + backend `openai_api` → LLM URL + LLM key.
      - ASR URL empty + backend `auto` → empty URL.
    - `setLlmGlossary` saves the project, emits `llmGlossaryChanged`, and does not stage a `_PendingInvalidation` on a rendered project. `refineWithLlm` passes the glossary to `refine_cues_with_llm`, applies partial results, and surfaces the notes through the hint.
  - Green implementation:
    - Add the project field and sparse serialization, the controller properties plus `_set_setting` signal-map entries, `_llm_config(task)` / `_asr_config()` resolution, the glossary property/slot, and notes plumbing in `_refine_with_llm_now`.

- [ ] **Task 4.2: Settings "Whisper API" + "LLM advanced" UI, Prepare-stage glossary editor, smoke coverage and i18n**
  <!-- files: src/vienetts_app/ui/qml/settings/SettingsModels.qml, src/vienetts_app/ui/qml/components/SubtitlePreparePanel.qml, tests/smoke/test_ui_tabs.py, tests/unit/test_subtitle_components.py, src/vienetts_app/ui/i18n/vienetts_en.ts, src/vienetts_app/ui/i18n/vienetts_en.qm -->
  <!-- depends: task1 -->
  - Red tests:
    - In an existing consolidated settings smoke scenario group (no new QML instantiation), `asrApiBaseUrlField`, `asrApiModelField`, `asrApiKeyField` (password echo) and `llmAdvancedToggle` exist. Expanding the toggle reveals `llmModelRefineField`, `llmModelTranslateField`, `llmModelFitField`, `llmConcurrencyField`, `llmTimeoutField` and `llmBatchSizeField`, and editing them writes through to the fake controller. The existing `llmBaseUrlField` / `llmModelField` / `llmApiKeyField` / `llmTestConnectionButton` objectNames are still present.
    - In the subtitles smoke scenario, `subtitleGlossaryField` shows `subtitleController.llmGlossary` and committing an edit calls `setLlmGlossary`. The fake controllers gain the new NOTIFY `@Property` members (plain attributes read as undefined in QML).
  - Green implementation:
    - Add the QML per FR-7.1 / FR-7.2 using `AppTextField` / `AppNumberField` and a disclosure modelled on the Settings CUDA "Details & diagnostics" pattern.
    - Run `scripts/update_i18n.sh` to 0 unfinished entries, translating the new strings in `vienetts_en.ts`.

- [ ] **Task 4.3: README "AI dubbing providers (LLM & Whisper API)" section**
  <!-- files: README.md -->
  <!-- depends: task1 -->
  - Add the FR-7.4 section after `## Models`: a provider/Base-URL table, per-task models, JSON-mode/temperature auto-fallback, the Ollama `OLLAMA_CONTEXT_LENGTH` tip, the Whisper API fields with the "LLM key is never sent to a different ASR host" rule and 10-minute chunking, owner-only `settings.json`, and the Azure-classic limitation. Docs-only — still run the full gate before committing.
