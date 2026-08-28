"""Deterministic engine, audio sink, and event-loop probes for local benchmarks."""

from __future__ import annotations

import time
from dataclasses import dataclass

import numpy as np
from PySide6.QtCore import QObject, QTimer, Signal

from scripts.benchmarks.statistics import summarize

BYTES_PER_SECOND = 48_000 * 4


def bytes_per_tick(rate: float, interval_ms: int = 10) -> int:
    return max(1, round(BYTES_PER_SECOND * rate * interval_ms / 1000))


class DeterministicEngine:
    """A small engine-shaped fake whose timing and audio are reproducible."""

    sample_rate = 48_000
    backend = "onnx"

    def __init__(
        self,
        *,
        chunk_samples: int = 4_800,
        chunks_per_segment: int = 4,
        chunk_delay_ms: float = 5.0,
    ) -> None:
        self.chunk_samples = chunk_samples
        self.chunks_per_segment = chunks_per_segment
        self.chunk_delay_ms = chunk_delay_ms

    def infer_stream(self, text: str, voice: str | None = None, **kwargs):
        del text, voice, kwargs
        for index in range(self.chunks_per_segment):
            time.sleep(self.chunk_delay_ms / 1000)
            yield np.full(
                self.chunk_samples,
                ((index % 4) + 1) / 10,
                dtype=np.float32,
            )

    def infer(self, text: str, voice: str | None = None, **kwargs) -> np.ndarray:
        return np.concatenate(list(self.infer_stream(text, voice, **kwargs)))

    def infer_batch(self, texts, voice: str | None = None, **kwargs) -> list[np.ndarray]:
        return [self.infer(text, voice, **kwargs) for text in texts]

    def close(self) -> None:
        return None


class RateLimitedSink(QObject):
    """A QAudioSink-shaped fake that pulls from a QIODevice at a fixed rate."""

    stateChanged = Signal(object)

    def __init__(self, rate: float = 1.0, interval_ms: int = 10) -> None:
        super().__init__()
        if rate <= 0:
            raise ValueError("rate must be positive")
        self.rate = float(rate)
        self.interval_ms = interval_ms
        self.device = None
        self.consumed_bytes = 0
        self._state = "StoppedState"
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._pull)

    def start(self, device) -> None:
        self.device = device
        self._state = "ActiveState"
        self.stateChanged.emit(self._state)
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()
        self._state = "StoppedState"
        self.stateChanged.emit(self._state)

    def state(self) -> str:
        return self._state

    @property
    def is_drained(self) -> bool:
        return self.device is not None and len(self.device) == 0

    def _pull(self) -> None:
        if self.device is None or self._state != "ActiveState":
            return
        payload = self.device.readData(bytes_per_tick(self.rate, self.interval_ms))
        self.consumed_bytes += len(payload)


@dataclass
class EventLoopProbeResult:
    delays_ms: list[float]

    def to_dict(self) -> dict[str, object]:
        if not self.delays_ms:
            return {
                "supported": False,
                "sample_count": 0,
            }
        distribution = summarize(self.delays_ms)
        return {
            "supported": True,
            "sample_count": distribution.count,
            "median_delay_ms": distribution.median,
            "p95_delay_ms": distribution.p95,
            "maximum_delay_ms": distribution.maximum,
        }


class EventLoopProbe(QObject):
    """Record numeric Qt event-loop delay samples without user content."""

    def __init__(self, interval_ms: int = 10) -> None:
        super().__init__()
        if interval_ms <= 0:
            raise ValueError("interval_ms must be positive")
        self._interval_ns = interval_ms * 1_000_000
        self._last_ns: int | None = None
        self.delays_ms: list[float] = []
        self._timer = QTimer(self)
        self._timer.setInterval(interval_ms)
        self._timer.timeout.connect(self._tick)

    def start(self) -> None:
        self._timer.start()

    def stop(self) -> None:
        self._timer.stop()

    def result(self) -> EventLoopProbeResult:
        return EventLoopProbeResult(list(self.delays_ms))

    def _tick(self) -> None:
        now = time.perf_counter_ns()
        if self._last_ns is not None:
            elapsed = now - self._last_ns
            delay_ns = max(0, elapsed - self._interval_ns)
            self.delays_ms.append(delay_ns / 1_000_000)
        self._last_ns = now
