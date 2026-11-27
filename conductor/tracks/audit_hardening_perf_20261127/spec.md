# Codebase Hardening, Reliability, Security & Performance — Audit Remediation

## Overview

A comprehensive read-only audit of `/root/project/VieNeuTTSApp` (~80k LOC at commit
`671bb49`, v0.4.0) identified **74 verified defects and performance/resource
bottlenecks** (7 Critical, 27 High, 33 Medium, 7 Low) across five subsystems:
engines/workers/streaming, audio DSP/audiobook/exports/studio, video
dubbing/ASR/diarization/subtitles, UI/QML/controllers, and managed
installs/persistence/security/packaging.

This track remediates all 74 audit findings and absorbs the 3 overlapping open
Beads issues (`VieNeuTTSApp-02pc`, `VieNeuTTSApp-c64z`, `VieNeuTTSApp-73r3`).

Track type: **bug-fix, security & performance hardening chore**. No new runtime
dependencies and no user-facing workflow redesigns.

### Approved Decisions (2026-11-27)

- **Scope:** All 4 audit waves plus all verified Medium/Low findings, absorbing
  open beads `VieNeuTTSApp-02pc`, `VieNeuTTSApp-c64z`, and `VieNeuTTSApp-73r3`.
  Structural decomposition of `ui/controller.py` (9,809 LOC) is explicitly
  deferred to a separate future refactor track.
- **Execution:** Phase- and task-level parallel execution enabled across disjoint
  file sets (orchestrator serializes git commits and full-suite gates).
- **Verification:** 100% automated verification (`manual_phase_verification: false`)
  via TDD unit tests, offscreen QML smoke tests, `ruff check .`,
  `ruff format --check .`, `scripts/update_i18n.sh`, and packaging/spec contract
  assertions. No manual pause checkpoints between phases.
- **Priority:** High (`P1` in Beads). No prerequisite tracks (`depends_on: []`);
  no time estimate (`estimated_hours: null`).
- **Legacy disk-leak cleanup:** Proactively prune orphaned legacy
  `active/wheels/` (3.5–7.7 GB) and double-nested
  `.staging/shared/speech_tokenizer/speech_tokenizer/` (682 MB) on the next
  `inspect()` / `install()` / `repair()`.
- **Custom LLM/Whisper endpoint URLs:** Continue permitting both `http://` and
  `https://` (supporting local Ollama/LM Studio/vLLM servers), while rejecting
  non-HTTP schemes (`file://`, `ftp://`, scheme-less URLs), bounding response
  sizes, and stripping `Authorization` headers on cross-origin or `https→http`
  redirects.
- **Workspace & cache preservation:** EPUB parser fixes apply to new imports;
  existing persisted audiobook workspaces, chapters, and valid render
  fingerprints remain untouched.

---

## Functional Requirements

### FR-1: Native ABI, Qwen IPC/Engine & Worker Lifecycle (C1, C2, C3, H1, H2, H3, H5, H6, P2, P7.3)

1. **GGUF ABI safety (`workers/qwen_gguf_abi.py`):**
   - Detach `qt_log_set` in `NativeQwenSession.close()` using a typed NULL
     function pointer `_QtLogCb(0)` (never bare `None`), clearing `self._log_cb`
     only after detachment succeeds; retain the previous callback across
     `set_log_callback()` swap until `qt_log_set` returns.
   - Guard NULL `samples` pointer and `n_samples <= 0` in `_chunk_relay()`, and
     pass `ref_codes = None` / `ref_T = 0` when `codes.size == 0`.
2. **Short-write-safe IPC framing (`core/qwen_protocol.py`):**
   - `write_frame()` loops over `memoryview(encode_frame(frame))` until every
     byte is written on unbuffered `io.FileIO` pipes, raising `OSError` on zero
     progress.
3. **Qwen engine job isolation, cancel & macOS RAM detection (`core/qwen_engine.py`):**
   - `_stream_job()` discards frames where `frame.job` is non-empty and does not
     match `job`.
   - Before sending a new synthesize frame and on abandoned/cancelled job or
     sequence teardown, cancel and settle any active job (or abort the host on
     timeout) and drain stale `_inbox` entries under `self._settled`.
   - Snapshot `generation = self._generation` at `cancel()` entry and pass it to
     `_wait_cancelled()` and `_abort_host()`.
   - Fix `physical_ram_bytes()` on `darwin` using `sysctlbyname("hw.memsize")`
     (or `sysctl -n hw.memsize`) so 8 GB Apple Silicon Macs get reduced batch
     bounds.
   - Wrap `process.stderr` in `io.BufferedReader` inside `_drain_stderr()` and
     move macOS RSS sampling outside `self._settled`.
4. **Host reliability, memory bounds & Windows ctypes (`workers/qwen_host.py`, `workers/qwen_gguf_host.py`, `core/qwen_engine.py`):**
   - Validate positive `rate`, non-empty `data`, and `np.all(np.isfinite(data))`
     *before* resampling in `QwenGgufHost._source_clip()` (mapping `ValueError`
     to `QwenGgufHostError`).
   - Catch `(OSError, ValueError)` in both host reader threads so pipe errors
     post `None` to `inbox` and exit cleanly instead of stranding child
     processes.
   - Bound `QwenModelHost._prompts` as an LRU (`OrderedDict`, max 8) keyed with
     reference file content digest; ensure `_heartbeats()` calls `drained.set()`
     in `finally:`.
   - Bind explicit `ctypes.wintypes.HANDLE` / `DWORD` / `BOOL` `argtypes` and
     `restype` on local `WinDLL` instances for `SetPriorityClass`,
     `SetStdHandle`, `OpenProcess`, `GetProcessMemoryInfo`, and `CloseHandle`.
5. **Worker cancellation, idle wakeup, engine init & frozen host boot (`workers/inference_worker.py`, `workers/job_queue.py`, `core/engine.py`, `app.py`, `__main__.py`):**
   - `InferenceWorker.stop(timeout_ms=...)` dispatches `_cancel_provider_job()`
     asynchronously (deduplicated per job ID) so `stop()` strictly honors
     `timeout_ms` (including `timeout_ms=0`).
   - Store the dequeued job under `_active_lock` immediately after `take()` so
     `cancel_owner()` and `cancel_job()` never miss the dequeue-to-active
     window; check `_is_aborted()` at the start of `_process_voice_job()`.
   - Wrap `chunk_stream` in `contextlib.closing(...)` so provider `finally:`
     runs before `_terminalize()`.
   - Add a `_woken` flag/generation to `FifoJobQueue` so `wake()` interrupts
     `take()` immediately (eliminating 20 Hz idle polling).
   - Guard process-global SDK/ORT monkey-patches in `core/engine.py` with a
     module-level `_SDK_PATCH_LOCK`, and isolate corrupt `voices.json` entries
     per voice before publishing `self._tts`.
   - Add a permanent `_shutting_down` latch set on `drain_pool=True` shutdown,
     cancel/stop tracked startup timers during `_teardown_qml()`, and force-close
     subprocess-backed engines on final quit so no Qwen host or live `QThread`
     survives exit.
   - Dispatch `--qwen-host`, `--qwen-gguf-host`, and `--version` in
     `__main__.py` *before* calling `_apply_startup_thread_caps()` and before
     importing `qwen_engine` / `numpy`.

### FR-2: Live Streaming Playback & Transport (H4, P6.1)

1. **Short utterances & alignment (`core/pcm_transport.py`, `ui/stream_playback.py`):**
   - `BoundedPcmTransport.ready_for_prebuffer()` returns `True` when
     `self._available() >= minimum_bytes` OR when gracefully closed with
     buffered audio (`self._closed and not self._discarded and available > 0`).
   - Align Float32 reads in `TransportIODevice._take()` (and `take()`) to whole
     4-byte `FRAME_BYTES`.
2. **Error recovery & memory leak (`ui/stream_playback.py`):**
   - Disconnect, schedule deletion (`deleteLater()`), and reset `self._sink = None`
     in `_enter_transport_fallback()` (and on `_start_sink` failure); clear
     stale `_error_text` at the start of `start()`.
   - Close, unparent (`setParent(None)`), and `deleteLater()` `self._io` inside
     `_shutdown_session()`.
3. **Playhead & teardown hygiene (`ui/controller.py`, `ui/playback.py`, `app.py`):**
   - Stop `_replay_pos_timer` on the first real `QMediaPlayer.positionChanged`
     tick to eliminate sawtooth jitter.
   - Parent `QMediaPlayer` to `PlaybackController`, release `setSource(QUrl())`
     on `EndOfMedia`, and wire `playback.stop()` + `batch.cancel()`/`shutdown()`
     into `app.py` shutdown and `_teardown_qml()`.

### FR-3: Audio DSP, Audiobook, EPUB/M4B/MP3 Export, Batch & Studio (C4, H7–H12, P6.2, P7.4)

1. **DSP & export temp paths (`core/audio.py`, `core/epub_export.py`, `core/m4b_export.py`):**
   - Fix `time_stretch_audio()` (WSOLA) for `rate < 1.0` by clamping
     `target_pos = min(best_pos + s_syn, n_in - frame_len)` rather than breaking
     early while `out_start + s_syn < target_len`.
   - Fix `wav_duration_seconds()` to query `soundfile.info()` without decoding
     audio into RAM.
   - Convert `trim_speech_edges()` to block-wise edge scanning (bounded RAM).
   - Use short UUID-only temp filenames in `audio.py`, `epub_export.py`, and
     `m4b_export.py` (avoiding 255-byte `NAME_MAX` overflow on multibyte titles).
2. **EPUB parser reliability (`core/epub.py`):**
   - Recurse container/wrapper elements (`div`, `section`, `article`, `blockquote`,
     `header`, `footer`, `aside`) and any block containing descendant block tags
     in `_extract_xhtml()` so `<p>` paragraphs stay distinct.
   - Catch `(zipfile.BadZipFile, zipfile.LargeZipFile, zlib.error, EOFError, RuntimeError)`
     in `import_epub()` and `extract_epub_metadata()`, wrapping into
     `DocumentImportError`.
   - Always normalize paths with `posixpath.normpath()` in `_resolve_href()`.
3. **Audiobook persistence, multi-part state machine & export (`core/audiobook.py`, `ui/chapter_persist.py`, `ui/audiobook_controller.py`, `core/epub_export.py`):**
   - In `promote_chapter_part(..., replace=True)` for multi-part chapters, clear
     prior chapter ready/provenance state and stale sibling segment WAVs/sidecars
     before promoting Part 0; only unlink `target` in `except Exception:` when
     `os.replace(part, target)` actually succeeded.
   - Catch `Exception` (including `ArtifactWriteError`) in
     `_ChapterPersistJob.run()` so `chapterPersisted` always emits.
   - Key/guard multi-part continuation state by `(book_id, index)` across persist
     gaps and clear `_chapter_parts` / `_render_segment` /
     `_render_parts_continue` / `_next_part` / `_play_after_render` on
     `cancelRender()`, `_fail_render()`, `_apply_opened_book()`,
     `selectBook("")`, and `removeBook()`.
   - Keep `_exporting = True` during `cancelExport()` until the worker exits.
   - Populate multi-part `_play_segment_durations` before estimating timelines,
     load segment 0 / transition envelopes properly, and preserve `(-1, -1)`
     sentinels in `_load_merged_segment_timeline()`.
   - Validate `is_empty_path(dest_dir)` in `export_epub_media_overlays()`,
     `export_chapter()`, and `export_mp3_chapters()`; pass
     `include_cover=(cover_bytes is None)` in `resolve_book_export_metadata()`.
   - Offload `_save_progress` and `removeBook` `rmtree` off the GUI thread,
     protect `library.json` with `_index_lock` (+ `_book_lock` in `add_book`),
     bump `_open_generation` / clear `_loading_book_id` in `selectBook("")` and
     `removeBook()`, and cache chapter readiness for `_chapters_model()`.
4. **Timelines (`core/timeline.py`, `ui/audiobook_controller.py`, `ui/chapter_persist.py`):**
   - Use `split_text_for_profile` matching render context in
     `audiobook_controller.py` and verify `len(segment_samples) == len(segments)`
     before `build_timeline()`.
   - Replace whitespace token-count matching in `map_segment_offsets()` with
     whitespace-insensitive character lock-step scanning (fixing hard-splits and
     spaceless CJK).
   - Replace O(N) list-allocating `locate_segment()` with `bisect`.
5. **Batch & Studio (`ui/batch_controller.py`, `core/studio.py`, `ui/audiobook_controller.py`):**
   - Resolve `_export_target()` inside `_start_next_save()` (or reserve in-flight
     targets) using `core.paths.sanitize_filename()`; track playing/current batch
     items by `uid` on list mutations; keep `_running = True` while any item is
     `STATUS_IMPORTING` (inheriting run context).
   - Load multi-part oversize chapters via `chapter_audio_paths` in
     `studio.load_project_from_chapters()` and `AudiobookController.chapterWavPath`.
   - Clear `_stretch_cache` when Studio switches/closes projects; replace
     `SilenceTrimOp`'s full-array `np.nonzero(np.abs(mix) > amp)` with block-wise
     edge scanning; avoid redundant `.astype(np.float32)` copies (`copy=False`)
     and `np.abs` peak arrays; short-circuit empty mixes before `SpeedOp` / empty
     SRT clip speed changes; guard `envelope_for()` with `np.isfinite`.

### FR-4: Subtitles, Video Dubbing, ASR, Diarization & LLM (H13–H16, P1, P6.4, P7.1–7.2, bead c64z)

1. **Single-pass Silero VAD (`core/transcribe_pipeline.py`, `core/asr.py`, `core/diarization.py`, `ui/subtitle_controller.py`):**
   - Use `normalize_asr_segments_to_cues()` inside `run_asr()` and
     `_cues_from_turns()`, running `refine_cues_speech_bounds()` once on the
     final cue list, and pass `already_refined=True` from `transcribeVideo.done()`
     to `_activate_cues_project()`.
2. **Windows handle release, cancel & off-thread removal (`ui/subtitle_controller.py`, `core/subtitle_project.py`, `ui/playback.py`, `core/diarization.py`):**
   - Stop playback (`_stop_playback()`) before `render()`/`renderer.finish()` and
     before speaker ref extraction in `playSpeakerSample()`/`cloneSpeakerFromVideo()`;
     use `_replace_retrying()` in `_extract_speaker_ref()`.
   - Bump `_sample_generation` in `_cancel_owners()`, `_clear_workspace()`, and
     `shutdown()` and guard `playSpeakerSample.failed` on active `project_id` and
     `DiarizationCancelled`.
   - Forward `cancel_check` to `extract_video_audio()` inside
     `extract_speaker_reference_wav()` and map `VideoDubCancelled` →
     `DiarizationCancelled`.
   - Gate `clearVideo()` with `_refuse_if_busy()` and bump sample/bounds
     generations.
   - Move `removeProject()`'s `OrderedExecutor.flush()` + `store.remove()` +
     media cache prune off the GUI thread (completing `VieNeuTTSApp-c64z`).
3. **CLI `cwd`/paths, ONNX hygiene, process trees & algorithmic speedups (`core/diarization.py`, `core/asr.py`, `core/onnx_audio.py`, `core/processes.py`, `core/subtitle_project.py`):**
   - Pass `cwd=cli.parent` in `extract_speaker_turns()` (and create `turns_temp`
     only inside `try:`); pass `.absolute()` paths to `_run_whisper_cpp()`.
   - Isolate per-cue Silero errors in `_neural_cue_vectors()` and return
     `(None, 0)` when Silero finds `spans == []`; cap input duration in
     `SpeakerEmbedder.embed()`; evict stale entries by resolved path in
     `onnx_audio.get_session()` and add `release_onnx_sessions()` wired to
     shutdown.
   - Replace O(C×T)/O(C²) loops in `diarization.py` and O(S×W) loop in `asr.py`
     with two-pointer sweeps / `_merge_spans` + `_covered_ms`.
   - Keep a single `sf.SoundFile` handle open across loops in
     `splice_studio_window_into_subtitle()`, `_run_acoustic_diarization()`, and
     `_run_openai_whisper_api()` (also unlink/reuse chunk WAVs and add bounded
     `Retry-After` backoff on 429/5xx).
   - Include resolved backend/model in `_asr_cache_key` for `auto` mode, guard
     null/invalid `start`/`end` in `_upload_whisper_api()`, split CJK chars in
     `_merge_whisper_tokens()`, and guard `_FASTER_WHISPER_CACHE` with a release
     generation + post-load `cancel_check()`.
   - Cap/tail-buffer stdout/stderr in `processes.run_cli()`, move setup inside
     `try:`, and terminate child process trees (POSIX process group / Windows
     Job Object).
4. **ASS burn-in, SRT round-trip, LLM parsing & Subtitle state (`core/video_dub.py`, `core/subtitles.py`, `core/llm_refine.py`, `ui/subtitle_controller.py`, `core/studio.py`):**
   - Read Display Matrix `rotation` (and `rotate` tag fallback) in
     `probe_video_info()`, swapping `width`/`height` for `±90°`/`270°`; escape
     `\` with `"\\\u2060"` before `{`/`}` in `_ass_escape()`.
   - Sanitize `-->` and newlines in `format_srt()`, and guard
     `position + 1 < len(entries)` in `parse_cues()` for digit-only final cues.
   - Track `attempted_batches` in `refine_cues_with_llm()` (`fit_timing` total
     failure check); require non-empty lists (or prefer `"cues"` dict) and
     `isinstance(text, str)` in `_parse_reply()`.
   - Tag `exportVideo` progress with generation/active check (`ProgressRelay`).
   - Preserve stored render metadata in `_apply_speaker_voice()`, hold
     `_store_lock` + call `_projects_changed()` + atomic `export_srt_file()` in
     `_update_project_cues()`/`_save_imported()`, pass `store.load(id) or project`
     to `_RenderCommitStore`, support queued deferred invalidations with
     `_stale_reason` applied only on AI success, and stamp/verify `render_id` on
     `SubtitleStudioMeta` inside `splice_studio_window_into_subtitle()`.

### FR-5: Controller Threading, Pool Isolation & QML Responsiveness (C5, C6, H17, H18, P4, P5, P6.3, bead 02pc)

1. **Pool partitioning & `OrderedExecutor` (`ui/bg_ops.py`, `ui/controller.py`, `app.py`):**
   - Provide dedicated `QThreadPool` lanes (`interactive`, `downloads`, `exports`)
     so multi-GB downloads and ffmpeg muxing never starve interactive tasks;
     drain all pools on shutdown.
   - Hoist `_OrderedJob(QRunnable)` to module scope in `OrderedExecutor.submit()`.
2. **`AppController` state & cancel safety (`ui/controller.py`):**
   - Keep `_qwen_operation = "cancelling"` until background completion frees it.
   - Guard `refreshModelState()` when `_model_downloading` is active and track
     download completion independently of inspection generation.
   - Add `("temperature", self.temperatureChanged)` to `_set_setting()`; make
     `OSError` persistence failure consistent (roll back `self._settings` on
     write failure so Python, QML, and engine never diverge).
   - Snapshot GUI-thread settings/engine state before pool work in
     `_start_qwen_inspection()`, `_qwen_model_removal_flags()`, and
     `studioExportVideo()`; move `resolveTorchAvailabilityAsync()` onto `_run_bg`
     with `_torch_probe_generation`.
   - Gate `studioSaveToSubtitle` and `studioExportVideo` `on_done` on
     `_studio_open_generation`.
   - Cancel `_model_cancel`, `_qwen_cancel`, and wire `cancel_check` for all 4
     ASR/diarization downloaders in `shutdown(drain_pool=True)` (completing
     `VieNeuTTSApp-02pc`).
3. **Eliminate GUI-thread blocking (`ui/controller.py`, `core/ai_models.py`, `CreateInspector.qml`):**
   - Pass cached async `CudaDriverProbe` version into
     `available_audiocpp_variants()` (zero GUI-thread `nvidia-smi`).
   - Offload `openInStudio()`, `studioSetClipSpeed()`, `_maybe_splice_regen()`,
     and `_complete_voice_op()` WAV I/O & WSOLA onto the interactive pool with
     generation guards.
   - Cache disk/`PATH`-probing `@Property` getters (`profileClones`, `asr*`,
     `diarization*`, `audioModels`, `ffmpegRuntimeStatus`) with async refresh.
   - Debounce `CreateInspector.qml` speed/pause slider persistence (~200 ms or on
     release).
4. **QML rendering, virtualization & UX bug fixes:**
   - `AppIcon.qml` & `AppButton.qml`: skip `requestPaint()`/`onPaint` when
     `!visible || kind === ""`; `Loader`-gate `normalIcon` and `busySpinner`.
   - `StudioTab.qml` & `StudioTimeline.qml`: replace clip `Repeater` with
     virtualized `ListView` over `studioClipModel`, draw timeline on a single
     `Canvas`, remove `controller.studioClips` `QVariantList` mirror.
   - `SubtitleVideoPreview.qml`: fix `scrubSlider.value` binding loop via
     `Binding`, `Loader`-gate `MediaPlayer` when `hasVideo && !_released`, pause
     when leaving `SubtitlesTab`, add `Component.onDestruction: releaseVideo()`,
     add ~400 ms seek cooldown & ~300 ms drift threshold for dub sync, split
     `_cuesRevision` from `_activeCueRevision`.
   - `QwenInstallCards.qml` + `controller.py`: expose `qwenModels` via
     `DictListModel` (or scalar progress properties) so 250 ms progress updates
     never destroy the Cancel button mid-click.
   - `VoicesTab.qml`, `VoicePicker.qml`, `SubtitlesTab.qml`, `AppIconButton.qml`,
     `AudiobookTab.qml`, `CloningTab.qml`, `AppProgressBar.qml`: debounce voice
     search; filter `VoicePicker` model instead of `height: 0` gaps; make
     `projectsDialog` bounded/scrollable; remove shadowed `tooltipText`/ToolTip
     overrides in `AppIconButton.qml`; replace broken indeterminate progress bars
     with `AppProgressBar` and gate `XAnimator` on `root.visible && root.indeterminate`.

### FR-6: Managed Installs, Disk-Leak Cleanup, Hash Speed & Cross-Platform Persistence (H19, H20, P3)

1. **Eliminate permanent disk leaks & clean legacy installs (`core/cuda_runtime.py`, `core/qwen_runtime.py`, `core/qwen_model_manager.py`, `core/qwen_gguf_models.py`):**
   - Keep resumable wheel archives outside `_staging_dir()`, delete after
     promotion, raise `_required_free_bytes` (`>= 3.5×` archive total), and
     proactively delete legacy `active/wheels/` on `inspect()`/`install()`/`repair()`.
   - Pass staging root (not `destination.parent`) as `local_dir` in
     `QwenModelManager._download_into()`, delete `.cache/` before promotion in
     both Qwen model managers, clean `.staging/shared/` after `_promote_shared()`,
     and proactively prune legacy nested `speech_tokenizer/speech_tokenizer/`.
2. **Atomic stamps, safe Windows deletion & resilient resume (`core/managed_install.py`, `core/cuda_runtime.py`, `core/qwen_runtime.py`, `core/qwen_gguf_runtime.py`, `core/qwen_gguf_models.py`, `core/ai_models.py`):**
   - Use unique UUID temp file + Windows `PermissionError` retry in
     `StampLedger.persist()`.
   - Route `safe_remove()` through `_writable_path()` with read-only `onexc`
     handler; add strict removal check for `remove()` and `promoted_install()` +
     Windows `PermissionError` retries around `os.replace` in `promoted_install()`
     and `_promote_runtime_dir()`.
   - Keep partial `< size_bytes` archives on transient network errors/cancel
     with bounded exponential backoff; unlink corrupt files on checksum mismatch
     in `QwenGgufRuntimeManager` and `QwenGgufModelManager` (including HF
     `.metadata`); catch `http.client.HTTPException` and bad `Content-Length`
     across all downloaders; wrap archive errors in `AiModelInstallError`;
     preflight combined `total_bytes` in `download_audiocpp_runtime()`.
3. **Eliminate redundant multi-GB hashing (`core/model_manager.py`, `core/qwen_model_manager.py`, `core/qwen_gguf_models.py`, `core/qwen_gguf_runtime.py`, `core/managed_install.py`):**
   - Adopt `StampLedger` in `ModelManager.inspect()`.
   - Persist freshly verified `FileStamp`s into `install.json` before promotion.
   - Hash fresh offset-0 (`mode="wb"`) streams inline in `stream_to_file()`.
4. **Cross-platform paths, settings, clones & model cleanup (`core/paths.py`, `core/model_manager.py`, `core/detector.py`, `core/settings.py`, `core/voice_profiles.py`, `core/models.py`, `core/ai_models.py`):**
   - Fix `to_extended()` (`os.path.abspath`), `path_to_file_url()` (`safe='/:%'`),
     `sanitize_filename()` (strip before `.` + UTF-8 byte cap), `to_short_path()`
     (`to_extended` + typed `WinDLL`), unconditional `to_extended` for Windows
     `hf_hub_download`, and `%PROGRAMFILES%\NVIDIA Corporation\NVSMI` in
     `_which_nvidia_smi()`.
   - Add Windows lock retry + graceful `OSError` fallback in `load_settings()`
     and lock + atomic staging in `_ensure_legacy_migration()`.
   - Add `threading.RLock`, catch `UnicodeDecodeError` (never quarantine on
     transient `OSError`), and check `soundfile.info()` duration before decoding
     in `CloneStore`.
   - Fix `TTSRequest._check_context()` early `return` so `ref_audio` validation
     runs for `qwen_base_0_6b`.
   - Resolve catalog IDs directly in `remove_asr_model()`/`remove_diarization_model()`
     and share symlink/containment guard across all 3 runtime removers.

### FR-7: Security & Crash-Reporter Hardening (H21)

1. **HTTP clients (`core/llm_client.py`, `core/asr.py`, `core/updates.py`, `core/ai_models.py`, `core/qwen_gguf_runtime.py`):**
   - Validate `scheme in ("http", "https")` and non-empty `netloc` inside `try:`
     in `llm_client.py` and `asr.py`; strip `Authorization` on cross-origin or
     `https→http` redirects via custom `HTTPRedirectHandler`; bound `resp.read()`;
     guard `http_error_detail()`.
   - Validate `https://` + allowlisted GitHub hosts/repo prefix for `html_url`
     and asset URLs in `updates.py`, cap response size at 2 MiB, block
     `https→http` redirects, and catch `UnicodeDecodeError`/`HTTPException` around
     read/decode.
   - Enforce `https://` + redirect scheme guard + in-loop `maximum_bytes` in
     `ai_models._stream_download()` and cap `_PackArchive.extract()` to
     `record.size_bytes + 1`.
2. **Secrets, symlink containment & crash handler (`core/models.py`, `core/voice_profiles.py`, `crash.py`):**
   - Set `repr=False` on `Settings.asr_api_key` and `Settings.llm_api_key`.
   - Restrict `CloneStore._resolve_reference()` strictly to regular non-symlink
     files inside `(root / REFERENCE_DIRNAME).resolve()`.
   - Stop showing a blocking modal dialog from `sys.unraisablehook` (log/record
     only), lock `write_crash_report()`, enforce `0o600` + per-user temp fallback
     + `O_NOFOLLOW`, redact `Bearer`/API keys, and pass macOS `osascript`
     arguments via `on run {t, m}` argv.

### FR-8: Packaging & Release Localization Gate (C7, bead 73r3)

1. **Bundle English catalog (`packaging/vienetts-app.spec`, `.github/workflows/release.yml`):**
   - Add `(str(REPO / "src/vienetts_app/ui/i18n/vienetts_en.qm"), "vienetts_app/ui/i18n")`
     to `datas` in `packaging/vienetts-app.spec`.
   - Add an automated test gate verifying `vienetts-app.spec` includes
     `vienetts_en.qm`, `vienetts_en.ts` has 0 unfinished/obsolete entries, and
     `vienetts_en.qm` exists and loads cleanly (closing `VieNeuTTSApp-73r3`).
   - Add a `dist/` assertion for `vienetts_en.qm` in `.github/workflows/release.yml`.

---

## Non-Functional Requirements

- **Zero new dependencies:** No changes to `pyproject.toml` runtime dependencies
  or `uv.lock`.
- **Cache & schema compatibility:** Existing persisted `settings.json`,
  `project.json` render fingerprints, `state.json`, and `clones.json` remain
  readable with zero breaking migrations.
- **No regressions to QML smoke contracts:** All existing `objectName`s and
  `API_ONLY_PROPERTIES` contracts remain intact.
- **Quantifiable performance improvements:**
  - Transcription Silero VAD runs at most **1 pass** per cue set (down from 3–4).
  - `ModelManager.inspect()` performs **0 full SHA-256 reads** on warm stamped
    startup (down from ~335 MB).
  - `locate_segment()` runs in **O(log N)** without per-tick list allocation.
  - `AppButton` allocates **0** `AppIcon` `Canvas` items when `iconKind == ""`
    and `!busy`; `StudioTab` uses virtualized `ListView`.
  - Zero GUI-thread `nvidia-smi` subprocesses during tab prebuild / Settings load.
- **Quality gates per task:** `ruff check .`, `ruff format --check .`, full
  offscreen `pytest`, and `scripts/update_i18n.sh` (whenever user-facing strings
  change) must pass before each task commit.

---

## Acceptance Criteria

- [x] **AC-1:** `NativeQwenSession.close()` detaches `qt_log_set` via typed NULL
  without raising `ctypes.ArgumentError`, verified with real `_bind_signatures`. (`1773614`)
- [x] **AC-2:** `write_frame()` delivers complete frames >64 KiB across short-writing
  raw `FileIO` streams; `QwenEngine` rejects mismatched `frame.job`, drains
  stale `_inbox` frames, settles abandoned jobs, and queries macOS RAM via
  `hw.memsize`. (`1773614`, `fd39737`)
- [x] **AC-3:** `InferenceWorker.stop(timeout_ms=0)` returns immediately even
  during a blocked Qwen cancel; `FifoJobQueue.wake()` unblocks `take()`
  immediately; `__main__.py` dispatches Qwen hosts before setting BLAS caps or
  importing `numpy`. (`87daf99`)
- [x] **AC-4:** Live streaming plays `<150 ms` utterances, recovers after a
  transient sink error, aligns Float32 reads to 4 bytes, and leaks 0
  `TransportIODevice` children across repeated sessions. (`67e045c`)
- [x] **AC-5:** `_extract_xhtml` preserves separate `<p>` paragraphs inside
  `<section>`/`<div>`; WSOLA `rate < 1.0` produces zero tail-silence truncation;
  multi-part chapter `replace=True` never stamps mixed-engine provenance;
  `_ChapterPersistJob` never swallows `ArtifactWriteError`; multi-part
  audiobook/batch/Studio races and CJK timeline mismatches are covered by tests. (`ac829af`, `b01a8cf`, `42a1b98`)
- [x] **AC-6:** Transcription runs Silero VAD at most once; Windows `render()` /
  `cloneSpeakerFromVideo()` release `PlaybackController` handles first;
  `audiocpp_cli` runs with `cwd=cli.parent` and `whisper-cli` receives absolute
  paths; portrait ASS burn-in swaps dimensions and escapes backslashes cleanly;
  `removeProject()` runs off the GUI thread (`VieNeuTTSApp-c64z` closed). (`a46465f`, `e097c1e`, `653b2f7`, `27c40e7`, `a94787c`)
- [x] **AC-7:** `QThreadPool` is partitioned into interactive/downloads/exports
  pools; `cancelQwen*` holds lane lock until exit; `refreshModelState()` cannot
  wedge `_model_downloading`; `temperatureChanged` emits; GUI-thread
  `nvidia-smi` / WSOLA / WAV I/O / `rmtree` are eliminated; QML Canvas,
  virtualization, `SubtitleVideoPreview`, `QwenInstallCards`, and `AppIconButton`
  fixes pass offscreen smoke tests; `shutdown()` cancels all download lanes
  (`VieNeuTTSApp-02pc` closed). (`153d4af`, `699d39a`, `b3c660e`, `e2c9d9f`, `e8e27c5`, `0a5ad24`, `fbc1614`)
- [x] **AC-8:** Managed CUDA/Qwen installs leak 0 bytes in `active/wheels/` and
  `.staging/shared/speech_tokenizer/speech_tokenizer/` (and prune legacy leaks),
  use unique atomic `StampLedger` temps, support Windows `\\?\` removal, resume
  transient download failures, and skip redundant SHA-256 passes on stamped
  startup/promotion. (`c49538a`, `f45355c`, `bfda07f`, `40921d7`)
- [x] **AC-9:** HTTP clients validate schemes, strip `Authorization` on
  cross-origin/downgrade redirects, and bound response/archive sizes; `Settings`
  masks API keys in `repr`; `CloneStore` blocks symlink/parent escape;
  `sys.unraisablehook` never opens a blocking modal dialog. (`c49538a`, `40921d7`, `e097c1e`, `27c40e7`)
- [x] **AC-10:** `packaging/vienetts-app.spec` and `release.yml` bundle and
  assert `vienetts_en.qm`, and automated i18n catalog checks pass
  (`VieNeuTTSApp-73r3` closed). (`d73b232`, `fbc1614`)

---

## Out of Scope

- Structural decomposition of `src/vienetts_app/ui/controller.py` (9,809 LOC)
  into smaller domain controllers (tracked as a future refactor track).
- The remaining 20 unrelated open Beads issues (e.g., GPL FFmpeg hosting
  `0j8g`/`k47m`, hardware-gated CUDA/Metal model probes, `whnr`, `ndbs`, `kje5`,
  etc., unless touched incidentally — those stay independently tracked).
- Adding new third-party dependencies or changing default engine tuning knobs
  without benchmark evidence.
