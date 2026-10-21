# Implementation Plan: Harden and Optimize On-Device ASR & Video Dubbing Subtitles (`asr_hardening_20261021`)

## Overview

3-phase TDD remediation of the 2026-10-21 On-Device ASR & Video Dubbing audit across `core/ai_models.py`, `core/asr.py`, `core/subtitles.py`, `core/models.py`, `core/settings.py`, `ui/controller.py`, `ui/subtitle_controller.py`, and `ui/qml/settings/SettingsModels.qml`.

---

## Phase 1: Hardened Whisper Model Catalog, Guarded Removal & `whisper.cpp` Runtime Installer

- [ ] **Task 1.1: Pin `WHISPER_GGUF_MODELS` size/SHA-256, verify downloads, and guard `resolve_asr_model_path` / `remove_asr_model`**
  - Files: `src/vienetts_app/core/ai_models.py`, `tests/unit/test_ai_models.py`
  - Red tests:
    - Truncated managed `ggml-base.bin` in `models/whisper` reports `is_asr_model_installed("base", tmp_path) is False` and `resolve_asr_model_path("base", tmp_path) is None`.
    - `download_asr_model` rejects unknown model IDs with `AiModelInstallError` instead of falling back to `"base"`.
    - `download_asr_model` rejects truncated/mismatched transfers and cleans up `.part` files.
    - `remove_asr_model` refuses paths outside `models/whisper` (including `../` traversal and arbitrary user files) and returns `False`.
  - Green implementation:
    - Add exact `size_bytes` and `sha256` to all 5 `WHISPER_GGUF_MODELS` entries (`tiny`, `base`, `small`, `medium`, `large-v3-turbo`).
    - Enforce `_managed_model_size_ok` in `resolve_asr_model_path` and restrict explicit paths to `.bin`/`.gguf` files.
    - Pass `expected_size`, `expected_sha256`, and `min_free_bytes` in `download_asr_model` when `url_override` is empty.
    - Guard `remove_asr_model` with `resolved.is_relative_to(base)` and `.bin`/`.gguf` suffix check, returning `bool`.

- [ ] **Task 1.2: Managed `whisper.cpp` (`whisper-cli`) runtime discovery, staged download, `--help` probe, and removal**
  - Files: `src/vienetts_app/core/ai_models.py`, `tests/unit/test_ai_models.py`
  - Red tests:
    - `available_whisper_cpp_variants()` offers platform-appropriate variants (`cpu` on Linux/Windows, `cuda` on Windows x64).
    - `download_whisper_cpp_runtime()` extracts into staging, probes `whisper-cli --help` with `cwd=cli.parent`, writes `runtime.json`, and atomically promotes to `runtimes/whisper`.
    - A broken `whisper.cpp` runtime archive (`exit 2` on `--help`) raises `AiModelInstallError`, cleans up staging, and preserves the previously installed working runtime.
    - `remove_whisper_cpp_runtime()` removes the managed runtime directory and returns `bool`.
  - Green implementation:
    - Implement `whisper_runtime_dir`, `resolve_whisper_cpp_cli`, `is_whisper_cpp_installed`, `available_whisper_cpp_variants`, `default_whisper_cpp_variant`, `resolve_whisper_cpp_variant`, `installed_whisper_cpp_variant`, `download_whisper_cpp_runtime`, and `remove_whisper_cpp_runtime`.

---

## Phase 2: Reliable & High-Performance On-Device ASR Core

- [ ] **Task 2.1: Shared 16 kHz mono WAV normalization, source fingerprint sidecar, and ASR segment caching**
  - Files: `src/vienetts_app/core/asr.py`, `tests/unit/test_asr.py`
  - Red tests:
    - Passing a 48 kHz or stereo `.wav` file to `transcribe_media_to_cues` downmixes/resamples it to 16 kHz mono before invoking `whisper-cli` (verified by a fake CLI inspecting WAV header).
    - Modifying the source media file invalidates a stale `source_16k.wav` in `work_dir` via the `.json` fingerprint sidecar.
    - Re-running `transcribe_media_to_cues` with the same media + `AsrConfig` + `work_dir` serves cues from `asr_segments_cache.json` without invoking the backend a second time.
  - Green implementation:
    - Add `_ensure_16k_wav` with `.json` sidecar (`source`, `size`, `mtime_ns`) in `core/asr.py`.
    - Add `_asr_cache_key`, `_load_cached_segments`, and `_store_cached_segments` (`asr_segments_cache.json`).

- [ ] **Task 2.2: `AsrCancelled` semantics, truthful `auto` fallback reporting, and backend execution hardening**
  - Files: `src/vienetts_app/core/asr.py`, `tests/unit/test_asr.py`
  - Red tests:
    - Cancelling during `transcribe_media_to_cues(..., AsrConfig(backend="auto"), cancel_check=lambda: True)` raises `AsrCancelled` immediately without calling subsequent fallback backends.
    - `auto` records fallback reasons in `notes` and surfaces the real execution error when an installed backend fails instead of masking it when no API is configured.
    - `_run_whisper_cpp` kills a hung child process at `timeout_s`, passes `-t <threads>`, `-sns`, `-pp`, and `-ng` (when `device="cpu"`), streams progress updates from stderr, and decodes UTF-8 output regardless of system locale.
    - `_run_openai_whisper_api` parses JSON responses containing `"-->"` inside `"text"` without raising `SubtitleError`, and uses WAV/payload duration instead of hardcoded `5000 ms` for top-level `"text"` responses.
  - Green implementation:
    - Add `AsrCancelled(AsrError)` and extend `AsrConfig` with `device: str = "auto"`, `threads: int = 0`, `timeout_s: float = 0.0`.
    - Harden `_run_whisper_cpp`, `_run_faster_whisper` (with 1-entry model cache and `condition_on_previous_text=False`), `_run_openai_whisper_api`, and the `auto` dispatch order in `transcribe_media_to_cues`.

- [ ] **Task 2.3: Non-speech token filtering, duration-aware segment splitting, and bidirectional micro-fragment merging**
  - Files: `src/vienetts_app/core/asr.py`, `src/vienetts_app/core/subtitles.py`, `tests/unit/test_asr.py`, `tests/unit/test_subtitles.py`
  - Red tests:
    - Segments containing only `[BLANK_AUDIO]`, `[MUSIC]`, `(music)`, `[Applause]`, or `♪` are dropped by `normalize_asr_segments_to_cues`; inline `[MUSIC] Xin chào` strips `[MUSIC]` and keeps `speaker=""` rather than setting `speaker="MUSIC"`.
    - A segment with `len(text) <= max_chars` (e.g. 50 chars, 2 clauses) but `duration > max_duration_ms` (e.g. `12_000 ms > 7_000 ms`) splits into 2 cues at the clause boundary.
    - A trailing micro-fragment (`< 400 ms`, gap `<= 150 ms`, same speaker) at the end of a sequence merges into the preceding segment.
  - Green implementation:
    - Add non-speech tag stripping in `core/asr.py` and exclude non-speech tags (`MUSIC`, `BLANK_AUDIO`, `APPLAUSE`, `LAUGHTER`, `SILENCE`, `NOISE`, `INAUDIBLE`) from `_SPEAKER_PREFIX_RE` in `core/subtitles.py`.
    - Update `_split_long_segment` to compute `eff_max_chars` when `duration > max_duration_ms`, and add backward micro-fragment merge in `normalize_asr_segments_to_cues`.

---

## Phase 3: Settings, Controller Plumbing, Workspace Collision Guard & QML UI

- [ ] **Task 3.1: ASR device/threads settings, throttled download progress signal, and runtime/model controller slots**
  - Files: `src/vienetts_app/core/models.py`, `src/vienetts_app/core/settings.py`, `src/vienetts_app/ui/controller.py`, `tests/unit/test_settings.py`
  - Red tests:
    - `Settings` validates `asr_device` (`{"auto", "cpu", "cuda", "metal"}`) and `asr_threads` (`0..64`), and `_clamp_engine_fields` clamps invalid values in `settings.json` back to defaults without losing other settings.
    - `AppController` exposes `asrDevice`, `asrThreads`, `whisperCppInstalled`, `whisperCppRuntimeVariant`, `whisperCppAvailableVariants`, `downloadWhisperCppRuntime()`, and `removeWhisperCppRuntime()`, normalizes unknown `asrModel` IDs to `"base"`, and throttles `aiDownloadProgressChanged` so 100 fine-grained progress callbacks emit at most ~100 progress signals and 0 mid-stream `aiModelsInstallChanged` signals.
  - Green implementation:
    - Extend `core/models.py` and `core/settings.py` with `asr_device` and `asr_threads`.
    - Add `aiDownloadProgressChanged = Signal()` on `AppController`, bind `asrDownloadProgress` and `diarizationDownloadProgress` to it with $\ge 0.01$ delta throttling, and wire `downloadAsrModel`, `removeAsrModel`, `downloadWhisperCppRuntime`, and `removeWhisperCppRuntime`.

- [ ] **Task 3.2: Persistent media cache dir, collision-safe managed `.srt` naming, and ASR warning propagation in `SubtitleController`**
  - Files: `src/vienetts_app/ui/subtitle_controller.py`, `tests/unit/test_subtitle_controller.py`
  - Red tests:
    - Two videos with the same filename in different folders (`dir_a/clip.mp4` and `dir_b/clip.mp4`) write distinct managed `.srt` files in `subtitles/` and `importVideo(dir_b/clip.mp4)` does not auto-load `dir_a/clip.mp4`'s transcript.
    - `transcribeVideo` followed by `runDiarization` shares the persistent per-video cache directory (`source_16k.wav` extracted only once).
    - Cancelling `transcribeVideo` (`AsrCancelled`) resets `aiBusy` cleanly without setting an error banner; fallback notes from ASR/diarization surface in `errorText`.
  - Green implementation:
    - Add `_media_cache_dir(vpath)` and `_managed_srt_path(vpath)` on `SubtitleController`.
    - Plumb `asrDevice` and `asrThreads` into `_asr_config()`, pass `notes` to `transcribe_media_to_cues`, and handle `AsrCancelled`.

- [ ] **Task 3.3: QML controls (`SettingsModels.qml`) + i18n catalog & smoke verification**
  - Files: `src/vienetts_app/ui/qml/settings/SettingsModels.qml`, `src/vienetts_app/ui/i18n/vienetts_en.ts`, `src/vienetts_app/ui/i18n/vienetts_en.qm`, `tests/smoke/test_ui_tabs.py`, `tests/unit/test_i18n.py`
  - Red tests:
    - Offscreen smoke test verifies `asrLanguageCombo`, `asrDeviceCombo`, `asrThreadsField`, `downloadWhisperCppRuntimeButton`, `removeWhisperCppRuntimeButton`, `asrDownloadProgressBar`, and `asrDownloadPercentLabel` in `SettingsModels.qml`.
    - `test_i18n.py` verifies all new Vietnamese/English translations are present and finished.
  - Green implementation:
    - Update `SettingsModels.qml` with the new ASR controls, runtime install buttons, progress bar, and status copy.
    - Run `scripts/update_i18n.sh`, translate new strings in `vienetts_en.ts`, re-run `scripts/update_i18n.sh`, and verify all unit + smoke suites and ruff gates pass.
