# VieNeuTTS Desktop App — Tech Stack

> Documenting the **existing** stack from `PROJECT_PLAN.md` (brownfield).
> No proposed changes — verified against the plan.
<!-- refreshed 2026-09-08: app v0.1.12; qwen install extra; managed CUDA runtime replaces -cuda bundles; MP3 via libsndfile (no new dep); cuda-runtime-spike.yml -->

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
- Multilingual engines (2026-09-08 track, archived): selectable VieNeu /
  Qwen3-TTS CustomVoice / Qwen3-TTS Base behind one `TtsBackend` contract
  (`core/tts_backend.py`, capabilities in `core/backends.py`,
  recommend/validate in `core/engine_selection.py`); all output normalized
  to mono 48 kHz (`core/resample.py` stateful resampler); engine-aware
  audiobook cache identity (`render.json` sidecar); Qwen weights via
  `core/qwen_models.py` + `scripts/fetch_qwen_models.py`
  (`docs/qwen-setup.md`); optional `qwen` install extra (`qwen-tts` +
  torch/torchaudio 2.8.0 + transformers 4.57.6) keeps the default install
  torch-free.

## GPU Dependency (optional)
- `torch==2.8.0` + `torchaudio==2.8.0` (cu128), CUDA >= 12.8.
- `transformers==4.57.6` (Qwen3 backbone + MOSS codec).
- NVIDIA CUDA only; Windows x64 and Linux x64 support the app-managed runtime.
  Apple Silicon / AMD / iGPU → ONNX/CPU. The app uses only the
  checksum-verified per-user runtime; it does not execute locally discovered
  Python environments.
- `scripts/lock_cuda_runtime.py` is a maintainer-only stdlib script. It runs
  pip's JSON-report resolver against official PyTorch/PyPI indexes, downloads
  the resolved direct wheel URLs, verifies their exact sizes and SHA-256
  digests, and renders sorted manifest records. The application never invokes
  the script or pip.
- Managed runtime (v0.1.11, `core/cuda_runtime.py` +
  `core/cuda_runtime_manifest.py`): opt-in download of the pinned verified
  PyTorch runtime into the per-user data dir (Windows/Linux x64, CUDA ≥
  12.8); downloads stay CPU-only by design. Supersedes the v0.1.9 `-cuda`
  bundle variants.

## UI Framework
- PySide6 + QML (Qt Quick / Qt6), GPU-rendered.
- `Theme.qml` design tokens; dark mode default.

## Audio
- QtMultimedia: `QAudioSink` (live preview), `QMediaPlayer` (artifact replay).
- `soundfile` (transitive via `vieneu`, not a direct dep) for WAV
  encode/decode + reference-clip decode.
- Reading speed 0.5–2.0× via NumPy WSOLA + inter-paragraph pause 0–2.0 s
  (`core/audio.py`, v0.1.5); WASAPI restart-storm guard on Windows.
- Export is WAV by default with user-choosable MP3 (v0.1.12,
  `core/audio.py`): MPEG Layer III via libsndfile — no ffmpeg dependency,
  no new package.

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
  0.1.12.
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
- **Shipped (2026-09-07):** `cuda-runtime-spike.yml` exercises dynamic
  managed-runtime activation; Windows artifacts zip via streaming tar
  (Compress-Archive fails past 2 GB); the torch cu128 install step runs
  under bash (pwsh cannot parse backslash continuations).
- Spec layout contract: `vieneu`/`vieneu_utils`/`sea_g2p`/
  `kaldi_native_fbank` data trees land inside the frozen `vienetts_app`
  package at the same relative layout, so no frozen-mode code paths are
  needed; torch/transformers excluded (CPU build stays torch-free).
- **Shipped (2026-09-08):** curated release notes per version in
  `packaging/release-notes/v0.1.1.md`–`v0.1.12.md`; windowed `.exe`
  stdio→devnull so packaged GUI builds can download + synthesize (184b600).
- **Releases v0.1.6–v0.1.12 (bead-driven, no track):** in-app update checks
  (`core/updates.py`) + `--version` (v0.1.6); batch synthesis queue
  (v0.1.7); crash diagnostics (`crash.py`) + path normalization
  (`core/paths.py`) + file-lock resilience (v0.1.8); `-cuda` bundles then
  managed CUDA runtime (`core/cuda_runtime.py`, v0.1.9/v0.1.11); MP3 export
  via libsndfile (`core/audio.py`, v0.1.12).
- **Not yet:** frozen-in model weights (by design — on-demand verified
  baseline instead), signing/notarization (macOS build
  is ad-hoc codesigned — no Apple Developer ID), `.msi`/`.deb`/AppImage
  installers (Linux has the `install.sh` stopgap only).
