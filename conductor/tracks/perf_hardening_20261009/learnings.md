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
