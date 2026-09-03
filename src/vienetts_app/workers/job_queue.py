"""Thread-safe FIFO admission queue with targeted cancellation.

Phase 2 Task 2: the worker admits ``SynthesisJob`` values (plus silent
``WarmupOp`` commands) through this queue instead of ``queue.Queue`` so a
queued job can be removed O(1) by ID without touching private state. Phase 4
replaces only the selection policy (FIFO ``take``) with stable priority
order; the cancellation API stays.
"""

from __future__ import annotations

import threading
import time
from collections import OrderedDict

from vienetts_app.core.jobs import JobOwner, SynthesisJob
from vienetts_app.core.models import WarmupOp

QueueItem = SynthesisJob | WarmupOp


class FifoJobQueue:
    """FIFO pending-job store keyed for exact cancellation.

    ``SynthesisJob`` entries are keyed by job ID; ``WarmupOp`` entries (which
    carry no ID and are never individually cancellable) take unique sequence
    keys so they preserve submission order with jobs. All methods are safe to
    call from any thread.
    """

    def __init__(self) -> None:
        self._items: OrderedDict[str, QueueItem] = OrderedDict()
        self._condition = threading.Condition()
        self._warmup_seq = 0

    def put(self, item: QueueItem) -> None:
        """Enqueue a job or warmup; duplicate job IDs raise ``ValueError``."""
        with self._condition:
            key = self._key_for(item)
            if key in self._items:
                raise ValueError(f"job already queued: {key}")
            self._items[key] = item
            self._condition.notify()

    def take(self, timeout_seconds: float) -> QueueItem | None:
        """Remove and return the oldest item, or ``None`` on timeout/wake."""
        deadline = time.monotonic() + max(0.0, timeout_seconds)
        with self._condition:
            while not self._items:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(timeout=remaining)
            _, item = self._items.popitem(last=False)
            return item

    def cancel(self, job_id: str) -> SynthesisJob | None:
        """Remove one queued job by ID; ``None`` when not queued."""
        with self._condition:
            item = self._items.pop(f"job:{job_id}", None)
            self._condition.notify_all()
            return item if isinstance(item, SynthesisJob) else None

    def cancel_owner(self, owner: JobOwner) -> tuple[SynthesisJob, ...]:
        """Remove every queued job of ``owner`` in FIFO order."""
        with self._condition:
            removed = tuple(
                item
                for item in self._items.values()
                if isinstance(item, SynthesisJob) and item.owner == owner
            )
            for job in removed:
                del self._items[f"job:{job.id}"]
            self._condition.notify_all()
            return removed

    def cancel_all(self) -> tuple[SynthesisJob, ...]:
        """Remove every queued job; warmups are dropped silently."""
        with self._condition:
            removed = tuple(item for item in self._items.values() if isinstance(item, SynthesisJob))
            self._items.clear()
            self._condition.notify_all()
            return removed

    def wake(self) -> None:
        """Unblock a thread waiting in :meth:`take` so it can observe stop."""
        with self._condition:
            self._condition.notify_all()

    def _key_for(self, item: QueueItem) -> str:
        if isinstance(item, SynthesisJob):
            return f"job:{item.id}"
        self._warmup_seq += 1
        return f"warmup:{self._warmup_seq}"
