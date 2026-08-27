# VieNeuTTS Desktop App — Tech Stack

> Documenting the **existing** stack from `PROJECT_PLAN.md` (brownfield).
> No proposed changes — verified against the plan.
<!-- refreshed 2026-08-27 against pyproject.toml (post phase03_corefeat) -->

## Language & Runtime
- Python `>=3.10,<3.14` — SDK caps at 3.13; provision dev venvs via `uv venv
  --python 3.13` even when system python is newer.
- Single-process app; inference runs in a dedicated worker `QThread`.

## TTS Engine
- `vieneu==3.3.0` — torch-free ONNX Runtime on CPU (int8 backbone by
  default); optional CUDA via `torch`/`torchaudio`.
- Model: `pnnbao-ump/VieNeu-TTS-v3-Turbo` (48 kHz, vi + en
  code-switching), bundled offline.

## GPU Dependency (optional)
- `torch==2.8.0` + `torchaudio==2.8.0` (cu128), CUDA >= 12.8.
- `transformers==4.57.6` (Qwen3 backbone + MOSS codec).
- NVIDIA CUDA only; Apple Silicon / AMD / iGPU → ONNX/CPU.

## UI Framework
- PySide6 + QML (Qt Quick / Qt6), GPU-rendered.
- `Theme.qml` design tokens; dark mode default.

## Audio
- QtMultimedia: `QAudioSink` (streaming), `QMediaPlayer` (full playback).
- `soundfile` for WAV encode/decode + reference-clip decode.

## File Import
- `.txt`/`.md` native; `.docx` via `python-docx`; `.pdf` via **`pypdf`**
  (MIT — chosen over AGPL PyMuPDF at Phase 0; see
  `docs/spike-report.md` §7).

## Persistence
- `platformdirs.user_data_dir("VieNeuTTSApp")` + JSON (settings).

## SDK Entry Points (reference)
- `vieneu-web` (Gradio), `vieneu-stream` (FastAPI).

## Build & Dev Tooling
- Build backend: hatchling (wheel packages `src/vienetts_app`); console
  script `vienetts-app` → `vienetts_app.__main__:main`.
- `[dev]` extra: pytest / pytest-cov / pytest-qt / ruff — install with
  `-e ".[dev]"`; gates run as `.venv/bin/{ruff,pytest}`.
- Ruff: line-length 100, target py310, rules E/F/W/I/UP/B/SIM; excludes
  `.agents`, `.beads`, `conductor`, `scripts/spike`, `*.md`.

## Packaging & Distribution
- Per-OS installable artifacts: `.dmg` (macOS), `.msi`/`.exe` (Windows),
  `.deb`/AppImage (Ubuntu). Signed/notarized; CI green.
