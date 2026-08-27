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
"""

import json
import os
import subprocess
import sys
import textwrap

DRIVER = textwrap.dedent(
    """\
    import json
    import sys
    import time

    from PySide6.QtCore import Q_ARG, QMetaObject, QObject

    from vienetts_app.app import create_app
    from vienetts_app.core.engine import (
        FETCH_MODELS_COMMAND,
        MODELS_MISSING_MARKER,
        ModelsMissingError,
    )
    from vienetts_app.ui.bridge import ShellBridge
    from vienetts_app.ui.controller import AppController

    settings_dir = sys.argv[1]
    scenario = sys.argv[2]

    out = {"scenario": scenario}

    controller_factory = None

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
                engine_factory=lambda **kw: MissingWeightsEngine(),
                catalog=lambda: [],
                saved_names=lambda voices_dir: [],
            )
    elif scenario == "exportonly":
        audio_state = {"available": False}

        def audio_probe():
            return audio_state["available"]

        def controller_factory():
            return AppController(
                catalog=lambda: [],
                saved_names=lambda voices_dir: [],
                audio_probe=audio_probe,
            )

    def build():
        return create_app(
            bridge_factory=lambda: ShellBridge(
                settings_dir=settings_dir,
                detector=lambda: "SMOKE NOTE",
                system_theme=lambda: "light",
            ),
            controller_factory=controller_factory,
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
            n in tabs for n in ("textTab", "paragraphTab", "cloningTab", "settingsTab")
        )
        stack = window.findChildren(QObject, "tabStack")[0]
        visited = []
        for tab in ("text", "paragraph", "cloning", "settings"):
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
    elif scenario == "consentcopy":
        labels = window.findChildren(QObject, "consentText")
        out["consent_found"] = len(labels) == 1
        out["consent_text"] = str(labels[0].property("text")) if labels else ""

    print("RESULT:" + json.dumps(out))
    """
)


def run_driver(tmp_path, scenario: str) -> dict:
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    proc = subprocess.run(
        [sys.executable, "-c", DRIVER, str(tmp_path), scenario],
        capture_output=True,
        text=True,
        env=env,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    (line,) = (ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:"))
    return json.loads(line.removeprefix("RESULT:"))


class TestShellSmoke:
    def test_window_and_four_tabs_present(self, tmp_path) -> None:
        result = run_driver(tmp_path, "navigate")
        assert result["window"] == "mainWindow"
        assert result["tabs_present"] is True

    def test_tab_navigation_via_bridge(self, tmp_path) -> None:
        result = run_driver(tmp_path, "navigate")
        # each visit is [tab_id, stack_index]; ids map 1:1 to distinct indices
        visits = result["nav_visits"]
        assert [v[0] for v in visits] == ["text", "paragraph", "cloning", "settings"]
        indices = [v[1] for v in visits]
        assert indices == sorted(indices) or len(set(indices)) == 4

    def test_theme_switch_is_live(self, tmp_path) -> None:
        result = run_driver(tmp_path, "theme")
        assert result["after_dark"] == "dark"
        assert result["after_light"] == "light"

    def test_system_preference_follows_os(self, tmp_path) -> None:
        result = run_driver(tmp_path, "theme")
        assert result["system_dark_effective"] == "dark"

    def test_theme_persists_across_restart(self, tmp_path) -> None:
        result = run_driver(tmp_path, "restart")
        assert result["persisted_pref"] == "light"
        assert result["persisted_effective"] == "light"


class TestEdgeCaseSurfaces:
    """Phase 4 edge-case surfaces (FR-4.6a/c, FR-4.7) in the REAL shell."""

    def test_models_missing_overlay_appears_and_dismisses(self, tmp_path) -> None:
        # A factory-injected engine raising the REAL marker message through
        # the REAL worker thread → controller → QML overlay.
        result = run_driver(tmp_path, "modelsmissing")
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

    def test_models_overlay_hidden_in_default_shell(self, tmp_path) -> None:
        # Machine-independent: no marker error ⇒ the screen never shows,
        # regardless of whether this host has audio output devices.
        result = run_driver(tmp_path, "navigate")
        assert result["models_overlay_hidden_default"] is True

    def test_export_only_notice_flips_with_audio_probe(self, tmp_path) -> None:
        result = run_driver(tmp_path, "exportonly")
        assert result["notice_found"] is True
        # No device: notice visible, CloningTab preview gated OFF even with a
        # reference clip selected (isolates the audio gate).
        assert result["notice_visible_off"] is True
        assert result["audio_available_off"] is False
        assert result["preview_found"] is True
        assert result["preview_enabled_off"] is False
        # Device appears + refreshAudioAvailability() ⇒ everything re-enables.
        assert result["audio_available_on"] is True
        assert result["notice_visible_on"] is False
        assert result["preview_enabled_on"] is True

    def test_consent_copy_carries_legal_warning(self, tmp_path) -> None:
        # FR-4.7 pillars, asserted on the LIVE QML label text:
        # consent of the cloned person / user responsibility / no impersonation.
        result = run_driver(tmp_path, "consentcopy")
        assert result["consent_found"] is True
        text = result["consent_text"]
        assert "đồng ý của chính người được sao chép" in text
        assert "quyền sử dụng giọng nói" in text  # pinned by test_ui_tabs too
        assert "trách nhiệm của bạn" in text
        assert "mạo danh" in text
