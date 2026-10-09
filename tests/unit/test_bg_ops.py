"""bg_ops: one-shot pool runner marshals worker crashes instead of dropping them."""

import logging
import time

import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QObject  # noqa: E402

from vienetts_app.ui.bg_ops import run_on_thread_pool, run_sync  # noqa: E402


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
