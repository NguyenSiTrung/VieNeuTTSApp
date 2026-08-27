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
