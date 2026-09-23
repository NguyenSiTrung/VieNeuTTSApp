# VieNeuTTS Desktop App — Tech Stack

> Documenting the **existing** stack from `PROJECT_PLAN.md` (brownfield).
> No proposed changes — verified against the plan.
<!-- refreshed 2026-09-10: no pyproject/uv.lock dep drift (vieneu 3.3.0, PySide6 6.11.2, app v0.1.14); v0.1.9 -cuda bundles withdrawn in v0.1.11 for managed CUDA runtime; MP3 export via libsndfile (no new dep); benchmark marker exclusion; release notes v0.1.1–v0.1.14 -->
<!-- refreshed 2026-09-10 (2): CI now main-pushes + PRs only (feature-branch pushes excluded); v0.1.14 pro-audio Studio deck; Paragraph tab document/queue mode split; Settings engine/CUDA/model-source card split; test items 872 collected / 860 selected (12 benchmark deselected) -->
<!-- refreshed 2026-09-14: no pyproject/uv.lock dep drift (vieneu 3.3.0, PySide6 6.11.2, app v0.1.14); SRT dub/transcript studio added (stdlib-only .srt parse + in-app WSOLA rate fit — no new direct dep); Paragraph tab gains a third "Phụ đề (SRT)" mode; test items 1036 collected / 1024 selected (12 benchmark deselected) -->
<!-- refreshed 2026-09-21: track `qwen_multiengine_20260920` implemented on `main` (unreleased, app v0.1.16) — optional isolated Qwen 0.6B engine profiles: their dependency stack (qwen-tts 0.1.1 / transformers 4.57.3 / torch 2.8.0 CPU/CUDA/MPS) lives ONLY in the app-managed runtime install, never in the root pyproject/uv.lock (verified: no dep drift); app-side additions are `core/engine_profiles.py`, `core/synthesis_context.py`, `core/text_segmentation.py`, `core/voice_profiles.py`, `core/qwen_protocol.py`, `core/qwen_engine.py`, `core/qwen_runtime.py`, `core/qwen_model_manager.py` + their pinned manifests, `workers/qwen_host.py`, the `EngineState`/`EngineProfilePicker`/`LanguagePicker` QML components, maintainer `scripts/lock_qwen_runtime.py` + `scripts/fetch_qwen_models.py`, the opt-in `scripts/qwen_release_smoke.py` + `.github/workflows/qwen-runtime-smoke.yml`, and the spec's Qwen-stack excludes + `--qwen-host` re-dispatch; test items 1732 collected / 1720 selected (12 benchmark deselected) -->

<!-- refreshed 2026-09-16: no pyproject/uv.lock dep drift (vieneu 3.3.0, PySide6 6.11.2, pypdf 6.16.2, app v0.1.16); v0.1.15/v0.1.16 Studio tabs are QML-only + stdlib (no new direct dep) — StudioTab decomposed into StudioRackModule/StudioParamRow/StudioClipRow, AppCard gains clickable/cardHovered/cardClicked, AppNumberField added for precise numeric FX entry; test items 1055 collected / 1054 selected (12 benchmark deselected), gate 1054 passed + 1 device-dependent real-QAudioSink host failure (bead VieNeuTTSApp-3iy) -->

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
- `torch==2.8.0` + `torchaudio==2.8.0` (cu128), CUDA >= 12.0 (the cu128 wheels
  bundle their CUDA runtime, so CUDA 12.x minor-version compatibility applies;
  any driver >= R525.60.13 Linux / R527.41 Windows runs them).
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

## Optional Qwen Engines (managed, isolated — track `qwen_multiengine_20260920`)

Two opt-in engine profiles beside VieNeu (`core/engine_profiles.py` is the
capability table: `vieneu` | `qwen_custom_0_6b` | `qwen_base_0_6b`, each with
its voices/languages/devices/cloning requirements):

- **Model + runtime are installs, not dependencies.** The Qwen stack
  (`qwen-tts==0.1.1`, `transformers==4.57.3`, `accelerate==1.12.0`,
  `soundfile`, `soxr`, `torch`/`torchaudio==2.8.0` — `+cpu`, `+cu128`, or the
  PyPI macOS wheel for MPS) lives ONLY inside the per-user app-managed runtime
  (`core/qwen_runtime.py` + pinned wheel records in
  `core/qwen_runtime_manifests.json`, rendered by maintainer-only
  `scripts/lock_qwen_runtime.py`). Models are separate checksum-pinned installs
  (`core/qwen_model_manager.py` + `core/qwen_model_manifests.json` from
  `scripts/fetch_qwen_models.py`) for
  `Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice` and `…-0.6B-Base`, sharing one
  tokenizer tree. Neither enters `pyproject.toml`/`uv.lock` and neither is
  frozen into the bundle (the PyInstaller spec excludes them and the packaged
  binary re-dispatches itself as the host via `--qwen-host`).
- **Inference is a subprocess.** `workers/qwen_host.py` runs in the managed
  runtime interpreter and speaks a framed JSON/PCM protocol
  (`core/qwen_protocol.py`); `core/qwen_engine.py` owns its lifecycle
  (spawn/hello/load/stream/cancel/terminate/kill/reap, lazy restart after a
  crash, offline environment, windowless spawn on Windows). The host resamples
  the model's native 24 kHz to the app's 48 kHz incrementally, so the app keeps
  one PCM contract (`core/engine_profiles.py`: `QWEN_SOURCE_RATE` →
  `APP_SAMPLE_RATE`).
- **One submission contract.** `core/synthesis_context.py` carries the
  immutable engine context (profile, language, speaker/clone, model revision)
  on every job and decides cache/Studio reuse; `core/voice_profiles.py` keeps
  clones profile-scoped; `core/text_segmentation.py` bounds Qwen segments.
  QML derives all capability state from one singleton (`EngineState.qml`).
- **Validation.** Ordinary CI never downloads weights: fake-host unit suites
  plus consolidated subprocess scenarios in `tests/smoke/test_e2e_flows.py`
  cover the flows. The real model is exercised only by the opt-in
  `scripts/qwen_release_smoke.py` and `.github/workflows/qwen-runtime-smoke.yml`
  matrix (see `docs/performance/qwen-runtime-compatibility.md`).

## GGUF format variant (native `qwentts.cpp` — track `qwen_gguf_engine_20260923`)

Both Qwen profiles additionally offer a GGUF variant — same profiles, same
capability table, different format/engine pair
(`core/qwen_variants.py`: *official* → `pytorch`, *gguf* + `Q8_0`/`Q4_K_M` →
`qwentts.cpp`):

- **Native runtime is a managed pack, not a dependency.** `libqwen` + the
  ggml backend modules are built per platform cell by
  `scripts/build_qwen_gguf_runtime.py`, locked by
  `scripts/lock_qwen_gguf_runtime.py` into
  `core/qwen_gguf_runtime_manifests.json` (six locked cells; only built and
  probed cells publish a recipe — today `linux-x64-cpu` only), and installed
  offline-verified by `core/qwen_gguf_runtime.py`. No PyTorch, no compilers,
  no `PATH`/`cwd` library discovery — the engine locates the library only
  inside the verified promoted pack.
- **Models are per-variant GGUF pairs.** `core/qwen_gguf_models.py` +
  `core/qwen_gguf_model_manifests.json` pin four talkers
  (`Serveurperso/Qwen3-TTS-GGUF` @ `b7ee2e8c`) plus two shared tokenizer/codec
  GGUFs — one codec per quantization shared by both profiles.
- **Same subprocess contract, native flavor.** `workers/qwen_gguf_host.py`
  reuses the framed JSON/PCM protocol; `workers/qwen_gguf_abi.py` is the
  ctypes boundary (loaded only inside the host, never in the GUI process);
  `core/qwen_gguf_engine.py` extends `QwenEngine` — it spawns the host with
  `cwd=<pack dir>` so ggml's `backend_init` finds its modules, and the load
  frame carries format/quantization/device/runtimeDir/talkerPath/codecPath.
  Missing/incomplete packs surface as `runtime_incomplete` (Settings Repair),
  not crashes.
- **Provenance is variant-aware.** `SynthesisContext` fingerprints carry the
  resolved device plus content-addressed runtime/model/tokenizer identities,
  so caches, Studio clips, and exports never replay across formats or
  quantizations; Studio's regen offer restores the exact recorded variant.
- **Packaging.** The frozen binary re-dispatches as the GGUF host via
  `--qwen-gguf-host` alongside `--qwen-host`; the spec bundles the host/ABI/
  protocol modules and all four manifest JSONs but never native packs or
  weights.
- **Validation.** `tests/unit/qwen_gguf_host_fake.py` scripts the native host
  (including a `FAKE_HOST_BACKENDS` refusal injection); the opt-in
  `scripts/qwen_gguf_release_smoke.py` +
  `.github/workflows/qwen-gguf-runtime-smoke.yml` gate the 24-combination
  matrix with per-run identity/device/streaming/cancel/restart/shutdown
  evidence (see `docs/performance/qwen-gguf-compatibility.md`).

## UI Framework
- PySide6 + QML (Qt Quick / Qt6), GPU-rendered.
- `Theme.qml` design tokens; dark mode default.
- Shared component library in `ui/qml/components/`, registered in the root
  `qmldir` (subfolder components are declared with relative paths):
  `AppButton`, `AppCard` (whole-card tap targets since v0.1.16 via
  `clickable`/`cardHovered`/`cardClicked`), `AppCombo`, `AppSlider`,
  `AppNumberField` (precise clamped numeric entry synced both ways with its
  slider, v0.1.16), `AppToggle`, `AppIcon`, `AppIconButton` (square 40 px
  minimum hit target, v0.1.16), `AppNotice`, `SectionLabel`, `StatusBadge`,
  `EmotionChip`, `VoicePicker`, `PageShell`/`PageHeader`, and the Studio trio
  `StudioRackModule`/`StudioParamRow`/`StudioClipRow` extracted from
  `StudioTab.qml` in v0.1.16 (1757 → 1398 lines). Feature-surface components
  that are NOT in `qmldir` — `BatchQueueCard`, `DocumentEditorCard`,
  `SubtitleCard`, `ModeTabs`, `SynthesisBar` — are reached by the consuming
  tab's `import "components"` (QML resolves a component from the imported
  directory by filename); `qmldir` entries exist for the ones that must also
  be visible to the root module. Tabs import `"."` for `Theme` and
  `"components"` for these, never a bare absolute path.

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
- Paragraph tab is a mode composition since 2026-09-10 (`0d684ba`, third mode
  2026-09-14): segmented `ModeTabs` ("Một tài liệu" | "Nhiều tệp" |
  "Phụ đề (SRT)") over mutually
  exclusive surfaces (`DocumentEditorCard` vs the queue panel vs the subtitle
  card) with a docked
  `SynthesisBar`, so Generate/Play/Export/Run-all never scroll away;
  `BatchQueueCard` is now a list-only queue panel, and a mode switch keeps the
  page scrolled in place (`e255027`).
- SRT subtitle dub/transcript studio (2026-09-14, bead-driven, no track):
  `core/subtitles.py` (pure `.srt` cue model — parse, strip styling /
  `{\an8}`-style override blocks, collapse whitespace, re-emit; tolerant
  `HH:MM:SS,mmm` stamps), `core/align.py` (cue→clock fit planning: `dub`
  compresses an overlong take up to a `1.0–2.0×` rate cap and pushes later
  cues on overflow, `transcript` never compresses and caps inter-cue pauses;
  optional sentence-unit merging; NumPy WSOLA reuses the app's
  reading-speed bounds), `core/subtitle_project.py` (workspace store +
  streaming `SubtitleTrackRenderer` writing `PCM_16` `track.wav` clip-by-clip,
  a render fingerprint that caches the track, atomic JSON writes with
  WinError-32 retry, and aligned-SRT export), and
  `ui/subtitle_controller.py` + `ui/qml/components/SubtitleCard.qml` (the
  `subtitleController` context property). Stdlib-only — no new dependency.
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
  0.1.16.
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
  `packaging/release-notes/v0.1.1.md`–`v0.1.16.md`; windowed `.exe`
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
- **Shipped (2026-09-14, on `main`, unreleased):** SRT subtitle
  dub/transcript studio — stdlib `.srt` cue model (`core/subtitles.py`),
  `dub`/`transcript` fit policies with WSOLA rate repair (`core/align.py`),
  streaming `PCM_16` track render with a per-workspace fingerprint cache and
  aligned-SRT export (`core/subtitle_project.py`), and the
  `subtitleController` + `SubtitleCard.qml` QML surface as a third
  Paragraph-tab mode; SRT studio English catalog (`617cfdc`); Paragraph
  mode-navigation stays stationary (`e255027`). Test items now 1055 collected
  / 1054 selected (12 benchmark deselected).
- **Shipped (2026-09-15, v0.1.15):** Audio Studio rebuilt around a **pinned
  transport dock** (play/pause/seek + timecodes + previews never scroll away),
  master-waveform **region selection** with one-click trim-to / delete-selection
  (`TrimOp`/`CutOp`), a **truthful dynamic op stack** (rack readouts fold gain
  sums / speed multipliers in real time and apply in place instead of stacking
  duplicates), truthful per-clip auditioning (the dock reflects the clip
  actually playing), **clickable op-history breadcrumb rollback**, keyboard
  transport (space/Esc/arrows), quick actions, and UI integrity down to 640×420.
  Also fixes the Windows soundfile handle release on abort (WinError 32 unlink).
- **Shipped (2026-09-16, v0.1.16):** Studio tab UX refinement +
  **component extraction** — visible seek ±5 s / stop buttons with key hints in
  tooltips, `AppNumberField` precise numeric entry beside every FX slider,
  fade dirty-state parity + reset confirmation, single primary dock CTA (quick
  export demoted to an icon action), clickable equal-height guide cards,
  danger styling for destructive actions, square 40 px icon hit targets,
  neutral-when-idle transport status dot, stable busy labels (no reflow), and
  `StudioTab.qml` decomposed into `StudioRackModule` / `StudioParamRow` /
  `StudioClipRow` (1757 → 1398 lines); `AppCard` gains
  `clickable`/`cardHovered`/`cardClicked` for whole-card tap targets
  (`156c82f`, `d5b2529`).
- **Not yet:** frozen-in model weights (by design — on-demand verified
  baseline instead), signing/notarization (macOS build
  is ad-hoc codesigned — no Apple Developer ID), `.msi`/`.deb`/AppImage
  installers (Linux has the `install.sh` stopgap only).
