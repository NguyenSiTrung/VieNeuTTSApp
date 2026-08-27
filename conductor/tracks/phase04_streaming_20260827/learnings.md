# Track Learnings: phase04_streaming_20260827

Patterns, gotchas, and context discovered during implementation.

## Codebase Patterns (Inherited)

📚 Full set: `conductor/patterns.md` (refreshed from phase01_core +
phase02_uishell + phase03_corefeat). Most load-bearing for this track:

- **Dev loop / gates**: `uv venv --python 3.13 .venv`, install
  `-e ".[dev]"`, gates via `.venv/bin/{ruff,pytest}`;
  `QT_QPA_PLATFORM=offscreen` for pytest; ruff + format + pytest all
  green before every commit (conventional prefix + `git notes add`
  summary).
- **Streaming contract** (spike §4): `infer_stream` yields float32
  1-D mono chunks, variable size ~15 360–96 000 samples @ 48 kHz;
  concatenate for full audio. It carries its own sampling defaults
  (temperature=0.8, top_k=25, top_p=0.95) and accepts `max_chars=256`
  — relevant to the arena-mitigation chunked dispatch.
- **Worker plumbing already exists**: `InferenceWorker._process_stream`
  emits `chunk_ready`/`progress`/`done`; cancel is cooperative between
  chunks (SDK cannot cancel mid-chunk). Streaming requests serialize
  through the single engine-owning worker thread.
- **QtMultimedia offscreen**: `QAudioOutput` construction deadlocks
  under pytest fd capture (pipewire devicemonitor probe) — set
  `QT_AUDIO_BACKEND=ffmpeg` in tests needing the real player;
  `QMediaPlayer` has no `resume()`; enum str() is
  "PlaybackState.PlayingState" — map via `.name`.
- **Memory**: ONNX arena grows with the largest workload and never
  returns to the OS — long-text synthesis plateaus ~2.5 GB RSS (budget
  breach, bead `VieNeuTTSApp-u5c`); interactive use ~766 MB.
  Measurement needs BOTH `ru_maxrss` and current RSS via
  `ps -o rss= -p PID`.
- **Qt threading**: cross-thread signals are queued to the receiver
  thread — tests pump `QCoreApplication.processEvents()` while
  waiting; coverage.py cannot trace QThread-run code, keep logic in
  directly callable methods.
- **QML integration**: anchor context-property objects on the engine;
  QML files use same-directory `import "."` for the Theme singleton
  (registered in qmldir); read QML-declared properties via
  `.property("name")`; Repeater delegates have no QObject parent —
  walk `contentItem().childItems()`.
- **GUI tests in pytest**: ONE QGuiApplication per process;
  GUI-object-tree assertions run in a subprocess (`sys.executable -c`
  + `RESULT:` json stdout line).
- **Testing fakes**: place the fake at the SDK layer (FakeVieneu
  implementing the spike §0 surface incl. generator `infer_stream`)
  and run the REAL controller/worker/engine/QML/playback code.
- **Consent gate already ships** (phase03): persisted acknowledgment
  in `<app data dir>/cloning_consent.json`; this track only polishes
  the copy.

---

<!-- Learnings from implementation will be appended below -->

## [2026-08-27] - Phase 1 Task 1: Streaming playback pipeline + controller streaming API
- **Implemented:** `ui/stream_playback.py` StreamPlaybackController — QAudioSink fed by a QIODevice ring buffer (`readData` pops, feed appends), 48 kHz/1ch/Float32, injectable sink+format factories (QtMultimedia imported only inside default factories), peak-amplitude `levelReady(float)` envelope for FR-4.5. Controller: `generateStream` slot, `streamActive`/`streamLevel` properties, chunk_ready→feed wiring, cancel/CANCELLED hard-stop the sink.
- **Files changed:** src/vienetts_app/ui/stream_playback.py; src/vienetts_app/ui/controller.py; tests/unit/test_stream_playback.py; tests/unit/test_controller.py
- **Commit:** 16742cc
- **Learnings:**
  - Patterns: QIODevice is importable from PySide6.QtCore — ring-buffer push device needs no QtMultimedia import; a "format factory" returning a configured object keeps fakes trivial (assert factory hand-off, pin real values in one smoke case).
  - Gotchas: connected sink signals must be optional (`getattr`) so minimal fakes work; underrun restart-on-feed keeps one start per session without complicating fakes.
---

## [2026-08-27] - Phase 1 Task 2: Import cap + audio-device probe
- **Implemented:** `IMPORT_CHAR_LIMIT = 200_000` post-extraction refusal in `import_document` (boundary: exactly limit passes); `audio_output_available(provider=None) -> bool` in ui/playback.py with injectable provider and lazy QMediaDevices import.
- **Files changed:** src/vienetts_app/core/importers.py; src/vienetts_app/ui/playback.py; tests/unit/test_importers.py; tests/unit/test_playback.py
- **Commit:** 0d3905b
- **Learnings:**
  - Patterns: module-level probe function (not a controller method) is the right seam — Phase 2 wires it into controller/QML without playback-controller coupling.
---

## [2026-08-27] - Phase 1 Task 3: Models-missing typed error
- **Implemented:** `ModelsMissingError(TTSEngineError)` + `MODELS_MISSING_MARKER` + `is_models_missing(message)` string seam (worker error signal carries str only). Classification grounded in a LIVE repro: empty HF_HOME + HF_HUB_OFFLINE=1 through real Vieneu raises huggingface_hub LocalEntryNotFoundError (MRO: →FileNotFoundError→OSError→EntryNotFoundError). Message names `python scripts/fetch_models.py`.
- **Files changed:** src/vienetts_app/core/engine.py; tests/unit/test_engine.py
- **Commit:** e3e33da (shared with P1T4)
- **Learnings:**
  - Gotchas: LocalEntryNotFoundError's message does NOT name the file/repo — classification, not message text, gives UI context; OfflineModeIsEnabled is a ConnectionError subclass so OSError-only checks are insufficient; generic `_ensure()` wrap runs after the torch branch — test weights-missing shapes with non-torch exceptions or the torch message wins.
---

## [2026-08-27] - Phase 1 Task 4: ONNX arena mitigation
- **Implemented:** `split_text_for_streaming(text, max_chars=DEFAULT_MAX_CHARS=512)` (sentence-boundary packing, ≤512 chars — at/below the SDK's own 256-char AR prefill cap so no extra workloads) + `TTSEngine.infer_stream_chunked`; worker `_process_stream` dispatches via module-level segmentation import (duck-typed engine fakes keep working), progress now segment-counted (0,N)→(N,N).
- **Files changed:** src/vienetts_app/core/engine.py; src/vienetts_app/workers/inference_worker.py; tests/unit/test_engine.py; tests/unit/test_inference_worker.py
- **Commit:** e3e33da
- **Learnings:**
  - Patterns: SDK infer_stream ALREADY bounds AR workloads to ~256-char chunks internally (normalize_to_chunks_v3); whole-doc RSS growth lives outside that bound. Session options unreachable cleanly (ort.SessionOptions constructed inside OnnxV3LiteEngine.__init__; only `threads` plumbs through) — chunked app-level dispatch chosen instead.
  - Context / AC-5 evidence (real model, CPU int8, ru_maxrss + ps): whole-text single infer_stream @2.6k chars peaked 937 MB; chunked holds ~785 MB FLAT as doc length doubles (784 MB @ 5k chars, 11 segments). The recorded ~2.5 GB plateau matches the NON-stream `infer()` path — recommend follow-up bead for that path; stream path satisfies <2 GB budget.
  - Gotchas: real-model stream chunk counts vary run-to-run (codec adaptive buffering) — never assert exact chunk counts against the live model; worker temperature=None in stream mode preserved as legacy parity (SDK treats None as greedy argmax).
---
