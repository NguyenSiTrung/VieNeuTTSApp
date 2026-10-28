# Specification: OpenAI-Compatible LLM & Whisper API Provider Hardening (`llm_provider_hardening_20261028`)

## Overview

The Dubbing & Subtitles pipeline talks to any OpenAI-compatible provider through two stdlib-`urllib` clients: `core/llm_refine.py` (`/chat/completions` for `refine_asr` / `translate_dub` / `fit_timing`) and `core/asr.py::_run_openai_whisper_api` (`/audio/transcriptions`). Custom providers already work (Base URL + model + optional Bearer key), but a 2026-10-28 audit found correctness bugs that silently corrupt dubs, brittle error handling, and wasted throughput. Bugs 1–3 below were reproduced by running `_parse_refined_batch` against crafted replies:

1. **Positional fallback misaligns cues** — when the model drops/merges cue 2, cue 2 receives cue 3's text (`[(1,'A'), (2,'C'), (3,'C')]`), silently.
2. **`"speaker": null` becomes the literal speaker `"None"`**, breaking speaker→voice casting.
3. **Greedy `\[.*\]` extraction fails on reasoning models** (`<think>…[3]…</think>`) or trailing prose containing brackets → whole job fails.
4. **One failed batch discards every finished batch** — no retry, no partial result.
5. **GPT-5 / o-series reject `temperature: 0.2`** (HTTP 400 "Only the default (1) value is supported"), and the HTTP error body is never read, so users only see `HTTP Error 400: Bad Request`.
6. **Wasted tokens** — `startMs`, `endMs`, `durationMs`, `speechDurationMs`, `gapAfterMs` (~45% of per-cue characters) are sent but never referenced by any prompt. Ollama defaults to a 4k context on <24 GiB VRAM and the `/v1` API cannot raise it, so a 25-cue batch is close to the limit.
7. **No JSON mode, no concurrency, fixed 60 s timeout / batch of 25**, no cross-batch context for translation (names, terms, Vietnamese pronouns `anh/em/chị`), `fit_timing` sends cues that already fit and never verifies the result, `refine_asr` may invent new speaker labels, and translation prompts receive raw codes (`"natural spoken en"`).
8. **Cloud Whisper is misconfigured** — it reuses the LLM URL/key, sends local model names (`tiny`/`base`/…/`large-v3-turbo`) instead of e.g. `whisper-1`, auto-mode tries chat-only providers (DeepSeek/OpenRouter) as ASR servers, and the whole 16 kHz WAV (~1.9 MB/min) goes up in one request with a fixed 120 s timeout — OpenAI's 25 MB cap ≈ 13 minutes of audio.
9. **API keys sit in a world-readable `settings.json`** (default umask).

User decisions (2026-10-28): optional **per-task model overrides** on the same provider; **separate ASR API URL/key/model** with fallback to the LLM endpoint; **owner-only file permissions** for key storage (no new dependency); **per-project glossary + read-only neighbouring-cue context** for consistency.

---

## Functional Requirements

### FR-1: Shared OpenAI-compatible transport (`core/llm_client.py`, new)
- **FR-1.1 Module split:** Move `LlmConfig` and `LlmRefineError` into `core/llm_client.py`; `core/llm_refine.py` re-exports both so every existing import path keeps working. `LlmConfig` gains `max_retries: int = 2`, `concurrency: int = 0` (0 = auto), `batch_size: int = 25`; `timeout_s` default becomes `120.0`.
- **FR-1.2 Error taxonomy:** `LlmRefineError(message, *, status: int | None = None, kind: str = "error", transient: bool = False)`. Kinds: `auth` (401/403), `not_found` (404), `bad_request` (400/422), `rate_limited` (429), `server` (5xx, 408, 409), `network` (`URLError`, `TimeoutError`, `OSError`), `format` (non-OpenAI envelope / unparseable reply), `cancelled`. `rate_limited`, `server`, `network` are `transient=True`.
- **FR-1.3 Readable provider errors:** `http_error_detail(exc: HTTPError) -> str` reads ≤ 4 KB of the body and extracts `error.message` | `error` (str) | `message` | `detail`, else the raw text trimmed to 300 chars. Messages include status, URL and detail — never request headers or the API key.
- **FR-1.4 `post_chat_completion(config, messages, *, temperature: float | None = 0.2, json_mode: bool = False, cancel_check=None) -> ChatResult`** where `ChatResult(content: str, finish_reason: str)`:
  - `json_mode=True` sends `response_format: {"type": "json_object"}`.
  - Transient errors retry up to `config.max_retries` with exponential backoff (1 s, 2 s, 4 s), honouring a numeric `Retry-After` header capped at 30 s; sleeps go through a module-level `_sleep` seam in ≤ 0.25 s slices that re-check `cancel_check` (→ `kind="cancelled"`).
  - **Capability downgrade:** a 400/422 whose detail mentions `temperature` retries once without `temperature`; one mentioning `response_format` retries once without it. The downgrade is remembered per `(base_url, model)` in a lock-guarded module cache (`reset_capability_cache()` for tests) so later batches skip the failing parameter.
- **FR-1.5** `probe_llm_connection` (still exported from `llm_refine`) uses the new transport with `max_retries=0` and shows the provider's error detail on failure.

### FR-2: Correct reply parsing & prompt contract (`core/llm_refine.py`)
- **FR-2.1 Robust extraction:** strip `<think>…</think>` blocks and markdown fences, then scan candidate `{`/`[` offsets with `json.JSONDecoder().raw_decode`, accepting the first value that is either `{"cues": [ {...}, … ]}` or a bare list of objects. Trailing prose, brackets inside reasoning, and fenced JSON all parse.
- **FR-2.2 Strict index matching:** match reply items by `index` only. Positional matching is allowed **only** when no reply item carries an `index` and the item count equals the batch size. Unmatched cues are reported as missing (FR-3.3), never filled from a neighbour.
- **FR-2.3 Speaker safety:** `null`/missing/blank speaker keeps the original. For `refine_asr`, a changed speaker is accepted only if it is one of the labels already present in the input cue list (`allowedSpeakers`, also sent in the prompt); for `translate_dub` and `fit_timing` the original speaker is always kept. Empty `text` keeps the original text.
- **FR-2.4 Slim payload:** per-cue items carry only `index`, `speaker`, `text` (all tasks) plus `maxChars` (`translate_dub`, `fit_timing`). `maxChars` is still computed exactly as today (speech duration + bounded post-cue slack, language-aware). This deliberately reverses the payload clause of `dub_pacing_alignment_20261026` FR-4.2 (timing numbers are no longer sent; the budget they produced still is).
- **FR-2.5 Request envelope:** the user message is one JSON object: `{"cues": [...]}` plus, when non-empty, `"glossary"` (≤ 2000 chars, truncated with a note), `"allowedSpeakers"` (`refine_asr`), `"contextBefore"` (≤ 3 preceding source cues as `{speaker, text}`) and `"contextAfter"` (≤ 2 following source cues). Context comes from the **input** cue list, so batches stay independent and parallel-safe. System prompts say context is read-only and the reply must be `{"cues": [{"index", "text"[, "speaker"]}]}`; requests use `json_mode=True`.
- **FR-2.6 Language names:** `LANGUAGE_NAMES` maps `vi, en, zh, ja, ko, de, fr, ru, pt, es, it` to English names for prompts; empty/`auto` → Vietnamese (unchanged); unknown codes pass through verbatim.

### FR-3: Batch resilience, timing verification & throughput (`core/llm_refine.py`)
- **FR-3.1 Signature:** `refine_cues_with_llm(cues, config, *, task, target_language, rate_cap, batch_size: int | None = None, glossary: str = "", progress_cb=None, cancel_check=None, notes: list[str] | None = None) -> list[Cue]` (`batch_size=None` → `config.batch_size`). Return type is unchanged; user-facing summaries are appended to `notes` (the same out-param pattern as `asr.transcribe_media_to_cues`).
- **FR-3.2 Failure isolation:** `auth`, `not_found` and non-downgradable `bad_request` errors abort the whole job (every batch would fail). Any other batch failure, after transport retries, keeps that batch's original cues and continues; the summary note reads like "2/32 lô không xử lý được — giữ nguyên văn bản gốc." If **every** batch fails, the last error is raised.
- **FR-3.3 Truncation & missing cues:** `finish_reason == "length"` or an unparseable reply bisects the batch (down to 5 cues) and retries each half; at the floor it retries once, then fails the batch. Missing indices get **one** follow-up request containing only those cues (same context/glossary); cues still missing keep their original text and are counted in a note.
- **FR-3.4 `fit_timing` targeting:** only cues whose `len(text) > maxChars` are sent (a batch with none makes no request). A returned text still longer than `ceil(maxChars × 1.1)` gets one corrective follow-up that includes `currentChars`; the final text is the shortest non-empty candidate that is not longer than the original. Cues still over budget are counted in a note.
- **FR-3.5 Concurrency:** batches run on a `ThreadPoolExecutor` with `config.concurrency` workers, or auto = 1 when the Base URL host is loopback/private/`localhost`/`*.local` and 3 otherwise; capped at 8 and at the batch count. Results are assembled in input order; progress reports after each finished batch; cancellation is checked before every request and between retries, and pending futures are cancelled on cancel or a fatal error.

### FR-4: Cloud Whisper ASR (`core/asr.py`)
- **FR-4.1 Model & errors:** `AsrConfig` gains `api_model: str = ""`. `_run_openai_whisper_api` sends `api_model or "whisper-1"` and never the local Whisper model name; HTTP failures surface `http_error_detail` text. `_asr_cache_key` appends `api_base_url` and `api_model` only when `api_base_url` is non-empty, so existing local caches keep their keys.
- **FR-4.2 Auto-mode gating:** auto mode appends the `openai_api` runner only when `config.api_base_url` is non-empty — the controller (FR-6.2) passes it only for an explicitly configured ASR URL, so chat-only providers are never tried as ASR servers.
- **FR-4.3 Chunked upload:** audio longer than `API_CHUNK_SECONDS = 600` (~19.2 MB of 16 kHz PCM16, under the 25 MB cap) is split; each cut lands on the quietest 200 ms RMS frame within the 15 s before the boundary (read through `core/audio.read_wav_range`, never loading the whole file). Each chunk is written as a temporary PCM16 WAV and uploaded with `prompt` set to the last ≤ 200 characters of the previous chunk's text. Segment and word timestamps are offset by the chunk start and concatenated. Per-chunk timeout = `max(120, 0.5 × chunk_seconds)` s; one retry on transient errors per chunk; progress per chunk; cancellation between chunks raises `AsrCancelled`; temporary chunk files are always removed.

### FR-5: Settings model, persistence & key hardening (`core/models.py`, `core/settings.py`)
- **FR-5.1 New fields** (validated in `Settings.__post_init__`; an invalid individual value is dropped to its default by the per-field clamp in `settings.py`, never discarding the file):
  - `asr_api_base_url: str = ""`, `asr_api_key: str = ""`, `asr_api_model: str = ""`
  - `llm_model_refine: str = ""`, `llm_model_translate: str = ""`, `llm_model_fit: str = ""` (empty = use `llm_model`)
  - `llm_concurrency: int = 0` (`[0, 8]`, 0 = auto), `llm_timeout_s: int = 120` (`[15, 600]`), `llm_batch_size: int = 25` (`[5, 60]`)
- **FR-5.2 Owner-only file:** on POSIX `save_settings` creates the temp file with mode `0o600` before the atomic `os.replace`, and `load_settings` best-effort tightens an existing broader-permission file to `0o600` (`OSError` suppressed). No-op on Windows (per-user AppData ACLs).
- **FR-5.3 No key leakage:** clamp warnings and `_set_setting` error messages never include the value of `llm_api_key` / `asr_api_key`.

### FR-6: Controller wiring & project glossary (`core/subtitle_project.py`, `ui/controller.py`, `ui/subtitle_controller.py`)
- **FR-6.1 AppController properties** (`notify=aiSettingsChanged`, registered in the `_set_setting` signal map): `asrApiBaseUrl`, `asrApiKey`, `asrApiModel`, `llmModelRefine`, `llmModelTranslate`, `llmModelFit`, `llmConcurrency`, `llmTimeoutS`, `llmBatchSize`.
- **FR-6.2 Config resolution in `SubtitleController`:**
  - `_llm_config(task)` uses that task's model override, otherwise `llmModel`, plus the timeout, concurrency and batch-size settings.
  - `_asr_config()`: `api_base_url = asrApiBaseUrl`, or `llmBaseUrl` only when `asrBackend == "openai_api"` and the ASR URL is empty; `api_key = asrApiKey` when an ASR URL is set, otherwise `llmApiKey` only when falling back to the LLM URL. **The LLM key is never sent to a different ASR host.** `api_model = asrApiModel`.
- **FR-6.3 Project glossary:** `SubtitleProject.llm_glossary: str = ""` is persisted as `"llmGlossary"` only when non-empty, capped at 4000 chars, and **excluded from `render_fingerprint`**. `SubtitleController` exposes `llmGlossary` (NOTIFY) and `setLlmGlossary(text)`, which saves the project **without** staging a render invalidation (it does not change audio).
- **FR-6.4 Notes:** `_refine_with_llm_now` passes the glossary and a `notes` list; on completion it applies the returned cues (partial results included) and shows joined notes through the existing hint channel.

### FR-7: UI & docs (`SettingsModels.qml`, `SubtitlePreparePanel.qml`, i18n, `README.md`)
- **FR-7.1 Settings › AI dubbing card:** a "Whisper API" group (`asrApiBaseUrlField`, `asrApiModelField` with placeholder `whisper-1`, password `asrApiKeyField`; description: empty URL reuses the LLM server only when the backend is set to Whisper API) and a collapsed-by-default "LLM advanced" disclosure (`llmAdvancedToggle`) holding `llmModelRefineField`, `llmModelTranslateField`, `llmModelFitField` (placeholder = main model), and `AppNumberField`s `llmConcurrencyField` (0–8), `llmTimeoutField` (15–600), `llmBatchSizeField` (5–60). New text inputs use `AppTextField`; existing `llm*Field` objectNames are preserved.
- **FR-7.2 Prepare stage glossary:** a collapsible "Thuật ngữ & cách xưng hô" section in `SubtitlePreparePanel.qml` with a multi-line `subtitleGlossaryField` (example placeholder covering a proper-name mapping and a speaker-pair pronoun rule) that commits through `subtitleController.setLlmGlossary` when editing finishes or focus leaves.
- **FR-7.3** `scripts/update_i18n.sh` leaves 0 unfinished entries.
- **FR-7.4** `README.md` gains an "AI dubbing providers (LLM & Whisper API)" section: example Base URLs (Ollama, LM Studio, llama.cpp server, vLLM, OpenAI, OpenRouter, DeepSeek, Groq, Gemini's OpenAI-compatible endpoint), per-task models, JSON-mode/temperature auto-fallback, the Ollama context tip (`OLLAMA_CONTEXT_LENGTH` ≥ 8192), the separate Whisper API fields, and the limitation that classic Azure OpenAI deployments (`api-key` header) are unsupported.

---

## Non-Functional Requirements

- **Zero new dependencies:** stdlib only (`urllib`, `json`, `concurrent.futures`, `ipaddress`, `threading`) plus existing `numpy`/`soundfile`; `pyproject.toml` / `uv.lock` untouched.
- **Backward compatibility:** existing `settings.json` and `project.json` load unchanged; `render_fingerprint` digests are byte-identical; `LlmConfig`, `LlmRefineError`, `refine_cues_with_llm`, `probe_llm_connection`, `max_chars_for_window`, `LANGUAGE_CHARS_PER_SEC` stay importable from `core/llm_refine.py`.
- **Secrets:** API keys never appear in logs, error messages, notes, or test output.
- **Deterministic tests:** network behaviour is tested against local `http.server` mocks or the `_post_chat_completion` / `_sleep` seams — no real provider calls and no real-time backoff waits.
- **GUI responsiveness:** all network work stays on the existing `_run_bg` path; the LLM pool lives inside that background call.

---

## Acceptance Criteria

1. A reply that omits cue 2 leaves cue 2's original text and never copies cue 3's; a `"speaker": null` reply keeps the original speaker; a `refine_asr` reply inventing `"SPEAKER_99"` keeps the original label.
2. Replies wrapped in `<think>…[3]…</think>`, followed by bracketed prose, fenced in markdown, or shaped `{"cues": [...]}` all parse.
3. With batch 2 of 3 failing permanently on 500s, the job returns batches 1 and 3 refined, batch 2 original, and a summary note; a 401 aborts immediately with the provider's error message.
4. A provider rejecting `temperature` (or `response_format`) with HTTP 400 succeeds on the automatic retry, and later batches omit the parameter without another 400.
5. A 429 with `Retry-After: 2` is retried after the advised delay (verified through the `_sleep` seam); cancellation during backoff raises `kind="cancelled"`.
6. Prompt cue items contain only `index`, `speaker`, `text` (+ `maxChars` for translate/fit); `maxChars` values equal today's budget; translation prompts name the language (`"English"`, not `"en"`).
7. `fit_timing` sends no request for cues already within budget and issues one corrective follow-up for a cue still over `ceil(maxChars × 1.1)`.
8. With concurrency 3 against a mock server, 6 batches overlap in flight and results come back in input order.
9. Cloud ASR sends `whisper-1` (or the configured ASR model) instead of `base`; auto mode never calls `/audio/transcriptions` unless an ASR URL is configured; a 25-minute WAV is uploaded as 3 chunks with offset timestamps and no chunk above 25 MB.
10. The LLM API key is never sent to a separately configured ASR host; `settings.json` is mode `0600` after save on POSIX; per-task model overrides reach the right task.
11. The glossary persists in `project.json`, reaches every LLM request as `"glossary"`, leaves `render_fingerprint` unchanged, and editing it does not stage a render invalidation.
12. `ruff check .`, `ruff format --check .`, `scripts/update_i18n.sh` (0 unfinished) and `pytest` all pass.

---

## Out of Scope

- Classic Azure OpenAI deployment URLs / `api-key` header, arbitrary custom headers, Anthropic-native or Responses API endpoints.
- OS keychain storage (`keyring`) — rejected to avoid a new dependency.
- Separate provider URL/key per task (only per-task model names).
- Streaming completions, LLM response caching, and translated-output (sequential) context — context stays source-only to keep batches parallel.
- Changing TTS, alignment, or diarization behaviour.
