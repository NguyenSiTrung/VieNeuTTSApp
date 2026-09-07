import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import patch

import pytest

pytest.importorskip("PySide6")


class TestNvidiaSmiProbeFlags:
    def test_win32_probe_hides_console_window(self, monkeypatch: pytest.MonkeyPatch) -> None:
        from vienetts_app.core import detector

        seen: dict[str, object] = {}

        class CompletedProcess:
            returncode = 1
            stdout = ""

        def fake_run(*args: object, **kwargs: object) -> CompletedProcess:
            seen.update(kwargs)
            return CompletedProcess()

        monkeypatch.setattr(sys, "platform", "win32")
        monkeypatch.setattr(detector.subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
        monkeypatch.setattr(detector.subprocess, "run", fake_run)
        detector.probe_cuda_driver()
        assert seen.get("creationflags") == 0x08000000

    def test_off_windows_probe_passes_no_creationflags(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from vienetts_app.core import detector

        seen: dict[str, object] = {}

        class CompletedProcess:
            returncode = 1
            stdout = ""

        def fake_run(*args: object, **kwargs: object) -> CompletedProcess:
            seen.update(kwargs)
            return CompletedProcess()

        monkeypatch.setattr(sys, "platform", "linux")
        monkeypatch.setattr(detector.subprocess, "run", fake_run)
        detector.probe_cuda_driver()
        assert "creationflags" not in seen


class TestQmlPreflight:
    def test_missing_main_qml_reports_actionable_reinstall(self, tmp_path: Path) -> None:
        code = textwrap.dedent(
            """
            import sys
            import vienetts_app.app as app_module

            app_module.MAIN_QML = app_module.MAIN_QML.with_name("DoesNotExist.qml")
            assert not app_module.MAIN_QML.is_file()
            try:
                app_module.create_app()
            except RuntimeError as exc:
                assert "reinstall" in str(exc).lower(), str(exc)
                assert str(app_module.MAIN_QML) in str(exc)
                sys.exit(0)
            sys.exit("create_app succeeded with a missing Main.qml")
            """
        )
        import os

        env = dict(os.environ, QT_QPA_PLATFORM="offscreen")
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=120, env=env
        )
        assert proc.returncode == 0, proc.stderr


class TestFrozenEntry:
    def test_main_calls_freeze_support(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import multiprocessing

        from vienetts_app.__main__ import main

        calls: list[str] = []
        monkeypatch.setattr(
            multiprocessing, "freeze_support", lambda: calls.append("freeze_support")
        )
        assert main(["--version"]) == 0
        assert calls == ["freeze_support"]


class TestSingleInstance:
    def test_second_lock_holder_is_refused(self, tmp_path: Path) -> None:
        from vienetts_app.app import acquire_single_instance_lock

        first = acquire_single_instance_lock(data_dir=tmp_path)
        assert first is not None
        assert acquire_single_instance_lock(data_dir=tmp_path) is None

    def test_run_gui_refuses_second_instance(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import vienetts_app.app as app_module

        monkeypatch.setattr(app_module, "acquire_single_instance_lock", lambda: None)
        with patch("vienetts_app.crash.show_native_error_dialog") as mock_dialog:
            assert app_module.run_gui() == 1
        assert mock_dialog.call_count == 1


class TestCrashHandlerInstall:
    def test_installs_unraisablehook_and_reinstalls_per_dir(self, tmp_path: Path) -> None:
        import threading

        import vienetts_app.crash as crash_module
        from vienetts_app.crash import install_crash_handler

        orig_sys, orig_thread, orig_unraisable = (
            sys.excepthook,
            threading.excepthook,
            sys.unraisablehook,
        )
        orig_installed, orig_dir = crash_module._INSTALLED, crash_module._ACTIVE_DATA_DIR
        try:
            first_dir = tmp_path / "first"
            second_dir = tmp_path / "second"
            install_crash_handler(data_dir=first_dir)
            assert first_dir == crash_module._ACTIVE_DATA_DIR
            assert sys.unraisablehook is not orig_unraisable
            # Reinstall with a new dir: hooks are re-pointed and the dir follows.
            install_crash_handler(data_dir=second_dir)
            assert second_dir == crash_module._ACTIVE_DATA_DIR
            assert sys.excepthook is not orig_sys
            assert threading.excepthook is not orig_thread
            assert sys.unraisablehook is not orig_unraisable
        finally:
            sys.excepthook, threading.excepthook, sys.unraisablehook = (
                orig_sys,
                orig_thread,
                orig_unraisable,
            )
            crash_module._INSTALLED = orig_installed
            crash_module._ACTIVE_DATA_DIR = orig_dir

    def test_broken_pipe_exits_quietly(self) -> None:
        from vienetts_app.crash import handle_unhandled_exception

        with (
            patch("vienetts_app.crash.write_crash_report") as mock_write,
            patch("vienetts_app.crash.show_native_error_dialog") as mock_dialog,
        ):
            handle_unhandled_exception(BrokenPipeError, BrokenPipeError(32, "Broken pipe"), None)
        assert mock_write.call_count == 0
        assert mock_dialog.call_count == 0


class TestLazyMigration:
    def test_migration_runs_once_per_target(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import vienetts_app.core.settings as settings_module
        from vienetts_app.core.settings import default_data_dir

        calls: list[Path] = []
        monkeypatch.setattr(settings_module, "_MIGRATED_TARGETS", set())
        monkeypatch.setattr(
            settings_module,
            "_migrate_legacy_data_dir",
            lambda target: calls.append(Path(target)),
        )
        default_data_dir()
        default_data_dir()
        assert len(calls) == 1


class TestMissingCatalog:
    def test_missing_qm_warns_and_falls_back_to_vietnamese(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
    ) -> None:
        import vienetts_app.ui.i18n as i18n_module
        from vienetts_app.ui.i18n import translator_for

        monkeypatch.setattr(i18n_module, "QM_PATH", tmp_path / "vienetts_en.qm")
        with caplog.at_level("WARNING", logger="vienetts_app.ui.i18n"):
            assert translator_for("en") is None
        assert any("vienetts_en.qm" in r.message for r in caplog.records)
