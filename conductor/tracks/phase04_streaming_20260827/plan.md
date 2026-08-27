# Plan: phase04_streaming_20260827

**Execution: parallel where file ownership allows** (kickoff decision).
Every task follows the workflow.md order: read patterns/learnings →
write failing tests first (where a Python contract changes) → implement
minimal → run `ruff check`, `ruff format --check`, `pytest` → commit
with a conventional prefix → attach a git note with the task summary →
append learnings. QML files are verified by the offscreen pytest-qt
smoke suite (NFR-4.3), not pixel assertions — QML authoring tasks are
not strictly test-first. Parallel tasks own disjoint files; commits are
serialized through the orchestrator (phase02/03 pattern: agents run
scoped tests, orchestrator runs the full gate).

## Phase 1: Foundations
<!-- execution: parallel -->
<!-- depends: -->

- [x] Task 1: Streaming playback pipeline + controller streaming API
  <!-- files: src/vienetts_app/ui/stream_playback.py, src/vienetts_app/ui/controller.py, tests/unit/test_stream_playback.py, tests/unit/test_controller.py -->
  - TDD: `ui/stream_playback.py` `StreamPlaybackController` —
    `QAudioSink` (48 kHz/1ch/Float32 `QAudioFormat`) fed by a ring
    buffer via `QIODevice`; `feed(chunk)` / `start()` / `stop()`;
    sink factory injectable for offscreen tests; tolerates variable
    chunk sizes (FR-4.1)
  - Controller: stream-mode requests through the worker, forward
    `chunk_ready` → sink, `streamActive` state, retain concatenated
    audio on `done` for replay/export, cancel stops synthesis +
    playback (FR-4.2)
  - Fakes at the SDK layer (FakeVieneu with generator `infer_stream`);
    pump `processEvents()` (phase01 pattern)

- [x] Task 2: Import cap + audio-device probe
  <!-- files: src/vienetts_app/core/importers.py, src/vienetts_app/ui/playback.py, tests/unit/test_importers.py, tests/unit/test_playback.py -->
  - TDD: 200 000-char cap in `core/importers.py` with an actionable
    warning error (refuse, not truncate) (FR-4.6b)
  - `ui/playback.py`: `QMediaDevices` audio-output availability probe
    exposed for the controller/QML (FR-4.6a core); device list
    injectable for offscreen tests

- [x] Task 3: Models-missing typed error
  <!-- files: src/vienetts_app/core/engine.py, src/vienetts_app/core/models.py, tests/unit/test_engine.py, tests/unit/test_models.py -->
  - TDD: lazy-init failure when weights are absent (HF cache missing,
    offline) → typed `ModelsMissingError` with an actionable message
    naming `scripts/fetch_models.py`; worker error path carries the
    type through to the controller (FR-4.6c core)

- [x] Task 4: ONNX arena mitigation
  <!-- files: src/vienetts_app/core/engine.py, src/vienetts_app/workers/inference_worker.py, tests/unit/test_engine.py, tests/unit/test_inference_worker.py -->
  <!-- depends: task3 -->
  - Investigate + implement: chunked dispatch via `infer_stream`
    `max_chars` (spike §4) or session options if the SDK exposes them;
    goal = long-document RSS back under the §18 2 GB budget (FR-4.6d,
    AC-5)
  - RSS measurement harness reusing the phase01 pattern (ru_maxrss +
    current RSS via `ps`); before/after numbers recorded in learnings

## Phase 2: Streaming UI & Edge-Case Surfaces
<!-- execution: parallel -->
<!-- depends: phase1 -->

- [x] Task 1: Waveform indicator + Text tab streaming
  <!-- files: src/vienetts_app/ui/qml/WaveformIndicator.qml, src/vienetts_app/ui/qml/TextTab.qml, tests/smoke/test_ui_tabs.py -->
  - TDD-ish (QML smoke, not test-first): shared
    `WaveformIndicator.qml` — rolling amplitude envelope (QML canvas)
    fed by playback samples + synthesis progress bar (FR-4.5)
  - Text tab: Generate → stream to sink; indicator live; replay +
    export still available from retained audio (FR-4.3, AC-1/AC-3)

- [x] Task 2: Paragraph/File tab streaming
  <!-- files: src/vienetts_app/ui/qml/ParagraphTab.qml, tests/smoke/test_ui_tabs.py -->
  <!-- depends: task1 -->
  - Long text + imports stream while synthesizing; progress stays
    live; cancel stops both; oversized-import warning UI from Phase 1
    Task 2 surfaced (FR-4.4, FR-4.6b, AC-2)

- [x] Task 3: Edge-case surfaces
  <!-- files: src/vienetts_app/ui/controller.py, src/vienetts_app/ui/qml/Main.qml, src/vienetts_app/ui/qml/CloningTab.qml, tests/unit/test_controller.py, tests/smoke/test_ui_shell.py -->
  - "Models missing" screen (typed error from Phase 1 Task 3) with
    fetch-script hint (FR-4.6c)
  - Export-only mode notice when the device probe finds no audio
    output; playback controls hidden/disabled (FR-4.6a)
  - Consent-notice copy polish on the Cloning gate (FR-4.7)

## Phase 3: Integration & Close-out
<!-- execution: sequential -->
<!-- depends: phase2 -->

- [ ] Task 1: Offscreen e2e smoke + real-model measurements
  <!-- files: tests/smoke/test_ui_tabs.py, tests/smoke/test_ui_shell.py -->
  - Fake-engine e2e through the real QML shell: streaming on both
    tabs, cancel mid-stream, waveform state, all §11 edge cases
    (AC-2/AC-4, NFR-4.3)
  - Real-model pass (offscreen CPU int8): first-audio latency vs the
    ~300 ms target (AC-1); long-doc RSS before/after mitigation
    (AC-5); full gate green (ruff + format + pytest)

- [ ] Task 2: Learnings, elevation, bead disposition
  <!-- files: conductor/tracks/phase04_streaming_20260827/learnings.md -->
  - Real-model manual pass on the Linux workspace (cross-OS gaps stay
    deferred to Phase 6, per precedent); append learnings; elevate
    reusable patterns to `conductor/patterns.md`
  - Close or re-scope bead `VieNeuTTSApp-u5c` with the AC-5 evidence;
    record any new gaps as beads for Phase 5/6
