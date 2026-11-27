"""One-shot background ops: import/export/EPUB-open off the GUI thread.

Document imports (.docx/.pdf lazy-import + parse) and file exports (WAV
writes up to hundreds of MB) used to run inside QML slots — the window
froze for the whole parse/write with no busy state and no way to interact.
``run_on_thread_pool`` runs the callable on a partitioned Qt thread pool
(``interactive``, ``downloads``, or ``exports``) and marshals the result back
to the GUI thread through a queued signal, where controllers flip state/emit
their own signals.

The runner is injectable (``bg_runner`` on the controllers): production
uses the thread pool; tests inject ``run_sync`` for deterministic,
inline-completing calls.

Each op gets a private ``_OneShotBridge``. Once it has delivered success or
error it drops its ``work``/``on_done``/``on_error`` references (and so
everything their closures capture) and ``deleteLater()``s itself — a long
session must not keep one live bridge per finished op under its parent.

``ProgressRelay`` is the matching hop for *progress*: workers call
``report(generation, value, text)`` from any thread and the GUI-side
``on_progress(value, text)`` callback runs on the GUI thread, only while
``generation`` is still the op's current one, with values kept monotonic
within a generation. AI stage/batch progress and model/runtime download
progress route through it so no worker thread touches controller state.
"""

from __future__ import annotations

import contextlib
import logging
import math
import os
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

logger = logging.getLogger(__name__)

T = TypeVar("T")

_SHUTDOWN_DRAIN_MS = 5000

POOL_INTERACTIVE = "interactive"
POOL_DOWNLOADS = "downloads"
POOL_EXPORTS = "exports"
POOL_LANES: tuple[str, ...] = (POOL_INTERACTIVE, POOL_DOWNLOADS, POOL_EXPORTS)

_POOL_MAX_THREADS: dict[str, int] = {
    POOL_INTERACTIVE: max(4, min(8, os.cpu_count() or 4)),
    POOL_DOWNLOADS: 2,
    POOL_EXPORTS: 2,
}
_POOLS: dict[str, QThreadPool] = {}
_POOLS_LOCK = threading.Lock()


def _validate_pool_name(pool: str) -> str:
    if pool not in _POOL_MAX_THREADS:
        known = ", ".join(POOL_LANES)
        raise ValueError(f"Unknown thread pool lane {pool!r}; expected one of: {known}")
    return pool


def get_thread_pool(pool: str = POOL_INTERACTIVE) -> QThreadPool:
    """Return the dedicated lazily-created ``QThreadPool`` for ``pool``."""
    lane = _validate_pool_name(pool)
    cached = _POOLS.get(lane)
    if cached is not None:
        return cached
    with _POOLS_LOCK:
        cached = _POOLS.get(lane)
        if cached is None:
            instance = QThreadPool()
            instance.setMaxThreadCount(_POOL_MAX_THREADS[lane])
            _POOLS[lane] = instance
            cached = instance
        return cached


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
        self._work: Callable[[], T] | None = work
        self._on_done: Callable[[T], None] | None = on_done
        self._on_error = on_error
        self.completed.connect(self._deliver)  # queued from the pool thread

    def run_work(self) -> None:
        # One-shot: the closure (and what it captures) is released as soon
        # as it has run, not only after the GUI thread got the result.
        work, self._work = self._work, None
        # A RuntimeError here means the bridge's parent was destroyed
        # mid-flight (app quitting) — the result is simply dropped.
        try:
            result = work()  # type: ignore[misc]
        except Exception as exc:  # noqa: BLE001 - marshal, never drop
            with contextlib.suppress(RuntimeError):
                self.completed.emit((False, exc))
        else:
            with contextlib.suppress(RuntimeError):
                self.completed.emit((True, result))

    def _deliver(self, envelope: object) -> None:
        ok, payload = envelope  # type: ignore[misc]
        try:
            if ok:
                self._on_done(payload)  # type: ignore[misc, arg-type]
            elif self._on_error is not None:
                self._on_error(payload)  # type: ignore[arg-type]
            else:
                logger.exception("background operation failed", exc_info=payload)
        finally:
            # Also on a raising callback: drop every closure and free the
            # QObject (FR-1.8) — see the module docstring.
            self._work = self._on_done = self._on_error = None
            self.deleteLater()


class _BridgeJob(QRunnable):
    """Pool job for one bridge; lets go of the bridge as soon as it starts.

    Module-level on purpose: a per-call ``QRunnable`` subclass sits in a
    reference cycle and would pin its bridge until the cyclic GC runs.
    """

    def __init__(self, bridge: _OneShotBridge) -> None:
        super().__init__()
        self._bridge: _OneShotBridge | None = bridge

    def run(self) -> None:
        bridge, self._bridge = self._bridge, None
        if bridge is not None:
            bridge.run_work()


def run_on_thread_pool(
    work: Callable[[], T],
    on_done: Callable[[T], None],
    parent: QObject,
    *,
    on_error: Callable[[BaseException], None] | None = None,
    pool: str = POOL_INTERACTIVE,
) -> None:
    """Production runner: partitioned pool lane, result delivered on the GUI thread.

    ``pool`` selects the isolated lane (``POOL_INTERACTIVE`` by default,
    ``POOL_DOWNLOADS`` for multi-GB transfers, ``POOL_EXPORTS`` for mux/encode
    work) so long transfers and exports never starve fast UI operations.
    ``on_error`` (optional) receives a worker-raised exception on the GUI
    thread so callers can reset busy state and surface an error banner;
    without it the failure is logged and ``on_done`` is skipped.
    """
    target_pool = get_thread_pool(pool)
    bridge = _OneShotBridge(work, on_done, parent, on_error=on_error)
    target_pool.start(_BridgeJob(bridge))


def run_on_downloads_pool(
    work: Callable[[], T],
    on_done: Callable[[T], None],
    parent: QObject,
    *,
    on_error: Callable[[BaseException], None] | None = None,
) -> None:
    """Run a download/install job on the dedicated downloads pool."""
    run_on_thread_pool(work, on_done, parent, on_error=on_error, pool=POOL_DOWNLOADS)


def run_on_exports_pool(
    work: Callable[[], T],
    on_done: Callable[[T], None],
    parent: QObject,
    *,
    on_error: Callable[[BaseException], None] | None = None,
) -> None:
    """Run an audio/video export job on the dedicated exports pool."""
    run_on_thread_pool(work, on_done, parent, on_error=on_error, pool=POOL_EXPORTS)


def run_sync(
    work: Callable[[], T],
    on_done: Callable[[T], None],
    parent: QObject,
    *,
    on_error: Callable[[BaseException], None] | None = None,
    pool: str = POOL_INTERACTIVE,
) -> None:
    """Test runner: work + delivery inline on the calling thread.

    Accepts ``pool`` so it stays a drop-in test substitute for
    :func:`run_on_thread_pool`. The bridge still ``deleteLater()``s itself, so
    it is freed the next time the event loop (or
    ``sendPostedEvents(None, DeferredDelete)``) runs.
    """
    _validate_pool_name(pool)
    _OneShotBridge(work, on_done, parent, on_error=on_error).run_work()


class ProgressRelay(QObject):
    """Thread-safe, generation-checked progress hop onto the GUI thread.

    Construct it on the GUI thread with the op's generation getter and the
    GUI-side ``on_progress(value, text)`` callback; workers call
    :meth:`report` from any thread. Reports from another thread are queued
    to the relay's home thread; a report made on the home thread itself is
    delivered inline, so ``run_sync``-driven tests stay synchronous and
    ordered.

    * **Generation check at delivery.** A report whose generation is not
      ``current_generation()`` *when it is delivered* is dropped, so one
      queued before a cancel/finish/project switch never repaints a reset
      UI. Bump the op's generation on cancel and finish, or make the getter
      return a value no op uses (e.g. ``-1``) while idle.
    * **Monotonic within a generation.** A value lower than the last one
      delivered for the same generation is clamped up to it (its text is
      still shown, so a stage message is never lost and the bar never jumps
      back). A new generation starts a fresh high-water mark.
    """

    _reported = Signal(int, float, str)

    def __init__(
        self,
        current_generation: Callable[[], int],
        on_progress: Callable[[float, str], None],
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._current_generation = current_generation
        self._on_progress = on_progress
        self._high_water: tuple[int, float] | None = None  # (generation, value)
        self._reported.connect(self._deliver)  # queued when sent from a worker

    def report(self, generation: int, value: float, text: str = "") -> None:
        """Send one progress update for ``generation``; any thread."""
        with contextlib.suppress(RuntimeError):  # relay destroyed (app quitting)
            self._reported.emit(generation, value, text)

    def _deliver(self, generation: int, value: float, text: str) -> None:
        if generation != self._current_generation():
            return
        if self._high_water is not None and self._high_water[0] == generation:
            value = max(value, self._high_water[1])
        self._high_water = (generation, value)
        self._on_progress(value, text)


class _OrderedJob(QRunnable):
    """Single-step job for :class:`OrderedExecutor`.

    Module-level on purpose: defining a ``QRunnable`` subclass inside
    ``OrderedExecutor.submit()`` leaks Shiboken dynamic class metadata and
    pins closure references on every submitted job.
    """

    def __init__(
        self,
        relay: Any,
        work: Callable[[], Any],
        on_done: Callable[[Any], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        super().__init__()
        self._relay: Any = relay
        self._work: Callable[[], Any] | None = work
        self._on_done: Callable[[Any], None] | None = on_done
        self._on_error: Callable[[BaseException], None] | None = on_error

    def run(self) -> None:
        relay, self._relay = self._relay, None
        work, self._work = self._work, None
        on_done, self._on_done = self._on_done, None
        on_error, self._on_error = self._on_error, None
        if work is None or relay is None:
            return
        try:
            result = work()
        except Exception as exc:  # noqa: BLE001 - marshal, never drop
            with contextlib.suppress(RuntimeError):  # executor destroyed
                relay.emit((on_error, exc))
        else:
            with contextlib.suppress(RuntimeError):  # executor destroyed
                relay.emit((on_done, result))


class OrderedExecutor(QObject):
    """One background thread, strict FIFO; results delivered on the GUI thread.

    For pipelines whose steps must apply in submission order (a subtitle
    dub track written cue by cue). Unlike :func:`run_on_thread_pool` it
    owns a private single-thread pool and ONE relay signal, so long runs do
    not allocate a bridge QObject per job. Construct it on the GUI thread:
    ``on_done``/``on_error`` run on the thread the executor lives on.
    """

    _completed = Signal(object)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._pool = QThreadPool(self)
        self._pool.setMaxThreadCount(1)
        self._completed.connect(self._deliver)

    def submit(
        self,
        work: Callable[[], T],
        on_done: Callable[[T], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        self._pool.start(_OrderedJob(self._completed, work, on_done, on_error))

    def _deliver(self, envelope: object) -> None:
        callback, payload = envelope  # type: ignore[misc]
        callback(payload)

    def flush(self, timeout_ms: int = _SHUTDOWN_DRAIN_MS) -> None:
        """Shutdown hook: wait (bounded) for queued jobs to finish."""
        self._pool.waitForDone(timeout_ms)


class SyncOrderedExecutor:
    """Test executor: work and delivery inline on the calling thread."""

    def submit(
        self,
        work: Callable[[], T],
        on_done: Callable[[T], None],
        on_error: Callable[[BaseException], None],
    ) -> None:
        try:
            result = work()
        except Exception as exc:  # noqa: BLE001 - mirrors OrderedExecutor
            on_error(exc)
            return
        on_done(result)

    def flush(self, timeout_ms: int = _SHUTDOWN_DRAIN_MS) -> None:
        return None


def drain_thread_pool(timeout_ms: int = _SHUTDOWN_DRAIN_MS) -> bool:
    """Shutdown hook: let in-flight jobs finish across every pool lane (bounded).

    Drains all instantiated partitioned pools (``interactive``, ``downloads``,
    ``exports``) plus ``QThreadPool.globalInstance()`` within a single shared
    ``timeout_ms`` budget. Returns ``True`` iff every pool drained in time.
    """
    with _POOLS_LOCK:
        pools = list(_POOLS.values())
    global_pool = QThreadPool.globalInstance()
    if global_pool is not None and global_pool not in pools:
        pools.append(global_pool)
    if timeout_ms < 0:
        drained = True
        for pool in pools:
            with contextlib.suppress(RuntimeError):
                if not pool.waitForDone(-1):
                    drained = False
        return drained
    deadline = time.monotonic() + (timeout_ms / 1000.0)
    drained = True
    for pool in pools:
        remaining_s = deadline - time.monotonic()
        remaining_ms = math.ceil(remaining_s * 1000) if remaining_s > 0 else 0
        with contextlib.suppress(RuntimeError):
            if not pool.waitForDone(remaining_ms):
                drained = False
    return drained
