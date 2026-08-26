"""Headless CLI: ``python -m vienetts_app --smoke "Xin chào" --voice Adam -o out.wav``.

Runs synthesis end-to-end through the threaded worker (never the main
thread), prints the detected engine, and exits 0 only when a valid WAV was
written.
"""

from __future__ import annotations

import argparse
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from vienetts_app.core.audio import write_wav_file
from vienetts_app.core.detector import detect_hardware, detected_engine_info
from vienetts_app.core.engine import TTSEngine
from vienetts_app.core.models import TTSRequest
from vienetts_app.workers.inference_worker import InferenceWorker

SMOKE_TIMEOUT_SECONDS = 600.0  # cold start + long text budget


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="vienetts-app", description="VieNeuTTS headless smoke CLI (Phase 1)"
    )
    parser.add_argument(
        "--smoke", metavar="TEXT", required=True, help="synthesize TEXT end-to-end and exit"
    )
    parser.add_argument("--voice", default="Adam", help="preset voice id (default: Adam)")
    parser.add_argument("--stream", action="store_true", help="use the streaming path")
    parser.add_argument("-o", "--output", default="out.wav", help="output WAV path")
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
    from PySide6.QtCore import QCoreApplication

    app = QCoreApplication.instance() or QCoreApplication([])

    hw = detect_hardware()
    info = detected_engine_info(hw)
    print(f"engine: {info.note} (backend={info.backend}, precision={info.precision})")

    engine = (engine_factory or (lambda **kw: TTSEngine(**kw)))(
        backend=info.backend, precision=info.precision
    )
    worker = InferenceWorker(engine)
    outcome: dict[str, Any] = {}
    worker.done.connect(lambda audio: outcome.__setitem__("audio", audio))
    worker.error.connect(lambda msg: outcome.__setitem__("error", msg))
    worker.start()
    try:
        worker.submit(TTSRequest(text=text, voice=voice, mode="stream" if stream else "infer"))
        deadline = time.monotonic() + timeout
        while "audio" not in outcome and "error" not in outcome:
            if time.monotonic() > deadline:
                print("error: smoke run timed out", file=sys.stderr)
                return 1
            app.processEvents()
            time.sleep(0.01)

        if "error" in outcome:
            print(f"error: {outcome['error']}", file=sys.stderr)
            return 1

        path = write_wav_file(outcome["audio"], output, sample_rate=engine.sample_rate)
        print(f"output: {path} ({len(outcome['audio']) / engine.sample_rate:.2f}s)")
        return 0
    finally:
        worker.stop()


def main(argv: list[str] | None = None, engine_factory: Callable[..., Any] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.smoke.strip():
        parser.error("--smoke text must not be blank")
    return run_smoke(
        text=args.smoke,
        voice=args.voice,
        output=args.output,
        stream=args.stream,
        engine_factory=engine_factory,
    )


if __name__ == "__main__":
    raise SystemExit(main())
