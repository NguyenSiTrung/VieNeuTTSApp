# Spec: phase03_corefeat_20260827

## Track

Implements **Phase 3** ("Core features") from [PROJECT_PLAN.md](../../../PROJECT_PLAN.md)
§19; feature detail §7.1–7.4. Builds on the archived tracks
`phase01_core_20260827` (headless engine) and `phase02_uishell_20260827`
(QML shell with placeholder tabs).

## Overview

Wire the four Phase 2 placeholder tabs to the Phase 1 headless engine. A new
`AppController` (`ui/controller.py`) owns the `InferenceWorker` lifecycle and
exposes synthesis state to QML; the Text tab gets free-text TTS with the
grouped preset-voice picker, full playback and 48 kHz WAV export; the
Paragraph/File tab adds long-text input plus `.txt`/`.md`/`.docx`/`.pdf`
import with live progress and cancel; the Cloning tab enrolls named voices
from a 3–8 s reference clip behind a consent gate, with SDK voice
persistence redirected from site-packages into the app data dir (§21
resolution owned by this track); the Settings tab binds backend, precision,
default voice, output dir, temperature and theme, applying on next engine
init. Streaming playback stays in Phase 4 — this track ships full playback
only.

## Kickoff Decisions

- **Spec + plan confirmed as drafted** (2026-08-27).
- **Execution: parallel where file ownership allows** (Phase 2 precedent;
  disjoint file sets per task).
- **Priority: high** — critical path; Phases 4–6 build on these features.
- **Dependencies: none** — both prerequisite tracks complete and archived;
  knowledge inherits via `conductor/patterns.md` (28 patterns, refreshed
  from both archives).

## Functional Requirements

- **FR-3.1** Controller wiring: new `ui/controller.py` `AppController`
  (plain QObject, registered by `app.py` as a QML context property alongside
  the shell bridge, which stays nav/theme-only). Owns exactly one
  `InferenceWorker`/`TTSEngine` (lazy init per NFR-2.1 precedent); exposes
  the voice catalog (20 presets grouped North/Central/South plus cloned
  voices), `generate`/`cancel`, busy/progress/error state, and WAV export.
  Settings changes apply on next engine init, never mid-flight.
- **FR-3.2** Text tab (§7.1): multiline vi/en code-switching input; voice
  picker grouped North/Central/South; Generate → full playback
  (`QMediaPlayer` via a thin injectable wrapper) + 48 kHz WAV export to the
  user-chosen or default output dir; emotion-cues hint
  (`[cười] [thở dài] [hắng giọng]`).
- **FR-3.3** Paragraph/File tab (§7.2): long-text input; import `.txt`,
  `.md`, `.docx` (`python-docx`), `.pdf` (`pypdf>=6`) via a new
  `core/importers.py`; progress bar during synthesis; cancel button;
  chunking left to SDK `infer` (auto-chunking per §7.2).
- **FR-3.4** Cloning tab (§7.3): load a 3–8 s mp3/wav reference clip;
  optional denoise with preview (denoise returns 44.1 kHz — gotcha from
  phase01); name the voice; `add_voice` enrollment; consent gate shown
  before the first clone; enrolled voices appear in the voice pickers and
  persist across restarts via `save_voices(path=<app data dir>)` — never
  the SDK's site-packages default.
- **FR-3.5** Settings tab (§7.4): engine backend auto/ONNX(CPU)/torch(CUDA)
  with the detected-engine readout; precision int8/fp32; default voice;
  output directory; temperature; theme system/light/dark. Invalid
  selections are handled gracefully (reverted/ignored with feedback, no
  crash); backend/precision take effect on the next engine init.

## Non-Functional Requirements

- **NFR-3.1** UI thread never blocks: all synthesis goes through the
  existing worker thread; cancel is cooperative between chunks (§11); no
  model load at startup (extends NFR-2.1 to the wired app).
- **NFR-3.2** Existing quality gates stay green: `ruff check`,
  `ruff format --check`, `pytest` — the 186 Phase 1+2 tests must not
  regress.
- **NFR-3.3** Controller/importer logic is unit-tested with injectable
  fakes (phase01 pattern); QML tabs are verified by the offscreen
  pytest-qt smoke suite plus a manual pass — no pixel assertions in CI.
- **NFR-3.4** Fully offline: no network access for any Phase 3 flow
  (offline bundle validated in Phase 1).

## Acceptance Criteria

- **AC-1** Text flow: paste text → pick voice → generate → playback works;
  export produces a valid 48 kHz WAV in the chosen directory.
- **AC-2** File flow: import a multi-page PDF → synthesized WAV; progress
  increments during synthesis; cancel stops work promptly.
- **AC-3** Cloning flow: clone from a 3–8 s clip → synthesized speech
  matches reference timbre; enrolled voice appears in the voice list and
  persists across restart.
- **AC-4** Settings flow: changing backend applies on next init; invalid
  selections handled gracefully; theme control works live.
- **AC-5** Offscreen smoke suite extended for the wired tabs and green in
  the standard gate; real-model manual pass on the Linux workspace
  (cross-OS gaps recorded and deferred to Phase 6 CI, per Phase 0/1/2
  precedent).

## Out of Scope

- Streaming playback, `QAudioSink`, waveform/level indicator, ~300 ms
  first-audio target — Phase 4.
- Error/edge-case screens and consent-notice copy polish (basic consent
  gate IS in scope, §7.3) — Phase 4.
- Packaging, offline bundling, installers — Phase 5; cross-OS CI matrix —
  Phase 6.
- Any new model download or network fetch.
