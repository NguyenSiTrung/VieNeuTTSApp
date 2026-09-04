"""Global crash diagnostic reporter and unhandled exception handler.

Prevents silent crashes on Windows (where console=False windowed executables
redirect stdio to devnull) by recording diagnostic crash logs and displaying
an actionable native error dialog on fatal unhandled exceptions.
"""

from __future__ import annotations

import contextlib
import ctypes
import datetime
import logging
import os
import platform
import sys
import threading
import traceback
from pathlib import Path
from types import TracebackType
from typing import Any

from vienetts_app import __version__
from vienetts_app.core.settings import default_data_dir

logger = logging.getLogger(__name__)

_INSTALLED = False
_CRASH_LOG_NAME = "crash.log"
_MAX_LOG_SIZE_BYTES = 2 * 1024 * 1024  # 2 MB max


def _get_crash_log_path(data_dir: Path | None = None) -> Path:
    base = default_data_dir() if data_dir is None else Path(data_dir)
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError:
        # Fallback to temp dir if data dir cannot be created
        import tempfile

        base = Path(tempfile.gettempdir())
    return base / _CRASH_LOG_NAME


def format_crash_report(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: TracebackType | None,
    thread_name: str | None = None,
) -> str:
    """Format an actionable diagnostic crash report."""
    now = datetime.datetime.now(datetime.timezone.utc).isoformat()
    tb_lines = "".join(traceback.format_exception(exc_type, exc_value, exc_traceback))

    # Qt version if available
    qt_ver = "unknown"
    try:
        from PySide6 import __version__ as pyside_version
        from PySide6.QtCore import qVersion

        qt_ver = f"PySide6 {pyside_version} (Qt {qVersion()})"
    except Exception:  # noqa: BLE001
        pass

    thread_info = thread_name or threading.current_thread().name

    return (
        f"================================================================================\n"
        f"VIENEUTTS CRASH REPORT — {now}\n"
        f"================================================================================\n"
        f"App Version:       {__version__}\n"
        f"Platform:          {platform.platform()} ({sys.platform})\n"
        f"Python Version:    {sys.version.split()[0]} ({platform.architecture()[0]})\n"
        f"Qt Runtime:        {qt_ver}\n"
        f"Thread:            {thread_info} (id: {threading.get_ident()})\n"
        f"Exception Type:    {exc_type.__module__}.{exc_type.__qualname__}\n"
        f"Exception Message: {exc_value}\n"
        f"--------------------------------------------------------------------------------\n"
        f"Traceback:\n{tb_lines}\n"
        f"================================================================================\n\n"
    )


def write_crash_report(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: TracebackType | None,
    data_dir: Path | None = None,
    thread_name: str | None = None,
) -> Path:
    """Write diagnostic crash report to disk; returns the path to the crash log."""
    log_path = _get_crash_log_path(data_dir)
    report = format_crash_report(exc_type, exc_value, exc_traceback, thread_name=thread_name)
    try:
        # Rotate if log exceeds max size
        if log_path.is_file() and log_path.stat().st_size > _MAX_LOG_SIZE_BYTES:
            backup = log_path.with_name(f"{_CRASH_LOG_NAME}.old")
            with contextlib.suppress(OSError):
                backup.unlink(missing_ok=True)
            log_path.replace(backup)

        with open(log_path, "a", encoding="utf-8", errors="replace") as f:
            f.write(report)
            f.flush()
            os.fsync(f.fileno())
    except Exception as exc:  # noqa: BLE001
        logger.error("Could not write crash log to %s: %s", log_path, exc)
    return log_path


def show_native_error_dialog(title: str, message: str) -> None:
    """Display an emergency native OS message box when GUI is unavailable/crashing."""
    if sys.platform == "win32":
        try:
            MB_ICONERROR = 0x00000010
            MB_OK = 0x00000000
            ctypes.windll.user32.MessageBoxW(0, str(message), str(title), MB_ICONERROR | MB_OK)
            return
        except Exception:  # noqa: BLE001
            pass

    # Off-Windows fallback or if MessageBox failed: print to stderr if writable
    try:
        if sys.stderr and hasattr(sys.stderr, "write") and not sys.stderr.closed:
            sys.stderr.write(f"\n[FATAL ERROR] {title}\n{message}\n")
            sys.stderr.flush()
    except Exception:  # noqa: BLE001
        pass


def handle_unhandled_exception(
    exc_type: type[BaseException],
    exc_value: BaseException,
    exc_traceback: TracebackType | None,
    data_dir: Path | None = None,
    thread_name: str | None = None,
) -> None:
    """Global handler for unhandled exceptions (sys.excepthook and threading.excepthook)."""
    # Clean exit on KeyboardInterrupt / SystemExit
    if issubclass(exc_type, (KeyboardInterrupt, SystemExit)):
        sys.__excepthook__(exc_type, exc_value, exc_traceback)
        return

    logger.critical(
        "Unhandled exception in thread %s: %s",
        thread_name or threading.current_thread().name,
        exc_value,
        exc_info=(exc_type, exc_value, exc_traceback),
    )

    log_path = write_crash_report(
        exc_type, exc_value, exc_traceback, data_dir=data_dir, thread_name=thread_name
    )

    # Show actionable user dialog on main thread or fatal crash
    short_msg = str(exc_value).strip() or exc_type.__name__
    dialog_title = "VieNeuTTS — Lỗi không mong muốn (Unexpected Error)"
    dialog_body = (
        f"Ứng dụng gặp lỗi nghiêm trọng và cần đóng:\n\n"
        f"Chi tiết: {short_msg}\n\n"
        f"Nhật ký lỗi (crash log) đã được lưu tại:\n"
        f"{log_path}\n\n"
        f"Vui lòng gửi tệp này cho nhà phát triển để được hỗ trợ."
    )
    show_native_error_dialog(dialog_title, dialog_body)


def install_crash_handler(data_dir: Path | None = None) -> None:
    """Install global sys.excepthook and threading.excepthook crash reporters."""
    global _INSTALLED  # noqa: PLW0603
    if _INSTALLED:
        return
    _INSTALLED = True

    def _sys_hook(
        exc_type: type[BaseException],
        exc_value: BaseException,
        exc_traceback: TracebackType | None,
    ) -> None:
        handle_unhandled_exception(
            exc_type, exc_value, exc_traceback, data_dir=data_dir, thread_name="MainThread"
        )

    def _thread_hook(args: Any) -> None:
        handle_unhandled_exception(
            args.exc_type,
            args.exc_value,
            args.exc_traceback,
            data_dir=data_dir,
            thread_name=getattr(args.thread, "name", None),
        )

    sys.excepthook = _sys_hook
    threading.excepthook = _thread_hook
    logger.debug("Crash handler installed")
