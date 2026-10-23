# Track Learnings: asr_hardening_20261021

Patterns, gotchas, and context discovered during implementation.

## Codebase Patterns (Inherited)

- **Verified managed installs (`core/ai_models.py`)**: Every managed model download must verify `Content-Length`, catalog `size_bytes`, and `sha256` before promoting `.part` → final path, and `_managed_model_size_ok` must gate `is_*_installed()` so truncated files from interrupted downloads read as uninstalled.
- **Guarded model removal**: Model settings (`asr_model`, `diarization_model`) are user-writable strings; `remove_*_model` must `resolve()` both the target and `<data_dir>/models/<subdir>` and verify `resolved.is_relative_to(base)` plus suffix `.bin`/`.gguf` before unlinking.
- **Staged native runtime install with `--help` probe**: Extract runtime archives into a `.staging_<uuid>` directory, `chmod +x` the binary on POSIX, run `<cli> --help` with `cwd=cli.parent` (so `RUNPATH=$ORIGIN` and `ggml` backend discovery find sibling shared libraries), write `runtime.json`, and atomically swap into `<data_dir>/runtimes/<name>` with backup rollback on failure.
- **Dedicated cancellation exception in `auto` chains**: `auto` fallback loops catch backend `RuntimeError` subclasses (`AsrError`, `DiarizationError`); user cancellation must raise a distinct subclass (`AsrCancelled`, `DiarizationCancelled`) that `auto` re-raises immediately instead of falling through to the next backend.
- **Windows subprocess UTF-8 discipline**: Always pass `text=True, encoding="utf-8", errors="replace"` to `subprocess.Popen` / `subprocess.run` when reading stdout/stderr from native CLIs (`whisper-cli`, `audiocpp_cli`, `ffmpeg`) so non-ASCII Vietnamese output never crashes `cp1252` reader threads.
- **i18n catalog gate**: Any change to user-facing `qsTr` / `self.tr` strings requires running `scripts/update_i18n.sh`, translating new `<translation type="unfinished">` entries in `src/vienetts_app/ui/i18n/vienetts_en.ts`, and re-running `scripts/update_i18n.sh` to compile `vienetts_en.qm`.

---

<!-- Learnings from implementation will be appended below -->

## [2026-10-21 11:00] - Phase 1 Task 1.1: Pin WHISPER_GGUF_MODELS size/SHA-256, verify downloads, and guard resolve_asr_model_path / remove_asr_model
- **Implemented:** Pinned exact `size_bytes` and `sha256` for all 5 `WHISPER_GGUF_MODELS` (`tiny`, `base`, `small`, `medium`, `large-v3-turbo`), wired `expected_size`/`expected_sha256`/`min_free_bytes` in `download_asr_model`, enforced `_managed_model_size_ok` and `.bin`/`.gguf` suffix validation in `resolve_asr_model_path`, and guarded `remove_asr_model` with `resolved.is_relative_to(asr_models_dir)`.
- **Files changed:** `src/vienetts_app/core/ai_models.py`, `tests/unit/test_ai_models.py`
- **Commit:** `b09dd45`
- **Learnings:**
  - Patterns: In `resolve_asr_model_path`, check `by_filename` across `WHISPER_GGUF_MODELS` in the fallback loop too so passing `"ggml-base.bin"` directly still enforces `_managed_model_size_ok` against truncated files.
  - Gotchas: End-to-end download tests that serve synthetic short payloads via `url_override` must monkeypatch `WHISPER_GGUF_MODELS["base"]` with `size_bytes=0, sha256=""` so `is_asr_model_installed` accepts the synthetic test file after promotion.
---

## [2026-10-21 11:05] - Phase 1 Task 1.2: Managed whisper.cpp (whisper-cli) runtime discovery, staged download, --help probe, and removal
- **Implemented:** Added `WHISPER_CPP_RELEASE_TAG = "b5454"`, `WhisperCppRuntimeVariant`, `whisper_runtime_dir`, `resolve_whisper_cpp_cli`, `is_whisper_cpp_installed`, `available_whisper_cpp_variants`, `resolve_whisper_cpp_variant`, `installed_whisper_cpp_variant`, `download_whisper_cpp_runtime` (staged extraction, executable bit setting, `--help` probe with `cwd=cli.parent`, `runtime.json`, atomic promotion with backup rollback), and `remove_whisper_cpp_runtime`.
- **Files changed:** `src/vienetts_app/core/ai_models.py`, `tests/unit/test_ai_models.py`
- **Commit:** `7f5ead2`
- **Learnings:**
  - Patterns: Running `_probe_runtime` with `cwd=str(cli.parent)` allows both `audiocpp_cli` and `whisper-cli` to locate sibling shared libraries (`libwhisper.so`, `libggml-*.so`, `whisper.dll`) during installation verification.
  - Gotchas: `whisper.cpp` Windows archives place `whisper-cli.exe` either at archive root or under `Release/`/`bin/`, so `_locate_whisper_cli` checks `(root / "bin" / name, root / "Release" / name, root / name)` before `rglob`.
---

## [2026-10-21 11:12] - Phase 2 Task 2.1: Shared 16 kHz mono WAV normalization, source fingerprint sidecar, and ASR segment caching
- **Implemented:** Added `_ensure_16k_wav` with `.json` source fingerprint sidecar (`source`, `size`, `mtime_ns`), in-process `soundfile`+`numpy` 16 kHz mono conversion for `.wav` inputs (with `extract_video_audio` fallback), and `asr_segments_cache.json` caching keyed by `(wav_path, size, mtime_ns, backend, model, language)`.
- **Files changed:** `src/vienetts_app/core/asr.py`, `tests/unit/test_asr.py`
- **Commit:** `9bf3c80`
- **Learnings:**
  - Patterns: Downmixing and resampling `.wav` inputs in-process with `soundfile` + `np.interp` before falling back to `ffmpeg` makes 48 kHz/stereo `.wav` normalization work even in environments without `ffmpeg` installed.
  - Gotchas: `_ensure_16k_wav` must verify `_is_16k_mono_wav(target)` in addition to the `.json` sidecar so a corrupted or non-16k cached WAV in `work_dir` is never reused.
---

## [2026-10-21 11:20] - Phase 2 Task 2.2: AsrCancelled semantics, truthful auto fallback reporting, and backend execution hardening
- **Implemented:** Added `AsrCancelled(AsrError)` and `device`/`threads`/`timeout_s` fields on `AsrConfig`, updated `auto` mode to prefer installed `whisper.cpp`, skip the default Ollama URL, record fallback reasons in `notes`, and surface real execution errors; hardened `_run_whisper_cpp` (`-t`, `-sns`, `-pp`, `-ng`, `cwd=binary.parent`, UTF-8 decoding, timeout, stderr progress streaming), `_run_faster_whisper` (1-entry model cache keyed by class + config, `condition_on_previous_text=False`), and `_run_openai_whisper_api` (JSON check before `"-->"` SRT check, WAV/payload duration for top-level `"text"`).
- **Files changed:** `src/vienetts_app/core/asr.py`, `tests/unit/test_asr.py`
- **Commit:** `4709c85`
- **Learnings:**
  - Patterns: Keying `_FASTER_WHISPER_CACHE` on `(faster_whisper.WhisperModel, model_name, fw_device, compute_type, threads)` reuses the loaded model across production calls while automatically invalidating the cache when unit tests monkeypatch `faster_whisper.WhisperModel`.
  - Gotchas: In `_run_whisper_cpp`, use `proc.wait(timeout=0.05)` with bounded `t.join(timeout=0.5)` after `proc.kill()` rather than `proc.communicate()` so a grandchild process holding inherited pipe descriptors cannot hang cancellation or timeout cleanup.
---

## [2026-10-21 11:28] - Phase 2 Task 2.3: Non-speech token filtering, duration-aware segment splitting, and bidirectional micro-fragment merging
- **Implemented:** Added `NON_SPEECH_LABELS` and `_STANDALONE_NON_SPEECH_RE` in `core/subtitles.py` so `[BLANK_AUDIO]`, `[MUSIC]`, etc. are stripped rather than parsed as `Cue.speaker`; added `_strip_non_speech` in `core/asr.py`; updated `_split_long_segment` to compute a duration-proportional `eff_max_chars` when `duration > max_duration_ms`; added backward micro-fragment merge in `normalize_asr_segments_to_cues`.
- **Files changed:** `src/vienetts_app/core/asr.py`, `src/vienetts_app/core/subtitles.py`, `tests/unit/test_asr.py`, `tests/unit/test_subtitles.py`
- **Commit:** `987a3af`
- **Learnings:**
  - Patterns: Sharing `NON_SPEECH_LABELS` between `core/subtitles.py` and `core/asr.py` guarantees that both direct `.srt` parsing (`whisper-cli` output or user files) and raw segment normalization reject `[MUSIC]`/`[BLANK_AUDIO]` as speaker names.
  - Gotchas: When `duration > max_duration_ms` on a cue with `len(text) <= max_chars`, `_split_long_segment` must tighten `eff_max_chars = min(max_chars, max(12, int(len(text) * max_duration_ms / duration)))` before packing clauses, otherwise the clause loop re-joins all clauses into a single overlong cue.
---

## [2026-10-21 11:36] - Phase 3 Task 3.1: ASR device/threads settings, throttled download progress signal, and runtime/model controller slots
- **Implemented:** Added `asr_device` (`auto|cpu|cuda|metal`) and `asr_threads` (`0..64`) to `Settings` and `_clamp_engine_fields`; added `aiDownloadProgressChanged` on `AppController` with `>= 0.01` delta throttling; added `asrDevice`, `asrThreads`, `whisperCppInstalled`, `whisperCppRuntimeVariant`, `whisperCppAvailableVariants`, `downloadWhisperCppRuntime()`, `removeWhisperCppRuntime()`, and normalized unknown `asrModel` IDs to `"base"`.
- **Files changed:** `src/vienetts_app/core/models.py`, `src/vienetts_app/core/settings.py`, `src/vienetts_app/ui/controller.py`, `tests/unit/test_settings.py`
- **Commit:** `804616c`
- **Learnings:**
  - Patterns: Separating `aiDownloadProgressChanged` (progress float only, throttled to `>= 0.01` deltas) from `aiModelsInstallChanged` (start/finish/error only) avoids triggering QML `stat()`/`rglob()` property re-evaluations on every 256 KB network chunk.
  - Gotchas: Include `asr_device`, `asr_threads`, `diarization_device`, `diarization_threads`, and `subtitle_export_speaker_labels` in `AppController._set_setting`'s signal table so QML bindings watching `aiSettingsChanged` update immediately on setter writes.
---

## [2026-10-21 11:44] - Phase 3 Task 3.2: Persistent media cache dir, collision-safe managed .srt naming, and ASR warning propagation in SubtitleController
- **Implemented:** Added `_managed_srt_path` (`<stem>_<hash8>.srt`) and `_media_cache_dir` (`subtitles/_media_cache/<stem>_<hash8>/`) in `SubtitleController`, preserved clean video stem as `project.title`, wired `asrDevice`/`asrThreads` into `_asr_config()`, and combined ASR `notes` with diarization warnings in `transcribeVideo()`.
- **Files changed:** `src/vienetts_app/ui/subtitle_controller.py`, `tests/unit/test_subtitle_controller.py`
- **Commit:** `7a3090e`
- **Learnings:**
  - Patterns: Disambiguating managed `.srt` filenames with an 8-hex digest of `video.resolve()` while passing `title=Path(eff_video).stem` to `build_project` prevents same-stem videos in different folders from overwriting each other's subtitles without polluting `project.title` or exported filenames.
  - Gotchas: Using the persistent `_media_cache_dir(vpath)` in both `transcribeVideo` and `runDiarization` lets `source_16k.wav`, `asr_segments_cache.json`, and `diar_turns_cache.json` survive across repeat transcriptions and re-diarizations of the same video.
---

## [2026-10-21 11:55] - Phase 3 Task 3.3: QML controls (SettingsModels.qml) + i18n catalog & smoke verification
- **Implemented:** Added `asrLanguageCombo`, `asrDeviceCombo`, `asrThreadsField`, `downloadWhisperCppRuntimeButton`, `removeWhisperCppRuntimeButton`, `asrDownloadProgressBar`, and `asrDownloadPercentLabel` in `SettingsModels.qml`, updated `asrInstallStatusLabel` to report both model and `whisper.cpp` runtime status, translated all 29 new strings in `vienetts_en.ts`/`.qm` (1065/1065 finished), and verified in `test_ui_tabs.py` and `test_i18n.py`.
- **Files changed:** `src/vienetts_app/ui/qml/settings/SettingsModels.qml`, `src/vienetts_app/ui/i18n/vienetts_en.ts`, `src/vienetts_app/ui/i18n/vienetts_en.qm`, `tests/smoke/test_ui_tabs.py`, `tests/unit/test_i18n.py`
- **Commit:** `d50f43b`
- **Learnings:**
  - Patterns: Folding the new ASR control existence assertions into the existing `settings_qwen_states` scenario in `test_ui_tabs.py` verifies the live QML surface with zero additional subprocess startup cost.
---
