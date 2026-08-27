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
cancelled signal + generate/cancel/exportWav slots, plus importDocument
(ParagraphTab's import seam — see below). exportWav writes a REAL
tiny WAV via ``write_wav_file`` so the play button's
``lastExportPath !== ""`` requirement is exercised for real.

The QML ``FileDialog`` (exportButton → Save As) is authored but deliberately
NOT exercised here: native save dialogs are unreliable headless, so export
coverage goes through quickExportButton (default-dir export). Do not "fix"
the tests by opening the dialog offscreen.

Paragraph/File tab (FR-3.3) — ``para_*`` scenarios: StackLayout instantiates
every tab, so shared objectNames (voicePicker, generateButton, ...) exist
TWICE in the window; paragraph lookups are scoped to the ``paragraphTab``
subtree and the tab is activated via ``bridge.setCurrentTab("paragraph")``
before click-driven assertions. Import seam: the QML calls
``controller.importDocument(path)`` and expects extracted text back — the
REAL AppController does not expose that slot yet (documented gap for the
integration task; the fake implements it, and QML guards with ``typeof`` so
the shipped UI shows an error label instead of crashing). The native import
dialog is authored but not opened headless (same policy as the export
dialog); ``para_import`` drives the QML-side ``importPath(path)`` — the
dialog's onAccepted entry point — via ``QMetaObject.invokeMethod`` on the
``paragraphTab`` item (QML function arguments are QVariant-typed in the
metaobject, hence ``Q_ARG("QVariant", ...)``).

Cloning tab (FR-3.4) — ``clone_*`` scenarios: the fake controller grows the
consent/voice-op surface (consentGiven + acknowledgeConsent, previewPath,
addVoice/removeVoice/denoisePreview; ``voices`` switches from constant to
NOTIFY so catalog updates re-render QML — addVoice appends to the cloned
group and emits voicesChanged like the real async completion). Lookups are
scoped to the ``cloningTab`` subtree and the tab is activated via
``bridge.setCurrentTab("cloning")``. The consent gate asserts the cloning
panel stays hidden until acknowledgeConsent() flips consentGiven. The clip
dialog's onAccepted seam is ``selectClip(path)`` — the same QMetaObject
idiom as ``importPath`` (native dialogs stay closed headless).

Text tab streaming (FR-4.3/FR-4.5) — ``stream_*`` scenarios:
* ``stream_bindings`` keeps the FakeController surface but adds the
  streaming API (generateStream slot + streamActive/streamLevel NOTIFY
  properties). Flipping the properties programmatically proves the
  WaveformIndicator bindings pick them up via ``.property()`` reads;
  ``slot_hits`` records WHICH submit slot ran so the generate→stream switch
  is pinned exactly.
* ``stream_e2e`` / ``stream_cancel`` swap the FakeController for the REAL
  AppController over a fake at the SDK layer (generator ``infer_stream``
  per spike §0) and a REAL StreamPlaybackController whose audio seam is
  faked (StreamPlaybackController's own duck-typed sink contract — zero
  QtMultimedia). This drives the whole stack: QML click → generateStream →
  InferenceWorker thread → chunk_ready → ring buffer → levelReady → QML
  envelope. Offscreen polling records the streamActive true→false cycle
  and the indicator's visibility DURING the session.

Paragraph/File tab streaming + oversize import (FR-4.4/FR-4.5/FR-4.6b,
AC-2) — ``para_stream_*`` / ``para_import_oversize``: the same contracts
scoped to the ``paragraphTab`` subtree. ``para_stream_bindings`` proves
this tab hosts the shared WaveformIndicator and submits through
generateStream (slot_hits); ``para_stream_e2e`` / ``para_stream_cancel``
run the REAL-controller harness with paragraph fixtures — cancel asserts
BOTH stop paths (busy/streamActive settled AND the sink back to
StoppedState via a captured fake-sink reference). This tab renders no
cancel toast by design (toastLabel belongs to TextTab's subtree).
``para_import_oversize`` imports a genuinely oversized .txt fixture
through the REAL AppController.importDocument and asserts the errorBanner
notice shows the IMPORT_CHAR_LIMIT refusal verbatim.
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
    from PySide6.QtCore import (
        Q_ARG,
        Q_RETURN_ARG,
        Property,
        QObject,
        QMetaObject,
        QThread,
        QUrl,
        Signal,
        Slot,
    )
    from PySide6.QtQuick import QQuickItem

    from vienetts_app.app import create_app
    from vienetts_app.core.audio import write_wav_file
    from vienetts_app.ui.bridge import ShellBridge
    from vienetts_app.ui.stream_playback import StreamPlaybackController

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
        consentGivenChanged = Signal()
        previewPathChanged = Signal()
        cancelled = Signal()
        backendChanged = Signal()
        precisionChanged = Signal()
        themeChanged = Signal()
        needsRestartChanged = Signal()
        streamActiveChanged = Signal()
        streamLevelChanged = Signal()
        audioAvailableChanged = Signal()

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
            self._backend = "auto"
            self._precision = "int8"
            self._theme = "system"
            self._needs_restart = False
            # Mirrors the real controller: engine-affecting settings only
            # flag needsRestart when an engine is ALREADY initialized.
            self.engine_initialized = False
            self.generate_calls = []
            self.cancel_calls = 0
            self.export_calls = []
            self.import_calls = []
            self.import_result = "Xin chào\\nThế giới"
            self._consent = False
            self._preview_path = ""
            self.consent_calls = 0
            self.add_voice_calls = []
            self.remove_voice_calls = []
            self.denoise_calls = []
            # Streaming surface (mirrors AppController FR-4.2) + which submit
            # slot the QML used ("generate" | "generateStream").
            self._stream_active = False
            self._stream_level = 0.0
            self.slot_hits = []
            # FR-4.6a seam: audio OUTPUT availability gates tab playback
            # buttons. Default True so export-first play flows stay asserted;
            # ui_shell covers the unavailable-device side with the REAL
            # controller's injected probe.
            self._audio_available = True

        @Property("QVariantList", notify=voicesChanged)
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

        @defaultVoice.setter
        def defaultVoice(self, value):
            self._mutate("_default_voice", str(value), self.defaultVoiceChanged)

        @Property(str, notify=outputDirChanged)
        def outputDir(self):
            return self._output_dir

        @outputDir.setter
        def outputDir(self, value):
            self._mutate("_output_dir", str(value), self.outputDirChanged)

        @Property(str, notify=backendChanged)
        def backend(self):
            return self._backend

        @backend.setter
        def backend(self, value):
            if self._mutate("_backend", str(value), self.backendChanged) and (
                self.engine_initialized
            ):
                self._mutate("_needs_restart", True, self.needsRestartChanged)

        @Property(str, notify=precisionChanged)
        def precision(self):
            return self._precision

        @precision.setter
        def precision(self, value):
            if self._mutate("_precision", str(value), self.precisionChanged) and (
                self.engine_initialized
            ):
                self._mutate("_needs_restart", True, self.needsRestartChanged)

        @Property(str, notify=themeChanged)
        def theme(self):
            return self._theme

        @theme.setter
        def theme(self, value):
            self._mutate("_theme", str(value), self.themeChanged)

        @Property(bool, notify=needsRestartChanged)
        def needsRestart(self):
            return self._needs_restart

        @Property(float, notify=temperatureChanged)
        def temperature(self):
            return self._temperature

        @temperature.setter
        def temperature(self, value):
            self._mutate("_temperature", float(value), self.temperatureChanged)

        def _mutate(self, attr, value, signal):
            if value != getattr(self, attr):
                setattr(self, attr, value)
                signal.emit()
                return True
            return False

        @Slot(str, str)
        def generate(self, text, voice):
            self.generate_calls.append([str(text), str(voice)])
            self.slot_hits.append("generate")

        @Slot(str, str)
        def generateStream(self, text, voice):
            # Same recording shape as generate() so existing assertions on
            # generate_calls keep working; slot_hits pins WHICH seam ran.
            self.generate_calls.append([str(text), str(voice)])
            self.slot_hits.append("generateStream")

        @Property(bool, notify=streamActiveChanged)
        def streamActive(self):
            return self._stream_active

        @streamActive.setter
        def streamActive(self, value):
            self._mutate("_stream_active", bool(value), self.streamActiveChanged)

        @Property(float, notify=streamLevelChanged)
        def streamLevel(self):
            return self._stream_level

        @streamLevel.setter
        def streamLevel(self, value):
            # Real controller clamps to 0..1 — mirror that so bindings see
            # the same numeric domain offscreen.
            clamped = max(0.0, min(float(value), 1.0))
            self._mutate("_stream_level", clamped, self.streamLevelChanged)

        @Property(bool, notify=audioAvailableChanged)
        def audioAvailable(self):
            return self._audio_available

        @audioAvailable.setter
        def audioAvailable(self, value):
            self._mutate("_audio_available", bool(value), self.audioAvailableChanged)

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

        @Slot(str, result=str)
        def importDocument(self, path):
            # ParagraphTab's import seam: path in, extracted text out. The
            # real controller will wrap core/importers.import_document
            # (documented gap — not implemented there yet); this fake just
            # records the call and hands back canned text.
            self.import_calls.append(str(path))
            return self.import_result

        @Property(bool, notify=consentGivenChanged)
        def consentGiven(self):
            return self._consent

        @Property(str, notify=previewPathChanged)
        def previewPath(self):
            return self._preview_path

        @previewPath.setter
        def previewPath(self, value):
            self._mutate("_preview_path", str(value), self.previewPathChanged)

        @Slot()
        def acknowledgeConsent(self):
            # Flip + NOTIFY like the real controller (which also persists to
            # cloning_consent.json; the fake only needs the QML-visible bit).
            self.consent_calls += 1
            self._consent = True
            self.consentGivenChanged.emit()

        @Slot(str, str, bool)
        def addVoice(self, name, clip_path, denoise):
            # Record the call, then mirror the real controller's ASYNC
            # completion: the voice lands in the cloned catalog group and
            # voicesChanged re-renders QML pickers/lists.
            self.add_voice_calls.append([str(name), str(clip_path), bool(denoise)])
            self._append_cloned(str(name))
            self.voicesChanged.emit()

        @Slot(str)
        def removeVoice(self, name):
            self.remove_voice_calls.append(str(name))
            for group in self._voices:
                if group["label"] == "Đã sao chép":
                    group["voices"] = [v for v in group["voices"] if v["id"] != str(name)]
            self.voicesChanged.emit()

        @Slot(str)
        def denoisePreview(self, clip_path):
            # The real controller completes asynchronously into previewPath;
            # clone_denoise drives that completion via the property setter.
            self.denoise_calls.append(str(clip_path))

        def _append_cloned(self, name):
            for group in self._voices:
                if group["label"] == "Đã sao chép":
                    group["voices"].append({"id": name, "label": name})
                    return
            self._voices.append({
                "label": "Đã sao chép",
                "voices": [{"id": name, "label": name}],
            })


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


    class BareController(QObject):
        \"\"\"No QML surface at all — the REAL controller while importDocument
        is still missing. Drives QML's typeof-guard (never crash, show the
        error label instead); undefined property reads are falsy in QML.\"\"\"

        pass


    controller = (
        BareController() if scenario == "para_import_guard" else FakeController()
    )
    playback = FakePlayback()
    bridge = ShellBridge(settings_dir=tmp, detector=lambda: "SMOKE NOTE")

    # stream_e2e / stream_cancel / para_stream_e2e / para_stream_cancel swap
    # the fake controller for the REAL AppController: TTSEngine over a
    # fake-at-the-SDK-layer (generator infer_stream) + a real InferenceWorker
    # thread + a REAL StreamPlaybackController whose audio seam is faked (its
    # own duck-typed sink contract, mirroring tests/unit/test_controller.py's
    # FakeSink — no QtMultimedia construction happens offscreen).
    if scenario in ("stream_e2e", "stream_cancel", "para_stream_e2e", "para_stream_cancel"):
        import time

        from vienetts_app.core.engine import TTSEngine
        from vienetts_app.ui.controller import AppController
        from vienetts_app.workers.inference_worker import InferenceWorker

        chunk_delay_ms = {
            "stream_e2e": 0,
            "stream_cancel": 150,
            "para_stream_e2e": 0,
            "para_stream_cancel": 150,
        }[scenario]

        class StreamVieneu:
            \"\"\"FakeVieneu subset with a GENERATOR infer_stream (spike §0).\"\"\"

            sample_rate = 48_000
            backend = "onnx"

            def __init__(self):
                self.infer_stream_calls = []

            def infer_stream(self, text, voice=None, temperature=None, **kw):
                self.infer_stream_calls.append(
                    {"text": str(text), "voice": voice, "temperature": temperature}
                )
                # Deterministic amplitudes → deterministic peak envelope.
                for amp in (0.05, 0.5, 0.9):
                    if chunk_delay_ms:
                        time.sleep(chunk_delay_ms / 1000)
                    yield np.full(2400, amp, dtype=np.float32)

            def close(self):
                pass

        class StreamSink:
            \"\"\"QAudioSink duck-type per StreamPlaybackController's contract.\"\"\"

            def __init__(self):
                self.state_name = "StoppedState"

            def start(self, io):
                self.state_name = "ActiveState"

            def stop(self):
                self.state_name = "StoppedState"

            def state(self):
                return self.state_name

        stream_sdk = StreamVieneu()
        # Capture the sink instance StreamPlaybackController builds so the
        # cancel scenarios can assert the AUDIO path hard-stopped too.
        sink_holder = {}

        def _capturing_sink(fmt):
            sink = StreamSink()
            sink_holder["sink"] = sink
            return sink

        controller = AppController(
            data_dir=tmp,
            engine_factory=lambda **kwargs: TTSEngine(factory=lambda **kw: stream_sdk),
            worker_factory=lambda engine: InferenceWorker(engine),
            stream_playback_factory=lambda: StreamPlaybackController(
                sink_factory=_capturing_sink,
                format_factory=lambda: object(),  # shape unused by the fake sink
            ),
        )
        # Keep quick exports inside tmp (settings default falls back to ~/Music).
        controller.outputDir = str(tmp)
    elif scenario == "para_import_oversize":
        # REAL AppController, REAL importer cap (FR-4.6b): importDocument is
        # engine-free, so a plain controller exercises the true
        # IMPORT_CHAR_LIMIT refusal instead of a stubbed seam.
        from vienetts_app.ui.controller import AppController

        controller = AppController(data_dir=tmp)

    app, engine = create_app(
        bridge_factory=lambda: bridge,
        controller_factory=lambda: controller,
        playback_factory=lambda: playback,
    )
    window = engine.rootObjects()[0]


    def find(name):
        return window.findChildren(QObject, name)[0]


    # Paragraph-tab lookups are scoped to its subtree: StackLayout
    # instantiates every tab, so shared objectNames exist twice in window.
    paragraph_tab = find("paragraphTab")


    def pfind(name):
        return paragraph_tab.findChildren(QObject, name)[0]


    # Text-tab lookups: scoped for symmetry with pfind/cfind so future tabs
    # may reuse shared names without silently re-pointing these tests.
    text_tab = find("textTab")


    def tfind(name):
        return text_tab.findChildren(QObject, name)[0]


    # Cloning-tab lookups, same scoping rule: shared objectNames (progressBar,
    # errorLabel) exist once per tab instantiated by the StackLayout.
    cloning_tab = find("cloningTab")


    def cfind(name):
        return cloning_tab.findChildren(QObject, name)[0]


    def item_walk(root):
        # All QQuickItems in the VISUAL tree. Repeater delegates are incubated
        # objects: they get a visual parent but NO QObject parent in the scene's
        # QObject tree, so findChildren(QObject, name) cannot see them at any
        # level — only a childItems() walk finds them.
        out, stack = [], [root]
        while stack:
            it = stack.pop()
            out.append(it)
            stack.extend(it.childItems())
        return out


    window_items = window.property("contentItem")  # ApplicationWindow root


    def ifind(name):
        # Visual-tree lookup for Repeater delegate items (e.g. clonedVoiceName).
        return [i for i in item_walk(window_items) if i.objectName() == name]


    def click_item(item):
        # Delegate wrappers come back QQuickItem-typed even for Controls;
        # click() lives on the runtime metaObject, so invoke it dynamically.
        return QMetaObject.invokeMethod(item, "click")


    def activate_item(item, index):
        # ComboBox.activate() is QML-side (not in the metaObject we see from
        # Python), but the underlying `activated` signal IS bound — emitting
        # it fires the QML onActivated handler exactly like user selection.
        item.activated.emit(int(index))


    def qjs_to_py(value):
        # QML `property var` reads come back as QJSValue wrappers.
        return value.toVariant() if hasattr(value, "toVariant") else value


    def wait_ms(ms):
        # Timer-driven toasts need the event loop to tick.
        for _ in range(ms // 50):
            QThread.msleep(50)
            app.processEvents()


    def wait_for(predicate, timeout_ms=10000, pump=25):
        # Cross-thread signals (worker → controller) are queued: pump the
        # loop until predicate() holds or the deadline passes.
        waited = 0
        while waited < timeout_ms:
            app.processEvents()
            if predicate():
                return True
            QThread.msleep(pump)
            waited += pump
        return False


    out = {"scenario": scenario}

    if scenario == "load":
        names = {o.objectName() for o in window.findChildren(QObject)}
        required = {
            "textTab", "textEditor", "voicePicker", "generateButton", "progressBar",
            "busyLabel", "cancelButton", "playButton", "exportButton",
            "quickExportButton", "errorLabel", "toastLabel", "waveformIndicator",
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
        out["slot_hits"] = controller.slot_hits

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
    elif scenario == "para_load":
        names = {o.objectName() for o in paragraph_tab.findChildren(QObject)}
        names.add(paragraph_tab.objectName())
        required = {
            "paragraphTab", "paragraphEditor", "importButton", "importDialog",
            "charCountLabel", "voicePicker", "generateButton", "progressBar",
            "cancelButton", "errorLabel", "playButton", "exportButton",
            # Streaming + notice surfaces (FR-4.4/FR-4.5/FR-4.6b): the shared
            # waveform and the banner hosting this tab's errorLabel.
            "waveformIndicator", "errorBanner",
        }
        out["missing"] = sorted(required - names)
        editor = pfind("paragraphEditor")
        out["editor_editable"] = not editor.property("readOnly")
        out["editor_placeholder"] = editor.property("placeholderText")
        out["import_button_text"] = pfind("importButton").property("text")
        dialog = pfind("importDialog")
        # fileMode (QQuickFileDialog::FileMode) has no PySide6 converter —
        # OpenFile is asserted indirectly: the accepted path is exercised
        # end-to-end in para_import.
        out["dialog_filters"] = dialog.property("nameFilters")
        out["char_count_text"] = pfind("charCountLabel").property("text")
        out["header_found"] = any(
            o.property("text") == "Đoạn văn / Tệp"
            for o in paragraph_tab.findChildren(QObject)
        )
        out["hint_mentions_extensions"] = any(
            ".pdf" in (o.property("text") or "")
            for o in paragraph_tab.findChildren(QObject)
        )
        picker = pfind("voicePicker")
        out["flat_ids"] = [row["id"] for row in qjs_to_py(picker.property("flatModel"))]
        out["selected_voice"] = picker.property("selectedVoice")
        out["current_index"] = picker.property("currentIndex")
        out["initial_generate_enabled"] = pfind("generateButton").property("enabled")
    elif scenario == "para_import":
        bridge.setCurrentTab("paragraph")
        app.processEvents()
        expected = "Xin chào\\nThế giới"
        doc = tmp / "doc.txt"
        doc.write_text(expected, encoding="utf-8")

        # URL conversion exactly as importDialog would supply it: QUrl in,
        # decoded local path out (toLocalPath is the same helper the dialog
        # onAccepted uses).
        url = QUrl.fromLocalFile(str(doc))
        local = QMetaObject.invokeMethod(
            paragraph_tab, "toLocalPath", Q_RETURN_ARG("QVariant"), Q_ARG("QVariant", url)
        )
        out["local_path"] = local
        out["local_path_matches"] = local == str(doc)

        # The dialog's onAccepted funnels into importPath — the tested seam
        # (QML function args are QVariant-typed in the metaobject).
        out["invoked"] = QMetaObject.invokeMethod(
            paragraph_tab, "importPath", Q_ARG("QVariant", local)
        )
        app.processEvents()

        editor = pfind("paragraphEditor")
        out["editor_text"] = editor.property("text")
        out["editor_matches"] = editor.property("text") == expected
        out["char_count_text"] = pfind("charCountLabel").property("text")
        out["char_count_expected"] = len(expected)
        out["import_calls"] = controller.import_calls
        out["generate_enabled_after"] = pfind("generateButton").property("enabled")
        out["error_hidden"] = not pfind("errorLabel").property("visible")
    elif scenario == "para_import_guard":
        # Missing-slot guard: a controller WITHOUT importDocument must never
        # crash the tab — the error label explains instead.
        bridge.setCurrentTab("paragraph")
        app.processEvents()
        out["invoked"] = QMetaObject.invokeMethod(
            paragraph_tab, "importPath", Q_ARG("QVariant", str(tmp / "missing.txt"))
        )
        app.processEvents()
        err = pfind("errorLabel")
        out["error_visible"] = err.property("visible")
        out["error_text"] = err.property("text")
        out["editor_unchanged"] = pfind("paragraphEditor").property("text") == ""
        out["no_import_recorded"] = getattr(controller, "import_calls", []) == []
    elif scenario == "para_generate":
        bridge.setCurrentTab("paragraph")
        app.processEvents()
        editor = pfind("paragraphEditor")
        generate = pfind("generateButton")
        progress = pfind("progressBar")
        cancel_btn = pfind("cancelButton")
        play = pfind("playButton")
        long_text = "Đoạn thứ nhất.\\n\\nĐoạn thứ hai."

        out["initial_generate_enabled"] = generate.property("enabled")
        editor.setProperty("text", long_text)
        app.processEvents()
        out["filled_generate_enabled"] = generate.property("enabled")

        generate.click()
        app.processEvents()
        out["generate_calls"] = controller.generate_calls
        out["slot_hits"] = controller.slot_hits
        out["char_count_text"] = pfind("charCountLabel").property("text")

        controller.busy = True
        app.processEvents()
        out["busy_generate_visible"] = generate.property("visible")
        out["busy_cancel_visible"] = cancel_btn.property("visible")
        out["busy_label_visible"] = pfind("paraBusyLabel").property("visible")
        out["busy_progress_visible"] = progress.property("visible")
        out["busy_progress_value"] = progress.property("value")
        out["busy_progress_indeterminate"] = progress.property("indeterminate")
        out["busy_play_enabled"] = play.property("enabled")
        out["busy_import_enabled"] = pfind("importButton").property("enabled")

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
        controller.lastExportPath = str(tmp / "para.wav")
        controller.busy = False
        app.processEvents()
        out["play_enabled_after"] = play.property("enabled")
        out["export_enabled_after"] = pfind("exportButton").property("enabled")
        out["progress_hidden_after"] = not progress.property("visible")
        out["cancel_hidden_after"] = not cancel_btn.property("visible")
        out["generate_visible_after"] = generate.property("visible")
    elif scenario == "para_cancel":
        bridge.setCurrentTab("paragraph")
        app.processEvents()
        cancel_btn = pfind("cancelButton")
        progress = pfind("progressBar")

        out["cancel_hidden_idle"] = not cancel_btn.property("visible")
        controller.busy = True
        app.processEvents()
        out["cancel_visible_busy"] = cancel_btn.property("visible")
        out["cancel_enabled_busy"] = cancel_btn.property("enabled")
        out["progress_visible_busy"] = progress.property("visible")
        out["generate_hidden_busy"] = not pfind("generateButton").property("visible")

        cancel_btn.click()
        app.processEvents()
        out["cancel_calls"] = controller.cancel_calls
    elif scenario == "clone_gate":
        bridge.setCurrentTab("cloning")
        app.processEvents()
        consent = cfind("consentPanel")
        clone = cfind("clonePanel")
        accept = cfind("consentAcceptButton")

        names = {o.objectName() for o in cloning_tab.findChildren(QObject)}
        names.add(cloning_tab.objectName())
        required = {
            "cloningTab", "consentPanel", "consentAcceptButton", "clonePanel",
            "clipPathLabel", "clipBrowseButton", "clipDialog", "denoiseCheck",
            "denoiseButton", "previewPlayButton", "voiceNameField", "cloneButton",
            "clonedVoiceList", "errorLabel", "progressBar",
        }
        out["missing"] = sorted(required - names)
        out["header_found"] = any(
            o.property("text") == "Sao chép giọng nói"
            for o in cloning_tab.findChildren(QObject)
        )
        # Consent gate: panel visible with the acknowledgment text, the
        # cloning panel hidden until the user accepts.
        out["consent_visible"] = consent.property("visible")
        out["clone_visible"] = clone.property("visible")
        # FR-4.7 legal-warning copy: consent of the person actually being
        # cloned + lawful-use responsibility (CloningTab "consentText").
        out["consent_text_found"] = any(
            "người được sao chép" in (o.property("text") or "")
            for o in cloning_tab.findChildren(QObject)
        )
        out["accept_text"] = accept.property("text")

        accept.click()
        app.processEvents()
        out["consent_calls"] = controller.consent_calls
        out["consent_visible_after"] = consent.property("visible")
        out["clone_visible_after"] = clone.property("visible")

        # Post-consent defaults of the main panel.
        out["clip_label_default"] = cfind("clipPathLabel").property("text")
        out["browse_text"] = cfind("clipBrowseButton").property("text")
        out["dialog_filters"] = cfind("clipDialog").property("nameFilters")
        out["guidance_found"] = any(
            "3–8 giây" in (o.property("text") or "")
            for o in cloning_tab.findChildren(QObject)
        )
        out["denoise_checked"] = cfind("denoiseCheck").property("checked")
        out["denoise_check_text"] = cfind("denoiseCheck").property("text")
        out["denoise_text"] = cfind("denoiseButton").property("text")
        out["preview_hidden_initially"] = not cfind("previewPlayButton").property("visible")
        out["name_placeholder"] = cfind("voiceNameField").property("placeholderText")
        out["clone_text"] = cfind("cloneButton").property("text")
    elif scenario == "clone_flow":
        bridge.setCurrentTab("cloning")
        cfind("consentAcceptButton").click()
        app.processEvents()

        name_field = cfind("voiceNameField")
        clone_btn = cfind("cloneButton")
        clip_label = cfind("clipPathLabel")
        clip_path = str(tmp / "ref.wav")

        out["clone_disabled_no_clip"] = not clone_btn.property("enabled")
        # The dialog's onAccepted entry point (native dialogs are unreliable
        # headless — same QMetaObject idiom as paragraphTab.importPath).
        out["invoked"] = QMetaObject.invokeMethod(
            cloning_tab, "selectClip", Q_ARG("QVariant", clip_path)
        )
        app.processEvents()
        out["clip_label"] = clip_label.property("text")
        out["clone_disabled_no_name"] = not clone_btn.property("enabled")

        name_field.setProperty("text", "Giọng đọc truyện")
        app.processEvents()
        out["clone_enabled"] = clone_btn.property("enabled")

        clone_btn.click()
        app.processEvents()
        out["add_voice_calls"] = controller.add_voice_calls
        out["row_names"] = [i.property("text") for i in ifind("clonedVoiceName")]
    elif scenario == "clone_denoise":
        bridge.setCurrentTab("cloning")
        cfind("consentAcceptButton").click()
        app.processEvents()

        denoise_btn = cfind("denoiseButton")
        preview_btn = cfind("previewPlayButton")
        clip_path = str(tmp / "ref.wav")

        out["denoise_disabled_no_clip"] = not denoise_btn.property("enabled")
        out["preview_hidden"] = not preview_btn.property("visible")

        QMetaObject.invokeMethod(cloning_tab, "selectClip", Q_ARG("QVariant", clip_path))
        app.processEvents()
        out["clip_label"] = cfind("clipPathLabel").property("text")
        out["denoise_enabled_with_clip"] = denoise_btn.property("enabled")

        denoise_btn.click()
        app.processEvents()
        out["denoise_calls"] = controller.denoise_calls

        # Async completion lands in previewPath → the play button appears.
        preview = str(tmp / "preview.wav")
        controller.previewPath = preview
        app.processEvents()
        out["preview_path"] = preview
        out["preview_visible"] = preview_btn.property("visible")
        out["preview_enabled"] = preview_btn.property("enabled")

        preview_btn.click()
        app.processEvents()
        out["playback_played"] = playback.played

        # Shared error contract mirrors the other tabs.
        controller.errorText = "Lỗi tạo giọng: tệp tham chiếu không hợp lệ"
        app.processEvents()
        out["error_visible"] = cfind("errorLabel").property("visible")
        out["error_text"] = cfind("errorLabel").property("text")
    elif scenario == "clone_remove":
        bridge.setCurrentTab("cloning")
        cfind("consentAcceptButton").click()
        app.processEvents()


        def row_names():
            return [i.property("text") for i in ifind("clonedVoiceName")]


        remove_buttons = ifind("cloneRemoveButton")
        out["rows_before"] = row_names()
        out["remove_button_text"] = remove_buttons[0].property("text")

        click_item(remove_buttons[0])
        app.processEvents()
        out["remove_calls"] = controller.remove_voice_calls
        out["rows_after"] = row_names()
    elif scenario == "clone_disabled":
        bridge.setCurrentTab("cloning")
        cfind("consentAcceptButton").click()
        app.processEvents()

        denoise_btn = cfind("denoiseButton")
        clone_btn = cfind("cloneButton")
        name_field = cfind("voiceNameField")

        out["denoise_disabled_no_clip"] = not denoise_btn.property("enabled")
        out["clone_disabled_no_clip"] = not clone_btn.property("enabled")

        QMetaObject.invokeMethod(
            cloning_tab, "selectClip", Q_ARG("QVariant", str(tmp / "ref.wav"))
        )
        app.processEvents()
        out["denoise_enabled_with_clip"] = denoise_btn.property("enabled")
        out["clone_disabled_empty_name"] = not clone_btn.property("enabled")

        name_field.setProperty("text", "   ")
        app.processEvents()
        out["clone_disabled_whitespace_name"] = not clone_btn.property("enabled")

        name_field.setProperty("text", "Giọng đọc truyện")
        app.processEvents()
        out["clone_enabled"] = clone_btn.property("enabled")

        controller.busy = True
        app.processEvents()
        out["clone_disabled_busy"] = not clone_btn.property("enabled")
        out["denoise_disabled_busy"] = not denoise_btn.property("enabled")
        out["busy_label_visible"] = cfind("cloneBusyLabel").property("visible")
        progress = cfind("progressBar")
        out["progress_visible_busy"] = progress.property("visible")
        out["progress_indeterminate_busy"] = progress.property("indeterminate")

    elif scenario == "settings_load":
        settings_tab = find("settingsTab")
        present = {o.objectName() for o in settings_tab.findChildren(QObject)}
        required = {
            "backendCombo", "detectedEngineLabel", "precisionCombo",
            "needsRestartBanner", "defaultVoiceCombo", "outputDirLabel",
            "outputDirBrowseButton", "temperatureSpin", "themeCombo", "errorLabel",
        }
        out["all_present"] = required <= present
        out["detected_note"] = settings_tab.findChildren(
            QObject, "detectedEngineLabel"
        )[0].property("text")
        backend_combo = settings_tab.findChildren(QObject, "backendCombo")[0]
        out["backend_index"] = backend_combo.property("currentIndex")
        banner = settings_tab.findChildren(QObject, "needsRestartBanner")[0]
        out["needs_restart_visible"] = banner.property("visible")
    elif scenario == "settings_engine":
        bridge.setCurrentTab("settings")
        settings_tab = find("settingsTab")
        backend_combo = settings_tab.findChildren(QObject, "backendCombo")[0]
        precision_combo = settings_tab.findChildren(QObject, "precisionCombo")[0]
        banner = settings_tab.findChildren(QObject, "needsRestartBanner")[0]

        out["banner_hidden_no_engine"] = not banner.property("visible")
        # activate() is Q_INVOKABLE on ComboBox (same class of dynamic call
        # as Button.click()).
        activate_item(backend_combo, 2)  # torch
        app.processEvents()
        out["backend_after"] = controller.backend
        out["banner_after_no_engine"] = not banner.property("visible")

        # Simulate a running engine: engine-affecting writes now flag restart.
        controller.engine_initialized = True
        activate_item(precision_combo, 1)  # fp32
        app.processEvents()
        out["precision_after"] = controller.precision
        out["banner_visible_with_engine"] = banner.property("visible")
    elif scenario == "settings_theme":
        bridge.setCurrentTab("settings")
        settings_tab = find("settingsTab")
        theme_combo = settings_tab.findChildren(QObject, "themeCombo")[0]
        out["pref_before"] = bridge.themePreference
        activate_item(theme_combo, 1)  # light
        app.processEvents()
        out["bridge_pref_after"] = bridge.themePreference
        out["controller_theme_after"] = controller.theme
        out["effective_after"] = bridge.effectiveTheme
    elif scenario == "settings_output":
        bridge.setCurrentTab("settings")
        settings_tab = find("settingsTab")
        label = settings_tab.findChildren(QObject, "outputDirLabel")[0]
        out["label_before"] = label.property("text")
        invoked = QMetaObject.invokeMethod(
            settings_tab, "setOutputDir", Q_ARG("QVariant", str(tmp / "exports"))
        )
        app.processEvents()
        out["invoked"] = invoked
        out["output_dir_after"] = controller.outputDir
        out["label_after"] = label.property("text")
    elif scenario == "settings_temperature":
        bridge.setCurrentTab("settings")
        settings_tab = find("settingsTab")
        spin = settings_tab.findChildren(QObject, "temperatureSpin")[0]
        out["temp_before"] = controller.temperature
        spin.setProperty("value", 120)  # ×100 → 1.20
        app.processEvents()
        out["temp_after"] = controller.temperature
        # SpinBox display text (the `text` property is write-only from C++).
        out["spin_text"] = spin.property("displayText")
    elif scenario == "settings_default_voice":
        bridge.setCurrentTab("settings")
        settings_tab = find("settingsTab")
        voice_combo = settings_tab.findChildren(QObject, "defaultVoiceCombo")[0]
        out["default_before"] = controller.defaultVoice
        # Flat model: header(Bắc), adam_north, eva_north, header(Đã sao chép),
        # my_clone → eva_north is index 2.
        activate_item(voice_combo, 2)
        app.processEvents()
        out["default_after"] = controller.defaultVoice
    elif scenario == "stream_bindings":
        # WaveformIndicator binding contract (FR-4.5): host flips controller
        # properties programmatically; QML picks them up via NOTIFY.
        wv = tfind("waveformIndicator")

        out["waveform_hidden_initially"] = not wv.property("visible")
        out["component_inactive_initially"] = not wv.property("active")
        out["level_initial"] = float(wv.property("level"))
        out["history_initial"] = int(wv.property("historyCount"))

        # Session live → host visibility flips AND the component mirrors
        # `active`; level changes roll into the bounded history.
        controller.streamActive = True
        app.processEvents()
        out["waveform_visible_during"] = bool(wv.property("visible"))
        out["component_active_during"] = bool(wv.property("active"))

        for value in (0.75, 0.4, 0.85):
            controller.streamLevel = value
            app.processEvents()
        out["level_bound_latest"] = float(wv.property("level"))
        out["history_after_pushes"] = int(wv.property("historyCount"))
        # Bar window stays capped at the declared barCount property.
        out["bar_count_declared"] = int(wv.property("barCount"))

        # Session end: history cleared back to baseline, hidden again.
        controller.streamActive = False
        app.processEvents()
        out["history_cleared_on_end"] = int(wv.property("historyCount"))
        out["waveform_hidden_after"] = not wv.property("visible")
        out["component_active_after"] = bool(wv.property("active"))

        # The Generate button now routes through the STREAMING slot (FR-4.3):
        # recorded like generate(), but slot_hits pins WHICH seam ran — and
        # the legacy batch seam must stay untouched by this tab's flow.
        editor = tfind("textEditor")
        editor.setProperty("text", "Xin chào thế giới")
        app.processEvents()
        tfind("generateButton").click()
        app.processEvents()
        out["generate_calls"] = controller.generate_calls
        out["slot_hits"] = controller.slot_hits
    elif scenario == "stream_e2e":
        # Real AppController + QML shell + fake-at-the-SDK-layer: full cycle
        # click → generateStream → worker thread → chunk_ready → ring buffer
        # → levelReady → streamLevel → waveform.
        wv = tfind("waveformIndicator")
        session = {"seen_active": False, "wave_visible": False, "levels": []}

        def _on_stream_changed():
            if controller.streamActive:
                session["seen_active"] = True
                if bool(wv.property("visible")):
                    session["wave_visible"] = True

        controller.streamActiveChanged.connect(_on_stream_changed)
        controller.streamLevelChanged.connect(
            lambda: session["levels"].append(float(controller.streamLevel))
        )

        tfind("textEditor").setProperty("text", "Xin chào thế giới")
        app.processEvents()
        find("generateButton").click()
        done = wait_for(lambda: controller.hasAudio and not controller.busy)
        app.processEvents()

        out["completed"] = done
        out["infer_stream_calls"] = stream_sdk.infer_stream_calls
        out["saw_session_live"] = session["seen_active"]
        out["waveform_visible_during_session"] = session["wave_visible"]
        out["peak_level_seen"] = max(session["levels"]) if session["levels"] else 0.0
        out["done_stream_inactive"] = not controller.streamActive
        out["done_waveform_hidden"] = not bool(wv.property("visible"))
        out["progress_final"] = float(controller.progress)
        # Retained audio still feeds replay/export after done (AC-3).
        out["export_ok"] = controller.exportWav("")
        out["last_export_path"] = controller.lastExportPath
    elif scenario == "stream_cancel":
        # Cancel mid-stream (FR-4.2): stops synthesis at a chunk boundary AND
        # the sink immediately, resets busy/streamActive silently with only
        # the "Đã hủy" toast, and no audio is retained.
        wv = tfind("waveformIndicator")
        session = {"seen_active": False}

        def _on_stream_changed():
            if controller.streamActive:
                session["seen_active"] = True

        controller.streamActiveChanged.connect(_on_stream_changed)

        tfind("textEditor").setProperty("text", "Xin chào thế giới")
        app.processEvents()
        find("generateButton").click()
        # Wait until the worker ACTUALLY began generating before cancelling:
        # cancel() drains the queue, so cancelling before pickup would drop
        # the request silently and leave busy stuck True forever.
        wait_for(lambda: len(stream_sdk.infer_stream_calls) == 1)
        cancel_btn = find("cancelButton")
        out["cancel_visible_mid_stream"] = bool(cancel_btn.property("visible"))
        cancel_btn.click()
        settled = wait_for(lambda: not controller.busy and not controller.streamActive)
        app.processEvents()

        out["settled_after_cancel"] = settled
        out["saw_session_live"] = session["seen_active"]
        out["no_error_banner"] = controller.errorText == ""
        out["toast_visible"] = bool(find("toastLabel").property("visible"))
        out["toast_text"] = find("toastLabel").property("text")
        out["no_audio_retained"] = not controller.hasAudio
        out["waveform_hidden_after_cancel"] = not bool(wv.property("visible"))
    elif scenario == "para_stream_bindings":
        # ParagraphTab's WaveformIndicator binding contract (FR-4.4/FR-4.5):
        # same programmatic flip as stream_bindings, scoped to this tab's
        # subtree, plus the submit-seam pin.
        #
        # ACTIVATE THIS TAB FIRST: while a StackLayout sibling owns
        # currentIndex, Qt defers `visible` binding updates inside the hidden
        # subtree (offscreen probe evidence) — `active`/level history still
        # update, so only visibility reads need the active-tab state.
        bridge.setCurrentTab("paragraph")
        app.processEvents()
        wv = pfind("waveformIndicator")

        out["waveform_hidden_initially"] = not wv.property("visible")
        out["component_inactive_initially"] = not wv.property("active")
        out["history_initial"] = int(wv.property("historyCount"))

        controller.streamActive = True
        app.processEvents()
        out["waveform_visible_during"] = bool(wv.property("visible"))
        out["component_active_during"] = bool(wv.property("active"))
        controller.streamLevel = 0.7
        app.processEvents()
        out["level_bound_latest"] = float(wv.property("level"))
        out["history_after_push"] = int(wv.property("historyCount"))

        controller.streamActive = False
        app.processEvents()
        out["history_cleared_on_end"] = int(wv.property("historyCount"))
        out["waveform_hidden_after"] = not wv.property("visible")

        long_text = "Đoạn thứ nhất.\\n\\nĐoạn thứ hai."
        pfind("paragraphEditor").setProperty("text", long_text)
        app.processEvents()
        pfind("generateButton").click()
        app.processEvents()
        out["generate_calls"] = controller.generate_calls
        out["slot_hits"] = controller.slot_hits
    elif scenario in ("para_stream_e2e", "para_stream_cancel"):
        # Real-controller paragraph streaming (FR-4.4): long text + the tab's
        # own editor/button wiring through generateStream; same session
        # recorder pattern as the text-tab e2e, with pfind-scoped lookups.
        bridge.setCurrentTab("paragraph")
        wv = pfind("waveformIndicator")
        session = {"seen_active": False, "wave_visible": False, "levels": []}

        def _on_para_stream_changed():
            if controller.streamActive:
                session["seen_active"] = True
                if bool(wv.property("visible")):
                    session["wave_visible"] = True

        controller.streamActiveChanged.connect(_on_para_stream_changed)
        controller.streamLevelChanged.connect(
            lambda: session["levels"].append(float(controller.streamLevel))
        )

        doc_text = "Đoạn thứ nhất.\\n\\nĐoạn thứ hai."
        pfind("paragraphEditor").setProperty("text", doc_text)
        app.processEvents()
        pfind("generateButton").click()

        if scenario == "para_stream_e2e":
            done = wait_for(lambda: controller.hasAudio and not controller.busy)
            app.processEvents()

            out["completed"] = done
            seg_texts = [str(call["text"]) for call in stream_sdk.infer_stream_calls]
            out["doc_text_sent"] = seg_texts[0] if seg_texts else ""
            out["segment_count"] = len(seg_texts)
            out["saw_session_live"] = session["seen_active"]
            out["waveform_visible_during_session"] = session["wave_visible"]
            out["peak_level_seen"] = max(session["levels"]) if session["levels"] else 0.0
            out["done_stream_inactive"] = not controller.streamActive
            out["done_waveform_hidden"] = not bool(wv.property("visible"))
            out["progress_final"] = float(controller.progress)
            # Retained audio keeps replay/export working post-done (hasAudio
            # gates both affordances in this tab).
            out["has_audio_after"] = controller.hasAudio
            out["export_ok"] = controller.exportWav("")
            out["last_export_path"] = controller.lastExportPath
        else:
            # Wait until the worker ACTUALLY began generating: cancel() drains
            # the queue, so cancelling before pickup would drop the request
            # silently and leave busy stuck True forever.
            wait_for(lambda: len(stream_sdk.infer_stream_calls) == 1)
            cancel_btn = pfind("cancelButton")
            out["cancel_visible_mid_stream"] = bool(cancel_btn.property("visible"))
            cancel_btn.click()
            settled = wait_for(lambda: not controller.busy and not controller.streamActive)
            app.processEvents()

            # Cancel hits BOTH paths (AC-2): synthesis never produced done-audio
            # AND the audio sink hard-stopped back to StoppedState.
            out["settled_after_cancel"] = settled
            out["segments_started"] = len(stream_sdk.infer_stream_calls)
            out["no_audio_retained"] = not controller.hasAudio
            out["sink_state_after_cancel"] = (
                sink_holder["sink"].state() if "sink" in sink_holder else "?"
            )
            out["no_error_banner"] = controller.errorText == ""
            out["waveform_hidden_after_cancel"] = not bool(wv.property("visible"))
    elif scenario == "para_import_oversize":
        # FR-4.6b surface: a genuinely oversized .txt through the REAL
        # AppController.importDocument → errorText carries the IMPORT_CHAR_LIMIT
        # refusal → importPath echoes it → errorBanner shows it verbatim.
        bridge.setCurrentTab("paragraph")
        app.processEvents()
        big = tmp / "big.txt"
        # 19-char unit × 11k = 209k > IMPORT_CHAR_LIMIT (200k).
        content = "Xin chào thế giới. " * 11_000
        big.write_text(content, encoding="utf-8")

        invoked = QMetaObject.invokeMethod(
            paragraph_tab, "importPath", Q_ARG("QVariant", str(big))
        )
        app.processEvents()

        err_label = pfind("errorLabel")
        label_text = str(err_label.property("text"))
        out["invoked"] = bool(invoked)
        out["banner_visible"] = bool(pfind("errorBanner").property("visible"))
        out["label_visible"] = bool(err_label.property("visible"))
        out["error_text"] = label_text
        out["mentions_limit"] = "200,000" in label_text and "too large" in label_text
        out["matches_controller_error"] = label_text == str(controller.errorText)
        out["editor_empty"] = pfind("paragraphEditor").property("text") == ""

    if getattr(controller, "_worker", None) is not None:  # noqa: SLF001 - teardown
        # Real-controller scenarios own a worker thread; stop it cleanly so
        # subprocess teardown never races an in-flight inference.
        controller.shutdown()

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
        # Streaming scenarios start a real worker thread; bound the wait.
        timeout=120,
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
        # Generate is wired through the STREAMING slot (FR-4.3): same
        # (text, voice) payload the batch seam used to receive, but
        # slot_hits proves which submit path ran.
        assert result["initial_generate_enabled"] is False
        assert result["filled_generate_enabled"] is True
        assert result["generate_calls"] == [["Xin chào thế giới", "adam_north"]]
        assert result["slot_hits"] == ["generateStream"]
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


class TestParagraphTabSmoke:
    def test_para_load_objectnames_and_import_ui(self, tmp_path) -> None:
        result = run_driver(tmp_path, "para_load")
        # ⚑ contract: every named element exists under the paragraphTab subtree.
        assert result["missing"] == []
        assert result["editor_editable"] is True
        assert result["import_button_text"] == "Nhập tệp…"
        # Import dialog: filters mirror SUPPORTED_EXTENSIONS (.txt .md .docx
        # .pdf). fileMode (OpenFile) has no PySide6 enum converter — its
        # accepted path is proven end-to-end by test_para_import_via_import_path.
        assert result["dialog_filters"] == ["Văn bản (*.txt *.md *.docx *.pdf)"]
        assert result["header_found"] is True
        assert result["hint_mentions_extensions"] is True
        # Empty editor → "0 ký tự" live counter, generate disabled.
        assert result["char_count_text"] == "0 ký tự"
        assert result["initial_generate_enabled"] is False
        # Same grouped picker contract as TextTab (headers non-selectable).
        assert result["flat_ids"] == ["", "adam_north", "eva_north", "", "my_clone"]
        assert result["selected_voice"] == "adam_north"
        assert result["current_index"] == 1

    def test_para_import_via_import_path(self, tmp_path) -> None:
        result = run_driver(tmp_path, "para_import")
        expected = "Xin chào\nThế giới"
        # QUrl → decoded local path, as the dialog's onAccepted supplies it.
        assert result["local_path_matches"] is True
        # importPath (the onAccepted entry point) ran without opening the
        # native dialog.
        assert result["invoked"] is True
        assert result["import_calls"] == [result["local_path"]]
        assert result["editor_matches"] is True
        # Live char counter reflects the imported text (computed, not hardcoded).
        assert result["char_count_text"] == f"{len(expected)} ký tự"
        assert result["char_count_expected"] == len(expected)
        assert result["generate_enabled_after"] is True
        assert result["error_hidden"] is True

    def test_para_import_guard_without_controller_slot(self, tmp_path) -> None:
        result = run_driver(tmp_path, "para_import_guard")
        # Missing importDocument on the controller must not crash the tab:
        # the error label explains and the editor stays untouched.
        assert result["invoked"] is True
        assert result["error_visible"] is True
        assert result["error_text"] == "Không thể nhập tệp"
        assert result["editor_unchanged"] is True
        assert result["no_import_recorded"] is True

    def test_para_generate_flow(self, tmp_path) -> None:
        result = run_driver(tmp_path, "para_generate")
        long_text = "Đoạn thứ nhất.\n\nĐoạn thứ hai."
        assert result["initial_generate_enabled"] is False
        assert result["filled_generate_enabled"] is True
        assert result["generate_calls"] == [[long_text, "adam_north"]]
        # ParagraphTab streams through the SAME seam as the Text tab now
        # (FR-4.4); the shared fake records which submit path ran.
        assert result["slot_hits"] == ["generateStream"]
        assert result["char_count_text"] == f"{len(long_text)} ký tự"
        # Busy state: progress (indeterminate at 0) + cancel, generate hidden.
        assert result["busy_generate_visible"] is False
        assert result["busy_label_visible"] is True
        assert result["busy_progress_visible"] is True
        assert result["busy_progress_value"] == 0
        assert result["busy_progress_indeterminate"] is True
        assert result["busy_play_enabled"] is False
        assert result["busy_import_enabled"] is False
        assert result["cancel_calls"] == 1
        # Progress 0 → 0.5 → 1 with indeterminate clearing.
        assert result["progress_mid"] == 0.5
        assert result["indeterminate_mid"] is False
        assert result["progress_full"] == 1.0
        # Done: play/export enabled (after an export path exists), UI reverts.
        assert result["play_enabled_after"] is True
        assert result["export_enabled_after"] is True
        assert result["progress_hidden_after"] is True
        assert result["cancel_hidden_after"] is True
        assert result["generate_visible_after"] is True

    def test_para_cancel_visible_only_when_busy(self, tmp_path) -> None:
        result = run_driver(tmp_path, "para_cancel")
        assert result["cancel_hidden_idle"] is True
        assert result["cancel_visible_busy"] is True
        assert result["cancel_enabled_busy"] is True
        assert result["progress_visible_busy"] is True
        assert result["generate_hidden_busy"] is True
        assert result["cancel_calls"] == 1


class TestCloningTabSmoke:
    def test_clone_gate_consent_flow(self, tmp_path) -> None:
        result = run_driver(tmp_path, "clone_gate")
        # ⚑ contract: every named element exists under the cloningTab subtree.
        assert result["missing"] == []
        assert result["header_found"] is True
        # Consent gate: the consent panel shows first with the acknowledgment
        # text; the cloning panel stays hidden until acknowledgeConsent() is
        # recorded and flips consentGiven.
        assert result["consent_visible"] is True
        assert result["clone_visible"] is False
        assert result["consent_text_found"] is True
        assert result["accept_text"] == "Tôi đồng ý"
        assert result["consent_calls"] == 1
        assert result["consent_visible_after"] is False
        assert result["clone_visible_after"] is True
        # Post-consent defaults: empty clip label, audio filters, 3–8 s
        # guidance, denoise checkbox on, name placeholder, hidden preview.
        assert result["clip_label_default"] == "Chưa chọn tệp"
        assert result["browse_text"] == "Chọn tệp…"
        assert result["dialog_filters"] == ["Âm thanh (*.wav *.mp3)"]
        assert result["guidance_found"] is True
        assert result["denoise_checked"] is True
        assert result["denoise_check_text"] == "Khử nhiễu trước khi sao chép"
        assert result["denoise_text"] == "Nghe bản khử nhiễu"
        assert result["preview_hidden_initially"] is True
        assert result["name_placeholder"] == "Tên giọng mới (vd: Giọng đọc truyện)"
        assert result["clone_text"] == "Tạo giọng nói"

    def test_clone_flow_select_clip_and_enroll(self, tmp_path) -> None:
        result = run_driver(tmp_path, "clone_flow")
        # selectClip (the dialog's onAccepted seam) stores the clip; the
        # label mirrors it and clone stays disabled until BOTH clip and name.
        assert result["invoked"] is True
        assert result["clone_disabled_no_clip"] is True
        assert result["clip_label"].endswith("ref.wav")
        assert result["clone_disabled_no_name"] is True
        assert result["clone_enabled"] is True
        # Clone button wires addVoice(trimmed name, selected clip, denoise).
        assert result["add_voice_calls"] == [["Giọng đọc truyện", result["clip_label"], True]]
        # voicesChanged re-render: existing + newly enrolled cloned rows.
        assert sorted(result["row_names"]) == ["Giọng đọc truyện", "my_clone"]

    def test_clone_denoise_preview_and_play(self, tmp_path) -> None:
        result = run_driver(tmp_path, "clone_denoise")
        assert result["denoise_disabled_no_clip"] is True
        assert result["preview_hidden"] is True
        assert result["denoise_enabled_with_clip"] is True
        assert result["denoise_calls"] == [result["clip_label"]]
        # Async completion lands in previewPath → the play button appears and
        # routes through the global playback context property.
        assert result["preview_visible"] is True
        assert result["preview_enabled"] is True
        assert result["playback_played"] == [result["preview_path"]]
        # Shared error contract mirrors the other tabs.
        assert result["error_visible"] is True
        assert result["error_text"] == "Lỗi tạo giọng: tệp tham chiếu không hợp lệ"

    def test_clone_remove_voice(self, tmp_path) -> None:
        result = run_driver(tmp_path, "clone_remove")
        # The cloned catalog group ("my_clone" from the seed catalog) renders
        # a row whose Xóa button wires controller.removeVoice(name).
        assert result["rows_before"] == ["my_clone"]
        assert result["remove_button_text"] == "Xóa"
        assert result["remove_calls"] == ["my_clone"]
        assert result["rows_after"] == []

    def test_clone_disabled_states(self, tmp_path) -> None:
        result = run_driver(tmp_path, "clone_disabled")
        assert result["denoise_disabled_no_clip"] is True
        assert result["clone_disabled_no_clip"] is True
        assert result["denoise_enabled_with_clip"] is True
        # Clip set but empty (or whitespace-only) name → clone still disabled.
        assert result["clone_disabled_empty_name"] is True
        assert result["clone_disabled_whitespace_name"] is True
        assert result["clone_enabled"] is True
        # Busy locks every action (shared busy/progress contract).
        assert result["clone_disabled_busy"] is True
        assert result["denoise_disabled_busy"] is True
        assert result["busy_label_visible"] is True
        assert result["progress_visible_busy"] is True
        assert result["progress_indeterminate_busy"] is True


class TestSettingsTabSmoke:
    def test_settings_controls_present(self, tmp_path) -> None:
        result = run_driver(tmp_path, "settings_load")
        assert result["all_present"] is True
        # Detector readout (model-free) repeats on the settings tab (FR-3.5).
        assert result["detected_note"] == "SMOKE NOTE"
        # Default backend "auto" → index 0; no stale restart banner at load.
        assert result["backend_index"] == 0
        assert result["needs_restart_visible"] is False

    def test_backend_and_precision_apply_on_next_init(self, tmp_path) -> None:
        result = run_driver(tmp_path, "settings_engine")
        assert result["backend_after"] == "torch"
        # With no engine initialized the change applies at (re)start — no banner.
        assert result["banner_after_no_engine"] is True
        # Once an engine is live, engine-affecting writes flag needsRestart
        # instead of mutating the running engine (FR-3.5, AC-4).
        assert result["precision_after"] == "fp32"
        assert result["banner_visible_with_engine"] is True

    def test_theme_writes_bridge_and_controller(self, tmp_path) -> None:
        result = run_driver(tmp_path, "settings_theme")
        assert result["pref_before"] == "system"
        assert result["bridge_pref_after"] == "light"
        # The controller mirrors the same settings.json field (its seam).
        assert result["controller_theme_after"] == "light"
        # Live switch: the bridge re-resolves the effective theme.
        assert result["effective_after"] == "light"

    def test_output_dir_setting(self, tmp_path) -> None:
        result = run_driver(tmp_path, "settings_output")
        assert result["invoked"] is True
        assert result["output_dir_after"].endswith("exports")
        assert result["label_after"].endswith("exports")

    def test_temperature_spin_writes_controller(self, tmp_path) -> None:
        result = run_driver(tmp_path, "settings_temperature")
        assert result["temp_before"] == 0.8
        assert abs(result["temp_after"] - 1.2) < 1e-9
        assert result["spin_text"] == "1.20"

    def test_default_voice_combo(self, tmp_path) -> None:
        result = run_driver(tmp_path, "settings_default_voice")
        assert result["default_before"] == "adam_north"
        assert result["default_after"] == "eva_north"


class TestWaveformIndicatorSmoke:
    """FR-4.5 groundwork: TextTab hosts the shared WaveformIndicator.

    Binding-level scenarios (fake controller): flipping streamActive /
    streamLevel programmatically must re-render the indicator — the tested
    surface is QML state (.property reads), never pixels.
    """

    def test_stream_bindings_and_visibility_cycle(self, tmp_path) -> None:
        result = run_driver(tmp_path, "stream_bindings")
        # Idle: hidden, inactive, empty rolling history at level 0.
        assert result["waveform_hidden_initially"] is True
        assert result["component_inactive_initially"] is True
        assert result["level_initial"] == 0.0
        assert result["history_initial"] == 0
        assert result["bar_count_declared"] > 0
        # Session live → visible + active; levels roll into bounded history.
        assert result["waveform_visible_during"] is True
        assert result["component_active_during"] is True
        assert result["level_bound_latest"] == 0.85  # last NOTIFY wins binding
        assert result["history_after_pushes"] == 3  # one bar per level change
        # Session end → history cleared to the flat baseline, hidden again.
        assert result["history_cleared_on_end"] == 0
        assert result["waveform_hidden_after"] is True
        assert result["component_active_after"] is False
        # Generate routes through generateStream (FR-4.3), not the batch seam.
        assert result["generate_calls"] == [["Xin chào thế giới", "adam_north"]]
        assert result["slot_hits"] == ["generateStream"]


class TestTextStreamE2E:
    """Full-stack streaming through the REAL controller (fake SDK + fake sink).

    The fake sits BELOW the controller (generator ``infer_stream`` per spike
    §0) and at StreamPlaybackController's audio seam (duck-typed sink), so
    every production layer between QML click and QML envelope runs real code:
    AppController → InferenceWorker thread → chunk_ready → ring buffer feed →
    levelReady peak envelope → controller.streamLevel/Active → WaveformIndicator.
    """

    def test_stream_cycle_streams_then_restores_export_path(self, tmp_path) -> None:
        result = run_driver(tmp_path, "stream_e2e")
        assert result["completed"] is True
        # Exactly one segmented infer_stream dispatch carrying the editor text.
        assert len(result["infer_stream_calls"]) >= 1
        assert result["infer_stream_calls"][0]["text"] == "Xin chào thế giới"
        # streamActive toggled true→false with the waveform live during.
        assert result["saw_session_live"] is True
        assert result["waveform_visible_during_session"] is True
        assert result["peak_level_seen"] > 0.5  # fake chunks peak at 0.9
        assert result["done_stream_inactive"] is True
        assert result["done_waveform_hidden"] is True
        assert result["progress_final"] == 1.0
        # Retained audio keeps the export affordance working after done.
        assert result["export_ok"] is True
        assert result["last_export_path"].endswith(".wav")

    def test_cancel_mid_stream_silently_resets_and_toasts(self, tmp_path) -> None:
        result = run_driver(tmp_path, "stream_cancel")
        # The session ran before cancel landed mid-stream.
        assert result["cancel_visible_mid_stream"] is True
        assert result["saw_session_live"] is True
        # Cancel halts synthesis AND playback promptly (AC-2).
        assert result["settled_after_cancel"] is True
        assert result["no_audio_retained"] is True
        assert result["waveform_hidden_after_cancel"] is True
        # Silent reset: toast, not an error banner.
        assert result["no_error_banner"] is True
        assert result["toast_visible"] is True
        assert result["toast_text"] == "Đã hủy"


class TestParagraphStreamSmoke:
    """ParagraphTab streaming bindings (fake controller) + oversize notice.

    FR-4.4: the Paragraph/File tab submits through generateStream exactly
    like the Text tab and hosts the shared WaveformIndicator; FR-4.6b: an
    oversized import surfaces the IMPORT_CHAR_LIMIT refusal in-tab.
    """

    def test_para_stream_bindings_and_submit_seam(self, tmp_path) -> None:
        result = run_driver(tmp_path, "para_stream_bindings")
        # Idle: hidden, inactive, empty rolling history.
        assert result["waveform_hidden_initially"] is True
        assert result["component_inactive_initially"] is True
        assert result["history_initial"] == 0
        # Session live → visible + active; the level rolls into history.
        assert result["waveform_visible_during"] is True
        assert result["component_active_during"] is True
        assert result["level_bound_latest"] == 0.7
        assert result["history_after_push"] == 1
        # Session end → baseline restored, hidden again.
        assert result["history_cleared_on_end"] == 0
        assert result["waveform_hidden_after"] is True
        # Generate routes through the STREAMING slot from THIS tab too.
        long_text = "Đoạn thứ nhất.\n\nĐoạn thứ hai."
        assert result["generate_calls"] == [[long_text, "adam_north"]]
        assert result["slot_hits"] == ["generateStream"]

    def test_para_import_oversize_shows_limit_notice(self, tmp_path) -> None:
        result = run_driver(tmp_path, "para_import_oversize")
        assert result["invoked"] is True
        # The banner notice is visible and carries the controller's exact
        # limit message (refuse + split-the-document guidance), not a generic
        # fallback.
        assert result["banner_visible"] is True
        assert result["label_visible"] is True
        assert result["mentions_limit"] is True
        assert result["matches_controller_error"] is True
        assert "Split the document" in result["error_text"]
        # Refusal, never truncation: the editor stays untouched.
        assert result["editor_empty"] is True


class TestParagraphStreamE2E:
    """Full-stack paragraph streaming through the REAL controller.

    QML click → controller.generateStream → InferenceWorker thread →
    chunk_ready → ring buffer feed → levelReady peak envelope →
    controller.streamLevel/streamActive → this tab's WaveformIndicator.
    """

    def test_para_stream_cycle_streams_then_restores_export_path(self, tmp_path) -> None:
        result = run_driver(tmp_path, "para_stream_e2e")
        assert result["completed"] is True
        # The editor text reached the SDK seam through the stream dispatch.
        # The chunked dispatcher packs sentence segments whitespace-normalized
        # (\n\n → space), so assert CONTENT, not the raw newline shape.
        assert "Đoạn thứ nhất." in result["doc_text_sent"]
        assert "Đoạn thứ hai." in result["doc_text_sent"]
        assert result["segment_count"] >= 1
        # streamActive toggled true→false with the waveform live during.
        assert result["saw_session_live"] is True
        assert result["waveform_visible_during_session"] is True
        assert result["peak_level_seen"] > 0.5  # fake chunks peak at 0.9
        assert result["done_stream_inactive"] is True
        assert result["done_waveform_hidden"] is True
        # Segment-counted progress completed, retained audio re-enables the
        # replay/export affordances (FR-4.4 done-path).
        assert result["progress_final"] == 1.0
        assert result["has_audio_after"] is True
        assert result["export_ok"] is True
        assert result["last_export_path"].endswith(".wav")

    def test_para_cancel_mid_stream_stops_synthesis_and_sink(self, tmp_path) -> None:
        result = run_driver(tmp_path, "para_stream_cancel")
        assert result["cancel_visible_mid_stream"] is True
        assert result["segments_started"] >= 1
        # Cancel halts BOTH paths promptly (AC-2): no done-audio was retained
        # AND the duck-typed sink reports StoppedState again.
        assert result["settled_after_cancel"] is True
        assert result["no_audio_retained"] is True
        assert result["sink_state_after_cancel"] == "StoppedState"
        assert result["waveform_hidden_after_cancel"] is True
        # Silent reset policy — this tab shows no banner for user cancels.
        assert result["no_error_banner"] is True
