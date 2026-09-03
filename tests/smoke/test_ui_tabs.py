"""Offscreen Text-tab smoke suite (FR-3.2, AC-1).

Drives the real GUI assembly — create_app + Main.qml + the rewritten
TextTab.qml — under ``QT_QPA_PLATFORM=offscreen`` with a fake controller and
fake playback injected through ``create_app`` factories (NO model load, NO
QtMultimedia). Each test group runs its scenarios in ONE subprocess (one QGuiApplication
per process; see conductor/patterns.md) and prints a ``RESULT:``-prefixed
JSON line these tests assert on — the same driver pattern as
``test_ui_shell.py``.

Fake-controller QML surface (mirrors AppController): voices, busy, progress,
errorText, hasAudio, lastExportPath, defaultVoice, outputDir, temperature +
cancelled signal + generate/cancel/exportWav slots, plus importDocument
(ParagraphTab's import seam — see below). exportWav writes a REAL
tiny WAV via ``write_wav_file`` so the export flows' ``wav_exists``
assertions are exercised for real.

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

Cross-tab lifecycle + error recovery — ``stream_cross_tab`` /
``stream_error_recover``: TWO sessions through ONE real controller + shell
instance. ``stream_cross_tab`` completes a Text-tab stream, then streams on
the Paragraph/File tab of the SAME window: streamActive cycles
false→true→false per tab, waveform visibility cycles, and tab 2's indicator
starts FRESH (a new session resets streamLevel to 0 before the first chunk,
so tab 1's final peak never leaks into tab 2's envelope). Hidden-subtree
historyCount reads are unreliable mid-session (StackLayout-deferred binding
side effects) — that reset is asserted post-session instead.
``stream_error_recover`` fails ONE mid-stream request at the fake SDK layer:
the generic error banner shows WITHOUT models-missing and without the cancel
toast, the sink hard-stops; a subsequent successful generation on the same
controller fully recovers (fresh session, error cleared, busy/streaming
reset, audio exportable).
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

DRIVER = textwrap.dedent(
    """\
    import gc
    import json
    import sys
    from pathlib import Path

    import numpy as np
    from vienetts_app.ui.bg_ops import run_sync
    from vienetts_app.ui.chapter_persist import SyncPersistExecutor
    from PySide6.QtCore import (
        Q_ARG,
        Q_RETURN_ARG,
        QCoreApplication,
        QEvent,
        Property,
        QObject,
        QMetaObject,
        QPointF,
        QThread,
        Qt,
        QUrl,
        Signal,
        Slot,
        qInstallMessageHandler,
    )
    from PySide6.QtGui import QMouseEvent
    from PySide6.QtQuick import QQuickItem

    from vienetts_app.app import create_app
    from vienetts_app.core.audio import write_wav_file
    from vienetts_app.ui.bridge import ShellBridge
    from vienetts_app.ui.stream_playback import StreamPlaybackController

    tmp_root = Path(sys.argv[1])
    scenarios = sys.argv[2].split(",")
    DEFAULT_VOICE = "adam_north"

    # This suite asserts Vietnamese UI copy; the app's "system" language
    # default follows the HOST locale (en_* hosts would render English and
    # break those assertions). Stub the controller's locale probe so every
    # REAL-controller scenario resolves the Vietnamese source language
    # deterministically (the fakes pin appliedLanguage = "vi" themselves).
    import vienetts_app.ui.controller as _controller_module

    class _ViLocale:
        @staticmethod
        def system():
            return _ViLocale()

        def name(self):
            return "vi_VN"

    _controller_module.QLocale = _ViLocale


    class FakeController(QObject):
        \"\"\"AppController's QML surface, with recording slots.\"\"\"

        voicesChanged = Signal()
        busyChanged = Signal()
        progressChanged = Signal()
        errorTextChanged = Signal()
        hasAudioChanged = Signal()
        hasArtifactChanged = Signal()
        artifactPathChanged = Signal()
        playbackStateChanged = Signal()
        lastExportPathChanged = Signal()
        defaultVoiceChanged = Signal()
        outputDirChanged = Signal()
        temperatureChanged = Signal()
        speedChanged = Signal()
        silencePChanged = Signal()
        consentGivenChanged = Signal()
        previewPathChanged = Signal()
        cancelled = Signal()
        backendChanged = Signal()
        precisionChanged = Signal()
        modelRepoChanged = Signal()
        themeChanged = Signal()
        languageChanged = Signal()
        needsRestartChanged = Signal()
        streamActiveChanged = Signal()
        streamLevelChanged = Signal()
        replayActiveChanged = Signal()
        waveformEnvelopeChanged = Signal()
        replayPositionChanged = Signal()
        replayDurationMsChanged = Signal()
        audioAvailableChanged = Signal()
        modelsMissingChanged = Signal()

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
            self._has_artifact = False
            self._artifact_path = ""
            self._playback_state = "idle"
            self._last_export_path = ""
            self._default_voice = DEFAULT_VOICE
            self._output_dir = str(tmp)
            self._temperature = 0.8
            self._speed = 1.0
            self._silence_p = 0.15
            self._backend = "auto"
            self._precision = "int8"
            self._theme = "system"
            self._language = "system"
            # Pinned startup language: the real controller resolves once at
            # construction; the fake fixes "vi" so needs-restart assertions
            # stay host-locale independent ("en" is the only other language).
            self._applied_language = "vi"
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
            self._model_repo = ""
            # Replay surface (Phát/Dừng toggle): QML drives replay/stopReplay
            # and binds text/icon to replayActive.
            self._replay_active = False
            self.replay_calls = 0
            self.stop_replay_calls = 0
            # PlaybackWaveform feed: empty overview + parked playhead until a
            # host scenario flips them (mirrors post-done real state).
            self._waveform_envelope: list[float] = []
            self._replay_position = 0.0
            self._replay_duration_ms = 0
            # FR-4.6a seam: audio OUTPUT availability gates tab playback
            # buttons. Default True so export-first play flows stay asserted;
            # ui_shell covers the unavailable-device side with the REAL
            # controller's injected probe.
            self._audio_available = True
            # Main.qml's models-missing scrim binds controller.modelsMissing;
            # leaving it undefined makes that binding assign [undefined] to
            # bool, which RESETS visible to true — a fullscreen scrim that
            # only matters to mouse-driven scenarios (hit-tested clicks).
            self._models_missing = False

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
            self._mutate("_has_artifact", bool(value), self.hasArtifactChanged)

        @Property(bool, notify=hasArtifactChanged)
        def hasArtifact(self):
            return self._has_artifact

        @hasArtifact.setter
        def hasArtifact(self, value):
            self._mutate("_has_artifact", bool(value), self.hasArtifactChanged)
            self._mutate("_has_audio", bool(value), self.hasAudioChanged)

        @Property(str, notify=artifactPathChanged)
        def artifactPath(self):
            return self._artifact_path

        @Property(str, notify=playbackStateChanged)
        def playbackState(self):
            return self._playback_state

        @playbackState.setter
        def playbackState(self, value):
            self._mutate("_playback_state", str(value), self.playbackStateChanged)

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

        @Property(str, notify=modelRepoChanged)
        def modelRepo(self):
            return self._model_repo

        @modelRepo.setter
        def modelRepo(self, value):
            if self._mutate("_model_repo", str(value).strip(), self.modelRepoChanged) and (
                self.engine_initialized
            ):
                self._mutate("_needs_restart", True, self.needsRestartChanged)

        @Property(str, notify=themeChanged)
        def theme(self):
            return self._theme

        @theme.setter
        def theme(self, value):
            self._mutate("_theme", str(value), self.themeChanged)

        @Property(str, notify=languageChanged)
        def language(self):
            return self._language

        @language.setter
        def language(self, value):
            self._mutate("_language", str(value), self.languageChanged)

        @Property(str, constant=True)
        def appliedLanguage(self):
            return self._applied_language

        @Property(bool, notify=needsRestartChanged)
        def needsRestart(self):
            return self._needs_restart

        @Property(float, notify=temperatureChanged)
        def temperature(self):
            return self._temperature

        @temperature.setter
        def temperature(self, value):
            self._mutate("_temperature", float(value), self.temperatureChanged)

        @Property(float, notify=speedChanged)
        def speed(self):
            return self._speed

        @speed.setter
        def speed(self, value):
            self._mutate("_speed", float(value), self.speedChanged)

        @Property(float, notify=silencePChanged)
        def silenceP(self):
            return self._silence_p

        @silenceP.setter
        def silenceP(self, value):
            self._mutate("_silence_p", float(value), self.silencePChanged)

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

        @Property(bool, notify=replayActiveChanged)
        def replayActive(self):
            return self._replay_active

        @replayActive.setter
        def replayActive(self, value):
            self._mutate("_replay_active", bool(value), self.replayActiveChanged)

        # PlaybackWaveform feed (mirrors the real NOTIFY-backed properties).
        @Property("QVariantList", notify=waveformEnvelopeChanged)
        def waveformEnvelope(self):
            return self._waveform_envelope

        @waveformEnvelope.setter
        def waveformEnvelope(self, value):
            self._mutate("_waveform_envelope", list(value), self.waveformEnvelopeChanged)

        @Property(float, notify=replayPositionChanged)
        def replayPosition(self):
            return self._replay_position

        @replayPosition.setter
        def replayPosition(self, value):
            clamped = max(0.0, min(float(value), 1.0))
            self._mutate("_replay_position", clamped, self.replayPositionChanged)

        @Property(int, notify=replayDurationMsChanged)
        def replayDurationMs(self):
            return self._replay_duration_ms

        @replayDurationMs.setter
        def replayDurationMs(self, value):
            self._mutate("_replay_duration_ms", max(0, int(value)), self.replayDurationMsChanged)

        @Slot()
        def replay(self):
            self.replay_calls += 1

        @Slot()
        def stopReplay(self):
            self.stop_replay_calls += 1

        @Property(bool, notify=audioAvailableChanged)
        def audioAvailable(self):
            return self._audio_available

        @audioAvailable.setter
        def audioAvailable(self, value):
            self._mutate("_audio_available", bool(value), self.audioAvailableChanged)

        @Property(bool, notify=modelsMissingChanged)
        def modelsMissing(self):
            # Getter-only like needsRestart: the fake never raises the
            # models-missing condition (ui_shell owns that surface).
            return self._models_missing

        @Slot()
        def cancel(self):
            self.cancel_calls += 1

        def attach_file_playback(self, playback):
            # create_app wires the temp-file replay player onto any
            # controller (RAM replay, other session's feature) — record it
            # like the real AppController's seam would.
            self.file_playback = playback

        @Slot(str, result=bool)
        def exportWav(self, path):
            # "" means export to the default dir; write a real tiny WAV so
            # the wav_exists assertion on the export flows is genuine.
            self.export_calls.append(str(path))
            target = Path(path) if str(path).strip() else tmp / "quick_export.wav"
            write_wav_file(np.linspace(-0.2, 0.2, 480).astype(np.float32), target)
            self._mutate("_last_export_path", str(target), self.lastExportPathChanged)
            return True

        @Slot(str, result=bool)
        def importDocument(self, path):
            # ParagraphTab's import seam, async contract (bead 12k): path in,
            # True back when accepted; the text lands synchronously on
            # documentImported — same shape the real pool path delivers.
            self.import_calls.append(str(path))
            if self.import_result:
                self.documentImported.emit(str(path), self.import_result)
            return True

        documentImported = Signal(str, str)
        importingChanged = Signal()

        @Property(bool, notify=importingChanged)
        def importing(self):
            return False

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
        # No QML surface at all - the REAL controller while importDocument
        # is still missing. Drives QML's typeof-guard (never crash, show the
        # error label instead); undefined property reads are falsy in QML.

        # create_app reads appliedLanguage (translator install), connects
        # languageChanged (live language swap), and wires the temp-file
        # replay player (attach_file_playback) off any controller.
        appliedLanguage = "vi"
        languageChanged = Signal()

        def attach_file_playback(self, playback) -> None:
            self.file_playback = playback

        pass


    results = {}
    for scenario in scenarios:
        # Per-scenario workspace under the shared tmp: settings/WAV
        # writes stay isolated even though the group shares one
        # process (and therefore one QGuiApplication/engine launch).
        tmp = tmp_root / scenario
        tmp.mkdir(parents=True, exist_ok=True)
        controller = (
            BareController() if scenario == "para_import_guard" else FakeController()
        )
        playback = FakePlayback()
        bridge = ShellBridge(settings_dir=tmp, detector=lambda: "SMOKE NOTE")
        # The engine note is deferred by design (startup perf): resolve the
        # injected fake probe up front, as run_gui's singleShot would.
        bridge.resolve_engine_note()

        # stream_e2e / stream_cancel / para_stream_e2e / para_stream_cancel /
        # stream_cross_tab / stream_error_recover swap the fake controller for
        # the REAL AppController: TTSEngine over a
        # fake-at-the-SDK-layer (generator infer_stream) + a real InferenceWorker
        # thread + a REAL StreamPlaybackController whose audio seam is faked (its
        # own duck-typed sink contract, mirroring tests/unit/test_controller.py's
        # FakeSink — no QtMultimedia construction happens offscreen).
        if scenario in (
            "stream_e2e",
            "stream_cancel",
            "para_stream_e2e",
            "para_stream_cancel",
            "stream_cross_tab",
            "stream_error_recover",
        ):
            import time

            from vienetts_app.core.engine import TTSEngine
            from vienetts_app.ui.controller import AppController
            from vienetts_app.workers.inference_worker import InferenceWorker

            chunk_delay_ms = {
                "stream_e2e": 0,
                "stream_cancel": 30,
                "para_stream_e2e": 0,
                "para_stream_cancel": 30,
                "stream_cross_tab": 40,
                "stream_error_recover": 30,
            }[scenario]

            class StreamVieneu:
                \"\"\"FakeVieneu subset with a GENERATOR infer_stream (spike §0).\"\"\"

                sample_rate = 48_000
                backend = "onnx"

                def __init__(self):
                    self.infer_stream_calls = []
                    # stream_error_recover arms this for ONE mid-stream failure;
                    # every other call (and scenario) streams normally.
                    self.fail_next = False

                def infer_stream(self, text, voice=None, temperature=None, **kw):
                    self.infer_stream_calls.append(
                        {"text": str(text), "voice": voice, "temperature": temperature}
                    )
                    if self.fail_next:
                        self.fail_next = False
                        if chunk_delay_ms:
                            time.sleep(chunk_delay_ms / 1000)
                        yield np.full(2400, 0.05, dtype=np.float32)
                        raise RuntimeError("boom-session-1: simulated SDK failure")
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
                bg_runner=run_sync,
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

            controller = AppController(data_dir=tmp, bg_runner=run_sync)

        from vienetts_app.ui.audiobook_controller import AudiobookController

        app, engine = create_app(
            bridge_factory=lambda: bridge,
            controller_factory=lambda: controller,
            playback_factory=lambda: playback,
            # The audiobook studio rides along on the shared controller. The
            # para_import_guard scenario uses a BARE QObject controller (no
            # signals at all) — give it a bare audiobook object too; every other
            # scenario gets the real AudiobookController over the scenario tmp.
            audiobook_factory=(
                (lambda _controller: QObject())
                if scenario == "para_import_guard"
                else (lambda app_controller: AudiobookController(
                    app_controller, data_dir=tmp, bg_runner=run_sync,
                    persist_executor=SyncPersistExecutor(),
                ))
            ),
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
        # errorLabel) exist once per instantiated tab. The cloning studio is
        # Loader-deferred (bead oey): resolve the item at USE time — every
        # cloning scenario navigates to the tab before looking inside it.
        def cloning_tab():
            return find("cloningTab")


        def cfind(name):
            return cloning_tab().findChildren(QObject, name)[0]


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
                "artifactPlaybackState",
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
            out["generate_hint"] = find("textActionHint").property("text")
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
            out["busy_generate_busy"] = generate.property("busy")
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
            controller.busy = True
            app.processEvents()
            out["play_enabled_while_busy_with_artifact"] = play.property("enabled")
            out["export_enabled_while_busy_with_artifact"] = find(
                "exportButton"
            ).property("enabled")
            out["quick_enabled_while_busy_with_artifact"] = find(
                "quickExportButton"
            ).property("enabled")
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
            out["play_disabled_without_audio"] = not play.property("enabled")

            controller.hasAudio = True
            app.processEvents()
            out["export_enabled_with_audio"] = export_btn.property("enabled")
            out["quick_enabled_with_audio"] = quick.property("enabled")
            # Phát works straight after generation — no export prerequisite.
            out["play_enabled_with_audio"] = play.property("enabled")
            out["play_text"] = play.property("text")

            play.click()
            app.processEvents()
            out["replay_calls"] = controller.replay_calls
            out["stop_replay_calls"] = controller.stop_replay_calls
            out["playback_played"] = playback.played  # RAM replay never touches the file player

            quick.click()
            app.processEvents()
            out["export_calls"] = controller.export_calls
            path = controller.lastExportPath
            out["last_export_path"] = path
            out["wav_exists"] = Path(path).is_file()
            out["play_enabled_after"] = play.property("enabled")

            # Toggle: replayActive flips Phát → Dừng; the click now stops.
            controller.replayActive = True
            app.processEvents()
            out["stop_text"] = play.property("text")
            play.click()
            app.processEvents()
            out["stop_replay_calls_after_toggle"] = controller.stop_replay_calls
        elif scenario == "error_flow":
            err = find("errorLabel")
            toast = find("toastLabel")

            out["error_hidden_initially"] = not err.property("visible")
            out["error_notice_tone"] = find("textErrorNotice").property("tone")

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
            # Find and trigger the toast timer directly instead of sleeping 2.4s
            timers = toast.findChildren(QObject)
            for t in timers:
                if "Timer" in t.metaObject().className():
                    QMetaObject.invokeMethod(t, "stop")
                    toast.setProperty("visible", False)
                    break
            app.processEvents()
            out["toast_hidden_after_timeout"] = not toast.property("visible")
        elif scenario == "disabled_states":
            editor = find("textEditor")
            generate = find("generateButton")

            out["generate_disabled_reason"] = generate.property("disabledReason")
            out["generate_min_height"] = generate.property("implicitHeight")

            editor.setProperty("text", "   ")
            app.processEvents()
            out["whitespace_generate_enabled"] = generate.property("enabled")
            out["blank_action_hint"] = find("textActionHint").property("text")

            editor.setProperty("text", "ok")
            app.processEvents()
            out["filled_generate_enabled"] = generate.property("enabled")
            out["filled_action_hint"] = find("textActionHint").property("text")

            controller.busy = True
            app.processEvents()
            out["busy_generate_visible"] = generate.property("visible")
            out["busy_cancel_visible"] = find("cancelButton").property("visible")

            controller.busy = False
            app.processEvents()
            out["idle_export_enabled"] = find("exportButton").property("enabled")
            out["idle_quick_enabled"] = find("quickExportButton").property("enabled")
            out["idle_play_enabled"] = find("playButton").property("enabled")
        elif scenario == "voice_picker_popup":
            picker = find("voicePicker")
            picker.setProperty(
                "flatModel",
                [
                    {"id": "", "label": "▸ Bắc"},
                    {"id": "adam_north", "label": "— Adam — Nam · Bắc · Ấm áp"},
                    {"id": "eva_north", "label": "— Eva — Nữ · Bắc · Rõ ràng"},
                    *[
                        {
                            "id": f"voice_{index}",
                            "label": f"— Giọng {index} — Trung tính · Tự nhiên",
                        }
                        for index in range(11)
                    ],
                    {"id": "", "label": "▸ Đã sao chép"},
                    {"id": "my_clone", "label": "— my_clone"},
                ],
            )
            picker.setProperty("currentIndex", 1)
            picker.setProperty("selectedVoice", "adam_north")
            app.processEvents()
            picker.window().show()
            wait_for(lambda: picker.window().isVisible())
            out["opened"] = QMetaObject.invokeMethod(picker, "openPopup")
            app.processEvents()
            out["popup_visible"] = picker.property("popupOpen")
            out["popup_dim"] = picker.property("popupDim")
            out["popup_title"] = picker.property("popupTitle")
            out["selected_voice_label"] = picker.property("selectedVoiceLabel")
            out["field_label"] = picker.property("fieldLabel")
            selected_before_filter = picker.property("selectedVoice")
            filters = picker.findChildren(QObject, "voicePickerFilter")
            out["filter_found"] = len(filters)
            out["filter_visible"] = bool(filters and filters[0].property("visible"))
            if filters:
                out["filter_placeholder"] = str(filters[0].property("placeholderText") or "")
                filters[0].setProperty("text", "Eva")
                app.processEvents()
            lists = picker.findChildren(QObject, "voicePickerList")
            rows = [
                item for item in item_walk(lists[0])
                if item.objectName() == "voicePickerRow"
            ] if lists else []
            out["filtered_visible_rows"] = [
                str(row.property("rowLabel"))
                for row in rows
                if bool(row.property("visible"))
            ]
            if filters:
                filters[0].setProperty("text", "")
                app.processEvents()
            out["selected_unchanged_after_filter"] = (
                picker.property("selectedVoice") == selected_before_filter
            )
            QMetaObject.invokeMethod(picker, "closePopup")
            app.processEvents()
            out["closed"] = not picker.property("popupOpen")
        elif scenario == "para_load":
            names = {o.objectName() for o in paragraph_tab.findChildren(QObject)}
            names.add(paragraph_tab.objectName())
            required = {
                "paragraphTab", "paragraphEditor", "importButton", "importDialog",
                "charCountLabel", "voicePicker", "generateButton", "progressBar",
                "cancelButton", "errorLabel", "playButton", "exportButton",
                # Streaming + notice surfaces (FR-4.4/FR-4.5/FR-4.6b): the shared
                # waveform and the banner hosting this tab's errorLabel.
                "waveformIndicator", "errorBanner", "srtKeepCheckbox", "artifactPlaybackState",
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
            out["generate_hint"] = pfind("paragraphActionHint").property("text")
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
            # Path-wrapped: toLocalPath() (a QUrl.toLocalFile round-trip)
            # normalizes to forward slashes; native str(Path) has backslashes
            # on Windows. Equal on every OS only through pathlib.
            out["local_path_matches"] = Path(str(local)) == doc

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
            out["busy_generate_busy"] = generate.property("busy")
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
            controller.busy = True
            app.processEvents()
            out["play_enabled_while_busy_with_artifact"] = play.property("enabled")
            out["export_enabled_while_busy_with_artifact"] = pfind(
                "exportButton"
            ).property("enabled")
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
            out["generate_visible_busy"] = pfind("generateButton").property("visible")

            cancel_btn.click()
            app.processEvents()
            out["cancel_calls"] = controller.cancel_calls
        elif scenario == "clone_gate":
            bridge.setCurrentTab("cloning")
            app.processEvents()
            consent = cfind("consentPanel")
            clone = cfind("clonePanel")
            accept = cfind("consentAcceptButton")

            names = {o.objectName() for o in cloning_tab().findChildren(QObject)}
            names.add(cloning_tab().objectName())
            required = {
                "cloningTab", "consentPanel", "consentAcceptButton", "clonePanel",
                "clipPathLabel", "clipBrowseButton", "clipDialog", "denoiseCheck",
                "denoiseButton", "previewPlayButton", "voiceNameField", "cloneButton",
                "clonedVoiceList", "errorLabel", "progressBar",
            }
            out["missing"] = sorted(required - names)
            out["header_found"] = any(
                o.property("text") == "Sao chép giọng nói"
                for o in cloning_tab().findChildren(QObject)
            )
            # Consent gate: panel visible with the acknowledgment text, the
            # cloning panel hidden until the user accepts.
            out["consent_visible"] = consent.property("visible")
            out["clone_visible"] = clone.property("visible")
            # FR-4.7 legal-warning copy: consent of the person actually being
            # cloned + lawful-use responsibility (CloningTab "consentText").
            out["consent_text_found"] = any(
                "người được sao chép" in (o.property("text") or "")
                for o in cloning_tab().findChildren(QObject)
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
                for o in cloning_tab().findChildren(QObject)
            )
            out["denoise_checked"] = cfind("denoiseCheck").property("checked")
            out["denoise_check_text"] = cfind("denoiseCheck").property("text")
            out["denoise_control_kind"] = cfind("denoiseCheck").property("controlKind")
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
                cloning_tab(), "selectClip", Q_ARG("QVariant", clip_path)
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

            QMetaObject.invokeMethod(cloning_tab(), "selectClip", Q_ARG("QVariant", clip_path))
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
            confirm_dialog = cfind("cloneRemoveConfirmDialog")
            out["confirm_visible"] = confirm_dialog.property("visible")
            out["remove_calls_before_confirm"] = list(controller.remove_voice_calls)

            click_item(cfind("cloneRemoveConfirmButton"))
            app.processEvents()
            out["remove_calls_after_confirm"] = list(controller.remove_voice_calls)
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
                cloning_tab(), "selectClip", Q_ARG("QVariant", str(tmp / "ref.wav"))
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
            bridge.setCurrentTab("settings")
            settings_tab = find("settingsTab")
            present = {o.objectName() for o in settings_tab.findChildren(QObject)}
            required = {
                "backendCombo", "detectedEngineLabel", "precisionCombo",
                "modelRepoField",
                "needsRestartBanner", "defaultVoiceCombo", "outputDirLabel",
                "outputDirBrowseButton", "temperatureSpin",
                "speedSpin", "silencePSpin",
                "themeCombo",
                "languageCombo", "errorLabel",
            }
            out["all_present"] = required <= present
            out["model_repo_placeholder"] = settings_tab.findChildren(
                QObject, "modelRepoField"
            )[0].property("placeholderText")
            out["detected_note"] = settings_tab.findChildren(
                QObject, "detectedEngineLabel"
            )[0].property("text")
            backend_combo = settings_tab.findChildren(QObject, "backendCombo")[0]
            out["backend_index"] = backend_combo.property("currentIndex")
            banner = settings_tab.findChildren(QObject, "needsRestartBanner")[0]
            out["needs_restart_visible"] = banner.property("visible")
            out["temperature_control_kind"] = settings_tab.findChildren(
                QObject, "temperatureSpin"
            )[0].property("controlKind")
            out["speed_control_kind"] = settings_tab.findChildren(
                QObject, "speedSpin"
            )[0].property("controlKind")
            out["silence_p_control_kind"] = settings_tab.findChildren(
                QObject, "silencePSpin"
            )[0].property("controlKind")
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
        elif scenario == "settings_model_repo":
            bridge.setCurrentTab("settings")
            settings_tab = find("settingsTab")
            field = settings_tab.findChildren(QObject, "modelRepoField")[0]
            banner = settings_tab.findChildren(QObject, "needsRestartBanner")[0]

            out["initial_text"] = field.property("text")
            out["placeholder"] = field.property("placeholderText")

            field.setProperty("text", "someone/vieneu-tts-custom")
            QMetaObject.invokeMethod(field, "editingFinished")
            app.processEvents()
            out["repo_after_commit"] = controller.modelRepo
            out["banner_no_engine"] = not banner.property("visible")

            # With a live engine, an override write flags needsRestart.
            controller.engine_initialized = True
            field.setProperty("text", "other-team/vieneu-tts-v4")
            QMetaObject.invokeMethod(field, "editingFinished")
            app.processEvents()
            out["repo_after_second_commit"] = controller.modelRepo
            out["banner_with_engine"] = banner.property("visible")

            # Blank commit resets to the official default.
            field.setProperty("text", "   ")
            QMetaObject.invokeMethod(field, "editingFinished")
            app.processEvents()
            out["repo_after_blank"] = controller.modelRepo
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
        elif scenario == "settings_language":
            bridge.setCurrentTab("settings")
            settings_tab = find("settingsTab")
            lang_combo = settings_tab.findChildren(QObject, "languageCombo")[0]

            def tab_texts():
                return [o.property("text") for o in settings_tab.findChildren(QObject)]

            out["banner_absent"] = (
                len(settings_tab.findChildren(QObject, "languageRestartBanner")) == 0
            )
            out["language_before"] = controller.language
            activate_item(lang_combo, 2)  # en
            app.processEvents()
            out["language_after"] = controller.language
            # LIVE switch: this very tab and the nav re-render in English with
            # no restart ("Color mode" = SettingsTab's color-mode label).
            out["live_english_label"] = "Color mode" in tab_texts()
            out["nav_after"] = bridge.tabs[0]["label"]
            activate_item(lang_combo, 1)  # vi
            app.processEvents()
            out["language_back"] = controller.language
            out["live_vietnamese_label"] = "Chế độ màu sắc" in tab_texts()
            out["nav_back"] = bridge.tabs[0]["label"]
        elif scenario == "settings_output":
            bridge.setCurrentTab("settings")
            settings_tab = find("settingsTab")
            label = settings_tab.findChildren(QObject, "outputDirLabel")[0]
            reset = settings_tab.findChildren(QObject, "outputDirResetButton")[0]
            out["label_before"] = label.property("text")
            invoked = QMetaObject.invokeMethod(
                settings_tab, "setOutputDir", Q_ARG("QVariant", str(tmp / "exports"))
            )
            app.processEvents()
            out["invoked"] = invoked
            out["output_dir_after"] = controller.outputDir
            out["label_after"] = label.property("text")
            out["reset_visible"] = reset.property("visible")
            QMetaObject.invokeMethod(reset, "click")
            app.processEvents()
            out["output_dir_after_reset"] = controller.outputDir
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
            speed_spin = settings_tab.findChildren(QObject, "speedSpin")[0]
            out["speed_before"] = controller.speed
            speed_spin.setProperty("value", 150)
            app.processEvents()
            out["speed_after"] = controller.speed

            silence_spin = settings_tab.findChildren(QObject, "silencePSpin")[0]
            out["silence_p_before"] = controller.silenceP
            silence_spin.setProperty("value", 35)
            app.processEvents()
            out["silence_p_after"] = controller.silenceP
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
        elif scenario == "settings_combo_delegates":
            # Popup delegate contract: opening a combo instantiates its delegates
            # and highlights currentIndex. A delegate that declares
            # `required property var modelData` but reads bare `index` throws
            # ReferenceError (required properties disable implicit index
            # injection) and the `highlighted` binding silently dies.
            bridge.setCurrentTab("settings")
            settings_tab = find("settingsTab")

            captured = []

            def record_message(_mode, _context, message):
                captured.append(str(message))

            qInstallMessageHandler(record_message)

            # Popups only open on a visible window and the harness never shows
            # the main one — show it (offscreen) before driving clicks.
            settings_tab.window().show()
            wait_for(lambda: settings_tab.window().isVisible())

            def hit_items(root, scene_point):
                # Deepest child chain under a scene point: what the window's
                # hit test would resolve for a click there (diagnostics).
                chain, item = [], root
                local = root.mapFromScene(scene_point)
                while item is not None:
                    chain.append(item)
                    child = item.childAt(local.x(), local.y())
                    if child is None:
                        break
                    item = child
                    local = item.mapFromScene(scene_point)
                return chain

            def combo_delegates():
                # In the popup's own window or overlay — walk EVERY window's visual tree.
                found = []
                for w in app.allWindows():
                    for obj in w.findChildren(QObject):
                        if obj.metaObject().className().startswith("ItemDelegate"):
                            if getattr(obj, "isVisible", lambda: True)():
                                found.append(obj)
                        elif hasattr(obj, "childItems"):
                            for item in obj.childItems():
                                if (
                                    item.metaObject().className().startswith("ItemDelegate")
                                    and item.isVisible()
                                    and item not in found
                                ):
                                    found.append(item)
                return found
            def click_at(point):
                for evt_type in (QEvent.MouseButtonPress, QEvent.MouseButtonRelease):
                    ev = QMouseEvent(
                        evt_type, point, point, point,
                        Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                        Qt.KeyboardModifier.NoModifier,
                    )
                    QCoreApplication.sendEvent(settings_tab.window(), ev)
                    app.processEvents()

            def open_combo(combo):
                if not QMetaObject.invokeMethod(combo, "openPopup"):
                    center = combo.mapToScene(
                        QPointF(combo.width() / 2, combo.height() / 2)
                    )
                    click_at(center)

            def close_combo(combo):
                if not QMetaObject.invokeMethod(combo, "closePopup"):
                    click_at(QPointF(40, 24))

            out["combo_results"] = {}
            out["opened"] = {}
            out["closed"] = {}
            settings_tab.window().requestActivate()
            wait_for(lambda: settings_tab.window().isActive())
            for name in ("backendCombo", "precisionCombo", "themeCombo"):
                combo = settings_tab.findChildren(QObject, name)[0]
                open_combo(combo)
                out.setdefault("hit", {})[name] = [
                    it.metaObject().className() + ":" + (it.objectName() or "")
                    for it in combo_delegates()
                ]
                # Popup incubation is asynchronous: a fixed sleep races it and
                # observes an empty popup — poll until every row materializes.
                out["opened"][name] = wait_for(
                    lambda: len(combo_delegates()) == combo.property("count")
                )
                during = combo_delegates()
                out["combo_results"][name] = {
                    "model_count": combo.property("count"),
                    "delegate_count": len(during),
                    "current_index": combo.property("currentIndex"),
                    "highlighted_index": combo.property("highlightedIndex"),
                    # Only this combo's popup is open, so during[] holds exactly
                    # its rows in model order.
                    "highlighted_delegate": [
                        d.property("highlighted") for d in during
                    ],
                }
                # Dismiss through the header (a press outside closes the popup;
                # re-clicking the combo would toggle) and poll for the popup's
                # delegates to be destroyed — otherwise they leak into the next
                # combo's observation.
                close_combo(combo)
                out["closed"][name] = wait_for(
                    lambda: not combo_delegates()
                )

            out["reference_errors"] = [
                m for m in captured if "is not defined" in m
            ]
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
            controller.playbackState = "generating"
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
            controller.playbackState = "idle"
            app.processEvents()
            out["history_cleared_on_end"] = int(wv.property("historyCount"))
            out["waveform_hidden_after"] = not wv.property("visible")
            out["component_active_after"] = bool(wv.property("active"))

            # PlaybackWaveform binding contract: the overview owns the slot once
            # audio exists and no synthesis stream is live — including memory
            # replays (streamActive True AND replayActive True).
            pw = tfind("playbackWaveform")
            out["overview_hidden_without_audio"] = not pw.property("visible")

            controller.hasAudio = True
            controller.waveformEnvelope = [0.2, 0.5, 1.0, 0.4]
            controller.replayDurationMs = 12_000
            app.processEvents()
            out["overview_visible_with_audio"] = bool(pw.property("visible"))
            out["overview_bucket_count"] = int(pw.property("bucketCount"))

            # Live synthesis reclaims the slot for the rolling meter.
            controller.streamActive = True
            controller.playbackState = "generating"
            app.processEvents()
            out["overview_hidden_during_stream"] = not pw.property("visible")

            # Memory replay: meter hidden, overview live with a moving playhead.
            controller.replayActive = True
            controller.replayPosition = 0.25
            app.processEvents()
            out["overview_visible_during_replay"] = bool(pw.property("visible"))
            out["overview_active_during_replay"] = bool(pw.property("active"))
            out["meter_hidden_during_replay"] = not wv.property("visible")
            out["position_bound"] = float(pw.property("position"))

            # Replay end: overview stays (idle shape), playhead parked at 0.
            controller.replayActive = False
            controller.replayPosition = 0.0
            controller.streamActive = False
            controller.playbackState = "idle"
            app.processEvents()
            out["overview_visible_after_replay"] = bool(pw.property("visible"))
            out["overview_inactive_after_replay"] = not pw.property("active")
            controller.hasAudio = False
            app.processEvents()
            out["overview_hidden_after_audio_cleared"] = not pw.property("visible")

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
            # Drain window (rqy): done must NOT kill the meter while audio is
            # still buffered in the sink (3×2400 samples = 150 ms + margin)...
            out["done_stream_draining"] = bool(controller.streamActive)
            out["done_waveform_visible_during_drain"] = bool(wv.property("visible"))
            # ...it flips once the buffered tail has played out.
            out["drained_stream_inactive"] = wait_for(
                lambda: not controller.streamActive, timeout_ms=3000
            )
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
            controller.playbackState = "generating"
            app.processEvents()
            out["waveform_visible_during"] = bool(wv.property("visible"))
            out["component_active_during"] = bool(wv.property("active"))
            controller.streamLevel = 0.7
            app.processEvents()
            out["level_bound_latest"] = float(wv.property("level"))
            out["history_after_push"] = int(wv.property("historyCount"))

            controller.streamActive = False
            controller.playbackState = "idle"
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
                # Drain window (rqy): the meter outlives done until the sink's
                # buffered tail played out, then hides.
                out["done_stream_draining"] = bool(controller.streamActive)
                out["drained_stream_inactive"] = wait_for(
                    lambda: not controller.streamActive, timeout_ms=3000
                )
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
        elif scenario == "stream_cross_tab":
            # TWO sessions through ONE real controller + shell: the Text tab
            # completes a full stream cycle, then the Paragraph/File tab of the
            # SAME instance streams — asserting per-tab session resets and that
            # tab 1's final peak level never leaks into tab 2's indicator.
            t_wv = tfind("waveformIndicator")
            p_wv = pfind("waveformIndicator")
            sessions = {"phase": 1, "live": [False, False], "wave": [False, False],
                        "levels": [[], []]}

            def _on_cross_active():
                if not controller.streamActive:
                    return
                idx = sessions["phase"] - 1
                sessions["live"][idx] = True
                wv = t_wv if idx == 0 else p_wv
                if bool(wv.property("visible")):
                    sessions["wave"][idx] = True

            controller.streamActiveChanged.connect(_on_cross_active)
            controller.streamLevelChanged.connect(
                lambda: sessions["levels"][sessions["phase"] - 1].append(
                    float(controller.streamLevel)
                )
            )

            # ── Session 1: Text tab, full cycle ──
            tfind("textEditor").setProperty("text", "Xin chào thế giới")
            app.processEvents()
            find("generateButton").click()
            done1 = wait_for(
                lambda: controller.hasAudio and not controller.busy
                and not controller.streamActive
            )
            app.processEvents()

            out["s1_completed"] = done1
            out["s1_segments"] = len(stream_sdk.infer_stream_calls)
            out["s1_saw_live"] = sessions["live"][0]
            out["s1_wave_visible_during"] = sessions["wave"][0]
            out["s1_peak"] = max(sessions["levels"][0]) if sessions["levels"][0] else 0.0
            out["s1_inactive_after"] = not controller.streamActive
            out["s1_waveform_hidden_after"] = not bool(t_wv.property("visible"))
            out["s1_history_cleared"] = int(t_wv.property("historyCount")) == 0
            # Done-path stale-level SETUP evidence: nothing resets streamLevel at
            # done, so tab 1's indicator still binds its final peak...
            out["s1_level_retained_indicator"] = float(t_wv.property("level"))
            out["s1_level_retained_controller"] = float(controller.streamLevel)

            # ── Session 2: Paragraph tab, SAME controller/shell instance ──
            bridge.setCurrentTab("paragraph")  # visibility updates need current tab
            app.processEvents()
            sessions["phase"] = 2
            pfind("paragraphEditor").setProperty("text", "Đoạn thứ nhất. Đoạn thứ hai.")
            app.processEvents()
            out["p_generate_enabled"] = bool(pfind("generateButton").property("enabled"))
            pfind("generateButton").click()
            started2 = wait_for(lambda: controller.streamActive)
            app.processEvents()
            # Leak guard, read BEFORE any chunk can arrive (the fake delays them):
            # a fresh session resets streamLevel to 0 at start (FR-4.2), so THIS
            # tab's indicator must show 0/empty history — never tab 1's peak.
            out["s2_session_started"] = started2
            out["s2_level_reset_controller"] = float(controller.streamLevel) == 0.0
            out["s2_indicator_fresh_level"] = float(p_wv.property("level")) == 0.0
            done2 = wait_for(
                lambda: controller.hasAudio and not controller.busy
                and not controller.streamActive
            )
            app.processEvents()

            out["s2_completed"] = done2
            out["s2_has_audio"] = controller.hasAudio
            out["s2_saw_live"] = sessions["live"][1]
            out["s2_wave_visible_during"] = sessions["wave"][1]
            out["s2_peak"] = max(sessions["levels"][1]) if sessions["levels"][1] else 0.0
            out["s2_done_inactive"] = not controller.streamActive
            out["s2_done_waveform_hidden"] = not bool(p_wv.property("visible"))
            out["s2_history_cleared_on_end"] = int(p_wv.property("historyCount")) == 0
            out["s2_progress_final"] = float(controller.progress)
            # Export affordance restored after BOTH sessions.
            out["export_ok_after_both"] = controller.exportWav("")
            out["last_export_path"] = controller.lastExportPath
        elif scenario == "stream_error_recover":
            # Mid-stream SDK failure → generic error banner (NOT models-missing),
            # then an immediate successful generation fully recovers the UI state
            # on the SAME controller/shell: busy/streaming reset, error cleared,
            # fresh audio exportable.
            wv = tfind("waveformIndicator")
            err_label = find("errorLabel")
            toast = find("toastLabel")

            risings = {"n": 0}

            def _count_rising():
                if controller.streamActive:
                    risings["n"] += 1

            controller.streamActiveChanged.connect(_count_rising)

            # ── Phase 1: exactly ONE mid-stream SDK failure ──
            stream_sdk.fail_next = True
            tfind("textEditor").setProperty("text", "Xin chào thế giới")
            app.processEvents()
            find("generateButton").click()
            settled = wait_for(lambda: not controller.busy and not controller.streamActive)
            app.processEvents()

            out["settled_after_error"] = settled
            err_text = str(err_label.property("text"))
            out["error_visible"] = bool(err_label.property("visible"))
            out["error_text"] = err_text
            # Generic failure ⇒ models-missing flag/overlay must stay absent.
            out["models_missing_absent"] = not controller.modelsMissing
            out["no_audio_from_failed_session"] = not controller.hasAudio
            # Error, not cancel: no toast; sink was hard-stopped by the reset.
            out["toast_absent"] = not bool(toast.property("visible"))
            out["sink_state_after_error"] = (
                sink_holder["sink"].state() if "sink" in sink_holder else "?"
            )
            out["waveform_hidden_after_error"] = not bool(wv.property("visible"))

            # ── Phase 2: successful recovery on the same controller/shell ──
            out["regenerate_enabled"] = bool(find("generateButton").property("enabled"))
            rising_before = risings["n"]
            find("generateButton").click()
            started = wait_for(lambda: controller.streamActive)
            app.processEvents()
            out["recovered_stream_started"] = started
            out["recovered_level_reset"] = float(controller.streamLevel) == 0.0
            out["error_cleared_at_start"] = (
                not bool(err_label.property("visible")) and controller.errorText == ""
            )
            out["recovery_started_fresh_session"] = risings["n"] > rising_before
            done = wait_for(
                lambda: controller.hasAudio and not controller.busy
                and not controller.streamActive
            )
            app.processEvents()

            out["recovery_completed"] = done
            out["recovered_busy_false"] = not controller.busy
            out["recovered_stream_inactive"] = not controller.streamActive
            out["recovered_waveform_hidden"] = not bool(wv.property("visible"))
            out["recovered_error_still_clear"] = controller.errorText == ""
            out["export_ok_after_recovery"] = controller.exportWav("")
            out["last_export_path"] = controller.lastExportPath
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
    # The driver is ~30 KB of Python — Windows CreateProcess caps the whole
    # command line at ~32 KB (WinError 206), so it must run from a file, not
    # `python -c`. Script mode drops cwd from sys.path, hence PYTHONPATH.
    driver_path = tmp_path / "_driver.py"
    driver_path.write_text(DRIVER, encoding="utf-8")
    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
    }
    proc = subprocess.run(
        [sys.executable, str(driver_path), str(tmp_path), ",".join(scenarios)],
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
    def test_surface_generate_export_and_error_flows(self, tmp_path) -> None:
        results = run_driver(
            tmp_path,
            [
                "load",
                "disabled_states",
                "voice_picker_popup",
                "generate_flow",
                "export_flow",
                "error_flow",
            ],
        )
        result = results["load"]
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
        assert result["generate_hint"] == "Nhập văn bản để tạo âm thanh."

        result = results["disabled_states"]
        assert isinstance(result["generate_disabled_reason"], str)
        assert result["generate_min_height"] >= 44
        assert result["whitespace_generate_enabled"] is False
        assert result["blank_action_hint"] == "Nhập văn bản để tạo âm thanh."
        assert result["filled_generate_enabled"] is True
        assert result["filled_action_hint"] == "Tạo âm thanh trước khi phát hoặc xuất."
        assert result["busy_generate_visible"] is True
        assert result["busy_cancel_visible"] is True
        assert result["idle_export_enabled"] is False
        assert result["idle_quick_enabled"] is False
        assert result["idle_play_enabled"] is False

        result = results["voice_picker_popup"]
        assert result["opened"] is True
        assert result["popup_visible"] is True
        assert result["popup_dim"] is False
        assert result["popup_title"] == "Chọn giọng đọc"
        assert result["field_label"] == "Giọng đọc"
        assert result["selected_voice_label"] == "Adam — Nam · Bắc · Ấm áp"
        assert result["filter_found"] == 1
        assert result["filter_visible"] is True
        assert result["filter_placeholder"] == "Tìm giọng đọc…"
        assert set(result["filtered_visible_rows"]) == {
            "▸ Bắc",
            "— Eva — Nữ · Bắc · Rõ ràng",
        }
        assert len(result["filtered_visible_rows"]) == 2
        assert result["selected_unchanged_after_filter"] is True
        assert result["closed"] is True

        result = results["generate_flow"]
        # Generate is wired through the STREAMING slot (FR-4.3): same
        # (text, voice) payload the batch seam used to receive, but
        # slot_hits proves which submit path ran.
        assert result["initial_generate_enabled"] is False
        assert result["filled_generate_enabled"] is True
        assert result["generate_calls"] == [["Xin chào thế giới", "adam_north"]]
        assert result["slot_hits"] == ["generateStream"]
        # Busy state keeps the primary action in place and adds progress + cancel.
        assert result["busy_generate_visible"] is True
        assert result["busy_generate_busy"] is True
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
        assert result["play_enabled_while_busy_with_artifact"] is True
        assert result["export_enabled_while_busy_with_artifact"] is True
        assert result["quick_enabled_while_busy_with_artifact"] is True
        assert result["progress_hidden_after"] is True
        assert result["cancel_hidden_after"] is True
        assert result["generate_visible_after"] is True

        result = results["export_flow"]
        assert result["export_disabled_without_audio"] is True
        assert result["quick_disabled_without_audio"] is True
        assert result["play_disabled_without_audio"] is True
        assert result["export_enabled_with_audio"] is True
        assert result["quick_enabled_with_audio"] is True
        # Phát replays WITHOUT any export (the 2026-08-28 flow change).
        assert result["play_enabled_with_audio"] is True
        assert result["play_text"] == "Phát"
        assert result["replay_calls"] == 1
        assert result["stop_replay_calls"] == 0
        assert result["playback_played"] == []  # replay rides the stream sink, not the file player
        # Quick export still routes through exportWav("") and writes a real WAV.
        assert result["export_calls"] == [""]
        assert result["last_export_path"].endswith(".wav")
        assert result["wav_exists"] is True
        assert result["play_enabled_after"] is True
        # Replay toggle: button becomes Dừng and stops instead of replaying.
        assert result["stop_text"] == "Dừng"
        assert result["stop_replay_calls_after_toggle"] == 1

        result = results["error_flow"]
        assert result["error_notice_tone"] == "error"
        assert result["error_hidden_initially"] is True
        assert result["error_visible"] is True
        assert result["error_text"] == "Lỗi tổng hợp: không đủ bộ nhớ"
        assert result["error_hidden_after_clear"] is True
        assert result["toast_hidden_initially"] is True
        assert result["toast_visible_on_cancel"] is True
        assert result["toast_text"] == "Đã hủy"
        assert result["toast_hidden_after_timeout"] is True


class TestParagraphTabSmoke:
    def test_import_ui_guards_generate_and_cancel_states(self, tmp_path) -> None:
        results = run_driver(
            tmp_path,
            [
                "para_load",
                "para_import",
                "para_import_guard",
                "para_generate",
                "para_cancel",
            ],
        )
        result = results["para_load"]
        # ⚑ contract: every named element exists under the paragraphTab subtree.
        assert result["missing"] == []
        assert result["editor_editable"] is True
        assert result["import_button_text"] == "Nhập tệp…"
        # Import dialog: filters mirror SUPPORTED_EXTENSIONS (.txt .md .docx
        # .pdf .srt). fileMode (OpenFile) has no PySide6 enum converter — its
        # accepted path is proven end-to-end by test_para_import_via_import_path.
        assert result["dialog_filters"] == ["Văn bản (*.txt *.md *.docx *.pdf *.srt)"]
        assert result["header_found"] is True
        assert result["hint_mentions_extensions"] is True
        # Empty editor → "0 ký tự" live counter, generate disabled.
        assert result["char_count_text"] == "0 ký tự"
        assert result["initial_generate_enabled"] is False
        assert result["generate_hint"] == "Nhập văn bản để tạo âm thanh."
        # Same grouped picker contract as TextTab (headers non-selectable).
        assert result["flat_ids"] == ["", "adam_north", "eva_north", "", "my_clone"]
        assert result["selected_voice"] == "adam_north"
        assert result["current_index"] == 1

        result = results["para_import"]
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

        result = results["para_import_guard"]
        # Missing importDocument on the controller must not crash the tab:
        # the error label explains and the editor stays untouched.
        assert result["invoked"] is True
        assert result["error_visible"] is True
        assert result["error_text"] == "Không thể nhập tệp"
        assert result["editor_unchanged"] is True
        assert result["no_import_recorded"] is True

        result = results["para_generate"]
        long_text = "Đoạn thứ nhất.\n\nĐoạn thứ hai."
        assert result["initial_generate_enabled"] is False
        assert result["filled_generate_enabled"] is True
        assert result["generate_calls"] == [[long_text, "adam_north"]]
        # ParagraphTab streams through the SAME seam as the Text tab now
        # (FR-4.4); the shared fake records which submit path ran.
        assert result["slot_hits"] == ["generateStream"]
        assert result["char_count_text"] == f"{len(long_text)} ký tự"
        # Busy state: primary action stays in place, with progress and cancel.
        assert result["busy_generate_visible"] is True
        assert result["busy_generate_busy"] is True
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
        assert result["play_enabled_while_busy_with_artifact"] is True
        assert result["export_enabled_while_busy_with_artifact"] is True
        assert result["progress_hidden_after"] is True
        assert result["cancel_hidden_after"] is True
        assert result["generate_visible_after"] is True

        result = results["para_cancel"]
        assert result["cancel_hidden_idle"] is True
        assert result["cancel_visible_busy"] is True
        assert result["cancel_enabled_busy"] is True
        assert result["progress_visible_busy"] is True
        assert result["generate_visible_busy"] is True
        assert result["cancel_calls"] == 1


class TestCloningTabSmoke:
    def test_consent_gate_enroll_denoise_remove_and_disabled_states(self, tmp_path) -> None:
        results = run_driver(
            tmp_path,
            [
                "clone_gate",
                "clone_flow",
                "clone_denoise",
                "clone_remove",
                "clone_disabled",
            ],
        )
        result = results["clone_gate"]
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
        assert result["denoise_control_kind"] == "toggle"
        assert result["denoise_text"] == "Nghe bản khử nhiễu"
        assert result["preview_hidden_initially"] is True
        assert result["name_placeholder"] == "Tên giọng mới (vd: Giọng đọc truyện)"
        assert result["clone_text"] == "Tạo giọng nói"

        result = results["clone_flow"]
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

        result = results["clone_denoise"]
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

        result = results["clone_remove"]
        # The cloned catalog group ("my_clone" from the seed catalog) renders
        # a row whose Xóa button wires controller.removeVoice(name).
        assert result["rows_before"] == ["my_clone"]
        assert result["remove_button_text"] == "Xóa"
        assert result["confirm_visible"] is True
        assert result["remove_calls_before_confirm"] == []
        assert result["remove_calls_after_confirm"] == ["my_clone"]
        assert result["rows_after"] == []

        result = results["clone_disabled"]
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
    def test_controls_and_engine_temperature_voice_delegates(self, tmp_path) -> None:
        results = run_driver(
            tmp_path,
            [
                "settings_load",
                "settings_model_repo",
                "settings_theme",
                "settings_language",
                "settings_output",
                "settings_engine",
                "settings_temperature",
                "settings_default_voice",
                "settings_combo_delegates",
            ],
        )
        result = results["settings_load"]
        assert result["all_present"] is True
        # Detector readout (model-free) repeats on the settings tab (FR-3.5).
        assert result["detected_note"] == "SMOKE NOTE"
        # Default backend "auto" → index 0; no stale restart banner at load.
        assert result["backend_index"] == 0
        assert result["needs_restart_visible"] is False
        assert result["temperature_control_kind"] == "number"
        assert result["speed_control_kind"] == "number"
        assert result["silence_p_control_kind"] == "number"

        result = results["settings_model_repo"]
        # Empty field + official-repo placeholder at load (empty = default).
        assert result["initial_text"] == ""
        assert "VieNeu-TTS" in str(result["placeholder"])
        # editingFinished commits to the controller seam (QML never persists
        # per keystroke); no engine → applies at next start, no banner.
        assert result["repo_after_commit"] == "someone/vieneu-tts-custom"
        assert result["banner_no_engine"] is True
        assert result["repo_after_second_commit"] == "other-team/vieneu-tts-v4"
        assert result["banner_with_engine"] is True
        # Blank → back to the official default repo.
        assert result["repo_after_blank"] == ""

        result = results["settings_theme"]
        assert result["pref_before"] == "system"
        assert result["bridge_pref_after"] == "light"
        # The controller mirrors the same settings.json field (its seam).
        assert result["controller_theme_after"] == "light"
        # Live switch: the bridge re-resolves the effective theme.
        assert result["effective_after"] == "light"

        result = results["settings_language"]
        # The restart banner is gone — language applies instantly.
        assert result["banner_absent"] is True
        assert result["language_before"] == "system"
        assert result["language_after"] == "en"
        assert result["live_english_label"] is True
        assert result["nav_after"] == "Text"
        assert result["language_back"] == "vi"
        assert result["live_vietnamese_label"] is True
        assert result["nav_back"] == "Văn bản"

        result = results["settings_output"]
        assert result["invoked"] is True
        assert result["output_dir_after"].endswith("exports")
        assert result["label_after"].endswith("exports")
        assert result["reset_visible"] is True
        assert result["output_dir_after_reset"] == ""

        result = results["settings_engine"]
        assert result["backend_after"] == "torch"
        # With no engine initialized the change applies at (re)start — no banner.
        assert result["banner_after_no_engine"] is True
        # Once an engine is live, engine-affecting writes flag needsRestart
        # instead of mutating the running engine (FR-3.5, AC-4).
        assert result["precision_after"] == "fp32"
        assert result["banner_visible_with_engine"] is True

        result = results["settings_temperature"]
        assert result["temp_before"] == 0.8
        assert abs(result["temp_after"] - 1.2) < 1e-9
        # SpinBox display text (the `text` property is write-only from C++).
        # DisplayText renders via QLocale: the decimal separator follows the
        # HOST system locale (vi_VN → comma), not LANG — normalize before
        # comparing so this stays machine-independent like the rest of the
        # suite.
        spin_text = str(result["spin_text"]).replace(",", ".")
        assert spin_text == "1.20"
        assert abs(result["speed_before"] - 1.0) < 1e-9
        assert abs(result["speed_after"] - 1.5) < 1e-9
        assert abs(result["silence_p_before"] - 0.15) < 1e-9
        assert abs(result["silence_p_after"] - 0.35) < 1e-9

        result = results["settings_default_voice"]
        assert result["default_before"] == "adam_north"
        assert result["default_after"] == "eva_north"

        # Regression (ReferenceError: index is not defined): delegates that
        # declare `required property var modelData` lose Qt 6's implicit
        # `index` injection, so the `highlighted` binding must read a
        # declared `required property int index` instead. Opening each combo
        # must instantiate every delegate and highlight exactly the current
        # row with zero ReferenceErrors.
        result = results["settings_combo_delegates"]
        assert result["reference_errors"] == []
        for name, combo in result["combo_results"].items():
            assert result["opened"][name] is True, name
            assert result["closed"][name] is True, name
            assert combo["delegate_count"] == combo["model_count"], name
            assert combo["highlighted_index"] == combo["current_index"], name
            highlighted = combo["highlighted_delegate"]
            assert highlighted[combo["current_index"]] is True, name
            assert sum(1 for h in highlighted if h) == 1, name


class TestWaveformIndicatorSmoke:
    """FR-4.5 groundwork: TextTab hosts the shared WaveformIndicator.

    Binding-level scenarios (fake controller): flipping streamActive /
    streamLevel programmatically must re-render the indicator — the tested
    surface is QML state (.property reads), never pixels.
    """

    def test_stream_bindings_and_visibility_cycle(self, tmp_path) -> None:
        results = run_driver(tmp_path, ["stream_bindings"])
        result = results["stream_bindings"]
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
        # PlaybackWaveform owns the slot once audio exists (no live stream):
        # idle dim overview, live playhead during replay, gone with the audio.
        assert result["overview_hidden_without_audio"] is True
        assert result["overview_visible_with_audio"] is True
        assert result["overview_bucket_count"] == 4
        assert result["overview_hidden_during_stream"] is False
        assert result["overview_visible_during_replay"] is True
        assert result["overview_active_during_replay"] is True
        assert result["meter_hidden_during_replay"] is True
        assert result["position_bound"] == 0.25
        assert result["overview_visible_after_replay"] is True
        assert result["overview_inactive_after_replay"] is True
        assert result["overview_hidden_after_audio_cleared"] is True
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

    def test_stream_cycle_and_cancel(self, tmp_path) -> None:
        results = run_driver(tmp_path, ["stream_e2e", "stream_cancel"])
        result = results["stream_e2e"]
        assert result["completed"] is True
        # Exactly one segmented infer_stream dispatch carrying the editor text.
        assert len(result["infer_stream_calls"]) >= 1
        assert result["infer_stream_calls"][0]["text"] == "Xin chào thế giới"
        # streamActive toggled true→false with the waveform live during; the
        # meter survives done until the sink's buffered tail drained (rqy).
        assert result["saw_session_live"] is True
        assert result["waveform_visible_during_session"] is True
        assert result["peak_level_seen"] > 0.5  # fake chunks peak at 0.9
        assert result["done_stream_draining"] is True
        assert result["done_waveform_visible_during_drain"] is False
        assert result["drained_stream_inactive"] is True
        assert result["done_waveform_hidden"] is True
        assert result["progress_final"] == 1.0
        # Retained audio keeps the export affordance working after done.
        assert result["export_ok"] is True
        assert result["last_export_path"].endswith(".wav")

        result = results["stream_cancel"]
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

    def test_bindings_and_oversize_import(self, tmp_path) -> None:
        results = run_driver(tmp_path, ["para_stream_bindings", "para_import_oversize"])
        result = results["para_stream_bindings"]
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

        result = results["para_import_oversize"]
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

    def test_para_stream_cycle_and_cancel(self, tmp_path) -> None:
        results = run_driver(tmp_path, ["para_stream_e2e", "para_stream_cancel"])
        result = results["para_stream_e2e"]
        assert result["completed"] is True
        # The editor text reached the SDK seam through the stream dispatch.
        # The chunked dispatcher packs sentence segments whitespace-normalized
        # (\n\n → space), so assert CONTENT, not the raw newline shape.
        assert "Đoạn thứ nhất." in result["doc_text_sent"]
        assert "Đoạn thứ hai." in result["doc_text_sent"]
        assert result["segment_count"] >= 1
        # streamActive toggled true→false with the waveform live during; the
        # meter survives done until the sink's buffered tail drained (rqy).
        assert result["saw_session_live"] is True
        assert result["waveform_visible_during_session"] is True
        assert result["peak_level_seen"] > 0.5  # fake chunks peak at 0.9
        assert result["done_stream_draining"] is True
        assert result["drained_stream_inactive"] is True
        assert result["done_waveform_hidden"] is True
        # Segment-counted progress completed, retained audio re-enables the
        # replay/export affordances (FR-4.4 done-path).
        assert result["progress_final"] == 1.0
        assert result["has_audio_after"] is True
        assert result["export_ok"] is True
        assert result["last_export_path"].endswith(".wav")

        result = results["para_stream_cancel"]
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


class TestCrossTabStreamLifecycle:
    """TWO streaming sessions through ONE real controller + shell instance.

    Session 1 completes on the Text tab; session 2 then runs on the
    Paragraph/File tab of the SAME window. Asserts per-tab session resets:
    streamActive cycles false→true→false on both tabs' indicators, and tab 2's
    indicator starts FRESH — a new session must reset streamLevel to 0 before
    the first chunk lands (FR-4.2), so tab 1's final peak never leaks into
    tab 2's envelope. Export keeps working after both sessions.
    """

    def test_text_then_paragraph_sessions_reset_between_tabs(self, tmp_path) -> None:
        results = run_driver(tmp_path, ["stream_cross_tab"])
        result = results["stream_cross_tab"]
        # ── Session 1: Text tab, full cycle ──
        assert result["s1_completed"] is True
        assert result["s1_segments"] >= 1
        assert result["s1_saw_live"] is True
        assert result["s1_wave_visible_during"] is True
        assert result["s1_peak"] > 0.5
        assert result["s1_inactive_after"] is True
        assert result["s1_waveform_hidden_after"] is True
        assert result["s1_history_cleared"] is True
        # Stale-level SETUP evidence: when session 1 ends the indicator still
        # binds session 1's final peak (nothing resets it on done).
        assert result["s1_level_retained_indicator"] == result["s1_level_retained_controller"]
        assert result["s1_level_retained_indicator"] > 0.5

        # ── Session 2: Paragraph/File tab, SAME controller/shell ──
        assert result["p_generate_enabled"] is True
        assert result["s2_session_started"] is True
        # The leak guard: at session start (before any chunk can have arrived,
        # the fake delays chunks) BOTH the controller property and THIS tab's
        # indicator read 0 — not tab 1's retained peak.
        assert result["s2_level_reset_controller"] is True
        assert result["s2_indicator_fresh_level"] is True
        # NOTE: tab 2's hidden-subtree historyCount is NOT readable reliably
        # mid-session (StackLayout-deferred binding side effects — same family
        # as the visible-binding gotcha); its post-session clear is asserted
        # below once this tab is current and settled.
        assert result["s2_completed"] is True
        assert result["s2_has_audio"] is True
        assert result["s2_saw_live"] is True
        assert result["s2_wave_visible_during"] is True
        assert result["s2_peak"] > 0.5
        assert result["s2_done_inactive"] is True
        assert result["s2_done_waveform_hidden"] is True
        assert result["s2_history_cleared_on_end"] is True
        assert result["s2_progress_final"] == 1.0
        # Export affordance restored after BOTH sessions.
        assert result["export_ok_after_both"] is True
        assert result["last_export_path"].endswith(".wav")


class TestStreamErrorRecovery:
    """A mid-stream SDK failure surfaces WITHOUT models-missing, and the next
    successful generation fully recovers the UI state on the same controller.

    Uncovered today: the models-missing overlay suite ends at dismiss/retry;
    it never proves a subsequent SUCCESSFUL generation clears busy, resets the
    streaming session, clears the error surface, and yields exportable audio.
    """

    def test_error_banner_then_next_generation_recovers_state(self, tmp_path) -> None:
        results = run_driver(tmp_path, ["stream_error_recover"])
        result = results["stream_error_recover"]
        # ── Phase 1: mid-stream failure ──
        assert result["settled_after_error"] is True
        assert result["error_visible"] is True
        assert "boom-session-1" in result["error_text"]
        # A generic error must NOT raise the models-missing overlay.
        assert result["models_missing_absent"] is True
        assert result["no_audio_from_failed_session"] is True
        # Error (not cancel): silent-reset toast stays absent, sink hard-stops.
        assert result["toast_absent"] is True
        assert result["sink_state_after_error"] == "StoppedState"
        assert result["waveform_hidden_after_error"] is True

        # ── Phase 2: successful recovery on the same controller/shell ──
        assert result["regenerate_enabled"] is True
        assert result["recovered_stream_started"] is True
        # Fresh session: level reset to 0, error cleared at submit time.
        assert result["recovered_level_reset"] is True
        assert result["error_cleared_at_start"] is True
        assert result["recovery_completed"] is True
        assert result["recovered_busy_false"] is True
        assert result["recovered_stream_inactive"] is True
        assert result["recovered_waveform_hidden"] is True
        assert result["recovered_error_still_clear"] is True
        assert result["export_ok_after_recovery"] is True
        assert result["last_export_path"].endswith(".wav")


# ── Audiobook tab (FR-A7) ────────────────────────────────────────────────────
# Same subprocess/driver discipline as above: a fake audiobook controller
# (mirroring AudiobookController's QML surface) + minimal fake app controller
# are injected through create_app factories; NO model, NO QtMultimedia.

AUDIOBOOK_DRIVER = textwrap.dedent(
    """
    import gc
    import json
    import sys
    import time

    from PySide6.QtCore import (
        Q_ARG,
        Property,
        QObject,
        QThread,
        QTimer,
        Signal,
        Slot,
    )
    from PySide6.QtQml import QQmlApplicationEngine

    from vienetts_app.app import create_app

    tmp = sys.argv[1]
    scenarios = sys.argv[2].split(",")

    GROUPS = [
        {
            "label": "Bắc",
            "voices": [{"id": "Minh Đức", "label": "Minh Đức — Nam · Bắc · tin tức"}],
        }
    ]


    class FakeAppController(QObject):
        voicesChanged = Signal()
        busyChanged = Signal()
        errorTextChanged = Signal()
        # create_app connects this for the live language swap.
        languageChanged = Signal()
        modelsMissingChanged = Signal()

        # REAL property (not a plain attr): Main.qml's models-missing scrim
        # binds controller.modelsMissing, and an undefined read leaves the
        # overlay at its default visible=true — eating every real mouse
        # event below it. False keeps the scrim out of the way.
        _models_missing = False

        @Property(bool, notify=modelsMissingChanged)
        def modelsMissing(self):
            return self._models_missing

        # Plain attrs for the Main.qml/other-tab bindings this scenario never
        # drives (undefined reads would spam warnings, not crash).
        audioAvailable = True
        streamActive = False
        streamLevel = 0.0
        hasAudio = False
        lastExportPath = ""
        progress = 0.0
        needsRestart = False
        consentGiven = False
        # create_app reads this off any controller (translator install).
        appliedLanguage = "vi"
        speed = 1.0
        silenceP = 0.15

        def __init__(self):
            super().__init__()
            self._voices = GROUPS
            self._busy = False
            self._error = ""
            self.defaultVoice = "Minh Đức"
            self.file_playback = None

        def attach_file_playback(self, playback):
            # create_app wires the temp-file replay player onto any
            # controller (RAM replay, other session's feature); the fake
            # just records the seam.
            self.file_playback = playback

        @Property("QVariantList", notify=voicesChanged)
        def voices(self):
            return self._voices

        @Property(bool, notify=busyChanged)
        def busy(self):
            return self._busy

        @busy.setter
        def busy(self, value):
            self._busy = bool(value)
            self.busyChanged.emit()

        @Property(str, notify=errorTextChanged)
        def errorText(self):
            return self._error


    class FakeAudiobook(QObject):
        booksChanged = Signal()
        currentBookIdChanged = Signal()
        currentBookTitleChanged = Signal()
        currentBookAuthorChanged = Signal()
        chaptersChanged = Signal()
        currentChapterChanged = Signal()
        playerStateChanged = Signal()
        positionMsChanged = Signal()
        durationMsChanged = Signal()
        chapterEnvelopeChanged = Signal()
        renderProgressChanged = Signal()
        renderingIndexChanged = Signal()
        autoAdvanceChanged = Signal()
        renderVoiceChanged = Signal()
        errorTextChanged = Signal()
        readerOpenChanged = Signal()
        paragraphsChanged = Signal()
        activeParagraphChanged = Signal()
        activeSpanChanged = Signal()
        syncAvailableChanged = Signal()
        renderEtaMsChanged = Signal()
        renderAllTotalChanged = Signal()
        renderAllDoneChanged = Signal()

        def __init__(self):
            super().__init__()
            self._books = []
            self._current_book_id = ""
            self._current_book_title = ""
            self._current_book_author = ""
            self._chapters = []
            self._current_chapter = -1
            self._player_state = "stopped"
            self._position_ms = 0
            self._duration_ms = 0
            self._chapter_envelope = []
            self._render_progress = 0.0
            self._rendering_index = -1
            self._auto_advance = True
            self._render_voice = ""
            self._error_text = ""
            self._reader_open = False
            self._paragraphs = []
            self._active_paragraph = -1
            self._active_char_start = -1
            self._active_char_end = -1
            self._sync_available = False
            self._render_eta_ms = -1
            self._render_all_total = 0
            self._render_all_done = 0
            self.hits = []

        @Property("QVariantList", notify=booksChanged)
        def books(self):
            return self._books

        @Property(str, notify=currentBookIdChanged)
        def currentBookId(self):
            return self._current_book_id

        @Property(str, notify=currentBookTitleChanged)
        def currentBookTitle(self):
            return self._current_book_title

        @Property(str, notify=currentBookAuthorChanged)
        def currentBookAuthor(self):
            return self._current_book_author

        @Property("QVariantList", notify=chaptersChanged)
        def chapters(self):
            return self._chapters

        @Property(int, notify=currentChapterChanged)
        def currentChapterIndex(self):
            return self._current_chapter

        @Property(str, notify=playerStateChanged)
        def playerState(self):
            return self._player_state

        @Property(int, notify=positionMsChanged)
        def positionMs(self):
            return self._position_ms

        @Property(int, notify=durationMsChanged)
        def durationMs(self):
            return self._duration_ms

        @Property("QVariantList", notify=chapterEnvelopeChanged)
        def chapterEnvelope(self):
            return self._chapter_envelope

        @chapterEnvelope.setter
        def chapterEnvelope(self, value):
            buckets = list(value)
            if buckets != self._chapter_envelope:
                self._chapter_envelope = buckets
                self.chapterEnvelopeChanged.emit()

        @Property(float, notify=renderProgressChanged)
        def renderProgress(self):
            return self._render_progress

        @Property(int, notify=renderingIndexChanged)
        def renderingIndex(self):
            return self._rendering_index

        @Property(str, notify=renderVoiceChanged)
        def renderVoice(self):
            return self._render_voice

        @Property(str, notify=errorTextChanged)
        def errorText(self):
            return self._error_text

        @Property(bool, notify=autoAdvanceChanged)
        def autoAdvance(self):
            return self._auto_advance

        @autoAdvance.setter
        def autoAdvance(self, value):
            self._auto_advance = bool(value)
            self.autoAdvanceChanged.emit()

        @Property(bool, notify=readerOpenChanged)
        def readerOpen(self):
            return self._reader_open

        @readerOpen.setter
        def readerOpen(self, value):
            self._reader_open = bool(value)
            self.readerOpenChanged.emit()

        @Property("QVariantList", notify=paragraphsChanged)
        def paragraphs(self):
            return self._paragraphs

        @Property(int, notify=activeParagraphChanged)
        def activeParagraph(self):
            return self._active_paragraph

        @Property(int, notify=activeSpanChanged)
        def activeCharStart(self):
            return self._active_char_start

        @Property(int, notify=activeSpanChanged)
        def activeCharEnd(self):
            return self._active_char_end

        @Property(bool, notify=syncAvailableChanged)
        def syncAvailable(self):
            return self._sync_available

        @Property(int, notify=renderEtaMsChanged)
        def renderEtaMs(self):
            return self._render_eta_ms

        @Property(int, notify=renderAllTotalChanged)
        def renderAllTotal(self):
            return self._render_all_total

        @Property(int, notify=renderAllDoneChanged)
        def renderAllDone(self):
            return self._render_all_done

        @Slot(str, result=bool)
        def openEpub(self, path):
            self.hits.append(["openEpub", str(path)])
            return True

        @Slot(str, result=bool)
        def openBook(self, book_id):
            self.hits.append(["openBook", str(book_id)])
            return True

        @Slot(str)
        def removeBook(self, book_id):
            self.hits.append(["removeBook", str(book_id)])

        @Slot(int)
        def playChapter(self, index):
            self.hits.append(["playChapter", int(index)])

        @Slot()
        def pause(self):
            self.hits.append(["pause"])

        @Slot()
        def resume(self):
            self.hits.append(["resume"])

        @Slot(int)
        def seek(self, ms):
            self.hits.append(["seek", int(ms)])

        @Slot()
        def prevChapter(self):
            self.hits.append(["prevChapter"])

        @Slot()
        def nextChapter(self):
            self.hits.append(["nextChapter"])

        @Slot(int)
        def renderChapter(self, index):
            self.hits.append(["renderChapter", int(index)])

        @Slot()
        def renderAllPending(self):
            self.hits.append(["renderAllPending"])

        @Slot()
        def cancelRender(self):
            self.hits.append(["cancelRender"])

        @Slot(str, result=int)
        def exportAllReady(self, dest_dir):
            self.hits.append(["exportAllReady", str(dest_dir)])
            return 0

        @Slot(int)
        def seekToParagraph(self, index):
            self.hits.append(["seekToParagraph", int(index)])

        @Slot(result=bool)
        def copyChapter(self):
            self.hits.append(["copyChapter"])
            return True


    results = {}
    for scenario in scenarios:
        out = {"scenario": scenario}
        fake_ab = FakeAudiobook()
        fake_app = FakeAppController()

        app, engine = create_app(
            controller_factory=lambda: fake_app,
            playback_factory=lambda: QObject(),
            audiobook_factory=lambda controller: fake_ab,
        )
        window = engine.rootObjects()[0]
        # StackLayout instantiates every tab; bindings only settle once the tab
        # is CURRENT (same rule as the paragraph/cloning scenarios).
        bridge = engine.rootContext().contextProperty("bridge")
        bridge.setCurrentTab("audiobook")
        app.processEvents()
        ab_tab = [o for o in window.findChildren(QObject) if o.objectName() == "audiobookTab"][0]

        def afind(name):
            return [o for o in ab_tab.findChildren(QObject) if o.objectName() == name]

        def item_walk(root):
            out_items, stack = [], [root]
            while stack:
                it = stack.pop()
                out_items.append(it)
                stack.extend(it.childItems())
            return out_items

        def ifind(name):
            content = ab_tab.property("contentItem")
            return [i for i in item_walk(content) if i.objectName() == name]

        def wait_ms(ms):
            for _ in range(ms // 50):
                QThread.msleep(50)
                app.processEvents()

        if scenario == "ab_load":
            names = {o.objectName() for o in ab_tab.findChildren(QObject)}
            names.add(ab_tab.objectName())
            expected = [
                "audiobookTab", "addEpubButton", "epubDialog", "shelfEmptyLabel",
                "audiobookBookCard", "chapterList", "renderAllButton",
                "exportAllButton", "autoAdvanceToggle", "voicePicker",
                "prevChapterButton", "playPauseButton", "nextChapterButton",
                "readerToggleButton", "readerCard", "readerView",
                "renderEtaLabel", "renderAllProgressBar", "renderAllProgressLabel",
                "positionLabel", "durationLabel", "seekSlider",
                "chapterWaveform",
                "audiobookErrorBanner", "audiobookErrorLabel", "playerDock",
                "readerCloseButton",
            ]
            out["objectnames"] = sorted(expected)
            out["missing"] = [n for n in expected if n not in names]
            out["shelf_empty_visible"] = bool(afind("shelfEmptyLabel")[0].property("visible"))
            out["book_card_hidden"] = not bool(afind("audiobookBookCard")[0].property("visible"))
            docks = afind("playerDock")
            out["dock_found"] = len(docks)
            out["dock_hidden_no_book"] = len(docks) == 1 and not bool(docks[0].property("visible"))
            nav_labels = [
                str(i.property("text"))
                for i in item_walk(window.property("contentItem"))
                if i.objectName() == "" and str(i.property("text") or "") == "Sách nói"
            ]
            out["nav_label_present"] = len(nav_labels) >= 1
        elif scenario == "ab_book":
            fake_ab._books = [{
                "id": "abc123", "title": "Sách thử nghiệm",
                "author": "Tác Giả A", "chapterCount": 3,
            }]
            fake_ab._current_book_id = "abc123"
            fake_ab._current_book_title = "Sách thử nghiệm"
            fake_ab._current_book_author = "Tác Giả A"
            fake_ab._chapters = [
                {"index": 0, "title": "Chương một", "chars": 61, "status": "ready",
                 "error": "", "current": True, "ready": True},
                {"index": 1, "title": "Chương hai", "chars": 95, "status": "pending",
                 "error": "", "current": False, "ready": False},
                {"index": 2, "title": "Chương 3", "chars": 84, "status": "failed",
                 "error": "engine exploded", "current": False, "ready": False},
            ]
            fake_ab._current_chapter = 0
            fake_ab.booksChanged.emit()
            fake_ab.currentBookIdChanged.emit()
            fake_ab.currentBookTitleChanged.emit()
            fake_ab.currentBookAuthorChanged.emit()
            fake_ab.chaptersChanged.emit()
            fake_ab.currentChapterChanged.emit()
            app.processEvents()
            out["book_card_visible"] = bool(afind("audiobookBookCard")[0].property("visible"))
            out["chapter_rows"] = len(ifind("chapterRow"))
            out["shelf_rows"] = len(ifind("shelfRow"))
            out["shelf_empty_hidden"] = not bool(afind("shelfEmptyLabel")[0].property("visible"))
            badges = ifind("chapterStatusBadge")
            out["status_badges"] = len(badges)
            out["error_chips"] = len([c for c in ifind("chapterErrorLabel")
                                      if c.property("visible")])
            out["render_buttons"] = len([b for b in ifind("chapterRenderButton")
                                         if b.property("visible")])
            out["prev_enabled"] = bool(afind("prevChapterButton")[0].property("enabled"))
            out["auto_toggle_control_kind"] = afind("autoAdvanceToggle")[0].property("controlKind")
            out["seek_control_kind"] = afind("seekSlider")[0].property("controlKind")
            out["transport_icons"] = [
                afind("prevChapterButton")[0].property("iconKind"),
                afind("playPauseButton")[0].property("iconKind"),
                afind("nextChapterButton")[0].property("iconKind"),
            ]
            out["batch_icons"] = [
                afind("exportAllButton")[0].property("iconKind"),
                afind("renderAllButton")[0].property("iconKind"),
            ]
        elif scenario == "ab_waveform":
            from PySide6.QtCore import QMetaObject

            # Book open + chapter current (same setup as ab_book, trimmed).
            fake_ab._books = [{
                "id": "abc123", "title": "Sách thử nghiệm",
                "author": "Tác Giả A", "chapterCount": 3,
            }]
            fake_ab._current_book_id = "abc123"
            fake_ab._current_book_title = "Sách thử nghiệm"
            fake_ab._chapters = [
                {"index": 0, "title": "Chương một", "chars": 61, "status": "ready",
                 "error": "", "current": True, "ready": True},
            ]
            fake_ab._current_chapter = 0
            for sig in (fake_ab.booksChanged, fake_ab.currentBookIdChanged,
                        fake_ab.chaptersChanged, fake_ab.currentChapterChanged):
                sig.emit()
            app.processEvents()

            wv = afind("chapterWaveform")[0]

            # No envelope yet → the transport shows no waveform row.
            out["hidden_without_envelope"] = not wv.property("visible")

            # Playing with an envelope: visible, mirrors every binding, seekable.
            fake_ab.chapterEnvelope = [0.3, 0.8, 1.0, 0.55, 0.2]
            fake_ab._player_state = "playing"
            fake_ab._duration_ms = 100_000
            fake_ab._position_ms = 25_000
            for sig in (fake_ab.chapterEnvelopeChanged, fake_ab.playerStateChanged,
                        fake_ab.durationMsChanged, fake_ab.positionMsChanged):
                sig.emit()
            app.processEvents()
            out["visible_with_envelope"] = bool(wv.property("visible"))
            out["bucket_count"] = int(wv.property("bucketCount"))
            out["position_bound"] = float(wv.property("position"))
            out["duration_bound"] = int(wv.property("durationMs"))
            out["active_while_playing"] = bool(wv.property("active"))
            out["seekable_while_playing"] = bool(wv.property("seekable"))

            # seekRequested (the widget's click path) routes to audiobook.seek.
            before = len(fake_ab.hits)
            QMetaObject.invokeMethod(
                wv, "seekRequested", Q_ARG("double", 0.5)
            )
            app.processEvents()
            seeks = [h for h in fake_ab.hits[before:] if h[0] == "seek"]
            out["seek_routed"] = seeks == [["seek", 50_000]]

            # REAL mouse path: a click on the canvas at ~40% width must seek to
            # 40% of the duration (guards the handler-scoped `mouse` usage).
            from PySide6.QtCore import QPoint, QPointF, Qt
            from PySide6.QtQuick import QQuickItem
            from PySide6.QtTest import QTest

            # findChildren(QObject) yields untyped wrappers (no width/mapToScene);
            # the QQuickItem-typed lookup gives the real geometry API.
            wv_item = next(
                o for o in ab_tab.findChildren(QQuickItem)
                if o.objectName() == "chapterWaveform"
            )

            def widget_point(fraction, local_y=10.0):
                scene = wv_item.mapToScene(
                    QPointF(wv_item.width() * fraction, local_y)
                )
                return QPoint(int(scene.x()), int(scene.y()))

            before = len(fake_ab.hits)
            QTest.mouseClick(
                window, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier, widget_point(0.4),
            )
            app.processEvents()
            seeks = [h for h in fake_ab.hits[before:] if h[0] == "seek"]
            # 40% of the widget width maps to ~40% of the chapter (a sub-1%
            # inset from the canvas margins); exact mapping is pinned by the
            # seekRequested assertion above — here it's the MOUSE path that
            # must deliver.
            out["click_seek_routed"] = (
                len(seeks) == 1
                and abs(seeks[0][1] - 40_000) <= 1_000
            )

            # Drag scrubbing: press at 10%, drag to 70%, release — the LAST seek
            # lands at the release point.
            p_start, p_end = widget_point(0.1), widget_point(0.7)
            before = len(fake_ab.hits)
            QTest.mousePress(
                window, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier, p_start,
            )
            QTest.mouseMove(window, p_end)
            QTest.mouseRelease(
                window, Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier, p_end,
            )
            app.processEvents()
            seeks = [h for h in fake_ab.hits[before:] if h[0] == "seek"]
            out["drag_scrub_final"] = (
                bool(seeks)
                and abs(seeks[-1][1] - 70_000) <= 1_000
            )

            # Paused: overview stays with the playhead, still seekable; stopped
            # hides the playhead glow but the shape remains (envelope present).
            fake_ab._player_state = "paused"
            fake_ab.playerStateChanged.emit()
            app.processEvents()
            out["seekable_while_paused"] = bool(wv.property("seekable"))
            fake_ab._player_state = "stopped"
            fake_ab.playerStateChanged.emit()
            app.processEvents()
            out["inactive_when_stopped"] = not wv.property("active")
            out["visible_when_stopped"] = bool(wv.property("visible"))
        elif scenario == "ab_render_progress":
            from PySide6.QtCore import QMetaObject

            # 12-chapter book, chapter index 9 rendering at 42% — index 9 sits
            # well below the fold (list starts ~y500 in a 740px window), so the
            # on-screen assertions prove the auto-scroll, not just placement.
            fake_ab._books = [{
                "id": "abc123", "title": "Sách thử nghiệm",
                "author": "Tác Giả A", "chapterCount": 12,
            }]
            fake_ab._current_book_id = "abc123"
            fake_ab._current_book_title = "Sách thử nghiệm"
            fake_ab._current_book_author = "Tác Giả A"
            fake_ab._chapters = [
                {"index": i, "title": f"Chương {i + 1}", "chars": 4200,
                 "status": "rendering" if i == 9 else ("ready" if i < 9 else "pending"),
                 "error": "", "current": i == 9, "ready": i < 9}
                for i in range(12)
            ]
            fake_ab._current_chapter = 9
            fake_ab._rendering_index = 9
            fake_ab._render_progress = 0.42
            for sig in (fake_ab.booksChanged, fake_ab.currentBookIdChanged,
                        fake_ab.currentBookTitleChanged, fake_ab.currentBookAuthorChanged,
                        fake_ab.chaptersChanged, fake_ab.currentChapterChanged,
                        fake_ab.renderingIndexChanged, fake_ab.renderProgressChanged):
                sig.emit()
            app.processEvents()
            wait_ms(200)

            def scene_y(item):
                return item.mapToItem(window.property("contentItem"), 0, 0).y()

            # "On screen" = inside the CHAPTER LIST viewport — the band the
            # app's positionViewAtIndex(Contain) actually scrolls into. Offscreen
            # font metrics differ per OS and can push the whole list below the
            # fixed 1120x740 window fold on CI; the list-viewport contract is
            # the font-stable one (and the one the QML controls).
            chapter_list = ifind("chapterList")[0]

            def list_y(item):
                return item.mapToItem(chapter_list, 0, 0).y()

            # Inline per-chapter progress: exactly one visible bar, on the
            # rendering row, reflecting the live fraction.
            bars = [b for b in ifind("chapterProgressBar") if b.property("visible")]
            out["inline_bars_visible"] = len(bars)
            out["inline_bar_value"] = round(float(bars[0].property("value")), 2) if bars else None
            out["inline_bar_on_screen"] = (
                bool(bars) and 0 <= list_y(bars[0]) < chapter_list.height()
            )
            # Inline stop: exactly one visible, on the rendering row, and it
            # routes to cancelRender.
            stops = [s for s in ifind("chapterStopButton") if s.property("visible")]
            out["stop_buttons_visible"] = len(stops)
            out["stop_on_screen"] = (
                bool(stops) and 0 <= list_y(stops[0]) < chapter_list.height()
            )
            if stops:
                QMetaObject.invokeMethod(stops[0], "click")
                app.processEvents()
            # Global row: visible and placed ABOVE the chapter list now.
            gbar = ifind("renderProgressBar")
            out["global_row_visible"] = len(gbar) == 1 and bool(gbar[0].property("visible"))
            out["global_above_list"] = (
                len(gbar) == 1 and scene_y(gbar[0]) < scene_y(afind("chapterList")[0])
            )
            out["global_bar_value"] = round(float(gbar[0].property("value")), 2)
            # Rendering row's render button must be hidden (replaced by stop);
            # other pending rows keep theirs (but disabled while busy).
            render_btns = [b for b in ifind("chapterRenderButton") if b.property("visible")]
            out["render_buttons_visible"] = len(render_btns)
            # Cancelled/idle reset: everything retreats.
            fake_ab._rendering_index = -1
            fake_ab._render_progress = 0.0
            fake_ab._chapters[9]["status"] = "pending"
            fake_ab._chapters[9]["current"] = False
            fake_ab.renderingIndexChanged.emit()
            fake_ab.renderProgressChanged.emit()
            fake_ab.chaptersChanged.emit()
            app.processEvents()
            out["idle_inline_bars"] = len([b for b in ifind("chapterProgressBar")
                                           if b.property("visible")])
            out["idle_stop_buttons"] = len([s for s in ifind("chapterStopButton")
                                            if s.property("visible")])
            out["idle_global_visible"] = len([b for b in ifind("renderProgressBar")
                                              if b.property("visible")])
            out["hits"] = fake_ab.hits
        elif scenario == "ab_interact":
            from PySide6.QtCore import QMetaObject, Q_ARG

            fake_ab._books = [{
                "id": "abc123", "title": "Sách thử nghiệm",
                "author": "Tác Giả A", "chapterCount": 2,
            }]
            fake_ab._current_book_id = "abc123"
            fake_ab._current_book_title = "Sách thử nghiệm"
            fake_ab._current_book_author = "Tác Giả A"
            fake_ab._chapters = [
                {"index": 0, "title": "Chương một", "chars": 61, "status": "ready",
                 "error": "", "current": False, "ready": True},
                {"index": 1, "title": "Chương hai", "chars": 95, "status": "pending",
                 "error": "", "current": False, "ready": False},
            ]
            fake_ab._current_chapter = 0
            for sig in (fake_ab.booksChanged, fake_ab.currentBookIdChanged,
                        fake_ab.currentBookTitleChanged, fake_ab.currentBookAuthorChanged,
                        fake_ab.chaptersChanged, fake_ab.currentChapterChanged):
                sig.emit()
            app.processEvents()
            # Click a chapter row → playChapter(index). item_walk order is
            # arbitrary, so pick the delegate whose model index is 1.
            rows = ifind("chapterRow")
            out["rows"] = len(rows)

            def model_index(item):
                md = item.property("modelData")
                return int(md.get("index", -1)) if isinstance(md, dict) else -1

            target = next((r for r in rows if model_index(r) == 1), None)
            out["target_found"] = target is not None
            if target is not None:
                QMetaObject.invokeMethod(target, "playRow")
                app.processEvents()
            # Play/pause button: state paused → resume path
            fake_ab._player_state = "paused"
            fake_ab.playerStateChanged.emit()
            app.processEvents()
            QMetaObject.invokeMethod(afind("playPauseButton")[0], "click")
            app.processEvents()
            # Render the pending chapter via its inline button (the READY row's
            # button is hidden — pick a visible one).
            btns = [b for b in ifind("chapterRenderButton") if b.property("visible")]
            out["render_btns"] = len(btns)
            if btns:
                QMetaObject.invokeMethod(btns[0], "click")
                app.processEvents()
            # Toggle auto-advance off (click, like every other control here —
            # `toggle` is not reliably invokable through the metaobject).
            QMetaObject.invokeMethod(afind("autoAdvanceToggle")[0], "click")
            app.processEvents()
            out["auto_advance_after"] = fake_ab.autoAdvance
            out["hits"] = fake_ab.hits
        elif scenario == "ab_reader":
            from PySide6.QtCore import QMetaObject

            fake_ab._books = [{
                "id": "abc123", "title": "Sách thử nghiệm",
                "author": "Tác Giả A", "chapterCount": 2,
            }]
            fake_ab._current_book_id = "abc123"
            fake_ab._current_book_title = "Sách thử nghiệm"
            fake_ab._current_book_author = "Tác Giả A"
            fake_ab._chapters = [
                {"index": 0, "title": "Chương một", "chars": 61, "status": "ready",
                 "error": "", "current": True, "ready": True},
                {"index": 1, "title": "Chương hai", "chars": 95, "status": "pending",
                 "error": "", "current": False, "ready": False},
            ]
            fake_ab._current_chapter = 0
            for sig in (fake_ab.booksChanged, fake_ab.currentBookIdChanged,
                        fake_ab.currentBookTitleChanged, fake_ab.currentBookAuthorChanged,
                        fake_ab.chaptersChanged, fake_ab.currentChapterChanged):
                sig.emit()
            app.processEvents()
            # Hidden until the user asks for it.
            out["reader_hidden_before"] = not bool(afind("readerCard")[0].property("visible"))
            QMetaObject.invokeMethod(afind("readerToggleButton")[0], "click")
            app.processEvents()
            out["reader_open_after_toggle"] = fake_ab._reader_open
            out["reader_visible_after"] = bool(afind("readerCard")[0].property("visible"))
            # Two paragraphs, karaoke word on paragraph 2 (chars 11..14).
            fake_ab._paragraphs = [
                {"index": 0, "text": "Câu một.", "charStart": 0, "charEnd": 8},
                {"index": 1, "text": "Câu hai.", "charStart": 10, "charEnd": 18},
            ]
            fake_ab._active_paragraph = 1
            fake_ab._active_char_start = 11
            fake_ab._active_char_end = 14
            fake_ab._sync_available = True
            for sig in (fake_ab.paragraphsChanged, fake_ab.activeParagraphChanged,
                        fake_ab.activeSpanChanged, fake_ab.syncAvailableChanged):
                sig.emit()
            app.processEvents()
            wait_ms(150)
            paras = ifind("readerParagraph")
            out["paragraphs"] = len(paras)
            # item_walk order is arbitrary — pair each row with ITS text child.
            out["rows"] = [
                {
                    "active": bool(p.property("isActive")),
                    # TextEdit re-serializes rich text, so the karaoke
                    # <b><font color> span surfaces as a font-weight:700
                    # style rather than a literal <b> tag.
                    "bold": bool(
                        "font-weight:700"
                        in str(
                            next(
                                (c.property("text") for c in p.childItems()
                                 if c.objectName() == "readerText"),
                                "",
                            )
                        )
                    ),
                }
                for p in paras
            ]
            active_rows = [p for p in paras if bool(p.property("isActive"))]
            out["active_rows"] = len(active_rows)
            out["active_row_opaque"] = (
                len(active_rows) == 1
                and int(active_rows[0].property("color").alpha()) == 255
            )
            if active_rows:
                QMetaObject.invokeMethod(active_rows[0], "seekHere")
                app.processEvents()
            # Snapshot: the REAL-mouse checks below keep appending to
            # fake_ab.hits — without a copy they'd leak into this list.
            out["hits"] = list(fake_ab.hits)

            # One-tap chapter copy: header button → audiobook.copyChapter.
            copy_btn = afind("readerCopyButton")
            out["copy_button_found"] = len(copy_btn)
            out["copy_button_visible"] = (
                bool(copy_btn) and bool(copy_btn[0].property("visible"))
            )
            if copy_btn:
                QMetaObject.invokeMethod(copy_btn[0], "click")
                app.processEvents()
            out["copy_chapter_hit"] = fake_ab.hits[-1:] == [["copyChapter"]]

            # ── Select/copy without editing: the transcript must be a
            # read-only, mouse-selectable TextEdit. Clean REAL clicks still
            # seek (guards the MouseArea → TapHandler swap), a REAL drag
            # selects, copy() reaches the clipboard, typed keys change
            # nothing, and the focused paragraph never shadows the
            # transport shortcuts.
            from PySide6.QtCore import QPoint, QPointF
            from PySide6.QtGui import QGuiApplication
            from PySide6.QtTest import QTest
            from PySide6.QtCore import Qt

            rows = ifind("readerParagraph")

            def text_child(row):
                return next(
                    (c for c in row.childItems() if c.objectName() == "readerText"),
                    None,
                )

            first_row, first_text = None, None
            for r in rows:
                t = text_child(r)
                if t is not None and "Câu một" in str(t.property("text")):
                    first_row, first_text = r, t
                    break
            out["select_by_mouse"] = bool(first_text.property("selectByMouse"))
            out["read_only"] = bool(first_text.property("readOnly"))

            def row_point(fx, fy=0.5):
                scene = first_row.mapToScene(
                    QPointF(first_row.width() * fx, first_row.height() * fy)
                )
                return QPoint(int(scene.x()), int(scene.y()))

            def text_point(fx, fy=0.5):
                # Points INSIDE the text editor (it is inset by its margins).
                # Clamp x to ≥1px so fx=0 stays inside the item; a position
                # left of the first glyph maps to cursor offset 0.
                scene = first_text.mapToScene(
                    QPointF(max(1.0, first_text.width() * fx),
                            first_text.height() * fy)
                )
                return QPoint(int(scene.x()), int(scene.y()))

            # Clean REAL click mid-paragraph: still seeks.
            before = len(fake_ab.hits)
            QTest.mouseClick(window, Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.NoModifier, row_point(0.5))
            app.processEvents()
            out["click_seek"] = fake_ab.hits[before:] == [["seekToParagraph", 0]]

            # REAL drag across the paragraph: selects the text.
            QTest.mousePress(window, Qt.MouseButton.LeftButton,
                             Qt.KeyboardModifier.NoModifier, text_point(0.0))
            QTest.mouseMove(window, text_point(0.98))
            QTest.mouseRelease(window, Qt.MouseButton.LeftButton,
                               Qt.KeyboardModifier.NoModifier, text_point(0.98))
            app.processEvents()
            out["active_focus_after_drag"] = bool(first_text.property("activeFocus"))
            out["drag_selected"] = str(first_text.property("selectedText"))
            QGuiApplication.clipboard().clear()
            QMetaObject.invokeMethod(first_text, "copy")
            app.processEvents()
            out["clipboard_after_copy"] = QGuiApplication.clipboard().text()

            # Typed keys land nowhere (readOnly); Space/← still reach the
            # window shortcuts despite the paragraph holding focus.
            # (keyClicks is QWidget-only; QWindow takes per-key clicks.)
            for ch in "ZZ":
                QTest.keyClick(window, Qt.Key(ord(ch)))
            app.processEvents()
            out["text_unchanged_after_keys"] = (
                "ZZ" not in str(first_text.property("text"))
            )
            fake_ab._player_state = "playing"
            fake_ab.playerStateChanged.emit()
            app.processEvents()
            before = len(fake_ab.hits)
            QTest.keyClick(window, Qt.Key.Key_Space)
            app.processEvents()
            QTest.keyClick(window, Qt.Key.Key_Left)
            app.processEvents()
            out["transport_hits_while_focused"] = fake_ab.hits[before:]
        elif scenario == "ab_render_all":
            fake_ab._books = [{
                "id": "abc123", "title": "Sách thử nghiệm",
                "author": "Tác Giả A", "chapterCount": 6,
            }]
            fake_ab._current_book_id = "abc123"
            fake_ab._current_book_title = "Sách thử nghiệm"
            fake_ab._current_book_author = "Tác Giả A"
            fake_ab._chapters = [
                {"index": i, "title": f"Chương {i + 1}", "chars": 900,
                 "status": "ready" if i < 2 else "pending",
                 "error": "", "current": i == 2, "ready": i < 2}
                for i in range(6)
            ]
            fake_ab._current_chapter = 2
            fake_ab._rendering_index = 2
            fake_ab._render_progress = 0.4
            fake_ab._render_eta_ms = 80_000
            fake_ab._render_all_total = 5
            fake_ab._render_all_done = 2
            for sig in (fake_ab.booksChanged, fake_ab.currentBookIdChanged,
                        fake_ab.currentBookTitleChanged, fake_ab.currentBookAuthorChanged,
                        fake_ab.chaptersChanged, fake_ab.currentChapterChanged,
                        fake_ab.renderingIndexChanged, fake_ab.renderProgressChanged,
                        fake_ab.renderEtaMsChanged, fake_ab.renderAllTotalChanged,
                        fake_ab.renderAllDoneChanged):
                sig.emit()
            app.processEvents()
            rows = ifind("renderAllRow")
            out["row_found"] = len(rows)
            out["row_visible"] = len(rows) == 1 and bool(rows[0].property("visible"))
            bars = ifind("renderAllProgressBar")
            out["bar_value"] = round(float(bars[0].property("value")), 2) if bars else None
            out["label_text"] = str(ifind("renderAllProgressLabel")[0].property("text"))
            out["eta_visible"] = bool(afind("renderEtaLabel")[0].property("visible"))
            out["eta_text"] = str(afind("renderEtaLabel")[0].property("text"))
            # Idle: the overall row retreats with the per-chapter row.
            fake_ab._rendering_index = -1
            fake_ab.renderingIndexChanged.emit()
            app.processEvents()
            out["idle_row_visible"] = len(rows) == 1 and bool(rows[0].property("visible"))
        elif scenario == "ab_dock":
            from PySide6.QtCore import QMetaObject

            # Geometry helper: an item's rect in audiobookTab coordinates (the
            # dock/overlay are siblings of the page shell, so their placement
            # relative to the TAB is the contract, not placement in the column).
            def rect_in_tab(item):
                pos = item.mapToItem(ab_tab, 0, 0)
                return (
                    pos.x(),
                    pos.y(),
                    float(item.property("width")),
                    float(item.property("height")),
                )

            docks = afind("playerDock")
            out["dock_found"] = len(docks)
            out["dock_hidden_no_book"] = len(docks) == 1 and not bool(docks[0].property("visible"))

            fake_ab._books = [{
                "id": "abc123", "title": "Sách thử nghiệm",
                "author": "Tác Giả A", "chapterCount": 2,
            }]
            fake_ab._current_book_id = "abc123"
            fake_ab._current_book_title = "Sách thử nghiệm"
            fake_ab._current_book_author = "Tác Giả A"
            fake_ab._chapters = [
                {"index": 0, "title": "Chương một", "chars": 61, "status": "ready",
                 "error": "", "current": True, "ready": True},
                {"index": 1, "title": "Chương hai", "chars": 95, "status": "pending",
                 "error": "", "current": False, "ready": False},
            ]
            fake_ab._current_chapter = 0
            for sig in (fake_ab.booksChanged, fake_ab.currentBookIdChanged,
                        fake_ab.currentBookTitleChanged, fake_ab.currentBookAuthorChanged,
                        fake_ab.chaptersChanged, fake_ab.currentChapterChanged):
                sig.emit()
            app.processEvents()
            wait_ms(150)

            dock = docks[0]
            tab_w = float(ab_tab.property("width"))
            tab_h = float(ab_tab.property("height"))
            pad = float(ab_tab.property("padding"))
            dx, dy, dw, dh = rect_in_tab(dock)
            out["dock_visible_with_book"] = bool(dock.property("visible"))
            # Pinned to the tab bottom, respecting the pane padding, and hosting
            # the whole transport (single instance of each control).
            out["dock_flush_bottom"] = abs((dy + dh) - (tab_h - pad)) <= 2
            out["dock_padded_width"] = abs(dw - (tab_w - 2 * pad)) <= 2
            out["transport_in_dock"] = (
                len(dock.findChildren(QObject, "seekSlider")) == 1
                and len(dock.findChildren(QObject, "playPauseButton")) == 1
                and len(dock.findChildren(QObject, "readerToggleButton")) == 1
            )
            out["reader_not_in_dock"] = len(dock.findChildren(QObject, "readerCard")) == 0

            # Reader overlay: hidden until asked, then fills the tab area ABOVE
            # the dock (full padded width) while the dock stays visible.
            out["reader_hidden_before"] = not bool(afind("readerCard")[0].property("visible"))
            QMetaObject.invokeMethod(afind("readerToggleButton")[0], "click")
            app.processEvents()
            out["reader_open_after_toggle"] = fake_ab._reader_open
            card = afind("readerCard")[0]
            out["reader_visible_after"] = bool(card.property("visible"))
            cx, cy, cw, ch = rect_in_tab(card)
            out["reader_padded_width"] = abs(cw - (tab_w - 2 * pad)) <= 2
            out["reader_sits_above_dock"] = 0 <= (dy - (cy + ch)) <= 20
            out["dock_still_visible"] = bool(dock.property("visible"))

            # The overlay's own close affordance retreats the reader.
            close_btns = afind("readerCloseButton")
            out["close_found"] = len(close_btns)
            if close_btns:
                QMetaObject.invokeMethod(close_btns[0], "click")
                app.processEvents()
            out["reader_closed_after_close"] = not bool(card.property("visible"))
            out["reader_state_closed"] = fake_ab._reader_open is False
            out["hits"] = fake_ab.hits
        elif scenario == "ab_export_url":
            from PySide6.QtCore import QMetaObject, QUrl

            # exportAllDialog.onAccepted routes through the root exportAllTo
            # seam: the folder URL must arrive decoded, with no stray slash
            # before a Windows drive letter (the toString().substring(7) bug
            # this dialog kept after the repo-wide toLocalPath fix).
            QMetaObject.invokeMethod(
                ab_tab, "exportAllTo",
                Q_ARG("QVariant", QUrl("file:///C:/Users/trung/Nh%E1%BA%A1c")),
            )
            QMetaObject.invokeMethod(
                ab_tab, "exportAllTo",
                Q_ARG("QVariant", QUrl("file:///home/u/VieNeuTTS%20Test")),
            )
            app.processEvents()
            out["hits"] = [list(h) for h in fake_ab.hits]
        QTimer.singleShot(50, app.quit)
        app.exec()
        if scenario == "ab_interact":
            out["hits"] = fake_ab.hits
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


def run_ab_driver(tmp_path, scenarios: list[str]) -> dict[str, dict]:
    # File-based like run_driver: `-c` blows the Windows command-line limit.
    driver_path = tmp_path / "_ab_driver.py"
    driver_path.write_text(AUDIOBOOK_DRIVER, encoding="utf-8")
    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        "PYTHONPATH": str(Path(__file__).resolve().parents[2]),
    }
    proc = subprocess.run(
        [sys.executable, str(driver_path), str(tmp_path), ",".join(scenarios)],
        capture_output=True,
        text=True,
        env=env,
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    (line,) = (ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:"))
    return json.loads(line.removeprefix("RESULT:"))


class TestAudiobookTabSmoke:
    def test_shelf_dock_book_render_and_export_url(self, tmp_path) -> None:
        results = run_ab_driver(tmp_path, ["ab_load", "ab_dock", "ab_book", "ab_export_url"])
        result = results["ab_load"]
        assert result["missing"] == []
        assert result["shelf_empty_visible"] is True
        assert result["book_card_hidden"] is True
        assert result["nav_label_present"] is True
        assert result["dock_found"] == 1
        assert result["dock_hidden_no_book"] is True

        result = results["ab_dock"]
        assert result["dock_found"] == 1
        assert result["dock_hidden_no_book"] is True
        assert result["dock_visible_with_book"] is True
        assert result["dock_flush_bottom"] is True
        assert result["dock_padded_width"] is True
        assert result["transport_in_dock"] is True
        assert result["reader_not_in_dock"] is True
        assert result["reader_hidden_before"] is True
        assert result["reader_open_after_toggle"] is True
        assert result["reader_visible_after"] is True
        assert result["reader_padded_width"] is True
        assert result["reader_sits_above_dock"] is True
        assert result["dock_still_visible"] is True
        assert result["close_found"] == 1
        assert result["reader_closed_after_close"] is True
        assert result["reader_state_closed"] is True

        result = results["ab_book"]
        assert result["book_card_visible"] is True
        assert result["chapter_rows"] == 3
        assert result["shelf_rows"] == 1
        assert result["shelf_empty_hidden"] is True
        assert result["status_badges"] == 3
        assert result["error_chips"] == 1
        # Render buttons only on pending/failed chapters — never on ready.
        assert result["render_buttons"] == 2
        assert result["prev_enabled"] is False  # current chapter is the first
        assert result["auto_toggle_control_kind"] == "toggle"
        assert result["seek_control_kind"] == "slider"
        assert result["transport_icons"] == ["previous", "play", "next"]
        assert result["batch_icons"] == ["download", "wave"]

        result = results["ab_export_url"]
        # Drive-letter slash stripped, diacritics decoded, space decoded.
        assert result["hits"] == [
            ["exportAllReady", "C:/Users/trung/Nhạc"],
            ["exportAllReady", "/home/u/VieNeuTTS Test"],
        ]

    def test_waveform_render_progress_interactions_and_render_all(self, tmp_path) -> None:
        results = run_ab_driver(
            tmp_path,
            ["ab_waveform", "ab_render_progress", "ab_interact", "ab_reader", "ab_render_all"],
        )
        result = results["ab_waveform"]
        # No envelope → no waveform row in the transport.
        assert result["hidden_without_envelope"] is True
        # Playing with an envelope: mirrors every controller binding.
        assert result["visible_with_envelope"] is True
        assert result["bucket_count"] == 5
        assert result["position_bound"] == pytest.approx(0.25)
        assert result["duration_bound"] == 100_000
        assert result["active_while_playing"] is True
        assert result["seekable_while_playing"] is True
        # The widget's click signal maps fraction → audiobook.seek(ms).
        assert result["seek_routed"] is True
        # The REAL mouse path works: click seeks, drag scrubs to release point.
        assert result["click_seek_routed"] is True
        assert result["drag_scrub_final"] is True
        # Pause keeps the playhead + seek; stop drops the glow, keeps the shape.
        assert result["seekable_while_paused"] is True
        assert result["inactive_when_stopped"] is True
        assert result["visible_when_stopped"] is True

        result = results["ab_render_progress"]
        # Inline per-chapter bar: one visible, live value, ON SCREEN.
        assert result["inline_bars_visible"] == 1
        assert result["inline_bar_value"] == pytest.approx(0.42, abs=0.01)
        assert result["inline_bar_on_screen"] is True
        # Inline stop button: one visible, on screen, routes to cancelRender.
        assert result["stop_buttons_visible"] == 1
        assert result["stop_on_screen"] is True
        hits = {h[0]: h[1:] for h in result["hits"]}
        assert hits["cancelRender"] == []
        # Global row: visible, ABOVE the chapter list, same live fraction.
        assert result["global_row_visible"] is True
        assert result["global_above_list"] is True
        assert result["global_bar_value"] == pytest.approx(0.42, abs=0.01)
        # The rendering row's render button is replaced by stop; the two
        # pending chapters after it keep theirs (visible = instantiated +
        # not ready — rows scrolled out of the viewport don't count).
        assert result["render_buttons_visible"] == 2
        # Idle reset: no inline bar, no stop, global row hidden.
        assert result["idle_inline_bars"] == 0
        assert result["idle_stop_buttons"] == 0
        assert result["idle_global_visible"] == 0

        result = results["ab_interact"]
        assert result["target_found"] is True
        hits = {h[0]: h[1:] for h in result["hits"]}
        assert hits["playChapter"] == [1]  # clicked the chapter with index 1
        assert hits["resume"] == []
        assert hits["renderChapter"] == [1]
        assert result["auto_advance_after"] is False

        result = results["ab_reader"]
        assert result["reader_hidden_before"] is True
        assert result["reader_open_after_toggle"] is True
        assert result["reader_visible_after"] is True
        assert result["paragraphs"] == 2
        # Karaoke word mark-up: exactly the ACTIVE paragraph's text has it.
        rows = result["rows"]
        assert [r["active"] for r in rows].count(True) == 1
        assert all(r["bold"] == r["active"] for r in rows)
        assert result["active_rows"] == 1
        assert result["active_row_opaque"] is True
        hits = {h[0]: h[1:] for h in result["hits"]}
        assert hits["seekToParagraph"] == [1]

        # One-tap chapter copy: button present and wired while the reader
        # is open (per-paragraph selection cannot span paragraphs, so the
        # whole-chapter export lives on one button).
        assert result["copy_button_found"] == 1
        assert result["copy_button_visible"] is True
        assert result["copy_chapter_hit"] is True

        # Select/copy without editing: read-only, mouse-selectable
        # transcript; a clean click still seeks; copy reaches the clipboard;
        # typed keys change nothing; the focused paragraph leaves the
        # Space/← transport shortcuts intact.
        assert result["select_by_mouse"] is True
        assert result["read_only"] is True
        assert result["click_seek"] is True
        assert result["active_focus_after_drag"] is True
        assert result["drag_selected"] == "Câu một."
        assert result["clipboard_after_copy"] == "Câu một."
        assert result["text_unchanged_after_keys"] is True
        assert result["transport_hits_while_focused"] == [["pause"], ["seek", 0]]

        result = results["ab_render_all"]
        assert result["row_found"] == 1
        assert result["row_visible"] is True
        assert result["bar_value"] == pytest.approx(0.4, abs=0.01)
        assert result["label_text"] == "Tổng: 2/5 chương"
        assert result["eta_visible"] is True
        assert result["eta_text"] == "còn ~1:20"
        assert result["idle_row_visible"] is False
