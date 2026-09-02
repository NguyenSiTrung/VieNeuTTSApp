"""One-shot background ops: import/export/EPUB-open off the GUI thread.

Document imports (.docx/.pdf lazy-import + parse) and file exports (WAV
writes up to hundreds of MB) used to run inside QML slots — the window
froze for the whole parse/write with no busy state and no way to interact.
``submit_off_thread`` runs the callable on the global Qt thread pool and
marshals the result back to the GUI thread through a queued signal, where
controllers flip state/emit their own signals.

The runner is injectable (``bg_runner`` on the controllers): production
uses the thread pool; tests inject ``run_sync`` for deterministic,
inline-completing calls.
"""

from __future__ import annotations

import contextlib
from collections.abc import Callable
from typing import TypeVar

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

T = TypeVar("T")

_SHUTDOWN_DRAIN_MS = 5000


class _OneShotBridge(QObject):
    """Marshals one background result back to the bridge's home thread."""

    completed = Signal(object)

    def __init__(
        self,
        work: Callable[[], T],
        on_done: Callable[[T], None],
        parent: QObject | None,
    ) -> None:
        super().__init__(parent)
        self._work = work
        self._on_done = on_done
        self.completed.connect(self._deliver)  # queued from the pool thread

    def run_work(self) -> None:
        # A RuntimeError here means the bridge's parent was destroyed
        # mid-flight (app quitting) — the result is simply dropped.
        with contextlib.suppress(RuntimeError):
            self.completed.emit(self._work())

    def _deliver(self, result: object) -> None:
        self._on_done(result)  # type: ignore[arg-type]


def run_on_thread_pool(
    work: Callable[[], T], on_done: Callable[[T], None], parent: QObject
) -> None:
    """Production runner: global pool, result delivered on the GUI thread."""
    bridge = _OneShotBridge(work, on_done, parent)

    class _Job(QRunnable):
        def run(self) -> None:
            bridge.run_work()

    QThreadPool.globalInstance().start(_Job())


def run_sync(work: Callable[[], T], on_done: Callable[[T], None], parent: QObject) -> None:
    """Test runner: work + delivery inline on the calling thread."""
    bridge = _OneShotBridge(work, on_done, parent)
    bridge.run_work()


def drain_thread_pool(timeout_ms: int = _SHUTDOWN_DRAIN_MS) -> None:
    """Shutdown hook: let in-flight file writes finish (bounded)."""
    QThreadPool.globalInstance().waitForDone(timeout_ms)
