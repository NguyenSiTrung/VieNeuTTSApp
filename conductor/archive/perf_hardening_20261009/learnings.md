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

## [2026-10-09] - Task 4.2: Decouple the worker writer from live playback
- **Implemented:** `BoundedPcmTransport` gained a file-backed backlog: `attach_source(read(start, end)->bytes)`, `publish()` (never waits; offers only when nothing is backlogged), `refill()` (whole frames, retries an unavailable source), `pending_bytes()`, `drop_backlog()`, and a graceful `close` deferred until the backlog is delivered. The worker publishes after `writer.append`; its source is the part reader, switched to the final artifact after `finalize()`. The stream-playback 20 ms timer calls `refill()`; `buffered_drain_ms` adds the backlog; discard mode drops the backlog. The controller re-arms the drain timer while the estimate shrinks.
- **Files changed:** core/pcm_transport.py, workers/inference_worker.py, ui/stream_playback.py, ui/controller.py, plus tests (pcm_transport, inference_worker, stream_playback, controller)
- **Commit:** f53598d
- **Learnings:**
  - Pattern: putting the backlog cursor INSIDE the transport (rather than a separate feed object) kept `SynthesisJob.live_transport` and every controller call site unchanged. Lock order is `_feed_lock` → `_condition`, and `take` needs only `_condition`.
  - Gotcha: once the producer is no longer paced, "job completed" no longer means "audio nearly played". Anything keyed to completion (`begin_drain` closing the transport, the drain timer) must account for the backlog. Otherwise the tail is cut.
  - Gotcha: a test that relied on the producer blocking in `put` (cancel-while-full) needed a gated engine to keep the job in flight.
  - Measured: 6 s of audio with a real-time sink, artifact done 3.82 s → 8.3 ms.
---

## [2026-10-09] - Task 4.3: Qwen live progressive segmentation
- **Implemented:** `PROGRESSIVE_FIRST_CHARS`=150 and `PROGRESSIVE_SECOND_CHARS`=250. `_pack_units(..., caps=)` gives a per-segment-index cap and, with caps, cuts at a clause mark (`_CLAUSE_END_RE`) before falling back to a space. `split_text_for_profile(..., progressive=)`. The worker sets progressive for live jobs whose profile runtime is `qwen_host`.
- **Files changed:** core/text_segmentation.py, workers/inference_worker.py, tests/unit/test_text_segmentation.py, tests/unit/test_inference_worker.py
- **Commit:** af0c409
- **Learnings:**
  - Pattern: a per-segment-index cap (`cap()` read from `len(segments)`) slots into the existing greedy packer, so the non-progressive path stays byte-identical (`caps=()`).
  - Measured: first segment 503 → 143 chars (en), 512 → 136 (zh).
---

## [2026-10-09] - Task 4.4: Qwen live next-segment prefetch
- **Implemented:** `QwenEngine.infer_stream_prefetched` (+ `_LiveSequence`) and the provider/worker routing for live multi-segment jobs
- **Commit:** ac5865e
- **Learnings:**
  - Pattern: one-ahead prefetch = "send N+1 when N settled ok AND the consumer is on N"; whichever event comes second sends it (reader thread on the terminal, consumer on moving forward). Bounded inbox, no extra thread.
  - Gotcha: the parent's single `_inbox` already preserves order across jobs (N's frames, N's terminal, then N+1's), so prefetch needs no reordering — just put the terminal in the inbox BEFORE sending N+1.
  - Gotcha: cancel by the id the caller knows must be mapped to the job the host is actually running (the prefetched one); do the stop-flag + active lookup under the engine lock so a reader-side send cannot slip in after the cancel.
  - Gotcha: an instant fake host hides prefetch gains (N+1 finishes before the consumer arrives); measure with a host that sleeps per synthesize.
  - Never recycle from the reader thread (close joins it): a bloated host skips the prefetch and the consumer's normal start path recycles.
  - Measured: 12 segments, 50 ms generate, 60 ms consumer per segment: 1362 → 793 ms.
---

## [2026-10-09] - Task 5.1: Faster WSOLA
- **Implemented:** `_WsolaSearch` uses FFT cross-correlation over the unchanged ±15 ms window. Region spectra and inverse candidate norms are computed per 128-frame block; only the target FFT and the irfft run per frame. Clipped edge frames get a one-off FFT. The old routine is kept verbatim as `tests/unit/wsola_reference.py`.
- **Files changed:** core/audio.py, tests/unit/test_audio.py, tests/unit/wsola_reference.py
- **Commit:** c4e7a4b
- **Learnings:**
  - Gotcha: numpy per-call overhead (~10–20 µs per call on aarch64) dominates small-array DSP. A "cheaper" coarse-to-fine search with ~10 small ops per frame was SLOWER than 2 FFTs. Count numpy calls per frame, not MACs.
  - Gotcha: batching rfft rows only saves call overhead (~16 vs ~25 µs per row of 2400). The win came from moving frame-index-only work (region spectra, energies) out of the loop.
  - Gotcha: exact-argmax rewrites can still diverge on long inputs. A float32 near-tie in the oracle flips one pick, and every later frame follows the new path. Parity tests use short inputs, where outputs are bit-identical.
  - Gotcha: `test_speed_path_stretches_per_chunk_without_concatenation` monkeypatches `np.concatenate` globally, so build prefix sums with `np.cumsum(..., out=buf[1:])`.
  - Measured: 60 s at 0.8: 2.20 → 0.65 s (×3.4); per audio-second 24–62 ms → 4–17 ms.
---

## [2026-10-09] - Task 5.2: Studio stretched-mix cache
- **Implemented:** `_stretched()` in core/studio.py is a one-entry, lock-guarded cache keyed on (sha1 of the input mix + its size, factor). Exposes `clear_stretch_cache()` and `stretch_cache_size()`.
- **Files changed:** core/studio.py, tests/unit/test_studio.py, tests/unit/test_studio_async.py
- **Commit:** 043e3e4
- **Learnings:**
  - Pattern: key a DSP cache on a content digest of its INPUT, not on project structure or `id()`. Rebuilt-but-equal inputs hit, and freed-array id reuse can't false-hit.
  - Pattern: never hand out the cached array; return a copy (≈1 ms per 60 s), so a caller writing into its render can't corrupt the next one.
  - Gotcha: a gain placed BEFORE the speed op changes the stretch input, so it misses on purpose. WSOLA is scale-invariant only up to its 1e-8 energy floor, so exactness wins.
  - Measured: gain edit after speed on 60 s: 446 → 10 ms.
---

## [2026-10-09] - Task 5.3: Pipelined bulk encode (planned as audiobook MP3)
- **Implemented:** BatchFileController save FIFO (`_save_queue`, `_saving`, `_start_next_save`, `_finish_save`). `_kick()` overlaps one save with the next render and keeps the run alive until the saves drain.
- **Files changed:** ui/batch_controller.py, tests/unit/test_batch_controller.py, plan.md (deviation note)
- **Commit:** 195caaa
- **Learnings:**
  - Gotcha: the plan's premise was off. Audiobook MP3 is only produced by the user-triggered export, never between chapter syntheses. Grep for the actual serial pattern (save done → `_kick()`) before editing the listed files.
  - Pattern: pipeline depth 1 = "start the next render only while ≤ 1 save is owed" + a FIFO of save starters. Order is preserved and memory/disk backlog is bounded without a second executor.
  - Gotcha: once work overlaps, completion callbacks must not reset shared UI state (`currentIndex`) that now belongs to the next item.
  - Measured: MP3 encode 1.23 s per audio-minute, now hidden behind synthesis.
---

## [2026-10-09] - Task 6.1: Lazy, asynchronous tabs with idle prebuild
- **Implemented:** Only TextTab is eager. The other five tabs are `asynchronous: true` Loaders with a `ready` flag. `TabPrebuild` (app.py) flips `window.prebuildTabs` one event-loop turn after the first `frameSwapped`. `window.tabsReady` plus `wait_for_tabs(app, window)` give headless drivers one wait contract.
- **Files changed:** ui/qml/Main.qml, app.py, tests/smoke/test_ui_shell.py, test_ui_tabs.py, test_e2e_flows.py, tests/unit/test_app_entry.py, test_startup_benchmark.py, scripts/generate_screenshots.py, scripts/benchmarks/run_ui.py
- **Commit:** 6f167a2
- **Learnings:**
  - Gotcha: `Loader.status` can't be read from Python ("Can't find converter for 'QQuickLoader::Status'"). Expose `readonly property bool ready: status === Loader.Ready` and read that.
  - Pattern: start first-frame work from Python with a QueuedConnection on `frameSwapped`, not from a QML handler. The signal can come from the render thread, and the queue also pushes the work past the first paint.
  - Pattern: every headless driver that looks tabs up by objectName calls `wait_for_tabs()` right after create_app. run_ui waits too, so prebuild incubation stays out of the measured frame window.
  - Measured: create_app→first frame 425–475 → 214–320 ms; cold first frame median 1093 → 909 ms; first Settings visit 194–259 ms → ~1 ms.
---

## [2026-10-09] - Task 6.2: Waveform repaint discipline
- **Implemented:** PlaybackWaveform is split into a base canvas plus a played canvas clipped to the playhead. The playhead and selection band are Rectangles. Repaints happen only on envelope/size/colour/selection change, and are deferred while hidden (`_baseStale`/`_playedStale`). WaveformIndicator neither animates nor paints while hidden. Both expose `paintCount`.
- **Files changed:** ui/qml/PlaybackWaveform.qml, ui/qml/WaveformIndicator.qml, tests/smoke/test_ui_tabs.py (host QML files needed no change)
- **Commit:** 25deef7
- **Learnings:**
  - Pattern: anything that moves every tick (playhead, progress fill) becomes a scene item bound to a property. A "played vs unplayed" colour split becomes a second static layer revealed by a `clip: true` parent's width, never a per-tick repaint.
  - Pattern: QML `visible` is EFFECTIVE visibility, so StackLayout-hidden tabs see `visible === false` and `onVisibleChanged` fires on tab switch. Gate requestPaint and timers on it, and keep a stale flag to repaint once on show.
  - Gotcha: Canvas colours bound to Theme never repainted on a theme flip (no handler), so stale colours stayed until the next data change. Repaint from on<Color>Changed handlers.
  - Gotcha: the driver's wait_ms pumps every 50 ms, so a 33 ms glide timer gets one tick per pump. Give glide-settle checks ≥1 s.
  - Measured: replay paints 22/19/22 → 0/0/0; hidden meter during stream 18 → 0.
---

## [2026-10-09] - Task 6.3: Icon render target and analytic shadows
- **Implemented:** AppIcon drops `renderTarget: Canvas.FramebufferObject`. AppCard's hidden shape + MultiEffect becomes `RectangularShadow` (objectName cardShadow, z -1, blur 18, spread -2, offset.y 2/4, theme shadow tokens). PySide6 floor is now >=6.9.
- **Files changed:** components/AppIcon.qml, components/AppCard.qml, pyproject.toml, uv.lock, tests/unit/test_theme.py, tests/smoke/test_ui_shell.py
- **Commit:** ef09727
- **Learnings:**
  - Gotcha: source-scanning tests (`"MultiEffect" not in file`, no `FramebufferObject` anywhere) also match COMMENTS. Describe removed APIs generically in comments, or the guard fails on its own explanation.
  - Pattern: to check theme-bound colours without screenshots, give the item an objectName, flip `bridge.themePreference`, wait out the ColorAnimation, then read `QColor(item.property("color")).name(HexArgb)` against the hex values regex-parsed from Theme.qml.
  - Pattern: a dependency floor change edits both pyproject and the matching `requires-dist` specifier in uv.lock. The resolved version needs no relock when it already satisfies the floor.
  - Measured: 30 blur passes → 0, and the visual tree loses 366 items once all tabs are built.
---
## [2026-10-09] - Task 6.4: List models for chapters, cues, batch items, Studio clips/ops
- **Implemented:** generic `DictListModel` (keyed row diff). chapterModel/cueModel/itemModel/studioClipModel/studioOpModel plus count/summary scalars. QML views bind the models; smoke drivers check that delegates survive updates.
- **Files changed:** ui/list_models.py (new), ui/audiobook_controller.py, ui/subtitle_controller.py, ui/batch_controller.py, ui/controller.py, AudiobookTab.qml, StudioTab.qml, ParagraphTab.qml, components/{BatchQueueCard,SubtitleCard,StudioClipRow}.qml, tests (list_models, 4 controller suites, test_ui_tabs)
- **Commit:** 14b1453
- **Learnings:**
  - `DictListModel` (ui/list_models.py): sync(rows) diffs by key (common prefix/suffix → remove/insert, same key → dataChanged with changed roles + `modelData`). A `modelData` role returning the whole dict keeps `required property var modelData` delegates working unchanged; `index` still works.
  - Gotcha: Qt skips a Repeater rebuild when a QVariantList NOTIFY yields an *equal* array — a delegate-survival smoke test must change one row's content (e.g. regen a clip), or it passes against the old model too. Always mutation-check by reverting the QML `model:` line.
  - Sync the model in a Python slot connected in `__init__` (before QML binds) so it is current when bindings re-evaluate on the same NOTIFY.
  - Scalar reads (`list.length`, `list[i].x`) move to controller scalars (`itemCount`, `studioLastOpName`): a binding on `model.get(i)` never re-evaluates on dataChanged.
  - Smoke fakes must mirror the new surface (models synced on the same signals + scalars).
---
## [2026-10-09] - Task 6.5: Ahead-of-time QML compilation in packaging
- **Implemented:** packaging/qml_aot.py compiles every .qml/.js with PySide6's qmlcachegen into the PyInstaller workpath. The spec bundles each unit beside its source, and the release workflow asserts they shipped.
- **Files changed:** packaging/qml_aot.py (new), packaging/vienetts-app.spec, .github/workflows/release.yml, tests/unit/test_package.py
- **Commit:** b16fb8c
- **Learnings:**
  - Qt 6 (6.11.2) still checks `<source>c` (Foo.qmlc / foo.jsc) beside a LOCAL QML file before its user cache. `qmlcachegen --only-bytecode -o Foo.qmlc Foo.qml` writes a raw `qv4cdata` unit with sourceTimeStamp=0, so it survives install-time mtime changes; validity is only the Qt version + QML compile hash, so build with the same PySide6 you bundle. No C++ or qrc needed.
  - Never generate units into src/: a stale unit beside an edited .qml would win in a dev checkout. Stage them in the PyInstaller workpath and add them as datas.
  - Cheap proof that Qt used a unit: with a fresh XDG_CACHE_HOME, nothing is written under `<cache>/<app>/qmlcache/`. Without the unit, Qt compiles and writes a .qmlc there.
  - Keep a Python reference to QQmlComponent until create() returns, or PySide deletes it first ("Internal C++ object already deleted").
  - Win: cold first frame ~605 → ~296 ms.
---
## [2026-10-09] - Task 6.6: Defer numpy past first frame (measure-gated)
- **Implemented:** nothing. The gate was unmet, so no code change.
- **Files changed:** none
- **Commit:** none (not kept)
- **Learnings:**
  - Gate: warm `import vienetts_app.app` must drop ≥200 ms. Upper bound measured by importing the app with numpy pre-imported vs. not (15 alternating runs, warm): median 675 → 527 ms, so removing numpy from the startup graph entirely saves at most ~150 ms (min-to-min 129 ms). Standalone warm `import numpy` costs 70–125 ms. Below the gate, so no code change; 17 modules import numpy at top level.
  - The pre-import trick (import X first, then time the app import) bounds a lazy-import refactor's maximum win without writing it. Use it before any lazy-import task.
  - The startup import is ~675 ms warm; the rest is spread over PySide6 (~120 ms) and the app's own modules. A future startup task should profile `-X importtime` for app modules rather than chase numpy.
---
## [2026-10-09] - Task 7.1: ORT session knobs
- **Implemented:** Settings knobs (intra threads, step-session single thread, spin, BLAS cap) → OrtTuning → a temporary `onnxruntime.InferenceSession` seam around SDK construction. BLAS cap is applied at the top of `__main__.main`. run_matrix gets a cartesian knob sweep with a `matrix_cell` stamp; child flags live in scripts/benchmarks/ort_knobs.py.
- **Files changed:** core/engine.py, core/models.py, core/performance.py, __main__.py, ui/controller.py, scripts/benchmarks/{run_matrix,run_engine,run_once,ort_knobs}.py, tests (engine, settings, controller, app_entry, benchmark_pack, performance_harness)
- **Commit:** 3caffc1
- **Learnings:**
  - The SDK (OnnxV3LiteEngine) shares ONE SessionOptions across all sessions. ORT copies the options when a session is built, so per-session tuning = mutate the shared options just before `original(path, so)` and restore in `finally`. That preserves every option the SDK set without cloning.
  - `SessionOptions.get_session_config_entry(key)` RAISES RuntimeError for an unset key. Guard it; an unset spin key means ORT's default (spinning on, "1").
  - A BLAS/OpenMP cap only works before numpy loads, and `vienetts_app.core.qwen_engine` (imported at the top of main() for the host flags) already imports numpy. Apply the cap before that, from numpy-free modules (core.settings and core.performance; there is a subprocess test for this). For benchmark children, put it in the child env from the parent.
  - `monkeypatch.delenv(name, raising=False)` on an ABSENT var records nothing, so a var the code under test sets leaks into the rest of the session. Use `setenv(name, "x"); delenv(name)` to register the restore.
  - The tiny per-step session is `vieneu_acoustic_cached.onnx` (1-layer local transformer, run n_vq times per frame on 1-token inputs). The backbone decode step and the codec are larger.
---
## [2026-10-09] - Task 7.2: Export codec chunking
- **Implemented:** `TTSEngine(export_chunk_frames=None)` + `infer_stream(..., export=True)`; `VieNeuProvider.infer_stream_export`; worker `_provider_chunks` prefers a provider's `infer_stream_export` only when `job.live_transport is None`; `run_engine --export-chunk-frames` (stamped in the record metadata).
- **Files changed:** core/engine.py, workers/inference_worker.py, scripts/benchmarks/run_engine.py, tests test_engine / test_inference_worker / test_benchmark_pack
- **Commit:** 84e3b15
- **Learnings:**
  - Pattern: the SDK `v3turbo.Vieneu.infer_stream` drops extra kwargs, so a per-call knob on the inner engine is applied by shadowing `tts.engine.infer_stream` with an instance attribute for one stream and restoring/deleting it afterwards (same scoped-seam idea as the 7.1 ORT session swap).
  - Pattern: optional provider capabilities are discovered with `getattr(provider, "...", None)` + `callable` — the same shape as `infer_stream_segments`/`infer_stream_prefetched` — so Qwen providers need no change.
  - Gotcha: the SDK default is already chunk_frames=25 and its adaptive lead-in (`_target()` → 4/6/8 frames) still runs while synthesis trails real time, so the knob only shapes faster-than-realtime export; app wiring stays off until 7.4 evidence.
---
## [2026-10-09] - Task 7.3: VieNeu PyTorch batched export
- **Implemented:** `TTSEngine(export_batch_size=None)` + `infer_export_segments(texts, voice, temperature) -> (index, wav)`; `VieNeuProvider.infer_stream_segments` delegating to it (sequential export-stream fallback for duck-typed engines); `run_engine --export-batch-size` (stamped in the record).
- **Files changed:** core/engine.py, scripts/benchmarks/run_engine.py, tests test_engine / test_inference_worker / test_benchmark_pack
- **Commit:** 5d4817d
- **Learnings:**
  - Pattern: the batching decision lives in the engine (it knows the resolved SDK backend after `_ensure`), not in the provider's attribute surface — a getattr-probed capability must never trigger a model load or raise before the worker's try block.
  - Gotcha: the SDK reports `backend == "pytorch"` while the app's setting says `"torch"`; gate on the SDK's value after init so `auto` resolves correctly.
  - Gotcha: SDK `infer_batch` re-chunks each text (max_chars=256) and joins with gap silences, so batched audio is equivalent, not bit-identical, to the stream path; temperature is passed only when set (SDK default 0.8).
  - Gotcha: a factory double must not name a parameter `backend` — TTSEngine passes `backend=` to the factory.
---
## [2026-10-09] - Task 7.4: Tuning evidence and decisions
- **Implemented:** scripted real-engine sweeps on this host (models fetched by the SDK into an isolated HF_HOME); `docs/performance/tuning-vieneu.md` + `evidence/vieneu-tuning-linux-arm64-cpu-onnx-int8.json` + README section. One default flip (`blas_threads` → 1, OpenBLAS/MKL only, stripped from Qwen host envs) in its own commit a04fe06. Bench fixes found on the way: a57ab65 (export tags were rejected by `SAFE_TAG_KEYS`; matrix export axes), 5048f6a (`summarize` grouped all matrix cells together).
- **Files changed:** docs/performance/{tuning-vieneu.md,README.md,evidence/...json}; flip: core/{performance,models,qwen_engine}.py, __main__.py, ui/controller.py + tests; bench: scripts/benchmarks/{run_matrix,summarize,fakes}.py, core/performance.py + tests
- **Commit:** 022fb0b
- **Learnings:**
  - Gotcha: numpy's OpenBLAS sized to all cores spins its idle workers against ORT's intra-op threads — `OPENBLAS_NUM_THREADS=1` took RTF 1.43 → 0.80 on 4 cores (same mechanism as ORT spin, 1.4–2.9× worse). `OMP_NUM_THREADS` gets the same win but also sizes torch/ggml pools; the torch Qwen host takes its intra-op count from it unless THREADS_ENV is set — so cap only BLAS vars and strip the app's cap from child host envs.
  - Gotcha: the perf-harness smoke tests are `benchmark`-marked, so the default gate never ran `run_engine` end to end; a new trace tag outside `SAFE_TAG_KEYS` broke every direct run with the gate green. Run `pytest -m benchmark` whenever scripts/benchmarks changes.
  - Pattern: confirm a large single-sweep effect with interleaved rounds per condition (A B C D, A B C D) before flipping a default; also split the variables of a compound knob (which env var carries the win) — that decided the safe flip shape.
  - Pattern: re-run dependent sweeps on top of a flipped default — the BLAS cap moved this host from slower to faster than real time, the only regime where export chunking can matter.
  - Gotcha: a host fact-check of the doc's summary table caught two "in every cell" overstatements (spin and step-single-thread behave differently at 1 intra thread); derive table claims from the per-cell data, not the headline.
---
