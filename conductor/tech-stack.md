# VieNeuTTS Desktop App — Tech Stack

> Documenting the **existing** stack from `PROJECT_PLAN.md` (brownfield).
> No proposed changes — verified against the plan.
<!-- refreshed 2026-09-04: no pyproject/uv.lock dep drift (vieneu 3.3.0, PySide6 6.11.2, app v0.1.7); added in-app update checks via GitHub Releases, artifact-first synthesis pipeline (jobs/artifacts/pcm_transport/job_queue), pinned model manifest + ModelManager, per-push CI now ruff format --check + QT_AUDIO_BACKEND=ffmpeg, release notes v0.1.1–v0.1.7 -->

## Language & Runtime
- Python `>=3.10,<3.14` — SDK caps at 3.13; provision dev venvs via `uv venv
  --python 3.13` even when system python is newer.
- Single-process app; inference runs in a dedicated worker `QThread`.

## TTS Engine
- `vieneu==3.3.0` — torch-free ONNX Runtime on CPU (int8 backbone by
  default); optional CUDA via `torch`/`torchaudio`.
- Model: `pnnbao-ump/VieNeu-TTS-v3-Turbo` (48 kHz, vi + en
  code-switching), installed on demand. Backbone repo is configurable
  (2026-09-02): a Settings field overrides `TTSEngine`'s backbone repo
  (empty = official); `scripts/fetch_models.py --backbone owner/repo`
  fetches and manifests a custom repo for fully-offline use. Since
  2026-09-03 `scripts/fetch_models.py` derives repos, revisions, and file
  lists from `core/official_model_manifest.py`, and first-run install goes
  through `core/model_manager.py` (SHA-256-pinned official CPU baseline
  ~330 MB backbone + codec, staging-only install, atomic promote, resume,
  free-space preflight, Windows MAX_PATH/long-path handling) — weights are
  NOT frozen into the build by design; offline `backbone/`+`codec/` pack
  import via Settings.

## GPU Dependency (optional)
- `torch==2.8.0` + `torchaudio==2.8.0` (cu128), CUDA >= 12.8.
- `transformers==4.57.6` (Qwen3 backbone + MOSS codec).
- NVIDIA CUDA only; Apple Silicon / AMD / iGPU → ONNX/CPU.

## UI Framework
- PySide6 + QML (Qt Quick / Qt6), GPU-rendered.
- `Theme.qml` design tokens; dark mode default.

## Audio
- QtMultimedia: `QAudioSink` (live preview), `QMediaPlayer` (artifact replay).
- `soundfile` (transitive via `vieneu`, not a direct dep) for WAV
  encode/decode + reference-clip decode.
- Reading speed 0.5–2.0× via NumPy WSOLA + inter-paragraph pause 0–2.0 s
  (`core/audio.py`, v0.1.5); WASAPI restart-storm guard on Windows.

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
  script `vienetts-app` → `vienetts_app.__main__:main`. Current version
  0.1.7.
- Synthesis pipeline (2026-09-03): immutable job values
  (`core/jobs.py`: SynthesisJob/JobChunk/JobTerminal) admitted via FIFO
  (`workers/job_queue.py`) to the single worker; incremental validated WAV
  writer (`core/artifacts.py`: `<job>.part.wav` → atomic promote) feeds
  both replay and export, with live preview through a bounded PCM transport
  (`core/pcm_transport.py`: 2 s / 384 KB cap, 150 ms prebuffer); reading
  speed 0.5–2.0× via NumPy WSOLA in `core/audio.py`.

- **Per-push CI (2026-09-02, `.github/workflows/ci.yml`):** `ruff check` +
  `ruff format --check` + full suite (offscreen Qt,
  `QT_AUDIO_BACKEND=ffmpeg`) on ubuntu-22.04 + windows-latest for every
  push and PR — the two non-dev platforms; Ubuntu pins 22.04 to match the
  release glibc floor and apt package names. Linux runners install the
  GStreamer packages QtMultimedia plays through.
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
- **Shipped (2026-09-04):** curated release notes per version in
  `packaging/release-notes/v0.1.1.md`–`v0.1.7.md`; windowed `.exe`
  stdio→devnull so packaged GUI builds can download + synthesize (184b600).
- **Not yet:** frozen-in model weights (by design — on-demand verified
  baseline instead), signing/notarization (macOS build
  is ad-hoc codesigned — no Apple Developer ID), `.msi`/`.deb`/AppImage
  installers (Linux has the `install.sh` stopgap only).
