"""bg_ops: one-shot pool runner marshals worker crashes instead of dropping them."""

import logging
import threading
import time
import weakref
from collections.abc import Callable

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QEvent, QObject, QThread, QThreadPool  # noqa: E402

from vienetts_app.ui.bg_ops import (  # noqa: E402
    ProgressRelay,
    _OneShotBridge,
    run_on_thread_pool,
    run_sync,
)


def wait_until(cond, timeout: float = 5.0, interval: float = 0.01) -> bool:
    app = QCoreApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        if app is not None:
            app.processEvents()
        time.sleep(interval)
    return False


@pytest.fixture
def parent(qcoreapp) -> QObject:
    return QObject()


def test_run_sync_delivers_raw_result_on_success(parent: QObject) -> None:
    seen: list[object] = []
    errors: list[BaseException] = []
    run_sync(lambda: ("path", ""), seen.append, parent, on_error=errors.append)
    assert seen == [("path", "")]
    assert errors == []


def test_run_sync_routes_exception_to_on_error_not_on_done(parent: QObject) -> None:
    boom = RuntimeError("disk gone")
    seen: list[object] = []
    errors: list[BaseException] = []

    def work() -> str:
        raise boom

    run_sync(work, seen.append, parent, on_error=errors.append)
    assert seen == []
    assert errors == [boom]


def test_run_sync_without_on_error_logs_instead_of_raising(
    parent: QObject, caplog: pytest.LogCaptureFixture
) -> None:
    seen: list[object] = []

    def work() -> str:
        raise OSError("locked")

    with caplog.at_level(logging.ERROR, logger="vienetts_app.ui.bg_ops"):
        run_sync(work, seen.append, parent)
    assert seen == []
    assert "background operation failed" in caplog.text


def test_pool_failure_surfaces_on_gui_thread(parent: QObject) -> None:
    boom = ValueError("parse blew up")
    seen: list[object] = []
    errors: list[BaseException] = []

    def work() -> str:
        raise boom

    run_on_thread_pool(work, seen.append, parent, on_error=errors.append)
    assert wait_until(lambda: len(errors) == 1, timeout=5.0, interval=0.001)
    assert errors == [boom]
    assert seen == []


def test_pool_success_still_delivers_raw_result(parent: QObject) -> None:
    seen: list[object] = []
    errors: list[BaseException] = []
    run_on_thread_pool(lambda: ("out.wav", ""), seen.append, parent, on_error=errors.append)
    assert wait_until(lambda: len(seen) == 1, timeout=5.0, interval=0.001)
    assert seen == [("out.wav", "")]
    assert errors == []


class _Captured:
    """Stands in for the heavy state (buffers, cues) a ``work`` closure captures."""

    def __init__(self, fail: bool) -> None:
        self.fail = fail


def _captured_work(n: int, refs: list[weakref.ref[_Captured]]) -> Callable[[], int]:
    # Built in a helper so only the closure (never the test frame) holds it.
    captured = _Captured(fail=n % 4 == 0)
    refs.append(weakref.ref(captured))

    def work() -> int:
        if captured.fail:
            raise ValueError(f"job {n} failed")
        return n

    return work


def test_bridges_release_work_and_delete_themselves_after_delivery(parent: QObject) -> None:
    """FR-1.8 / AC-4: 1,000 pool ops (success + error) leave ≤ 1 live bridge
    and no captured ``work`` state; ``run_sync`` bridges are released too."""
    refs: list[weakref.ref[_Captured]] = []
    done: list[int] = []
    errors: list[str] = []

    def on_error(exc: BaseException) -> None:
        errors.append(str(exc))  # never keep the exception: its traceback pins frames

    for n in range(1000):
        run_on_thread_pool(_captured_work(n, refs), done.append, parent, on_error=on_error)
    run_sync(_captured_work(1001, refs), done.append, parent, on_error=on_error)
    run_sync(_captured_work(1004, refs), done.append, parent, on_error=on_error)

    assert QThreadPool.globalInstance().waitForDone(10_000)
    QCoreApplication.sendPostedEvents()  # deliver every queued completion
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)  # run deleteLater()

    assert len(done) == 751 and len(errors) == 251
    assert len(parent.findChildren(_OneShotBridge)) <= 1
    alive = sum(ref() is not None for ref in refs)
    assert alive == 0, f"{alive} work closures outlived their delivery"


def test_progress_relay_delivers_on_gui_thread_by_generation_and_monotonic(qcoreapp) -> None:
    """FR-1.2 infrastructure: worker-thread reports hop to the GUI thread,
    stale generations (checked at delivery) drop, values never regress."""
    gui_thread = QThread.currentThread()
    current = [1]
    seen: list[tuple[float, str, bool]] = []
    relay = ProgressRelay(
        lambda: current[0],
        lambda value, text: seen.append((value, text, QThread.currentThread() is gui_thread)),
    )

    def from_worker(*reports: tuple[int, float, str]) -> None:
        thread = threading.Thread(target=lambda: [relay.report(*r) for r in reports])
        thread.start()
        thread.join()

    from_worker((0, 0.9, "old op"), (1, 0.2, "a"), (1, 0.6, "b"), (1, 0.4, "c"))
    assert seen == [], "a worker-thread report must not run the callback inline"
    QCoreApplication.sendPostedEvents()  # the worker joined: every report is queued
    assert seen == [(0.2, "a", True), (0.6, "b", True), (0.6, "c", True)]

    from_worker((1, 0.8, "late"))  # queued while gen 1 was current …
    current[0] = 2  # … then the op is cancelled before it is delivered
    from_worker((2, 0.1, "next op"))  # a new generation resets the high-water mark
    QCoreApplication.sendPostedEvents()
    assert seen[3:] == [(0.1, "next op", True)]

    relay.report(2, 0.05, "gui")  # from the home thread: delivered inline, clamped
    assert seen[4:] == [(0.1, "gui", True)]


def test_ordered_executor_runs_fifo_off_thread_and_delivers_on_gui_thread(qcoreapp) -> None:
    import threading

    from vienetts_app.ui.bg_ops import OrderedExecutor

    executor = OrderedExecutor()
    gate = threading.Event()
    ran: list[tuple[int, bool]] = []
    delivered: list[tuple[int, bool]] = []
    errors: list[str] = []

    def job(n: int):
        def work() -> int:
            if n == 0:
                gate.wait(2)  # later jobs queue behind a slow first one
            ran.append((n, threading.current_thread() is threading.main_thread()))
            if n == 3:
                raise ValueError("boom")
            return n

        return work

    for n in range(5):
        executor.submit(
            job(n),
            lambda value: delivered.append(
                (value, threading.current_thread() is threading.main_thread())
            ),
            lambda exc: errors.append(str(exc)),
        )
    gate.set()
    assert wait_until(lambda: len(delivered) + len(errors) == 5)
    assert [n for n, _ in ran] == [0, 1, 2, 3, 4]
    assert not any(on_main for _, on_main in ran), "work must run off the GUI thread"
    assert delivered == [(0, True), (1, True), (2, True), (4, True)]
    assert errors == ["boom"]
    executor.flush()


def test_sync_ordered_executor_runs_inline() -> None:
    from vienetts_app.ui.bg_ops import SyncOrderedExecutor

    executor = SyncOrderedExecutor()
    seen: list[object] = []
    executor.submit(lambda: 7, seen.append, seen.append)
    executor.submit(lambda: 1 / 0, seen.append, lambda exc: seen.append(type(exc)))
    assert seen == [7, ZeroDivisionError]
    executor.flush()
