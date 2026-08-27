"""Offscreen Text-tab smoke suite (FR-3.2, AC-1).

Drives the real GUI assembly — create_app + Main.qml + the rewritten
TextTab.qml — under ``QT_QPA_PLATFORM=offscreen`` with a fake controller and
fake playback injected through ``create_app`` factories (NO model load, NO
QtMultimedia). Each scenario runs in its own subprocess (one QGuiApplication
per process; see conductor/patterns.md) and prints a ``RESULT:``-prefixed
JSON line these tests assert on — the same driver pattern as
``test_ui_shell.py``.

Fake-controller QML surface (mirrors AppController): voices, busy, progress,
errorText, hasAudio, lastExportPath, defaultVoice, outputDir, temperature +
cancelled signal + generate/cancel/exportWav slots. exportWav writes a REAL
tiny WAV via ``write_wav_file`` so the play button's
``lastExportPath !== ""`` requirement is exercised for real.

The QML ``FileDialog`` (exportButton → Save As) is authored but deliberately
NOT exercised here: native save dialogs are unreliable headless, so export
coverage goes through quickExportButton (default-dir export). Do not "fix"
the tests by opening the dialog offscreen.
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
    from pathlib import Path

    import numpy as np
    from PySide6.QtCore import Property, QObject, QThread, Signal, Slot

    from vienetts_app.app import create_app
    from vienetts_app.core.audio import write_wav_file
    from vienetts_app.ui.bridge import ShellBridge

    tmp = Path(sys.argv[1])
    scenario = sys.argv[2]
    DEFAULT_VOICE = "adam_north"


    class FakeController(QObject):
        \"\"\"AppController's QML surface, with recording slots.\"\"\"

        voicesChanged = Signal()
        busyChanged = Signal()
        progressChanged = Signal()
        errorTextChanged = Signal()
        hasAudioChanged = Signal()
        lastExportPathChanged = Signal()
        defaultVoiceChanged = Signal()
        outputDirChanged = Signal()
        temperatureChanged = Signal()
        cancelled = Signal()

        def __init__(self):
            super().__init__()
            self._voices = [
                {
                    "label": "Bắc",
                    "voices": [
                        {"id": DEFAULT_VOICE, "label": "Adam — Nam · Bắc · Ấm áp"},
                        {"id": "eva_north", "label": "Eva — Nữ · Bắc · Dịu dàng"},
                    ],
                },
                {
                    "label": "Đã sao chép",
                    "voices": [{"id": "my_clone", "label": "my_clone"}],
                },
            ]
            self._busy = False
            self._progress = 0.0
            self._error_text = ""
            self._has_audio = False
            self._last_export_path = ""
            self._default_voice = DEFAULT_VOICE
            self._output_dir = str(tmp)
            self._temperature = 0.8
            self.generate_calls = []
            self.cancel_calls = 0
            self.export_calls = []

        @Property("QVariantList", constant=True)
        def voices(self):
            return self._voices

        @Property(bool, notify=busyChanged)
        def busy(self):
            return self._busy

        @busy.setter
        def busy(self, value):
            self._mutate("_busy", bool(value), self.busyChanged)

        @Property(float, notify=progressChanged)
        def progress(self):
            return self._progress

        @progress.setter
        def progress(self, value):
            self._mutate("_progress", float(value), self.progressChanged)

        @Property(str, notify=errorTextChanged)
        def errorText(self):
            return self._error_text

        @errorText.setter
        def errorText(self, value):
            self._mutate("_error_text", str(value), self.errorTextChanged)

        @Property(bool, notify=hasAudioChanged)
        def hasAudio(self):
            return self._has_audio

        @hasAudio.setter
        def hasAudio(self, value):
            self._mutate("_has_audio", bool(value), self.hasAudioChanged)

        @Property(str, notify=lastExportPathChanged)
        def lastExportPath(self):
            return self._last_export_path

        @lastExportPath.setter
        def lastExportPath(self, value):
            self._mutate("_last_export_path", str(value), self.lastExportPathChanged)

        @Property(str, notify=defaultVoiceChanged)
        def defaultVoice(self):
            return self._default_voice

        @Property(str, notify=outputDirChanged)
        def outputDir(self):
            return self._output_dir

        @Property(float, notify=temperatureChanged)
        def temperature(self):
            return self._temperature

        def _mutate(self, attr, value, signal):
            if value != getattr(self, attr):
                setattr(self, attr, value)
                signal.emit()

        @Slot(str, str)
        def generate(self, text, voice):
            self.generate_calls.append([str(text), str(voice)])

        @Slot()
        def cancel(self):
            self.cancel_calls += 1

        @Slot(str, result=bool)
        def exportWav(self, path):
            # "" means export to the default dir; write a real tiny WAV so
            # the play button's lastExportPath requirement is genuine.
            self.export_calls.append(str(path))
            target = Path(path) if str(path).strip() else tmp / "quick_export.wav"
            write_wav_file(np.linspace(-0.2, 0.2, 480).astype(np.float32), target)
            self._mutate("_last_export_path", str(target), self.lastExportPathChanged)
            return True


    class FakePlayback(QObject):
        \"\"\"PlaybackController's QML surface, recording what got played.\"\"\"

        def __init__(self):
            super().__init__()
            self.played = []

        @Slot(str)
        def play(self, path):
            self.played.append(str(path))

        @Slot()
        def stop(self):
            pass

        @Slot()
        def pause(self):
            pass

        @Slot()
        def resume(self):
            pass


    controller = FakeController()
    playback = FakePlayback()

    app, engine = create_app(
        bridge_factory=lambda: ShellBridge(
            settings_dir=tmp, detector=lambda: "SMOKE NOTE"
        ),
        controller_factory=lambda: controller,
        playback_factory=lambda: playback,
    )
    window = engine.rootObjects()[0]


    def find(name):
        return window.findChildren(QObject, name)[0]


    def qjs_to_py(value):
        # QML `property var` reads come back as QJSValue wrappers.
        return value.toVariant() if hasattr(value, "toVariant") else value


    def wait_ms(ms):
        # Timer-driven toasts need the event loop to tick.
        for _ in range(ms // 50):
            QThread.msleep(50)
            app.processEvents()


    out = {"scenario": scenario}

    if scenario == "load":
        names = {o.objectName() for o in window.findChildren(QObject)}
        required = {
            "textTab", "textEditor", "voicePicker", "generateButton", "progressBar",
            "busyLabel", "cancelButton", "playButton", "exportButton",
            "quickExportButton", "errorLabel", "toastLabel",
        }
        out["missing"] = sorted(required - names)
        picker = find("voicePicker")
        flat = qjs_to_py(picker.property("flatModel"))
        out["flat_ids"] = [row["id"] for row in flat]
        out["flat_labels"] = [row["label"] for row in flat]
        out["current_index"] = picker.property("currentIndex")
        out["selected_voice"] = picker.property("selectedVoice")
        out["editor_placeholder"] = find("textEditor").property("placeholderText")
        out["generate_text"] = find("generateButton").property("text")
        out["emotion_hint"] = any(
            "[cười]" in (o.property("text") or "")
            for o in window.findChildren(QObject)
        )
        out["initial_generate_enabled"] = find("generateButton").property("enabled")
    elif scenario == "generate_flow":
        editor = find("textEditor")
        generate = find("generateButton")
        progress = find("progressBar")
        cancel_btn = find("cancelButton")
        play = find("playButton")

        out["initial_generate_enabled"] = generate.property("enabled")
        editor.setProperty("text", "Xin chào thế giới")
        app.processEvents()
        out["filled_generate_enabled"] = generate.property("enabled")

        generate.click()
        app.processEvents()
        out["generate_calls"] = controller.generate_calls

        controller.busy = True
        app.processEvents()
        out["busy_generate_visible"] = generate.property("visible")
        out["busy_cancel_visible"] = cancel_btn.property("visible")
        out["busy_label_visible"] = find("busyLabel").property("visible")
        out["busy_progress_visible"] = progress.property("visible")
        out["busy_progress_value"] = progress.property("value")
        out["busy_progress_indeterminate"] = progress.property("indeterminate")
        out["busy_play_enabled"] = play.property("enabled")

        cancel_btn.click()
        app.processEvents()
        out["cancel_calls"] = controller.cancel_calls

        controller.progress = 0.5
        app.processEvents()
        out["progress_mid"] = progress.property("value")
        out["indeterminate_mid"] = progress.property("indeterminate")

        controller.progress = 1.0
        app.processEvents()
        out["progress_full"] = progress.property("value")

        controller.hasAudio = True
        controller.lastExportPath = str(tmp / "generated.wav")
        controller.busy = False
        app.processEvents()
        out["play_enabled_after"] = play.property("enabled")
        out["progress_hidden_after"] = not progress.property("visible")
        out["cancel_hidden_after"] = not cancel_btn.property("visible")
        out["generate_visible_after"] = generate.property("visible")
    elif scenario == "export_flow":
        quick = find("quickExportButton")
        export_btn = find("exportButton")
        play = find("playButton")

        out["export_disabled_without_audio"] = not export_btn.property("enabled")
        out["quick_disabled_without_audio"] = not quick.property("enabled")

        controller.hasAudio = True
        app.processEvents()
        out["export_enabled_with_audio"] = export_btn.property("enabled")
        out["quick_enabled_with_audio"] = quick.property("enabled")
        out["play_disabled_before_export"] = not play.property("enabled")

        quick.click()
        app.processEvents()
        out["export_calls"] = controller.export_calls
        path = controller.lastExportPath
        out["last_export_path"] = path
        out["wav_exists"] = Path(path).is_file()
        out["play_enabled_after"] = play.property("enabled")

        play.click()
        app.processEvents()
        out["playback_played"] = playback.played
    elif scenario == "error_flow":
        err = find("errorLabel")
        toast = find("toastLabel")

        out["error_hidden_initially"] = not err.property("visible")

        controller.errorText = "Lỗi tổng hợp: không đủ bộ nhớ"
        app.processEvents()
        out["error_visible"] = err.property("visible")
        out["error_text"] = err.property("text")

        controller.errorText = ""
        app.processEvents()
        out["error_hidden_after_clear"] = not err.property("visible")

        out["toast_hidden_initially"] = not toast.property("visible")
        controller.cancelled.emit()
        app.processEvents()
        out["toast_visible_on_cancel"] = toast.property("visible")
        out["toast_text"] = toast.property("text")
        wait_ms(2400)  # toast Timer auto-hides after 2 s
        out["toast_hidden_after_timeout"] = not toast.property("visible")
    elif scenario == "disabled_states":
        editor = find("textEditor")
        generate = find("generateButton")

        editor.setProperty("text", "   ")
        app.processEvents()
        out["whitespace_generate_enabled"] = generate.property("enabled")

        editor.setProperty("text", "ok")
        app.processEvents()
        out["filled_generate_enabled"] = generate.property("enabled")

        controller.busy = True
        app.processEvents()
        out["busy_generate_visible"] = generate.property("visible")
        out["busy_cancel_visible"] = find("cancelButton").property("visible")

        controller.busy = False
        app.processEvents()
        out["idle_export_enabled"] = find("exportButton").property("enabled")
        out["idle_quick_enabled"] = find("quickExportButton").property("enabled")
        out["idle_play_enabled"] = find("playButton").property("enabled")

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


class TestTextTabSmoke:
    def test_load_objectnames_and_picker_model(self, tmp_path) -> None:
        result = run_driver(tmp_path, "load")
        # ⚑ contract: every named element exists under the real Main.qml.
        assert result["missing"] == []
        # Flat picker model: group headers (id "") then prefixed voices.
        assert result["flat_ids"] == ["", "adam_north", "eva_north", "", "my_clone"]
        labels = result["flat_labels"]
        assert "▸ Bắc" in labels
        assert "▸ Đã sao chép" in labels
        assert "— Adam — Nam · Bắc · Ấm áp" in labels
        assert "— my_clone" in labels
        # Preselection: currentIndex lands on defaultVoice.
        assert result["current_index"] == 1
        assert result["selected_voice"] == "adam_north"
        assert result["editor_placeholder"] == "Nhập hoặc dán văn bản tiếng Việt / English…"
        assert result["emotion_hint"] is True
        assert result["generate_text"] == "Tạo âm thanh"
        assert result["initial_generate_enabled"] is False

    def test_generate_flow_reaches_playable_audio(self, tmp_path) -> None:
        result = run_driver(tmp_path, "generate_flow")
        # Generate is wired: click passes (text, selectedVoice=default).
        assert result["initial_generate_enabled"] is False
        assert result["filled_generate_enabled"] is True
        assert result["generate_calls"] == [["Xin chào thế giới", "adam_north"]]
        # Busy state swaps generate for progress + cancel.
        assert result["busy_generate_visible"] is False
        assert result["busy_cancel_visible"] is True
        assert result["busy_label_visible"] is True
        assert result["busy_progress_visible"] is True
        assert result["busy_progress_value"] == 0
        assert result["busy_progress_indeterminate"] is True
        assert result["busy_play_enabled"] is False
        assert result["cancel_calls"] == 1
        # Progress value transitions 0 → 0.5 → 1 with indeterminate clearing.
        assert result["progress_mid"] == 0.5
        assert result["indeterminate_mid"] is False
        assert result["progress_full"] == 1.0
        # Done: play enabled, busy UI reverts.
        assert result["play_enabled_after"] is True
        assert result["progress_hidden_after"] is True
        assert result["cancel_hidden_after"] is True
        assert result["generate_visible_after"] is True

    def test_quick_export_enables_and_plays(self, tmp_path) -> None:
        result = run_driver(tmp_path, "export_flow")
        assert result["export_disabled_without_audio"] is True
        assert result["quick_disabled_without_audio"] is True
        assert result["export_enabled_with_audio"] is True
        assert result["quick_enabled_with_audio"] is True
        # Play stays disabled until an export produced a path (simplest
        # correct UX: export first, then play).
        assert result["play_disabled_before_export"] is True
        # Quick export routes through exportWav("") and writes a real WAV.
        assert result["export_calls"] == [""]
        assert result["last_export_path"].endswith(".wav")
        assert result["wav_exists"] is True
        assert result["play_enabled_after"] is True
        assert result["playback_played"] == [result["last_export_path"]]

    def test_error_banner_and_cancel_toast(self, tmp_path) -> None:
        result = run_driver(tmp_path, "error_flow")
        assert result["error_hidden_initially"] is True
        assert result["error_visible"] is True
        assert result["error_text"] == "Lỗi tổng hợp: không đủ bộ nhớ"
        assert result["error_hidden_after_clear"] is True
        assert result["toast_hidden_initially"] is True
        assert result["toast_visible_on_cancel"] is True
        assert result["toast_text"] == "Đã hủy"
        assert result["toast_hidden_after_timeout"] is True

    def test_disabled_states(self, tmp_path) -> None:
        result = run_driver(tmp_path, "disabled_states")
        assert result["whitespace_generate_enabled"] is False
        assert result["filled_generate_enabled"] is True
        assert result["busy_generate_visible"] is False
        assert result["busy_cancel_visible"] is True
        assert result["idle_export_enabled"] is False
        assert result["idle_quick_enabled"] is False
        assert result["idle_play_enabled"] is False
