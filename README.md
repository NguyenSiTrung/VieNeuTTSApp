# VieNeuTTS App

Cross-platform, fully-offline Vietnamese/English text-to-speech desktop app
powered by [VieNeu-TTS v3 Turbo](https://huggingface.co/pnnbao-ump/VieNeu-TTS-v3-Turbo)
through the `vieneu` Python SDK. PySide6 + QML UI; on-device ONNX inference
on CPU (int8 by default) with optional NVIDIA CUDA.

## Status

v1 core features are implemented and tested (593 tests collected at time of
writing). What works:

- Free-text TTS with vi/en code-switching, ~20 preset voices grouped
  Bắc / Trung / Nam (North/Central/South)
- Streaming playback with a historical direct-engine first-chunk observation
  of about 100 ms on one Apple M4. End-to-end controller, audio-device, and
  cross-platform results are tracked separately in
  [docs/performance](docs/performance/README.md), plus 48 kHz WAV export
- File import: `.txt`, `.md`, `.docx`, `.pdf`
- Instant voice cloning from a 3–8 s reference clip
- EPUB audiobook studio (chapter-aware playback, per-chapter WAV cache,
  resume, ordered export, karaoke transcript sync with click-to-seek, render
  ETA and overall render-all progress)
- Automatic engine detection (CPU/ONNX vs NVIDIA/CUDA) with manual override

Not yet done: packaging and offline model bundling (Phase 5) and
release hardening (Phase 6). See [PROJECT_PLAN.md](PROJECT_PLAN.md) and
[conductor/tracks.md](conductor/tracks.md).

## Requirements

- Python 3.10–3.13 (3.13 recommended — the SDK does not support 3.14 yet)
- `uv` for environment management
- A display server for the GUI (the headless smoke CLI works without one)
- Model files cached or bundled (first synthesis needs a one-time download —
  see [Models](#models))

Optional GPU: NVIDIA CUDA >= 12.8.

## Setup

```bash
uv venv --python 3.13 .venv
uv pip install -p .venv/bin/python -e ".[dev]"
```

CUDA build (instead of CPU ONNX):

```bash
uv pip install -p .venv/bin/python -e ".[dev,gpu]"
```

## Models

The app loads the model via the SDK. Two options:

**A. Let the SDK download on first use** — no setup; the model is fetched
into the Hugging Face cache automatically.

**B. Pre-fetch a pinned offline bundle** — deterministic CPU (int8) files
with a SHA256 manifest:

```bash
.venv/bin/python scripts/fetch_models.py --out models        # download
.venv/bin/python scripts/fetch_models.py --out models --verify # re-check
```

Use `--precision fp32` for the fp32 graphs (~455 MB extra). See
`scripts/fetch_models.py` for the exact file set and layout.

## Run

```bash
.venv/bin/vienetts-app                # GUI (or: .venv/bin/python -m vienetts_app)
```

Headless smoke test — synthesizes end-to-end and exits 0 only on a valid WAV:

```bash
.venv/bin/vienetts-app --smoke "Xin chào, đây là thử nghiệm"
.venv/bin/vienetts-app --smoke "Hello world" --voice "Trúc Ly" --stream -o /tmp/out.wav
```

Options:

| Flag | Description |
| --- | --- |
| `--smoke TEXT` | Synthesize TEXT end-to-end and exit (omit to open the GUI) |
| `--voice ID`   | Preset voice id (default: `Adam`) |
| `--stream`     | Use the streaming synthesis path |
| `-o, --output` | Output WAV path (default: `out.wav`) |

The smoke run prints the detected engine, then `output: <path> (<N>s)` on
success. It exercises real synthesis through the threaded worker — useful as
a full-stack check without the UI.

### UI language

The interface is bilingual: **Tiếng Việt** (the source language) and
**English**. Switch it in *Settings → Appearance → Ngôn ngữ* — the change
applies instantly (no restart) and persists; `system` follows the OS locale,
defaulting to Vietnamese. Strings already shown in an error banner or a
transient toast when you switch keep the old language until the next event.
When adding or changing user-facing strings, regenerate the English catalog
with:

```bash
scripts/update_i18n.sh        # lupdate merge → translate new entries → recompile .qm
```

The unit suite fails on unfinished catalog entries, so untranslated strings
block the quality gates.

## Quality gates

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
```

## Project layout

src/vienetts_app/
  core/     engine, models, settings, detectors, importers, EPUB
  workers/  inference worker (QThread owning the SDK instance)
  ui/       PySide6 controllers + QML surface
  app.py    GUI bootstrap (QGuiApplication + QQmlApplicationEngine)
  __main__.py  entry point: GUI + --smoke CLI
scripts/     model fetch/verify
tests/       unit, integration, smoke, fixtures
conductor/   context-driven dev tracks (product, tech-stack, patterns)
```

## License

Apache-2.0 (model + SDK). Qt/PySide6 is LGPL v3, dynamically linked.
