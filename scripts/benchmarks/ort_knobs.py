"""ORT/BLAS knob flags shared by the benchmark children (perf track 7.1)."""

from __future__ import annotations

import argparse
from typing import Any

from vienetts_app.core.engine import OrtTuning


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def add_ort_knob_arguments(parser: argparse.ArgumentParser) -> None:
    """``--threads`` stays with each runner; these are the other knobs."""
    parser.add_argument("--step-single-thread", action="store_true")
    parser.add_argument("--spin", action="store_true")
    parser.add_argument(
        "--blas-threads",
        type=_positive_int,
        default=None,
        help="recorded only: the BLAS cap must be in the environment before numpy "
        "loads, which run_matrix does for its children",
    )


def ort_tuning_kwargs(args: argparse.Namespace) -> dict[str, Any]:
    """``ort_tuning`` for TTSEngine, or nothing when every knob is at its default."""
    tuning = OrtTuning(
        step_session_single_thread=args.step_single_thread,
        spin_during_job=args.spin,
        blas_threads=args.blas_threads,
    )
    return {} if tuning.is_default else {"ort_tuning": tuning}
