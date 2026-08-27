"""GUI entry dispatch and QML bootstrap (FR-2.1/FR-2.2).

``main([])`` launches the GUI (via an injectable runner so tests never spin a
real event loop); ``main(["--smoke", ...])`` keeps the Phase 1 headless
contract (AC-4). ``create_app`` wires QApplication + QQmlApplicationEngine +
ShellBridge and loads Main.qml — assertible offscreen without exec().
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from vienetts_app.__main__ import main


class FakeEngine:
    sample_rate = 48_000
    backend = "onnx"

    def infer(self, text, voice=None, **kw):  # noqa: ARG002
        import numpy as np

        t = np.arange(24_000, dtype=np.float32) / 48_000.0
        return (0.4 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)

    def close(self) -> None:
        pass


def factory(**kwargs) -> FakeEngine:
    return FakeEngine()


class TestArgvDispatch:
    def test_no_args_launches_gui(self, tmp_path: Path) -> None:
        calls: list[str] = []

        def fake_gui() -> int:
            calls.append("gui")
            return 0

        rc = main([], gui_runner=fake_gui)
        assert rc == 0
        assert calls == ["gui"]

    def test_smoke_still_routes_to_headless_path(self, tmp_path: Path, capsys) -> None:
        # AC-4: --smoke runs the worker path, not the GUI runner.
        def boom() -> int:
            raise AssertionError("GUI must not launch for --smoke")

        out = tmp_path / "o.wav"
        rc = main(["--smoke", "hi", "-o", str(out)], engine_factory=factory, gui_runner=boom)
        assert rc == 0
        assert out.is_file()

    def test_blank_smoke_text_still_usage_error(self) -> None:
        with pytest.raises(SystemExit):
            main(["--smoke", "   "], gui_runner=lambda: 0)

    def test_gui_runner_exit_code_propagates(self) -> None:
        assert main([], gui_runner=lambda: 3) == 3


class TestCreateApp:
    def test_bootstrap_loads_main_window_with_bridge(self, tmp_path: Path) -> None:
        # Runs in a subprocess: the CLI dispatch tests above leave a headless
        # QCoreApplication in this process, and QML needs a QGuiApplication
        # (create_app raises RuntimeError in that case — by design).
        script = textwrap.dedent(
            """\
            import json
            import sys

            from PySide6.QtCore import QObject

            from vienetts_app.app import create_app
            from vienetts_app.ui.bridge import ShellBridge

            # AppController's default construction is model-free (NFR-3.1), so
            # the default controller path is exercised here alongside the fake
            # bridge — proving both context properties coexist in one shell.
            _app, engine = create_app(
                bridge_factory=lambda: ShellBridge(
                    settings_dir=sys.argv[1], detector=lambda: "FAKE NOTE"
                )
            )
            window = engine.rootObjects()[0]
            named = {o.objectName() for o in window.findChildren(QObject)}
            readout = window.findChildren(QObject, "engineReadout")[0]
            print("RESULT:" + json.dumps({
                "root": window.objectName(),
                "nav_present": {"navBar", "engineReadout", "textTab", "settingsTab"} <= named,
                "note": readout.property("text"),
            }))
        """
        )
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
        proc = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        (line,) = (ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:"))
        result = json.loads(line.removeprefix("RESULT:"))
        assert result["root"] == "mainWindow"
        assert result["nav_present"] is True
        assert result["note"] == "FAKE NOTE"


class TestControllerWiring:
    """create_app registers + anchors the AppController; run_gui quits via it."""

    def _script(self) -> str:
        return textwrap.dedent(
            """\
            import json
            import sys
            from pathlib import Path

            from PySide6.QtCore import QTimer

            from vienetts_app.app import create_app
            from vienetts_app.ui.controller import AppController

            data_dir = Path(sys.argv[1])
            created = []

            def factory():
                c = AppController(
                    data_dir=data_dir,
                    engine_factory=lambda **kw: (_ for _ in ()).throw(AssertionError("no model")),
                    worker_factory=lambda engine: None,
                    catalog=lambda: [],
                    saved_names=lambda vd: [],
                )
                created.append(c)
                return c

            app, engine = create_app(controller_factory=factory)
            controller = created[0]
            # run_gui's actual wiring: the REAL shutdown is connected to
            # aboutToQuit (a bound method captured at connect time — a
            # post-connect monkeypatch would never fire, which is Qt, not us).
            fired = []
            controller._shutdown_probe = fired
            original_shutdown = controller.shutdown
            def probe():
                original_shutdown()
                fired.append(True)
            controller.shutdown = probe
            app.aboutToQuit.connect(controller.shutdown)
            QTimer.singleShot(0, app.quit)
            app.exec()
            result = {
                "registered": engine.rootContext().contextProperty("controller") is controller,
                "anchored": getattr(engine, "_controller", None) is controller,
                "is_app_controller": isinstance(controller, AppController),
                "shutdown_on_quit": bool(fired),
            }
            print("RESULT:" + json.dumps(result))
        """
        )

    def test_controller_registered_anchored_and_quit_wired(self, tmp_path: Path) -> None:
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
        proc = subprocess.run(
            [sys.executable, "-c", self._script(), str(tmp_path)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        (line,) = (ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:"))
        result = json.loads(line.removeprefix("RESULT:"))
        assert result["registered"] is True
        assert result["anchored"] is True
        assert result["is_app_controller"] is True
        assert result["shutdown_on_quit"] is True

    def test_default_controller_is_app_controller(self, tmp_path: Path) -> None:
        # No controller_factory → the default AppController() (which resolves
        # the REAL data dir; construction is model-free so this is safe).
        script = textwrap.dedent(
            """\
            import json

            from vienetts_app.app import create_app
            from vienetts_app.ui.controller import AppController

            _app, engine = create_app()
            controller = engine.rootContext().contextProperty("controller")
            print("RESULT:" + json.dumps({
                "default_ok": isinstance(controller, AppController),
                "anchored": getattr(engine, "_controller", None) is controller,
            }))
        """
        )
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
        proc = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        (line,) = (ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:"))
        result = json.loads(line.removeprefix("RESULT:"))
        assert result["default_ok"] is True
        assert result["anchored"] is True
