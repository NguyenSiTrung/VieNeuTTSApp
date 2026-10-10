# Performance Hardening Implementation Plan

> **For agentic workers:** Use the `executing-plans` skill to implement this
> plan task-by-task. The user approved sequential execution, not parallel
> subagents. Read the specification, `conductor/patterns.md`, and this
> track's `learnings.md` first.

**Goal:** Remove GUI-thread freezes, decouple live playback from synthesis
speed, raise synthesis/export throughput, and fix the Int16 live-sink bug —
without behavior regressions or new runtime dependencies.

**Architecture:** Reuse existing seams: `ui/bg_ops.py` / injectable
`bg_runner` for GUI offload, `ChapterPersist`-style single-thread executors
for ordered work, the `TTSEngine.__init__` patch seam for SDK knobs, the
framed Qwen protocol for prefetch, and `scripts/benchmarks/run_matrix.py`
for evidence.

**Spec:** [spec.md](./spec.md)

## Global constraints

- Sequential phases and tasks. **No manual verification of any kind** —
  no per-phase approval gates, no manual UI/screenshot checks, no manual
  hardware runs. Every acceptance check is an automated test, smoke driver,
  or scripted benchmark; continue to the next task once the gates pass.
- Defaults for hardware tuning knobs (Phase 7) stay unchanged unless the
  task records benchmark evidence in `docs/performance/`.
- Live transport cap stays at `MAX_PCM_BYTES` (2 s); no duration-sized
  in-memory PCM buffers anywhere.
- Integrity: full SHA-256 only at install/repair/import/explicit verify.
- Only dependency change allowed: PySide6 floor → `>=6.9` (Task 6.3).
- Commit each task locally with its message + `git notes add` summary
  including before/after measurements. Never fetch, pull, push, or
  `bd dolt push` automatically.

## Task execution and gates

For each task:

1. Add the named tests first; run the focused command and confirm they fail
   for the missing contract (not an import/setup error).
2. Implement; rerun the focused command; refactor within the task's files.
3. Run the repository gates from the repo root:

   ```bash
   .venv/bin/ruff check .
   .venv/bin/ruff format --check .
   QT_QPA_PLATFORM=offscreen QT_AUDIO_BACKEND=ffmpeg .venv/bin/pytest
   ```

4. The documented device-less real-QAudioSink failure
   (`VieNeuTTSApp-3iy`) is expected; investigate anything else.
5. Stage only the task's files, commit, add the git note, update the Bead
   and checkbox, and append learnings to `learnings.md`.

Perf-sensitive tasks also add a `@pytest.mark.benchmark` case (deselected
by default) or a scratch measurement whose numbers go into the git note.

## File and responsibility map

| Area | Files |
| --- | --- |
| Live playback / transport | `ui/stream_playback.py`, `core/pcm_transport.py`, `workers/inference_worker.py`, `core/artifacts.py` |
| Qwen parent/host | `core/qwen_protocol.py`, `core/qwen_engine.py`, `core/qwen_gguf_engine.py`, `workers/qwen_host.py`, `workers/qwen_gguf_host.py`, `core/text_segmentation.py` |
| Integrity stamps | `core/managed_install.py`, `core/qwen_model_manager.py`, `core/qwen_gguf_models.py`, `core/qwen_gguf_runtime.py` |
| GUI controllers | `ui/controller.py`, `ui/audiobook_controller.py`, `ui/subtitle_controller.py`, `ui/batch_controller.py`, `ui/bg_ops.py` |
| Audiobook core | `core/audiobook.py` |
| DSP | `core/audio.py`, `core/studio.py` |
| VieNeu engine | `core/engine.py`, `scripts/benchmarks/*` |
| QML | `ui/qml/Main.qml`, `TextTab.qml`, `AudiobookTab.qml`, `StudioTab.qml`, `SettingsTab.qml`, `PlaybackWaveform.qml`, `components/{DocumentEditorCard,SubtitleCard,SynthesisBar,AppIcon,AppCard,WaveformIndicator}.qml` |
| Packaging | `packaging/vienetts-app.spec`, `pyproject.toml` |
| Evidence | `docs/performance/README.md`, `docs/performance/tuning-*.md`, `docs/performance/evidence/` |

---

## Phase 1: Correctness and quick wins

- [x] **Task 1.1: Int16 live-sink conversion**
  - Files: `ui/stream_playback.py`, `tests/unit/test_stream_playback.py`
  - Red tests: `TransportIODevice` constructed for an Int16 format returns
    `len(float_bytes)//2` bytes of correctly scaled/clipped int16 for a
    known float32 ramp (incl. ±1.0 and >1.0 clipping); odd float byte counts
    never split a sample; `buffered_drain_ms` uses 2 bytes/sample for Int16.
    Float32 path returns identical bytes to today.
  - Implement: pass the negotiated sample width into `TransportIODevice`;
    when Int16, `take()` an even multiple of 4 bytes ≈ `2*maxSize`, convert
    with NumPy. Then grep for `StreamIODevice`/`feed`/`play_buffer`/
    `_emit_levels` production callers; delete only if unreachable (FR-1.2),
    else record why in learnings.
  - Commit: `fix(playback): convert live PCM for Int16 fallback sinks` — `c35558c`

- [x] **Task 1.2: Vectorized Qwen PCM codec**
  - Files: `core/qwen_protocol.py`, `core/qwen_engine.py`,
    `tests/unit/test_qwen_protocol.py`
  - Red tests: `pcm_from_bytes` returns a float32 `ndarray` equal to the old
    tuple decode for random payloads; partial-sample payload still raises
    `ProtocolError`; `pcm_to_bytes` byte-identical to the struct encoding;
    benchmark case asserting ≥100× speedup on a 24,000-sample frame.
  - Implement: `np.frombuffer(payload, "<f4").copy()`; `np.asarray(samples,
    "<f4").tobytes()`; drop the redundant `np.asarray` wrap at
    `qwen_engine.py:1136`.
  - Commit: `perf(qwen): vectorize PCM frame encode/decode` — `540b6db`

- [x] **Task 1.3: Debounced text metrics**
  - Files: `ui/controller.py`, `ui/qml/TextTab.qml`,
    `ui/qml/components/DocumentEditorCard.qml`, `core/text_metrics.py`,
    `tests/unit/test_text_metrics.py`, `tests/unit/test_controller.py`,
    `tests/smoke/test_ui_tabs.py`
  - Red tests: new slot `textMetrics(text)` → `{"words", "seconds"}`
    matching `wordCount`/`estimateDurationSeconds`; smoke: typing N chars
    in quick succession produces one metrics call after the debounce and
    the label text equals today's settled text.
  - Implement: 250 ms single-shot `Timer` restarted on `textChanged`; labels
    bind to cached `metrics` properties. Keep the old slots for other callers.
  - Commit: `perf(ui): debounce word-count and duration metrics` — `838822f`

- [x] **Task 1.4: Subtitle rate slider applies on release**
  - Files: `ui/qml/components/SubtitleCard.qml`, `ui/subtitle_controller.py`,
    `tests/unit/test_subtitle_controller.py`, `tests/smoke/test_ui_tabs.py`
  - Red tests: dragging through 10 steps triggers one `_rebuild_for_policy`;
    keyboard step debounced; `cuesChanged` not emitted when the rebuilt cue
    list is equal.
  - Implement: `onPressedChanged: if (!pressed) apply`, debounced
    `onMoved` for keyboard; equality guard before emitting.
  - Commit: `perf(subtitles): rebuild dub policy on slider release only` — `3394826`

- [x] **Task 1.5: Warm the text pipeline during prewarm**
  - Files: `core/engine.py`, `workers/inference_worker.py`,
    `tests/unit/test_engine.py`, `tests/unit/test_inference_worker.py`
  - Red tests: `_process_warmup` calls a new `TTSEngine.warm_text_pipeline()`
    after `initialize()`; failures stay silent; non-default providers not
    warmed; Qwen providers unaffected.
  - Implement: normalize + phonemize a fixed short phrase through the same
    SDK entry points as `infer_stream`. Measure a tiny discarded synthesis
    in a scratch run; include it only if it lowers first-job TTFC
    measurably (record the decision in the git note).
  - Commit: `perf(engine): warm normalization and phonemizer at prewarm` — `04191a9`

- [x] **Task 1.6: Single-copy transport take**
  - Files: `core/pcm_transport.py`, `tests/unit/test_pcm_transport.py`
  - Red tests: existing contract tests + `take` returns `bytes` with one
    copy (memoryview slice → `bytes`), compaction unchanged.
  - Commit: `perf(transport): drop the redundant copy in take()` — `ecabad9`

## Phase 2: GUI-thread offload

- [x] **Task 2.1: Audiobook state-read memo and single-read model rebuild**
  - Files: `core/audiobook.py`, `ui/audiobook_controller.py`,
    `tests/unit/test_audiobook.py`, `tests/unit/test_audiobook_controller.py`
  - Red tests: `_read_state` parses once for repeated calls with unchanged
    `(mtime_ns, size)` and re-parses after a write; `_chapters_model`
    performs one state read per rebuild (spy); benchmark: 300-chapter
    rebuild ≥10× faster than baseline.
  - Implement: memo keyed on `(path, mtime_ns, size)`, invalidated by the
    library's own writes; helpers (`segment_ready_count`,
    `has_chapter_audio`, `chapter_audio_paths`) accept an optional parsed
    state; `_reconcile_status` reuses it.
  - Commit: `perf(audiobook): read chapter state once per model rebuild` — `6177622`

- [x] **Task 2.2: Load books off the GUI thread**
  - Files: `ui/audiobook_controller.py`, `ui/qml/AudiobookTab.qml`,
    `tests/unit/test_audiobook_controller.py`
  - Red tests: `openBook` / `refreshChapters` route `load_book` through
    `bg_runner`; a `loading` property toggles; stale completions (different
    book id) are dropped.
  - Commit: `perf(audiobook): load books in the background` — `8c55a94`

- [x] **Task 2.3: Background audiobook export**
  - Files: `ui/audiobook_controller.py`, `core/audiobook.py`,
    `ui/qml/AudiobookTab.qml`, `tests/unit/test_audiobook_controller.py`,
    `tests/smoke/test_ui_tabs.py`
  - Red tests: `exportAllReady`/`exportChapter` return immediately and run
    through `bg_runner`; `exporting` property + `exportProgress(done,total)`
    + `exportFinished(count, error)`; book loaded once per export (spy on
    `load_book`); playing chapter still skipped with the same message;
    second export while exporting is refused.
  - Implement: `export_chapter` accepts the pre-loaded record; QML disables
    export actions while `exporting` and shows progress.
  - Commit: `perf(audiobook): export chapters off the GUI thread` — `03ec5bc`

- [x] **Task 2.4: Ordered executor for subtitle dub rendering**
  - Files: `ui/subtitle_controller.py`, `ui/chapter_persist.py` (reuse or
    generalize), `tests/unit/test_subtitle_controller.py`
  - Red tests: `_consume_unit` work (read → split → stretch → add_clip) runs
    on the executor in submission order; `_finish_render` writes off-thread;
    cancel drops queued units; stale render generations are ignored.
  - Commit: `perf(subtitles): process dub units on an ordered worker` — `6ce09b0`

- [x] **Task 2.5: Remaining small GUI-thread offloads**
  - Files: `ui/controller.py`, `tests/unit/test_controller.py`,
    `tests/unit/test_studio_controller.py`
  - Red tests: `openChapterInStudio`, `studioPreviewClip`,
    `_complete_audition` file I/O run through `bg_runner`; `torchAvailable`
    re-probe after a CUDA state flip is scheduled off-thread and notifies.
  - Commit: `perf(ui): move studio/audition file I/O off the GUI thread` — `5de5be2`

## Phase 3: Qwen integrity stamps and host hygiene

- [x] **Task 3.1: Stat-stamp verification contract**
  - Files: `core/managed_install.py`, `tests/unit/test_managed_install.py`
  - Red tests: `file_stamp(path)` → `(size, mtime_ns, inode)`;
    `file_matches_stamped(path, size, sha, stamp)` skips hashing on an equal
    stamp, hashes on mismatch/missing stamp, returns the fresh stamp after a
    successful hash; Windows-safe (`st_ino` may be 0 → fall back to
    size+mtime).
  - Commit: `feat(install): stat-stamp fast path for verified files` — `e9c46f9`

- [x] **Task 3.2: Stamps in Qwen model/runtime installers**
  - Files: `core/qwen_model_manager.py`, `core/qwen_gguf_models.py`,
    `core/qwen_gguf_runtime.py`, their unit tests
  - Red tests: install/repair/import write stamps to the install record;
    `inspect(mode="stamp")` on an unchanged tree performs zero `sha256_of`
    calls (spy); touching a file forces one full hash and still reports
    corrupt files as missing; legacy records without stamps hash once and
    persist stamps; `inspect(mode="full")` always hashes. Fix the "safe on
    the GUI thread" docstrings.
  - Commit: `perf(qwen): skip full hashes for unchanged installs` — `6f85024`

- [x] **Task 3.3: Off-thread Qwen engine preparation + "Verify files"**
  - Files: `ui/controller.py`, `ui/qml/SettingsTab.qml`,
    `ui/qml/components/QwenInstallCards.qml`,
    `tests/unit/test_controller.py`, `tests/unit/test_qwen_variant_controller.py`,
    `tests/smoke/test_ui_tabs.py`, i18n `.ts` updates
  - Red tests: `_build_qwen_*_engine` inspection runs through `bg_runner`
    with `mode="stamp"`; submits during preparation enter a
    `preparingEngine` state and are released after; failure surfaces the
    existing actionable error; profile switch during preparation drops the
    stale result; the Settings action runs `mode="full"` off-thread and
    reports result.
  - Commit: `perf(qwen): prepare engines off the GUI thread; add Verify files` — `42e938c`

- [x] **Task 3.4: Qwen host hygiene**
  - Files: `workers/qwen_host.py`, `workers/qwen_gguf_host.py`,
    `core/qwen_engine.py`, `tests/unit/test_qwen_host.py`,
    `tests/unit/test_qwen_gguf_host.py`, `tests/unit/test_qwen_engine.py`
  - Red tests: `_release_accelerator` runs every N jobs or when RSS growth
    exceeds the threshold (fake RSS), never per job otherwise;
    `configure_torch_threads` invoked before the loader; generate wrapped in
    `inference_mode` when torch exposes it; `host_footprint` reads
    `/proc/<pid>/statm` on Linux without spawning; GGUF `_source_clip` hash
    reused when the clip stat is unchanged.
  - Commit: `perf(qwen): trim per-job host overhead` — `541d079`

## Phase 4: Live pipeline

- [x] **Task 4.1: Non-blocking transport offer + artifact tail reader**
  - Files: `core/pcm_transport.py`, `core/artifacts.py`,
    `tests/unit/test_pcm_transport.py`, `tests/unit/test_artifacts.py`
  - Red tests: `offer(payload)` accepts up to free capacity and returns the
    accepted byte count without blocking; `IncrementalArtifactWriter`
    exposes `frames_written` and a reader that returns PCM frames
    `[start, end)` from the in-progress `.part.wav` (flushed data only).
  - Commit: `feat(live): non-blocking transport offer and part-file reader` — `c36ea3b`

- [x] **Task 4.2: Decouple the worker writer from live playback**
  - Files: `workers/inference_worker.py`, `ui/stream_playback.py`,
    `tests/unit/test_inference_worker.py`, `tests/unit/test_stream_playback.py`,
    `tests/unit/test_replay_transport.py`
  - Red tests: with a fake provider faster than real time and a slow sink,
    the job's artifact completes in < audio duration; transport never
    exceeds cap; the feeder tops up from the part file in order with no gap
    or duplicate samples (sample-exact comparison); prebuffer, first-pull
    markers, cancel discard, and post-completion replay unchanged;
    export-only mode unchanged.
  - Implement: worker writes the artifact then `offer()`s what fits; a live
    cursor tracks delivered frames; the stream-playback feeder timer (20 ms)
    refills from the part reader (then the final artifact) while frames
    remain.
  - Commit: `perf(live): stop throttling synthesis to playback speed` — `f53598d`

- [x] **Task 4.3: Qwen live progressive segmentation**
  - Files: `core/text_segmentation.py`, `workers/inference_worker.py`,
    `tests/unit/test_text_segmentation.py`
  - Red tests: `split_text_for_profile(..., progressive=True)` yields first
    ≤150, second ≤250, then ≤512 chars at sentence/clause boundaries with
    faithful joining; non-live calls unchanged.
  - Commit: `perf(qwen): smaller first segments for live jobs` — `af0c409`

- [x] **Task 4.4: Qwen live next-segment prefetch**
  - Files: `core/qwen_engine.py`, `core/qwen_gguf_engine.py`,
    `workers/inference_worker.py`, `tests/unit/test_qwen_engine.py`,
    `tests/unit/test_qwen_gguf_engine.py`
  - Red tests (fake host): segment N+1 `synthesize` sent right after N's
    terminal frame; at most one prefetched job; cancel cancels both; host
    crash during prefetch surfaces the same error as today; frames stay in
    order.
  - Commit: `perf(qwen): prefetch the next live segment` — `ac5865e`

## Phase 5: DSP and export throughput

- [x] **Task 5.1: Faster WSOLA**
  - Files: `core/audio.py`, `tests/unit/test_audio.py`
  - Red tests: parity vs the current implementation within documented
    tolerance (length exact, RMS error bound, no clicks at chunk joins) for
    rates 0.5/0.8/1.25/2.0; benchmark ≥3× faster on 60 s.
  - Implement: decimated coarse search + ±4-sample refine (or FFT
    correlation); keep the old routine as a test oracle.
  - Commit: `perf(audio): coarse-to-fine WSOLA search` — `c4e7a4b`

- [x] **Task 5.2: Studio stretched-mix cache**
  - Files: `core/studio.py`, `tests/unit/test_studio.py`,
    `tests/unit/test_studio_async.py`
  - Red tests: a non-speed edit reuses the cached stretched mix (spy on
    stretch); a speed edit or mix change invalidates; bounded to one entry.
  - Commit: `perf(studio): cache the time-stretched mix` — `043e3e4`

- [x] **Task 5.3: Pipelined audiobook MP3 encode**
  - Files: `core/audiobook.py`, `ui/audiobook_controller.py`,
    `tests/unit/test_audiobook.py`, `tests/unit/test_audiobook_controller.py`
  - Red tests: chapter N encode runs on the side executor while N+1
    synthesizes; output order and failure reporting unchanged; cancel waits
    for/aborts the in-flight encode cleanly.
  - Commit: `perf(audiobook): encode MP3 alongside synthesis` — `195caaa`
  - Note (implementation): the audiobook path never encodes during synthesis.
    Its MP3 encode happens only in the user-triggered chapter export, after
    rendering, and that export is already off the GUI thread. The
    synthesize → encode → next-item serialization this task targets is in
    `ui/batch_controller.py` (Paragraph bulk queue), so the pipelining landed
    there: `tests/unit/test_batch_controller.py::TestPipelinedSave`.

## Phase 6: QML rendering and startup

- [x] **Task 6.1: Lazy, asynchronous tabs with idle prebuild**
  - Files: `ui/qml/Main.qml`, `app.py`, `tests/smoke/test_ui_shell.py`,
    `tests/smoke/test_ui_tabs.py`, `tests/unit/test_startup_benchmark.py`
  - Red tests: Paragraph/Studio not instantiated at startup; all tab
    Loaders `asynchronous: true`; idle prebuild activates them after first
    frame; drivers wait on `Loader.status === Loader.Ready`; startup
    benchmark shows no regression and first Settings visit < 100 ms GUI
    block.
  - Update smoke drivers that look up tab objects immediately.
  - Commit: `perf(qml): lazy async tab loading with idle prebuild` — `6f167a2`

- [x] **Task 6.2: Waveform repaint discipline**
  - Files: `ui/qml/PlaybackWaveform.qml`,
    `ui/qml/components/WaveformIndicator.qml`, `TextTab.qml`,
    `components/SynthesisBar.qml`, `StudioTab.qml`,
    `tests/smoke/test_ui_tabs.py`
  - Red tests (driver): invisible instances paint 0 times during replay;
    the visible instance repaints the canvas only on envelope/size/theme
    change; playhead `x` tracks position.
  - Commit: `perf(qml): repaint waveforms only when visible and changed` — `25deef7`

- [x] **Task 6.3: Icon render target and analytic shadows**
  - Files: `ui/qml/components/AppIcon.qml`, `ui/qml/components/AppCard.qml`,
    `pyproject.toml`, `uv.lock` (floor only), `tests/unit/test_theme.py`,
    `tests/smoke/test_ui_shell.py`
  - Red tests: no `FramebufferObject` render targets; `AppCard` uses
    `RectangularShadow` with theme tokens in both themes (smoke driver reads
    the shadow item's properties; no manual screenshot review).
  - Commit: `perf(qml): drop per-icon FBOs and use analytic card shadows` — `ef09727`

- [x] **Task 6.4: List models for chapters, cues, batch items, Studio clips/ops**
  - Files: `ui/list_models.py` (new), `ui/audiobook_controller.py`,
    `ui/subtitle_controller.py`, `ui/batch_controller.py`, `ui/controller.py`,
    `AudiobookTab.qml`, `StudioTab.qml`, subtitle/batch QML,
    `tests/unit/test_list_models.py` (new), affected controller tests and
    smoke drivers
  - Red tests: generic dict-row `QAbstractListModel` emits `dataChanged` for
    changed rows only and insert/remove for structural changes; controllers
    update rows in place; QML delegates read roles; list scroll position
    survives a status update (driver); `readyCount` etc. exposed as scalars.
  - Implement one controller at a time inside the task; commit once gates
    pass for all.
  - Commit: `perf(ui): row-level list models for long lists` — `14b1453`

- [x] **Task 6.5: Ahead-of-time QML compilation in packaging**
  - Files: `packaging/vienetts-app.spec`, `tests/unit/test_package.py`,
    release workflow if it needs a step
  - Red tests: spec includes compiled QML cache / rcc resource for every
    `.qml`; packaged smoke (CI) still boots.
  - Commit: `build: precompile QML in packaged builds` — `b16fb8c`

- [x] **Task 6.6: Defer numpy past first frame (measure-gated)**
  - Files: `core/artifacts.py`, `ui/audiobook_controller.py`,
    `ui/stream_playback.py`, others as found, `tests/unit/test_startup_hardening.py`
  - Red tests: `import vienetts_app.app` does not import numpy
    (`sys.modules` check in a subprocess).
  - Gate: keep the change only if warm import drops ≥200 ms; otherwise
    revert and record the measurement in learnings.
  - Commit: `perf(startup): import numpy lazily` — `not kept: gate unmet (max ~150 ms < 200 ms), no commit`

## Phase 7: Bench-gated engine tuning

- [x] **Task 7.1: ORT session knobs**
  - Files: `core/engine.py`, `core/models.py`, `core/settings.py`,
    `scripts/benchmarks/run_matrix.py`, `run_engine.py`,
    `tests/unit/test_engine.py`, `tests/unit/test_settings.py`,
    `tests/unit/test_benchmark_pack.py`
  - Red tests: knobs (intra threads, small-session single thread, spin
    during job, BLAS/OMP cap) default to today's behavior; when set they
    reach the SDK session options via the patch seam; the matrix sweeps
    them.
  - Commit: `feat(engine): expose ORT threading knobs for benchmarking` — `3caffc1`

- [x] **Task 7.2: Export codec chunking**
  - Files: `core/engine.py`, `workers/inference_worker.py`,
    `tests/unit/test_engine.py`
  - Red tests: non-live jobs request `chunk_frames=25` only when the knob is
    on (default off); live jobs never do.
  - Commit: `feat(engine): optional export-sized codec chunks` — `84e3b15`

- [x] **Task 7.3: VieNeu PyTorch batched export**
  - Files: `core/engine.py`, `tests/unit/test_engine.py`,
    `tests/unit/test_inference_worker.py`
  - Red tests: with backend `pytorch` and the flag on,
    `infer_stream_segments` groups ≤ configured size and yields
    `(index, wav)` in order; ONNX backend never batches; flag default off.
  - Commit: `feat(engine): optional batched export on the PyTorch backend` — `5d4817d`

- [x] **Task 7.4: Tuning evidence and decisions**
  - Files: `docs/performance/tuning-vieneu.md` (new),
    `docs/performance/evidence/*.json`, `docs/performance/README.md`
  - Run the matrix scripted on the host the task executes on (no manual
    hardware sessions); record raw JSON, method, and per-knob decision. Flip a default only where evidence shows
    a win (separate commit per flip). Close/supersede bead `1v6`; file
    beads for unmeasured cells, the GGUF thread-count ABI change, GGUF Base
    prefix reuse, resampler anti-imaging, and stateful streaming WSOLA.
  - Commit: `docs(perf): record engine tuning evidence` — `022fb0b`
