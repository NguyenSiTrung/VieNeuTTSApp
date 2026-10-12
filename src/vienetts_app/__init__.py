"""VieNeuTTS desktop app — on-device Vietnamese/English TTS powered by VieNeu-TTS v3 Turbo."""

from __future__ import annotations

import contextlib
import os
import sys

__version__ = "0.3.0"


def _restore_default_sigpipe() -> None:
    """Let a closed stdout pipe kill the process quietly (POSIX ``--smoke`` use).

    CPython ignores ``SIGPIPE`` by default and raises ``BrokenPipeError``
    on the next ``print`` instead — a traceback for ``... | head`` pipelines.
    Restoring ``SIG_DFL`` gives the conventional exit-status-141 silence.
    No-op on Windows (no ``SIGPIPE``) and when stdout is not a pipe.
    """
    if sys.platform == "win32":
        return
    try:
        fileno = sys.stdout.fileno() if sys.stdout is not None else None
    except (AttributeError, OSError, ValueError):
        return
    if fileno is None:
        return
    try:
        import signal
        import stat

        if not stat.S_ISFIFO(os.fstat(fileno).st_mode):
            return  # regular files/devnull can never SIGPIPE
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
    except (AttributeError, OSError, ValueError):  # noqa: BLE001 — best effort
        pass


def _ensure_tls_ca_bundle() -> None:
    """Point stdlib TLS at certifi's CA bundle when nothing configured one.

    Frozen builds ship OpenSSL whose compiled-in default CA directory
    (python.org: ``/Library/Frameworks/Python.framework/...``) does not
    exist on end-user machines, and no system keychain is consulted — so
    every *stdlib urllib* HTTPS call (update checks, GGUF runtime pack
    downloads) dies with ``CERTIFICATE_VERIFY_FAILED``.
    ``requests``/``huggingface_hub`` already trust certifi directly;
    setting ``SSL_CERT_FILE`` once at startup gives urllib the same roots.
    A pre-set value is respected; no-op when certifi cannot be resolved.
    """
    if os.environ.get("SSL_CERT_FILE"):
        return
    try:
        import certifi

        cafile = certifi.where()
    except Exception:  # noqa: BLE001 — never let TLS setup kill startup
        return
    if os.path.isfile(cafile):
        os.environ["SSL_CERT_FILE"] = cafile


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
    _restore_default_sigpipe()
    _ensure_tls_ca_bundle()
    if sys.stdout is None:
        with contextlib.suppress(OSError):
            sys.stdout = open(os.devnull, "w", encoding="utf-8", errors="replace")  # noqa: PTH123,SIM115 — kept open as stdio
    if sys.stderr is None:
        with contextlib.suppress(OSError):
            sys.stderr = open(os.devnull, "w", encoding="utf-8", errors="replace")  # noqa: PTH123,SIM115 — kept open as stdio
    if sys.stdin is None:
        with contextlib.suppress(OSError):
            sys.stdin = open(os.devnull)  # noqa: PTH123,SIM115 — read-only guard
    try:
        from vienetts_app.crash import install_crash_handler

        install_crash_handler()
    except Exception:  # noqa: BLE001
        pass
