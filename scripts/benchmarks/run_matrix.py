"""Run benchmark scenarios in fresh child processes."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from scripts.benchmarks.corpus import get_corpus_entry


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
    parser.add_argument("--precision", choices=("int8", "fp32"), default="int8")
    parser.add_argument("--threads", type=_nonnegative_int, default=None)
    parser.add_argument("--max-batch-size", type=_positive_int, default=None)
    parser.add_argument("--path", choices=("direct", "pipeline"), default="pipeline")
    parser.add_argument("--sink", choices=("fake", "real", "null"), default="fake")
    parser.add_argument("--hardware-class", default="unspecified")
    parser.add_argument("--cold-iterations", type=_nonnegative_int, default=5)
    parser.add_argument("--warm-iterations", type=_nonnegative_int, default=20)
    parser.add_argument("--output", type=Path, default=Path("benchmark-matrix.jsonl"))
    return parser


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
    warmup_iterations: int = 0,
    iterations: int = 1,
) -> list[str]:
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
    if args.threads is not None:
        command.extend(["--threads", str(args.threads)])
    if args.max_batch_size is not None:
        command.extend(["--max-batch-size", str(args.max_batch_size)])
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


def _run_child(command: list[str], output: Path) -> list[dict[str, object]]:
    process = subprocess.run(
        command,
        cwd=Path.cwd(),
        env={**os.environ, "QT_QPA_PLATFORM": os.environ.get("QT_QPA_PLATFORM", "offscreen")},
        capture_output=True,
        text=True,
        check=False,
    )
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
        for scenario in args.scenario:
            for index in range(args.cold_iterations):
                child_output = temp_root / f"cold-{scenario}-{index}.jsonl"
                child_output.unlink(missing_ok=True)
                command = _child_command(args, scenario, child_output)
                for payload in _run_child(command, child_output):
                    payload["matrix_run_kind"] = "cold"
                    _append_line(args.output, payload)
            if args.warm_iterations:
                child_output = temp_root / f"warm-{scenario}.jsonl"
                child_output.unlink(missing_ok=True)
                command = _child_command(
                    args,
                    scenario,
                    child_output,
                    warmup_iterations=1,
                    iterations=args.warm_iterations,
                )
                for payload in _run_child(command, child_output):
                    payload["matrix_run_kind"] = "warm"
                    _append_line(args.output, payload)
    print(args.output)
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
