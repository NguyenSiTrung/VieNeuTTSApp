"""Offscreen UI shell smoke suite (AC-2, AC-3, AC-5; NFR-2.2).

Launches the real GUI assembly — create_app + ShellBridge + Main.qml — under
``QT_QPA_PLATFORM=offscreen`` and drives it exactly like the smoke criteria
require: window present, four tabs navigable via the bridge, live theme
switch, persistence across a "restart" (a second bridge+window built
after a theme write, reading the same settings dir), plus the Phase 4
edge-case surfaces: models-missing overlay (FR-4.6c), export-only notice
(FR-4.6a), and the polished consent copy (FR-4.7).

Each scenario runs in its own subprocess: Qt allows exactly one
QGuiApplication per process, and pytest-qt's qapp fixture may leave a
headless QCoreApplication from the CLI tests (see track learnings — QML
aborts without a QGuiApplication). The subprocess script prints a
``RESULT:``-prefixed JSON line that these tests assert on.

Edge-case scenarios inject fakes ONLY at the controller's seams (an engine
factory raising the real ``ModelsMissingError`` marker message; an audio-
probe callable per FR-4.6a) while running the REAL controller, REAL worker
thread and REAL QML wiring — fake-at-the-seam per the project's pattern.

Tab-level audio gate (``audio_gate_tabs``, FR-4.6a): a forced-False probe on
the REAL controller drives BOTH synthesis tabs' playButton into export-only
posture (audio-ready state reached via a REAL batch job + quick export over
a success duck-typed engine), then refreshAudioAvailability() after the
probe flips True re-enables playback on both tabs.
"""

import json
import os
import subprocess
import sys
import textwrap

DRIVER = textwrap.dedent(
    """\
    import gc
    import json
    import sys
    import time
    from pathlib import Path

    from PySide6.QtCore import Q_ARG, QMetaObject, QObject, QPointF

    from vienetts_app.app import create_app
    from vienetts_app.core.engine import (
        FETCH_MODELS_COMMAND,
        MODELS_MISSING_MARKER,
        ModelsMissingError,
    )
    from vienetts_app.ui.bridge import ShellBridge
    from vienetts_app.ui.controller import AppController

    settings_root = sys.argv[1]
    scenarios = sys.argv[2].split(",")

    # This suite asserts Vietnamese UI copy; the app's "system" language
    # default follows the HOST locale (en_* hosts would render English and
    # break those assertions). Stub the controller's locale probe so every
    # scenario resolves the Vietnamese source language deterministically.
    import vienetts_app.ui.controller as _controller_module

    class _ViLocale:
        @staticmethod
        def system():
            return _ViLocale()

        def name(self):
            return "vi_VN"

    _controller_module.QLocale = _ViLocale

    results = {}
    for scenario in scenarios:
        # Per-scenario settings workspace keeps groups isolated.
        settings_dir = str(Path(settings_root) / scenario)
        Path(settings_dir).mkdir(parents=True, exist_ok=True)
        out = {"scenario": scenario}

        # The app controller owns persisted language and output preferences. Keep
        # it in the scenario directory too, otherwise a developer's real settings
        # can install the English translator and invalidate Vietnamese copy pins.
        controller_factory = lambda: AppController(data_dir=Path(settings_dir))

        if scenario == "modelsmissing":

            class MissingWeightsEngine:
                \"\"\"Duck-typed engine whose lazy init hits the REAL marker path.\"\"\"

                sample_rate = 48_000
                backend = "onnx"

                def infer(self, *args, **kwargs):
                    raise ModelsMissingError(
                        f"{MODELS_MISSING_MARKER}: the TTS model files were not "
                        f"found in the local Hugging Face cache (missing). Fetch "
                        f"the offline bundle once with `{FETCH_MODELS_COMMAND}`."
                    )

                def close(self):
                    pass

            def controller_factory():
                return AppController(
                    data_dir=Path(settings_dir),
                    engine_factory=lambda **kw: MissingWeightsEngine(),
                    catalog=lambda: [],
                    saved_names=lambda voices_dir: [],
                )
        elif scenario in ("exportonly", "narrow_layout"):
            audio_state = {"available": False}

            def audio_probe():
                return audio_state["available"]

            def controller_factory():
                return AppController(
                    data_dir=Path(settings_dir),
                    catalog=lambda: [],
                    saved_names=lambda voices_dir: [],
                    audio_probe=audio_probe,
                )

        elif scenario == "audio_gate_tabs":
            import numpy as np

            audio_state = {"available": False}

            def audio_probe():
                return audio_state["available"]

            class ReadyEngine:
                \"\"\"Duck-typed engine whose batch infer succeeds immediately.\"\"\"

                sample_rate = 48_000
                backend = "onnx"

                def infer(self, *args, **kwargs):
                    return np.full(4800, 0.4, dtype=np.float32)

                def close(self):
                    pass

            def controller_factory():
                return AppController(
                    data_dir=Path(settings_dir),
                    engine_factory=lambda **kw: ReadyEngine(),
                    catalog=lambda: [],
                    saved_names=lambda voices_dir: [],
                    audio_probe=audio_probe,
                )

        def build():
            from vienetts_app.ui.audiobook_controller import AudiobookController

            return create_app(
                bridge_factory=lambda: ShellBridge(
                    settings_dir=settings_dir,
                    detector=lambda: "SMOKE NOTE",
                    system_theme=lambda: "light",
                ),
                controller_factory=controller_factory,
                # Keep the audiobook workspace inside the scenario tmp dir —
                # the default factory would touch the real user data dir.
                audiobook_factory=lambda controller: AudiobookController(
                    controller, data_dir=Path(settings_dir)
                ),
            )

        app, engine = build()
        window = engine.rootObjects()[0]

        def pump_until(cond, timeout=5.0):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if cond():
                    return True
                app.processEvents()
                time.sleep(0.01)
            return False

        if scenario == "navigate":
            tabs = [o.objectName() for o in window.findChildren(QObject)]
            out["window"] = window.objectName()
            out["tabs_present"] = all(
                n in tabs
                for n in ("textTab", "paragraphTab", "audiobookTab", "cloningTab", "settingsTab")
            )
            stack = window.findChildren(QObject, "tabStack")[0]
            visited = []
            for tab in ("text", "paragraph", "audiobook", "cloning", "settings"):
                bridge = engine.rootContext().contextProperty("bridge")
                bridge.setCurrentTab(tab)
                app.processEvents()
                # QML-declared property: read through the meta-object
                visited.append([tab, stack.property("currentIndex")])
            out["nav_visits"] = visited
            # Machine-independent default-state assertion: without a marker error
            # the models-missing screen NEVER shows (the export-only notice IS
            # machine-dependent here — real probe vs host devices — so it is not
            # asserted in this scenario).
            overlay = window.findChildren(QObject, "modelsMissingOverlay")[0]
            out["models_overlay_hidden_default"] = not bool(overlay.property("visible"))
        elif scenario == "theme":
            bridge = engine.rootContext().contextProperty("bridge")
            out["initial_pref"] = bridge.themePreference
            out["initial_effective"] = bridge.effectiveTheme
            # live switch dark → light with system=light
            bridge.currentTab = "settings"
            bridge.themePreference = "dark"
            app.processEvents()
            out["after_dark"] = bridge.effectiveTheme
            bridge.themePreference = "light"
            app.processEvents()
            out["after_light"] = bridge.effectiveTheme
            # simulate OS flip while pref=system → effective follows
            bridge._system_theme = lambda: "dark"  # noqa: SLF001 - test seam
            bridge.themePreference = "system"
            bridge.refreshSystemTheme()
            app.processEvents()
            out["system_dark_effective"] = bridge.effectiveTheme
        elif scenario == "restart":
            bridge = engine.rootContext().contextProperty("bridge")
            bridge.themePreference = "light"
            app.processEvents()
            # "restart": fresh bridge + engine against the same settings dir
            app2, engine2 = build()
            bridge2 = engine2.rootContext().contextProperty("bridge")
            out["persisted_pref"] = bridge2.themePreference
            out["persisted_effective"] = bridge2.effectiveTheme
        elif scenario == "modelsmissing":
            controller = engine.rootContext().contextProperty("controller")
            out["initial_missing"] = bool(controller.modelsMissing)
            controller.generate("Xin chào", "Adam")
            # Real InferenceWorker thread: error() is queued — pump until it lands.
            out["missing_after_error"] = pump_until(
                lambda: controller.modelsMissing and not controller.busy
            )
            overlays = window.findChildren(QObject, "modelsMissingOverlay")
            out["overlay_found"] = len(overlays) == 1
            out["overlay_visible"] = bool(overlays[0].property("visible"))
            commands = window.findChildren(QObject, "modelsMissingCommand")
            out["command_found"] = len(commands) == 1
            out["command_text"] = str(commands[0].property("text"))
            retry_buttons = window.findChildren(QObject, "modelsRetryButton")
            out["retry_found"] = len(retry_buttons) == 1
            # Retry DISMISSES the overlay (fix runs outside the app); a fresh
            # marker error would raise it again via onModelsMissingChanged.
            QMetaObject.invokeMethod(retry_buttons[0], "click")
            app.processEvents()
            out["overlay_after_retry"] = bool(overlays[0].property("visible"))
            out["flag_still_true"] = bool(controller.modelsMissing)
            controller.shutdown()  # stop the real worker thread before exit
        elif scenario == "exportonly":
            controller = engine.rootContext().contextProperty("controller")
            app.processEvents()
            notices = window.findChildren(QObject, "exportOnlyNotice")
            out["notice_found"] = len(notices) == 1
            out["notice_visible_off"] = bool(notices[0].property("visible"))
            out["audio_available_off"] = bool(controller.audioAvailable)
            refresh_buttons = window.findChildren(QObject, "audioRefreshButton")
            out["refresh_variant"] = (
                refresh_buttons[0].property("variant") if refresh_buttons else ""
            )
            cloning_tabs = window.findChildren(QObject, "cloningTab")
            previews = window.findChildren(QObject, "previewPlayButton")
            out["preview_found"] = len(previews) == 1
            if cloning_tabs and previews:
                # Select a clip so ONLY the audio gate can hold enabled=False
                # (QML function args travel as QVariant through the metaobject).
                QMetaObject.invokeMethod(
                    cloning_tabs[0], "selectClip", Q_ARG("QVariant", "/tmp/reference.wav")
                )
                app.processEvents()
                out["preview_enabled_off"] = bool(previews[0].property("enabled"))
            audio_state["available"] = True  # device hot-plugged
            controller.refreshAudioAvailability()
            app.processEvents()
            out["audio_available_on"] = bool(controller.audioAvailable)
            out["notice_visible_on"] = bool(notices[0].property("visible"))
            if previews:
                out["preview_enabled_on"] = bool(previews[0].property("enabled"))
        elif scenario == "narrow_layout":
            window.setWidth(640)
            window.setHeight(740)
            window.show()
            for _ in range(10):
                app.processEvents()
                time.sleep(0.01)
            notice = window.findChildren(QObject, "exportOnlyNotice")[0]
            tabs = {
                name: window.findChildren(QObject, name)[0]
                for name in ("textTab", "paragraphTab", "audiobookTab", "cloningTab", "settingsTab")
            }
            out["notice_visible"] = bool(notice.property("visible"))
            out["notice_bottom"] = float(notice.y() + notice.height())
            out["tab_y"] = float(tabs["textTab"].mapToScene(QPointF(0, 0)).y())
            out["nav_width"] = float(window.findChildren(QObject, "navBar")[0].width())

            def tab_find(tab, name):
                (item,) = tab.findChildren(QObject, name)
                return item

            critical_items = {
                "text": ("voicePicker", "generateButton", "quickExportButton"),
                "paragraph": ("voicePicker", "generateButton", "exportButton"),
                "settings": (
                    "backendCombo",
                    "precisionCombo",
                    "defaultVoiceCombo",
                    "outputDirBrowseButton",
                    "temperatureSpin",
                ),
                "cloning": ("consentAcceptButton",),
            }
            bridge = engine.rootContext().contextProperty("bridge")
            out["window_width"] = float(window.width())
            out["tab_widths"] = {}
            out["critical_right_edges"] = {}
            for tab_name, names in critical_items.items():
                bridge.setCurrentTab(tab_name)
                app.processEvents()
                tab = tabs[tab_name + "Tab"]
                out["tab_widths"][tab_name] = float(tab.width())
                out["critical_right_edges"].update(
                    {
                        name: float(
                            tab_find(tab, name).mapToScene(
                                QPointF(tab_find(tab, name).width(), 0)
                            ).x()
                        )
                        for name in names
                    }
                )
        elif scenario == "consentcopy":
            labels = window.findChildren(QObject, "consentText")
            out["consent_found"] = len(labels) == 1
            out["consent_text"] = str(labels[0].property("text")) if labels else ""
        elif scenario == "audio_gate_tabs":
            from pathlib import Path

            text_tab = window.findChildren(QObject, "textTab")[0]
            para_tab = window.findChildren(QObject, "paragraphTab")[0]

            def tab_find(tab, name):
                matches = tab.findChildren(QObject, name)
                assert len(matches) == 1, name
                return matches[0]

            controller = engine.rootContext().contextProperty("controller")
            text_play = tab_find(text_tab, "playButton")
            para_play = tab_find(para_tab, "playButton")
            text_quick = tab_find(text_tab, "quickExportButton")
            para_export = tab_find(para_tab, "exportButton")

            # Ready-minus-device state through REAL flows: a batch job on the
            # real worker thread (queued done signal), then a quick export that
            # writes an actual WAV. Only the audio gate can then hold playButton.
            controller.generate("Xin chào thế giới", "Adam")
            out["audio_ready"] = pump_until(
                lambda: controller.hasAudio and not controller.busy, timeout=15.0
            )
            controller.outputDir = settings_dir  # keep the export inside tmp
            QMetaObject.invokeMethod(text_quick, "click")
            out["export_path_set"] = pump_until(
                lambda: str(controller.lastExportPath) != "", timeout=5.0
            )
            out["wav_exists"] = Path(str(controller.lastExportPath)).is_file()

            # Probe False → export-only posture (FR-4.6a): exports usable on BOTH
            # tabs while every playback button is gated off.
            out["audio_available_off"] = bool(controller.audioAvailable)
            out["text_export_enabled_off"] = bool(text_quick.property("enabled"))
            out["para_export_enabled_off"] = bool(para_export.property("enabled"))
            out["text_play_disabled_off"] = not bool(text_play.property("enabled"))
            out["para_play_disabled_off"] = not bool(para_play.property("enabled"))

            # Device hot-plug seam: probe flips True; refreshAudioAvailability()
            # re-probes and re-notifies → both tabs' playback controls re-enable.
            audio_state["available"] = True
            controller.refreshAudioAvailability()
            pump_until(
                lambda: bool(text_play.property("enabled"))
                and bool(para_play.property("enabled")),
                timeout=5.0,
            )
            app.processEvents()
            out["audio_available_after_refresh"] = bool(controller.audioAvailable)
            out["text_play_enabled_after_refresh"] = bool(text_play.property("enabled"))
            out["para_play_enabled_after_refresh"] = bool(para_play.property("enabled"))
            controller.shutdown()  # stop the real worker thread before exit

        results[scenario] = out
        # Deterministic engine teardown before the next scenario
        # reuses this process (one QGuiApplication per process).
        engine.deleteLater()
        window = None
        engine = None
        gc.collect()
        app.processEvents()

    print("RESULT:" + json.dumps(results))
    """
)


def run_driver(tmp_path, scenarios: list[str]) -> dict[str, dict]:
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    proc = subprocess.run(
        [sys.executable, "-c", DRIVER, str(tmp_path), ",".join(scenarios)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    (line,) = (ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:"))
    return json.loads(line.removeprefix("RESULT:"))


class TestShellSmoke:
    def test_shell_navigation_theme_restart_and_layout(self, tmp_path) -> None:
        results = run_driver(tmp_path, ["navigate", "theme", "restart", "narrow_layout"])
        result = results["navigate"]
        assert result["window"] == "mainWindow"
        assert result["tabs_present"] is True
        assert result["models_overlay_hidden_default"] is True
        visits = result["nav_visits"]
        assert [v[0] for v in visits] == [
            "text",
            "paragraph",
            "audiobook",
            "cloning",
            "settings",
        ]
        indices = [v[1] for v in visits]
        assert indices == sorted(indices) or len(set(indices)) == 5

        result = results["theme"]
        assert result["after_dark"] == "dark"
        assert result["after_light"] == "light"
        assert result["system_dark_effective"] == "dark"

        result = results["restart"]
        assert result["persisted_pref"] == "light"
        assert result["persisted_effective"] == "light"

        result = results["narrow_layout"]
        assert result["notice_visible"] is True
        assert result["notice_bottom"] <= result["tab_y"]
        assert all(width >= 560 for width in result["tab_widths"].values())
        assert result["nav_width"] <= 80
        assert all(
            right <= result["window_width"] for right in result["critical_right_edges"].values()
        )


class TestEdgeCaseSurfaces:
    """Phase 4 edge-case surfaces (FR-4.6a/c, FR-4.7) in the REAL shell."""

    def test_edge_surfaces(self, tmp_path) -> None:
        results = run_driver(tmp_path, ["modelsmissing", "exportonly", "consentcopy"])
        # A factory-injected engine raising the REAL marker message through
        # the REAL worker thread → controller → QML overlay.
        result = results["modelsmissing"]
        assert result["initial_missing"] is False
        assert result["missing_after_error"] is True  # queued signal processed
        assert result["overlay_found"] is True
        assert result["overlay_visible"] is True
        assert "python scripts/fetch_models.py" in result["command_text"]
        assert result["retry_found"] is True
        # Retry dismisses the overlay; the underlying flag stays True until
        # the next successful op start re-evaluates it (controller contract).
        assert result["overlay_after_retry"] is False
        assert result["flag_still_true"] is True

        result = results["exportonly"]
        assert result["notice_found"] is True
        # No device: notice visible, CloningTab preview gated OFF even with a
        # reference clip selected (isolates the audio gate).
        assert result["notice_visible_off"] is True
        assert result["audio_available_off"] is False
        assert result["refresh_variant"] == "quiet"
        assert result["preview_found"] is True
        assert result["preview_enabled_off"] is False
        # Device appears + refreshAudioAvailability() ⇒ everything re-enables.
        assert result["audio_available_on"] is True
        assert result["notice_visible_on"] is False
        assert result["preview_enabled_on"] is True

        result = results["consentcopy"]
        # FR-4.7 pillars, asserted on the LIVE QML label text:
        # consent of the cloned person / user responsibility / no impersonation.
        assert result["consent_found"] is True
        text = result["consent_text"]
        assert "đồng ý của chính người được sao chép" in text
        assert "quyền sử dụng giọng nói" in text  # pinned by test_ui_tabs too
        assert "trách nhiệm của bạn" in text
        assert "mạo danh" in text


class TestTabPlaybackAudioGate:
    """FR-4.6a at the TAB level: playButton gated by the injected probe on
    BOTH synthesis tabs.

    Existing coverage pins the global export-only banner + CloningTab preview;
    nobody yet proves the TextTab/ParagraphTab play buttons go export-only
    (disabled with everything else ready) when the audio probe returns False,
    and re-enable after refreshAudioAvailability() flips the probe True.
    """

    def test_playbutton_gated_on_both_tabs_then_refresh_reenables(self, tmp_path) -> None:
        results = run_driver(tmp_path, ["audio_gate_tabs"])
        result = results["audio_gate_tabs"]
        # Ready-minus-device state reached via REAL batch flow + quick export.
        assert result["audio_ready"] is True
        assert result["export_path_set"] is True
        assert result["wav_exists"] is True
        # Probe False → export-only posture: exports stay usable...
        assert result["text_export_enabled_off"] is True
        assert result["para_export_enabled_off"] is True
        # ...but playback controls are disabled on BOTH tabs (only the audio
        # gate can hold them back — hasAudio/busy/lastExportPath are satisfied).
        assert result["audio_available_off"] is False
        assert result["text_play_disabled_off"] is True
        assert result["para_play_disabled_off"] is True
        # Device "hot-plugged": refreshAudioAvailability() flips every gate.
        assert result["audio_available_after_refresh"] is True
        assert result["text_play_enabled_after_refresh"] is True
        assert result["para_play_enabled_after_refresh"] is True
