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
    def test_bootstrap_observer_and_metadata(self, tmp_path: Path) -> None:
        # Runs in a subprocess: the CLI dispatch tests above leave a headless
        # QCoreApplication in this process, and QML needs a QGuiApplication
        # (create_app raises RuntimeError in that case — by design).
        # Bootstrap surface, startup observer, and app metadata/icon all ride
        # ONE create_app call (previously three subprocess launches).
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
            events = []
            app, engine = create_app(
                bridge_factory=lambda: ShellBridge(
                    settings_dir=sys.argv[1], detector=lambda: "FAKE NOTE"
                ),
                startup_observer=events.append,
            )
            window = engine.rootObjects()[0]
            named = {o.objectName() for o in window.findChildren(QObject)}
            readout = window.findChildren(QObject, "engineReadout")[0]
            icon = app.windowIcon()
            print("RESULT:" + json.dumps({
                "root": window.objectName(),
                "nav_present": {"navBar", "engineReadout", "textTab", "settingsTab"} <= named,
                "note": readout.property("text"),
                "observer_events": events,
                "name": app.applicationName(),
                "display_name": app.applicationDisplayName(),
                "org_name": app.organizationName(),
                "desktop_file_name": app.desktopFileName(),
                "icon_is_null": icon.isNull(),
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
        assert result["observer_events"] == ["qml_loaded"]
        assert result["name"] == "VieNeuTTS"
        assert result["display_name"] == "VieNeuTTS"
        assert result["org_name"] == "VieNeuTTS"
        assert result["desktop_file_name"] == "vienetts-app"
        assert result["icon_is_null"] is False


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

    def test_bootstrap_live_switch_and_qstr_function(self, tmp_path: Path) -> None:
        # Three phases in ONE subprocess (fresh engine per phase — one
        # QGuiApplication per process): (1) boot with language=en from
        # settings proves the translator installs before QML evaluates;
        # (2) boot vi then live-flip to en proves no-restart retranslation;
        # (3) a bare QQmlEngine snippet pins the function-mediated qsTr
        # refresh idiom (AudiobookTab's statusText) against the real catalog.
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
            from PySide6.QtQml import QQmlComponent, QQmlEngine

            from vienetts_app.app import create_app
            from vienetts_app.ui.controller import AppController
            from vienetts_app.ui.i18n import translator_for

            data_dir = Path(sys.argv[1])
            out = {}

            def factory():
                return AppController(
                    data_dir=data_dir,
                    engine_factory=lambda **kw: (_ for _ in ()).throw(AssertionError("no model")),
                    worker_factory=lambda engine: None,
                    catalog=lambda: [],
                    saved_names=lambda vd: [],
                )

            def drop(engine):
                # Per-phase isolation: the translator is installed on the
                # shared QGuiApplication, so a leftover EN translator would
                # leak into the next (vi) phase's "still Vietnamese" check.
                t = getattr(engine, "_translator", None)
                if t is not None:
                    app.removeTranslator(t)
                engine.deleteLater()
                app.processEvents()

            # ── Phase 1: settings language=en at boot ──
            (data_dir / "settings.json").write_text(
                json.dumps({"language": "en"}), encoding="utf-8"
            )
            app, engine = create_app(controller_factory=factory)
            controller = engine._controller
            bridge = engine.rootContext().contextProperty("bridge")
            window = engine.rootObjects()[0]
            # The nav labels come from ShellBridge.tabs (runtime self.tr over
            # QT_TRANSLATE_NOOP'd TABS) — a translated read proves the
            # translator was installed before the QML/property evaluation.
            # And a rendered qsTr binding (SettingsTab's color-mode label,
            # source "Chế độ màu sắc") proves QML itself consults the
            # translator — the full settings→controller→qm→QML chain.
            out["en_applied"] = controller.appliedLanguage
            out["en_translator_anchored"] = getattr(engine, "_translator", None) is not None
            out["en_first_nav_label"] = bridge.tabs[0]["label"]
            out["en_qml_translated"] = any(
                o.property("text") == "Color mode" for o in window.findChildren(QObject)
            )
            drop(engine)

            # ── Phase 2: boot vi, flip to en mid-session (live, no restart) ──
            (data_dir / "settings.json").write_text(
                json.dumps({"language": "vi"}), encoding="utf-8"
            )
            app, engine = create_app(controller_factory=factory)
            controller = engine._controller
            bridge = engine.rootContext().contextProperty("bridge")
            window = engine.rootObjects()[0]

            def qml_texts():
                return [o.property("text") for o in window.findChildren(QObject)]

            assert "Color mode" not in qml_texts()  # still Vietnamese pre-switch
            out["vi_first_nav_label"] = bridge.tabs[0]["label"]
            controller.language = "en"  # the Settings-tab write
            app.processEvents()
            out["en_nav_label_after_flip"] = bridge.tabs[0]["label"]
            out["vi_qml_english_after"] = "Color mode" in qml_texts()
            out["persisted"] = json.loads(
                (data_dir / "settings.json").read_text(encoding="utf-8")
            )["language"]
            drop(engine)

            # ── Phase 3: function-mediated qsTr on a bare engine ──
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

            engine = QQmlEngine()
            ctrl = Ctrl()
            engine.rootContext().setContextProperty("controller", ctrl)
            obj = QQmlComponent(engine, QUrl.fromLocalFile(sys.argv[2]))
            if obj.isError():
                print(obj.errorString()); raise SystemExit(1)
            item = obj.create()
            out["snippet_before"] = item.property("status")
            translator = translator_for("en")
            app.installTranslator(translator)
            ctrl.language = "en"
            engine.retranslate()
            out["snippet_after"] = item.property("status")

            print("RESULT:" + json.dumps(out))
            """
        )
        env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
        proc = subprocess.run(
            [sys.executable, "-c", script, str(tmp_path), str(snippet)],
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        assert proc.returncode == 0, proc.stderr
        (line,) = (ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:"))
        result = json.loads(line.removeprefix("RESULT:"))
        # Phase 1: boot-time translator install (before QML evaluation).
        assert result["en_applied"] == "en"
        assert result["en_translator_anchored"] is True
        assert result["en_first_nav_label"] == "Text"
        assert result["en_qml_translated"] is True
        # Phase 2: live swap retranslates QML + nav with NO restart.
        assert result["vi_first_nav_label"] == "Văn bản"
        assert result["en_nav_label_after_flip"] == "Text"
        assert result["vi_qml_english_after"] is True
        assert result["persisted"] == "en"
        # Phase 3: the statusText idiom refreshes on the language flip.
        assert result["snippet_before"] == "Sẵn sàng"
        assert result["snippet_after"] == "Ready"


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


class TestFocusClearing:
    """Clicking outside an active editable text control clears its focus."""

    def test_clicking_outside_clears_focus_across_controls(self, tmp_path: Path) -> None:
        # One subprocess, three sections on one shell (previously three
        # launches): Text editor (outside clears / inside keeps), ParagraphTab
        # editor + SettingsTab SpinBox input (outside clears), and the
        # wrapper-recreation regression for FocusClearFilter itself.
        script = textwrap.dedent(
            """\
            import json
            from PySide6.QtCore import QEvent, QPointF, Qt
            from PySide6.QtGui import QMouseEvent
            from PySide6.QtQuick import QQuickItem
            from vienetts_app.app import FocusClearFilter, create_app

            app, engine = create_app()
            window = engine.rootObjects()[0]
            bridge = engine.rootContext().contextProperty("bridge")
            out = {}

            def click_at(pos):
                btn = Qt.MouseButton.LeftButton
                no_mod = Qt.KeyboardModifier.NoModifier
                press = QMouseEvent(QEvent.Type.MouseButtonPress, pos, pos, btn, btn, no_mod)
                release = QMouseEvent(QEvent.Type.MouseButtonRelease, pos, pos, btn, btn, no_mod)
                app.sendEvent(window, press)
                app.sendEvent(window, release)
                app.processEvents()

            # 1. Text editor: force focus → outside click clears → inside keeps
            text_editor = window.findChild(QQuickItem, "textEditor")
            text_editor.forceActiveFocus()
            app.processEvents()
            out["focused_initial"] = text_editor.property("activeFocus")
            click_at(QPointF(500, 50))  # page header, outside the editor
            out["focused_after_outside"] = text_editor.property("activeFocus")
            text_editor.forceActiveFocus()
            app.processEvents()
            center_pt = QPointF(text_editor.width() / 2, text_editor.height() / 2)
            click_at(text_editor.mapToItem(None, center_pt))
            out["focused_after_inside"] = text_editor.property("activeFocus")

            # 2. ParagraphTab editor + SettingsTab SpinBox input
            bridge.setCurrentTab("paragraph")
            app.processEvents()
            para_editor = window.findChild(QQuickItem, "paragraphEditor")
            para_editor.forceActiveFocus()
            app.processEvents()
            out["para_initial"] = para_editor.property("activeFocus")
            click_at(QPointF(500, 50))
            out["para_after_outside"] = para_editor.property("activeFocus")

            bridge.setCurrentTab("settings")
            app.processEvents()
            temp_spin = window.findChild(QQuickItem, "temperatureSpin")
            temp_input = temp_spin.property("contentItem")
            temp_input.forceActiveFocus()
            app.processEvents()
            out["spin_initial"] = temp_input.property("activeFocus")
            click_at(QPointF(500, 50))
            out["spin_after_outside"] = temp_input.property("activeFocus")

            # 3. Regression: Python wrapper recreation (instance attribute
            #    _win absent) must fall back to the parent, not crash.
            filt = getattr(engine, "_focus_clear_filter", None)
            assert filt is not None
            if hasattr(filt, "_win"):
                del filt._win
            text_editor.forceActiveFocus()
            app.processEvents()
            out["filter_focused_initial"] = text_editor.property("activeFocus")
            btn = Qt.MouseButton.LeftButton
            no_mod = Qt.KeyboardModifier.NoModifier
            pos = QPointF(500, 50)
            press = QMouseEvent(QEvent.Type.MouseButtonPress, pos, pos, btn, btn, no_mod)
            filt.eventFilter(window, press)
            app.processEvents()
            out["filter_focused_after"] = text_editor.property("activeFocus")

            print("RESULT:" + json.dumps(out))
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
        assert result["para_initial"] is True
        assert result["para_after_outside"] is False
        assert result["spin_initial"] is True
        assert result["spin_after_outside"] is False
        assert result["filter_focused_initial"] is True
        assert result["filter_focused_after"] is False

    def test_focus_helpers_direct(self) -> None:
        from PySide6.QtCore import QPointF

        from vienetts_app.app import is_click_inside_active_control

        assert is_click_inside_active_control(None, QPointF(0, 0)) is False


class TestStartupImportBudget:
    def test_gui_import_path_stays_lazy(self) -> None:
        # Cold-start budget (importtime-measured): the heavy optional deps
        # must not load until their code path actually runs — soundfile on
        # first WAV I/O, docx/pypdf on first .docx/.pdf import,
        # huggingface_hub only after a FAILED engine init. Subprocess: this
        # test process has long since imported everything.
        code = textwrap.dedent(
            """
            import sys
            import vienetts_app.app
            heavy = [
                m
                for m in ("soundfile", "docx", "pypdf", "requests", "urllib3",
                          "huggingface_hub")
                if m in sys.modules
                or any(k == m or k.startswith(m + ".") for k in sys.modules)
            ]
            assert not heavy, f"heavy modules loaded at startup: {heavy}"
            """
        )
        proc = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, timeout=60
        )
        assert proc.returncode == 0, proc.stderr
