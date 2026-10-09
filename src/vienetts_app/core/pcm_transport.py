"""Bounded, condition-protected PCM byte transport."""

from __future__ import annotations

import threading
from collections.abc import Callable

PCM_BYTES_PER_SECOND = 48_000 * 4
MAX_PCM_BYTES = PCM_BYTES_PER_SECOND * 2
PREBUFFER_BYTES = PCM_BYTES_PER_SECOND * 150 // 1000


class TransportClosed(RuntimeError):
    """The transport was closed or its producer was cancelled."""


class BoundedPcmTransport:
    def __init__(self, capacity_bytes: int = MAX_PCM_BYTES) -> None:
        if capacity_bytes <= 0:
            raise ValueError("capacity_bytes must be positive")
        self._capacity = capacity_bytes
        self._buffer = bytearray()
        self._offset = 0
        self._closed = False
        self._discarded = False
        self._max_available = 0
        self._condition = threading.Condition(threading.Lock())

    def _available(self) -> int:
        return len(self._buffer) - self._offset

    @property
    def max_available_bytes(self) -> int:
        with self._condition:
            return self._max_available

    def available_bytes(self) -> int:
        with self._condition:
            return self._available()

    def ready_for_prebuffer(self, minimum_bytes: int = PREBUFFER_BYTES) -> bool:
        return self.available_bytes() >= minimum_bytes

    def put(self, payload: memoryview, *, cancelled: Callable[[], bool] = lambda: False) -> None:
        remaining = memoryview(payload)
        while remaining:
            with self._condition:
                while not self._closed and self._available() >= self._capacity:
                    if cancelled():
                        raise TransportClosed("producer cancelled")
                    self._condition.wait(timeout=0.05)
                if cancelled():
                    raise TransportClosed("producer cancelled")
                if self._closed:
                    raise TransportClosed("transport closed")
                room = self._capacity - self._available()
                count = min(room, len(remaining))
                self._buffer.extend(remaining[:count])
                remaining = remaining[count:]
                self._max_available = max(self._max_available, self._available())
                self._condition.notify_all()

    def offer(self, payload: memoryview) -> int:
        """Accept what fits right now and return the byte count — never waits.

        A producer that must not be paced by the consumer (the worker writes
        the artifact first and only tops live playback up) offers instead of
        ``put``-ting; whatever is refused is delivered later from the file.
        """
        with self._condition:
            if self._closed:
                raise TransportClosed("transport closed")
            count = min(self._capacity - self._available(), len(payload))
            if count <= 0:
                return 0
            self._buffer.extend(payload[:count])
            self._max_available = max(self._max_available, self._available())
            self._condition.notify_all()
            return count

    def take(self, maximum_bytes: int) -> bytes:
        with self._condition:
            count = min(max(0, maximum_bytes), self._available())
            if count:
                start = self._offset
                # One copy: slicing a bytearray copies, and bytes() would copy
                # that again. The view must be released before compaction
                # below resizes the buffer (a live export raises BufferError).
                with memoryview(self._buffer) as view:
                    data = view[start : start + count].tobytes()
                self._offset += count
                if self._offset > len(self._buffer) // 2:
                    del self._buffer[: self._offset]
                    self._offset = 0
                self._condition.notify_all()
                return data
            if self._closed:
                raise TransportClosed("transport closed")
            return b""

    def close(self, *, discard: bool) -> None:
        with self._condition:
            self._closed = True
            self._discarded = discard
            if discard:
                self._buffer.clear()
                self._offset = 0
            self._condition.notify_all()
