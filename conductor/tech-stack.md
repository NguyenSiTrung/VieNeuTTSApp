# VieNeuTTS Desktop App — Tech Stack

> Documenting the **existing** stack from `PROJECT_PLAN.md` (brownfield).
> No proposed changes — verified against the plan.

## Language & Runtime
- Python >= 3.10 (classifiers 3.10–3.13).
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
- `.txt`/`.md` native; `.docx` via `python-docx`; `.pdf` via PyMuPDF
  (SDK `pdf` extra).

## Persistence
- `platformdirs.user_data_dir("VieNeuTTSApp")` + JSON (settings).

## SDK Entry Points (reference)
- `vieneu-web` (Gradio), `vieneu-stream` (FastAPI).

## Packaging & Distribution
- Per-OS installable artifacts: `.dmg` (macOS), `.msi`/`.exe` (Windows),
  `.deb`/AppImage (Ubuntu). Signed/notarized; CI green.
