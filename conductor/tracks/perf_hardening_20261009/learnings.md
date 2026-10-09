# Track Learnings: perf_hardening_20261009

Patterns, gotchas, and context discovered during implementation.

## Codebase patterns inherited

Source: `conductor/patterns.md` (138 entries) — read it in full before each
task. The ones this track leans on most:

- GUI-thread offload goes through `ui/bg_ops.py` / `ui/chapter_persist.py`:
  a `QRunnable` on the global pool, result marshalled back by a queued
  signal; controllers take an injectable `bg_runner` (`run_sync` in tests)
  and re-validate identity (book id, render generation) before touching
  state.
- Audio hot path: zero-copy chunk views, doubling buffers, offset-based IO
  devices with amortized compaction — never per-chunk full-array copies.
- Artifact-first audio and the bounded (2 s) live transport: no
  duration-sized PCM buffers anywhere.
- Atomic audio writes keep the target extension (`x.part.wav`).
- One worker owns one resident engine; listener controllers attach to it.
- QML: Repeater/ComboBox delegates declare `required property` on the
  delegate ROOT; `var` properties need reassignment to notify; byte counts
  are `qlonglong`; hidden StackLayout tabs have unsettled bindings — activate
  + processEvents before asserting; GUI-object-tree assertions run in a
  subprocess driver (one QGuiApplication per process).
- Animation loops: a ~33 ms Timer that stops once settled.
- Shared-subprocess smoke drivers: run scenario lists per subprocess.

## Audit baseline (2026-10-09, 4-vCPU Linux, warm cache, no model weights)

Use these as the "before" numbers; re-measure on the executing host.

- Qwen `pcm_from_bytes`: 7.2 ms per 0.5 s frame (vs 9 µs `np.frombuffer`).
- hashlib SHA-256: 625 MB/s–1.35 GB/s warm.
- Audiobook chapter-model rebuild: 152 ms @100 ch, 1.37 s @300 ch;
  `load_book` 92 ms @100, 336 ms @300.
- MP3 encode: ~1.33 s per 60 s audio; WAV export 85 ms per 60 s.
- WSOLA: 1.41 s (rate 1.25) / 2.23 s (rate 0.8) per 60 s audio.
- Text metrics: 8 ms/keystroke @20k chars, 78 ms @200k.
- Subtitle rate slider: 18–27 ms per 0.05 step @1,500 cues.
- First tab visit: settings 170–410 ms, audiobook 28–61, cloning 23–57.
- `create_app` warm 378–457 ms (eager Paragraph/Studio) vs 215–277 ms lazy;
  cold 1147 ms vs warm 690 ms (runtime QML compile).
- Warm `import vienetts_app.app` ≈830 ms, numpy ≈310 ms of it.
- Cold first-job text prep: normalize ≈373 ms, phonemize ≈193 ms.

---

<!-- Learnings from implementation will be appended below -->

## [2026-10-09] - Phase 1 Task 1.1: Int16 live-sink conversion
- **Implemented:** `TransportIODevice(int16=...)` converts transport float32 → clipped int16 on read (whole samples only; sizes in output bytes so `buffered_drain_ms` is right). Removed the production-dead push path (`StreamIODevice`, `feed`, `play_buffer`, level drip, `levelReady`/`finished`) and the controller's dead wiring.
- **Files changed:** `ui/stream_playback.py`, `ui/controller.py`, `tests/unit/test_stream_playback.py`, `tests/smoke/test_ui_tabs.py`
- **Commit:** c35558c
- **Learnings:**
  - Context: live levels reach QML from `JobChunk.peak` (worker side), never from the stream player — the player is transport-only.
  - Gotcha: the sink format is negotiated in `_ensure_sink`, AFTER the session's `TransportIODevice` exists — width must be pushed into the device (`set_int16`) and passed to the next session's constructor.
  - Gotcha (host): on linux-aarch64 seven Qwen/GGUF tests fail pre-change (no arm64 host cell in the manifests) — baseline is 7 failed, bead filed. The `3iy` real-sink smoke now passes because it drains via `begin_drain()` + transport.
---

## [2026-10-09] - Phase 1 Task 1.2: Vectorized Qwen PCM codec
- **Implemented:** `pcm_from_bytes` → `np.frombuffer("<f4").astype(float32)` (writable copy); `pcm_to_bytes` → `np.ascontiguousarray("<f4").tobytes()`; numpy imported inside the functions.
- **Files changed:** `core/qwen_protocol.py`, `core/qwen_engine.py`, `tests/unit/test_qwen_protocol.py`
- **Commit:** 540b6db
- **Learnings:**
  - Measured: decode 5.42 ms → 4.8 µs per 0.5 s frame (~1100×), encode 5.39 ms → 3.2 µs.
  - Pattern: keep the old implementation inside the test as a byte-exactness oracle when vectorizing a codec.
---

## [2026-10-09] - Phase 1 Task 1.3: Debounced text metrics
- **Implemented:** `text_metrics()` + `AppController.textMetrics` (QVariantMap); 250 ms debounce Timer in TextTab/DocumentEditorCard; chips bind to cached `metricWords`/`metricSeconds`/`metricMinutes`.
- **Files changed:** `core/text_metrics.py`, `ui/controller.py`, `TextTab.qml`, `DocumentEditorCard.qml`, tests (text_metrics, controller, smoke ui_tabs)
- **Commit:** 838822f
- **Learnings:**
  - Measured: 200k chars — 57.5 ms per keystroke → 0 per keystroke + one 28.1 ms call after the pause.
  - Gotcha: smoke drivers that set editor text and read the chip must now pump ≥250 ms (a `settle_metrics()` helper in the generate_flow scenario).
---

## [2026-10-09] - Phase 1 Task 1.4: Subtitle rate slider applies on release
- **Implemented:** slider writes `rateCap` on release (drag) or after a 300 ms debounce (keys); `_rebuild_for_policy` skips `cuesChanged` when the source cue list is unchanged.
- **Files changed:** `SubtitleCard.qml`, `ui/subtitle_controller.py`, tests (subtitle_controller, smoke ui_tabs)
- **Commit:** 3394826
- **Learnings:**
  - Gotcha: Qt Quick Controls `Slider` sets `pressed` true/false around EVERY arrow-key step — "apply on release" alone still writes per key. Flag key steps with `Keys.onPressed: (e) => { keyStepping = true; e.accepted = false }` (attached Keys handlers run before the C++ keyPressEvent).
  - Gotcha: real pointer input in smoke drivers is swallowed by `modelSetupOverlay` while the fake controller has no model source — set `controller.modelRepo` first (the scrim lesson from patterns.md).
  - Context: policy knobs (rate/gap/offset/merge) never change `project.cues` — offset is applied in the adjusted plan.
---

## [2026-10-09] - Task 1.5: Warm the text pipeline during prewarm
- **Implemented:** `TTSEngine.warm_text_pipeline()` + `_warm_sdk_text_pipeline` seam, `VieNeuProvider.warm_text_pipeline`, and a duck-typed call in `_process_warmup` after `initialize()`
- **Files changed:** core/engine.py, workers/inference_worker.py, tests/unit/test_engine.py, tests/unit/test_inference_worker.py
- **Commit:** 04191a9
- **Learnings:**
  - Gotcha: `Vieneu()` defaults to mode v3turbo, whose text path is `normalize_to_chunks_v3` + `phonemize_text_with_emotions`. The `normalize_to_chunks`/`phonemize_batch` pair used by standard/fast is a different G2P singleton, so warming it would load unused data.
  - Pattern: an engine-level warm hook is a no-op until `_tts` is loaded, so it can never trigger the model load itself.
  - Gotcha: `EngineProviders` checks each provider's `profile` against its key, so a test needs a subclass with `profile = QWEN_BASE` to register a second VieNeuProvider.
---

## [2026-10-09] - Task 1.6: Single-copy transport take
- **Implemented:** `take()` copies once via `memoryview(...)[a:b].tobytes()` inside a `with` block
- **Files changed:** core/pcm_transport.py, tests/unit/test_pcm_transport.py
- **Commit:** ecabad9
- **Learnings:**
  - Gotcha: `bytes(bytearray[a:b])` copies twice. Release a memoryview over a bytearray (`with memoryview(buf) as v`) before `del buf[:n]`, or the resize raises BufferError.
  - Pattern: tracemalloc peak < 1.5× payload is a deterministic way to pin a "single copy" contract in a unit test.
---

## [2026-10-09] - Task 2.1: Audiobook state-read memo and single-read model rebuild
- **Implemented:** `_state_memo` keyed on (mtime_ns, size, st_ino); public read-only `read_state()`; `state=` kwarg on chapter_audio_paths/has_chapter_audio/segment_ready_count/load_segment_plan; writers use `_parse_state` + `_write_state` (pop the memo in finally)
- **Files changed:** core/audiobook.py, ui/audiobook_controller.py, tests/unit/test_audiobook.py, tests/unit/test_audiobook_controller.py
- **Commit:** 6177622
- **Learnings:**
  - Pattern: memoize JSON reads on (mtime_ns, size, inode). An atomic temp-file replace always changes the inode, so writes by other processes or library instances within one mtime tick are still caught.
  - Gotcha: a shared memo must never be handed to a mutator. Writers parse a private copy, so a failed write cannot leave unwritten changes in the cache.
  - Gotcha: an existing test spied on `has_chapter_audio` as a proxy for per-chapter stats. Spy on the call the rebuild actually makes (`segment_ready_count`).
---

## [2026-10-09] - Task 2.2: Load books off the GUI thread
- **Implemented:** `_open_book` (generation-guarded bg load + `_apply_opened_book`), async `refreshChapters` (its own generation; also dropped if an open happened), `loading`/`loadingBookId` properties, a shelf row "Đang mở…" label, and `epubOpened` chained after the load
- **Files changed:** ui/audiobook_controller.py, ui/qml/AudiobookTab.qml, tests/unit/test_audiobook_controller.py, tests/smoke/test_ui_tabs.py
- **Commit:** 8c55a94
- **Learnings:**
  - Pattern: `DeferredRunner` (queue work, then `run(i)` in any order) is the way to test stale-completion logic for bg_runner seams. `run_sync` cannot reorder.
  - Gotcha: give refresh and open separate generation counters. With one shared counter, a refresh fired while an open is pending would cancel the open.
  - Gotcha: tests that build an AudiobookController without `bg_runner` fall back to the real thread pool, so they need `bg_runner=run_sync` once a slot goes async.
---

## [2026-10-09] - Task 2.3: Background audiobook export
- **Implemented:** `_start_export` (validate on the GUI thread, then load once and copy on the pool), the `exporting` property, `exportProgress` relayed through the private `_exportStep` signal, `exportFinished(count, error)`, refusal while an export runs, and `export_chapter(..., book=)`
- **Files changed:** ui/audiobook_controller.py, core/audiobook.py, ui/qml/AudiobookTab.qml, tests/unit/test_audiobook_controller.py, tests/unit/test_audiobook.py, tests/smoke/test_ui_tabs.py
- **Commit:** 03ec5bc
- **Learnings:**
  - Pattern: report progress from a pool thread through a signal defined on the GUI-thread QObject and connected to a bound `@Slot` method on that object. AutoConnection queues it across threads and runs it directly under run_sync. A real-pool unit test asserts `threading.current_thread() is main_thread()` in the handler.
  - Pattern: emit `progress(0, total)` on accept so the UI never shows "0/0" before the first item lands.
---

## [2026-10-09] - Task 2.4: Ordered executor for subtitle dub rendering
- **Implemented:** `OrderedExecutor` (QThreadPool max 1 + one relay signal) and `SyncOrderedExecutor` in ui/bg_ops.py. SubtitleController gains the `unit_executor` seam, `_place_unit` on the worker, a queued finish and abort, `_render_generation` guards, and `_fail_render` cancels the overlapping job.
- **Files changed:** ui/bg_ops.py, ui/subtitle_controller.py, tests/unit/test_bg_ops.py, tests/unit/test_subtitle_controller.py
- **Commit:** 6ce09b0
- **Learnings:**
  - Pattern: route the abort of a stateful writer through the same ordered worker as its writes, and bump the generation first. Queued writes become no-ops and the abort never races a write in flight.
  - Pattern: one relay `Signal(object)` carrying `(callback, payload)` per executor, instead of a bridge QObject per job. `run_on_thread_pool` creates a never-deleted bridge per call, which is fine for one-shots but leaks over thousands of units.
  - Gotcha: once synthesis overlaps placement, a placement failure can happen while the next job is in flight. The fail path must cancel `_job_id`, which the old serial code never needed.
  - Gotcha: "never abort a finished renderer" needs an explicit `promoted` flag, because abort is now queued rather than inline.
---

## [2026-10-09] - Task 2.5: Remaining small GUI-thread offloads
- **Implemented:** `openChapterInStudio` loads the book and chapter WAV on the pool, guarded by `_studio_open_generation` (openInStudio bumps it too). `studioPreviewClip` writes the clip WAV and computes its envelope on the pool, guarded by `_studio_clip_preview_generation` plus a `_studio_seq` snapshot; stopReplay bumps the generation, and playback starts in the new `_play_studio_clip`. `_complete_audition` copies to the cache on the pool via a `.part.wav` file plus rename, guarded by `_audition_finish_token`, which is cleared on stop/reset. Also fixed the missing `return` after an invalid artifact. A CUDA readiness flip now calls `_reprobe_torch_availability()`, which keeps the cached answer, probes on the pool by generation, and emits only on change.
- **Files changed:** ui/controller.py, tests/unit/test_controller.py, tests/unit/test_studio_controller.py
- **Commit:** 5de5be2
- **Learnings:**
  - Gotcha: soundfile infers the container from the suffix, so a temp name must end in `.wav` (`x.part.wav`, not `x.wav.part`).
  - Gotcha: adding a new `_run_bg` job to an existing flow shifts index-based `_DeferredBackground.complete()` calls in older tests. The torch re-probe therefore schedules only when a cached value exists; never-probed still just notifies.
  - Pattern: snapshot GUI-thread state (managed CUDA readiness) before submitting, so the pool closure only runs the slow probe and never reads controller state.
  - Measured: clip preview of a 120 s clip, 149 ms in the slot → ~0 ms. Chapter read (10 min, warm cache) 21 ms and audition copy (6 s) 9 ms moved off-thread.
---

## [2026-10-09] - Task 3.1: Stat-stamp verification contract
- **Implemented:** `FileStamp`, `file_stamp(path)` returning `(size, mtime_ns, inode)` or None, and `file_matches_stamped(path, size, sha, stamp)` returning the stamp to persist or None. Malformed or JSON-list stamps are coerced, and a zero inode on either side compares size + mtime only.
- **Files changed:** core/managed_install.py, tests/unit/test_managed_install.py
- **Commit:** e9c46f9
- **Learnings:**
  - Pattern: return the stamp (not a bool) from the verifier so callers persist exactly what was verified; None keeps the "re-download" meaning of False.
  - Gotcha: a stamp must also match the manifest size before it is trusted; otherwise a manifest bump with an unchanged file would skip the hash.
  - Measured: 512 MB full hash 373 ms vs stamped check 6.2 µs.
---

## [2026-10-09] - Task 3.2: Stamps in Qwen model/runtime installers
- **Implemented:** `STAMPS_KEY`, `split_install_record`, `require_verify_mode` and `StampLedger` (matches / persist via atomic tmp + os.replace) in managed_install. `QwenModelManager.inspect(mode=)`, `QwenGgufModelManager.status(mode=)` and `QwenGgufRuntimeManager.status(mode=)` compare the record without stamps, verify through the ledger and persist fresh stamps when ready. Stamp keys are `files/<path>` and `shared/<path>`.
- **Files changed:** core/managed_install.py, core/qwen_model_manager.py, core/qwen_gguf_models.py, core/qwen_gguf_runtime.py, plus their unit tests
- **Commit:** 6f85024
- **Learnings:**
  - Gotcha: every manager compared `metadata != self._metadata()` exactly, so any new install.json key must be split off before the comparison or every existing install reads as "does not match the manifest".
  - Pattern: no installer changes were needed for "install writes stamps". Each `_promote_staging` already runs inspect/status inside `promoted_install`, and that inspect hashes once and persists. Rename-based promotion keeps inode and mtime, so the stamps stay valid afterwards.
  - Follow-up candidate: install still hashes twice (staging verify + post-promotion inspect). Handing the staging ledger's stamps into install.json would halve install verification time.
  - Measured: 400 MB profile inspect, full 278 ms → stamped 0.2 ms.
---

## [2026-10-09] - Task 3.3: Off-thread Qwen engine preparation + "Verify files"
- **Implemented:** `_PreparingWorker` stand-in, installed as `self._worker` while `_start_qwen_engine_preparation` runs the inspection and build planning on the pool. Submissions queue on it. On success the real engine and worker start and the queue is resubmitted. On failure `fail_all` terminalizes every queued job with the actionable error. A profile switch or shutdown drops the stand-in, so a stale result is ignored. The `preparingEngine` property drives a "Đang chuẩn bị mô hình…" busy hint. `verifyQwenFiles()` runs `inspect(mode="full")` off-thread and reports through `qwenVerifyMessage` / `qwenVerifyFinished(ok, msg)`; the button and result label live on the QwenInstallCards runtime card.
- **Files changed:** ui/controller.py, core/qwen_model_manager.py, core/qwen_gguf_models.py, core/qwen_gguf_runtime.py, QwenInstallCards.qml, TextTab.qml, SynthesisBar.qml, i18n catalog, unit + smoke tests
- **Commit:** 42e938c
- **Learnings:**
  - Pattern: a stand-in worker with the InferenceWorker surface (submit / cancel_job / cancel_owner / stop / has_pending_work) lets every existing submit path queue during async preparation without new branches.
  - Gotcha: under the inline `run_sync` runner, a preparation failure with nothing queued must re-raise synchronously, or the existing synchronous error paths (and their tests) silently change.
  - Gotcha: device resolution must stay synchronous. "Still resolving its compute device" is a submit-time refusal, not a preparation failure.
  - Pattern: the `StampLedger` now persists on failure paths too, so a full-verify finding (dropped stamp) sticks for later stamped inspects instead of being re-trusted.
  - Gotcha: earlier tasks added qsTr/self.tr strings without running scripts/update_i18n.sh. test_i18n only checks the catalog itself, so the missing entries went unnoticed. Run the script whenever user-facing strings change.
---

## [2026-10-09] - Task 3.4: Qwen host hygiene
- **Implemented:** `AcceleratorReleasePolicy` in workers/qwen_host.py (every `RELEASE_EVERY_JOBS`=8 jobs, or when RSS grows `RELEASE_GROWTH_BYTES`=512 MiB past the last release/load; rebaselined after a load). `serve(release_every_jobs=, release_growth_bytes=, footprint=)` seams. `configure_torch_threads()` now runs before the loader. `_inference_mode()` wraps generate and clone-prompt building; it is looked up via `sys.modules`, so torch-free hosts stay torch-free. `host_footprint` reads `/proc/<pid>/statm` on Linux (`PROC_ROOT` seam). The GGUF `_source_clip` keeps `_clip_digests[path] = ((size, mtime_ns, ino, dev), sha)`.
- **Files changed:** workers/qwen_host.py, workers/qwen_gguf_host.py, core/qwen_engine.py, plus their unit tests
- **Commit:** 541d079
- **Learnings:**
  - Gotcha (racy stamps): a file rewritten within the filesystem's timestamp granularity can keep its size, mtime and inode (Linux coarse clock about 4 ms; FAT 2 s). The existing voice-ref invalidation test rewrites a clip immediately. Trust a stat stamp only once `now - mtime >= 2 s`, as git does for racily-clean index entries. Tests backdate clips with `os.utime`.
  - Gotcha: `torch.set_num_interop_threads` is refused after any parallel work. Calling it after the load (as before) was a silent no-op on real runtimes.
  - Measured: gc.collect over ~2M objects 190 ms per job → amortized ÷8; ps spawn 13.9 ms → statm 0.034 ms; 5.8 MB clip read+hash 4.5 ms → stat 3 µs.
---

## [2026-10-09] - Task 4.1: Non-blocking transport offer + artifact tail reader
- **Implemented:** `BoundedPcmTransport.offer(payload) -> int` (non-blocking, raises TransportClosed when closed). `IncrementalArtifactWriter.frames_written` and `open_reader()`. `WavPcmReader(path, frames_available=)` with `.read(start, end)`. `wav_data_offset(path)`.
- **Files changed:** core/pcm_transport.py, core/artifacts.py, tests/unit/test_pcm_transport.py, tests/unit/test_artifacts.py
- **Commit:** c36ea3b
- **Learnings:**
  - Gotcha: a libsndfile float WAV does NOT start its data at byte 44. It writes `fact` and `PEAK` chunks first, so walk the RIFF chunks to find `data`. The data chunk's size field stays a placeholder until close.
  - Pattern: libsndfile writes through the raw fd with no user-space buffering. Bytes from `SoundFile.write` are visible to another reader as soon as the call returns, so clamping reads to a counter that advances after `write` returns is enough ("flushed data only") without fsync.
  - Gotcha: open the part file per read instead of holding a handle. A held handle on Windows makes the writer's `os.replace` promotion fail (PermissionError → alternate-name fallback).
  - Measured: offer on a full transport 3.2 µs; 20 ms part read 13.6 µs.
---
