"""Measure process-to-QML startup milestones without loading a TTS model."""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

from PySide6.QtCore import QCoreApplication, QTimer
from PySide6.QtQuick import QQuickWindow

from scripts.benchmarks.corpus import get_corpus_entry
from scripts.benchmarks.resources import ResourceSampler
from scripts.benchmarks.schema import (
    BenchmarkRecord,
    BenchmarkScenario,
    environment_manifest,
    write_jsonl,
)
from vienetts_app.core.performance import PerformanceRecorder


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("fake", "real"), default="fake")
    parser.add_argument("--iterations", type=int, default=10)
    parser.add_argument("--hardware-class", default="unspecified")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--output", type=Path, default=Path("benchmark-startup.jsonl"))
    return parser


def _run_one(args: argparse.Namespace, iteration: int) -> BenchmarkRecord:
    from vienetts_app.app import create_app
    from vienetts_app.ui.controller import AppController

    entry = get_corpus_entry("vi_20")
    recorder = PerformanceRecorder(enabled=True)
    job_id = f"startup-{iteration}"
    recorder.begin(
        job_id,
        {
            "mode": "infer",
            "run_kind": "startup",
            "scenario_id": entry.scenario_id,
            "streaming": False,
        },
    )
    recorder.mark(job_id, "process_started")
    sampler = ResourceSampler(interval_seconds=0.01)
    frame_swaps_supported = False
    started_ns = time.perf_counter_ns()
    app = QCoreApplication.instance()
    engine = None
    controller = None
    audiobook = None
    frame_received = False
    with tempfile.TemporaryDirectory(prefix="vienetts-startup-") as data_dir:

        def controller_factory():
            return AppController(
                data_dir=Path(data_dir),
                engine_factory=lambda **_kwargs: None,
                worker_factory=lambda _engine: None,
                catalog=lambda: [],
                saved_names=lambda _voices_dir: [],
            )

        def observer(event: str) -> None:
            recorder.mark(job_id, event)

        try:
            sampler.start()
            app, engine = create_app(
                controller_factory=controller_factory,
                startup_observer=observer,
            )
            controller = engine._controller
            audiobook = engine._audiobook
            root = engine.rootObjects()[0]
            if isinstance(root, QQuickWindow):

                def on_exposed() -> None:
                    recorder.mark(job_id, "window_exposed")

                def on_frame_swapped() -> None:
                    nonlocal frame_received, frame_swaps_supported
                    frame_received = True
                    frame_swaps_supported = True
                    recorder.mark(job_id, "first_frame_swapped")

                root.visibleChanged.connect(on_exposed)
                root.frameSwapped.connect(on_frame_swapped)
                if root.isVisible() or root.isExposed():
                    recorder.mark(job_id, "window_exposed")
            deadline = time.monotonic() + args.timeout
            while time.monotonic() < deadline and not frame_received:
                app.processEvents()
                QTimer.singleShot(0, lambda: None)
                time.sleep(0.001)
            recorder.finish(job_id, "completed")
        except Exception:
            recorder.finish(job_id, "failed")
            raise
        finally:
            sampler.stop()
            if audiobook is not None:
                audiobook.shutdown()
            if controller is not None:
                controller.shutdown()
    trace = recorder.snapshot(job_id)[0]
    trace["startup"] = {
        "frame_swaps_supported": frame_swaps_supported,
    }
    elapsed_ns = time.perf_counter_ns() - started_ns
    scenario = BenchmarkScenario.from_entry(
        entry,
        backend="startup",
        precision="none",
        mode="infer",
        sink_kind="none",
    )
    return BenchmarkRecord(
        environment=environment_manifest(hardware_class=args.hardware_class),
        scenario=scenario,
        trace=trace,
        resources=sampler.result(),
        elapsed_ns=elapsed_ns,
    )


def run(args: argparse.Namespace) -> int:
    if args.iterations < 1:
        raise ValueError("iterations must be positive")
    records = [_run_one(args, iteration) for iteration in range(args.iterations)]
    write_jsonl(records, args.output)
    print(args.output)
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
