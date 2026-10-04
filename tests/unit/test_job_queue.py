"""FifoJobQueue: FIFO admission with targeted cancellation (Phase 2 Task 2)."""

import pytest

from vienetts_app.core.jobs import SynthesisJob
from vienetts_app.core.models import TTSRequest, WarmupOp
from vienetts_app.workers.job_queue import FifoJobQueue


def make_job(job_id: str, text: str = "hello", owner: str = "text") -> SynthesisJob:
    return SynthesisJob(
        id=job_id,
        owner=owner,  # type: ignore[arg-type]
        kind="interactive",
        priority=0,
        request=TTSRequest(text=text, job_id=job_id),
    )


def hex_id(n: int) -> str:
    return f"{n:032x}"


def test_queue_order_and_dedup() -> None:
    queue = FifoJobQueue()
    jobs = [make_job(hex_id(n), text=f"job {n}") for n in range(1, 4)]
    for job in jobs:
        queue.put(job)

    assert [queue.take(0) for _ in range(3)] == jobs
    assert queue.take(0) is None

    queue = FifoJobQueue()
    queue.put(make_job(hex_id(1)))
    with pytest.raises(ValueError, match="already queued"):
        queue.put(make_job(hex_id(1)))


def test_cancellation() -> None:
    queue = FifoJobQueue()
    first = make_job("a" * 32, text="first")
    second = make_job("b" * 32, text="second")
    queue.put(first)
    queue.put(second)

    assert queue.cancel(second.id) == second
    assert queue.take(0) == first
    assert queue.take(0) is None

    queue = FifoJobQueue()
    queue.put(make_job(hex_id(1)))
    assert queue.cancel(hex_id(9)) is None
    assert queue.take(0) is not None

    queue = FifoJobQueue()
    text = make_job(hex_id(1), owner="text")
    book_a = make_job(hex_id(2), owner="audiobook")
    cloning = make_job(hex_id(3), owner="cloning")
    book_b = make_job(hex_id(4), owner="audiobook")
    for job in (text, book_a, cloning, book_b):
        queue.put(job)

    assert queue.cancel_owner("audiobook") == (book_a, book_b)
    assert queue.take(0) == text
    assert queue.take(0) == cloning
    assert queue.take(0) is None

    queue = FifoJobQueue()
    first = make_job(hex_id(1))
    second = make_job(hex_id(2))
    queue.put(first)
    queue.put(WarmupOp())
    queue.put(second)

    assert queue.cancel_all() == (first, second)
    assert queue.take(0) is None


def test_warmup_and_pending() -> None:
    queue = FifoJobQueue()
    job = make_job(hex_id(1))
    warmup = WarmupOp()
    queue.put(warmup)
    queue.put(job)

    assert queue.take(0) == warmup
    assert queue.take(0) == job

    queue = FifoJobQueue()
    first = make_job(hex_id(1))
    second = make_job(hex_id(2), owner="paragraph")
    queue.put(first)
    queue.put(WarmupOp())
    queue.put(second)

    # Task 5.1's switch gate asks this: warmups are not work (they are silent
    # engine preparation the incoming profile redoes), and taking a job must
    # remove it from the answer.
    assert queue.pending_jobs() == (first, second)
    assert queue.take(0) == first
    assert queue.pending_jobs() == (second,)
    assert queue.cancel(second.id) == second
    assert queue.pending_jobs() == ()

    queue = FifoJobQueue()
    queue.put(make_job(hex_id(3)))
    queue.cancel_all()
    assert queue.pending_jobs() == ()
