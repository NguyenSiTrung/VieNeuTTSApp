"""Unit tests for crash diagnostic reporter and unhandled exception handler."""

import os
import stat
import subprocess
import tempfile
import threading
from pathlib import Path
from unittest.mock import patch

import pytest

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

    def test_redacts_bearer_tokens_and_api_keys(self, tmp_path: Path) -> None:
        try:
            raise RuntimeError(
                "Auth failed: Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.secret "
                "llm_api_key='my-llm-secret' asr_api_key=\"my-asr-secret\" "
                "token=sk-proj-1234567890abcdef1234"
            )
        except RuntimeError as exc:
            report = format_crash_report(type(exc), exc, exc.__traceback__)
            log_path = write_crash_report(type(exc), exc, exc.__traceback__, data_dir=tmp_path)

        disk = log_path.read_text(encoding="utf-8")
        for text in (report, disk):
            assert "eyJhbGciOiJIUzI1NiJ9.secret" not in text
            assert "my-llm-secret" not in text
            assert "my-asr-secret" not in text
            assert "sk-proj-1234567890abcdef1234" not in text
            assert "[REDACTED]" in text


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

    @pytest.mark.skipif(os.name == "nt", reason="POSIX permissions and O_NOFOLLOW")
    def test_posix_permissions_symlink_protection_and_secure_temp_fallback(
        self, tmp_path: Path
    ) -> None:
        # Crash log is written with owner-only 0o600 permissions.
        previous_umask = os.umask(0)
        try:
            try:
                raise RuntimeError("Perm check")
            except RuntimeError as exc:
                log_path = write_crash_report(type(exc), exc, exc.__traceback__, data_dir=tmp_path)
        finally:
            os.umask(previous_umask)
        assert stat.S_IMODE(log_path.stat().st_mode) == 0o600

        # Pre-existing symlink target is never followed.
        symlink_dir = tmp_path / "symcase"
        symlink_dir.mkdir()
        victim = tmp_path / "victim.txt"
        victim.write_text("precious", encoding="utf-8")
        (symlink_dir / "crash.log").symlink_to(victim)
        try:
            raise RuntimeError("Symlink attack")
        except RuntimeError as exc:
            write_crash_report(type(exc), exc, exc.__traceback__, data_dir=symlink_dir)
        assert victim.read_text(encoding="utf-8") == "precious"

        # Unwritable data_dir falls back to a per-user 0o700 subdir, not shared /tmp/crash.log.
        blocked = tmp_path / "not-a-dir"
        blocked.write_text("file", encoding="utf-8")
        try:
            raise RuntimeError("Fallback check")
        except RuntimeError as exc:
            fallback_log = write_crash_report(type(exc), exc, exc.__traceback__, data_dir=blocked)
        assert fallback_log.parent != Path(tempfile.gettempdir())
        assert stat.S_IMODE(fallback_log.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(fallback_log.stat().st_mode) == 0o600

    def test_write_crash_report_is_thread_safe(self, tmp_path: Path) -> None:
        import vienetts_app.crash as crash_module

        assert isinstance(crash_module._WRITE_LOCK, type(threading.Lock()))
        errors: list[Exception] = []

        def worker(i: int) -> None:
            try:
                try:
                    raise RuntimeError(f"Concurrent crash {i}")
                except RuntimeError as exc:
                    write_crash_report(type(exc), exc, exc.__traceback__, data_dir=tmp_path)
            except Exception as exc:  # noqa: BLE001
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=10.0)
        assert errors == []
        content = (tmp_path / "crash.log").read_text(encoding="utf-8")
        assert content.count("VIENEUTTS CRASH REPORT") == 8
        for i in range(8):
            assert f"Concurrent crash {i}" in content


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
        title = '-e Lỗi "nặng"'
        body = 'body \\ with "quotes" & do shell script "id"\nline2'
        show_native_error_dialog(title, body)
        assert calls and calls[0][0] == "/usr/bin/osascript"
        cmd = calls[0]
        assert cmd[1] == "-e"
        script = cmd[2]
        assert "on run {t, m}" in script
        assert "display alert t message m" in script
        # Untrusted title/body are passed strictly via argv, never interpolated into script.
        assert title not in script
        assert "do shell script" not in script
        assert cmd[-3:] == ["--", title, body]

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
