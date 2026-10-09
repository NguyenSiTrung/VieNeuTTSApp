from __future__ import annotations

import threading
import tracemalloc

import pytest

from vienetts_app.core.pcm_transport import (
    MAX_PCM_BYTES,
    PREBUFFER_BYTES,
    BoundedPcmTransport,
    TransportClosed,
)


def test_transport_never_exceeds_capacity_and_cancel_unblocks() -> None:
    transport = BoundedPcmTransport(capacity_bytes=8)
    transport.put(memoryview(b"12345678"))
    cancelled = threading.Event()
    started = threading.Event()

    def producer() -> None:
        started.set()
        with pytest.raises(TransportClosed):
            transport.put(memoryview(b"9"), cancelled=cancelled.is_set)

    thread = threading.Thread(target=producer)
    thread.start()
    assert started.wait(1)
    assert transport.available_bytes() == 8
    cancelled.set()
    transport.close(discard=True)
    thread.join(1)
    assert not thread.is_alive()
    assert transport.max_available_bytes <= 8


def test_take_wakes_blocked_producer_without_losing_order() -> None:
    transport = BoundedPcmTransport(capacity_bytes=4)
    transport.put(memoryview(b"abcd"))
    complete = threading.Event()

    def producer() -> None:
        transport.put(memoryview(b"ef"))
        complete.set()

    thread = threading.Thread(target=producer)
    thread.start()
    assert transport.take(2) == b"ab"
    assert complete.wait(1)
    assert transport.take(10) == b"cdef"
    thread.join(1)


def test_graceful_close_drains_then_raises() -> None:
    transport = BoundedPcmTransport(capacity_bytes=8)
    transport.put(memoryview(b"abc"))
    transport.close(discard=False)
    assert transport.take(8) == b"abc"
    with pytest.raises(TransportClosed):
        transport.take(1)


def test_prebuffer_threshold_is_exact() -> None:
    transport = BoundedPcmTransport()
    transport.put(memoryview(b"x" * (PREBUFFER_BYTES - 1)))
    assert not transport.ready_for_prebuffer()
    transport.put(memoryview(b"x"))
    assert transport.ready_for_prebuffer()


def test_invalid_capacity_rejected() -> None:
    with pytest.raises(ValueError):
        BoundedPcmTransport(capacity_bytes=0)
    with pytest.raises(ValueError):
        BoundedPcmTransport(capacity_bytes=-1)


def test_take_returns_bytes_with_a_single_copy() -> None:
    transport = BoundedPcmTransport()
    payload = bytes(range(256)) * (MAX_PCM_BYTES // 256)
    transport.put(memoryview(payload))
    count = MAX_PCM_BYTES // 2

    tracemalloc.start()
    try:
        data = transport.take(count)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert type(data) is bytes
    assert data == payload[:count]
    # A slice-then-bytes() take allocates the chunk twice; one copy stays well
    # under 1.5x even with tracemalloc's own bookkeeping.
    assert peak < count * 1.5, f"take allocated {peak} bytes for a {count}-byte chunk"


def test_take_compaction_keeps_order_across_many_partial_reads() -> None:
    transport = BoundedPcmTransport(capacity_bytes=1_000)
    payload = bytes(i % 251 for i in range(5_000))
    received = bytearray()
    sent = 0
    while sent < len(payload) or transport.available_bytes():
        if sent < len(payload) and transport.available_bytes() < 1_000:
            room = 1_000 - transport.available_bytes()
            transport.put(memoryview(payload[sent : sent + room]))
            sent += room
        received += transport.take(137)
        # Compaction keeps the backing buffer bounded by twice the capacity.
        assert len(transport._buffer) <= 2_000
        assert transport._offset <= len(transport._buffer)
    assert bytes(received) == payload


def test_offer_accepts_up_to_free_capacity_without_blocking() -> None:
    transport = BoundedPcmTransport(capacity_bytes=8)
    assert transport.offer(memoryview(b"12345")) == 5
    # Only 3 bytes fit: the call returns at once with the accepted count.
    assert transport.offer(memoryview(b"abcdef")) == 3
    assert transport.offer(memoryview(b"x")) == 0  # full: nothing, no wait
    assert transport.available_bytes() == 8
    assert transport.max_available_bytes == 8
    assert transport.take(8) == b"12345abc"
    assert transport.offer(memoryview(b"")) == 0
    assert transport.offer(memoryview(b"zz")) == 2
    assert transport.take(8) == b"zz"


def test_offer_refuses_after_close() -> None:
    transport = BoundedPcmTransport(capacity_bytes=4)
    transport.close(discard=False)
    with pytest.raises(TransportClosed):
        transport.offer(memoryview(b"c"))


class _Stream:
    """The producer's artifact stand-in: every published byte, re-readable."""

    def __init__(self, transport: BoundedPcmTransport) -> None:
        self.data = bytearray()
        self.reads: list[tuple[int, int]] = []
        self.transport = transport
        transport.attach_source(self.read)

    def read(self, start: int, end: int) -> bytes:
        self.reads.append((start, end))
        return bytes(self.data[start:end])

    def produce(self, payload: bytes) -> int:
        self.data.extend(payload)
        return self.transport.publish(memoryview(payload))


def test_publish_never_waits_and_backlog_refills_in_order() -> None:
    transport = BoundedPcmTransport(capacity_bytes=8)
    stream = _Stream(transport)
    assert stream.produce(b"AAAABBBB") == 8
    assert stream.produce(b"CCCC") == 0  # full: backlogged, the producer moves on
    assert transport.pending_bytes() == 4
    assert transport.take(6) == b"AAAABB"
    # Room exists, but a backlog does: a newer chunk never jumps the queue.
    assert stream.produce(b"DDDD") == 0
    assert transport.pending_bytes() == 8
    # Refills move whole frames only (6 free bytes → one 4-byte frame).
    assert transport.refill() == 4
    assert transport.take(8) == b"BBCCCC"
    assert transport.refill() == 4
    assert transport.pending_bytes() == 0
    assert transport.refill() == 0
    # Backlog drained: direct delivery resumes.
    assert stream.produce(b"EEEE") == 4
    assert transport.take(16) == b"DDDDEEEE"
    assert transport.max_available_bytes <= 8


def test_a_graceful_close_waits_for_the_backlog() -> None:
    transport = BoundedPcmTransport(capacity_bytes=4)
    stream = _Stream(transport)
    stream.produce(b"AAAABBBBCCCC")  # 4 delivered, 8 backlogged
    transport.close(discard=False)
    assert transport.take(4) == b"AAAA"
    assert transport.take(4) == b""  # not closed yet: the backlog is pending
    assert transport.refill() == 4
    assert transport.take(4) == b"BBBB"
    assert transport.refill() == 4
    assert transport.take(4) == b"CCCC"
    with pytest.raises(TransportClosed):
        transport.take(4)


def test_a_discarding_close_drops_the_backlog() -> None:
    transport = BoundedPcmTransport(capacity_bytes=4)
    stream = _Stream(transport)
    stream.produce(b"AAAABBBB")
    transport.close(discard=True)
    assert transport.pending_bytes() == 0
    assert transport.refill() == 0
    with pytest.raises(TransportClosed):
        stream.produce(b"CCCC")


def test_drop_backlog_skips_undelivered_audio_and_completes_a_pending_close() -> None:
    transport = BoundedPcmTransport(capacity_bytes=4)
    stream = _Stream(transport)
    stream.produce(b"AAAABBBBCCCC")
    transport.close(discard=False)
    transport.drop_backlog()
    assert transport.pending_bytes() == 0
    assert stream.reads == []
    assert transport.take(4) == b"AAAA"
    with pytest.raises(TransportClosed):
        transport.take(4)


def test_an_unavailable_source_is_retried_on_the_next_refill() -> None:
    transport = BoundedPcmTransport(capacity_bytes=4)
    data = b"AAAABBBB"
    broken = {"now": True}

    def read(start: int, end: int) -> bytes:
        if broken["now"]:
            raise OSError("artifact being promoted")
        return data[start:end]

    transport.attach_source(read)
    transport.publish(memoryview(data))
    transport.take(4)
    assert transport.refill() == 0
    broken["now"] = False
    assert transport.refill() == 4
    assert transport.take(4) == b"BBBB"
