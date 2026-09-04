"""Unit tests for crash diagnostic reporter and unhandled exception handler."""

import sys
from pathlib import Path
from unittest.mock import patch

from vienetts_app.crash import (
    format_crash_report,
    handle_unhandled_exception,
    install_crash_handler,
    write_crash_report,
)


class TestFormatCrashReport:
    def test_contains_all_diagnostic_sections(self) -> None:
        try:
            raise ValueError("Test crash message")
        except ValueError as exc:
            report = format_crash_report(
                type(exc), exc, exc.__traceback__, thread_name="WorkerThread"
            )

        assert "VIENEUTTS CRASH REPORT" in report
        assert "App Version:" in report
        assert "Platform:" in report
        assert "Python Version:" in report
        assert "Thread:            WorkerThread" in report
        assert "Exception Type:    builtins.ValueError" in report
        assert "Exception Message: Test crash message" in report
        assert "Traceback:" in report
        assert "raise ValueError" in report


class TestWriteCrashReport:
    def test_writes_to_data_dir(self, tmp_path: Path) -> None:
        try:
            raise RuntimeError("Disk write failure")
        except RuntimeError as exc:
            log_path = write_crash_report(type(exc), exc, exc.__traceback__, data_dir=tmp_path)

        assert log_path.is_file()
        content = log_path.read_text(encoding="utf-8")
        assert "RuntimeError" in content
        assert "Disk write failure" in content

    def test_rotates_oversized_log(self, tmp_path: Path) -> None:
        log_file = tmp_path / "crash.log"
        # Seed with > 2 MB of dummy data
        log_file.write_bytes(b"X" * (2 * 1024 * 1024 + 100))

        try:
            raise RuntimeError("New crash")
        except RuntimeError as exc:
            write_crash_report(type(exc), exc, exc.__traceback__, data_dir=tmp_path)

        # Old log rotated to crash.log.old
        backup = tmp_path / "crash.log.old"
        assert backup.is_file()
        assert log_file.is_file()
        content = log_file.read_text(encoding="utf-8")
        assert "New crash" in content


class TestHandleUnhandledException:
    def test_system_exit_and_keyboard_interrupt_pass_through(self) -> None:
        with (
            patch("vienetts_app.crash.write_crash_report") as mock_write,
            patch("sys.__excepthook__") as mock_sys_hook,
        ):
            handle_unhandled_exception(SystemExit, SystemExit(0), None)
            handle_unhandled_exception(KeyboardInterrupt, KeyboardInterrupt(), None)

        assert mock_write.call_count == 0
        assert mock_sys_hook.call_count == 2

    def test_fatal_exception_writes_report_and_shows_dialog(self, tmp_path: Path) -> None:
        with patch("vienetts_app.crash.show_native_error_dialog") as mock_dialog:
            try:
                raise ValueError("Fatal failure")
            except ValueError as exc:
                handle_unhandled_exception(type(exc), exc, exc.__traceback__, data_dir=tmp_path)

        assert mock_dialog.call_count == 1
        log_file = tmp_path / "crash.log"
        assert log_file.is_file()
        assert "Fatal failure" in log_file.read_text(encoding="utf-8")


class TestInstallCrashHandler:
    def test_hooks_sys_and_threading(self) -> None:
        orig_sys = sys.excepthook
        try:
            install_crash_handler()
            # Calling again is idempotent
            install_crash_handler()
            assert sys.excepthook != orig_sys
        finally:
            sys.excepthook = orig_sys
