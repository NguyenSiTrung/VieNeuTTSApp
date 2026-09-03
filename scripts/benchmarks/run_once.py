"""Run one deterministic full controller/worker/transport benchmark."""

from __future__ import annotations

import argparse
import contextlib
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QCoreApplication

from scripts.benchmarks.corpus import get_corpus_entry
from scripts.benchmarks.fakes import DeterministicEngine, EventLoopProbe, RateLimitedSink
from scripts.benchmarks.resources import ResourceSampler
from scripts.benchmarks.schema import (
    BenchmarkRecord,
    BenchmarkScenario,
    environment_manifest,
    write_jsonl,
)
from vienetts_app.core.engine import TTSEngine
from vienetts_app.core.performance import PerformanceRecorder
from vienetts_app.ui.controller import AppController
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
    parser.add_argument("--scenario", default="vi_50")
    parser.add_argument("--mode", choices=("stream", "infer"), default="stream")
    parser.add_argument("--backend", choices=("onnx", "torch"), default="onnx")
    parser.add_argument("--precision", choices=("int8", "fp32"), default="int8")
    parser.add_argument("--threads", type=_nonnegative_int, default=None)
    parser.add_argument("--max-batch-size", type=_positive_int, default=None)
    parser.add_argument("--sink", choices=("fake", "real", "null"), default="fake")
    parser.add_argument("--sink-rate", type=float, default=1.0)
    parser.add_argument("--cancel-after-first-chunk", action="store_true")
    parser.add_argument("--warmup-iterations", type=_nonnegative_int, default=0)
    parser.add_argument("--iterations", type=_positive_int, default=1)
    parser.add_argument("--hardware-class", default="unspecified")
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--output", type=Path, default=Path("benchmark-record.jsonl"))
    return parser


def _pump_until(
    app: QCoreApplication,
    predicate,
    timeout_seconds: float,
) -> bool:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.001)
    app.processEvents()
    return bool(predicate())


class _NullStreamPlayback:
    active = False
    errorText = ""

    def set_performance_recorder(self, _recorder: PerformanceRecorder) -> None:
        return None

    def begin_trace(self, _job_id: str | None) -> None:
        return None

    def start(self) -> None:
        self.active = True

    def stop(self) -> None:
        self.active = False

    def feed(self, _chunk: Any) -> None:
        return None


def _make_engine(args: argparse.Namespace):
    if args.engine == "fake":
        return DeterministicEngine()
    return TTSEngine(
        backend=args.backend,
        precision=args.precision,
        threads=args.threads,
        max_batch_size=args.max_batch_size,
    )


def run(args: argparse.Namespace) -> int:
    if args.warmup_iterations < 0 or args.iterations < 1:
        raise ValueError("warmup-iterations must be non-negative and iterations must be positive")
    entry = get_corpus_entry(args.scenario)
    recorder = PerformanceRecorder(enabled=True)
    engine = _make_engine(args)
    sink = RateLimitedSink(args.sink_rate) if args.sink == "fake" else None
    app = QCoreApplication.instance() or QCoreApplication([])

    with tempfile.TemporaryDirectory(prefix="vienetts-benchmark-") as data_dir:

        def engine_factory(**_kwargs):
            return engine

        def worker_factory(current_engine):
            return InferenceWorker(current_engine, performance_recorder=recorder)

        def playback_factory():
            if args.sink == "null":
                return _NullStreamPlayback()
            if args.sink == "real":
                return StreamPlaybackController(performance_recorder=recorder)
            assert sink is not None
            return StreamPlaybackController(
                sink_factory=lambda _format: sink,
                format_factory=lambda: None,
                performance_recorder=recorder,
            )

        controller = AppController(
            data_dir=Path(data_dir),
            engine_factory=engine_factory,
            worker_factory=worker_factory,
            catalog=lambda: [],
            saved_names=lambda _voices_dir: [],
            stream_playback_factory=playback_factory,
            performance_recorder=recorder,
        )

        def run_job(*, measured: bool, iteration: int) -> BenchmarkRecord | None:
            probe = EventLoopProbe() if measured else None
            sampler = (
                ResourceSampler(
                    interval_seconds=0.01,
                    sample_cuda=args.engine == "real" and args.backend == "torch",
                )
                if measured
                else None
            )
            started_ns = time.perf_counter_ns()
            if probe is not None:
                probe.start()
            if sampler is not None:
                sampler.start()
            if args.mode == "stream":
                controller.generateStream(entry.text, "")
            else:
                controller.generate(entry.text, "")
            job_id = controller.foregroundJobId or None
            worker = controller._worker
            cancel_requested = False

            def cancel_on_chunk(_chunk) -> None:
                nonlocal cancel_requested
                if not cancel_requested:
                    cancel_requested = True
                    controller.cancel()

            if args.cancel_after_first_chunk and measured and worker is not None:
                worker.chunk_ready.connect(cancel_on_chunk)

            timed_out = False
            sink_timed_out = False
            terminal_ns: int | None = None
            try:
                terminal = _pump_until(
                    app,
                    lambda: not controller.busy,
                    args.timeout,
                )
                if terminal:
                    terminal_ns = time.perf_counter_ns()
                if not terminal:
                    timed_out = True
                    controller.cancel()
                    if job_id is not None:
                        recorder.mark(job_id, "benchmark_timeout")
                        recorder.finish(job_id, "failed")

                if (
                    args.mode == "stream"
                    and not timed_out
                    and args.sink == "fake"
                    and not _pump_until(
                        app,
                        lambda: sink is not None and sink.is_drained,
                        args.timeout,
                    )
                ):
                    sink_timed_out = True
                    if job_id is not None:
                        recorder.mark(job_id, "benchmark_sink_timeout")
                        recorder.finish(job_id, "failed")
                if probe is not None:
                    _pump_until(app, lambda: len(probe.delays_ms) >= 1, min(args.timeout, 1.0))
            finally:
                if sampler is not None:
                    sampler.stop()
                if probe is not None:
                    probe.stop()
                if args.cancel_after_first_chunk and measured and worker is not None:
                    worker.chunk_ready.disconnect(cancel_on_chunk)

            if not measured:
                return None

            elapsed_ns = max(0, (terminal_ns or time.perf_counter_ns()) - started_ns)
            trace = recorder.snapshot(job_id)[0] if job_id else {"outcome": "failed"}
            outcome = str(trace.get("outcome") or "failed")
            if timed_out or sink_timed_out:
                outcome = "failed"
                recorder.finish(job_id, outcome)
            if args.sink == "real" and controller.errorText:
                recorder.mark(job_id, "audio_sink_failed")
                recorder.finish(job_id, "failed")
                outcome = "failed"
            trace = recorder.snapshot(job_id)[0] if job_id else {"outcome": outcome}
            resolved_backend = None
            if outcome in {"completed", "cancelled"}:
                with contextlib.suppress(Exception):
                    resolved_backend = str(engine.backend)
            audio = controller._audio
            try:
                sample_rate = engine.sample_rate
            except Exception:  # noqa: BLE001 - failed initialization has no rate
                sample_rate = 48_000
            audio_duration_ms = (
                float(np.asarray(audio).size * 1000 / sample_rate) if audio is not None else None
            )
            scenario = BenchmarkScenario.from_entry(
                entry,
                backend=args.backend,
                precision=args.precision,
                mode=args.mode,
                intra_op_threads=args.threads,
                max_batch_size=args.max_batch_size,
                sink_kind=args.sink,
                resolved_backend=resolved_backend,
            )
            recorder.mark(job_id, "benchmark_iteration", iteration)
            record = BenchmarkRecord(
                environment=environment_manifest(hardware_class=args.hardware_class),
                scenario=scenario,
                trace=recorder.snapshot(job_id)[0] if job_id else trace,
                resources=sampler.result() if sampler is not None else ResourceSampler().result(),
                elapsed_ns=elapsed_ns,
                audio_duration_ms=audio_duration_ms,
                event_loop=probe.result().to_dict() if probe is not None else None,
            )
            return record

        try:
            for _ in range(args.warmup_iterations):
                run_job(measured=False, iteration=-1)
            records = [
                record
                for iteration in range(args.iterations)
                if (record := run_job(measured=True, iteration=iteration)) is not None
            ]
            write_jsonl(records, args.output)
            outcomes = [str(record.trace.get("outcome")) for record in records]
            success = all(outcome in {"completed", "cancelled"} for outcome in outcomes)
            elapsed_ms = sum(record.elapsed_ns for record in records) / 1_000_000
            print(f"{args.output}: records={len(records)} elapsed_ms={elapsed_ms:.2f}")
            return 0 if success else 1
        finally:
            controller.shutdown()


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
