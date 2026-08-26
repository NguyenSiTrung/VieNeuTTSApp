# VieNeuTTS App

Cross-platform on-device Vietnamese/English text-to-speech desktop app powered
by [VieNeu-TTS v3 Turbo](https://huggingface.co/pnnbao-ump/VieNeu-TTS-v3-Turbo)
via the `vieneu` Python SDK. Fully offline after install; PySide6 + QML UI.

## Status

Phase 0/1 (spike + headless core engine) — in development.
See [PROJECT_PLAN.md](PROJECT_PLAN.md) and [conductor/tracks.md](conductor/tracks.md).

## Development setup

Requires Python 3.10–3.13 (3.13 recommended; the SDK does not support 3.14 yet).

```bash
uv venv --python 3.13 .venv
uv pip install -p .venv/bin/python -e ".[dev]"
```

## Quality gates

```bash
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/pytest
```

## License

Apache-2.0 (model + SDK). Qt/PySide6 is LGPL v3, dynamically linked.
