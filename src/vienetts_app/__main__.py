"""Dual entry: ``python -m vienetts_app`` (no args) opens the GUI shell
(FR-2.1); ``--smoke TEXT`` keeps the Phase 1 headless CLI — synthesis
end-to-end through the threaded worker, exit 0 only on a valid WAV (AC-4).

``--qwen-host`` is the packaged app re-dispatching ITSELF as the isolated Qwen
model host (Task 7.1): a frozen build has no ``python -m <module>`` to hand
the host to, so the same executable runs the host half. It is routed before
any GUI or stdio setup — the host's stdout is its frame channel.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

SMOKE_TIMEOUT_SECONDS = 600.0  # cold start + long text budget
# Duplicated as string literals (verified against qwen_engine.HOST_FLAG and
# qwen_gguf_engine.GGUF_HOST_FLAG in tests) so host re-dispatch and --version
# never import qwen_engine or numpy before branching.
_QWEN_HOST_FLAG = "--qwen-host"
_QWEN_GGUF_HOST_FLAG = "--qwen-gguf-host"


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vienetts-app", description="VieNeuTTS desktop app (GUI) + headless smoke CLI"
    )
    parser.add_argument(
        "--smoke",
        metavar="TEXT",
        help="synthesize TEXT end-to-end and exit (omit to open the GUI)",
    )
    parser.add_argument("--voice", default="Adam", help="preset voice id (default: Adam)")
    parser.add_argument("--stream", action="store_true", help="use the streaming path")
    parser.add_argument("-o", "--output", default="out.wav", help="output WAV path")
    parser.add_argument(
        "--version", action="store_true", help="print the build-stamped version and exit"
    )
    return parser


def run_smoke(
    text: str,
    voice: str,
    output: str | Path,
    stream: bool = False,
    engine_factory: Callable[..., Any] | None = None,
    timeout: float = SMOKE_TIMEOUT_SECONDS,
) -> int:
    """Synthesize ``text`` via the worker; return a process exit code."""
    import multiprocessing

    # Frozen Windows exes (PyInstaller) spawn via multiprocessing: without
    # this the child re-executes the entry point instead of attaching.
    multiprocessing.freeze_support()
    from vienetts_app import ensure_windowed_stdio

    ensure_windowed_stdio()
    # Smoke-only imports (deferred so the GUI path never pays for the
    # engine/worker/numpy import chain before argparse runs).
    from PySide6.QtCore import QCoreApplication

    from vienetts_app.core.artifacts import SynthesisArtifact
    from vienetts_app.core.detector import detect_hardware, detected_engine_info
    from vienetts_app.core.engine import TTSEngine
    from vienetts_app.core.jobs import new_synthesis_job
    from vienetts_app.core.models import TTSRequest
    from vienetts_app.workers.inference_worker import InferenceWorker

    app = QCoreApplication.instance() or QCoreApplication([])

    hw = detect_hardware()
    info = detected_engine_info(hw)
    print(f"engine: {info.note} (backend={info.backend}, precision={info.precision})")

    engine = (engine_factory or (lambda **kw: TTSEngine(**kw)))(
        backend=info.backend, precision=info.precision
    )
    worker = InferenceWorker(engine)
    outcome: dict[str, Any] = {}

    def on_terminal(event: Any) -> None:
        if event.state == "completed" and isinstance(event.value, SynthesisArtifact):
            outcome["artifact"] = event.value
        elif event.state == "completed":
            outcome["error"] = "smoke synthesis produced no artifact"
        elif event.state == "cancelled":
            outcome["error"] = "Cancelled by user"
        else:
            outcome["error"] = str(event.error)

    worker.terminal.connect(on_terminal)
    worker.start()
    try:
        job = new_synthesis_job(
            "text",
            "interactive",
            TTSRequest(text=text, voice=voice, mode="stream" if stream else "infer"),
            artifact_path=Path(output),
        )
        worker.submit(job)
        deadline = time.monotonic() + timeout
        while "artifact" not in outcome and "error" not in outcome:
            if time.monotonic() > deadline:
                print("error: smoke run timed out", file=sys.stderr)
                return 1
            app.processEvents()
            time.sleep(0.01)

        if "error" in outcome:
            print(f"error: {outcome['error']}", file=sys.stderr)
            return 1

        artifact = outcome["artifact"]
        print(f"output: {artifact.path} ({artifact.duration_ms / 1000:.2f}s)")
        return 0
    finally:
        worker.stop()


def _apply_startup_thread_caps() -> None:
    """Settings.blas_threads → BLAS env caps (perf tracks 7.1/7.4).

    Runs before GUI/smoke paths import numpy, because OpenBLAS sizes its pool
    when the library loads. Both imported modules are numpy-free. The default
    caps at 1; ``None`` leaves the environment untouched. Qwen host
    re-dispatches and ``--version`` return before this runs so host
    subprocesses keep their own thread pools.
    """
    from vienetts_app.core import settings as settings_module
    from vienetts_app.core.performance import apply_blas_thread_cap

    apply_blas_thread_cap(settings_module.load_settings().blas_threads)


def main(
    argv: list[str] | None = None,
    engine_factory: Callable[..., Any] | None = None,
    gui_runner: Callable[[], int] | None = None,
) -> int:
    """Entry point for ``python -m vienetts_app``."""
    import multiprocessing

    # See run_smoke: frozen Windows children must attach, not re-execute.
    multiprocessing.freeze_support()
    args_in = sys.argv[1:] if argv is None else argv
    if _QWEN_GGUF_HOST_FLAG in args_in:
        # Frozen-build re-dispatch (qwen_gguf_engine.gguf_host_command): run
        # the host half before BLAS caps, qwen_engine/numpy imports, GUI
        # setup, or ensure_windowed_stdio() — stdout carries protocol frames.
        from vienetts_app.workers.qwen_gguf_host import main as gguf_host_main

        return gguf_host_main()
    if _QWEN_HOST_FLAG in args_in:
        # Frozen-build re-dispatch (qwen_engine.host_command): same early
        # handoff before BLAS caps, qwen_engine/numpy, GUI, or stdio setup.
        from vienetts_app.workers.qwen_host import main as host_main

        return host_main()
    from vienetts_app import ensure_windowed_stdio

    ensure_windowed_stdio()
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.version:
        from vienetts_app import __version__ as _pkg_version
        from vienetts_app._version import get_version

        print(get_version(_pkg_version))
        return 0
    if args.smoke is not None and not args.smoke.strip():
        parser.error("--smoke text must not be blank")
    _apply_startup_thread_caps()
    if args.smoke is None:
        # FR-2.1: no args → GUI. Injectable so tests never spin a real loop.
        if gui_runner is None:
            from vienetts_app.app import run_gui

            gui_runner = run_gui
        return gui_runner()
    return run_smoke(
        text=args.smoke,
        voice=args.voice,
        output=args.output,
        stream=args.stream,
        engine_factory=engine_factory,
    )


if __name__ == "__main__":
    raise SystemExit(main())
