"""Measure the engine directly, without controller or audio transport."""

from __future__ import annotations

import argparse
import contextlib
import sys
import time
from pathlib import Path

import numpy as np

from scripts.benchmarks.corpus import get_corpus_entry
from scripts.benchmarks.fakes import DeterministicEngine
from scripts.benchmarks.resources import ResourceSampler
from scripts.benchmarks.schema import (
    BenchmarkRecord,
    BenchmarkScenario,
    environment_manifest,
    write_jsonl,
)
from vienetts_app.core.engine import TTSEngine
from vienetts_app.core.performance import PerformanceRecorder


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
    parser.add_argument("--warmup-iterations", type=_nonnegative_int, default=0)
    parser.add_argument("--iterations", type=_positive_int, default=1)
    parser.add_argument("--hardware-class", default="unspecified")
    parser.add_argument("--output", type=Path, default=Path("benchmark-engine.jsonl"))
    return parser


def _make_engine(args: argparse.Namespace):
    if args.engine == "fake":
        return DeterministicEngine()
    return TTSEngine(
        backend=args.backend,
        precision=args.precision,
        threads=args.threads,
        max_batch_size=args.max_batch_size,
    )


def _consume_audio(engine, entry_text: str, mode: str) -> int:
    if mode == "stream":
        return sum(
            np.asarray(chunk, dtype=np.float32).size for chunk in engine.infer_stream(entry_text)
        )
    return int(np.asarray(engine.infer(entry_text), dtype=np.float32).size)


def _run_measured_job(
    args: argparse.Namespace,
    entry,
    engine,
    recorder: PerformanceRecorder,
    iteration: int,
    *,
    initialize: bool,
) -> BenchmarkRecord:
    job_id = f"direct-engine-{iteration}"
    recorder.begin(
        job_id,
        {
            "backend": args.backend,
            "engine": args.engine,
            "intra_op_threads": args.threads,
            "max_batch_size": args.max_batch_size,
            "mode": args.mode,
            "precision": args.precision,
            "run_kind": "direct_engine",
            "scenario_id": entry.scenario_id,
            "streaming": args.mode == "stream",
        },
    )
    sampler = ResourceSampler(sample_cuda=args.engine == "real" and args.backend == "torch")
    started_ns = time.perf_counter_ns()
    audio_samples = 0
    try:
        recorder.mark(job_id, "engine_constructed")
        sampler.start()
        if initialize:
            recorder.mark(job_id, "engine_initialize_started")
            initializer = getattr(engine, "initialize", None)
            if initializer is not None:
                initializer()
            recorder.mark(job_id, "engine_initialize_completed")
        recorder.mark(job_id, "engine_call_started")
        first_chunk = True
        if args.mode == "stream":
            for chunk in engine.infer_stream(entry.text):
                array = np.asarray(chunk, dtype=np.float32)
                audio_samples += int(array.size)
                if first_chunk:
                    recorder.mark(job_id, "engine_first_chunk")
                    first_chunk = False
        else:
            audio_samples = _consume_audio(engine, entry.text, args.mode)
        recorder.observe_max(job_id, "audio_samples", audio_samples)
        recorder.mark(job_id, "engine_completed")
        recorder.finish(job_id, "completed")
    except Exception:
        recorder.mark(job_id, "engine_failed")
        recorder.finish(job_id, "failed")
    finally:
        sampler.stop()
    elapsed_ns = time.perf_counter_ns() - started_ns
    try:
        sample_rate = int(engine.sample_rate)
    except Exception:  # noqa: BLE001 - failed initialization has no sample rate
        sample_rate = 48_000
    resolved_backend = None
    if recorder.snapshot(job_id)[0]["outcome"] in {"completed", "cancelled"}:
        with contextlib.suppress(Exception):
            resolved_backend = str(engine.backend)
    scenario = BenchmarkScenario.from_entry(
        entry,
        backend=args.backend,
        precision=args.precision,
        mode=args.mode,
        intra_op_threads=args.threads,
        max_batch_size=args.max_batch_size,
        sink_kind="none",
        resolved_backend=resolved_backend,
    )
    record = BenchmarkRecord(
        environment=environment_manifest(hardware_class=args.hardware_class),
        scenario=scenario,
        trace=recorder.snapshot(job_id)[0],
        resources=sampler.result(),
        elapsed_ns=elapsed_ns,
        audio_duration_ms=float(audio_samples * 1000 / sample_rate) if audio_samples else None,
    )
    return record


def run(args: argparse.Namespace) -> int:
    if args.warmup_iterations < 0 or args.iterations < 1:
        raise ValueError("warmup-iterations must be non-negative and iterations must be positive")
    entry = get_corpus_entry(args.scenario)
    recorder = PerformanceRecorder(enabled=True)
    engine = _make_engine(args)
    initialized = False
    try:
        if args.warmup_iterations:
            try:
                initializer = getattr(engine, "initialize", None)
                if initializer is not None:
                    initializer()
                initialized = True
                for _ in range(args.warmup_iterations):
                    _consume_audio(engine, entry.text, args.mode)
            except Exception:
                initialized = False

        records = []
        for iteration in range(args.iterations):
            record = _run_measured_job(
                args,
                entry,
                engine,
                recorder,
                iteration,
                initialize=not initialized,
            )
            records.append(record)
            initialized = str(record.trace.get("outcome")) == "completed"
        write_jsonl(records, args.output)
    finally:
        engine.close()
    outcomes = [str(record.trace.get("outcome")) for record in records]
    elapsed_ms = sum(record.elapsed_ns for record in records) / 1_000_000
    print(f"{args.output}: records={len(records)} elapsed_ms={elapsed_ms:.2f}")
    return 0 if all(outcome == "completed" for outcome in outcomes) else 1


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    return run(args)


if __name__ == "__main__":
    sys.exit(main())
