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
import logging
from collections.abc import Callable
from typing import TypeVar

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

logger = logging.getLogger(__name__)

T = TypeVar("T")

_SHUTDOWN_DRAIN_MS = 5000


class _OneShotBridge(QObject):
    """Marshals one background result back to the bridge's home thread.

    ``completed`` always carries an outcome envelope ``(ok, payload)``: worker
    crashes must surface as an error delivery, never as a dropped signal —
    a dropped signal leaves the controller's busy flag (importing/exporting/
    update-checking/…) stuck True with no error banner (Windows crash audit
    2026-09-07). Success unwraps to ``on_done(result)`` exactly as before;
    failure routes to the optional ``on_error`` hook, else is logged.
    """

    completed = Signal(object)

    def __init__(
        self,
        work: Callable[[], T],
        on_done: Callable[[T], None],
        parent: QObject | None,
        *,
        on_error: Callable[[BaseException], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._work = work
        self._on_done = on_done
        self._on_error = on_error
        self.completed.connect(self._deliver)  # queued from the pool thread

    def run_work(self) -> None:
        # A RuntimeError here means the bridge's parent was destroyed
        # mid-flight (app quitting) — the result is simply dropped.
        try:
            result = self._work()
        except Exception as exc:  # noqa: BLE001 - marshal, never drop
            with contextlib.suppress(RuntimeError):
                self.completed.emit((False, exc))
        else:
            with contextlib.suppress(RuntimeError):
                self.completed.emit((True, result))

    def _deliver(self, envelope: object) -> None:
        ok, payload = envelope  # type: ignore[misc]
        if ok:
            self._on_done(payload)  # type: ignore[arg-type]
        elif self._on_error is not None:
            self._on_error(payload)  # type: ignore[arg-type]
        else:
            logger.exception("background operation failed", exc_info=payload)


def run_on_thread_pool(
    work: Callable[[], T],
    on_done: Callable[[T], None],
    parent: QObject,
    *,
    on_error: Callable[[BaseException], None] | None = None,
) -> None:
    """Production runner: global pool, result delivered on the GUI thread.

    ``on_error`` (optional) receives a worker-raised exception on the GUI
    thread so callers can reset busy state and surface an error banner;
    without it the failure is logged and ``on_done`` is skipped.
    """
    bridge = _OneShotBridge(work, on_done, parent, on_error=on_error)

    class _Job(QRunnable):
        def run(self) -> None:
            bridge.run_work()

    QThreadPool.globalInstance().start(_Job())


def run_sync(
    work: Callable[[], T],
    on_done: Callable[[T], None],
    parent: QObject,
    *,
    on_error: Callable[[BaseException], None] | None = None,
) -> None:
    """Test runner: work + delivery inline on the calling thread."""
    bridge = _OneShotBridge(work, on_done, parent, on_error=on_error)
    bridge.run_work()


def drain_thread_pool(timeout_ms: int = _SHUTDOWN_DRAIN_MS) -> None:
    """Shutdown hook: let in-flight file writes finish (bounded)."""
    QThreadPool.globalInstance().waitForDone(timeout_ms)
