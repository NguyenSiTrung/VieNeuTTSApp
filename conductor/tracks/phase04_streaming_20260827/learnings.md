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
  - Gotchas: real-model stream chunk counts vary run-to-run (codec adaptive buffering) — never assert exact chunk counts against the live model; worker temperature was not forwarded on stream mode — FIXED during Phase 2 (request.temperature now passed through infer_stream_chunked).
---

## [2026-08-27] - Phase 2 Task 1: Waveform indicator + Text tab streaming
- **Implemented:** `WaveformIndicator.qml` — rolling 48-bar peak envelope Canvas (level 0..1 in; no samples reach QML), newest bars right-anchored, flat hairline idle, no timers (requestPaint on level change). TextTab: Generate → generateStream, waveform visible/active bound to streamActive, level to streamLevel. Smoke: fake-controller binding flips + REAL-controller e2e through QML→worker→ring-buffer→levelReady.
- **Files changed:** src/vienetts_app/ui/qml/WaveformIndicator.qml; src/vienetts_app/ui/qml/TextTab.qml; tests/smoke/test_ui_tabs.py
- **Commit:** 853593c (+ TextTab audioAvailable gate)
- **Learnings:**
  - Gotchas: QML var-property signals do NOT fire on in-place array mutation (`samples.push(...)` leaves derived properties stale) — reassign a new array per push; cancel-before-worker-pickup drains the queue silently and busy sticks forever — tests must await evidence of processing before cancelling; triple-quoted docstrings must be escaped inside textwrap.dedent test drivers.
---

## [2026-08-27] - Phase 2 Task 3: Edge-case surfaces
- **Implemented:** controller `modelsMissing` (recomputed from is_models_missing(error_text) on every error transition; CANCELLED bypasses both set and clear) + `audioAvailable` (tri-state None=unprobed, lazy first read, injectable audio_probe, refreshAudioAvailability Slot emitting NOTIFY unconditionally). Main.qml fullscreen models-missing overlay with fetch-command hint + retry, global export-only banner. CloningTab consent copy polish + preview gated on audioAvailable.
- **Files changed:** src/vienetts_app/ui/controller.py; src/vienetts_app/ui/qml/Main.qml; src/vienetts_app/ui/qml/CloningTab.qml; tests/unit/test_controller.py; tests/smoke/test_ui_shell.py
- **Commit:** c8b4734
- **Learnings:**
  - Context: this host exposes ZERO pipewire output sinks offscreen — the audio-probe False branch is the natural path here; playback-flow tests must inject `audio_probe=lambda: True`.
  - Gotchas: initial QML binding evaluation happens during engine.load(), not after event-loop start (probe at load verified safe); cross-agent copy edits caused test ping-pong until the final copy pinned all partner phrases verbatim — pin shared strings by objectName and coordinate via orchestrator; subprocess GUI drivers using the REAL worker must shutdown() or rc=-6 teardown aborts.
---

## [2026-08-27] - Phase 2 Task 2: Paragraph/File tab streaming + orchestrator integration fixes
- **Implemented:** ParagraphTab → generateStream with mirrored waveform placement; visible oversize-import banner preferring the verbatim controller cap message; playButton gated on audioAvailable (absorbing P2T3's handoff). Orchestrator fixes: worker stream path forwards request.temperature (settings were silently ignored for streams); test_e2e_flows FakeVieneu gains an infer_stream twin (same call record, deterministic 3×800 chunks); e2e driver injects positive audio_probe.
- **Files changed:** src/vienetts_app/ui/qml/ParagraphTab.qml; tests/smoke/test_ui_tabs.py; (orchestrator) src/vienetts_app/workers/inference_worker.py; tests/smoke/test_e2e_flows.py
- **Commit:** 853593c + 408f2c7
- **Learnings:**
  - Gotchas: Qt defers `visible` binding updates inside a NON-current StackLayout tab offscreen (subtree state updates but visible reads stale) — setCurrentTab before asserting visibility; literal `\n` vs `\\n` inside nested textwrap.dedent driver strings breaks dedent into IndentationError in every subprocess; segmenter joins sentence whitespace (assert by content, not newline shape); compute oversize fixtures from len(unit)*n — one char short imports silently under the cap.
---
