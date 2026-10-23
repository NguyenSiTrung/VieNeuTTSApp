"""Task 5.1: the extracted subtitle components instantiate outside the card.

The panels take their state through properties and read the shared
``subtitleController``/``EngineState``/``bridge`` context objects, so a stub
controller is enough to prove each file has no hidden dependency on the whole
studio (no ids borrowed from ``SubtitleCard.qml``).

Run in a subprocess: a QGuiApplication is per-process, like the smoke suite.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
QML_DIR = REPO_ROOT / "src" / "vienetts_app" / "ui" / "qml"
COMPONENTS = (
    "SubtitleSourcePanel.qml",
    "SubtitleCueEditor.qml",
    "SubtitlePreparePanel.qml",
    "SubtitleCastPanel.qml",
)

_DRIVER = textwrap.dedent(
    """
    import json
    import sys

    from PySide6.QtCore import Property, QObject, QUrl, Signal, Slot
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlComponent, QQmlEngine

    qml_dir, files = sys.argv[1], sys.argv[2:]


    class StubController(QObject):
        speakersChanged = Signal()
        cueModelChanged = Signal()
        activeCueChanged = Signal()
        aiProgressChanged = Signal()
        aiStageTextChanged = Signal()
        videoOverrunMsChanged = Signal()
        embedSubtitlesChanged = Signal()
        subtitleEmbedModeChanged = Signal()
        videoMixModeChanged = Signal()
        videoBgGainDbChanged = Signal()
        softSubtitlesSupportedChanged = Signal()

        def __init__(self):
            super().__init__()
            self._speakers = []
            self._active_cue = 0
            self._ai_progress = 0.0
            self._ai_stage = ""
            self._overrun = 0
            self._embed = True
            self._embed_mode = "soft"
            self._mix = "duck"
            self._gain = -16.0
            self._soft = True

        @Property("QVariant", notify=speakersChanged)
        def speakers(self):
            return self._speakers

        @Property("QVariant", notify=cueModelChanged)
        def cueModel(self):
            return []

        @Property(int, notify=activeCueChanged)
        def activeCue(self):
            return self._active_cue

        @Property(float, notify=aiProgressChanged)
        def aiProgress(self):
            return self._ai_progress

        @Property(str, notify=aiStageTextChanged)
        def aiStageText(self):
            return self._ai_stage

        @Property("QVariant")
        def profileLanguages(self):
            return [{"code": "en", "label": "English"}]

        @Property(bool, notify=softSubtitlesSupportedChanged)
        def softSubtitlesSupported(self):
            return self._soft

        @Property(int, notify=videoOverrunMsChanged)
        def videoOverrunMs(self):
            return self._overrun

        @Property(bool, notify=embedSubtitlesChanged)
        def embedSubtitles(self):
            return self._embed

        @embedSubtitles.setter
        def embedSubtitles(self, value):
            self._embed = bool(value)
            self.embedSubtitlesChanged.emit()

        @Property(str, notify=subtitleEmbedModeChanged)
        def subtitleEmbedMode(self):
            return self._embed_mode

        @subtitleEmbedMode.setter
        def subtitleEmbedMode(self, value):
            self._embed_mode = str(value)
            self.subtitleEmbedModeChanged.emit()

        @Property(str, notify=videoMixModeChanged)
        def videoMixMode(self):
            return self._mix

        @videoMixMode.setter
        def videoMixMode(self, value):
            self._mix = str(value)
            self.videoMixModeChanged.emit()

        @Property(float, notify=videoBgGainDbChanged)
        def videoBgGainDb(self):
            return self._gain

        @videoBgGainDb.setter
        def videoBgGainDb(self, value):
            self._gain = float(value)
            self.videoBgGainDbChanged.emit()

        @Slot()
        def runDiarization(self):
            pass

        @Slot(str)
        def playSpeakerSample(self, speaker_id):
            pass

        @Slot(str, str)
        def setSpeakerVoice(self, speaker_id, voice_id):
            pass

        @Slot(str)
        def cloneSpeakerFromVideo(self, speaker_id):
            pass

        @Slot(str, str)
        def refineWithLlm(self, mode, language):
            pass

        @Slot()
        def cancelAiTask(self):
            pass

        @Slot(int)
        def seekToCue(self, index):
            pass

        @Slot(int, str, str)
        def updateCue(self, index, text, speaker):
            return True

        @Slot(bool)
        def transcribeVideo(self, diarize):
            pass

        @Slot()
        def clearVideo(self):
            pass

        @Slot(str)
        def importSrt(self, path):
            pass

        @Slot(str)
        def importVideo(self, path):
            pass


    class StubEngineState(QObject):
        @Property(bool)
        def supportsCloning(self):
            return True


    class StubBridge(QObject):
        @Slot(str)
        def setCurrentTab(self, tab):
            pass


    app = QGuiApplication(sys.argv[:1])

    controller = StubController()
    engine = QQmlEngine()
    context = engine.rootContext()
    context.setContextProperty("subtitleController", controller)
    context.setContextProperty("EngineState", StubEngineState())
    context.setContextProperty("bridge", StubBridge())

    results = {}
    for path in files:
        component = QQmlComponent(engine, QUrl.fromLocalFile(path))
        item = component.create()
        created = item is not None
        loaded = False
        if created:
            item.setProperty("available", True)
            item.setProperty("shown", True)
            loaded = True
        results[path.rsplit("/", 1)[-1]] = {
            "created": created,
            "loaded": loaded,
            "errors": [str(err) for err in component.errors()],
        }
    print("RESULT:" + json.dumps(results))
    """
)


def test_extracted_subtitle_components_instantiate_standalone(tmp_path: Path) -> None:
    driver = tmp_path / "component_driver.py"
    driver.write_text(_DRIVER, encoding="utf-8")
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    proc = subprocess.run(
        [sys.executable, str(driver), str(QML_DIR)]
        + [str(QML_DIR / "components" / name) for name in COMPONENTS],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(REPO_ROOT),
    )
    payload = [line for line in proc.stdout.splitlines() if line.startswith("RESULT:")]
    assert proc.returncode == 0 and payload, (proc.stdout, proc.stderr)
    results = json.loads(payload[-1][len("RESULT:") :])
    for name in COMPONENTS:
        assert results[name]["created"], results[name]
        assert results[name]["loaded"], results[name]
        assert results[name]["errors"] == [], results[name]
