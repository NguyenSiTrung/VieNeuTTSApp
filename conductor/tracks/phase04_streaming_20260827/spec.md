# Spec: phase04_streaming_20260827

## Track

Implements **Phase 4** ("Streaming & polish") from
[PROJECT_PLAN.md](../../../PROJECT_PLAN.md) §19; detail §5 (QAudioSink
streaming architecture), §8 (waveform indicator), §10 (audio pipeline),
§11 (error/edge cases). Builds on the archived tracks
`phase01_core_20260827` (headless engine + worker-side streaming
plumbing: `chunk_ready` + cooperative cancel), `phase02_uishell_20260827`
(QML shell) and `phase03_corefeat_20260827` (wired tabs, full playback,
consent gate, importers).

## Overview

Add live streaming playback (`infer_stream` → `QAudioSink`) to the Text
and Paragraph/File tabs with a shared waveform + progress indicator;
handle the remaining §11 edge cases (audio-device missing, oversized
imports, models-missing screen, ONNX arena memory growth per bead
`VieNeuTTSApp-u5c`); polish the consent-notice copy. Worker-side
streaming already ships from Phase 1 — this track adds the audio-sink
consumer, the controller streaming API, UI wiring on both tabs, and the
resilience work.

## Kickoff Decisions (2026-08-27)

- **Spec + plan confirmed as drafted**; execution **parallel where file
  ownership allows** (Phase 2/3 precedent; disjoint file sets per task).
- **Streaming surfaces: both Text and Paragraph/File tabs** — file
  imports play while synthesizing.
- **Indicator: waveform + progress combo** — rolling amplitude envelope
  fed by playback samples plus synthesis progress.
- **Edge cases: all four in scope**, including the ONNX arena
  mitigation (bead `VieNeuTTSApp-u5c`).
- **Priority: high** — Phase 5–6 build on this feature set.
- **Dependencies: none** — all prerequisite tracks complete and
  archived; knowledge inherits via `conductor/patterns.md`.

## Functional Requirements

- **FR-4.1** Streaming playback pipeline: new `ui/stream_playback.py` —
  `QAudioSink` with `QAudioFormat(48 000 Hz, 1 ch, Float32)` fed by a
  ring buffer; consumes worker `chunk_ready` signals; tolerates
  variable chunk sizes (~15 360–96 000 samples, spike §4); start/stop;
  sink injectable for offscreen tests (`QT_AUDIO_BACKEND=ffmpeg`
  pattern from phase03).
- **FR-4.2** Controller streaming API: stream mode through
  `AppController` — generate streams chunks to the sink as they arrive,
  `streamActive` state exposed to QML; full audio retained on `done`
  for replay (existing PlaybackController) and WAV export; cancel stops
  synthesis (cooperative, between chunks) and playback immediately.
- **FR-4.3** Text tab: Generate → streaming playback; first-audio
  ~300 ms target on CPU (§18); §6.2 heuristic stands (streaming →
  `backend="onnx"`).
- **FR-4.4** Paragraph/File tab: long text and imports synthesize and
  stream concurrently; progress stays live; cancel stops both.
- **FR-4.5** Shared waveform + progress QML component
  (`WaveformIndicator.qml`), active on both tabs during streaming.
- **FR-4.6** §11 edge cases:
  - (a) empty `QMediaDevices` audio outputs → playback controls
    disabled, export-only mode with a visible notice;
  - (b) imported documents capped at 200 000 chars with an actionable
    warning (§11 "oversized file import");
  - (c) missing model weights → typed engine error surfaced as a
    "models missing" screen pointing at `scripts/fetch_models.py`;
  - (d) long-document RSS mitigation (chunked dispatch via stream
    `max_chars` or session options if the SDK exposes them), measured
    against the §18 < 2 GB budget — closes or re-scopes bead
    `VieNeuTTSApp-u5c`.
- **FR-4.7** Consent-notice copy polish on the Cloning gate (legal
  warning wording, §15/§20).

## Non-Functional Requirements

- **NFR-4.1** UI thread never blocked; playback starts without waiting
  for full synthesis (extends NFR-3.1 to streaming).
- **NFR-4.2** Existing quality gates stay green: `ruff check`,
  `ruff format --check`, `pytest` — the 369 Phase 1–3 tests must not
  regress.
- **NFR-4.3** Injectable fakes (phase01 pattern; fakes at the SDK
  layer per phase03); QML verified by the offscreen pytest-qt smoke
  suite plus a real-model manual pass — no pixel assertions in CI.
- **NFR-4.4** Fully offline: no network access for any Phase 4 flow.

## Acceptance Criteria

- **AC-1** Short text on CPU streams with ~300 ms first audio
  (measured evidence recorded); full 48 kHz mono audio available at
  `done` for replay and export.
- **AC-2** Both tabs stream; cancel mid-stream stops synthesis and
  playback promptly.
- **AC-3** Waveform indicator live during streaming; progress accurate
  on both tabs.
- **AC-4** §11 edge cases handled: no audio device → export-only mode;
  oversized import → warning + refuse; missing weights → clear screen
  with fetch hint; consent copy polished.
- **AC-5** Long-document memory measurably reduced toward < 2 GB RSS
  (before/after evidence); bead `VieNeuTTSApp-u5c` closed or re-scoped
  with that evidence.
- **AC-6** Offscreen smoke suite extended for streaming + edge cases;
  real-model manual pass green; full gate green.

## Out of Scope

- Packaging, offline bundling, installers, mp3 decode cross-OS (bead
  `VieNeuTTSApp-vis`) — Phase 5.
- Cross-OS CI matrix — Phase 6.
- Any new model download or network fetch; GPU/torch streaming tuning
  (streaming stays ONNX/CPU per §6.2).
