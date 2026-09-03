"""VieNeuTTS desktop app — on-device Vietnamese/English TTS powered by VieNeu-TTS v3 Turbo."""

from __future__ import annotations

import contextlib
import os
import sys

__version__ = "0.1.3"


def ensure_windowed_stdio() -> None:
    """Replace ``None`` stdio with devnull (windowed-exe safety net).

    PyInstaller ``console=False`` (and ``pythonw.exe``) starts the process
    with ``sys.stdout``/``sys.stderr`` set to ``None``. Any library that
    writes progress to the console — tqdm (used by ``huggingface_hub`` for
    weight downloads), ``print()``, ``traceback`` — then dies with
    ``AttributeError: 'NoneType' object has no attribute 'write'``, which
    the engine seam surfaces as ``Engine initialization failed: ...`` on
    first synthesis. Redirecting to devnull keeps the windowed process
    silent instead of crashing; a no-op when stdio already exists.
    """
    if sys.stdout is None:
        sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")  # noqa: PTH123,SIM115 — kept open as stdio
    if sys.stderr is None:
        sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")  # noqa: PTH123,SIM115 — kept open as stdio
    if sys.stdin is None:
        with contextlib.suppress(OSError):
            sys.stdin = open(os.devnull)  # noqa: PTH123,SIM115 — read-only guard
