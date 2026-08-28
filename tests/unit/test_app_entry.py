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

    def test_controller_wiring_and_shutdown(self, tmp_path: Path) -> None:
        script = textwrap.dedent(
            """\
            import json
            import sys
            from pathlib import Path

            from PySide6.QtCore import QTimer

            from vienetts_app.app import create_app
            from vienetts_app.ui.controller import AppController

            data_dir = Path(sys.argv[1])

            # 1. Default controller
            _app0, engine0 = create_app()
            ctrl0 = engine0.rootContext().contextProperty("controller")
            default_ok = isinstance(ctrl0, AppController)
            default_anchored = getattr(engine0, "_controller", None) is ctrl0

            # 2. Injected controller with shutdown wiring
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
                "default_ok": default_ok,
                "default_anchored": default_anchored,
                "registered": engine.rootContext().contextProperty("controller") is controller,
                "anchored": getattr(engine, "_controller", None) is controller,
                "is_app_controller": isinstance(controller, AppController),
                "shutdown_on_quit": bool(fired),
            }
            print("RESULT:" + json.dumps(result))
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
        assert result["default_anchored"] is True
        assert result["registered"] is True
        assert result["anchored"] is True
        assert result["is_app_controller"] is True
        assert result["shutdown_on_quit"] is True

class TestLanguageBootstrap:
    """create_app installs the UI-language translator BEFORE QML loads."""

    def _script(self) -> str:
        return textwrap.dedent(
            """\
            import json
            import sys
            from pathlib import Path

            from PySide6.QtCore import QObject

            from vienetts_app.app import create_app
            from vienetts_app.ui.controller import AppController

            data_dir = Path(sys.argv[1])
            (data_dir / "settings.json").write_text(
                json.dumps({"language": sys.argv[2]}), encoding="utf-8"
            )

            def factory():
                return AppController(
                    data_dir=data_dir,
                    engine_factory=lambda **kw: (_ for _ in ()).throw(AssertionError("no model")),
                    worker_factory=lambda engine: None,
                    catalog=lambda: [],
                    saved_names=lambda vd: [],
                )

            app, engine = create_app(controller_factory=factory)
            controller = engine._controller
            bridge = engine.rootContext().contextProperty("bridge")
            # The nav labels come from ShellBridge.tabs (runtime self.tr over
            # QT_TRANSLATE_NOOP'd TABS) — a translated read proves the
            # translator was installed before the QML/property evaluation.
            window = engine.rootObjects()[0]
            # And a rendered qsTr binding (SettingsTab's color-mode label,
            # source "Chế độ màu sắc") proves QML itself consults the
            # translator — the full settings→controller→qm→QML chain.
            qml_translated = any(
                o.property("text") == "Color mode" for o in window.findChildren(QObject)
            )
            print("RESULT:" + json.dumps({
                "applied": controller.appliedLanguage,
                "translator_anchored": getattr(engine, "_translator", None) is not None,
                "first_nav_label": bridge.tabs[0]["label"],
                "qml_translated": qml_translated,
            }))
            """
        )

    def _run(self, tmp_path: Path, language: str) -> dict:
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
        proc = subprocess.run(
            [sys.executable, "-c", self._script(), str(tmp_path), language],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        (line,) = (ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:"))
        return json.loads(line.removeprefix("RESULT:"))

    def test_english_setting_installs_translator(self, tmp_path: Path) -> None:
        result = self._run(tmp_path, "en")
        assert result["applied"] == "en"
        assert result["translator_anchored"] is True
        assert result["first_nav_label"] == "Text"
        assert result["qml_translated"] is True

    def test_language_switch_applies_live_without_restart(self, tmp_path: Path) -> None:
        # Flip vi → en mid-session: the bootstrap swaps the translator and
        # retranslate()s, so QML bindings and the nav re-render in English
        # with NO restart (the restart banner is gone entirely).
        script = textwrap.dedent(
            """\
            import json
            import sys
            from pathlib import Path

            from PySide6.QtCore import QObject

            from vienetts_app.app import create_app
            from vienetts_app.ui.controller import AppController

            data_dir = Path(sys.argv[1])
            (data_dir / "settings.json").write_text(
                json.dumps({"language": "vi"}), encoding="utf-8"
            )

            def factory():
                return AppController(
                    data_dir=data_dir,
                    engine_factory=lambda **kw: (_ for _ in ()).throw(AssertionError("no model")),
                    worker_factory=lambda engine: None,
                    catalog=lambda: [],
                    saved_names=lambda vd: [],
                )

            app, engine = create_app(controller_factory=factory)
            controller = engine._controller
            bridge = engine.rootContext().contextProperty("bridge")
            window = engine.rootObjects()[0]

            def qml_texts():
                return [o.property("text") for o in window.findChildren(QObject)]

            assert "Color mode" not in qml_texts()  # still Vietnamese pre-switch
            before = bridge.tabs[0]["label"]
            controller.language = "en"  # the Settings-tab write
            app.processEvents()
            after = bridge.tabs[0]["label"]
            print("RESULT:" + json.dumps({
                "nav_before": before,
                "nav_after": after,
                "qml_english_after": "Color mode" in qml_texts(),
                "persisted": json.loads(
                    (data_dir / "settings.json").read_text(encoding="utf-8")
                )["language"],
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
        assert result["nav_before"] == "Văn bản"
        assert result["nav_after"] == "Text"
        assert result["qml_english_after"] is True
        assert result["persisted"] == "en"
    def test_function_mediated_qstr_refreshes_on_language_flip(self, tmp_path: Path) -> None:
        # AudiobookTab's statusText pattern: qsTr INSIDE a JS function does
        # not register a translation dependency with retranslate() — the
        # function must read controller.language so every calling binding
        # refreshes on the live swap. Pins that mechanism against the real
        # catalog (context "AudiobookTab", source "Sẵn sàng" → "Ready").
        snippet = tmp_path / "AudiobookTab.qml"
        snippet.write_text(
            "import QtQml\n"
            "QtObject {\n"
            "    property string status: {\n"
            "        controller.language;  // dependency read (statusText idiom)\n"
            "        return (function(s) {\n"
            '            switch (s) { case "ready": return qsTr("Sẵn sàng"); }\n'
            '        })("ready");\n'
            "    }\n"
            "}\n",
            encoding="utf-8",
        )
        script = textwrap.dedent(
            """\
            import json
            import sys
            from pathlib import Path

            from PySide6.QtCore import Property, QObject, QUrl, Signal
            from PySide6.QtGui import QGuiApplication
            from PySide6.QtQml import QQmlComponent, QQmlEngine
            from vienetts_app.ui.i18n import translator_for

            class Ctrl(QObject):
                languageChanged = Signal()

                def __init__(self):
                    super().__init__()
                    self._language = "vi"

                @Property(str, notify=languageChanged)
                def language(self):
                    return self._language

                @language.setter
                def language(self, value):
                    if value != self._language:
                        self._language = value
                        self.languageChanged.emit()

            app = QGuiApplication([])
            engine = QQmlEngine()
            ctrl = Ctrl()
            engine.rootContext().setContextProperty("controller", ctrl)
            obj = QQmlComponent(engine, QUrl.fromLocalFile(sys.argv[1]))
            if obj.isError():
                print(obj.errorString()); raise SystemExit(1)
            item = obj.create()
            before = item.property("status")
            translator = translator_for("en")
            app.installTranslator(translator)
            ctrl.language = "en"
            engine.retranslate()
            after = item.property("status")
            print("RESULT:" + json.dumps({"before": before, "after": after}))
            """
        )
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
        proc = subprocess.run(
            [sys.executable, "-c", script, str(snippet)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        (line,) = (ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:"))
        result = json.loads(line.removeprefix("RESULT:"))
        assert result["before"] == "Sẵn sàng"
        assert result["after"] == "Ready"


class TestPlaybackWiring:
    """create_app registers + anchors PlaybackController (lazily-constructed,
    so startup never touches QtMultimedia); playback_factory injection works."""

    def test_playback_wiring_and_injection(self, tmp_path: Path) -> None:
        script = textwrap.dedent(
            """\
            import json

            from PySide6.QtCore import QObject, Slot

            from vienetts_app.app import create_app
            from vienetts_app.ui.playback import PlaybackController

            # 1. Default playback
            _app1, engine1 = create_app()
            playback1 = engine1.rootContext().contextProperty("playback")

            # 2. Injected playback
            class FakePlayback(QObject):
                def __init__(self):
                    super().__init__()
                    self.played = []

                @Slot(str)
                def play(self, path):
                    self.played.append(str(path))

            fake = FakePlayback()
            _app2, engine2 = create_app(playback_factory=lambda: fake)
            print("RESULT:" + json.dumps({
                "default_ok": isinstance(playback1, PlaybackController),
                "default_anchored": getattr(engine1, "_playback", None) is playback1,
                "default_initial_state": playback1.property("state"),
                "injected_registered": engine2.rootContext().contextProperty("playback") is fake,
                "injected_anchored": getattr(engine2, "_playback", None) is fake,
                "injected_ok": isinstance(fake, PlaybackController) is False,
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
        assert result["default_anchored"] is True
        assert result["default_initial_state"] == "stopped"
        assert result["injected_registered"] is True
        assert result["injected_anchored"] is True
        assert result["injected_ok"] is True

class TestAppMetadataAndIcon:
    """create_app sets application metadata properties and window icon."""

    def test_application_metadata_and_icon_set(self, tmp_path: Path) -> None:
        script = textwrap.dedent(
            """\
            import json

            from PySide6.QtGui import QGuiApplication

            from vienetts_app.app import create_app

            app, _engine = create_app()
            icon = app.windowIcon()
            result = {
                "name": app.applicationName(),
                "display_name": app.applicationDisplayName(),
                "org_name": app.organizationName(),
                "desktop_file_name": app.desktopFileName(),
                "icon_is_null": icon.isNull(),
            }
            print("RESULT:" + json.dumps(result))
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
        assert result["name"] == "VieNeuTTS"
        assert result["display_name"] == "VieNeuTTS"
        assert result["org_name"] == "VieNeuTTS"
        assert result["desktop_file_name"] == "vienetts-app"
        assert result["icon_is_null"] is False



class TestFocusClearing:
    """Clicking outside an active editable text control clears its focus."""

    def test_clicking_outside_and_inside_text_editor(self, tmp_path: Path) -> None:
        script = textwrap.dedent(
            """\
            import json
            from PySide6.QtCore import QPointF, Qt, QEvent
            from PySide6.QtGui import QMouseEvent
            from PySide6.QtQuick import QQuickItem
            from vienetts_app.app import create_app

            app, engine = create_app()
            window = engine.rootObjects()[0]
            text_editor = window.findChild(QQuickItem, "textEditor")

            def click_at(pos):
                btn = Qt.MouseButton.LeftButton
                no_mod = Qt.KeyboardModifier.NoModifier
                press = QMouseEvent(QEvent.Type.MouseButtonPress, pos, pos, btn, btn, no_mod)
                release = QMouseEvent(QEvent.Type.MouseButtonRelease, pos, pos, btn, btn, no_mod)
                app.sendEvent(window, press)
                app.sendEvent(window, release)
                app.processEvents()
            # 1. Force focus
            text_editor.forceActiveFocus()
            app.processEvents()
            focused_initial = text_editor.property("activeFocus")

            # 2. Click outside (at 500, 50 - page header)
            click_at(QPointF(500, 50))
            focused_after_outside = text_editor.property("activeFocus")

            # 3. Re-focus and click inside
            text_editor.forceActiveFocus()
            app.processEvents()
            center_pt = QPointF(text_editor.width() / 2, text_editor.height() / 2)
            ed_center = text_editor.mapToItem(None, center_pt)
            click_at(ed_center)
            focused_after_inside = text_editor.property("activeFocus")
            result = {
                "focused_initial": focused_initial,
                "focused_after_outside": focused_after_outside,
                "focused_after_inside": focused_after_inside,
            }
            print("RESULT:" + json.dumps(result))
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
        assert result["focused_initial"] is True
        assert result["focused_after_outside"] is False
        assert result["focused_after_inside"] is True

    def test_clicking_outside_paragraph_and_spinbox(self, tmp_path: Path) -> None:
        script = textwrap.dedent(
            """\
            import json
            from PySide6.QtCore import QPointF, Qt, QEvent
            from PySide6.QtGui import QMouseEvent
            from PySide6.QtQuick import QQuickItem
            from vienetts_app.app import create_app

            app, engine = create_app()
            window = engine.rootObjects()[0]
            bridge = engine.rootContext().contextProperty("bridge")

            def click_at(pos):
                btn = Qt.MouseButton.LeftButton
                no_mod = Qt.KeyboardModifier.NoModifier
                press = QMouseEvent(QEvent.Type.MouseButtonPress, pos, pos, btn, btn, no_mod)
                release = QMouseEvent(QEvent.Type.MouseButtonRelease, pos, pos, btn, btn, no_mod)
                app.sendEvent(window, press)
                app.sendEvent(window, release)
                app.processEvents()
            # ParagraphTab
            bridge.setCurrentTab("paragraph")
            app.processEvents()
            para_editor = window.findChild(QQuickItem, "paragraphEditor")
            para_editor.forceActiveFocus()
            app.processEvents()
            para_initial = para_editor.property("activeFocus")
            click_at(QPointF(500, 50))
            para_after_outside = para_editor.property("activeFocus")

            # SettingsTab (SpinBox)
            bridge.setCurrentTab("settings")
            app.processEvents()
            temp_spin = window.findChild(QQuickItem, "temperatureSpin")
            temp_input = temp_spin.property("contentItem")
            temp_input.forceActiveFocus()
            app.processEvents()
            spin_initial = temp_input.property("activeFocus")
            click_at(QPointF(500, 50))
            spin_after_outside = temp_input.property("activeFocus")

            result = {
                "para_initial": para_initial,
                "para_after_outside": para_after_outside,
                "spin_initial": spin_initial,
                "spin_after_outside": spin_after_outside,
            }
            print("RESULT:" + json.dumps(result))
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
        assert result["para_initial"] is True
        assert result["para_after_outside"] is False
        assert result["spin_initial"] is True
        assert result["spin_after_outside"] is False

    def test_focus_helpers_direct(self) -> None:
        from PySide6.QtCore import QPointF

        from vienetts_app.app import is_click_inside_active_control

        assert is_click_inside_active_control(None, QPointF(0, 0)) is False
