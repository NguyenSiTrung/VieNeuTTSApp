# VieNeuTTS Desktop App — Tech Stack

> Documenting the **existing** stack from `PROJECT_PLAN.md` (brownfield).
> No proposed changes — verified against the plan.
<!-- refreshed 2026-09-10: no pyproject/uv.lock dep drift (vieneu 3.3.0, PySide6 6.11.2, app v0.1.14); v0.1.9 -cuda bundles withdrawn in v0.1.11 for managed CUDA runtime; MP3 export via libsndfile (no new dep); benchmark marker exclusion; release notes v0.1.1–v0.1.14 -->
<!-- refreshed 2026-09-10 (2): CI now main-pushes + PRs only (feature-branch pushes excluded); v0.1.14 pro-audio Studio deck; Paragraph tab document/queue mode split; Settings engine/CUDA/model-source card split; test items 872 collected / 860 selected (12 benchmark deselected) -->

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
- NVIDIA CUDA only; Windows x64 and Linux x64 support the app-managed runtime
  (`core/cuda_runtime.py` + pinned records in `core/cuda_runtime_manifest.py`,
  v0.1.11): opt-in Settings download of the verified PyTorch runtime into the
  per-user data dir, offline after install, with diagnostics/removal.
  Apple Silicon / AMD / iGPU → ONNX/CPU. The app uses only the
  checksum-verified per-user runtime; it does not execute locally discovered
  Python environments. History: v0.1.9 briefly shipped `-cuda` download
  variants (~2 GB torch bundles per OS); withdrawn in v0.1.11 — all downloads
  are CPU-only again, CUDA is post-install opt-in.
- `scripts/lock_cuda_runtime.py` is a maintainer-only stdlib script. It runs
  pip's JSON-report resolver against official PyTorch/PyPI indexes, downloads
  the resolved direct wheel URLs, verifies their exact sizes and SHA-256
  digests, and renders sorted manifest records. The application never invokes
  the script or pip.

## UI Framework
- PySide6 + QML (Qt Quick / Qt6), GPU-rendered.
- `Theme.qml` design tokens; dark mode default.

## Audio
- QtMultimedia: `QAudioSink` (live preview), `QMediaPlayer` (artifact replay).
- `soundfile` (transitive via `vieneu`, not a direct dep) for WAV
  encode/decode + reference-clip decode. MP3 export (v0.1.12,
  `exportFormat` setting, WAV default) encodes via libsndfile through
  `core/audio.py` — no new direct dependency.
- Reading speed 0.5–2.0× via NumPy WSOLA + inter-paragraph pause 0–2.0 s
  (`core/audio.py`, v0.1.5); Mini Audio Studio post-synthesis op stack
  (v0.1.13, `core/studio.py`: gain/fade/speed/gap/normalize/trim +
  per-segment re-synthesis with 10 ms crossfade); WASAPI restart-storm
  guard on Windows. v0.1.14 redesigns the studio as a pro-audio deck:
  72 px master waveform deck with peak/RMS/dynamic-range telemetry, grouped
  FX rack (Dynamics / Tempo & Cadence / Transitions), op-stack breadcrumbs
  with contextual undo/reset, pause + click-drag waveform seek on the shared
  player, per-clip audition and in-dialog transcript editing, and
  off-GUI-thread render/export with stale-generation guards (`studioBusy` +
  per-op spinners).

## File Import
- `.txt`/`.md` native; `.docx` via `python-docx`; `.pdf` via **`pypdf`**
  (MIT — chosen over AGPL PyMuPDF at Phase 0; see
  `docs/spike-report.md` §7).
- `.epub` audiobook import is **stdlib-only** (`zipfile` +
  `xml.etree.ElementTree` with an `html.parser` fallback for malformed
  XHTML) — `ebooklib` was rejected because it drags in `lxml`
  (`audiobook_epub_20260828`).
- Multi-file batch queue (v0.1.7, `ui/batch_controller.py` +
  `BatchQueueCard.qml`): multi-select/drop onto Paragraph tab, off-thread
  import with oversize guard, sequential auto-run with per-file auto-export
  (`<stem>.wav`, collision-safe suffixing), failure-continue.
- Paragraph tab is a mode composition since 2026-09-10 (`0d684ba`):
  segmented `ModeTabs` ("Một tài liệu" | "Nhiều tệp") over mutually
  exclusive surfaces (`DocumentEditorCard` vs the queue panel) with a docked
  `SynthesisBar`, so Generate/Play/Export/Run-all never scroll away;
  `BatchQueueCard` is now a list-only queue panel.
- Cross-platform path normalization (v0.1.8, `core/paths.py`): QML `QUrl`
  forms, percent-decoding, quote-stripping, extended-length/UNC paths,
  reserved device names; BOM-safe (`utf-8-sig`) + universal-newline import.

## Persistence
- `platformdirs.user_data_dir("VieNeuTTSApp")` + JSON (settings, incl.
  `exportFormat` WAV/MP3 since v0.1.12). Diagnostic `crash.py` (v0.1.8):
  global excepthooks → timestamped `logs/crash.log` + native Windows dialog
  for windowed builds.

## SDK Entry Points (reference)
- `vieneu-web` (Gradio), `vieneu-stream` (FastAPI).

## Build & Dev Tooling
- Build backend: hatchling (wheel packages `src/vienetts_app`); console
  script `vienetts-app` → `vienetts_app.__main__:main`. Current version
  0.1.14.
- Synthesis pipeline (2026-09-03): immutable job values
  (`core/jobs.py`: SynthesisJob/JobChunk/JobTerminal) admitted via FIFO
  (`workers/job_queue.py`) to the single worker; incremental validated WAV
  writer (`core/artifacts.py`: `<job>.part.wav` → atomic promote) feeds
  both replay and export, with live preview through a bounded PCM transport
  (`core/pcm_transport.py`: 2 s / 384 KB cap, 150 ms prebuffer); reading
  speed 0.5–2.0× via NumPy WSOLA in `core/audio.py`.

- **CI (2026-09-02, `.github/workflows/ci.yml`; branch filter 2026-09-10):**
  `ruff check` + `ruff format --check` + suite (offscreen Qt,
  `QT_AUDIO_BACKEND=ffmpeg`) on ubuntu-22.04 + windows-latest for **pushes
  to `main` and pull requests only** — feature-branch pushes are excluded to
  cut noise, so open a PR (or push to `main`) for signal. The job is capped at
  `timeout-minutes: 8` and ignores `tests/smoke/test_performance_harness.py`.
  Ubuntu pins 22.04 to match the release glibc floor and apt package names.
  Linux runners install the GStreamer packages QtMultimedia plays through and
  remove the preinstalled Google Chrome apt source, which flakes with hash-sum
  mismatch failures.
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
  `packaging/release-notes/v0.1.1.md`–`v0.1.14.md`; windowed `.exe`
  stdio→devnull so packaged GUI builds can download + synthesize (184b600).
- **Shipped (2026-09-06…10):** in-app update checks (`core/updates.py`,
  v0.1.6: platform-aware GitHub Releases matching, variant-aware for the
  brief `-cuda` era, background recheck + Settings badge); pytest benchmark
  marker (`-m 'not benchmark'`, benchmarks opt-in only) + smoke/unit
  consolidation (1029 → 872 items collected / 860 selected, same-function
  micro-tests merged); Windows artifact zipping via streaming tar
  (Compress-Archive fails past 2 GB); `shell: bash` on all release steps
  (pwsh backslash parsing); manual CUDA-runtime activation spike
  (`.github/workflows/cuda-runtime-spike.yml`, workflow_dispatch).
- **Shipped (2026-09-10, v0.1.14):** pro-audio Studio deck (telemetry, FX
  rack, op-stack undo/reset, interactive transport, per-clip audition,
  off-thread renders with generation guards); Paragraph tab document/queue
  mode split with docked `SynthesisBar`; Settings engine/ CUDA/model-source
  card split + `Details & diagnostics` disclosure and a `controller.copyText`
  seam; 64-bit QML-facing byte counts + deterministic QML teardown
  (`5a578eb`); CI restricted to `main` pushes + PRs.
- **Not yet:** frozen-in model weights (by design — on-demand verified
  baseline instead), signing/notarization (macOS build
  is ad-hoc codesigned — no Apple Developer ID), `.msi`/`.deb`/AppImage
  installers (Linux has the `install.sh` stopgap only).
