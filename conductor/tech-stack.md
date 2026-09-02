# VieNeuTTS Desktop App — Tech Stack

> Documenting the **existing** stack from `PROJECT_PLAN.md` (brownfield).
> No proposed changes — verified against the plan.
<!-- refreshed 2026-09-02 against pyproject.toml/uv.lock (no dependency or dev-extra drift since 2026-08-31; PyInstaller still enters via the Release workflow only), new .github/workflows/ci.yml, and release.yml + packaging/linux/ (new since last refresh) -->

## Language & Runtime
- Python `>=3.10,<3.14` — SDK caps at 3.13; provision dev venvs via `uv venv
  --python 3.13` even when system python is newer.
- Single-process app; inference runs in a dedicated worker `QThread`.

## TTS Engine
- `vieneu==3.3.0` — torch-free ONNX Runtime on CPU (int8 backbone by
  default); optional CUDA via `torch`/`torchaudio`.
- Model: `pnnbao-ump/VieNeu-TTS-v3-Turbo` (48 kHz, vi + en
  code-switching), bundled offline. Backbone repo is configurable
  (2026-09-02): a Settings field overrides `TTSEngine`'s backbone repo
  (empty = official); `scripts/fetch_models.py --backbone owner/repo`
  fetches and manifests a custom repo for fully-offline use.

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
- `.epub` audiobook import is **stdlib-only** (`zipfile` +
  `xml.etree.ElementTree` with an `html.parser` fallback for malformed
  XHTML) — `ebooklib` was rejected because it drags in `lxml`
  (`audiobook_epub_20260828`).

## Persistence
- `platformdirs.user_data_dir("VieNeuTTSApp")` + JSON (settings).

## SDK Entry Points (reference)
- `vieneu-web` (Gradio), `vieneu-stream` (FastAPI).

## Build & Dev Tooling
- Build backend: hatchling (wheel packages `src/vienetts_app`); console
  script `vienetts-app` → `vienetts_app.__main__:main`.
- `[dev]` extra: pytest / pytest-cov / pytest-qt / pytest-xdist / ruff — install with
  `-e ".[dev]"`; gates run as `.venv/bin/{ruff,pytest}` (default addopts `-ra -n auto`).
- Ruff: line-length 100, target py310, rules E/F/W/I/UP/B/SIM; excludes
  `.agents`, `.beads`, `conductor`, `scripts/spike`, `*.md`.

## Packaging & Distribution
- **Per-push CI (2026-09-02, `.github/workflows/ci.yml`):** ruff + full
  suite (offscreen Qt) on ubuntu-22.04 + windows-latest for every push and
  PR — the two non-dev platforms; Ubuntu pins 22.04 to match the release
  glibc floor and apt package names. Linux runners install the GStreamer
  packages QtMultimedia plays through.
- **Shipped (2026-08-29):** tag-triggered 3-OS release pipeline
  (`.github/workflows/release.yml`, `v*` tags only). Per OS: quality gates → full
  pytest (offscreen Qt) → **PyInstaller** one-dir CPU build
  (`packaging/vienetts-app.spec`, `pyinstaller>=6,<7` installed
  in-workflow, not a project dep) → `--smoke` binary verified by
  `scripts/check_smoke_wav.py` → artifact upload (Windows/Linux zip, macOS
  `dmg`). A `v*` tag collects all three into a GitHub Release. Since
  2026-09-02 the Linux zip carries `share/linux/` (`.desktop` entry,
  hicolor icons, `install.sh`) for menu-entry install, and the Linux
  runner installs the same GStreamer set so packaged audio works in CI.
- Spec layout contract: `vieneu`/`vieneu_utils`/`sea_g2p`/
  `kaldi_native_fbank` data trees land inside the frozen `vienetts_app`
  package at the same relative layout, so no frozen-mode code paths are
  needed; torch/transformers excluded (CPU build stays torch-free).
- **Not yet:** offline model bundling, signing/notarization (macOS build
  is ad-hoc codesigned — no Apple Developer ID), `.msi`/`.deb`/AppImage
  installers (Linux has the `install.sh` stopgap only).
