# Track Learnings: audit_hardening_perf_20261127

Patterns, gotchas, and context discovered during implementation.

## Codebase Patterns (Inherited)

📚 **261 patterns** inherited from `conductor/patterns.md`, plus targeted learnings
from archived hardening tracks `perf_hardening_20261009`,
`dub_asr_diar_hardening_20261029`, and `audiobook_booksreader_export_20261126`:

- **Quality gates per task:** `.venv/bin/ruff check .`,
  `.venv/bin/ruff format --check .`, and
  `QT_QPA_PLATFORM=offscreen QT_AUDIO_BACKEND=ffmpeg .venv/bin/pytest` (unset
  `PYTHONDONTWRITEBYTECODE`). Run `scripts/update_i18n.sh` whenever user-facing
  strings change (0 unfinished). Commit locally with conventional prefix +
  `git notes add -m "..."`; never `git push`, `git pull`, `git fetch`, or
  `bd dolt push` per task.
- **Pre-existing host baselines:** `test_stream_playback.py::TestRealQtSmoke::test_real_qaudiosink_offscreen_smoke`
  (`VieNeuTTSApp-3iy`) fails on device-less hosts when sharing a run; on
  linux-aarch64, x64-cell Qwen/audio.cpp tests are known baselines (`4jwz`/`x898`).
  Re-run any suspected flake in isolation (`pytest <nodeid> -q`) before calling
  it a regression; prefer `-n 4` on a loaded box.
- **Parallel sub-agent discipline:** Sub-agents own disjoint files per
  `<!-- files: ... -->` and run only scoped tests; serialize git commits and
  full-suite gates through the orchestrator to avoid `.git/index.lock` races.
- **Atomic file & audio writes:** Keep the target extension (`x.part.wav`,
  `x.part.mp3`, `x.part.epub`, `x.part.m4b`) because `soundfile`/`ffmpeg` infer
  container formats from the suffix; use short UUID-only temp names to stay
  under 255-byte `NAME_MAX`, promote via `os.replace` with bounded Windows
  `PermissionError` retries, and never monkeypatch global `time.sleep` in tests
  that spawn subprocesses (`subprocess.Popen.wait` polls via `time.sleep`).
- **Native CLI & Windows child discipline:** Spawn CLIs via `core/processes.py`
  (`run_cli`/`spawn_cli`, `CREATE_NO_WINDOW`, UTF-8 `errors="replace"`,
  `cancel_check`) and `ai_models.cli_command()` (Python shebang test-stub
  dispatch on Windows). Never use `communicate()` when grandchildren may inherit
  pipes; use bounded `wait()` + reader thread `join()`.
- **Managed installs & frozen bundles:** Pin `size_bytes` + `sha256`; keep
  non-Python runtime data files (`core/*_manifests.json`, `ui/i18n/vienetts_en.qm`,
  `certifi`) explicitly listed in `packaging/vienetts-app.spec` `datas` because
  `collect_submodules` only collects `.py`.
- **GUI-thread offload & generation guards:** Controllers take injectable
  `bg_runner` (`run_sync` or `DeferredRunner` in tests). Give each async op its
  own generation counter; never share one counter across unrelated ops. Wait on
  queued Qt signals via `processEvents()` in tests.
- **Settings & fingerprints:** Validate in `core/models.py` (reject `bool` before
  `int`) and clamp per-field in `core/settings.py`; list every setting key in
  `AppController._set_setting`'s signal table. Keep `render_fingerprint` payload
  evolution sparse so existing persisted projects retain their digests.
- **QML & smoke contracts:** Preserve all `objectName`s and `API_ONLY_PROPERTIES`;
  use `DictListModel` for long/updating lists; `QML` context fakes need real
  NOTIFY `@Property` descriptors; consolidate new smoke assertions into existing
  scenario groups; activate hidden `StackLayout` tabs + `processEvents()` before
  asserting visibility; use `DontUseNativeDialog` when testing file dialogs.
- **Test diet:** Merge same-function scenarios into existing test modules rather
  than adding repetitive micro-tests; keep regression guards, error/cancel/race
  tests, and cross-file invariants.

---

<!-- Learnings from implementation will be appended below -->
<!-- Format for each entry:

## [YYYY-MM-DD HH:MM] - Phase N Task M: <task_name>
- **Implemented:** <brief description of what was done>
- **Files changed:** <list of files modified/created>
- **Commit:** <sha_7chars>
- **Learnings:**
  - Patterns: <reusable patterns discovered>
  - Gotchas: <things to watch out for>
  - Context: <useful context for future>
---

-->

## [2026-11-27 05:37] - Phase 1 Tasks 1.1–1.4: Native ABI, Qwen IPC/Engine, Workers & Live Playback
- **Implemented:** Hardened GGUF ctypes callback detachment (`_QtLogCb(0)`), NULL/empty guards, short-write-safe IPC `write_frame()`, Qwen engine job-frame filtering + active-job settlement + snapshotted cancel generation + macOS `hw.memsize` RAM + buffered stderr/unlocked RSS, host reader error handling + bounded content-hashed prompt LRU + `_source_clip` pre-resample validation + local typed `WinDLL`, `InferenceWorker` deduplicated async provider cancel + atomic `FifoJobQueue.take(on_take=...)` + `wake()` + `contextlib.closing(chunk_stream)`, `_SDK_PATCH_LOCK` (`RLock`) + per-entry `voices.json` isolation, early `--qwen-host`/`--qwen-gguf-host`/`--version` dispatch in `__main__.py`, `<150 ms` closed prebuffer + 4-byte aligned Float32 transport + sink recovery + `TransportIODevice` cleanup + `PlaybackController` parenting/`EndOfMedia` handle release.
- **Commits:** `1773614` (1.1), `fd39737` (1.2), `87daf99` (1.3), `67e045c` (1.4)
- **Learnings:**
  - Patterns: Test real `_bind_signatures` ctypes marshalling with `ctypes.CFUNCTYPE` trampolines (using `c_void_p` + `addressof(create_string_buffer(...))` for string returns to avoid callback leak warnings). Guard `_SDK_PATCH_LOCK` with `threading.RLock()` because `TTSEngine._ensure()` nests `_default_factory()` inside `_ort_session_tuning()`.
  - Gotchas: `ProtocolError` subclasses `ValueError`, so `except ProtocolError` must precede `except (OSError, ValueError)` in host reader threads; `_prefetch_next()` and `cancel()` must not drain `_inbox` (only pre-send when idle and generator `finally:`); `PlaybackController._on_media_status_changed()` must emit `finished` first and guard `setSource(QUrl())` on unchanged `_play_id` so synchronous auto-advance isn't clobbered while releasing handles before `on_released`.
---

## [2026-11-27 05:37] - Phase 2 Tasks 2.1–2.5: Managed Installs, Disk-Leak Cleanup, Hash Speed, Security & Packaging
- **Implemented:** Bundled `vienetts_en.qm` in `vienetts-app.spec` + `release.yml` + `tests/unit/test_packaging_i18n.py` (closed `VieNeuTTSApp-73r3`); hardened `paths.py`/`detector.py`/`models.py`/`settings.py`/`voice_profiles.py`/`updates.py`/`crash.py`; added `replace_retrying()`, unique UUID `StampLedger.persist()`, strict Windows `safe_remove()`, inline SHA-256 on fresh offset-0 `stream_to_file()`/`download_wheel_archive()`, and `StampLedger` in `ModelManager`; eliminated CUDA/Qwen `active/wheels/` & nested `speech_tokenizer/speech_tokenizer/` leaks with proactive pruning, resilient backoff resume, mismatch unlink, `_PackArchive` byte caps, and pre-promotion stamps; hardened `ai_models.py` HTTPS/redirect/size-cap/archive/removal + injectable `driver_cuda_version` & `cancel_check`.
- **Commits:** `d73b232` (2.1), `c49538a` (2.2), `f45355c` (2.3), `bfda07f` (2.4), `40921d7` (2.5)
- **Learnings:**
  - Patterns: Content-parity validation of all 1,301 `vienetts_en.ts` messages against `QTranslator` on `vienetts_en.qm` runs in ~12 ms and beats fragile git-checkout mtime checks; pre-promotion `StampLedger` recording survives same-volume `os.replace` `(size, mtime_ns, inode)` so fresh HTTP 200 install + post-promote inspect does 0 extra `sha256_of()` passes.
  - Gotchas: On POSIX hosts when tests monkeypatch `os.name = "nt"`, `Path(...)` builds `WindowsPath` — guard `_writable_path()` on `str(extended).startswith("\\\\?\\")` and avoid re-wrapping `isinstance(result, Path)`. In `ai_models.py`, keep `available_audiocpp_variants()` zero-arg compatible (`_UNSET` sentinel) because existing tests monkeypatch zero-arg lambdas. On `release.yml` macOS legs (`bash -eo pipefail`), check `dist/VieNeuTTS.app` without `find | head -n 1` (SIGPIPE 141).
---

## [2026-11-27 06:11] - Phase 3 Tasks 3.1–3.3: Audio DSP, EPUB, Audiobook, Exports, Batch & Studio Core
- **Implemented:** WSOLA slow-rate tail continuation via in-bounds suffix correlation, header-only `wav_duration_seconds()`, block-wise `trim_speech_edges()`, UUID-only temp filenames in `audio`/`epub_export`/`m4b_export`, empty `dest_dir` validation, recursive `_extract_xhtml` + corrupt archive wrapping + `posixpath.normpath` + `include_cover` flag in `epub.py`; character lock-step `map_segment_offsets` + pre-indexed O(log N) `bisect` `locate_segment`, multi-part chapters + `clear_stretch_cache()` + block-wise `SilenceTrimOp` + zero-copy float32/min-max peaks + `np.isfinite` `envelope_for` in `studio.py`; hardened multi-part `audiobook.py`/`chapter_persist.py`/`audiobook_controller.py`/`batch_controller.py` persistence, locks, off-thread saves/removal, timeline matching, and `uid`-tracked batch queue.
- **Commits:** `ac829af` (3.1), `b01a8cf` (3.2), `42a1b98` (3.3)
- **Learnings:**
  - Patterns: For WSOLA `rate < 1.0`, correlate against the remaining in-bounds suffix `mono[target_pos:n_in]` (zero-padded by `rfft(target, self._span)`) rather than shifting `target_pos` backward, preserving 92–99% tail RMS without phase cancellation. Check `is_empty_path` both before and after `normalize_local_path` to catch quoted-empty `'""'` and bare `"file://"`.
  - Gotchas: `trim_speech_edges` block scan cannot use `chunk.max() > threshold` because `np.nan` propagates through `.max()`; per-block `np.abs(chunk) > threshold` on 16k-sample chunks is bounded and NaN-safe. Guard redundant `"stopped" -> "stopped"` transitions in `_on_player_state_changed()` before enqueuing background `_save_progress`.
---

## [2026-11-27 06:11] - Phase 4 Wave A Tasks 4.1–4.3: Processes, ONNX Audio, LLM HTTP & Subtitles/Video Dub
- **Implemented:** Byte-capped tail buffers + post-spawn cleanup + POSIX process-group / Windows Job Object grandchild teardown in `processes.py`; 30 s pre-resample `SpeakerEmbedder.embed()` cap + stale eviction + `release_onnx_sessions()` in `onnx_audio.py`; `validate_http_url` + `SafeRedirectHandler` (`Authorization` strip on cross-origin/downgrade) + 8 MiB response cap + guarded `http_error_detail` in `llm_client.py`; explicit `attempted_batches` + hardened `_parse_reply` in `llm_refine.py`; SRT `format_srt` sanitization & final digit-only cue retention in `subtitles.py`; ffprobe JSON Display Matrix rotation swap & `\\\u2060` ASS backslash escaping in `video_dub.py`.
- **Commits:** `a46465f` (4.1), `e097c1e` (4.2), `653b2f7` (4.3)
- **Learnings:**
  - Patterns: Record `pgid = proc.pid` at spawn time and poll `os.killpg(pgid, 0)` so orphaned `SIGTERM`-ignoring grandchildren still escalate to `SIGKILL` after the direct child exits. Use `stream=width,height,pix_fmt:stream_tags=rotate:stream_side_data=side_data_type,rotation` in `ffprobe` to keep rotation probing header-only without packet decoding.
  - Gotchas: Stdlib `HTTPRedirectHandler` binds `http_error_301/303/307/308` to `http_error_302` at class definition time — rebind all 4 in `SafeRedirectHandler`. On LP64 Linux, `wintypes.DWORD` is 8-byte `c_ulong`; use fixed-width `c_uint32`/`c_int64`/`c_uint64` for Windows ABI struct layouts in cross-platform tests.
---

## [2026-11-27 06:49] - Phase 4 Wave B Tasks 4.4–4.5: Single-Pass VAD, ASR/Diarization & Subtitle Controller
- **Implemented:** Single-pass Silero VAD across `transcribe_pipeline.py`/`asr.py`/`diarization.py`/`subtitle_controller.py` (`already_refined=True`); `cwd=cli.parent` + `turns_temp` inside `try:` + `.absolute()` whisper-cli paths; Whisper API `llm_client` URL/redirect/cap/backoff + null/non-numeric start/end guards + single `sf.SoundFile` chunking + resolved backend/model cache key + CJK token splitting + `_FASTER_WHISPER_CACHE` generation/cancel; per-cue Silero error isolation + empty-span non-anchor + O(N log N) sweeps + single `sf.SoundFile` acoustic reads + `extract_speaker_reference_wav` cancel forwarding; playback stop + `_replace_retrying` before subtitle/ref writes, `_sample_generation` + `clearVideo` gating, `ProgressRelay` export progress, metadata-preserving `_apply_speaker_voice`, FIFO deferred invalidation queue, `SubtitleStudioMeta.render_id` validation + single-handle splice, and off-thread `removeProject()` + zero-syscall GUI `UnitClipCache.contains()` (closed `VieNeuTTSApp-c64z`).
- **Commits:** `27c40e7` (4.4), `a94787c` (4.5)
- **Learnings:**
  - Patterns: Fubini coordinate sweep (`lc * rc * dt`) matches nested double-sum interval overlaps bit-for-bit in O(C log C); check `sys.modules` before `importlib.util.find_spec()` because test fakes with `__spec__ = None` raise `ValueError`.
  - Gotchas: Guard `SubtitleStudioMeta.render_id` validation so stale Studio snapshots cannot splice into a re-rendered `track.wav`; keep `UnitClipCache.contains(touch_disk=False)` on the GUI thread while letting the placement worker's `get()` refresh on-disk mtime.
---

## [2026-11-27 06:49] - Phase 5 Tasks 5.1–5.2: Pool Partitioning, Teardown & AppController Hardening
- **Implemented:** Partitioned `QThreadPool` lanes (`interactive`/`downloads`/`exports`) drained under one budget + module-level `_OrderedJob` in `bg_ops.py`; tracked startup `QTimer`s + `TabPrebuild.cancel()` + full `playback`/`batch`/`subtitle`/`audiobook`/`controller`/`onnx_audio` teardown in `app.py`; `AppController` Qwen `"cancelling"` lane lock, split official model inspect/download generations, `temperatureChanged` + atomic `_set_setting` `OSError` rollback, GUI-thread state snapshots + `_run_bg` `resolveTorchAvailabilityAsync`, Studio save/export `_studio_open_generation` gates + `clear_stretch_cache()`, `_replay_player_clock` playhead jitter fix, permanent `_shutting_down` latch, and quit cancellation for all 4 ASR/diarization downloaders + retired Qwen host close (closed `VieNeuTTSApp-02pc`).
- **Commits:** `153d4af` (5.1), `699d39a` (5.2)
- **Learnings:**
  - Patterns: Emit `relay.emit(...)` directly inside `except`/`else` in `QRunnable.run()` so `exc.__traceback__` never creates a closure reference cycle; track `_connected` before disconnecting PySide6 signals to avoid `RuntimeWarning` on double-disconnect.
  - Gotchas: Re-raise `AiModelDownloadCancelled` explicitly before `except Exception:` around runtime installs in `downloadAsrModel()`/`downloadDiarizationModel()` so quit cancel never falls through to downloading the model; track `_replay_player_clock` across pause/resume/seek so the fallback timer doesn't re-arm after real `positionChanged` takes over.
---

## [2026-11-27 07:15] - Phase 5 Task 5.3: AppController GUI-Thread Offloads, Pool Routing & `qwenModelsModel`
- **Implemented:** Wired cached async `CudaDriverProbe` version into `available_audiocpp_variants`/`audiocpp_unavailable_reason` (0 GUI-thread `nvidia-smi`); offloaded `openInStudio`/`openChapterInStudio`/`openSubtitleInStudio`, `studioSetClipSpeed`, `_maybe_splice_regen`, and `_complete_voice_op` onto `POOL_INTERACTIVE` with generation guards; cached async snapshots for `profileClones`, `asr*`, `diarization*`, `audioModels`, and `ffmpegRuntimeStatus`; routed all downloads to `POOL_DOWNLOADS` and exports to `POOL_EXPORTS` via `_submit_bg`; exposed 15-role `DictListModel` `qwenModelsModel` synced in place on progress ticks without resets.
- **Commit:** `b3c660e`
- **Learnings:**
  - Patterns: Combine Studio WAV load + `render_overview()` into a single `POOL_INTERACTIVE` job to keep `studio_async` single-job invariants and eliminate a second GUI round-trip; track a local `inline` flag around `_submit_bg` so synchronous `run_sync` cache population inside passive getters doesn't emit redundant notify signals.
  - Gotchas: `controller._unwrap_bg_result()` unwraps any 2-tuple `(bool, payload)` — never place a `bool` first in a 2-tuple returned from background `work()`; drop pre-shutdown `_complete_voice_op` callbacks via `generation != self._voice_op_generation` (already bumped in `shutdown()`) rather than `self._shutting_down` so E2E flows reusing a controller post-`shutdown()` (`clone_settings_e2e`) don't wedge `busy = True`.
---

## [2026-11-27 07:47] - Phase 6 Tasks 6.1–6.4: QML Rendering, Virtualization, UX Fixes & Full Verification
- **Implemented:** Guarded `AppIcon` `requestPaint`/`onPaint` with `_dirty` flag; `Loader`-gated `AppButton` `normalIcon`/`busySpinner` (0 Canvases when plain & not busy); removed shadowed `tooltipText`/`ToolTip` in `AppIconButton` restoring `disabledReason`; bound `AppProgressBar` `XAnimator.running` via declarative `Binding` (fixing initially-hidden ancestor startup animation bug); filtered `VoicePicker.filteredModel` without `height: 0` gaps; debounced `CreateInspector` slider writes; virtualized `StudioTab` clip `ListView` over `studioClipModel` and dropped `controller.studioClips` QML mirror; rendered `StudioTimeline` on one Canvas with 0 playhead repaints; bound `QwenInstallCards` to `controller.qwenModelsModel` preserving Cancel C++ pointer across progress ticks; fixed `SubtitleVideoPreview` scrub `Binding`, lazy `MediaPlayer` `Loader`, tab-leave pause, `Component.onDestruction` release, 300 ms/400 ms dub-sync cooldown, and split `_cuesRevision`/`_activeCueRevision`; bounded/scrolled `SubtitlesTab` `projectsDialog` with footer Close button; debounced focused `VoicesTab` search; migrated `AudiobookTab`/`CloningTab` progress bars to `AppProgressBar`; added full offscreen QML smoke assertions and synced `vienetts_en.ts`/`.qm` (1303/1303 finished, 0 unfinished).
- **Commits:** `e2c9d9f` (6.1), `e8e27c5` (6.2), `0a5ad24` (6.3), `fbc1614` (6.4)
- **Learnings:**
  - Patterns: In Qt Quick Controls 2, an `XAnimator on x { running: root.visible && root.indeterminate }` instantiated inside an initially-hidden ancestor evaluates `running: true` before the ancestor's `visible: false` propagates during component completion — initialize `running: false` and drive it via a declarative `Binding { target: indetAnimator; property: "running"; value: root.visible && root.indeterminate }`.
  - Gotchas: `AppController.studioClipCount` notifies on `studioProjectChanged` which can evaluate in QML before Python's `_sync_studio_lists` populates `studioClipModel` — bind QML clip counts directly to `controller.studioClipModel.count`. Under `QT_QPA_PLATFORM=offscreen`, `window.requestActivate()` is required before `forceActiveFocus()` for `activeFocus` to become `true`.
---
