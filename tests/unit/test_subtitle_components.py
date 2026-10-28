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
    "SubtitleVideoPreview.qml",
    "SubtitleActionDock.qml",
)
# Composed pages instantiated in the same engine run (FR-1.1 gating checks).
PAGES = ("SubtitlesTab.qml",)

VIDEO_PREVIEW_OBJECT_NAMES = (
    "subtitleVideoPreview",
    "subtitleVideoViewport",
    "subtitleVideoCaptionOverlay",
    "subtitleVideoCaptionToggle",
    "subtitleVideoPlayButton",
    "subtitleVideoScrubSlider",
    "subtitleVideoTimecodeLabel",
    "subtitleVideoAudioModeSwitch",
    "subtitleVideoCollapseButton",
    "subtitleVideoFallbackLabel",
)

_DRIVER = textwrap.dedent(
    """
    import json
    import sys
    from pathlib import Path

    from PySide6.QtCore import Property, QMetaObject, QObject, QUrl, Signal, Slot
    from PySide6.QtGui import QGuiApplication
    from PySide6.QtQml import QQmlComponent, QQmlEngine
    from PySide6.QtQuick import QQuickItem  # noqa: F401 - registers the item converter

    from vienetts_app.ui.list_models import DictListModel

    qml_dir, files = sys.argv[1], sys.argv[2:]
    DOCK_BUTTONS = ("subtitleRenderButton", "subtitlePlayButton", "subtitleExportMenuButton")


    class CountingCueModel(DictListModel):
        # The real cue model's shape; counts the row reads QML makes.
        def __init__(self, rows):
            super().__init__(tuple(rows[0]), key="index")
            self.sync(rows)
            self.gets = 0

        @Slot(int, result="QVariantMap")
        def get(self, row):
            self.gets += 1
            return super().get(row)


    class StubController(QObject):
        loadedChanged = Signal()
        renderedChanged = Signal()
        cuesChanged = Signal()
        speakersChanged = Signal()
        cueModelChanged = Signal()
        activeCueChanged = Signal()
        aiProgressChanged = Signal()
        aiStageTextChanged = Signal()
        videoChanged = Signal()
        videoSeekRequested = Signal(int)
        videoReleaseRequested = Signal()
        positionMsChanged = Signal()
        durationChanged = Signal()
        playerStateChanged = Signal()
        videoOverrunMsChanged = Signal()
        embedSubtitlesChanged = Signal()
        subtitleEmbedModeChanged = Signal()
        videoMixModeChanged = Signal()
        videoBgGainDbChanged = Signal()
        softSubtitlesSupportedChanged = Signal()
        llmGlossaryChanged = Signal()
        busyChanged = Signal()
        projectsChanged = Signal()
        exportFinished = Signal(str, str)

        def __init__(self):
            super().__init__()
            self._loaded = True
            self._rendered = True
            self._has_video = True
            self._video_path = "/tmp/stub_video.mp4"
            self._video_url = QUrl.fromLocalFile(self._video_path).toString()
            self._video_name = "stub_video.mp4"
            self._position_ms = 0
            self._duration_ms = 12000
            self._player_state = "stopped"
            self._cues = [
                {
                    "index": 1,
                    "startMs": 0,
                    "endMs": 2000,
                    "startLabel": "00:00:00,000",
                    "endLabel": "00:00:02,000",
                    "text": "Xin chào thế giới",
                    "speaker": "SPEAKER_01",
                },
                {
                    "index": 2,
                    "startMs": 3000,
                    "endMs": 5000,
                    "startLabel": "00:00:03,000",
                    "endLabel": "00:00:05,000",
                    "text": "Câu thứ hai",
                    "speaker": "",
                },
            ]
            self._cue_model = CountingCueModel(self._cues)
            self.index_lookups = 0
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
            self._llm_glossary = "Minh -> Minh"
            self.glossary_calls = []
            self.notified_video_playing = 0
            self.synced_positions = []
            self._busy_owner = ""
            self._projects = [
                {"id": "p1", "title": "Một", "cueCount": 2, "rendered": True, "active": True},
                {"id": "p2", "title": "Hai", "cueCount": 1, "rendered": False, "active": False},
            ]

        def set_owner(self, owner):
            # Drive the FR-1.1 busy axis: one owner (or none) at a time.
            self._busy_owner = owner
            self.busyChanged.emit()

        @Property(str, notify=busyChanged)
        def busyOwner(self):
            return self._busy_owner

        @Property(str, notify=busyChanged)
        def busyReason(self):
            return {"ai": "AI đang chạy", "render": "Đang tạo", "export": "Đang xuất"}.get(
                self._busy_owner, ""
            )

        @Property(bool, notify=busyChanged)
        def aiBusy(self):
            return self._busy_owner == "ai"

        @Property(bool, notify=busyChanged)
        def rendering(self):
            return self._busy_owner == "render"

        @Property(bool, notify=busyChanged)
        def exporting(self):
            return self._busy_owner == "export"

        @Property(bool, notify=renderedChanged)
        def renderStale(self):
            return False

        @Property(str, notify=renderedChanged)
        def renderStaleReason(self):
            return ""

        @Property(float, notify=busyChanged)
        def renderProgress(self):
            return 0.0

        @Property(float, notify=busyChanged)
        def exportProgress(self):
            return 0.0

        @Property(bool, notify=renderedChanged)
        def canExportTrack(self):
            return self._rendered

        @Property(bool, notify=renderedChanged)
        def canExportSrt(self):
            return self._rendered

        @Property("QVariantList", notify=projectsChanged)
        def projects(self):
            return self._projects

        @Property(int, notify=projectsChanged)
        def projectCount(self):
            return len(self._projects)

        @Property(str, notify=llmGlossaryChanged)
        def llmGlossary(self):
            return self._llm_glossary

        @Slot(str)
        def setLlmGlossary(self, text):
            self._llm_glossary = str(text)
            self.glossary_calls.append(self._llm_glossary)
            self.llmGlossaryChanged.emit()

        @Property(bool, notify=loadedChanged)
        def loaded(self):
            return self._loaded

        @Property(bool, notify=renderedChanged)
        def rendered(self):
            return self._rendered

        @Property(bool, notify=videoChanged)
        def hasVideo(self):
            return self._has_video

        @Property(str, notify=videoChanged)
        def videoPath(self):
            return self._video_path

        @Property(str, notify=videoChanged)
        def videoUrl(self):
            return self._video_url

        @Property(str, notify=videoChanged)
        def videoName(self):
            return self._video_name

        @Property(int, notify=positionMsChanged)
        def positionMs(self):
            return self._position_ms

        @positionMs.setter
        def positionMs(self, value):
            self._position_ms = int(value)
            self.positionMsChanged.emit()

        @Property(int, notify=durationChanged)
        def durationMs(self):
            return self._duration_ms

        @Property(str, notify=playerStateChanged)
        def playerState(self):
            return self._player_state

        @playerState.setter
        def playerState(self, value):
            self._player_state = str(value)
            self.playerStateChanged.emit()

        @Property("QVariant", notify=speakersChanged)
        def speakers(self):
            return self._speakers

        @Property(QObject, constant=True)
        def cueModel(self):
            return self._cue_model

        @Slot(int, result=int)
        def cueIndexAt(self, ms):
            self.index_lookups += 1
            spans = [(row["startMs"], row["endMs"]) for row in self._cues]
            return next((i for i, (a, b) in enumerate(spans) if a <= ms < b), -1)

        @Property(int, notify=activeCueChanged)
        def activeCue(self):
            return self._active_cue

        @activeCue.setter
        def activeCue(self, value):
            self._active_cue = int(value)
            self.activeCueChanged.emit()

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
        def notifyVideoPreviewPlaying(self):
            self.notified_video_playing += 1

        @Slot(int)
        def syncActiveCueFromVideo(self, ms):
            self.synced_positions.append(int(ms))

        @Slot()
        def refreshProjects(self):
            pass

        @Slot()
        def pause(self):
            self.playerState = "paused"

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

        @Property(str)
        def blockerReason(self):
            return ""


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
        name = Path(path).name
        component = QQmlComponent(engine, QUrl.fromLocalFile(path))
        item = component.create()
        created = item is not None
        loaded = False
        extra = {}
        if created:
            item.setProperty("available", True)
            item.setProperty("shown", True)
            loaded = True
            if name == "SubtitlePreparePanel.qml":
                app.processEvents()
                glossary_toggle = item.findChild(QObject, "subtitleGlossaryToggle")
                glossary_field = item.findChild(QObject, "subtitleGlossaryField")
                extra["glossary_found"] = (
                    glossary_toggle is not None and glossary_field is not None
                )
                if glossary_field is not None and glossary_toggle is not None:
                    extra["glossary_expanded_initial"] = bool(
                        item.property("glossaryExpanded")
                    )
                    QMetaObject.invokeMethod(glossary_toggle, "click")
                    app.processEvents()
                    extra["glossary_expanded_after_click"] = bool(
                        item.property("glossaryExpanded")
                    )
                    extra["glossary_initial"] = str(glossary_field.property("text"))
                    glossary_field.setProperty("text", "An -> An; anh - em")
                    QMetaObject.invokeMethod(glossary_field, "editingFinished")
                    app.processEvents()
                    extra["glossary_calls"] = list(controller.glossary_calls)

            if name == "SubtitleVideoPreview.qml":
                app.processEvents()
                names = {item.objectName()} | {
                    child.objectName() for child in item.findChildren(QObject) if child.objectName()
                }
                extra["object_names"] = sorted(names)
                extra["initial_collapsed"] = bool(item.property("collapsed"))
                extra["initial_audio_mode"] = str(item.property("audioMode"))
                extra["initial_muted"] = bool(item.property("audioMuted"))
                extra["caption_text_0"] = str(item.property("activeCaptionText"))
                extra["caption_speaker_0"] = str(item.property("activeCaptionSpeaker"))
                extra["captions_enabled_initial"] = bool(item.property("captionsEnabled"))

                # Toggle captions off and back on
                cap_btn = item.findChild(QObject, "subtitleVideoCaptionToggle")
                QMetaObject.invokeMethod(cap_btn, "click")
                app.processEvents()
                extra["captions_enabled_after_click"] = bool(item.property("captionsEnabled"))
                QMetaObject.invokeMethod(cap_btn, "click")
                app.processEvents()

                # Auto-collapse in compact mode outside "source" stage
                item.setProperty("compact", True)
                item.setProperty("stageName", "prepare")
                app.processEvents()
                extra["auto_collapsed_compact_prepare"] = bool(item.property("collapsed"))

                # Manual expand via collapse button overrides auto-collapse
                col_btn = item.findChild(QObject, "subtitleVideoCollapseButton")
                QMetaObject.invokeMethod(col_btn, "click")
                app.processEvents()
                extra["expanded_after_toggle"] = not bool(item.property("collapsed"))

                # Dub playback auto-switches to muted "dub" sync mode
                controller.playerState = "playing"
                controller.positionMs = 3400
                controller.activeCue = 1
                app.processEvents()
                extra["dub_playing_mode"] = str(item.property("audioMode"))
                extra["dub_playing_muted"] = bool(item.property("audioMuted"))
                extra["caption_text_1"] = str(item.property("activeCaptionText"))
                controller.playerState = "stopped"
                app.processEvents()

                # FR-6.2: a source-video position tick asks the controller for
                # the cue (one bisect), then reads at most that one row.
                controller.activeCue = -1
                model = controller._cue_model
                gets_before, lookups_before = model.gets, controller.index_lookups
                tick_captions = []
                ticks = (500, 2500, 3500, 6000)
                for ms in ticks:
                    item.setProperty("_lastRequestedPositionMs", ms)
                    app.processEvents()
                    tick_captions.append(str(item.property("activeCaptionText")))
                extra["tick_captions"] = tick_captions
                extra["tick_row_reads"] = model.gets - gets_before
                extra["tick_lookups"] = controller.index_lookups - lookups_before
                extra["tick_count"] = len(ticks)

                # Seek and release signals
                controller.videoSeekRequested.emit(4200)
                app.processEvents()
                extra["seek_position_ms"] = int(item.property("displayPositionMs"))
                controller.videoReleaseRequested.emit()
                app.processEvents()
                extra["released_source"] = str(item.property("mediaSourceString"))

                # Codec/backend fallback state
                item.setProperty("forceFallbackError", True)
                app.processEvents()
                extra["fallback_error_active"] = bool(item.property("hasDecodeError"))

            # FR-1.1: per busy owner, the dock's Render/Play/Export and every
            # project delete button. `enabled` itself is asserted — click()
            # would bypass it.
            if name in ("SubtitleActionDock.qml", "SubtitlesTab.qml"):
                app.processEvents()
                gated = {}
                for owner in ("", "ai", "render", "export"):
                    controller.set_owner(owner)
                    app.processEvents()
                    if name == "SubtitleActionDock.qml":
                        buttons = {n: item.findChild(QObject, n) for n in DOCK_BUTTONS}
                        gated[owner] = {
                            n: [bool(b.property("enabled")), str(b.property("disabledReason"))]
                            for n, b in buttons.items()
                        }
                    else:
                        # Repeater delegates are visual children only: walk
                        # the projects dialog's item tree, not QObject children.
                        dialog = item.findChild(QObject, "subtitleProjectsDialog")
                        stack, deletes = [dialog.property("contentItem")], []
                        while stack:
                            node = stack.pop()
                            if node.objectName() == "subtitleDeleteProjectButton":
                                deletes.append(bool(node.property("enabled")))
                            stack.extend(node.childItems())
                        gated[owner] = deletes
                controller.set_owner("")
                extra["gated"] = gated

        results[name] = {
            "created": created,
            "loaded": loaded,
            "errors": [str(err) for err in component.errors()],
            **extra,
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
        + [str(QML_DIR / "components" / name) for name in COMPONENTS]
        + [str(QML_DIR / name) for name in PAGES],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(REPO_ROOT),
    )
    payload = [line for line in proc.stdout.splitlines() if line.startswith("RESULT:")]
    assert proc.returncode == 0 and payload, (proc.stdout, proc.stderr)
    results = json.loads(payload[-1][len("RESULT:") :])
    for name in (*COMPONENTS, *PAGES):
        assert results[name]["created"], results[name]
        assert results[name]["loaded"], results[name]
        assert results[name]["errors"] == [], results[name]

    pp = results["SubtitlePreparePanel.qml"]
    assert pp["glossary_found"] is True
    assert pp["glossary_expanded_initial"] is False
    assert pp["glossary_expanded_after_click"] is True
    assert pp["glossary_initial"] == "Minh -> Minh"
    assert pp["glossary_calls"] == ["An -> An; anh - em"]

    vp = results["SubtitleVideoPreview.qml"]
    assert set(VIDEO_PREVIEW_OBJECT_NAMES) <= set(vp["object_names"])
    assert vp["initial_collapsed"] is False
    assert vp["initial_audio_mode"] == "source"
    assert vp["initial_muted"] is False
    assert vp["caption_text_0"] == "Xin chào thế giới"
    assert vp["caption_speaker_0"] == "SPEAKER_01"
    assert vp["captions_enabled_initial"] is True
    assert vp["captions_enabled_after_click"] is False
    assert vp["auto_collapsed_compact_prepare"] is True
    assert vp["expanded_after_toggle"] is True
    assert vp["dub_playing_mode"] == "dub"
    assert vp["dub_playing_muted"] is True
    assert vp["caption_text_1"] == "Câu thứ hai"
    assert vp["tick_captions"] == ["Xin chào thế giới", "", "Câu thứ hai", ""]
    assert vp["tick_lookups"] >= vp["tick_count"]
    assert vp["tick_row_reads"] <= 2  # one per tick inside a cue, none in silence
    assert vp["seek_position_ms"] == 4200
    assert vp["released_source"] == ""
    assert vp["fallback_error_active"] is True

    # FR-1.1: Render/Play/Export are disabled (with the owner's reason) while
    # AI rewrites the cues; the dock's own render/export gates still hold.
    dock = results["SubtitleActionDock.qml"]["gated"]
    assert all(enabled for enabled, _reason in dock[""].values()), dock[""]
    for owner, reason in (("ai", "AI đang chạy"), ("export", "Đang xuất")):
        assert dock[owner] == {
            name: [False, reason]
            for name in ("subtitleRenderButton", "subtitlePlayButton", "subtitleExportMenuButton")
        }, (owner, dock[owner])
    assert dock["render"]["subtitleRenderButton"] == [False, "Đang tạo"]
    assert dock["render"]["subtitleExportMenuButton"] == [False, "Đang tạo"]

    # The project delete button is disabled while ANY owner is busy.
    deletes = results["SubtitlesTab.qml"]["gated"]
    assert deletes[""] and all(deletes[""]), deletes
    assert all(deletes[owner] == [False] * len(deletes[""]) for owner in ("ai", "render", "export"))
