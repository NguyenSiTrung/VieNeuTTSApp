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
