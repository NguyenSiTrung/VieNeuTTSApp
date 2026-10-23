# Specification: Harden and Optimize On-Device ASR & Video Dubbing Subtitles (`asr_hardening_20261021`)

## Overview

The 2026-10-21 audit of the on-device Whisper ASR & Video Dubbing/Subtitles pipeline (`core/ai_models.py`, `core/asr.py`, `core/subtitles.py`, `ui/controller.py`, `ui/subtitle_controller.py`, `SettingsModels.qml`) identified 10 correctness, security/integrity, performance, and UX issues that remained after the companion diarization hardening (`VieNeuTTSApp-2ifr`):

1. **Missing `whisper.cpp` runtime installer & false offline-ready status**: `download_asr_model()` downloads `ggml-*.bin` weights into `<data_dir>/models/whisper/`, and `SettingsModels.qml` immediately claims `"Đã cài đặt — sẵn sàng chạy ngoại tuyến."`, but no `whisper-cli` runtime installer exists (`<data_dir>/runtimes/whisper` is never populated) and `faster-whisper` cannot load `ggml-*.bin` weights.
2. **Unverified ASR model downloads & unguarded file deletion**: `WHISPER_GGUF_MODELS` lacks `size_bytes` and `sha256` digests; `resolve_asr_model_path()` accepts any non-empty file (`cand.is_file()` / `size > 0`), and `remove_asr_model()` unlinks any resolved path without checking if it resides inside `<data_dir>/models/whisper`. Unknown `asr_model` IDs silently download `ggml-base.bin` while continuing to report uninstalled.
3. **Cancellation swallowed by `auto` fallback & masked errors**: `_run_faster_whisper` and `_run_whisper_cpp` raise `AsrError` on user cancellation, which the `backend="auto"` loop catches and swallows before launching the next fallback backend. Furthermore, `SubtitleController._asr_config()` passes `llmBaseUrl` (defaulting to Ollama `http://localhost:11434/v1`) as `api_base_url`, so `auto` always attempts a failing HTTP request to Ollama and overwrites the real local backend error with `"Không tìm thấy bộ nhận diện giọng nói (ASR) khả dụng"`.
4. **Whisper non-speech tokens spoken aloud or parsed as fake speakers**: `[BLANK_AUDIO]`, `[MUSIC]`, `[Applause]`, `(music)`, etc. emitted by Whisper are either synthesized aloud by TTS or matched by `_SPEAKER_PREFIX_RE` in `core/subtitles.py` as speaker labels (`SPEAKER = "MUSIC"`).
5. **Managed `.srt` filename collision across videos**: `transcribeVideo` and `importVideo` store and auto-attach `<data_dir>/subtitles/<stem>.srt` using only `vpath.stem`, causing different videos with the same filename (e.g. `video.mp4`, `lecture.mp4`) to overwrite or silently adopt each other's transcripts.
6. **`max_duration_ms` ignored in `_split_long_segment` & one-way micro-fragment merge**: `_split_long_segment` re-packs clauses up to `max_chars` without bounding chunk duration, so a 15-second cue with `<= 84` characters is never split. Trailing micro-fragments (`< 400 ms`) are only merged forward, never into the preceding segment.
7. **Unnormalized `.wav` input & missing source fingerprint sidecar**: `transcribe_media_to_cues` passes `.wav` inputs directly to `whisper-cli` without verifying 16 kHz mono PCM, and reuses `source_16k.wav` in `work_dir` without checking source `(size, mtime_ns)`.
8. **`_run_whisper_cpp` & `_run_openai_whisper_api` reliability bugs**: `_run_whisper_cpp` has no process timeout, omits `encoding="utf-8", errors="replace"` on `Popen` (crashing on Windows `cp1252`), and freezes progress at `0.2`. `_run_openai_whisper_api` runs `parse_cues()` on JSON responses if the transcript text contains `"-->"`, and hardcodes `0..5000 ms` when a server returns top-level `{"text": "..."}`.
9. **Inference & caching performance bottlenecks**: `_run_whisper_cpp` defaults to 4 threads and omits `-sns`; `_run_faster_whisper` reloads `WhisperModel` from disk on every call and leaves `condition_on_previous_text=True`; `SubtitleController.transcribeVideo` uses an ephemeral `TemporaryDirectory` that discards `source_16k.wav`, ASR output, and `diar_turns_cache.json` after every run.
10. **Missing QML controls & download progress signal storm**: `asrLanguage` has no UI control in `SettingsModels.qml` (permanently `"auto"` unless hand-edited), `asrDownloadProgress` has no `ProgressBar` in QML, and `_on_progress` emits `aiModelsInstallChanged` on every 256 KB chunk (~2,188 times for a 547 MB model), triggering thousands of synchronous `stat()`/`rglob()` checks on the GUI thread.

---

## Functional Requirements

### FR-1: Hardened Whisper Model Catalog, Guarded Removal & `whisper.cpp` Runtime Installer (`core/ai_models.py`)
- **FR-1.1:** Populate exact `size_bytes` and `sha256` digests in `WHISPER_GGUF_MODELS` for all 5 models (`tiny`, `base`, `small`, `medium`, `large-v3-turbo`) from `ggerganov/whisper.cpp`.
- **FR-1.2:** `download_asr_model()` must reject unknown catalog IDs with `AiModelInstallError` and pass `expected_size`, `expected_sha256`, and `min_free_bytes` (`size_bytes + INSTALL_SPACE_HEADROOM`) to `_stream_download()` when `url_override` is empty.
- **FR-1.3:** `resolve_asr_model_path()` must honour explicit file paths only when they end in `.bin` or `.gguf`, and validate managed catalog files with `_managed_model_size_ok(p, spec)`.
- **FR-1.4:** `remove_asr_model()` must return `bool` and refuse to delete any file outside `asr_models_dir(data_dir).resolve()` or without a `.bin`/`.gguf` suffix.
- **FR-1.5:** Add managed `whisper.cpp` runtime support (`WHISPER_CPP_RELEASE_TAG = "b5454"`):
  - `available_whisper_cpp_variants()`, `resolve_whisper_cpp_variant()`, `resolve_whisper_cpp_cli(data_dir)`, `is_whisper_cpp_installed(data_dir)`, `installed_whisper_cpp_variant(data_dir)`, `download_whisper_cpp_runtime(data_dir, ...)`, and `remove_whisper_cpp_runtime(data_dir)`.
  - Extract into a temporary staging directory, set executable bits on POSIX, probe `<cli> --help` with `cwd=cli.parent`, write `runtime.json`, and atomically swap into `<data_dir>/runtimes/whisper` while preserving the previous install on failure.

### FR-2: Reliable & High-Performance On-Device ASR Core (`core/asr.py`, `core/subtitles.py`)
- **FR-2.1:** Share 16 kHz mono WAV normalization (`_ensure_16k_wav`) with `.json` source fingerprint sidecar (`source`, `size`, `mtime_ns`) so 48 kHz/stereo `.wav` inputs are downmixed to 16 kHz mono and stale cached WAVs are invalidated when the media file changes.
- **FR-2.2:** Cache raw ASR segments in `work_dir / "asr_segments_cache.json"` keyed by `(wav_path, size, mtime_ns, backend, model, language)` so repeated runs or diarization toggles reuse the transcription.
- **FR-2.3:** Introduce `AsrCancelled(AsrError)` and raise it whenever `cancel_check()` is true; `backend="auto"` must re-raise `AsrCancelled` immediately without invoking fallback backends.
- **FR-2.4:** Add `notes: list[str] | None = None` to `transcribe_media_to_cues`. In `auto` mode, prefer `whisper_cpp` first when both the `whisper-cli` binary and the selected `ggml-*.bin` model are installed locally; record suppressed fallback errors in `notes`; and only attempt `openai_api` in `auto` mode when an explicit ASR API URL is provided (not the default Ollama LLM URL).
- **FR-2.5:** Harden `_run_whisper_cpp`:
  - Spawn with `cwd=binary.parent`, `encoding="utf-8", errors="replace"`, `-t <threads>` (resolved from `AsrConfig.threads` or `min(16, os.cpu_count() or 4)`), `-sns` (suppress non-speech tokens), `-pp` (print progress), and `-ng` when `config.device == "cpu"`.
  - Enforce a duration-based wall-clock timeout (`max(180.0, duration_s * 6.0 + 60.0)` or `config.timeout_s`).
  - Parse progress from `stderr` to report smooth `0.2..0.95` progress updates, and treat exit `0` with an empty/missing `.srt` as empty segments (no speech) rather than a crash.
- **FR-2.6:** Harden `_run_faster_whisper` and `_run_openai_whisper_api`:
  - Cache the loaded `faster_whisper.WhisperModel` instance (bounded 1-entry cache), pass `cpu_threads`, `condition_on_previous_text=False`, and select `compute_type` from device (`int8` on CPU, `int8_float16` on CUDA).
  - In `_run_openai_whisper_api`, check JSON structure (`raw_resp.lstrip().startswith("{")`) before checking `"-->"` for SRT, wrap `SubtitleError` in `AsrError`, use `payload.get("duration")` or WAV duration instead of hardcoded `5000 ms`, and honour `cancel_check`.
- **FR-2.7:** Filter Whisper non-speech hallucinations (`[BLANK_AUDIO]`, `[MUSIC]`, `[Applause]`, `[Laughter]`, `(music)`, `♪...♪`, etc.) in `normalize_asr_segments_to_cues`, and update `_SPEAKER_PREFIX_RE` / speaker extraction in `core/subtitles.py` so non-speech bracket markers are never parsed as speaker IDs.
- **FR-2.8:** Fix `_split_long_segment` so segments with `duration > max_duration_ms` (even when `len(text) <= max_chars`) split across clauses/words using a duration-proportional character target, and update `normalize_asr_segments_to_cues` to merge trailing micro-fragments (`< 400 ms`, gap `<= 150 ms`, same speaker) into the preceding segment when no following segment can absorb them.

### FR-3: Settings, Controller Plumbing, Workspace Collision Guard & QML UI (`core/models.py`, `core/settings.py`, `ui/controller.py`, `ui/subtitle_controller.py`, `SettingsModels.qml`)
- **FR-3.1:** Add validated `asr_device` (`"auto" | "cpu" | "cuda" | "metal"`) and `asr_threads` (`0..64`) fields to `Settings` (`core/models.py`, `core/settings.py`), and expose `asrDevice`, `asrThreads`, `whisperCppInstalled`, `whisperCppRuntimeVariant`, `whisperCppAvailableVariants`, `downloadWhisperCppRuntime()`, and `removeWhisperCppRuntime()` on `AppController`.
- **FR-3.2:** Separate download progress notifications (`aiDownloadProgressChanged` throttled to $\ge 1\%$ increments) from `aiModelsInstallChanged` so 256 KB download chunks do not trigger GUI-thread filesystem checks on every chunk.
- **FR-3.3:** Use a persistent per-video cache directory (`<data_dir>/subtitles/.media_cache/<hash>`) in `SubtitleController.transcribeVideo` and `runDiarization` so extracted 16 kHz WAVs, ASR caches, and diarization turn caches survive across operations.
- **FR-3.4:** Disambiguate managed `.srt` paths in `<data_dir>/subtitles/` by including an 8-hex digest of the resolved video path (`<stem>_<hash8>.srt`) while keeping `project.title` set to the clean video stem.
- **FR-3.5:** Update `SettingsModels.qml` with `asrLanguageCombo`, `asrDeviceCombo`, `asrThreadsField`, `downloadWhisperCppRuntimeButton`, `removeWhisperCppRuntimeButton`, `asrDownloadProgressBar`, `asrDownloadPercentLabel`, and truthful install status copy; update `vienetts_en.ts`/`.qm` via `scripts/update_i18n.sh`.

---

## Non-Functional Requirements

- **Zero new mandatory PyPI dependencies** in `pyproject.toml` / `uv.lock`.
- **No GUI-thread blocking**: all downloads, runtime probes, audio extractions, and ASR executions run off-thread; progress signals are throttled.
- **100% i18n coverage**: `scripts/update_i18n.sh` reports 0 unfinished strings in `vienetts_en.ts`.

---

## Acceptance Criteria

1. Truncated or SHA-256-mismatched Whisper model downloads are rejected and `.part` files cleaned up; legacy truncated `.bin` files read as `is_asr_model_installed == False`.
2. `remove_asr_model` refuses paths outside `<data_dir>/models/whisper` and non-`.bin`/`.gguf` files; unknown catalog IDs raise `AiModelInstallError`.
3. `download_whisper_cpp_runtime` stages, probes `--help` with `cwd=cli.parent`, and atomically promotes `<data_dir>/runtimes/whisper`, preserving an existing working runtime if a new archive fails validation.
4. Cancelling an in-flight transcription raises `AsrCancelled` and stops immediately in `auto` mode without invoking fallback backends.
5. 48 kHz / stereo `.wav` inputs are normalized to 16 kHz mono before ASR; repeated ASR runs on unchanged media reuse `asr_segments_cache.json` and `source_16k.wav`.
6. `[BLANK_AUDIO]`, `[MUSIC]`, and similar non-speech tags are stripped rather than spoken aloud or parsed as `Cue.speaker`; overlong-duration cues (`> max_duration_ms` with `<= max_chars` text) are split at clause/word boundaries; trailing micro-fragments merge backward.
7. Two videos with the same filename in different folders receive distinct managed `.srt` files and never overwrite or adopt each other's subtitles.
8. `SettingsModels.qml` exposes `asrLanguageCombo`, `asrDeviceCombo`, `asrThreadsField`, `downloadWhisperCppRuntimeButton`, `removeWhisperCppRuntimeButton`, `asrDownloadProgressBar`, and `asrDownloadPercentLabel`.
9. `ruff check .`, `ruff format --check .`, `scripts/update_i18n.sh`, and `pytest` all pass.

---

## Out of Scope

- Bundling Whisper weights or `whisper-cli` binaries inside the PyInstaller installer (weights and runtimes remain on-demand managed installs in `data_dir`).
- Modifying TTS synthesis engines (`vieneu`, `qwen_host`, `qwen_gguf_host`).
