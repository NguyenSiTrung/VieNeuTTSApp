"""Run benchmark scenarios in fresh child processes.

ORT/BLAS knobs (perf track 7.1) are swept: ``--threads``, ``--step-single-thread``,
``--spin`` and ``--blas-threads`` each take one or more values, and every
combination is a cell run with the same cold/warm iterations. Each record is
stamped with its ``matrix_cell``. The default is one untuned cell, which
passes no knob flags.
"""

from __future__ import annotations

import argparse
import itertools
import json
import os
import subprocess
import sys
import tempfile
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from pathlib import Path

from scripts.benchmarks.corpus import get_corpus_entry
from vienetts_app.core.performance import apply_blas_thread_cap


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
    parser.add_argument("--engine", choices=("fake", "real"), default="real")
    parser.add_argument("--scenario", nargs="+", default=["vi_50"])
    parser.add_argument("--mode", choices=("stream", "infer"), default="stream")
    parser.add_argument("--backend", choices=("onnx", "torch"), default="onnx")
    parser.add_argument(
        "--cuda-runtime",
        type=Path,
        default=None,
        metavar="DIR",
        help="managed CUDA runtime root forwarded to child runs (see run_engine)",
    )
    parser.add_argument("--precision", choices=("int8", "fp32"), default="int8")
    parser.add_argument("--threads", type=_nonnegative_int, nargs="+", default=None)
    parser.add_argument("--step-single-thread", choices=("off", "on"), nargs="+", default=["off"])
    parser.add_argument("--spin", choices=("off", "on"), nargs="+", default=["off"])
    parser.add_argument("--blas-threads", type=_positive_int, nargs="+", default=None)
    parser.add_argument("--max-batch-size", type=_positive_int, default=None)
    parser.add_argument("--path", choices=("direct", "pipeline"), default="pipeline")
    parser.add_argument("--sink", choices=("fake", "real", "null"), default="fake")
    parser.add_argument("--hardware-class", default="unspecified")
    parser.add_argument("--cold-iterations", type=_nonnegative_int, default=5)
    parser.add_argument("--warm-iterations", type=_nonnegative_int, default=20)
    parser.add_argument("--output", type=Path, default=Path("benchmark-matrix.jsonl"))
    return parser


@dataclass(frozen=True)
class KnobCell:
    """One ORT/BLAS knob combination; the defaults are the SDK's own."""

    threads: int | None = None
    step_single_thread: bool = False
    spin: bool = False
    blas_threads: int | None = None


def _cells(args: argparse.Namespace) -> list[KnobCell]:
    return [
        KnobCell(threads, step == "on", spin == "on", blas)
        for threads, step, spin, blas in itertools.product(
            args.threads or [None],
            args.step_single_thread,
            args.spin,
            args.blas_threads or [None],
        )
    ]


def _child_env(cell: KnobCell) -> dict[str, str]:
    """Environment for the cell's child: the BLAS cap must exist before numpy loads."""
    env: dict[str, str] = {}
    apply_blas_thread_cap(cell.blas_threads, env)
    return env


def _append_line(output: Path, payload: dict[str, object]) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())


def _child_command(
    args: argparse.Namespace,
    scenario: str,
    child_output: Path,
    *,
    cell: KnobCell | None = None,
    warmup_iterations: int = 0,
    iterations: int = 1,
) -> list[str]:
    cell = cell or KnobCell()
    module = (
        "scripts.benchmarks.run_engine" if args.path == "direct" else "scripts.benchmarks.run_once"
    )
    command = [
        sys.executable,
        "-m",
        module,
        "--engine",
        args.engine,
        "--scenario",
        scenario,
        "--mode",
        args.mode,
        "--backend",
        args.backend,
        "--precision",
        args.precision,
        "--hardware-class",
        args.hardware_class,
        "--output",
        str(child_output),
    ]
    if cell.threads is not None:
        command.extend(["--threads", str(cell.threads)])
    if cell.step_single_thread:
        command.append("--step-single-thread")
    if cell.spin:
        command.append("--spin")
    if cell.blas_threads is not None:
        command.extend(["--blas-threads", str(cell.blas_threads)])
    if args.max_batch_size is not None:
        command.extend(["--max-batch-size", str(args.max_batch_size)])
    if args.cuda_runtime is not None:
        command.extend(["--cuda-runtime", str(args.cuda_runtime)])
    if args.path == "pipeline":
        command.extend(
            [
                "--sink",
                args.sink,
                "--warmup-iterations",
                str(warmup_iterations),
                "--iterations",
                str(iterations),
            ]
        )
    else:
        command.extend(
            [
                "--warmup-iterations",
                str(warmup_iterations),
                "--iterations",
                str(iterations),
            ]
        )
    return command


def _run_child(
    command: list[str], output: Path, env: Mapping[str, str] | None = None
) -> list[dict[str, object]]:
    try:
        process = subprocess.run(
            command,
            cwd=Path.cwd(),
            env={
                **os.environ,
                "QT_QPA_PLATFORM": os.environ.get("QT_QPA_PLATFORM", "offscreen"),
                **(env or {}),
            },
            capture_output=True,
            text=True,
            check=False,
            timeout=900.0,
        )
    except subprocess.TimeoutExpired as exc:
        # Generous for real-engine manual runs, but a deadlocked child must
        # surface as an error instead of hanging the parent forever.
        raise RuntimeError("benchmark child timed out after 900 s") from exc
    if process.returncode != 0:
        raise RuntimeError(process.stderr or process.stdout or "benchmark child failed")
    return [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]


def run(args: argparse.Namespace) -> int:
    if args.cold_iterations < 0 or args.warm_iterations < 0:
        raise ValueError("iteration counts must be non-negative")
    for scenario in args.scenario:
        get_corpus_entry(scenario)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text("", encoding="utf-8")
    with tempfile.TemporaryDirectory(prefix="vienetts-matrix-") as temp_dir:
        temp_root = Path(temp_dir)
        for cell_index, cell in enumerate(_cells(args)):
            env = _child_env(cell)
            stamp = asdict(cell)
            for scenario in args.scenario:
                for index in range(args.cold_iterations):
                    child_output = temp_root / f"cold-{cell_index}-{scenario}-{index}.jsonl"
                    child_output.unlink(missing_ok=True)
                    command = _child_command(args, scenario, child_output, cell=cell)
                    for payload in _run_child(command, child_output, env):
                        payload["matrix_run_kind"] = "cold"
                        payload["matrix_cell"] = stamp
                        _append_line(args.output, payload)
                if args.warm_iterations:
                    child_output = temp_root / f"warm-{cell_index}-{scenario}.jsonl"
                    child_output.unlink(missing_ok=True)
                    command = _child_command(
                        args,
                        scenario,
                        child_output,
                        cell=cell,
                        warmup_iterations=1,
                        iterations=args.warm_iterations,
                    )
                    for payload in _run_child(command, child_output, env):
                        payload["matrix_run_kind"] = "warm"
                        payload["matrix_cell"] = stamp
                        _append_line(args.output, payload)
    print(args.output)
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
