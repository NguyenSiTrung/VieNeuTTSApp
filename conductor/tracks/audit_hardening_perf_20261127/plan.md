# Codebase Hardening, Reliability, Security & Performance — Implementation Plan

> **For agentic workers:** Parallel execution is enabled for phases and tasks
> annotated `<!-- execution: parallel -->` with disjoint `<!-- files: ... -->`
> ownership. Read `spec.md`, `conductor/patterns.md`, and this track's
> `learnings.md` before starting any task. Sub-agents run scoped tests only;
> serialize git commits and full-suite gates through the orchestrator to avoid
> `.git/index.lock` races.

**Track ID:** `audit_hardening_perf_20261127`  
**Spec:** [spec.md](./spec.md)  
**Goal:** Remediate all 74 verified audit findings (7 Critical, 27 High, 33
Medium, 7 Low) and close overlapping beads `VieNeuTTSApp-02pc`,
`VieNeuTTSApp-c64z`, and `VieNeuTTSApp-73r3` with 100% automated TDD
verification and zero new runtime dependencies.

## Global Constraints & Per-Task Gates

- Follow `conductor/workflow.md` TDD loop: write failing red tests first ->
  implement minimal fix/improvement -> refactor within owned files.
- Automated verification only (`manual_phase_verification: false`); no manual
  per-phase pause checkpoints.
- Per-task gate from repository root (unset `PYTHONDONTWRITEBYTECODE`):
  ```bash
  .venv/bin/ruff check .
  .venv/bin/ruff format --check .
  QT_QPA_PLATFORM=offscreen QT_AUDIO_BACKEND=ffmpeg .venv/bin/pytest
  ```
  Run `scripts/update_i18n.sh` whenever any user-facing string changes (must
  finish with 0 unfinished entries).
- Commit locally after each task with a conventional commit prefix and attach a
  task summary via `git notes add -m "..."`. Never run `git push`, `git pull`,
  `git fetch`, or `bd dolt push` per task.
- Documented pre-existing host baselines (`VieNeuTTSApp-3iy` real-`QAudioSink`,
  linux-aarch64 x64-cell tests) must not be masked; re-run any suspected flake
  in isolation before calling it a regression.

---

## Phase 1: Native ABI, Qwen IPC/Engine, Workers & Live Playback (FR-1, FR-2)
<!-- execution: parallel -->
<!-- depends: -->

- [x] **Task 1.1: GGUF ABI callback detachment safety & short-write IPC framing** (FR-1.1, FR-1.2) — `1773614`
  <!-- files: src/vienetts_app/workers/qwen_gguf_abi.py, src/vienetts_app/core/qwen_protocol.py, tests/unit/test_qwen_gguf_abi.py, tests/unit/test_qwen_protocol.py -->
  - [x] Red tests: real `_bind_signatures` `close()` detaches `qt_log_set` via `_QtLogCb(0)` without `ctypes.ArgumentError`; `set_log_callback()` swap retains prior callback across C call; `_chunk_relay()` guards NULL pointer & `n_samples <= 0`; empty `(0, 0)` `codes` leaves `ref_codes = None` / `ref_T = 0`; `write_frame()` loops over partial writes on raw `io.FileIO` streams (>64 KiB frames) and raises `OSError` on zero progress.
  - [x] Implement fixes in `qwen_gguf_abi.py` and `qwen_protocol.py`; run gates and commit.

- [x] **Task 1.2: Qwen engine job isolation, cancel generation snapshot, macOS RAM & host reliability** (FR-1.3, FR-1.4) — `fd39737`
  <!-- files: src/vienetts_app/core/qwen_engine.py, src/vienetts_app/workers/qwen_host.py, src/vienetts_app/workers/qwen_gguf_host.py, tests/unit/test_qwen_engine.py, tests/unit/test_qwen_gguf_host.py -->
  <!-- depends: task1 -->
  - [x] Red tests: `_stream_job()` skips mismatched `frame.job`; abandoned/cancelled jobs and sequences settle active host jobs and drain stale `_inbox` frames; `cancel()` snapshots `generation` before `_wait_cancelled()` / `_abort_host()`; macOS `physical_ram_bytes()` queries `hw.memsize`; `_drain_stderr()` wraps stream in `io.BufferedReader` and RSS sampling runs outside `self._settled`; `QwenGgufHost._source_clip()` validates positive rate & finiteness before resampling; both host reader threads catch `(OSError, ValueError)` and post `None`; `QwenModelHost._prompts` uses bounded content-hashed LRU & `_heartbeats()` sets `drained` in `finally:`; Windows `SetPriorityClass`, `SetStdHandle`, `OpenProcess`/`GetProcessMemoryInfo`/`CloseHandle` use typed `wintypes.HANDLE` on local `WinDLL`.
  - [x] Implement fixes in `qwen_engine.py`, `qwen_host.py`, and `qwen_gguf_host.py`; run gates and commit.

- [x] **Task 1.3: InferenceWorker async cancel, `FifoJobQueue.wake()`, `engine.py` patch lock/voices & `__main__.py` early host dispatch** (FR-1.5) — `87daf99`
  <!-- files: src/vienetts_app/workers/inference_worker.py, src/vienetts_app/workers/job_queue.py, src/vienetts_app/core/engine.py, src/vienetts_app/__main__.py, tests/unit/test_inference_worker.py, tests/unit/test_engine.py, tests/unit/test_app_entry.py -->
  - [x] Red tests: `InferenceWorker.stop(timeout_ms=0)` never blocks on slow provider cancel (deduplicated background thread); `cancel_owner()` & `cancel_job()` catch the dequeue-to-active window and `_process_voice_job()` honors abort; `chunk_stream` closes via `contextlib.closing` before `_terminalize()`; `FifoJobQueue.wake()` unblocks `take()` immediately (no 20 Hz polling); module-level `_SDK_PATCH_LOCK` serializes `OnnxV3LiteEngine.__init__` & `ort.InferenceSession` patches; corrupt `voices.json` entry is skipped without half-initializing `self._tts`; `__main__.py` dispatches `--qwen-host`, `--qwen-gguf-host`, and `--version` before `_apply_startup_thread_caps()` and before importing `qwen_engine`/`numpy`.
  - [x] Implement fixes in `inference_worker.py`, `job_queue.py`, `engine.py`, and `__main__.py`; run gates and commit.

- [x] **Task 1.4: Live streaming playback (<150 ms, sink recovery, Float32 alignment, `TransportIODevice` cleanup) & `PlaybackController` parenting** (FR-2.1, FR-2.2, FR-2.3) — `67e045c`
  <!-- files: src/vienetts_app/core/pcm_transport.py, src/vienetts_app/ui/stream_playback.py, src/vienetts_app/ui/playback.py, tests/unit/test_stream_playback.py, tests/unit/test_replay_transport.py -->
  - [x] Red tests: `<150 ms` gracefully closed transport satisfies `ready_for_prebuffer()`; Float32 `_take()` and `take()` align reads to 4-byte `FRAME_BYTES`; transient sink error disconnects/deletes/resets `self._sink = None` and clears `_error_text` on next `start()`; `_shutdown_session()` closes/unparents/`deleteLater()`s `TransportIODevice` (0 leaked Qt child objects across repeated sessions); `PlaybackController` parents `QMediaPlayer` to `self` and clears source (`setSource(QUrl())`) on `EndOfMedia`.
  - [x] Implement fixes in `pcm_transport.py`, `stream_playback.py`, and `playback.py`; run gates and commit.

---

## Phase 2: Managed Installs, Disk-Leak Cleanup, Hash Speed, Security & Packaging (FR-6, FR-7, FR-8)
<!-- execution: parallel -->
<!-- depends: -->

- [x] **Task 2.1: Bundle `vienetts_en.qm` in frozen builds & restore i18n catalog gate (`VieNeuTTSApp-73r3`)** (FR-8.1) — `d73b232`
  <!-- files: packaging/vienetts-app.spec, .github/workflows/release.yml, tests/unit/test_packaging_i18n.py -->
  - [x] Red tests: assert `packaging/vienetts-app.spec` includes `(..., "vienetts_app/ui/i18n")` for `vienetts_en.qm`, `.github/workflows/release.yml` asserts `vienetts_en.qm` exists in `dist/`, `vienetts_en.ts` has 0 unfinished and 0 obsolete entries, and `vienetts_en.qm` exists and loads cleanly via `QTranslator`.
  - [x] Update `packaging/vienetts-app.spec` and `.github/workflows/release.yml`; run gates, close `VieNeuTTSApp-73r3`, and commit.

- [x] **Task 2.2: Cross-platform paths, GPU detection, settings/clones/models reliability, `updates.py` & `crash.py` security** (FR-6.4, FR-7.2, FR-7.3) — `c49538a`
  <!-- files: src/vienetts_app/core/paths.py, src/vienetts_app/core/detector.py, src/vienetts_app/core/models.py, src/vienetts_app/core/settings.py, src/vienetts_app/core/voice_profiles.py, src/vienetts_app/core/updates.py, src/vienetts_app/crash.py, tests/unit/test_settings.py, tests/unit/test_detector.py, tests/unit/test_updates.py, tests/unit/test_startup_hardening.py -->
  - [x] Red tests: `to_extended` normalizes via `abspath`; `path_to_file_url` preserves `C:` (`safe='/:%'`); `sanitize_filename` strips whitespace before `.` (catching `"CON .txt"`) and enforces UTF-8 byte cap; `to_short_path` uses `to_extended` + typed `WinDLL`; `_which_nvidia_smi` checks `%PROGRAMFILES%\NVIDIA Corporation\NVSMI`; `load_settings` retries transient Windows `PermissionError` and degrades gracefully on `OSError`, `_ensure_legacy_migration` locks & stages atomically; `Settings` sets `repr=False` on API keys and `TTSRequest._check_context` validates `ref_audio` for `qwen_base_0_6b`; `CloneStore` uses `RLock`, catches `UnicodeDecodeError` (no quarantine on transient `OSError`), checks `soundfile.info()` duration before decode, and restricts `_resolve_reference` to regular non-symlink files inside `references/`; `updates.py` allowlists `https://` GitHub hosts/repo paths, caps reads at 2 MiB, blocks downgrade redirects, and catches `UnicodeDecodeError`/`HTTPException`; `crash.py` does not show blocking modal dialogs in `sys.unraisablehook`, locks `write_crash_report()`, enforces `0o600` + per-user temp + `O_NOFOLLOW`, redacts tokens/keys, and passes `osascript` args via `on run {t, m}`.
  - [x] Implement fixes across those 7 modules; run gates and commit.

- [x] **Task 2.3: `managed_install.py` unique stamps, Windows `\\?\` `safe_remove`, inline SHA-256 & `model_manager.py` `StampLedger`** (FR-6.2, FR-6.3, FR-6.4) — `f45355c`
  <!-- files: src/vienetts_app/core/managed_install.py, src/vienetts_app/core/model_manager.py, tests/unit/test_model_manager.py -->
  - [x] Red tests: `StampLedger.persist()` writes unique UUID temp file with Windows `PermissionError` retry; `safe_remove()` routes through `_writable_path()` with read-only `onexc` handler, plus strict removal verification for `remove()` & `promoted_install()` and Windows `PermissionError` retries on `os.replace`; `stream_to_file(mode="wb")` computes SHA-256 inline; `ModelManager.inspect()` uses `StampLedger` (0 full SHA-256 passes on warm stamped startup) and passes `to_extended` to `hf_hub_download` on Windows.
  - [x] Implement fixes in `managed_install.py` and `model_manager.py`; run gates and commit.

- [x] **Task 2.4: Eliminate CUDA/Qwen 3.5–7.7 GB & 682 MB disk leaks, proactive legacy pruning, resilient resume & stamped promotion** (FR-6.1, FR-6.2, FR-6.3, FR-7.2) — `bfda07f`
  <!-- files: src/vienetts_app/core/cuda_runtime.py, src/vienetts_app/core/qwen_runtime.py, src/vienetts_app/core/qwen_model_manager.py, src/vienetts_app/core/qwen_gguf_runtime.py, src/vienetts_app/core/qwen_gguf_models.py, tests/unit/test_qwen_gguf_models.py, tests/unit/test_qwen_variants.py -->
  <!-- depends: task2, task3 -->
  - [x] Red tests: CUDA & Qwen wheel caches live outside `_staging_dir()`, delete after promotion, raise `_required_free_bytes` (`>= 3.5×`), and proactively prune legacy `active/wheels/` on `inspect()`/`install()`/`repair()`; `QwenModelManager._download_into()` passes staging root as `local_dir` (no double-nested `speech_tokenizer/speech_tokenizer/`), deletes `.cache/` before promotion in both Qwen model managers, cleans `.staging/shared/` after `_promote_shared()`, and proactively prunes legacy nested `speech_tokenizer/speech_tokenizer/`; partial downloads survive transient network errors/cancel with bounded backoff retries; checksum mismatch unlinks corrupt files in `QwenGgufRuntimeManager` and `QwenGgufModelManager` (clearing HF `.metadata`); `http.client.HTTPException` caught cleanly; `_PackArchive.extract` caps bytes at `record.size_bytes + 1`; promotion persists fresh `FileStamp`s so post-promote `inspect()` skips re-hashing.
  - [x] Implement fixes across those 5 managers; run gates and commit.

- [x] **Task 2.5: `ai_models.py` HTTPS/redirect/size-cap/archive/removal hardening & injectable `driver_cuda_version`** (FR-5.3, FR-6.2, FR-6.4, FR-7.2) — `40921d7`
  <!-- files: src/vienetts_app/core/ai_models.py, tests/unit/test_ai_models.py -->
  <!-- depends: task2, task3 -->
  - [x] Red tests: `_stream_download()` enforces `https://` (no downgrade redirects), bounds bytes inside the read loop, and catches `http.client.HTTPException` & bad `Content-Length`; `_extract_archive()` wraps `BadZipFile`/`TarError`/`OSError` into `AiModelInstallError` and uses `to_extended` on Windows; `_promote_runtime_dir()` moves backup `os.replace` inside `try:` with Windows `PermissionError` retries; `download_audiocpp_runtime()` preflights combined `total_bytes`; `remove_asr_model()`/`remove_diarization_model()` remove corrupt catalog files; all 3 runtime removers enforce symlink/containment guards; `available_audiocpp_variants(driver_cuda_version=...)` skips synchronous `probe_cuda_driver()` when supplied.
  - [x] Implement fixes in `ai_models.py`; run gates and commit.

---

## Phase 3: Audio DSP, EPUB, Audiobook, Exports, Batch & Studio Core (FR-3)
<!-- execution: sequential -->
<!-- depends: phase2 -->

- [x] **Task 3.1: WSOLA `rate < 1.0` tail clamp, header-only `wav_duration_seconds`, block-wise `trim_speech_edges`, short UUID export temps & `epub.py` container recursion** (FR-3.1, FR-3.2) — `ac829af`
  <!-- files: src/vienetts_app/core/audio.py, src/vienetts_app/core/epub.py, src/vienetts_app/core/epub_export.py, src/vienetts_app/core/m4b_export.py, tests/unit/test_audio.py, tests/unit/test_audiobook_m4b.py, tests/unit/test_importers.py -->
  - [x] Red tests: `time_stretch_audio()` at `rate=0.5/0.75` produces continuous non-zero tail energy (no 50 ms / 50% zero-padding); `wav_duration_seconds()` reads `soundfile.info()` without decoding PCM; `trim_speech_edges()` block scan matches exact reference indices; 80-char CJK titles produce short UUID-only temp filenames (<255 bytes); `export_epub_media_overlays()` rejects empty `dest_dir`; `_extract_xhtml()` preserves separate `<p>` paragraphs inside `<section>`/`<div>`/`<article>`, wraps `zlib.error`/`EOFError`/`RuntimeError`/`LargeZipFile` into `DocumentImportError`, and normalizes `/./` hrefs via `posixpath.normpath()`.
  - [x] Implement fixes in `audio.py`, `epub.py`, `epub_export.py`, and `m4b_export.py`; run gates and commit.

- [x] **Task 3.2: `timeline.py` CJK/hard-split character offsets & `bisect` `locate_segment` + `studio.py` multi-part chapters, stretch cache & bounded RAM** (FR-3.4, FR-3.5) — `b01a8cf`
  <!-- files: src/vienetts_app/core/timeline.py, src/vienetts_app/core/studio.py, tests/unit/test_timeline.py, tests/unit/test_studio.py -->
  - [x] Red tests: `map_segment_offsets()` accurately aligns mid-token hard-splits (>512 chars) and spaceless CJK paragraphs; `locate_segment()` uses `bisect` matching exact boundary/clamp semantics; `load_project_from_chapters()` loads multi-part `ch_XXXX_sYY.wav` chapters; `SilenceTrimOp` block scan matches exact bounds without full-array temporaries; `GainOp`/`NormalizeOp`/`_concat` avoid redundant `.astype(np.float32)` copies; empty mix before `SpeedOp` and empty SRT clip speed change short-circuit cleanly; `envelope_for()` guards non-finite samples.
  - [x] Implement fixes in `timeline.py` and `studio.py`; run gates and commit.

- [x] **Task 3.3: `audiobook.py`, `chapter_persist.py`, `audiobook_controller.py` & `batch_controller.py` persistence, multi-part & export hardening** (FR-3.3, FR-3.4, FR-3.5) — `42a1b98`
  <!-- files: src/vienetts_app/core/audiobook.py, src/vienetts_app/ui/chapter_persist.py, src/vienetts_app/ui/audiobook_controller.py, src/vienetts_app/ui/batch_controller.py, tests/unit/test_audiobook.py, tests/unit/test_batch_controller.py -->
  - [x] Red tests: multi-part `promote_chapter_part(..., replace=True)` clears stale ready/provenance & sibling segments before Part 0; failed `os.replace` never unlinks pre-existing `target`; `_ChapterPersistJob.run()` catches `ArtifactWriteError` and always emits `chapterPersisted`; multi-part continuation state keyed by `(book_id, index)`, survives persist gaps, honors `cancelRender()`, and clears on error/book switch; `cancelExport()` keeps `_exporting=True` until worker finishes; multi-part playback populates `_play_segment_durations`, loads segment 0 / transition envelopes, and preserves `(-1, -1)` timeline sentinels; `audiobook_controller` uses `split_text_for_profile` and `_save_timeline` checks `len(segment_samples) == len(segments)`; `export_chapter`/`export_mp3_chapters` reject empty `dest_dir`; `resolve_book_export_metadata` passes `include_cover=(cover_bytes is None)`; `_save_progress` and `removeBook` run off GUI thread with `_index_lock` + `_book_lock` in `add_book` + `_open_generation` bump + cached chapter readiness; `BatchFileController` resolves `_export_target` at `_start_next_save()` using `sanitize_filename`, tracks playing/current by `uid` on removal, and keeps `_running=True` while `STATUS_IMPORTING`.
  - [x] Implement fixes in `audiobook.py`, `chapter_persist.py`, `audiobook_controller.py`, and `batch_controller.py`; run gates and commit.

---

## Phase 4: Subtitles, Video Dubbing, ASR, Diarization & LLM Core (FR-4, FR-7.1, bead c64z)
<!-- execution: parallel -->
<!-- depends: phase1, phase2, phase3 -->

- [x] **Task 4.1: `processes.py` bounded pipe buffers & process-tree teardown + `onnx_audio.py` duration cap & session lifecycle** (FR-4.3) — `a46465f`
  <!-- files: src/vienetts_app/core/processes.py, src/vienetts_app/core/onnx_audio.py, tests/unit/test_processes.py, tests/unit/test_onnx_audio.py -->
  - [x] Red tests: `run_cli()` caps/tail-buffers stdout/stderr lines (skipping unbounded retention when streamed), cleans up `proc` if setup before `try:` raises, and terminates grandchild process trees (POSIX process group / Windows Job Object); `SpeakerEmbedder.embed()` caps input duration; `onnx_audio.get_session()` evicts stale keys for the same resolved path and `release_onnx_sessions()` clears cached sessions.
  - [x] Implement fixes in `processes.py` and `onnx_audio.py`; run gates and commit.

- [x] **Task 4.2: `llm_client.py` & `llm_refine.py` HTTP security + `fit_timing` total-failure & `_parse_reply` fixes** (FR-4.4, FR-7.1) — `e097c1e`
  <!-- files: src/vienetts_app/core/llm_client.py, src/vienetts_app/core/llm_refine.py, tests/unit/test_llm_client.py, tests/unit/test_llm_refine.py -->
  - [x] Red tests: reject non-http/https or scheme-less URLs as `LlmRefineError` inside `try:`; strip `Authorization` on cross-origin or `https->http` redirect; cap `resp.read()` to `MAX_RESPONSE_BYTES`; guard `http_error_detail()`; `refine_cues_with_llm()` raises when all attempted `fit_timing` batches fail even if earlier batches were skipped; `_parse_reply()` ignores empty `[]` preamble and non-string `item.get("text")`.
  - [x] Implement fixes in `llm_client.py` and `llm_refine.py`; run gates and commit.

- [x] **Task 4.3: `subtitles.py` & `video_dub.py` SRT round-trip safety + portrait Display Matrix rotation & ASS escaping** (FR-4.4) — `653b2f7`
  <!-- files: src/vienetts_app/core/subtitles.py, src/vienetts_app/core/video_dub.py, tests/unit/test_subtitles.py, tests/unit/test_video_dub.py -->
  - [x] Red tests: `format_srt()` sanitizes `-->` and flattens newlines so `parse_cues()` round-trips without error; `parse_cues()` retains a digit-only final cue lacking a trailing blank line; `probe_video_info()` reads Display Matrix `rotation` (and `rotate` tag fallback) and swaps `width`/`height` for `±90°`/`270°`; `_ass_escape()` replaces `\` with `"\\\u2060"` before `{`/`}` so `\N`/`\n`/`\h` stay literal.
  - [x] Implement fixes in `subtitles.py` and `video_dub.py`; run gates and commit.

- [x] **Task 4.4: Single-pass Silero VAD, `asr.py` & `diarization.py` CLI `cwd`/abs paths, HTTP security, O(N) sweeps & single-handle WAV reads** (FR-4.1, FR-4.3, FR-7.1) — `27c40e7`
  <!-- files: src/vienetts_app/core/asr.py, src/vienetts_app/core/diarization.py, src/vienetts_app/core/transcribe_pipeline.py, tests/unit/test_asr.py, tests/unit/test_diarization.py -->
  <!-- depends: task1, task2 -->
  - [x] Red tests: `run_transcription()` calls `refine_cues_speech_bounds()` at most once across ASR + turn splits + speaker assignment; `extract_speaker_turns()` passes `cwd=cli.parent` and creates `turns_temp` inside `try:`; `_run_whisper_cpp()` passes `.absolute()` paths; `_upload_whisper_api()` validates URL scheme inside `try:`, strips `Authorization` on redirect, guards null/non-numeric `start`/`end`, and backs off on HTTP 429/5xx (`Retry-After`); `_run_openai_whisper_api()` keeps one `sf.SoundFile` open and unlinks chunk WAVs after upload; `_asr_cache_key` encodes resolved backend/model in `auto` mode; `_merge_whisper_tokens()` splits CJK characters; `_FASTER_WHISPER_CACHE` respects `release_asr_models()` generation & post-load `cancel_check()`; `_neural_cue_vectors()` isolates Silero errors & treats `spans == []` as `(None, 0)` non-anchor; two-pointer / `_merge_spans` sweeps in `diarization.py` & `asr.py` + single `sf.SoundFile` in `_run_acoustic_diarization()`; `extract_speaker_reference_wav()` forwards `cancel_check` to `extract_video_audio()` and maps `VideoDubCancelled` -> `DiarizationCancelled`.
  - [x] Implement fixes in `asr.py`, `diarization.py`, and `transcribe_pipeline.py`; run gates and commit.

- [x] **Task 4.5: `subtitle_project.py` & `subtitle_controller.py` Windows handle release, cancel/state races, `render_id` splice guard & off-thread `removeProject()` (`VieNeuTTSApp-c64z`)** (FR-4.1, FR-4.2, FR-4.4) — `a94787c`
  <!-- files: src/vienetts_app/core/subtitle_project.py, src/vienetts_app/ui/subtitle_controller.py, src/vienetts_app/core/studio.py, tests/unit/test_subtitle_project.py, tests/unit/test_subtitle_controller.py -->
  <!-- depends: task3, task4 -->
  - [x] Red tests: `render()`, `playSpeakerSample()`, and `cloneSpeakerFromVideo()` stop playback before replacing files (`_replace_retrying` on ref WAV); `_sample_generation` bumped in `_cancel_owners()`, `_clear_workspace()`, and `shutdown()`; `clearVideo()` gated by `_refuse_if_busy()`; `exportVideo` progress routes through generation-checked `ProgressRelay`; `transcribeVideo.done()` passes `already_refined=True`; `_apply_speaker_voice()` preserves stored render metadata; `_update_project_cues()`/`_save_imported()` acquire `_store_lock`, emit `_projects_changed()`, and write `.srt` via `export_srt_file()`; `_RenderCommitStore` receives `store.load(id) or project`; multiple deferred invalidations queue cleanly and set `_stale_reason` only on AI success; `SubtitleStudioMeta` records `render_id` validated in `splice_studio_window_into_subtitle()` (streaming via single `sf.SoundFile`); `removeProject()` runs abort-flush + `store.remove()` + media prune off the GUI thread.
  - [x] Implement fixes in `subtitle_project.py`, `subtitle_controller.py`, and `studio.py`; run gates, close `VieNeuTTSApp-c64z`, and commit.

---

## Phase 5: Pool Partitioning, `AppController` Hardening & App Shutdown (FR-1.5, FR-2.3, FR-5.1–5.3, bead 02pc)
<!-- execution: sequential -->
<!-- depends: phase1, phase2, phase3, phase4 -->

- [x] **Task 5.1: Partitioned `QThreadPool`s & module-level `_OrderedJob` in `bg_ops.py` + deterministic teardown in `app.py`** (FR-1.5, FR-2.3, FR-5.1) — `153d4af`
  <!-- files: src/vienetts_app/ui/bg_ops.py, src/vienetts_app/app.py, tests/unit/test_bg_ops.py, tests/unit/test_app_entry.py -->
  - [x] Red tests: `interactive`, `downloads`, and `exports` pools isolate long downloads/muxing from fast UI ops and all drain in `drain_thread_pool()`; `OrderedExecutor.submit()` uses module-level `_OrderedJob` (no per-call dynamic subclass creation); `app.py` tracks & stops startup `QTimer`s before `_teardown_qml()`, wires `playback.stop()`, `batch.cancel()`, and `onnx_audio.release_onnx_sessions()` into shutdown.
  - [x] Implement fixes in `bg_ops.py` and `app.py`; run gates and commit.

- [x] **Task 5.2: `AppController` lane locks, state integrity, thread safety, playhead clock & full download cancel on quit (`VieNeuTTSApp-02pc`)** (FR-2.3, FR-5.2) — `699d39a`
  <!-- files: src/vienetts_app/ui/controller.py, tests/unit/test_controller.py, tests/unit/test_studio_controller.py -->
  - [x] Red tests: `cancelQwenRuntimeInstall()`/`cancelQwenModelDownload()` keep `_qwen_operation="cancelling"` until worker exits; `refreshModelState()` during active official download/import cannot wedge `_model_downloading`; `controller.temperature` emits `temperatureChanged`; `_set_setting()` rolls back `self._settings` on `OSError`; pool work snapshots GUI state (`_start_qwen_inspection`, `_qwen_model_removal_flags`, `studioExportVideo`) and `resolveTorchAvailabilityAsync()` runs via `_run_bg` + `_torch_probe_generation`; `studioSaveToSubtitle`/`studioExportVideo` `on_done` check `_studio_open_generation`; `clear_stretch_cache()` runs on Studio project switch/close; `_replay_pos_timer` stops on first real `QMediaPlayer.positionChanged`; `shutdown(drain_pool=True)` latches `_shutting_down`, cancels `_model_cancel`, `_qwen_cancel`, and wires `cancel_check` into all 4 ASR/diarization downloads, and force-closes retired Qwen hosts.
  - [x] Implement fixes in `controller.py`; run gates, close `VieNeuTTSApp-02pc`, and commit.

- [x] **Task 5.3: `AppController` GUI-thread offloads, workload-pool routing & `qwenModels` `DictListModel`** (FR-5.1, FR-5.3, FR-5.4) — `b3c660e`
  <!-- files: src/vienetts_app/ui/controller.py, src/vienetts_app/ui/list_models.py, tests/unit/test_controller.py, tests/unit/test_studio_controller.py -->
  - [x] Red tests: `audiocppAvailableVariants` uses cached async `CudaDriverProbe` version (0 GUI-thread `nvidia-smi` calls); `openInStudio()`, `studioSetClipSpeed()`, `_maybe_splice_regen()`, and `_complete_voice_op()` offload WAV/WSOLA I/O with generation guards; `profileClones`, `asr*`, `diarization*`, `audioModels`, `ffmpegRuntimeStatus` serve cached async snapshots; downloads route to `downloads` pool and exports route to `exports` pool; `qwenModelsModel` (`DictListModel`) updates progress roles in place without delegate resets.
  - [x] Implement fixes in `controller.py` and `list_models.py`; run gates and commit.

---

## Phase 6: QML Rendering, Virtualization, UX Fixes & Full Verification (FR-5.4, FR-8)
<!-- execution: parallel -->
<!-- depends: phase5 -->

- [x] **Task 6.1: Shared QML components (`AppIcon`, `AppButton`, `AppIconButton`, `AppProgressBar`, `VoicePicker`, `CreateInspector`)** (FR-5.3, FR-5.4) — `e2c9d9f`
  <!-- files: src/vienetts_app/ui/qml/components/AppIcon.qml, src/vienetts_app/ui/qml/components/AppButton.qml, src/vienetts_app/ui/qml/components/AppIconButton.qml, src/vienetts_app/ui/qml/components/AppProgressBar.qml, src/vienetts_app/ui/qml/components/VoicePicker.qml, src/vienetts_app/ui/qml/components/CreateInspector.qml -->
  - [x] Implement: guard `AppIcon` `requestPaint()`/`onPaint` on `visible && kind !== ""`; `Loader`-gate `normalIcon` and `busySpinner` in `AppButton`; remove shadowed `tooltipText`/ToolTip override in `AppIconButton` (restoring `disabledReason`); gate `AppProgressBar` `XAnimator` on `root.visible && root.indeterminate`; filter `VoicePicker` model instead of `height: 0` spacing gaps; debounce `CreateInspector` speed/pause slider writes.

- [x] **Task 6.2: Studio clip `ListView` virtualization, Canvas `StudioTimeline` & `QwenInstallCards` `DictListModel`** (FR-5.4) — `e8e27c5`
  <!-- files: src/vienetts_app/ui/qml/StudioTab.qml, src/vienetts_app/ui/qml/components/StudioTimeline.qml, src/vienetts_app/ui/qml/components/QwenInstallCards.qml -->
  - [x] Implement: replace `StudioTab` clip `Repeater` with virtualized `ListView` over `studioClipModel` and remove `controller.studioClips` `QVariantList` mirror; render `StudioTimeline` clip segments on a single `Canvas`; bind `QwenInstallCards` to `controller.qwenModelsModel` so 250 ms download ticks never tear down the Cancel button.

- [x] **Task 6.3: `SubtitleVideoPreview`, `SubtitlesTab`, `VoicesTab`, `AudiobookTab` & `CloningTab` fixes** (FR-5.4) — `0a5ad24`
  <!-- files: src/vienetts_app/ui/qml/components/SubtitleVideoPreview.qml, src/vienetts_app/ui/qml/SubtitlesTab.qml, src/vienetts_app/ui/qml/VoicesTab.qml, src/vienetts_app/ui/qml/AudiobookTab.qml, src/vienetts_app/ui/qml/CloningTab.qml -->
  - [x] Implement: fix `SubtitleVideoPreview` `scrubSlider.value` binding loop via `Binding`, lazily `Loader`-mount `MediaPlayer` when `hasVideo && !_released`, pause on leaving `SubtitlesTab`, add `Component.onDestruction: releaseVideo()`, add ~400 ms seek cooldown & ~300 ms drift threshold for dub sync, split `_cuesRevision`/`_activeCueRevision`; make `SubtitlesTab` `projectsDialog` bounded & scrollable with footer Close button; debounce `VoicesTab` search; replace custom indeterminate `ProgressBar`s in `AudiobookTab` and `CloningTab` with `AppProgressBar`.

- [x] **Task 6.4: Offscreen QML smoke assertions, `scripts/update_i18n.sh` & final full-suite gate** (FR-5.4, FR-8, all ACs) — `fbc1614`
  <!-- files: tests/unit/test_subtitle_components.py, tests/smoke/test_ui_tabs.py, tests/smoke/test_ui_studio.py, tests/smoke/test_ui_shell.py, src/vienetts_app/ui/i18n/vienetts_en.ts, src/vienetts_app/ui/i18n/vienetts_en.qm -->
  <!-- depends: task1, task2, task3 -->
  - [x] Add/update offscreen smoke assertions covering lazy `AppButton` Canvases, `AppIconButton` `disabledReason`, Studio `ListView`, `SubtitleVideoPreview` lazy player & binding-loop-free scrub, `QwenInstallCards` stable Cancel button, `projectsDialog` scrollability, and `AppProgressBar` indeterminate state.
  - [x] Run `scripts/update_i18n.sh` (0 unfinished), `ruff check .`, `ruff format --check .`, and full offscreen `pytest`; commit and verify all 10 acceptance criteria.
