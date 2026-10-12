"""Unit tests for crash diagnostic reporter and unhandled exception handler."""

import subprocess
from pathlib import Path
from unittest.mock import patch

from vienetts_app.crash import (
    format_crash_report,
    handle_unhandled_exception,
    show_native_error_dialog,
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


class TestShowNativeErrorDialog:
    def test_macos_shows_osascript_alert(self, monkeypatch) -> None:
        """Finder-launched .app stderr is invisible, so macOS gets a real
        osascript alert instead of the stderr fallback."""
        import vienetts_app.crash as crash_module

        calls: list[list[str]] = []

        def fake_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            calls.append(cmd)
            return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

        monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
        monkeypatch.setattr(crash_module.sys, "platform", "darwin")
        monkeypatch.setattr(subprocess, "run", fake_run)
        show_native_error_dialog('Lỗi "nặng"', r"body \ with 'quotes'")
        assert calls and calls[0][0] == "/usr/bin/osascript"
        script = calls[0][-1]
        assert 'display alert "Lỗi \\"nặng\\""' in script
        assert "body \\\\ with" in script  # backslash escaped for AppleScript

    def test_headless_qt_never_opens_a_native_dialog(self, monkeypatch, capsys) -> None:
        """Offscreen/minimal Qt has no screen to dismiss a modal dialog on, so
        a crash in a headless run (CI smoke drivers) must not block on one."""
        import vienetts_app.crash as crash_module

        def forbidden_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            raise AssertionError(f"native dialog opened: {cmd}")

        monkeypatch.setattr(subprocess, "run", forbidden_run)
        for qpa in ("offscreen", "minimal:enable_fonts", " Offscreen "):
            monkeypatch.setenv("QT_QPA_PLATFORM", qpa)
            for platform_name in ("darwin", "win32"):
                monkeypatch.setattr(crash_module.sys, "platform", platform_name)
                show_native_error_dialog("Error", "details")
                assert "[FATAL ERROR] Error" in capsys.readouterr().err, (qpa, platform_name)

    def test_macos_falls_back_to_stderr_when_alert_fails(self, monkeypatch, capsys) -> None:
        import vienetts_app.crash as crash_module

        def failing_run(cmd: list[str], **kwargs: object) -> subprocess.CompletedProcess:
            return subprocess.CompletedProcess(cmd, 1, stdout=b"", stderr=b"no display")

        monkeypatch.delenv("QT_QPA_PLATFORM", raising=False)
        monkeypatch.setattr(crash_module.sys, "platform", "darwin")
        monkeypatch.setattr(subprocess, "run", failing_run)
        show_native_error_dialog("Error", "details")
        assert "[FATAL ERROR] Error" in capsys.readouterr().err
