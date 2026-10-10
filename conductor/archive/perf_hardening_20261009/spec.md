# Performance hardening — GUI-thread freezes, live pipeline, throughput

## Overview

A read-only performance audit (2026-10-09; four parallel sub-audits over the
VieNeu synthesis path, GUI-thread controllers, playback/QML, and the Qwen
host/IPC) found 20 concrete improvements plus one correctness bug. This
track implements all of them. Findings were measured on a 4-vCPU Linux box
with a warm page cache and no model weights (model compute itself was not
timed); every figure below is indicative, and each task re-measures its own
before/after.

Track type: **performance / bug-fix chore**. No new user-facing features
beyond a "Verify model files" action and progress states for work that
previously froze the window.

**Approved decisions (2026-10-09):**

- **Integrity:** full SHA-256 only at install, repair, and on an explicit
  "Verify files" request. At engine build, compare a stored
  `(size, mtime_ns, inode)` stamp per file; any stamp mismatch falls back to a
  full hash. The engine-build inspection never runs on the GUI thread.
- **Live mode:** decouple the artifact writer from the live transport. The
  writer never blocks on playback; the in-memory live transport keeps its
  strict 2 s cap and is refilled from the `.part.wav` tail when it falls
  behind. Qwen live jobs also get progressive segment sizes and next-segment
  prefetch.
- **Hardware tuning:** implement the knobs (ORT threads/spinning, export
  `chunk_frames`, VieNeu PyTorch batching) with **defaults unchanged**, add
  benchmark-matrix coverage, and flip a default only when evidence recorded
  under `docs/performance/` shows a win. Unverified hardware cells stay open
  beads (the `ysl8.7` posture).
- **Execution:** sequential phases and tasks; automated gates per task
  (ruff + pytest); commit per task with a git note. **No manual
  verification of any kind** (no approval gates, manual UI checks, or manual
  hardware sessions) — every acceptance criterion is checked by automated
  tests, smoke drivers, or scripted benchmarks.
- **Priority:** high. No prerequisite track; no time estimate.

## Findings → requirements map

| # | Finding (audit) | Requirement |
|---|---|---|
| B | Int16 sink fallback receives raw float32 bytes on the live path | FR-1.1 |
| 1 | Audiobook export-all/chapter export run on the GUI thread (MP3 ≈13.6 s per 10 min audio) | FR-2.1 |
| 2 | Qwen engine build SHA-256s 1–2.5 GB on the GUI thread (≈2–4 s warm, 5–20 s cold) | FR-2.2 |
| 3 | Qwen parent PCM decode is per-sample `struct.unpack_from` (7.2 ms vs 9 µs per frame) | FR-3.1 |
| 4 | Word count/duration recomputed per keystroke (78 ms at 200k chars) | FR-2.5 |
| 5 | Warmup loads sessions but leaves text pipeline cold (~0.6 s on first job) | FR-3.2 |
| 6 | First Settings visit blocks 170–410 ms (synchronous Loader) | FR-5.1 |
| 7 | Audiobook chapter model re-parses state.json twice per chapter (1.37 s at 300 ch) | FR-2.3 |
| 8 | Live transport `put()` throttles the writer to playback speed | FR-3.3 |
| 9 | Qwen live: 512-char first segment; no cross-segment lookahead | FR-3.4, FR-3.5 |
| 10 | Subtitle dub WSOLA + disk writes per unit on GUI thread | FR-2.4 |
| 11 | Subtitle rate slider rebuilds project on every 0.05 step | FR-2.6 |
| 12 | QVariantList list properties replaced wholesale | FR-5.4 |
| 13 | Paragraph/Studio tabs built eagerly at startup | FR-5.1 |
| 14 | ORT thread/spin config never tuned (bead `1v6`) | FR-4.1 |
| 15 | VieNeu PyTorch batched export unused | FR-4.3 |
| 16 | Export jobs use latency-tuned 4-frame codec decode | FR-4.2 |
| 17 | Qwen host `gc.collect()` + `empty_cache()` after every job | FR-3.6 |
| 18 | WSOLA 23–37 ms per audio-second; Studio re-stretches the whole mix per edit | FR-3.7 |
| 19 | Audiobook MP3 encode serial with synthesis | FR-3.8 |
| 20 | QML runtime-compiled; hidden waveforms repaint; 196 FBO icons; MultiEffect shadows; numpy at startup; minor host/transport costs | FR-5.2–5.6, FR-3.6, FR-3.9 |

## Functional requirements

### FR-1: Correctness

1. **Int16 live fallback.** When the negotiated sink format is Int16, the
   live path must deliver Int16 PCM (clipped, scaled float32 → int16), and
   `buffered_drain_ms` must use the negotiated sample width. Float32 sinks
   are byte-identical to today.
2. Remove production-dead playback code paths (`StreamIODevice`, `feed`,
   `play_buffer`, `_emit_levels`) **only if** a usage search confirms they
   are unreachable in production; otherwise leave them and record why.

### FR-2: No multi-hundred-millisecond work on the GUI thread

1. **Audiobook export.** `exportChapter` and `exportAllReady` run through the
   background runner (`ui/bg_ops.py` pattern) with an `exporting` property,
   per-chapter progress, and a completion signal carrying the exported count
   and any error. The book is loaded once per export, not per chapter. The
   "skip the chapter that is playing" rule is preserved. QML callers move to
   the completion signal instead of the synchronous return value.
2. **Qwen integrity stamps + off-thread engine build.**
   - Installers (`qwen_model_manager`, `qwen_gguf_models`,
     `qwen_gguf_runtime`) write a per-file `(size, mtime_ns, inode)` stamp
     into their install record after a successful full verification
     (install, repair, offline-pack import, explicit verify).
   - A new `inspect(mode="stamp" | "full")` contract: `stamp` compares stamps
     and falls back to a full hash only for files whose stamp mismatches or
     is missing (legacy installs self-upgrade on first full pass); `full`
     always hashes.
   - Engine build (`_build_qwen_engine`, `_build_qwen_gguf_engine`) uses
     `stamp` mode and runs the inspection off the GUI thread; submits made
     while the engine is being prepared wait in a "Preparing engine…" state
     and are released (or failed with the existing actionable error) on
     completion. Busy gating and the one-resident-model rule are unchanged.
   - Settings gains a **"Verify files"** action per installed Qwen
     model/runtime that runs `full` in the background.
   - Fix the "safe to call on the GUI thread" docstring claims.
3. **Audiobook chapter model.** One `state.json` read per model rebuild
   (helpers accept the parsed state/plan), plus a `_read_state` memo keyed on
   `(path, mtime_ns, size)`. `openBook` / `refreshChapters` load the book off
   the GUI thread. Chapter-list rebuild at 300 chapters must drop by ≥ 10×
   versus baseline.
4. **Subtitle dub rendering.** Per-unit `read_wav` → split → WSOLA →
   `add_clip` and `_finish_render` file writes run on an order-preserving
   single-thread executor; the GUI thread only receives completion signals.
   Cancel and the stale-render guard behave as today.
5. **Text metrics.** Word count and duration estimate are computed by a
   single slot returning both values, driven by a ≈250 ms debounce timer in
   `TextTab.qml` and `DocumentEditorCard.qml`. Displayed values are identical
   once settled.
6. **Subtitle rate slider.** The rate cap is applied on release (and on
   keyboard steps after a short debounce), not on every `onMoved` step;
   `cuesChanged` is not emitted when the rebuilt cues are unchanged.
7. **Minor GUI-thread work moved off-thread:** `openChapterInStudio`
   (book load + whole-chapter WAV read), `studioPreviewClip` (clip WAV
   write), `_complete_audition` (WAV rewrite), and the `torchAvailable`
   re-probe after a CUDA state flip.

### FR-3: Synthesis latency and throughput

1. **Qwen PCM decode** uses `np.frombuffer(payload, "<f4")` (copied) and
   `pcm_to_bytes` uses a vectorized encode; byte-for-byte identical results.
2. **VieNeu warmup** additionally normalizes and phonemizes a short fixed
   phrase (and runs one tiny discarded synthesis if it measurably lowers
   first-job TTFC) — still silent on failure, still only the active
   provider.
3. **Live writer decoupling (VieNeu + Qwen).** The worker's artifact write
   never blocks on the live transport. The transport stays bounded at 2 s;
   when it has room, a feeder tops it up from the artifact (in-memory
   overflow is not allowed beyond the cap). Behavior preserved: prebuffer,
   first-sink-pull markers, cancel/stop discard, replay after completion,
   export-only mode. A live job's wall time is bounded by synthesis speed,
   not audio duration.
4. **Qwen live progressive segmentation.** For live jobs only, segments grow
   at sentence/clause boundaries: first ≤ 150 chars, second ≤ 250, then the
   existing 512 cap. Export/batch segmentation is unchanged.
5. **Qwen live prefetch.** The next segment's `synthesize` is sent as soon as
   the previous segment's terminal frame is read (one segment of lookahead;
   bounded memory ≈ one segment of PCM). Cancel stops both the current and
   the prefetched job.
6. **Qwen host hygiene.**
   - `_release_accelerator()` runs only every N jobs or when the RSS check
     shows growth past a threshold below the recycle limit (the OOM-ratchet
     guard from the earlier incident stays effective).
   - `configure_torch_threads()` runs before `from_pretrained`.
   - Generation runs under `torch.inference_mode()`.
   - `host_footprint` reads `/proc/<pid>/statm` on Linux (no `ps` fork),
     keeping the existing fallback elsewhere.
   - GGUF `_source_clip` caches the reference-clip hash keyed on its stat.
7. **WSOLA.** Coarse-to-fine similarity search (decimated search + local
   refine, or FFT correlation) with output within a documented tolerance of
   the current implementation; ≥ 3× faster on 60 s of audio. Studio caches
   the stretched mix keyed on (mix identity, rate) so unrelated edits do not
   re-stretch.
8. **Audiobook MP3 pipelining.** Chapter N's MP3 encode runs on a side
   thread while chapter N+1 synthesizes; ordering, cancel, and failure
   reporting are unchanged.
9. `BoundedPcmTransport.take` avoids the extra copy (single copy out).

### FR-4: Bench-gated engine tuning (defaults unchanged until evidence)

1. **ORT session knobs** plumbed through `TTSEngine` and the benchmark
   matrix: intra-op thread count, per-session single-thread for the small
   per-step session, spin-during-job, and an OpenBLAS/OMP thread cap.
   Supersedes bead `VieNeuTTSApp-1v6`.
2. **Export codec chunking:** non-live jobs may request `chunk_frames=25`
   from the SDK stream (through the existing patch seam).
3. **VieNeu PyTorch batched export:** `VieNeuProvider.infer_stream_segments`
   via the SDK's `infer_batch` over bounded groups, behind a setting/flag.
4. **Evidence.** `docs/performance/` gains a tuning record (method, hardware
   class, raw benchmark JSON, decision). A default flips only with recorded
   evidence; cells without evidence get open beads.

### FR-5: QML rendering and startup

1. **Tabs:** Paragraph and Studio load on first visit like the other heavy
   tabs; all tab Loaders are `asynchronous: true` and are pre-built during
   idle time after the first frame so first visits are instant. Existing
   objectName contracts keep working (drivers wait for `Loader.status`).
2. **Waveforms:** `PlaybackWaveform` / `WaveformIndicator` stop animating and
   repainting while not visible; the playhead and played-overlay are
   separate items so the Canvas repaints only on envelope/size/theme change.
3. **Icons and shadows:** `AppIcon` drops `renderTarget: FramebufferObject`;
   `AppCard` shadows use `RectangularShadow` (raises the PySide6 floor to
   ≥ 6.9 — `uv.lock` already pins 6.11.2) with visually equivalent tokens.
4. **List models:** audiobook `chapters`, subtitle `cues`, batch `items`,
   Studio `clips`/`ops` become `QAbstractListModel`s with row-level
   `dataChanged`/insert/remove; QML delegates keep their role names so
   existing `modelData` consumers are migrated, not broken. Scroll position
   survives updates.
5. **Ahead-of-time QML:** the PyInstaller spec precompiles QML
   (`pyside6-qmlcachegen` or rcc resources) so first launch after install
   skips runtime compilation.
6. **Startup import:** numpy is no longer imported before the first frame
   (deferred in `core.artifacts`, `audiobook_controller`,
   `stream_playback`), if the measured win holds (≥ 200 ms warm).

## Non-functional requirements

- **No behavior regressions:** existing tests stay green; audio artifacts
  from unchanged paths are byte-identical.
- **Bounded memory:** live transport stays ≤ 384,000 bytes; prefetch adds at
  most one segment of PCM; no duration-sized in-memory buffers.
- **No new runtime dependencies.** Only change: PySide6 floor → ≥ 6.9.
- **Testability:** GUI-thread offloads use the injectable `bg_runner`
  (`run_sync` in tests); new perf contracts get unit tests, and perf-sensitive
  changes get a `benchmark`-marked test or script entry.
- **Measurement:** every task records before/after numbers in its git note;
  phase-level figures land in `learnings.md`.

## Acceptance criteria

1. Int16 sink + live preview plays correct PCM (unit test on the IO device
   with an Int16 format) and the drain estimate uses 2 bytes/sample.
2. Audiobook export-all of a 30-chapter book never blocks the GUI thread
   (test: export runs through `bg_runner`; QML receives completion signal).
3. Qwen engine build with an unchanged install performs zero full-file
   hashes (test: hash function spy) and never runs inspection on the GUI
   thread; a tampered file (size/mtime change) triggers a full hash and the
   existing reinstall error. "Verify files" performs a full hash off-thread.
4. Qwen PCM decode ≥ 100× faster than baseline on a 0.5 s frame, identical
   output.
5. Keystroke in a 200k-char document triggers no Python metric call until
   the debounce fires; one call returns both metrics.
6. Chapter model rebuild at 300 chapters ≥ 10× faster than the 1.37 s
   baseline (benchmark test).
7. Subtitle dub unit processing and rate-slider rebuilds no longer run on
   the GUI thread / per step (tests on executor + slider release path).
8. A live job whose synthesis is faster than real time completes its
   artifact in less than its audio duration (fake provider test), with the
   live transport never exceeding its cap.
9. Qwen live: first segment ≤ 150 chars; next segment's synthesize is sent
   before the current segment finishes playing; cancel kills both (fake
   host tests).
10. Warmup leaves normalize/phonemize warm (second call under the cold
    threshold in a test with a timing seam).
11. WSOLA ≥ 3× faster on 60 s; parity within documented tolerance.
12. Settings first visit no longer blocks ≥ 100 ms on the GUI thread
    (async Loader + idle prebuild, startup benchmark updated); Paragraph and
    Studio lazy; startup `create_app` time does not regress.
13. Hidden waveforms perform zero paints during replay (QML driver count).
14. Tuning knobs exist, default values unchanged, a tuning evidence record
    exists in `docs/performance/`, and bead `1v6` is closed or superseded.
15. Full gate green: `ruff check .`, `ruff format --check .`, `pytest`
    (the documented device-less `VieNeuTTSApp-3iy` failure excepted).

## Out of scope

- GGUF CPU thread count — the pinned `qwentts.cpp` fork's ABI has no thread
  field; needs an upstream/fork ABI change → follow-up bead.
- GGUF Base reference-prefill reuse (upstream prefix-KV reuse).
- Audio *quality* items found in passing: resampler anti-imaging filter,
  stateful streaming WSOLA (per-chunk 2 ms fade notches) → follow-up beads.
- Real-hardware CUDA/Metal verification beyond what the benchmark-gated
  tuning records; those cells remain beads.
- Job-id tagging of worker signals (bead `n23`) — unrelated correctness work.
