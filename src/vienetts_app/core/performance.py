"""Thread-safe, content-safe performance tracing for local measurements."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field

JSONScalar = str | int | float | bool | None

SAFE_TAG_KEYS = frozenset(
    {
        "backend",
        "char_count",
        "engine",
        "intra_op_threads",
        "max_batch_size",
        "mode",
        "precision",
        "run_kind",
        "scenario_id",
        "sink_kind",
        "streaming",
        "voice_kind",
    }
)


@dataclass(frozen=True)
class PerformanceEvent:
    """One event relative to the beginning of a trace."""

    name: str
    offset_ns: int
    value: int | float | None = None


@dataclass(frozen=True)
class PerformanceTrace:
    """Immutable public representation of a performance trace."""

    job_id: str
    started_ns: int
    tags: Mapping[str, JSONScalar]
    events: tuple[PerformanceEvent, ...] = ()
    maxima: Mapping[str, int | float] = field(default_factory=dict)
    counters: Mapping[str, int | float] = field(default_factory=dict)
    outcome: str | None = None


@dataclass
class _TraceState:
    job_id: str
    started_ns: int
    tags: dict[str, JSONScalar]
    events: list[PerformanceEvent] = field(default_factory=list)
    maxima: dict[str, int | float] = field(default_factory=dict)
    counters: dict[str, int | float] = field(default_factory=dict)
    outcome: str | None = None


def _validate_tag_value(key: str, value: JSONScalar) -> None:
    if value is not None and not isinstance(value, (str, int, float, bool)):
        raise ValueError(f"tag value for {key!r} must be a JSON scalar")


class PerformanceRecorder:
    """Collect local timing and resource observations when explicitly enabled."""

    def __init__(
        self,
        *,
        enabled: bool = False,
        clock_ns: Callable[[], int] = time.perf_counter_ns,
    ) -> None:
        self.enabled = bool(enabled)
        self._clock_ns = clock_ns
        self._lock = threading.RLock()
        self._traces: dict[str, _TraceState] = {}

    def begin(self, job_id: str, tags: Mapping[str, JSONScalar]) -> None:
        if not self.enabled:
            return
        if not isinstance(job_id, str) or not job_id.strip():
            raise ValueError("job_id must be a non-empty, non-blank string")

        unknown = set(tags) - SAFE_TAG_KEYS
        if unknown:
            raise ValueError(f"unsafe tag key(s): {sorted(unknown)}")
        for key, value in tags.items():
            _validate_tag_value(key, value)

        with self._lock:
            self._traces[job_id] = _TraceState(
                job_id=job_id,
                started_ns=self._clock_ns(),
                tags=dict(tags),
            )

    def mark(
        self,
        job_id: str | None,
        name: str,
        value: int | float | None = None,
    ) -> None:
        if not self.enabled or job_id is None:
            return
        with self._lock:
            trace = self._traces.get(job_id)
            if trace is None:
                return
            trace.events.append(
                PerformanceEvent(
                    name=name,
                    offset_ns=max(0, self._clock_ns() - trace.started_ns),
                    value=value,
                )
            )

    def observe_max(self, job_id: str | None, name: str, value: int | float) -> None:
        if not self.enabled or job_id is None:
            return
        with self._lock:
            trace = self._traces.get(job_id)
            if trace is None:
                return
            previous = trace.maxima.get(name)
            if previous is None or value > previous:
                trace.maxima[name] = value

    def increment(
        self,
        job_id: str | None,
        name: str,
        amount: int | float = 1,
    ) -> None:
        if not self.enabled or job_id is None:
            return
        with self._lock:
            trace = self._traces.get(job_id)
            if trace is None:
                return
            trace.counters[name] = trace.counters.get(name, 0) + amount

    def finish(self, job_id: str | None, outcome: str) -> None:
        if not self.enabled or job_id is None:
            return
        with self._lock:
            trace = self._traces.get(job_id)
            if trace is not None:
                trace.outcome = outcome

    def snapshot(self, job_id: str | None = None) -> list[dict[str, object]]:
        if not self.enabled:
            return []
        with self._lock:
            traces = (
                [self._traces[job_id]]
                if job_id is not None and job_id in self._traces
                else list(self._traces.values())
                if job_id is None
                else []
            )
            return [self._snapshot_trace(trace) for trace in traces]

    @staticmethod
    def _snapshot_trace(trace: _TraceState) -> dict[str, object]:
        return {
            "job_id": trace.job_id,
            "started_ns": trace.started_ns,
            "tags": dict(trace.tags),
            "events": [
                {
                    "name": event.name,
                    "offset_ns": event.offset_ns,
                    "value": event.value,
                }
                for event in trace.events
            ],
            "maxima": dict(trace.maxima),
            "counters": dict(trace.counters),
            "outcome": trace.outcome,
        }
