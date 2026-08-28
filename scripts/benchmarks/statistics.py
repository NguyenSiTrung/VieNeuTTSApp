"""Small, deterministic distribution summaries for benchmark records."""

from __future__ import annotations

import math
import statistics as _statistics
from collections.abc import Sequence
from dataclasses import dataclass


@dataclass(frozen=True)
class Distribution:
    count: int
    minimum: float
    median: float
    p90: float
    p95: float
    maximum: float
    mad: float


def _nearest_rank(values: Sequence[float], percentile: float) -> float:
    ordered = sorted(float(value) for value in values)
    rank = max(1, math.ceil(percentile * len(ordered)))
    return ordered[rank - 1]


def summarize(values: Sequence[float]) -> Distribution:
    if not values:
        raise ValueError("at least one value is required")
    ordered = [float(value) for value in values]
    median = float(_statistics.median(ordered))
    deviations = [abs(value - median) for value in ordered]
    return Distribution(
        count=len(ordered),
        minimum=min(ordered),
        median=median,
        p90=_nearest_rank(ordered, 0.90),
        p95=_nearest_rank(ordered, 0.95),
        maximum=max(ordered),
        mad=float(_statistics.median(deviations)),
    )
