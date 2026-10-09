"""Bounded, condition-protected PCM byte transport.

Live jobs never pace synthesis to playback: the producer ``publish``es every
chunk it has already written to its artifact, the transport takes what fits
(capped at ``MAX_PCM_BYTES``) and remembers the rest as a byte-range backlog.
The consumer side ``refill``s that backlog, in order, from the artifact via
the attached source — so a slow sink delays only the listener, never the
artifact. Nothing duration-sized is ever held in memory.
"""

from __future__ import annotations

import threading
from collections.abc import Callable

PCM_BYTES_PER_SECOND = 48_000 * 4
MAX_PCM_BYTES = PCM_BYTES_PER_SECOND * 2
PREBUFFER_BYTES = PCM_BYTES_PER_SECOND * 150 // 1000

#: float32 mono: refills move whole samples only.
FRAME_BYTES = 4

#: ``read(start, end)`` → the producer's published bytes ``[start, end)``.
BacklogSource = Callable[[int, int], bytes]


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
        # File-backed overflow. ``_feed_lock`` serializes every producer-side
        # step (publish / refill / graceful close) so the delivered cursor and
        # the buffer advance together; it is always taken before
        # ``_condition``, and the consumer's ``take`` needs only the latter.
        self._feed_lock = threading.Lock()
        self._source: BacklogSource | None = None
        self._produced = 0
        self._delivered = 0
        self._close_pending = False

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

    def free_bytes(self) -> int:
        with self._condition:
            return self._capacity - self._available()

    def pending_bytes(self) -> int:
        """Published bytes not yet in the buffer (they wait in the source)."""
        with self._feed_lock:
            return self._produced - self._delivered

    def attach_source(self, read: BacklogSource | None) -> None:
        """Where backlogged bytes are re-read from (swappable, e.g. on promotion)."""
        with self._feed_lock:
            self._source = read

    def publish(self, payload: memoryview) -> int:
        """Producer: account ``payload`` as produced; deliver it now only if it is next.

        Never waits. With a backlog pending the payload joins it (a newer
        chunk must never overtake older audio); otherwise the whole frames
        that fit go straight in. Returns the bytes delivered now.
        """
        with self._feed_lock:
            start = self._produced
            self._produced += len(payload)
            if self._delivered != start:
                with self._condition:
                    if self._closed:
                        raise TransportClosed("transport closed")
                return 0
            accepted = self._offer_frames(payload)
            self._delivered += accepted
            return accepted

    def refill(self) -> int:
        """Consumer side: move backlog from the source into free room, in order.

        A source that cannot serve right now (an artifact mid-promotion) is
        simply retried on the next call. Completes a pending graceful close
        once the backlog is delivered. Returns the bytes moved.
        """
        with self._feed_lock:
            moved = 0
            backlog = self._produced - self._delivered
            source = self._source
            if backlog > 0 and source is not None:
                room = self.free_bytes()
                count = min(room - room % FRAME_BYTES, backlog)
                if count > 0:
                    try:
                        data = source(self._delivered, self._delivered + count)
                    except Exception:  # noqa: BLE001 - retried next tick
                        data = b""
                    usable = len(data) - len(data) % FRAME_BYTES
                    if usable:
                        try:
                            moved = self._offer_frames(memoryview(data)[:usable])
                        except TransportClosed:
                            return 0
                        self._delivered += moved
            self._complete_pending_close()
            return moved

    def drop_backlog(self) -> None:
        """Forget undelivered bytes (live playback gave up); never reads them."""
        with self._feed_lock:
            self._delivered = self._produced
            self._complete_pending_close()

    def _complete_pending_close(self) -> None:
        # Caller holds _feed_lock.
        if self._close_pending and self._delivered >= self._produced:
            self._close_pending = False
            with self._condition:
                self._closed = True
                self._condition.notify_all()

    def _offer_frames(self, payload: memoryview) -> int:
        with self._condition:
            if self._closed:
                raise TransportClosed("transport closed")
            room = self._capacity - self._available()
            count = min(room - room % FRAME_BYTES, len(payload))
            if count <= 0:
                return 0
            self._buffer.extend(payload[:count])
            self._max_available = max(self._max_available, self._available())
            self._condition.notify_all()
            return count

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
        """End the stream. Graceful: buffered and backlogged bytes still play.

        With a backlog pending a graceful close is deferred until ``refill``
        has delivered it; a discarding close drops buffer and backlog now.
        """
        with self._feed_lock:
            if not discard and self._produced > self._delivered:
                self._close_pending = True
                return
            if discard:
                self._delivered = self._produced
                self._close_pending = False
                self._source = None
            with self._condition:
                self._closed = True
                self._discarded = discard
                if discard:
                    self._buffer.clear()
                    self._offset = 0
                self._condition.notify_all()
