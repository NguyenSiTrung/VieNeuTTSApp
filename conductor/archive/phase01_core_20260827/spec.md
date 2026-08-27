# Spec: phase01_core_20260827

## Track

Implements **Phase 0** ("Spike & environment validation") and **Phase 1**
("Core engine (headless)") from [PROJECT_PLAN.md](../../../PROJECT_PLAN.md) §19.

## Overview

Validate the `vieneu==3.3.0` SDK against real hardware and the plan's
assumptions (Phase 0), then build the headless core engine — data models,
engine detection, settings, audio helpers, `TTSEngine`, a threaded inference
worker, and a `--smoke` CLI — with no UI (Phase 1). This track is the
foundation every later phase (UI shell, features, streaming, packaging)
depends on.

Source of truth: PROJECT_PLAN.md §3 (ground truth), §6 (auto-detection),
§9 (data model), §10 (audio pipeline), §12 (structure), §13 (pinned deps),
§16 (testing strategy), §18 (performance budgets), §21 (open questions).

## Functional Requirements

### Phase 0 — Spike & environment validation

- **FR-0.1** Install the CPU (torch-free) dependency set `vieneu==3.3.0` on
  macOS, Windows 10/11, and Ubuntu 22.04+; record the environment matrix
  (Python version, wheel availability, install issues).
- **FR-0.2** Confirm the synthesis contract on CPU/ONNX: `infer()` returns
  `np.float32 @ 48 kHz` with duration > 0 and non-silent audio;
  `list_preset_voices()` returns ≥ 20 entries (capture label fields for
  North/Central/South grouping); `add_voice()` + infer with a cloned voice;
  `tts.save()` writes a valid WAV.
- **FR-0.3** Confirm `infer_stream()` chunk format: dtype, shape, channel
  count; measure first-chunk latency (~300 ms expected) and RTF.
- **FR-0.4** Measure: RTF (short + long text), cold-start model load, warm
  synthesis latency, resident memory — compare against §18 budgets.
- **FR-0.5** Confirm the offline approach: loading from a local model path
  and running with `HF_HUB_OFFLINE=1` (weights pre-seeded); identify the
  minimal set of files/dirs needed for a CPU int8 bundle.
- **FR-0.6** Resolve open questions from §21 that affect Phases 0–1: PDF
  import library (PyMuPDF AGPL vs MIT `pypdf`), mp3 reference-clip decode
  via `soundfile`, whether the SDK exposes a temperature parameter,
  mono/stereo channel count, cloned-voice persistence across restarts, and
  the thread-safety assumption.
- **FR-0.7** Deliver `docs/spike-report.md`: environment matrix,
  measurements, decisions, and the confirmed API contract that Phase 1 codes
  against.

### Phase 1 — Core engine (headless)

- **FR-1.1** Package scaffold per §12: `pyproject.toml` (app package + pinned
  CPU deps + dev deps + console script), `src/vienetts_app/` layout,
  `tests/{unit,integration,smoke}/`, ruff config, `.gitignore`.
- **FR-1.2** `core/models.py`: `EngineInfo`, `Settings`, `TTSRequest`,
  `TTSProgress` per §9, with input validation.
- **FR-1.3** `core/detector.py`: hardware → engine detection matrix (§6.1) +
  workload heuristic (§6.2) + user-override resolution. Mirrors SDK logic for
  display and heuristic purposes only — the SDK remains the source of truth
  for the actual engine pick.
- **FR-1.4** `core/settings.py`: load/save `Settings` to
  `platformdirs.user_data_dir("VieNeuTTSApp")` as JSON; defaults on first
  run; graceful handling of corrupt/missing files.
- **FR-1.5** `core/audio.py`: WAV encode/export helpers (48 kHz float32 →
  WAV, in-memory and file) and format-conversion utilities.
- **FR-1.6** `core/engine.py`: `TTSEngine` owning a single
  lazily-initialized `Vieneu` instance; wraps `infer`, `infer_stream`,
  `infer_batch`, `add_voice`, `denoise`, `save` per the confirmed Phase 0
  contract.
- **FR-1.7** `workers/inference_worker.py`: dedicated QThread worker +
  thread-safe request queue; requests serialized; cooperative cancel flag
  checked between chunks/segments; signals: `progress`, `chunkReady`, `done`,
  `error`.
- **FR-1.8** `__main__.py`: `python -m vienetts_app --smoke "Xin chào"
  --voice Adam -o out.wav` — headless end-to-end synthesis.

## Non-Functional Requirements

- **NFR-1** CPU build stays torch-free; GPU stack only via an optional
  `[gpu]` extra.
- **NFR-2** All inference runs off the (future) UI thread — a single worker
  owns the `Vieneu` instance; requests are serialized.
- **NFR-3** Measurable §18 budgets recorded in the spike report: cold start
  < 15 s, warm short-text latency < 1 s, streaming RTF < 1, RSS < 2 GB.
- **NFR-4** Coverage ≥ 80% (line) on `src/vienetts_app` core + workers;
  `ruff check`, `ruff format --check`, `pytest` all green (workflow.md gate).
- **NFR-5** Cross-platform code (`pathlib`, `platformdirs`); no OS-specific
  branches in core modules.

## Acceptance Criteria

- **AC-1** (Phase 0) Headless synthesis works via a scripted spike on macOS,
  Windows, and Ubuntu; measured numbers recorded in `docs/spike-report.md`.
- **AC-2** (Phase 0) API contract documented and confirmed: signatures,
  return dtypes/shapes, stream chunk format, voice-list fields.
- **AC-3** (Phase 1) `python -m vienetts_app --smoke "Xin chào" --voice Adam
  -o out.wav` exits 0 and writes a valid 48 kHz WAV.
- **AC-4** (Phase 1) Unit tests green: detector matrix with mocked
  torch/CUDA, settings round-trip, audio WAV encode; ≥ 80% line coverage on
  core.
- **AC-5** (Phase 1) Cancel flag stops a long synthesis cooperatively
  (between chunks/segments).

## Out of Scope

- Any QML/UI work (Phase 2+).
- Streaming playback via `QAudioSink` + waveform UI (Phase 4) — Phase 1 only
  defines the `chunkReady` signal contract.
- PyInstaller/installers, model bundling into artifacts, signing/notarization
  (Phase 5).
- CI matrix (Phase 6).
- GPU/torch code paths beyond detection and heuristic handling of torch
  presence.
- MP3/AAC export.

## Assumptions & Constraints

- macOS (Apple Silicon) is the primary development machine; Windows/Ubuntu
  validation (FR-0.1, AC-1) runs when those environments are available and
  may be completed via CI later — the spike report records which OSes are
  confirmed.
- Phase 0 needs one-time network access to download weights (~240 MB CPU
  int8); the app itself stays offline afterwards.
- `Vieneu` is assumed not thread-safe (plan §4) — this is validated by the
  spike, not relaxed.
