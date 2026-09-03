"""Measure synthesis while the real QML shell is rendering."""

from __future__ import annotations

import argparse
import sys
import tempfile
import time
from pathlib import Path

import numpy as np
from PySide6.QtCore import QCoreApplication
from PySide6.QtQuick import QQuickWindow

from scripts.benchmarks.corpus import get_corpus_entry
from scripts.benchmarks.fakes import DeterministicEngine, EventLoopProbe, RateLimitedSink
from scripts.benchmarks.resources import ResourceSampler
from scripts.benchmarks.schema import (
    BenchmarkRecord,
    BenchmarkScenario,
    environment_manifest,
)
from scripts.benchmarks.statistics import summarize
from vienetts_app.core.engine import TTSEngine
from vienetts_app.core.performance import PerformanceRecorder
from vienetts_app.ui.controller import AppController
from vienetts_app.ui.playback import PlaybackController
from vienetts_app.ui.stream_playback import StreamPlaybackController
from vienetts_app.workers.inference_worker import InferenceWorker


def _nonnegative_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 0:
        raise argparse.ArgumentTypeError("must be non-negative")
    return parsed


def _positive_int(value: str) -> int:
    parsed = _nonnegative_int(value)
    if parsed == 0:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--engine", choices=("fake", "real"), default="fake")
    parser.add_argument("--scenario", nargs="+", default=["vi_50"])
    parser.add_argument("--backend", choices=("onnx", "torch"), default="onnx")
    parser.add_argument("--precision", choices=("int8", "fp32"), default="int8")
    parser.add_argument("--threads", type=_nonnegative_int, default=None)
    parser.add_argument("--max-batch-size", type=_positive_int, default=None)
    parser.add_argument("--sink", choices=("fake", "real", "null"), default="fake")
    parser.add_argument("--sink-rate", type=float, default=1.0)
    parser.add_argument("--iterations", type=_positive_int, default=1)
    parser.add_argument("--hardware-class", default="unspecified")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output", type=Path, default=Path("benchmark-ui.jsonl"))
    return parser


def _pump(app: QCoreApplication, predicate, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.001)
    app.processEvents()
    return bool(predicate())


def _frame_summary(intervals_ms: list[float]) -> dict[str, object]:
    if not intervals_ms:
        return {
            "supported": False,
            "sample_count": 0,
            "frames_above_16_7_ms": 0,
            "frames_above_33_3_ms": 0,
        }
    distribution = summarize(intervals_ms)
    return {
        "supported": True,
        "sample_count": distribution.count,
        "median_interval_ms": distribution.median,
        "p95_interval_ms": distribution.p95,
        "maximum_interval_ms": distribution.maximum,
        "frames_above_16_7_ms": sum(value > 16.7 for value in intervals_ms),
        "frames_above_33_3_ms": sum(value > 33.3 for value in intervals_ms),
    }


def _run_one(args: argparse.Namespace, entry_id: str, iteration: int) -> BenchmarkRecord:
    entry = get_corpus_entry(entry_id)
    recorder = PerformanceRecorder(enabled=True)
    engine = (
        DeterministicEngine()
        if args.engine == "fake"
        else TTSEngine(
            backend=args.backend,
            precision=args.precision,
            threads=args.threads,
            max_batch_size=args.max_batch_size,
        )
    )
    sink = RateLimitedSink(args.sink_rate) if args.sink == "fake" else None
    event_loop = EventLoopProbe()
    sampler = ResourceSampler(sample_cuda=args.engine == "real" and args.backend == "torch")
    frame_times_ns: list[int] = []
    frame_start_index = 0
    job_id: str | None = None
    controller = None
    audiobook = None
    app = None
    with tempfile.TemporaryDirectory(prefix="vienetts-ui-") as data_dir:

        def engine_factory(**_kwargs):
            return engine

        def worker_factory(current_engine):
            return InferenceWorker(current_engine, performance_recorder=recorder)

        def stream_playback_factory():
            if args.sink == "null":
                return StreamPlaybackController(
                    sink_factory=lambda _format: None,
                    format_factory=lambda: None,
                    performance_recorder=recorder,
                )
            if args.sink == "real":
                return StreamPlaybackController(performance_recorder=recorder)
            assert sink is not None
            return StreamPlaybackController(
                sink_factory=lambda _format: sink,
                format_factory=lambda: None,
                performance_recorder=recorder,
            )

        def controller_factory():
            return AppController(
                data_dir=Path(data_dir),
                engine_factory=engine_factory,
                worker_factory=worker_factory,
                catalog=lambda: [],
                saved_names=lambda _voices_dir: [],
                stream_playback_factory=stream_playback_factory,
                performance_recorder=recorder,
            )

        started_ns = time.perf_counter_ns()
        try:
            from vienetts_app.app import create_app

            app, qml_engine = create_app(
                controller_factory=controller_factory,
                playback_factory=PlaybackController,
            )
            controller = qml_engine._controller
            audiobook = qml_engine._audiobook
            root = qml_engine.rootObjects()[0]
            if isinstance(root, QQuickWindow):
                root.frameSwapped.connect(lambda: frame_times_ns.append(time.perf_counter_ns()))
            event_loop.start()
            sampler.start()
            idle_ready = _pump(app, lambda: len(frame_times_ns) >= 2, min(args.timeout, 2.0))
            if not idle_ready:
                _pump(app, lambda: False, 0.05)
            frame_start_index = len(frame_times_ns)
            controller.generateStream(entry.text, "")
            job_id = controller.foregroundJobId or None
            completed = _pump(app, lambda: not controller.busy, args.timeout)
            if not completed and job_id is not None:
                controller.cancel()
                _pump(app, lambda: not controller.busy, min(args.timeout, 2.0))
                recorder.finish(job_id, "failed")
            if sink is not None and completed:
                _pump(app, lambda: sink.is_drained, args.timeout)
            _pump(app, lambda: len(event_loop.delays_ms) >= 1, min(args.timeout, 1.0))
            frame_intervals = [
                (later - earlier) / 1_000_000
                for earlier, later in zip(
                    frame_times_ns[frame_start_index:],
                    frame_times_ns[frame_start_index + 1 :],
                    strict=False,
                )
            ]
            trace = recorder.snapshot(job_id)[0] if job_id else {"outcome": "failed"}
            trace["run_kind"] = "ui_pipeline"
            scenario = BenchmarkScenario.from_entry(
                entry,
                backend=args.backend,
                precision=args.precision,
                mode="stream",
                intra_op_threads=args.threads,
                max_batch_size=args.max_batch_size,
                sink_kind=args.sink,
            )
            audio = controller._audio if controller is not None else None
            audio_duration_ms = (
                float(np.asarray(audio).size * 1000 / engine.sample_rate)
                if audio is not None
                else None
            )
            return BenchmarkRecord(
                environment=environment_manifest(hardware_class=args.hardware_class),
                scenario=scenario,
                trace=trace,
                resources=sampler.result(),
                elapsed_ns=time.perf_counter_ns() - started_ns,
                audio_duration_ms=audio_duration_ms,
                event_loop=event_loop.result().to_dict(),
            ), _frame_summary(frame_intervals)
        finally:
            sampler.stop()
            event_loop.stop()
            if audiobook is not None:
                audiobook.shutdown()
            if controller is not None:
                controller.shutdown()


def run(args: argparse.Namespace) -> int:
    if args.iterations < 1:
        raise ValueError("iterations must be positive")
    records = []
    for entry_id in args.scenario:
        for iteration in range(args.iterations):
            record, frames = _run_one(args, entry_id, iteration)
            payload = record.to_dict()
            payload["run_kind"] = "ui_pipeline"
            payload["frames"] = frames
            records.append(payload)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(
        "".join(
            __import__("json").dumps(payload, ensure_ascii=False, sort_keys=True) + "\n"
            for payload in records
        ),
        encoding="utf-8",
    )
    print(args.output)
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
