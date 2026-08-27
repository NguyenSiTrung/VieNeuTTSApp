# VieNeuTTS Desktop App — Project Plan

> Cross-platform on-device Vietnamese/English TTS desktop application powered by
> [VieNeu-TTS v3 Turbo](https://huggingface.co/pnnbao-ump/VieNeu-TTS-v3-Turbo).

| | |
|---|---|
| Status | In progress — Phases 0–4 complete (spike, headless core, UI shell, core features, streaming & polish); Phase 5 packaging & offline next |
| Target platforms | macOS (Apple Silicon + Intel), Windows 10/11 x64, Ubuntu 22.04+ x64 |
| Runtime | Python `vieneu==3.3.0` (torch-free ONNX Runtime on CPU; optional CUDA) |
| UI | PySide6 + QML (Qt Quick) |
| License | Apache-2.0 (model + SDK); Qt LGPL v3 (dynamic-linked) |
| Last updated | 2026-08-27 |

---

## 0. Current status (2026-08-27)

Phases 0–4 are **complete** (conductor tracks `phase01_core_20260827`,
`phase02_uishell_20260827`, `phase03_corefeat_20260827`,
`phase04_streaming_20260827`, all archived under `conductor/archive/`). The Phase 0 spike validated the SDK contract on macOS and
corrected several plan assumptions — authoritative findings live in
[`docs/spike-report.md`](docs/spike-report.md); affected sections below carry inline notes.

| Milestone (§19) | Status | Evidence |
|---|---|---|
| Phase 0 — Spike & validation | ✅ Complete* | `docs/spike-report.md` — API contract, budgets, offline bundling all confirmed |
| Phase 1 — Core engine (headless) | ✅ Complete | `src/vienetts_app/{core,workers}` + `--smoke` CLI; gate: 137 unit tests, 91% coverage, ruff clean, real-model smoke green |
| Phase 2 — UI shell | ✅ Complete | PySide6+QML shell (`Main.qml` nav + StackLayout, Theme singleton, ShellBridge); GUI launch 0.15–0.28 s with no engine deps imported; window exposed on Wayland; 186 tests green |
| Phase 3 — Core features | ✅ Complete | All four tabs wired (Text/Paragraph/Cloning/Settings), importers (.txt/.md/.docx/.pdf), PlaybackController + WAV export, cloned-voice persistence; 369 tests green, real-model offscreen pass 15/15 |
| Phase 4 — Streaming & polish | ✅ Complete | `StreamPlaybackController` (QAudioSink ring buffer) + `WaveformIndicator` on Text & Paragraph tabs; models-missing screen, export-only mode, 200k import cap, consent copy; 478 tests green; real-model first-audio 99–102 ms (target ~300 ms), long-doc stream RSS 1120 MB (< 2 GB) |
| Phase 5 — Packaging & offline | ⬜ Next | `packaging/` absent; `scripts/fetch_models.py` ready from Phase 0 |
| Phase 6 — Hardening & release | ⬜ Not started | `tests/integration/` still only `__init__.py` |

\* Cross-OS gaps deferred to Phase 6 CI: Ubuntu docker script committed but not run
locally (`scripts/spike/phase0_ubuntu_docker.sh`); Windows never exercised.

**Known carry-overs:** the **non-stream** `infer()` long-text path still exceeds the
§18 memory budget (~2.5 GB full-audio concat watermark; bead `VieNeuTTSApp-8jm`;
the streaming path is fixed — see §18); mp3 decode via libsndfile unverified on
Windows/Linux (bead `VieNeuTTSApp-vis`, slated for Phase 5 validation).

## 1. Executive Summary

Build a single-codebase desktop app that runs VieNeu-TTS v3 Turbo **fully offline and
on-device** across macOS, Windows, and Ubuntu. The app converts text, paragraphs, and
imported files (.txt/.md/.docx/.pdf) to 48 kHz speech using 20 built-in Vietnamese voices,
instant voice cloning, and streaming playback — while **auto-detecting the best available
engine (CPU / NVIDIA GPU)** on the user's machine.

**Key architectural decision:** a single-process **PySide6 + QML** app that calls the
official `vieneu` Python SDK **in-process** from a worker thread. The SDK already performs
the CPU/GPU auto-detection we need; the app's job is to surface it in the UI, add a
workload-aware heuristic (short text → CPU, long/bulk → GPU), and keep inference off the
UI thread.

---

## 2. Goals & Non-Goals

### Goals
1. Cross-platform desktop app from one codebase: macOS, Windows, Ubuntu.
2. Fully offline inference on device — no cloud API, no network after install.
3. TTS for: free text, long paragraphs, and imported files (.txt, .md, .docx, .pdf).
4. 20 preset voices + instant voice cloning (3–8 s reference clip).
5. Auto-detect and expose the best engine (CPU/ONNX vs NVIDIA/CUDA), with manual override.
6. Playback + streaming playback + WAV export.
7. Installable artifacts per OS (`.dmg`, `.msi`/`.exe`, `.deb`/AppImage).

### Non-Goals (v1)
- MP3/AAC export (requires FFmpeg; WAV only in v1).
- Cloud synthesis, multi-user, or remote models.
- Training / fine-tuning / LoRA / GGUF custom backbones (possible later; SDK supports them).
- Mobile (Android/iOS).
- GPU support beyond NVIDIA CUDA (no AMD ROCm / Apple MPS path exists in the SDK).

---

## 3. Ground Truth — Model & SDK Facts

Source: HF model card, PyPI `vieneu` 3.3.0 metadata, SDK `pyproject.toml`, HF file tree.

| Fact | Value |
|---|---|
| Model | `pnnbao-ump/VieNeu-TTS-v3-Turbo` (48 kHz, vi + en code-switching) |
| License | Apache-2.0 (weights, ONNX exports, preset voices, generated audio — commercial OK) |
| SDK | `vieneu==3.3.0`, Python >= 3.10 (3.10–3.13 classifiers) |
| CPU engine | ONNX Runtime, **torch-free**, int8 backbone by default (~1.6× faster, ~4× smaller than fp32) |
| GPU engine | PyTorch `torch==2.8.0` + `torchaudio==2.8.0` (cu128), **CUDA >= 12.8**, automatic batching |
| Pinned GPU dep | `transformers==4.57.6` (Qwen3 backbone + MOSS codec) |
| Streaming | Both backends expose `infer_stream` (ONNX-only claim corrected at Phase 0); first chunk 153 ms, RTF 0.13 measured on M4 |
| Voices | 20 preset (North/Central/South); `list_preset_voices()` authoritative |
| Cloning | `infer(..., ref_audio=..., denoise=True)`, `add_voice(name, clip)`; torch-free on CPU |
| Emotion cues | `[cười]`, `[thở dài]`, `[hắng giọng]` (experimental) |
| Local model path | Supported (custom/local model loading) → enables offline bundling |
| SDK entry points | `vieneu-web` (Gradio), `vieneu-stream` (FastAPI) — reference implementations |

### SDK API surface (v3.3.0)

```python
from vieneu import Vieneu

tts = Vieneu()                       # auto: CUDA→torch, else onnx/int8
tts = Vieneu(backend="onnx"|"torch"|"auto", precision="int8"|"fp32")

audio = tts.infer(text, voice="Adam", ref_audio=..., denoise=True)  # np.float32 @ 48k
for chunk in tts.infer_stream(text, voice=...):  # frame-by-frame (ONNX only)
    ...
audios = tts.infer_batch(texts, voice=..., batch_size=...)          # batched forward
tts.list_preset_voices()          # [(label, voice_id), ...]
tts.add_voice(name, clip_path)    # enroll cloned voice by name
wav, sr = tts.denoise(clip, out_path=...)
tts.save(audio, "out.wav")
```

### Model weights breakdown (disk footprint)

| Artifact | Size | Engine |
|---|---|---|
| `model.safetensors` (fp32 backbone) | 262 MB | torch/GPU |
| `denoiser.onnx` | 43 MB | both (cloning) |
| `speaker_encoder.onnx` | 28 MB | both (cloning) |
| `onnx_int8/` (backbone + heads + acoustic + decode) | ~166 MB | onnx/CPU int8 |
| `onnx_update/` (fp32 graphs — folder renamed from `onnx/`, corrected at Phase 0) | ~455 MB est. | onnx/CPU fp32 |
| tokenizer/config/preset-voice JSON | < 1 MB | both |
| MOSS-Audio-Tokenizer-Nano-ONNX (codec repo — required, added at Phase 0) | ~86 MB | both; always resolved via HF cache, no local-dir load |

**Bundled install sizes (weights only):**
- CPU int8 build: **≈ 327 MB measured** (minimal 16-file bundle incl. the codec repo; the
  original ~240 MB estimate excluded it — spike §6), plus ONNX Runtime & deps ≈ 200–300 MB.
- CPU fp32 build: ~530 MB.
- GPU build (safetensors + codec + torch cu128): weights ~335 MB, runtime adds ~2–3 GB.

---

## 4. Constraints & Assumptions

- `vieneu` is **Python-only**: tokenizer (`tokenizers`), `sea-g2p` phonemizer, Qwen3 backbone,
  MOSS codec, `kaldi-native-fbank`, `speaker_encoder`/`denoiser` ONNX — none have a Rust/JS
  equivalent. Any desktop shell must run Python.
- `Vieneu` inference is **blocking**; the app must keep it off the UI thread.
- A `Vieneu` instance is **not assumed thread-safe** → single worker owns it; requests serialized.
- GPU = NVIDIA CUDA only. Apple Silicon, AMD, Intel Arc/iGPU → ONNX/CPU (expected, documented).
- Apple Silicon: ONNX/CPU is **faster than MPS** per the model card — no GPU path on macOS.
- GPU only wins on **batched/long** workloads; short interactive text is faster on CPU/ONNX.
- Model weights are downloaded on first use by the SDK (`huggingface_hub`); for offline install
  we bundle them and pre-seed the HF cache.

---

## 5. Architecture Decision

| Option | Verdict | Reason |
|---|---|---|
| **PySide6 + QML (single process)** | **Chosen** | One language end-to-end; `vieneu` in-process; no IPC; lowest streaming latency; native widgets + GPU-rendered QML for modern UI. |
| Tauri + Rust shell + Python sidecar | Rejected for v1 | Model runtime is Python-only → mandatory sidecar + IPC + port/health/lifecycle plumbing for zero model benefit. Revisit only if a web front end or Rust-native integration becomes a hard requirement. |
| Electron + Python sidecar | Rejected | Same sidecar cost as Tauri plus high memory (Chromium); no advantage over Qt. |
| Pure Rust (ONNX via `ort`) | Rejected | Only covers the ONNX graphs; would require reimplementing tokenizer, sea-g2p, codec, speaker encoder, denoiser, chunking — large fragile port, drifts from official SDK. |

### System diagram

```
┌───────────────────────────────────────────────────────────────┐
│  UI layer — QML (Qt Quick), GPU-rendered                      │
│  Tabs: Text | Paragraph/File | Cloning | Settings             │
└──────────────────────┬────────────────────────────────────────┘
                       │ QML <-> Python bridge (QObject signals/slots)
┌──────────────────────▼────────────────────────────────────────┐
│  AppController (main thread)                                  │
│  orchestration, voice list, settings, playback, export        │
└──────────────┬─────────────────────────────────────────────────┘
               │ command objects → request queue (thread-safe)
┌──────────────▼─────────────────────────────────────────────────┐
│  TTSEngine worker (dedicated QThread)                          │
│  owns the single Vieneu() instance                             │
│  infer / infer_stream / infer_batch / add_voice / denoise      │
│  emits: progress, chunkReady(np.float32), done, error          │
└────────────────────────────────────────────────────────────────┘
```

- **Single long-lived worker thread** owns `Vieneu()` (lazy init on first request).
- **Request queue** serializes inference; `infer_stream` emits a signal per chunk.
- **QtMultimedia** (`QAudioSink`) consumes `chunkReady` for streaming playback; `QMediaPlayer`
  plays full results from an in-memory WAV buffer.

---

## 6. Resource Auto-Detection Design

### 6.1 Detection matrix (hardware → engine)

| Hardware | Backend | Precision | Notes |
|---|---|---|---|
| NVIDIA, CUDA >= 12.8, torch present | `torch` | fp32 | batched; wins on long/bulk |
| NVIDIA, CUDA < 12.8 | `onnx` | int8 | CUDA too old → CPU |
| Apple Silicon (M1+) | `onnx` | int8 | faster than MPS per model card |
| Apple Intel | `onnx` | int8 | CPU |
| AMD / Intel Arc / iGPU | `onnx` | int8 | no CUDA/ROCm path |
| No GPU | `onnx` | int8 | default |

### 6.2 Workload heuristic (on top of raw detection)

- **Streaming** → always `backend="onnx"` (SDK 3.3.0 streams on both engines — spike §4 —
  but streaming workloads are short/interactive, so ONNX/CPU stands).
- **Short interactive text** → `onnx` even if CUDA present (CPU is faster for short input).
- **Long text / bulk / batch** → `torch` if available, else `onnx` (auto-batches sequentially on CPU).
- User override in Settings: `backend = auto | onnx | torch`, `precision = int8 | fp32`.

### 6.3 Implementation note

`Vieneu()` already performs engine auto-detection internally. The app's `detector.py` only
**mirrors** that logic to *display* the chosen engine and to apply the workload heuristic +
user override. The SDK remains the source of truth for the actual engine pick.

---

## 7. Feature Specification

### 7.1 Text tab
- Multiline text box (Vi + En code-switching).
- Voice picker (20 presets, grouped North/Central/South).
- Generate → full playback + WAV export.
- Emotion cues hint (`[cười] [thở dài] [hắng giọng]`).
- *Acceptance:* paste text → voice → WAV 48 kHz; playback works; export saved to chosen dir.

### 7.2 Paragraph / File tab
- Long-text input; progress bar during synthesis.
- Import `.txt`, `.md`, `.docx`, `.pdf` (PDF via MIT `pypdf>=6` — AGPL PyMuPDF rejected at
  Phase 0; `.docx` via `python-docx`).
- Auto-chunking (handled by SDK `infer`); per-file/per-chunk progress; cancel button.
- *Acceptance:* import a multi-page PDF → synthesized WAV; progress increments; cancel stops work.

### 7.3 Cloning tab
- Load 3–8 s reference clip (mp3/wav), optional denoise.
- Preview denoised clip; name the voice; `add_voice` then reuse by name.
- Consent notice (voice-cloning legal warning) before first clone.
- *Acceptance:* clone from clip → synthesized speech matches reference timbre; enrolled voice
  appears in voice list; persists across restarts (if SDK persistence allows — verify).

### 7.4 Settings tab
- Engine: auto / ONNX (CPU) / torch (CUDA) with detected-engine readout.
- Precision: int8 / fp32.
- Default voice, output directory, temperature/top-K sampling (SDK exposes both —
  spike §0: `infer(temperature=0.4, top_k=50)`).
- Theme: system / light / dark.
- *Acceptance:* changing backend applies on next init; invalid selections handled gracefully.

---

## 8. UI/UX Design

- **QML (Qt Quick)** for the UI: GPU-rendered, animated, modern look; one codebase.
- Fallback: Qt Widgets + QSS if QML iteration proves too slow for the team — keep the
  `TTSEngine`/controller layers UI-framework-agnostic.
- Design tokens in a single `Theme.qml` (colors, spacing, typography); dark mode default.
- Responsive layout: 3-column on wide, stacked on narrow.
- Perceived-performance rules:
  - Engine init is slow (~seconds) → show "Loading model…" state with progress, never a frozen UI.
  - Long synthesis shows live progress + a working cancel button.
  - Streaming playback starts in ~300 ms; waveform/level indicator.

---

## 9. Data Model & State

```python
@dataclass(frozen=True)
class EngineInfo:
    backend: Literal["auto", "onnx", "torch"]
    device: Literal["cpu", "cuda"]
    precision: Literal["int8", "fp32"]
    cuda_version: str | None
    note: str                    # human-readable, e.g. "ONNX Runtime CPU · int8"

@dataclass
class Settings:                  # persisted to platform data dir (JSON via platformdirs)
    backend: str = "auto"
    precision: str = "int8"
    default_voice: str = "Adam"
    output_dir: str = ""         # default: ~/Music/VieNeuTTS
    theme: str = "system"
    denoise_ref: bool = True
    temperature: float = 0.4     # SDK exposes it (spike §0); implemented in models.py

@dataclass(frozen=True)
class TTSRequest:
    text: str
    voice: str | None = None
    ref_audio: str | None = None
    denoise: bool = True
    mode: Literal["infer", "stream", "batch"] = "infer"

@dataclass(frozen=True)
class TTSProgress:               # signal payload
    done: int
    total: int
    stage: str                   # "init" | "synthesizing" | "exporting"
```

Persistence: `platformdirs.user_data_dir("VieNeuTTSApp")` + JSON (testable, portable).
Alternative QSettings noted but rejected for testability.

---

## 10. Audio Pipeline

- `infer()` → `np.float32 @ 48 kHz`, **mono confirmed** at Phase 0 (1-D array).
- **Playback (full):** encode to in-memory WAV (`soundfile`) → `QBuffer` → `QMediaPlayer`.
- **Streaming:** `QAudioSink` with `QAudioFormat(48000 Hz, 1ch, Float32)`; worker emits
  `chunkReady` → ring buffer feeds sink; stop via signal.
- **Export:** `tts.save()` → WAV; optional `.mp3` out of scope (FFmpeg) for v1.
- Cloning path gotcha (spike §3): `denoise()` outputs **44.1 kHz**, not 48 kHz — resample
  before feeding 48 kHz pipelines.
- Input file decode: `soundfile`/`librosa` for reference clips (mp3 via `soundfile` + system
  libs; verify mp3 decode works across all 3 OSes — likely needs `libsndfile` with mp3,
  fallback `audioread`/`ffmpeg`).

---

## 11. Error Handling & Edge Cases

| Case | Behavior |
|---|---|
| First run, no network, weights not bundled | Fallback to bundled weights; else clear "models missing" screen with path to fetch script. |
| CUDA present but torch missing | Fall back to ONNX/CPU with a visible notice. |
| Unsupported GPU (AMD/Arc/MPS) | Quietly select ONNX/CPU; note in Settings readout. |
| Invalid voice name | Validate against `list_preset_voices()`; dropdown prevents it. |
| Empty/whitespace text | Disable Generate. |
| Oversized file import | Chunked read; cap at N chars (e.g. 200k) with warning. |
| Mid-synthesis cancel | Cooperative flag checked between chunks/segments (SDK may not cancel mid-chunk — documented). |
| Cloning clip too long/short | Trim/normalize to 3–8 s (SDK auto-trims ≤ 8 s); warn if < 3 s. |
| Audio device missing | Detect `QMediaDevices` empty → disable playback, allow export only. |

---

## 12. Project Structure

```
VieNeuTTSApp/
├── PROJECT_PLAN.md
├── README.md
├── pyproject.toml                 # app package + deps + console script
├── .gitignore
├── src/
│   └── vienetts_app/
│       ├── __init__.py
│       ├── __main__.py            # python -m vienetts_app
│       ├── app.py                 # entry: QML engine bootstrap
│       ├── core/
│       │   ├── engine.py          # TTSEngine (owns Vieneu instance; infer_stream_chunked arena mitigation)
│       │   ├── detector.py        # hardware/engine detection (display + heuristic)
│       │   ├── settings.py        # load/save Settings (platformdirs + JSON)
│       │   ├── models.py          # EngineInfo, Settings, TTSRequest, TTSProgress
│       │   ├── importers.py       # .txt/.md/.docx/.pdf import, 200k char cap
│       │   └── audio.py           # WAV encode/export, format helpers
│       ├── workers/
│       │   └── inference_worker.py # single engine-owning worker; infer + stream dispatch
│       │                          #   (no separate stream_worker — Phase 4 kept streaming
│       │                          #    in the worker's _process_stream + engine chunked dispatch)
│       ├── ui/
│       │   ├── bridge.py          # QObject exposed to QML
│       │   ├── controller.py      # AppController — orchestration, generateStream, edge-case props
│       │   ├── playback.py        # QMediaPlayer full playback + audio-output-device probe
│       │   ├── stream_playback.py # StreamPlaybackController — QAudioSink ring buffer (Phase 4)
│       │   ├── theme.py
│       │   └── qml/
│       │       ├── Main.qml
│       │       ├── TextTab.qml
│       │       ├── ParagraphTab.qml
│       │       ├── CloningTab.qml
│       │       ├── SettingsTab.qml
│       │       └── WaveformIndicator.qml
│       └── resources/assets/      # icons, consent text, voice metadata
├── packaging/
│   ├── pyinstaller/
│   │   ├── app_cpu.spec
│   │   └── app_gpu.spec
│   ├── macos/                     # entitlements, dmg, notarization
│   ├── windows/                   # NSIS/MSI, signing
│   └── linux/                     # .deb + AppImage
├── scripts/
│   ├── fetch_models.py            # download + seed HF cache / local model dir
│   ├── build_cpu.sh
│   ├── build_gpu.sh
│   └── sign_notarize.sh
├── tests/
│   ├── unit/                      # detector, settings, audio, models
│   ├── integration/               # real synthesis contract test (CPU/ONNX)
│   └── smoke/                     # headless CLI smoke
└── docs/
```

---

## 13. Dependencies (pinned)

Runtime (CPU build — torch-free):

```
vieneu==3.3.0            # pulls: onnxruntime, sea-g2p, kaldi-native-fbank, tokenizers,
                         #        soundfile, soxr, librosa, numpy, huggingface_hub, perth
PySide6>=6.7             # Qt for Python (LGPL, dynamic-linked)
platformdirs>=4          # cross-platform app data dir
python-docx>=1.1         # .docx import
pypdf>=6                 # .pdf import (MIT; AGPL PyMuPDF rejected — spike §7)
```

GPU build (optional, NVIDIA only, `[gpu]` extra):

```
torch==2.8.0             # index: https://download.pytorch.org/whl/cu128
torchaudio==2.8.0        # index: https://download.pytorch.org/whl/cu128
transformers==4.57.6     # pinned — Qwen3 backbone + MOSS codec
```

Dev/test: `pytest`, `pytest-asyncio`, `pytest-qt` (UI smoke), `ruff`.

---

## 14. Packaging & Distribution

| OS | Artifact | Build | Notes |
|---|---|---|---|
| macOS | `.dmg` (universal2 or per-arch) | PyInstaller one-dir | Apple Silicon → ONNX/CPU; **codesign + notarize** required (hardened runtime + entitlements). |
| Windows | `.msi` (or NSIS `.exe`) | PyInstaller one-dir | x64; code-sign cert for SmartScreen trust. |
| Ubuntu | `.deb` + AppImage | PyInstaller one-dir | AppImage for glibc portability across distros. |

Build variants (same code, different dependency sets):

- **CPU (default):** torch-free, ONNX Runtime int8. Small (~500–600 MB installer).
- **GPU (opt-in):** + torch/torchaudio cu128 + transformers. Installer grows to ~2–3 GB;
  NVIDIA-only; ship as separate download.

### Offline model bundling

- Bundle `onnx_int8/`, `denoiser.onnx`, `speaker_encoder.onnx`, tokenizer/config, and
  preset-voice JSON (and optionally `onnx_update/` fp32 for the precision switch —
  folder name corrected at Phase 0; it is not `onnx/`).
- Pre-seed the HF cache at build time, or load from a **local model path** (SDK supports it)
  and point the app there. Set `HF_HUB_OFFLINE=1` at runtime when bundled.
- `scripts/fetch_models.py` downloads and verifies all artifacts (hashes) once, checked into
  build pipeline (or fetched by CI at release).

---

## 15. Licensing & Attribution

- Model + SDK: **Apache-2.0** — commercial use, modification, redistribution OK; retain
  notices + attribution to `pnnbao97/VieNeu-TTS` and `pnnbao-ump/VieNeu-TTS-v3-Turbo`.
- Qt/PySide6: **LGPL v3** — keep Qt **dynamically linked** (PyInstaller default); ship the
  LGPL text + offer to replace the Qt libraries.
- Third-party (also permissive, keep notices): MOSS-Audio-Tokenizer-Nano (Apache-2.0),
  `sea-g2p`, ONNX Runtime (MIT), `pypdf` (MIT — chosen over AGPL PyMuPDF at Phase 0,
  spike §7).
- Voice cloning: user consent notice required in UI (not a code issue, a compliance one).

---

## 16. Testing Strategy

| Layer | What | Tool |
|---|---|---|
| Unit | `detector` matrix (mocked torch/CUDA), `settings` round-trip, `audio` WAV encode, `models` validation | pytest |
| Integration (contract) | Real CPU/ONNX synthesis: fixed Vietnamese string → assert 48 kHz, duration > 0, non-silent; voice list ≥ 20 | pytest (needs bundled model) |
| UI smoke | Launch QML, tab navigation, generate a short clip | pytest-qt |
| Headless smoke | `python -m vienetts_app --smoke "Xin chào" --voice Adam -o out.wav` exits 0, produces WAV | CI |
| Manual/visual | Playback quality, streaming latency, cancel behavior on all 3 OSes | release checklist |

Every test defends an observable contract; no source-text/plumbing assertions.

---

## 17. CI/CD & Release Pipeline

- **Matrix:** `ubuntu-latest` (x64), `macos-latest` (arm64), `windows-latest` (x64).
- Per OS: install CPU deps → run unit + headless smoke → build PyInstaller artifact → upload
  to GitHub Release.
- **GPU build:** separate Linux/Windows job with cu128 torch (no macOS GPU build).
- **macOS:** notarization job using Apple Developer ID secrets (`notarytool`).
- **Windows:** optional code-sign step when cert present.
- Version via `git tag`; artifacts named `VieNeuTTS-<version>-<os>-<arch>-<cpu|gpu>.<ext>`.

---

## 18. Performance Budgets

| Metric | Target |
|---|---|
| First synthesis (cold, model load) | < 15 s on SSD (CPU int8) |
| Warm synthesis latency (short text) | < 1 s (CPU/ONNX, ~2–3× realtime per model card) |
| Streaming RTF | < 1 on CPU; first audio ~300 ms — **measured 99–102 ms** CPU int8, real model (phase04 AC-1, 3 runs) |
| Streaming first-audio | ~300 ms — **met with ~3× margin** (99–102 ms; cold first request adds ~1.0–1.8 s lazy model load) |
| UI responsiveness | Main thread never blocks; progress/cancel live |
| Memory (CPU int8 engine) | < 2 GB resident — holds for interactive use (~766 MB) and for **streaming long-doc synthesis** (1120 MB flat plateau via 512-char chunked dispatch, phase04 AC-5; bead `VieNeuTTSApp-u5c` closed); still breached by the non-stream `infer()` full-concat path (~2.5 GB watermark; bead `VieNeuTTSApp-8jm`) |
| Installer size (CPU build) | ~500–600 MB |

---

## 19. Milestones & Acceptance Criteria

### Phase 0 — Spike & environment validation — ✅ COMPLETE
- Install `vieneu` on all 3 OSes; confirm CPU/ONNX synthesis + voice list + cloning.
- Measure RTF and memory; confirm `infer_stream` chunk format (dtype, shape, channel count).
- Confirm local-model-path loading and `HF_HUB_OFFLINE` bundling approach.
- **Deliverable:** spike report + confirmed API contract.
- **Acceptance:** headless synth works on macOS/Windows/Ubuntu; measured numbers recorded.
- **Done:** macOS fully validated end-to-end (`docs/spike-report.md`; contract §0, budgets
  §4–5, offline §6). Ubuntu docker script committed but not run locally; Windows never
  exercised — both deferred to Phase 6 CI. Long-workload RSS exceeds budget (§18 note above;
  bead `VieNeuTTSApp-u5c`).

### Phase 1 — Core engine (headless) — ✅ COMPLETE
- Implement `TTSEngine`, `detector.py`, `settings.py`, `models.py`, `audio.py`.
- Threaded worker + request queue; cancel flag.
- **Acceptance:** `python -m vienetts_app --smoke` produces a valid 48 kHz WAV on all 3 OSes;
  unit tests green.
- **Done:** gate green 2026-08-27 — 137 unit tests, 91% coverage, ruff clean, real-model
  smoke verified. `--smoke` CLI runs through the worker (commit `5176eeb`); per-OS
  verification still lands with Phase 6 CI as in Phase 0.

### Phase 2 — UI shell — ✅ COMPLETE
- PySide6 + QML bootstrap, navigation, theme, empty tabs.
- **Acceptance:** app launches on all 3 OSes; tabs navigate; dark/light theme applies.
- **Done:** QML shell with `Main.qml` nav + StackLayout, Theme singleton, ShellBridge;
  GUI launch 0.15–0.28 s with no engine deps imported; window exposed on Wayland;
  186 tests green (archived track `phase02_uishell_20260827`).

### Phase 3 — Core features — ✅ COMPLETE
- Text tab, Paragraph/File tab, Cloning tab, Settings tab wired to engine.
- Playback (full) + WAV export; progress + cancel.
- **Acceptance:** end-to-end generate/play/export and clone/play flows pass manual + smoke tests.
- **Done:** all four tabs wired; importers (.txt/.md/.docx/.pdf); PlaybackController +
  WAV export; cloned voices persisted to app data dir; 369 tests green; real-model
  offscreen pass 15/15 (archived track `phase03_corefeat_20260827`).

### Phase 4 — Streaming & polish — ✅ COMPLETE
- `infer_stream` + `QAudioSink` streaming playback; waveform indicator.
- Error/edge-case screens; consent notice.
- **Acceptance:** streaming first audio ~300 ms; cancel works; edge cases handled per §11.
- **Done:** all 9 tasks closed (archived track `phase04_streaming_20260827`). Streaming
  playback via `StreamPlaybackController` (QAudioSink ring buffer, 48 kHz Float32) +
  `WaveformIndicator.qml` on Text & Paragraph tabs; edge-case surfaces per §11 —
  models-missing screen (`ModelsMissingError`), export-only mode when no audio output
  device, 200k import cap, consent-notice copy. ONNX arena mitigation via chunked stream
  dispatch (512-char segments through `infer_stream_chunked`; bead `VieNeuTTSApp-u5c`
  closed with AC-5 evidence). Gate: 478 tests green offscreen, ruff clean (verified on
  HEAD 2026-08-27). Real-model CPU int8: first-audio 99–102 ms vs ~300 ms target (AC-1);
  long-doc stream RSS peak 1120 MB vs < 2 GB budget (AC-5). Residual: non-stream
  `infer()` long-text RSS re-scoped to bead `VieNeuTTSApp-8jm`.

### Phase 5 — Packaging & offline
- PyInstaller specs; bundle ONNX weights; per-OS installers; sign/notarize (macOS).
- **Acceptance:** fresh machine (no Python) installs and synthesizes offline on each OS.

### Phase 6 — Hardening & release
- Integration tests, CI matrix, docs, license/attribution files.
- **Acceptance:** CI green on all 3 OSes; release artifacts install and pass smoke checklist.

---

## 20. Risks & Mitigations

| Risk | Impact | Mitigation |
|---|---|---|
| Native wheels (`onnxruntime`, `kaldi-native-fbank`) break under PyInstaller | Build failure | One-dir mode; per-OS hooks; build early (Phase 5 starts at Phase 1 spike artifacts) |
| torch cu128 bundling = huge installer; AV flags | UX/trust | GPU = separate opt-in build; one-dir; code-sign Windows |
| PyMuPDF is AGPL | Licensing | Evaluate MIT `pypdf` for PDF import; or keep PyMuPDF and comply (source offer) — decide in Phase 0 |
| macOS notarization friction | Release delay | Entitlements + hardened runtime set up in Phase 5; start signing early |
| No mid-chunk cancel in SDK | UX | Cooperative cancel between chunks; document limitation |
| `Vieneu` not thread-safe | Crashes | Single worker owns instance; all requests serialized |
| Apple Silicon/AMD users expect "GPU" | Confusion | Settings readout explains ONNX/CPU is optimal for their hardware |
| Voice-cloning misuse | Legal | Consent notice + user acknowledgment gate before cloning |

---

## 21. Open Questions — resolutions after Phase 0

| # | Question | Status |
|---|---|---|
| 1 | QML vs Qt Widgets+QSS | **Resolved** — QML chosen at Phase 2 (`Theme.qml` design tokens, GPU-rendered Qt Quick; the Widgets fallback proved unnecessary). |
| 2 | Commercial vs open-source release | Open — decide before Phase 5 (affects signing/distribution). |
| 3 | App name / branding | Open. |
| 4 | Installer size budget | **Resolved** — CPU-only default build; weights ≈ 327 MB measured (spike §6). |
| 5 | PDF import library | **Resolved** — `pypdf>=6` (MIT); AGPL PyMuPDF rejected for distribution (spike §7; already in `pyproject.toml`). |
| 6 | MP3 reference-clip decode | **Resolved on macOS** via libsndfile MP3 (soundfile 0.14, no ffmpeg); Windows/Linux builds remain a packaging risk to validate in Phase 5. |

Also resolved at Phase 0 (folded into the sections above): streaming works on both
backends (§3), mono output confirmed (§10), temperature/top-K exposed by SDK (§7.4, §9),
and the SDK's internal RLock does not relax the single-worker rule (§4). Resolved at
Phase 3: cloned-voice persistence lives in `<app data dir>/voices/voices.json` (written
via `save_voices(path=...)`, merged back into the engine catalog on init) — the SDK's
site-packages default is never used.

---

## 22. Definition of Done (overall)

1. One codebase installs and runs on macOS, Windows, Ubuntu.
2. Fully offline synthesis (text, paragraph, file import) with 20 preset voices.
3. Voice cloning + enrollment works.
4. Engine auto-detection + workload heuristic + manual override, surfaced in UI.
5. Streaming playback with ~300 ms first audio.
6. WAV export; progress + cancel.
7. Signed/notarized installers per OS; CI green; license/attribution files shipped.
