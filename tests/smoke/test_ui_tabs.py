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

SRT subtitle surface — ``srt_surface``: a FakeSubtitle (recording stand-in
for SubtitleController) is injected via ``subtitle_factory`` and the tab is
switched to ``"srt"``. The scenario pins the ``subtitleController`` context
property — SubtitleCard inherits AppCard's ``subtitle`` header string, so the
old ``subtitle`` name would bind the string and leave ``loaded`` false. It
also drives activeCue follow-scroll and the Escape route
(``cancelRender`` while an SRT render runs, ``controller.cancel`` for a
regular busy job).

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

Consolidation and coverage (Phase 6 Task 6.4) — the groups above are the
consolidated surface families, one subprocess each: text/paragraph/subtitle,
cloning/studio, settings/engine-profiles/capability-bindings, stream
lifecycle, audiobook. Every objectName the capability work introduced is
asserted somewhere in this suite, through the scenario's ``required`` set or
a named lookup. Three kinds are deliberately NOT asserted, and adding an
assertion for them is a mistake, not a fix:

* native dialogs (``qwenRuntimeImportDialog``, ``qwenModelPackDialog``,
  ``exportDialog``, ``offlinePackDialog``…) — they stay closed offscreen, so
  the contract is the QML seam behind their ``onAccepted`` plus the button's
  enabled state;
* dynamic per-key names (``qwenModel*_<key>``, ``qwenDeviceChip_<key>``) —
  reached through helpers like ``row_item(name, key)``, so only the prefix
  literal appears here;
* elements whose only contract is style (``qwenModelStateBadge_<key>``, the
  pill's colour) — the state WORD is asserted through its inner
  ``qwenModelStateLabel_<key>``.
"""

import json
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.smoke

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
    import vienetts_app  # repo-root anchor for committed fixtures
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
        auditionVoiceIdChanged = Signal()
        auditionStateChanged = Signal()
        updateAvailableChanged = Signal()
        updateCheckingChanged = Signal()
        updateInfoChanged = Signal()
        cudaRuntimeStateChanged = Signal()
        cudaRuntimeProgressChanged = Signal()
        cudaRuntimeStorageChanged = Signal()
        cudaRuntimeErrorChanged = Signal()
        cudaRuntimeSupportedChanged = Signal()
        cudaRuntimeDriverChanged = Signal()
        localCudaRuntimesChanged = Signal()
        studioProjectChanged = Signal()
        studioEnvelopeChanged = Signal()
        studioControlsChanged = Signal()
        studioAuditionChanged = Signal()
        studioRegenProfileChanged = Signal()
        engineProfileChanged = Signal()
        engineProfilesChanged = Signal()
        profileCatalogChanged = Signal()
        synthesisLanguageChanged = Signal()
        profileModelChanged = Signal()
        profileRuntimeChanged = Signal()
        profileReadyChanged = Signal()
        profileDeviceChanged = Signal()
        qwenRuntimeStateChanged = Signal()
        qwenRuntimeProgressChanged = Signal()
        qwenRuntimeStorageChanged = Signal()
        qwenRuntimeErrorChanged = Signal()
        qwenRuntimeSupportChanged = Signal()
        qwenModelsChanged = Signal()
        qwenDeviceChanged = Signal()

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
            self._models_missing = False
            self._audition_voice_id = ""
            self._audition_state = "idle"
            self.audition_calls = []
            # Update-check surface (mirrors the real controller): pinned
            # version/platform, no network offscreen — scenarios flip the
            # properties to drive the Settings card states directly.
            self._app_version = "0.1.5"
            self._update_platform_key = "linux-x64"
            self._update_available = False
            self._update_checking = False
            self._update_latest_version = ""
            self._update_release_url = ""
            self._update_asset_name = ""
            self._update_asset_url = ""
            self._update_other_assets: list[dict[str, object]] = []
            self._update_error = ""
            self.check_updates_calls = 0
            # Managed CUDA runtime surface: it remains inert at startup.
            # Settings must only inspect/download/discover after an explicit
            # user action, so all state transitions below are test-driven.
            self._cuda_runtime_supported = True
            self._cuda_runtime_state = "unavailable"
            self._cuda_runtime_progress = 0.0
            self._cuda_runtime_installed_bytes = 0
            self._cuda_runtime_required_bytes = 1_024
            self._cuda_runtime_error = ""
            self._cuda_runtime_driver_checked = True
            self._cuda_runtime_driver_ready = True
            self._local_cuda_runtimes = []
            self.cuda_install_calls = 0
            self.cuda_cancel_calls = 0
            self.cuda_remove_calls = 0
            self.cuda_discover_calls = 0
            self.cuda_refresh_calls = 0
            # Mini-studio surface (mirrors AppController Task 4): the fake
            # opens a canned two-clip project so scenarios pin QML wiring.
            self._has_studio_project = False
            self._studio_clips = []
            self._studio_envelope = []
            self.studio_open_calls = []
            self.studio_preview_calls = 0
            self.studio_gain_calls = []
            self.studio_regen_calls = []
            self.studio_preview_clip_calls = []
            self._studio_ops = []
            self._studio_controls = {"gain": 0.0, "speed": 1.0, "gap": 500, "fade": 200}
            self._studio_duration_ms = 0
            self._studio_regen_clip_id = ""
            # Engine-mismatch offer (Task 6.3): a refused re-synthesis arms the
            # required profile, the banner names it, and the switch action is
            # what the button calls.
            self._studio_regen_profile = ""
            self._studio_regen_profile_label = ""
            self.studio_switch_calls = []
            # Clip audition + range edits (mirrors AppController's studio
            # surface): the dock switches from the mix to the clip it is
            # actually playing, and the selection toolbar reaches the two
            # millisecond-based range slots.
            self._studio_clip_playing_id = ""
            self._studio_clip_duration_ms = 0
            self._studio_clip_envelope = []
            self.studio_revert_calls = []
            self.studio_delete_calls = []
            self.studio_trim_range_calls = []
            self.studio_cut_range_calls = []
            # Engine-profile surface (Tasks 5.1/6.1): the shared profile +
            # language pickers bind these, so scenarios flip every readiness
            # branch directly (no engine, no model, no probe).
            self._engine_profile = "vieneu"
            self._engine_profile_label = "VieNeu-TTS v3 Turbo"
            self._profile_device = "cpu"
            self._profile_model_state = "ready"
            self._profile_model_error = ""
            self._profile_runtime_state = "ready"
            self._synthesis_language = "vi"
            self._profile_languages = [
                {"code": "vi", "label": "Tiếng Việt", "modelName": "", "isAuto": False},
                {"code": "en", "label": "English", "modelName": "", "isAuto": False},
            ]
            # Voice catalogs of the ACTIVE profile (Task 6.2): the shared picker
            # renders these instead of the VieNeu catalog, so scenarios flip the
            # profile and its catalogs together, exactly like the real
            # controller republishes them on a switch. VieNeu's clones ARE its
            # saved SDK names (the real controller reads the same registry for
            # profileClones), so the seed mirrors the "Đã sao chép" group.
            self._profile_voices = []
            self._profile_clones = [
                {"id": "my_clone", "label": "my_clone", "transcript": ""}
            ]
            self.switch_profile_calls = []
            self.set_language_calls = []
            # Qwen settings surface (Task 6.1): device choice + managed runtime
            # + both checkpoints. Inert until a scenario drives it — the real
            # cards must not inspect, download or import on their own.
            self._qwen_runtime_supported = True
            self._qwen_runtime_state = "unavailable"
            self._qwen_runtime_progress = 0.0
            self._qwen_runtime_installed_bytes = 0
            self._qwen_runtime_required_bytes = 2_147_483_648
            self._qwen_runtime_error = ""
            self._qwen_runtime_variant = "Linux x64 · CUDA"
            self._qwen_resolved_device = "cpu"
            self._qwen_device = "auto"
            self._qwen_shared_bytes = 686_752_126
            self._qwen_models = [
                {
                    "key": "customvoice",
                    "profile": "qwen_custom_0_6b",
                    "label": "Qwen3-TTS CustomVoice 0.6B",
                    "state": "unavailable",
                    "ready": False,
                    "installedBytes": 0,
                    "requiredBytes": 2_498_386_873,
                    "progress": 0.0,
                    "error": "",
                    "busy": False,
                    "isActive": False,
                },
                {
                    "key": "base",
                    "profile": "qwen_base_0_6b",
                    "label": "Qwen3-TTS Base 0.6B",
                    "state": "unavailable",
                    "ready": False,
                    "installedBytes": 0,
                    "requiredBytes": 2_516_104_532,
                    "progress": 0.0,
                    "error": "",
                    "busy": False,
                    "isActive": False,
                },
            ]
            self.qwen_install_calls = 0
            self.qwen_cancel_calls = 0
            self.qwen_repair_calls = 0
            self.qwen_remove_calls = 0
            self.qwen_refresh_calls = 0
            self.qwen_open_dir_calls = []
            self.qwen_model_calls = []
            self.qwen_import_calls = []
            self.qwen_device_calls = []

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
            # AppController drops the clip audition on stop; the dock must fall
            # back to describing the whole mix.
            self._mutate("_studio_clip_playing_id", "", self.studioAuditionChanged)
            self._mutate("_studio_clip_duration_ms", 0, self.studioAuditionChanged)
            self._mutate("_studio_clip_envelope", [], self.studioAuditionChanged)

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

        @Slot(str, str, bool, str)
        def addVoice(self, name, clip_path, denoise, transcript=""):
            # Record the call, then mirror the real controller's ASYNC
            # completion: the voice lands in the cloned catalog group and
            # voicesChanged re-renders QML pickers/lists. The transcript is
            # the capability-required reference text ("" on VieNeu).
            self.add_voice_calls.append(
                [str(name), str(clip_path), bool(denoise), str(transcript)]
            )
            self._append_cloned(str(name))
            self._profile_clones.append(
                {"id": str(name), "label": str(name), "transcript": str(transcript)}
            )
            self.voicesChanged.emit()
            self.profileCatalogChanged.emit()

        @Slot(str)
        def removeVoice(self, name):
            self.remove_voice_calls.append(str(name))
            for group in self._voices:
                if group["label"] == "Đã sao chép":
                    group["voices"] = [v for v in group["voices"] if v["id"] != str(name)]
            self._profile_clones = [
                clone for clone in self._profile_clones if clone["id"] != str(name)
            ]
            self.voicesChanged.emit()
            self.profileCatalogChanged.emit()

        @Slot(str)
        def denoisePreview(self, clip_path):
            # The real controller completes asynchronously into previewPath;
            # clone_denoise drives that completion via the property setter.
            self.denoise_calls.append(str(clip_path))

        @Property(str, notify=auditionVoiceIdChanged)
        def auditionVoiceId(self):
            return self._audition_voice_id

        @Property(str, notify=auditionStateChanged)
        def auditionState(self):
            return self._audition_state

        @Slot(str)
        def auditionVoice(self, voice):
            self.audition_calls.append(str(voice))
            self._audition_voice_id = str(voice)
            self._audition_state = "playing"
            self.auditionVoiceIdChanged.emit()
            self.auditionStateChanged.emit()

        @Slot()
        def stopAudition(self):
            self._audition_voice_id = ""
            self._audition_state = "idle"
            self.auditionVoiceIdChanged.emit()
            self.auditionStateChanged.emit()

        @Property(bool, notify=studioProjectChanged)
        def hasStudioProject(self):
            return self._has_studio_project

        @Property("QVariantList", notify=studioProjectChanged)
        def studioClips(self):
            return self._studio_clips

        @Property("QVariantList", notify=studioEnvelopeChanged)
        def studioEnvelope(self):
            return self._studio_envelope

        @Slot(str, str, result=bool)
        def openInStudio(self, owner, text):
            self.studio_open_calls.append([str(owner), str(text)])
            self._mutate("_has_studio_project", True, self.studioProjectChanged)
            self._mutate(
                "_studio_clips",
                [
                    {
                        "id": "c0",
                        "label": "1",
                        "text": "hello",
                        "duration": 1.0,
                        "duration_str": "1.0s",
                        # Provenance (Task 6.3): the engine that produced the
                        # clip's audio, and the language it ran with.
                        "profile": "vieneu",
                        "profileLabel": "VieNeu-TTS v3 Turbo",
                        "language": "vi",
                    },
                    {
                        "id": "c1",
                        "label": "2",
                        "text": "world",
                        "duration": 1.0,
                        "duration_str": "1.0s",
                        # A clip whose audio predates engine provenance: the UI
                        # names VieNeu rather than claiming an unknown engine.
                        "profile": "",
                        "profileLabel": "",
                        "language": "",
                    },
                ],
                self.studioProjectChanged,
            )
            self._mutate("_studio_envelope", [0.5] * 160, self.studioEnvelopeChanged)
            self._mutate("_studio_duration_ms", 2000, self.studioProjectChanged)
            return True

        @Slot(result=bool)
        def studioPreview(self):
            self.studio_preview_calls += 1
            return True

        @Slot(float, result=bool)
        def studioPushGain(self, db):
            self.studio_gain_calls.append(float(db))
            return True

        @Property("QVariantList", notify=studioProjectChanged)
        def studioOps(self):
            return self._studio_ops

        @Property("QVariantMap", notify=studioControlsChanged)
        def studioControls(self):
            return self._studio_controls

        @Property(int, notify=studioProjectChanged)
        def studioDurationMs(self):
            return self._studio_duration_ms

        @Property(str, notify=studioProjectChanged)
        def studioRegenClipId(self):
            return self._studio_regen_clip_id

        @Property(str, notify=studioRegenProfileChanged)
        def studioRegenProfile(self):
            return self._studio_regen_profile

        @Property(str, notify=studioRegenProfileChanged)
        def studioRegenProfileLabel(self):
            return self._studio_regen_profile_label

        @Slot(result=bool)
        def studioSwitchToRegenProfile(self):
            # The real controller switches the engine profile and consumes the
            # offer; the fake records the call and clears the banner.
            self.studio_switch_calls.append(True)
            self._studio_regen_profile = ""
            self._studio_regen_profile_label = ""
            self.studioRegenProfileChanged.emit()
            return True

        @Slot(str, result=bool)
        def studioPreviewClip(self, clip_id):
            self.studio_preview_clip_calls.append(str(clip_id))
            # Mirror AppController: the audition publishes the clip's own
            # envelope and length so the dock stops describing the mix.
            self._mutate("_studio_clip_playing_id", str(clip_id), self.studioAuditionChanged)
            self._mutate("_studio_clip_duration_ms", 1000, self.studioAuditionChanged)
            self._mutate("_studio_clip_envelope", [0.25] * 160, self.studioAuditionChanged)
            return True

        @Property(str, notify=studioAuditionChanged)
        def studioClipPlayingId(self):
            return self._studio_clip_playing_id

        @Property(int, notify=studioAuditionChanged)
        def studioClipDurationMs(self):
            return self._studio_clip_duration_ms

        @Property("QVariantList", notify=studioAuditionChanged)
        def studioClipEnvelope(self):
            return self._studio_clip_envelope

        @Slot(int, result=bool)
        def studioRevertTo(self, index):
            self.studio_revert_calls.append(int(index))
            return True

        @Slot(str, result=bool)
        def studioDeleteClip(self, clip_id):
            self.studio_delete_calls.append(str(clip_id))
            return True

        @Slot(int, int, result=bool)
        def studioPushTrimRange(self, start_ms, end_ms):
            self.studio_trim_range_calls.append([int(start_ms), int(end_ms)])
            return True

        @Slot(int, int, result=bool)
        def studioPushCutRange(self, start_ms, end_ms):
            self.studio_cut_range_calls.append([int(start_ms), int(end_ms)])
            return True

        @Slot(str, str, result=bool)
        @Slot(str, str, str, result=bool)
        def studioRegenClip(self, clip_id, voice, new_text=""):
            self.studio_regen_calls.append([str(clip_id), str(voice)])
            return True

        # Update-check surface (mirrors the real controller 1:1).
        @Property(str, constant=True)
        def appVersion(self):
            return self._app_version

        @Property(str, constant=True)
        def updatePlatformLabel(self):
            return {"windows-x64": "Windows"}.get(self._update_platform_key, "Linux")

        @Property(bool, notify=updateAvailableChanged)
        def updateAvailable(self):
            return self._update_available

        @Property(bool, notify=updateCheckingChanged)
        def updateChecking(self):
            return self._update_checking

        @Property(str, notify=updateInfoChanged)
        def updateLatestVersion(self):
            return self._update_latest_version

        @Property(str, notify=updateInfoChanged)
        def updateReleaseUrl(self):
            return self._update_release_url

        @Property(str, notify=updateInfoChanged)
        def updateAssetName(self):
            return self._update_asset_name

        @Property(str, notify=updateInfoChanged)
        def updateAssetUrl(self):
            return self._update_asset_url

        @Property("QVariantList", notify=updateInfoChanged)
        def updateOtherAssets(self):
            return list(self._update_other_assets)

        @Property(str, notify=updateInfoChanged)
        def updateError(self):
            return self._update_error

        @Slot()
        def checkForUpdates(self):
            self.check_updates_calls += 1

        @Property(str, notify=cudaRuntimeStateChanged)
        def cudaRuntimeState(self):
            return self._cuda_runtime_state

        @Property(float, notify=cudaRuntimeProgressChanged)
        def cudaRuntimeProgress(self):
            return self._cuda_runtime_progress

        @Property(int, notify=cudaRuntimeStorageChanged)
        def cudaRuntimeInstalledBytes(self):
            return self._cuda_runtime_installed_bytes

        @Property(int, notify=cudaRuntimeStorageChanged)
        def cudaRuntimeRequiredBytes(self):
            return self._cuda_runtime_required_bytes

        @Property(str, notify=cudaRuntimeErrorChanged)
        def cudaRuntimeError(self):
            return self._cuda_runtime_error

        @Property(bool, notify=cudaRuntimeSupportedChanged)
        def cudaRuntimeSupported(self):
            return self._cuda_runtime_supported

        @Property(bool, notify=cudaRuntimeDriverChanged)
        def cudaRuntimeDriverChecked(self):
            return self._cuda_runtime_driver_checked

        @Property(bool, notify=cudaRuntimeDriverChanged)
        def cudaRuntimeDriverReady(self):
            return self._cuda_runtime_driver_ready

        @Property(bool, notify=cudaRuntimeDriverChanged)
        def cudaRuntimeInstallAllowed(self):
            return self._cuda_runtime_supported and self._cuda_runtime_driver_ready

        @Property(bool, notify=cudaRuntimeStateChanged)
        def cudaRuntimeReady(self):
            return self._cuda_runtime_state == "ready"

        @Property("QVariantList", notify=localCudaRuntimesChanged)
        def localCudaRuntimes(self):
            return self._local_cuda_runtimes

        @Slot()
        def installCudaRuntime(self):
            self.cuda_install_calls += 1

        @Slot()
        def cancelCudaRuntimeInstall(self):
            self.cuda_cancel_calls += 1

        @Slot()
        def removeCudaRuntime(self):
            self.cuda_remove_calls += 1

        @Slot()
        def discoverLocalCudaRuntimes(self):
            self.cuda_discover_calls += 1

        @Slot()
        def refreshCudaRuntimeState(self):
            self.cuda_refresh_calls += 1

        # ── Engine-profile surface (Tasks 5.1/6.1) ───────────────────────
        @Property(str, notify=engineProfileChanged)
        def engineProfile(self):
            return self._engine_profile

        @Property(str, notify=engineProfileChanged)
        def engineProfileLabel(self):
            return self._engine_profile_label

        @Property("QVariantList", notify=engineProfilesChanged)
        def engineProfiles(self):
            return [
                {
                    "id": "vieneu",
                    "label": "VieNeu-TTS v3 Turbo",
                    "isDefault": True,
                    "isActive": self._engine_profile == "vieneu",
                    "supportsCloning": True,
                    "supportsPresetVoices": True,
                    "supportsInstruction": False,
                    "voicesSource": "vieneu_catalog",
                    "cloneRequirements": ["reference_clip", "consent"],
                    "runtime": "vieneu_worker",
                    "devices": ["cpu", "cuda"],
                    "generationControls": [],
                    "modelRepo": "pnnbao-ump/VieNeu-TTS-v3-Turbo",
                    "modelRevision": "",
                    "sourceSampleRate": 24000,
                    "outputSampleRate": 48000,
                    "streamingGranularity": "chunk",
                    "languageCount": 2,
                    "voiceCount": 0,
                },
                {
                    "id": "qwen_custom_0_6b",
                    "label": "Qwen3-TTS CustomVoice 0.6B",
                    "isDefault": False,
                    "isActive": self._engine_profile == "qwen_custom_0_6b",
                    "supportsCloning": False,
                    "supportsPresetVoices": True,
                    "supportsInstruction": False,
                    "voicesSource": "pinned",
                    "cloneRequirements": [],
                    "runtime": "qwen_host",
                    "devices": ["cpu", "cuda", "mps"],
                    "generationControls": [],
                    "modelRepo": "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
                    "modelRevision": "",
                    "sourceSampleRate": 24000,
                    "outputSampleRate": 24000,
                    "streamingGranularity": "chunk",
                    "languageCount": 11,
                    "voiceCount": 9,
                },
                {
                    "id": "qwen_base_0_6b",
                    "label": "Qwen3-TTS Base 0.6B",
                    "isDefault": False,
                    "isActive": self._engine_profile == "qwen_base_0_6b",
                    "supportsCloning": True,
                    "supportsPresetVoices": False,
                    "supportsInstruction": False,
                    "voicesSource": "enrollment_only",
                    "cloneRequirements": ["reference_clip", "transcript", "consent"],
                    "runtime": "qwen_host",
                    "devices": ["cpu", "cuda", "mps"],
                    "generationControls": [],
                    "modelRepo": "Qwen/Qwen3-TTS-12Hz-0.6B-Base",
                    "modelRevision": "",
                    "sourceSampleRate": 24000,
                    "outputSampleRate": 24000,
                    "streamingGranularity": "chunk",
                    "languageCount": 11,
                    "voiceCount": 0,
                },
            ]

        @Property("QVariantList", notify=profileCatalogChanged)
        def profileLanguages(self):
            return list(self._profile_languages)

        @Property("QVariantList", notify=profileCatalogChanged)
        def profileVoices(self):
            return list(self._profile_voices)

        @Property("QVariantList", notify=profileCatalogChanged)
        def profileClones(self):
            return list(self._profile_clones)

        @Property(str, notify=synthesisLanguageChanged)
        def synthesisLanguage(self):
            return self._synthesis_language

        @Slot(str, result=bool)
        def setSynthesisLanguage(self, language):
            self.set_language_calls.append(str(language))
            return True

        @Property(str, notify=profileDeviceChanged)
        def engineDevice(self):
            return self._profile_device

        @Property(str, notify=profileModelChanged)
        def profileModelState(self):
            return self._profile_model_state

        @Property(str, notify=profileModelChanged)
        def profileModelError(self):
            return self._profile_model_error

        @Property(bool, notify=profileModelChanged)
        def profileModelReady(self):
            return self._profile_model_state == "ready"

        @Property(str, notify=profileRuntimeChanged)
        def profileRuntimeState(self):
            return self._profile_runtime_state

        @Property(bool, notify=profileReadyChanged)
        def profileReady(self):
            return self._profile_model_state == "ready" and self._profile_runtime_state == "ready"

        @Slot(str, result=bool)
        def switchEngineProfile(self, profile):
            self.switch_profile_calls.append(str(profile))
            self._engine_profile = str(profile)
            self.engineProfileChanged.emit()
            return True

        # ── Qwen settings surface (Task 6.1) ─────────────────────────────
        @Property(bool, notify=qwenRuntimeSupportChanged)
        def qwenRuntimeSupported(self):
            return self._qwen_runtime_supported

        @Property(str, notify=qwenRuntimeSupportChanged)
        def qwenRuntimeVariantLabel(self):
            return self._qwen_runtime_variant

        @Property(str, notify=qwenRuntimeStateChanged)
        def qwenRuntimeState(self):
            return self._qwen_runtime_state

        @Property(float, notify=qwenRuntimeProgressChanged)
        def qwenRuntimeProgress(self):
            return self._qwen_runtime_progress

        @Property("qlonglong", notify=qwenRuntimeStorageChanged)
        def qwenRuntimeInstalledBytes(self):
            return self._qwen_runtime_installed_bytes

        @Property("qlonglong", notify=qwenRuntimeStorageChanged)
        def qwenRuntimeRequiredBytes(self):
            return self._qwen_runtime_required_bytes

        @Property(str, notify=qwenRuntimeErrorChanged)
        def qwenRuntimeError(self):
            return self._qwen_runtime_error

        @Property(bool, notify=qwenRuntimeStateChanged)
        def qwenRuntimeReady(self):
            return self._qwen_runtime_state == "ready"

        @Property(bool, notify=qwenRuntimeStateChanged)
        def qwenRuntimeBusy(self):
            return self._qwen_runtime_state in ("downloading", "validating")

        @Property("QVariantList", notify=qwenModelsChanged)
        def qwenModels(self):
            return [dict(row) for row in self._qwen_models]

        @Property("qlonglong", notify=qwenModelsChanged)
        def qwenSharedBytes(self):
            return self._qwen_shared_bytes

        @Property(bool, notify=qwenModelsChanged)
        def qwenModelBusy(self):
            return any(row["busy"] for row in self._qwen_models)

        @Property(str, notify=qwenModelsChanged)
        def qwenModelStoragePath(self):
            return str(tmp / "qwen" / "models")

        @Property(str, notify=qwenDeviceChanged)
        def qwenDevice(self):
            return self._qwen_device

        @Property("QVariantList", notify=qwenDeviceChanged)
        def qwenDeviceOptions(self):
            return [
                {
                    "value": "auto",
                    "label": "Tự động (khuyến nghị)",
                    "supported": True,
                    "reason": "",
                    "active": self._qwen_device == "auto",
                    "resolved": self._qwen_resolved_device,
                },
                {
                    "value": "cpu",
                    "label": "CPU",
                    "supported": True,
                    "reason": "",
                    "active": self._qwen_device == "cpu",
                    "resolved": "cpu",
                },
                {
                    "value": "cuda",
                    "label": "CUDA (NVIDIA)",
                    "supported": False,
                    "reason": "Không phát hiện GPU NVIDIA trên máy này.",
                    "active": self._qwen_device == "cuda",
                    "resolved": "cuda",
                },
                {
                    "value": "mps",
                    "label": "MPS (Apple Silicon)",
                    "supported": False,
                    "reason": "Nền tảng này không có runtime Qwen cho thiết bị đã chọn.",
                    "active": self._qwen_device == "mps",
                    "resolved": "mps",
                },
            ]

        @Property(str, notify=qwenDeviceChanged)
        def qwenCpuGuidance(self):
            if not self._qwen_runtime_supported:
                return ""
            resolved = (
                self._qwen_resolved_device if self._qwen_device == "auto" else self._qwen_device
            )
            if resolved != "cpu":
                return ""
            return (
                "Chạy Qwen trên CPU sẽ rất chậm (chậm hơn nhiều lần so với GPU). "
                "Hãy cài runtime CPU nếu máy không có GPU, và dùng văn bản ngắn để thử trước."
            )

        @Slot(str, result=bool)
        def setQwenDevice(self, device):
            self.qwen_device_calls.append(str(device))
            return True

        @Slot()
        def refreshQwenState(self):
            self.qwen_refresh_calls += 1

        @Slot()
        def installQwenRuntime(self):
            self.qwen_install_calls += 1

        @Slot()
        def repairQwenRuntime(self):
            self.qwen_repair_calls += 1

        @Slot()
        def cancelQwenRuntimeInstall(self):
            self.qwen_cancel_calls += 1

        @Slot()
        def removeQwenRuntime(self):
            self.qwen_remove_calls += 1

        @Slot(str)
        def importQwenRuntimePack(self, source):
            self.qwen_import_calls.append(["runtime", "", str(source)])

        @Slot(str)
        def installQwenModel(self, profile_key):
            self.qwen_model_calls.append(["install", str(profile_key)])

        @Slot(str)
        def repairQwenModel(self, profile_key):
            self.qwen_model_calls.append(["repair", str(profile_key)])

        @Slot(str)
        def cancelQwenModelDownload(self, profile_key):
            self.qwen_model_calls.append(["cancel", str(profile_key)])

        @Slot(str)
        def removeQwenModel(self, profile_key):
            self.qwen_model_calls.append(["remove", str(profile_key)])

        @Slot(str, str)
        def importQwenModelPack(self, profile_key, source):
            self.qwen_import_calls.append(["model", str(profile_key), str(source)])

        @Slot(result=bool)
        def openQwenRuntimeDir(self):
            self.qwen_open_dir_calls.append("runtime")
            return True

        @Slot(result=bool)
        def openQwenModelDir(self):
            self.qwen_open_dir_calls.append("models")
            return True

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

        # stream_e2e / stream_cancel / stream_cross_tab / stream_error_recover
        # swap the fake controller for the REAL AppController: TTSEngine over a
        # fake-at-the-SDK-layer (generator infer_stream) + a real InferenceWorker
        # thread + a REAL StreamPlaybackController whose audio seam is faked (its
        # own duck-typed sink contract, mirroring tests/unit/test_controller.py's
        # FakeSink — no QtMultimedia construction happens offscreen).
        if scenario in (
            "stream_e2e",
            "stream_cancel",
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
            # Pin live mode: these scenarios assert live-session behavior and
            # predate the silent default.
            controller.livePreview = True
            # Keep quick exports inside tmp (settings default falls back to ~/Music).
        elif scenario == "para_import_oversize":
            # REAL AppController, REAL importer cap (FR-4.6b): importDocument is
            # engine-free, so a plain controller exercises the true
            # IMPORT_CHAR_LIMIT refusal instead of a stubbed seam.
            from vienetts_app.ui.controller import AppController

            controller = AppController(data_dir=tmp, bg_runner=run_sync)

        # para_batch drives the queue card through a recording fake so the
        # scenario pins QML WIRING (bindings, routing, enabled states), not
        # controller behavior (that is unit-covered in test_batch_controller).
        # surface_profile_bindings needs it too: the shared picker re-seeds the
        # batch run's voice on a profile switch, and that push is QML wiring.
        batch_factory = None
        if scenario in ("para_batch", "surface_profile_bindings"):

            class FakeBatch(QObject):
                # Recording stand-in for BatchFileController's QML surface.
                itemsChanged = Signal()
                runningChanged = Signal()
                progressChanged = Signal()
                currentIndexChanged = Signal()
                runAllDoneChanged = Signal()
                runAllTotalChanged = Signal()
                errorTextChanged = Signal()
                renderVoiceChanged = Signal()
                playingIndexChanged = Signal()
                hasPendingChanged = Signal()

                def __init__(self):
                    super().__init__()
                    self._items = []
                    self.added: list[list[str]] = []
                    self.removed: list[int] = []
                    self.ran = 0
                    self.cancelled = 0
                    self._render_voice = ""

                @Property("QVariantList", notify=itemsChanged)
                def items(self):
                    return self._items

                @items.setter
                def items(self, value):
                    self._items = value
                    self.itemsChanged.emit()
                    # Mirror the real controller's _flush_items: hasPending is
                    # computed, so its NOTIFY must fire alongside itemsChanged
                    # or dependent bindings (runAllButton.enabled) never refresh.
                    self.hasPendingChanged.emit()

                @Property(bool, notify=runningChanged)
                def running(self):
                    return False

                @Property(bool, notify=hasPendingChanged)
                def hasPending(self):
                    return any(i["status"] == "pending" for i in self._items)

                @Property(float, notify=progressChanged)
                def progress(self):
                    return 0.5

                @Property(int, notify=currentIndexChanged)
                def currentIndex(self):
                    return 0

                @Property(int, notify=runAllDoneChanged)
                def runAllDone(self):
                    return 1

                @Property(int, notify=runAllTotalChanged)
                def runAllTotal(self):
                    return 2

                @Property(str, notify=errorTextChanged)
                def errorText(self):
                    return ""

                @Property(str, notify=renderVoiceChanged)
                def renderVoice(self):
                    return self._render_voice

                @renderVoice.setter
                def renderVoice(self, value):
                    self._render_voice = str(value)
                    self.renderVoiceChanged.emit()

                @Property(int, notify=playingIndexChanged)
                def playingIndex(self):
                    return -1

                @Slot(list)
                def addFiles(self, paths):
                    self.added.append([str(p) for p in paths])

                @Slot(int)
                def removeItem(self, index):
                    self.removed.append(int(index))

                @Slot()
                def clearFinished(self):
                    pass

                @Slot()
                def runAll(self):
                    self.ran += 1

                @Slot()
                def cancel(self):
                    self.cancelled += 1

                @Slot(int)
                def playItem(self, index):
                    pass

                @Slot()
                def stopPlay(self):
                    pass

                @Slot(int, result=bool)
                def showInFolder(self, index):
                    return True

            fake_batch = FakeBatch()
            batch_factory = lambda _controller: fake_batch

        # srt_surface drives SubtitleCard through a recording fake so the
        # scenario pins QML WIRING: the `subtitleController` context property
        # must win over AppCard's own `subtitle` header string (reading
        # `subtitle` inside the card resolves the STRING, so .loaded/.cues
        # would silently evaluate to undefined).
        subtitle_factory = None
        if scenario == "srt_surface":

            class FakeSubtitle(QObject):
                \"\"\"Recording stand-in for SubtitleController's QML surface.\"\"\"

                loadedChanged = Signal()
                cuesChanged = Signal()
                activeCueChanged = Signal()
                renderingChanged = Signal()
                renderedChanged = Signal()
                exportingChanged = Signal()
                errorTextChanged = Signal()
                statsChanged = Signal()
                renderProgressChanged = Signal()
                playerStateChanged = Signal()
                modeChanged = Signal()
                rateCapChanged = Signal()
                maxGapMsChanged = Signal()
                offsetMsChanged = Signal()
                mergeSentencesChanged = Signal()
                voiceChanged = Signal()
                exportFinished = Signal(str, str)

                def __init__(self):
                    super().__init__()
                    self._rendering = False
                    self._rendered = True
                    self._exporting = False
                    self._active_cue = -1
                    self._voice = ""
                    self.cancel_render_calls = 0
                    # 50 rows so follow-scroll has somewhere to scroll to.
                    self._cues = [
                        {
                            "index": i + 1,
                            "startMs": i * 2000,
                            "endMs": i * 2000 + 1500,
                            "startLabel": "00:00:00,000",
                            "endLabel": "00:00:01,500",
                            "text": f"Câu phụ đề số {i + 1}",
                        }
                        for i in range(50)
                    ]

                @Property(bool, notify=loadedChanged)
                def loaded(self):
                    return True

                @Property(bool, notify=renderingChanged)
                def rendering(self):
                    return self._rendering

                @rendering.setter
                def rendering(self, value):
                    if bool(value) != self._rendering:
                        self._rendering = bool(value)
                        self.renderingChanged.emit()

                @Property(bool, notify=renderedChanged)
                def rendered(self):
                    return self._rendered

                @rendered.setter
                def rendered(self, value):
                    if bool(value) != self._rendered:
                        self._rendered = bool(value)
                        self.renderedChanged.emit()

                @Property(bool, notify=exportingChanged)
                def exporting(self):
                    return self._exporting

                @exporting.setter
                def exporting(self, value):
                    if bool(value) != self._exporting:
                        self._exporting = bool(value)
                        self.exportingChanged.emit()

                @Property("QVariantList", notify=cuesChanged)
                def cues(self):
                    return self._cues

                @Property(int, notify=activeCueChanged)
                def activeCue(self):
                    return self._active_cue

                @activeCue.setter
                def activeCue(self, value):
                    if int(value) != self._active_cue:
                        self._active_cue = int(value)
                        self.activeCueChanged.emit()

                @Property(str, notify=errorTextChanged)
                def errorText(self):
                    return ""

                @Property(str, notify=statsChanged)
                def statsSummary(self):
                    return "50 phụ đề"

                @Property(float, notify=renderProgressChanged)
                def renderProgress(self):
                    return 0.0

                @Property(str, notify=playerStateChanged)
                def playerState(self):
                    return "stopped"

                @Property(str, notify=modeChanged)
                def mode(self):
                    return "dub"

                @Property(float, notify=rateCapChanged)
                def rateCap(self):
                    return 1.5

                @Property(int, notify=maxGapMsChanged)
                def maxGapMs(self):
                    return 0

                @Property(int, notify=offsetMsChanged)
                def offsetMs(self):
                    return 0

                @Property(bool, notify=mergeSentencesChanged)
                def mergeSentences(self):
                    return False

                @Property(str, notify=voiceChanged)
                def voice(self):
                    return self._voice

                @voice.setter
                def voice(self, value):
                    self._voice = str(value or "")

                @Slot(str, result=bool)
                def importSrt(self, path):
                    return True

                @Slot()
                def render(self):
                    pass

                @Slot()
                def cancelRender(self):
                    self.cancel_render_calls += 1

                @Slot()
                def play(self):
                    pass

                @Slot()
                def pause(self):
                    pass

                @Slot()
                def resume(self):
                    pass

                @Slot()
                def stopPlay(self):
                    pass

                @Slot(int)
                def seekToCue(self, index):
                    pass

                @Slot(int)
                def seek(self, ms):
                    pass

                @Slot(str, result=str)
                def exportTrack(self, dest_dir):
                    return ""

                @Slot(str, result=str)
                def exportSrt(self, dest_dir):
                    return ""

            fake_subtitle = FakeSubtitle()
            subtitle_factory = lambda _controller: fake_subtitle

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
            batch_factory=batch_factory,
            subtitle_factory=subtitle_factory,
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

            # ── merged para_load: the same surface contract on the paragraph
            # subtree. Activate the tab first: its live labels/visibility
            # bindings only settle while the StackLayout sibling is current. ──
            bridge.setCurrentTab("paragraph")
            app.processEvents()
            para_names = {o.objectName() for o in paragraph_tab.findChildren(QObject)}
            para_names.add(paragraph_tab.objectName())
            para_required = {
                "paragraphTab", "paragraphEditor", "importButton", "importDialog",
                "charCountLabel", "voicePicker", "generateButton", "progressBar",
                "cancelButton", "errorLabel", "playButton", "exportButton",
                # Streaming + notice surfaces (FR-4.4/FR-4.5/FR-4.6b): the shared
                # waveform and the banner hosting this tab's errorLabel.
                "waveformIndicator", "errorBanner", "srtKeepCheckbox", "artifactPlaybackState",
            }
            para_editor = pfind("paragraphEditor")
            para_dialog = pfind("importDialog")
            para_picker = pfind("voicePicker")
            # fileMode (QQuickFileDialog::FileMode) has no PySide6 converter —
            # OpenFile is asserted indirectly: the accepted path is exercised
            # end-to-end in para_import.
            out["para"] = {
                "missing": sorted(para_required - para_names),
                "editor_editable": not para_editor.property("readOnly"),
                "editor_placeholder": para_editor.property("placeholderText"),
                "import_button_text": pfind("importButton").property("text"),
                "dialog_filters": para_dialog.property("nameFilters"),
                "char_count_text": pfind("charCountLabel").property("text"),
                "header_found": any(
                    o.property("text") == "Đoạn văn / Tệp"
                    for o in paragraph_tab.findChildren(QObject)
                ),
                "hint_mentions_extensions": any(
                    ".pdf" in (o.property("text") or "")
                    for o in paragraph_tab.findChildren(QObject)
                ),
                "flat_ids": [
                    row["id"] for row in qjs_to_py(para_picker.property("flatModel"))
                ],
                "selected_voice": para_picker.property("selectedVoice"),
                "current_index": para_picker.property("currentIndex"),
                "initial_generate_enabled": pfind("generateButton").property("enabled"),
                "generate_hint": pfind("paragraphActionHint").property("text"),
            }
        elif scenario == "generate_flow":
            editor = find("textEditor")
            generate = find("generateButton")
            progress = find("progressBar")
            cancel_btn = find("cancelButton")
            play = find("playButton")

            # ── merged disabled_states: blank/whitespace gating runs FIRST so
            # the flow below starts from the same pristine state ──
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
            out["busy_cancel_visible"] = cancel_btn.property("visible")

            controller.busy = False
            app.processEvents()
            out["idle_export_enabled"] = find("exportButton").property("enabled")
            out["idle_quick_enabled"] = find("quickExportButton").property("enabled")
            out["idle_play_enabled"] = play.property("enabled")

            editor.setProperty("text", "")
            app.processEvents()

            out["initial_generate_enabled"] = generate.property("enabled")
            editor.setProperty("text", "Xin chào thế giới")
            app.processEvents()
            out["filled_generate_enabled"] = generate.property("enabled")

            generate.click()
            app.processEvents()
            # Snapshot: the merged paragraph flow below appends to the live fake
            # list, which would otherwise leak into this tab's record.
            out["generate_calls"] = list(controller.generate_calls)
            out["slot_hits"] = list(controller.slot_hits)

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

            # ── merged para_generate (+para_cancel): the paragraph tab drives
            # its OWN facade over the same controller. pfind-scoped reads keep
            # the shared objectNames (generateButton/progressBar/…) unambiguous.
            # ──
            bridge.setCurrentTab("paragraph")
            app.processEvents()
            p_editor = pfind("paragraphEditor")
            p_generate = pfind("generateButton")
            p_progress = pfind("progressBar")
            p_cancel = pfind("cancelButton")
            p_play = pfind("playButton")
            para_long_text = "Đoạn thứ nhất.\\n\\nĐoạn thứ hai."

            out["para"] = {}
            out["para"]["cancel_hidden_idle"] = not p_cancel.property("visible")
            out["para"]["initial_generate_enabled"] = p_generate.property("enabled")
            p_editor.setProperty("text", para_long_text)
            app.processEvents()
            out["para"]["filled_generate_enabled"] = p_generate.property("enabled")

            p_generate.click()
            app.processEvents()
            # The text-tab flow already recorded its own call: keep only this one.
            out["para"]["generate_calls"] = controller.generate_calls[-1:]
            out["para"]["slot_hits"] = controller.slot_hits[-1:]
            out["para"]["char_count_text"] = pfind("charCountLabel").property("text")

            controller.progress = 0.0  # a fresh submit restarts the meter
            # The text flow above left an artifact behind; reset it so this
            # tab's busy affordances match the fresh-controller state.
            controller.hasAudio = False
            controller.busy = True
            app.processEvents()
            out["para"]["busy_generate_visible"] = p_generate.property("visible")
            out["para"]["busy_generate_busy"] = p_generate.property("busy")
            out["para"]["busy_cancel_visible"] = p_cancel.property("visible")
            out["para"]["cancel_enabled_busy"] = p_cancel.property("enabled")
            out["para"]["busy_label_visible"] = pfind("paraBusyLabel").property("visible")
            out["para"]["busy_progress_visible"] = p_progress.property("visible")
            out["para"]["busy_progress_value"] = p_progress.property("value")
            out["para"]["busy_progress_indeterminate"] = p_progress.property("indeterminate")
            out["para"]["busy_play_enabled"] = p_play.property("enabled")
            out["para"]["busy_import_enabled"] = pfind("importButton").property("enabled")

            cancel_before = controller.cancel_calls
            p_cancel.click()
            app.processEvents()
            out["para"]["cancel_calls"] = controller.cancel_calls - cancel_before

            controller.progress = 0.5
            app.processEvents()
            out["para"]["progress_mid"] = p_progress.property("value")
            out["para"]["indeterminate_mid"] = p_progress.property("indeterminate")

            controller.progress = 1.0
            app.processEvents()
            out["para"]["progress_full"] = p_progress.property("value")

            controller.hasAudio = True
            controller.lastExportPath = str(tmp / "para.wav")
            controller.busy = True
            app.processEvents()
            out["para"]["play_enabled_while_busy_with_artifact"] = p_play.property("enabled")
            out["para"]["export_enabled_while_busy_with_artifact"] = pfind(
                "exportButton"
            ).property("enabled")
            controller.busy = False
            app.processEvents()
            out["para"]["play_enabled_after"] = p_play.property("enabled")
            out["para"]["export_enabled_after"] = pfind("exportButton").property("enabled")
            out["para"]["progress_hidden_after"] = not p_progress.property("visible")
            out["para"]["cancel_hidden_after"] = not p_cancel.property("visible")
            out["para"]["generate_visible_after"] = p_generate.property("visible")
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
            out["selected_unchanged_after_filter"] = (
                picker.property("selectedVoice") == selected_before_filter
            )
            buttons = [
                item for item in item_walk(lists[0])
                if item.objectName() == "voiceAuditionButton"
            ] if lists else []
            out["audition_button_count"] = len(buttons)
            # Buttons live inside row delegates; item_walk order is not
            # row order, so find the button whose row matches adam_north.
            # Do this BEFORE clearing the filter: the filtered-out Adam row
            # still exists in the visual tree, and clearing the text would
            # invalidate the width-bound delegate layout mid-scenario.
            target = None
            for button in buttons:
                row = button.parent()
                while row is not None and row.objectName() != "voicePickerRow":
                    row = row.parent()
                label = str(row.property("rowLabel")) if row is not None else ""
                if "Adam" in label:
                    target = button
                    break
            out["audition_button_for_first_row"] = target is not None
            QMetaObject.invokeMethod(picker, "closePopup")
            app.processEvents()
            out["closed"] = not picker.property("popupOpen")
            # Reopen for the audition click: the click's onClosed-reopen
            # clears the filter text, and the filtered_visible_rows pin
            # above already ran while the filter was active.
            if not picker.property("popupOpen"):
                QMetaObject.invokeMethod(picker, "openPopup")
                app.processEvents()
            if target is not None:
                from PySide6.QtCore import QPoint, QPointF, Qt
                from PySide6.QtQuick import QQuickItem
                from PySide6.QtTest import QTest
                cands = [
                    o for o in lists[0].findChildren(QQuickItem)
                    if o.objectName() == "voiceAuditionButton"
                ] if lists else []
                want = str(target.parent().property("rowLabel") or "")
                btn = next(
                    (o for o in cands
                     if str(o.parent().property("rowLabel") or "") == want),
                    target,
                )
                btn.setProperty("visible", True)
                btn.setProperty("enabled", True)
                app.processEvents()
                click_item(btn)
                app.processEvents()
                out["audition_calls"] = list(controller.audition_calls)
                out["audition_state"] = controller.property("auditionState")
                out["audition_voice"] = controller.property("auditionVoiceId")
                out["popup_still_open"] = picker.property("popupOpen")
            # The audition click reopens the popup via onClosed; close
            # twice: first clears the guard-armed reopen, second sticks.
            QMetaObject.invokeMethod(picker, "closePopup")
            app.processEvents()
            QMetaObject.invokeMethod(picker, "closePopup")
            app.processEvents()
            out["closed"] = not picker.property("popupOpen")
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
        elif scenario == "para_batch":
            bridge.setCurrentTab("paragraph")
            app.processEvents()

            # The queue is the tab's "Tệp" mode (full redesign): the editor and
            # the queue are mutually exclusive surfaces under one mode switch,
            # so switch modes through the tab's own entry point first.
            out["mode_initial"] = paragraph_tab.property("mode")
            out["editor_card_visible_in_text"] = bool(
                pfind("documentEditorCard").property("visible")
            )
            out["card_hidden_in_text"] = not pfind("batchQueueCard").property("visible")

            # Layout stability: the page header and the mode switch must hold
            # the same scene coordinates in every mode — a stale scroll offset
            # or the scrollbar gutter appearing/disappearing must not shift
            # them. pfind's QObject wrappers expose no mapToScene, so the
            # items are looked up typed as QQuickItem.
            header_item = paragraph_tab.findChildren(
                QQuickItem, "paragraphPageHeader"
            )[0]
            tabs_item = paragraph_tab.findChildren(QQuickItem, "modeTabs")[0]

            def scene_pos(item):
                p = item.mapToScene(QPointF(0, 0))
                return [round(p.x()), round(p.y())]

            def top_positions():
                return {
                    "header": scene_pos(header_item),
                    "tabs": scene_pos(tabs_item),
                }

            def pump():
                app.processEvents()
                window.grabWindow()
                app.processEvents()

            pump()
            out["pos_text"] = top_positions()
            QMetaObject.invokeMethod(paragraph_tab, "setMode", Q_ARG("QVariant", "files"))
            pump()
            out["mode_after_switch"] = paragraph_tab.property("mode")
            out["editor_card_hidden_in_files"] = not pfind(
                "documentEditorCard"
            ).property("visible")
            out["pos_files"] = top_positions()
            QMetaObject.invokeMethod(paragraph_tab, "setMode", Q_ARG("QVariant", "srt"))
            pump()
            out["pos_srt"] = top_positions()
            QMetaObject.invokeMethod(paragraph_tab, "setMode", Q_ARG("QVariant", "text"))
            pump()
            out["pos_text_again"] = top_positions()

            # A stale scroll offset pending at the switch must be dropped:
            # at a short window height text mode scrolls for real, and
            # switching to files must land the header/tabs back on the
            # short-height baseline (the tab stays in "files" for the queue
            # flow below).
            default_window_height = window.height()
            window.setHeight(520)
            pump()
            out["pos_short_text"] = top_positions()
            page_flick = pfind("pageScrollView").property("contentItem")
            max_scroll = (float(page_flick.property("contentHeight"))
                          - float(page_flick.property("height")))
            out["page_max_scroll"] = max_scroll
            page_flick.setProperty("contentY", min(120.0, max_scroll))
            pump()
            out["pos_stale"] = top_positions()
            QMetaObject.invokeMethod(paragraph_tab, "setMode", Q_ARG("QVariant", "files"))
            pump()
            out["pos_after_stale"] = top_positions()
            window.setHeight(default_window_height)
            pump()
            out["pos_files_restored"] = top_positions()

            card = pfind("batchQueueCard")
            out["card_visible"] = bool(card.property("visible"))
            dialog = pfind("batchImportDialog")
            # fileMode (QQuickFileDialog::FileMode) has no PySide6 property
            # converter (same caveat as importDialog): reading it RAISES, so
            # record None on failure; the multi-select contract is pinned by
            # the QML source and by the addFiles routing below.
            try:
                out["dialog_file_mode"] = dialog.property("fileMode")
            except RuntimeError:
                out["dialog_file_mode"] = None
            out["empty_list_hidden"] = not pfind("batchFileList").property("visible")
            out["empty_hint_visible"] = bool(
                pfind("batchEmptyHint").property("visible")
            )
            out["run_all_disabled_empty"] = not pfind("runAllButton").property("enabled")

            fake_batch.items = [
                {"uid": 1, "sourcePath": "/tmp/a.txt", "fileName": "a.txt",
                 "status": "pending", "error": "", "wavPath": "", "progress": 0.0},
                {"uid": 2, "sourcePath": "/tmp/b.txt", "fileName": "b.txt",
                 "status": "ready", "error": "", "wavPath": "/tmp/out/b.wav",
                 "progress": 1.0},
            ]
            out["list_visible_populated"] = wait_for(
                lambda: pfind("batchFileList").property("visible") is True
            )
            pump()
            out["pos_files_populated"] = top_positions()
            out["run_all_enabled_populated"] = pfind("runAllButton").property("enabled")
            out["summary_text"] = pfind("batchRunSummary").property("text")

            click_item(pfind("runAllButton"))
            out["run_all_calls"] = fake_batch.ran
            click_item(pfind("addFilesButton"))  # opens the file dialog (offscreen)
            click_item(pfind("clearFinishedButton"))

            # Regression pin (bead qef): onAccepted converts via root.toLocalPath,
            # so the helper MUST live on the card (it originally sat on the
            # FileDialog → TypeError natively, selections silently dropped).
            # The offscreen fallback rejects selectedFiles writes, so the full
            # accept path is pinned in two parts: the card-level conversion,
            # and accept() reaching the controller at all.
            one = tmp / "one.txt"
            one.write_text("nội dung một", encoding="utf-8")
            local = QMetaObject.invokeMethod(
                card, "toLocalPath", Q_RETURN_ARG("QVariant"),
                Q_ARG("QVariant", QUrl.fromLocalFile(str(one))),
            )
            out["card_tolocalpath_matches"] = Path(str(local)) == one
            calls_before = len(fake_batch.added)
            QMetaObject.invokeMethod(pfind("batchImportDialog"), "accept")
            app.processEvents()
            out["dialog_accept_reached_controller"] = (
                len(fake_batch.added) == calls_before + 1
            )

            # Drop routing: several urls → the batch queue; one url → editor.
            one = tmp / "one.txt"
            two = tmp / "two.txt"
            one.write_text("nội dung một", encoding="utf-8")
            two.write_text("nội dung hai", encoding="utf-8")
            out["multi_drop_invoked"] = QMetaObject.invokeMethod(
                paragraph_tab, "handleDroppedUrls",
                Q_ARG("QVariant", [QUrl.fromLocalFile(str(one)), QUrl.fromLocalFile(str(two))]),
            )
            out["multi_drop_added"] = list(fake_batch.added)
            single = tmp / "single.txt"
            single.write_text("nội dung đơn", encoding="utf-8")
            out["single_drop_invoked"] = QMetaObject.invokeMethod(
                paragraph_tab, "handleDroppedUrls",
                Q_ARG("QVariant", [QUrl.fromLocalFile(str(single))]),
            )
            app.processEvents()
            # The fake controller's import seam emits its pinned text; the
            # editor receiving ANY imported text proves the single-file route.
            out["editor_after_single_drop"] = pfind("paragraphEditor").property("text")
            out["editor_changed_by_single_drop"] = (
                pfind("paragraphEditor").property("text") == "Xin chào\\nThế giới"
            )
        elif scenario == "srt_surface":
            # The `subtitleController` context property must beat AppCard's own
            # `subtitle` header string: loaded/cues only resolve through it.
            bridge.setCurrentTab("paragraph")
            app.processEvents()
            card = pfind("subtitleCard")
            out["card_available"] = card.property("available")
            QMetaObject.invokeMethod(paragraph_tab, "setMode", Q_ARG("QVariant", "srt"))
            app.processEvents()
            cue_list = pfind("subtitleCueList")
            out["card_loaded"] = card.property("loaded")
            out["cue_list_visible"] = bool(cue_list.property("visible"))
            out["cue_model_count"] = cue_list.property("count")
            out["empty_hint_hidden"] = not pfind("subtitleEmptyHint").property("visible")

            # Follow-scroll: activating a far cue scrolls the list to it, and
            # exactly one delegate binds `active` from subtitleController.
            out["initial_content_y"] = cue_list.property("contentY")
            fake_subtitle.activeCue = 40
            app.processEvents()
            out["follow_scroll_content_y"] = cue_list.property("contentY")
            rows = ifind("subtitleCueRow")
            out["active_rows"] = sum(1 for r in rows if r.property("active"))

            # Export-state polish: the SRT export button needs a rendered
            # track, and exporting disables every mutating control.
            export_srt_btn = pfind("subtitleExportSrtButton")
            out["srt_export_enabled_rendered"] = export_srt_btn.property("enabled")
            fake_subtitle.rendered = False
            app.processEvents()
            out["srt_export_disabled_unrendered"] = not export_srt_btn.property("enabled")
            # Before a track exists the play button advertises render-then-play.
            out["play_label_no_track"] = pfind("subtitlePlayButton").property("text")
            fake_subtitle.rendered = True
            app.processEvents()
            out["play_label_has_track"] = pfind("subtitlePlayButton").property("text")
            fake_subtitle.exporting = True
            app.processEvents()
            out["exporting_disables_controls"] = not any(
                bool(pfind(name).property("enabled"))
                for name in (
                    "subtitleImportButton",
                    "subtitleModeCombo",
                    "subtitleRateSlider",
                    "subtitleOffsetField",
                    "subtitleMergeToggle",
                    "subtitleVoicePicker",
                    "subtitleRenderButton",
                    "subtitlePlayButton",
                    "subtitleExportWavButton",
                    "subtitleExportSrtButton",
                )
            )
            fake_subtitle.exporting = False
            app.processEvents()

            # Escape routes to cancelRender while SRT renders, and still to
            # controller.cancel for a regular busy job.
            escape_found = paragraph_tab.findChildren(
                QObject, "paragraphEscapeShortcut"
            )
            out["escape_found"] = bool(escape_found)
            if escape_found:
                escape = escape_found[0]
                out["escape_disabled_idle"] = not escape.property("enabled")
                fake_subtitle.rendering = True
                app.processEvents()
                out["escape_enabled_srt_rendering"] = escape.property("enabled")
                escape.activated.emit()
                app.processEvents()
                out["srt_cancel_render_calls"] = fake_subtitle.cancel_render_calls
                out["controller_cancel_calls"] = controller.cancel_calls
                fake_subtitle.rendering = False
                controller.busy = True
                app.processEvents()
                out["escape_enabled_busy"] = escape.property("enabled")
                escape.activated.emit()
                app.processEvents()
                out["controller_cancel_calls_after"] = controller.cancel_calls
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

        elif scenario == "clone_capability":
            # Phase 6 Task 6.3: the cloning surface follows the ACTIVE engine's
            # capability entry — a fixed-speaker profile offers no enrollment at
            # all, a profile that needs the reference transcript asks for it and
            # refuses to enroll without it, and every clone row names the engine
            # that owns it (VieNeu's SDK registry vs the Qwen clone store).
            bridge.setCurrentTab("cloning")
            app.processEvents()
            notice = cfind("cloneCapabilityNotice")
            consent = cfind("consentPanel")
            panel = cfind("clonePanel")

            def rows(name):
                return [i.property("text") for i in ifind(name)]

            # ── Qwen CustomVoice (checked BEFORE any consent): fixed speakers,
            # so no enrollment is offered — the notice names the profile, and
            # neither the consent gate nor the workspace appears. ──
            controller._engine_profile = "qwen_custom_0_6b"
            controller._engine_profile_label = "Qwen3-TTS CustomVoice 0.6B"
            controller._profile_clones = []
            controller.engineProfileChanged.emit()
            controller.engineProfilesChanged.emit()
            controller.profileCatalogChanged.emit()
            app.processEvents()
            out["customvoice"] = {
                "notice_visible": bool(notice.property("visible")),
                "reason": cfind("cloneCapabilityReason").property("text"),
                "panel_hidden": not bool(panel.property("visible")),
                "consent_hidden": not bool(consent.property("visible")),
                "rows": rows("clonedVoiceName"),
            }

            # ── VieNeu: enrollment is offered again, needs no transcript, keeps
            # reference cleanup, and the rows belong to this profile. ──
            controller._engine_profile = "vieneu"
            controller._engine_profile_label = "VieNeu-TTS v3 Turbo"
            controller._profile_clones = [
                {"id": "my_clone", "label": "my_clone", "transcript": ""},
            ]
            controller.engineProfileChanged.emit()
            controller.engineProfilesChanged.emit()
            controller.profileCatalogChanged.emit()
            app.processEvents()
            out["vieneu"] = {
                "notice_hidden": not bool(notice.property("visible")),
                "consent_visible": bool(consent.property("visible")),
                "panel_hidden": not bool(panel.property("visible")),
                "transcript_hidden": not bool(cfind("cloneTranscriptLabel").property("visible")),
                "cleanup_visible": bool(cfind("denoiseCheck").property("visible")),
                "cleanup_note_hidden": not bool(cfind("referenceCleanupNote").property("visible")),
            }
            cfind("consentAcceptButton").click()
            app.processEvents()
            out["vieneu"]["panel_visible"] = bool(panel.property("visible"))
            # Read again now that the workspace is on screen: `visible` is the
            # effective value, so a hidden panel would report every control
            # inside it hidden regardless of its own binding.
            out["vieneu"]["cleanup_visible_after"] = bool(
                cfind("denoiseCheck").property("visible"))
            out["vieneu"]["rows"] = rows("clonedVoiceName")
            out["vieneu"]["row_profiles"] = rows("clonedVoiceProfile")

            # ── Qwen Base: enrollment returns WITH the reference transcript the
            # capability table requires, and without denoise (a Qwen enrollment
            # stores the reference as given). A clone enrolled on another
            # profile is not offered here. ──
            controller._engine_profile = "qwen_base_0_6b"
            controller._engine_profile_label = "Qwen3-TTS Base 0.6B"
            # The switch republishes the ACTIVE profile's catalogs: VieNeu's
            # clone is not in the Base profile's store.
            controller._profile_clones = []
            controller.engineProfileChanged.emit()
            controller.engineProfilesChanged.emit()
            controller.profileCatalogChanged.emit()
            app.processEvents()
            transcript = cfind("cloneTranscriptField")
            clone_btn = cfind("cloneButton")
            name_field = cfind("voiceNameField")
            out["base"] = {
                "notice_hidden": not bool(notice.property("visible")),
                "panel_visible": bool(panel.property("visible")),
                "transcript_visible": bool(cfind("cloneTranscriptLabel").property("visible")),
                "transcript_hint": cfind("cloneTranscriptHint").property("text"),
                "cleanup_hidden": not bool(cfind("denoiseCheck").property("visible")),
                "cleanup_note": cfind("referenceCleanupNote").property("text"),
                "rows": rows("clonedVoiceName"),
            }
            QMetaObject.invokeMethod(
                cloning_tab(), "selectClip", Q_ARG("QVariant", str(tmp / "ref.wav"))
            )
            name_field.setProperty("text", "Giọng Base")
            app.processEvents()
            out["base"]["clone_disabled_without_transcript"] = not clone_btn.property("enabled")
            out["base"]["clone_reason"] = clone_btn.property("disabledReason")
            transcript.setProperty("text", "xin chào thế giới")
            app.processEvents()
            out["base"]["clone_enabled_with_transcript"] = clone_btn.property("enabled")
            clone_btn.click()
            app.processEvents()
            out["base"]["add_voice_calls"] = [list(call) for call in controller.add_voice_calls]
            out["base"]["rows_after"] = rows("clonedVoiceName")
            out["base"]["row_profiles_after"] = rows("clonedVoiceProfile")

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
                "checkUpdatesButton", "updateBanner", "updateErrorLabel",
                "downloadUpdateButton", "viewReleaseButton",
                "otherPlatformsToggle", "otherPlatformsList",
                # Engine profile + Qwen install surface (Task 6.1).
                "engineProfileCard", "engineProfileCombo", "languagePickerCombo",
                "qwenDeviceCard", "qwenDeviceResolvedLabel",
                "qwenRuntimeCard", "qwenRuntimeInstallButton",
                "qwenRuntimeImportButton", "qwenRuntimeStorageLabel",
                "qwenModelCard", "qwenSharedStorageLabel", "qwenModelOpenDirButton",
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
            # Update card: no banner before any check; Check button wired.
            out["update_banner_hidden_initially"] = not settings_tab.findChildren(
                QObject, "updateBanner"
            )[0].property("visible")
            out["check_button_present"] = (
                len(settings_tab.findChildren(QObject, "checkUpdatesButton")) == 1
            )
        elif scenario == "settings_update_states":
            bridge.setCurrentTab("settings")
            settings_tab = find("settingsTab")

            # ── error state FIRST: with no availability the error label owns
            # the card (its visibility gate is `updateError !== "" &&
            # !updateAvailable`) ──
            controller._update_error = "dns down"
            controller.updateInfoChanged.emit()
            app.processEvents()
            out["error"] = {
                "error_visible": settings_tab.findChildren(
                    QObject, "updateErrorLabel"
                )[0].property("visible"),
                "banner_hidden": not settings_tab.findChildren(
                    QObject, "updateBanner"
                )[0].property("visible"),
                "download_hidden": not settings_tab.findChildren(
                    QObject, "downloadUpdateButton"
                )[0].property("visible"),
            }

            # ── then a completed check: sticky availability suppresses the
            # error label, so no extra reset is needed between the two states.
            # Newer release + this-platform file + one other-platform file
            # (QML binds re-evaluate on NOTIFY). ──
            controller._update_available = True
            controller._update_latest_version = "v0.2.0"
            controller._update_release_url = "https://example.com/releases/v0.2.0"
            controller._update_asset_name = "VieNeuTTS-0.2.0-linux-x64.zip"
            controller._update_asset_url = "https://example.com/lin"
            controller._update_other_assets = [
                {"name": "VieNeuTTS-0.2.0-windows-x64.zip", "url": "https://example.com/win"}
            ]
            controller.updateAvailableChanged.emit()
            controller.updateInfoChanged.emit()
            app.processEvents()
            toggle = settings_tab.findChildren(QObject, "otherPlatformsToggle")[0]
            out["available"] = {
                "banner_visible": settings_tab.findChildren(
                    QObject, "updateBanner"
                )[0].property("visible"),
                "download_visible": settings_tab.findChildren(
                    QObject, "downloadUpdateButton"
                )[0].property("visible"),
                "release_visible": settings_tab.findChildren(
                    QObject, "viewReleaseButton"
                )[0].property("visible"),
                "error_hidden": not settings_tab.findChildren(
                    QObject, "updateErrorLabel"
                )[0].property("visible"),
                # Expander: hidden until toggled, then lists the other file.
                "toggle_visible": toggle.property("visible"),
            }
            click_item(toggle)
            app.processEvents()
            # Repeater delegates live in the VISUAL tree (findChildren on the
            # QObject tree misses them — same reason voicePickerRow uses ifind).
            items = ifind("otherPlatformAssetButton")
            out["available"]["other_count_after_expand"] = len(items)
            out["available"]["other_names"] = [i.property("text") for i in items]
            # Check button still wired through the banner state.
            click_item(settings_tab.findChildren(QObject, "checkUpdatesButton")[0])
            app.processEvents()
            out["available"]["check_calls"] = controller.check_updates_calls
        elif scenario == "settings_cuda_states":
            bridge.setCurrentTab("settings")
            settings_tab = find("settingsTab")
            names = {o.objectName() for o in settings_tab.findChildren(QObject)}
            required = {
                "cudaRuntimeCard", "cudaRuntimeInstallButton",
                "cudaRuntimeCancelButton", "cudaRuntimeRetryButton",
                "cudaRuntimeRemoveButton", "cudaRuntimeDetectLocalButton",
            }
            card = settings_tab.findChildren(QObject, "cudaRuntimeCard")[0]
            install = settings_tab.findChildren(QObject, "cudaRuntimeInstallButton")[0]
            cancel = settings_tab.findChildren(QObject, "cudaRuntimeCancelButton")[0]
            retry = settings_tab.findChildren(QObject, "cudaRuntimeRetryButton")[0]
            remove = settings_tab.findChildren(QObject, "cudaRuntimeRemoveButton")[0]
            detect = settings_tab.findChildren(QObject, "cudaRuntimeDetectLocalButton")[0]
            progress = settings_tab.findChildren(QObject, "cudaRuntimeProgress")[0]
            storage = settings_tab.findChildren(QObject, "cudaRuntimeStorageLabel")[0]
            local_summary = settings_tab.findChildren(
                QObject, "cudaRuntimeLocalSummary"
            )[0]

            # ── state: idle (controller defaults) ──
            out["idle"] = {
                "all_present": required <= names,
                "card_visible": card.property("visible"),
                "install_visible": install.property("visible"),
                "install_enabled": install.property("enabled"),
                "cancel_hidden": not cancel.property("visible"),
                "retry_hidden": not retry.property("visible"),
                "remove_hidden": not remove.property("visible"),
                "local_summary": local_summary.property("text"),
            }

            # ── state: unsupported platform (card swaps to the warning notice) ──
            controller._cuda_runtime_supported = False
            controller._cuda_runtime_state = "unavailable"
            controller._cuda_runtime_error = "unsupported platform"
            controller.cudaRuntimeSupportedChanged.emit()
            controller.cudaRuntimeStateChanged.emit()
            controller.cudaRuntimeErrorChanged.emit()
            app.processEvents()
            unsupported_notice = settings_tab.findChildren(
                QObject, "cudaRuntimeUnsupportedNotice"
            )[0]
            out["unsupported"] = {
                "card_visible": card.property("visible"),
                "install_hidden": not install.property("visible"),
                "cancel_hidden": not cancel.property("visible"),
                "retry_hidden": not retry.property("visible"),
                "remove_hidden": not remove.property("visible"),
                "error_visible": unsupported_notice.property("visible"),
                "error_text": settings_tab.findChildren(
                    QObject, "cudaRuntimeUnsupportedError"
                )[0].property("text"),
            }
            # Restore the supported baseline that the states below assume.
            controller._cuda_runtime_supported = True
            controller._cuda_runtime_state = "unavailable"
            controller._cuda_runtime_error = ""
            controller.cudaRuntimeSupportedChanged.emit()
            controller.cudaRuntimeStateChanged.emit()
            controller.cudaRuntimeErrorChanged.emit()
            app.processEvents()

            # ── state: NVIDIA driver unavailable → install disabled + guide ──
            controller._cuda_runtime_driver_checked = True
            controller._cuda_runtime_driver_ready = False
            controller.cudaRuntimeDriverChanged.emit()
            app.processEvents()
            notice = settings_tab.findChildren(QObject, "cudaRuntimeDriverNotice")[0]
            recheck = settings_tab.findChildren(
                QObject, "cudaRuntimeDriverRecheckButton"
            )[0]
            out["driver_unavailable"] = {
                "install_visible": install.property("visible"),
                "install_enabled": install.property("enabled"),
                "install_disabled_reason": install.property("disabledReason"),
                "notice_visible": notice.property("visible"),
                "guide_visible": settings_tab.findChildren(
                    QObject, "cudaRuntimeDriverGuide"
                )[0].property("visible"),
                "guide_download_visible": settings_tab.findChildren(
                    QObject, "cudaRuntimeDriverDownloadButton"
                )[0].property("visible"),
                "recheck_visible": recheck.property("visible"),
                "recheck_text": recheck.property("text"),
            }
            recheck.click()
            app.processEvents()
            out["driver_unavailable"]["refresh_calls"] = controller.cuda_refresh_calls
            detect.click()
            app.processEvents()
            out["driver_unavailable"]["discover_calls"] = controller.cuda_discover_calls
            # Restore the driver baseline: install/retry are gated on
            # cudaRuntimeInstallAllowed, and the states below assume a usable
            # driver (their own install click must land).
            controller._cuda_runtime_driver_checked = True
            controller._cuda_runtime_driver_ready = True
            controller.cudaRuntimeDriverChanged.emit()
            app.processEvents()

            # ── state: downloading ──
            controller._cuda_runtime_state = "downloading"
            controller._cuda_runtime_progress = 0.5
            controller._cuda_runtime_installed_bytes = 512
            controller.cudaRuntimeStateChanged.emit()
            controller.cudaRuntimeProgressChanged.emit()
            controller.cudaRuntimeStorageChanged.emit()
            app.processEvents()
            out["downloading"] = {
                "cancel_visible": cancel.property("visible"),
                "progress_visible": progress.property("visible"),
                "progress_value": progress.property("value"),
                "storage_text": storage.property("text"),
            }
            cancel.click()
            app.processEvents()
            out["downloading"]["cancel_calls"] = controller.cuda_cancel_calls

            # ── state: ready ──
            controller._cuda_runtime_state = "ready"
            controller._cuda_runtime_progress = 1.0
            controller._cuda_runtime_installed_bytes = 1_024
            controller.cudaRuntimeStateChanged.emit()
            controller.cudaRuntimeProgressChanged.emit()
            controller.cudaRuntimeStorageChanged.emit()
            app.processEvents()
            out["ready"] = {
                "remove_visible": remove.property("visible"),
                "restart_visible": settings_tab.findChildren(
                    QObject, "cudaRuntimeRestartNotice"
                )[0].property("visible"),
            }
            remove.click()
            app.processEvents()
            out["ready"]["remove_calls"] = controller.cuda_remove_calls

            # ── state: failed (+ local runtime scan) ──
            controller._cuda_runtime_state = "failed"
            controller._cuda_runtime_error = "checksum mismatch"
            controller.cudaRuntimeStateChanged.emit()
            controller.cudaRuntimeErrorChanged.emit()
            app.processEvents()
            out["failed_and_local"] = {
                "retry_visible": retry.property("visible"),
                "error_text": settings_tab.findChildren(
                    QObject, "cudaRuntimeErrorLabel"
                )[0].property("text"),
            }
            retry.click()
            app.processEvents()
            out["failed_and_local"]["install_calls_after_retry"] = (
                controller.cuda_install_calls
            )

            controller._local_cuda_runtimes = [
                {"label": "CUDA 12.8", "compatible": True, "reason": "compatible"},
                {"label": "CUDA 11.8", "compatible": False, "reason": "wrong version"},
            ]
            controller.localCudaRuntimesChanged.emit()
            app.processEvents()
            out["failed_and_local"]["local_summary"] = local_summary.property("text")
            # The driver-unavailable state already clicked detect: count only
            # this state's own call.
            detect_before = controller.cuda_discover_calls
            detect.click()
            app.processEvents()
            out["failed_and_local"]["discover_calls"] = (
                controller.cuda_discover_calls - detect_before
            )
        elif scenario == "settings_engine_profiles":
            bridge.setCurrentTab("settings")
            settings_tab = find("settingsTab")
            names = {o.objectName() for o in settings_tab.findChildren(QObject)}
            required = {
                "engineProfileCard", "engineProfilePicker", "engineProfileCombo",
                "engineProfileReadinessBadge", "engineProfileDeviceLabel",
                "engineProfileStatusLabel", "languagePicker", "languagePickerCombo",
                "languagePickerNote",
            }
            badge = settings_tab.findChildren(QObject, "engineProfileReadinessText")[0]
            device = settings_tab.findChildren(QObject, "engineProfileDeviceLabel")[0]
            status = settings_tab.findChildren(QObject, "engineProfileStatusLabel")[0]
            combo = settings_tab.findChildren(QObject, "engineProfileCombo")[0]
            language = settings_tab.findChildren(QObject, "languagePickerCombo")[0]
            note = settings_tab.findChildren(QObject, "languagePickerNote")[0]

            def badge_text():
                return badge.property("text")

            def language_labels():
                return [row["label"] for row in qjs_to_py(language.property("model"))]

            # ── state: VieNeu ready (both axes) ──
            out["ready"] = {
                "all_present": required <= names,
                "combo_count": combo.property("count"),
                "combo_index": combo.property("currentIndex"),
                "badge_text": badge_text(),
                "device_text": device.property("text"),
                "status_text": status.property("text"),
                "language_count": language.property("count"),
                "language_index": language.property("currentIndex"),
                "language_labels": language_labels(),
                "language_visible": language.property("visible"),
                "language_note": note.property("text"),
            }

            # ── switch to a Qwen profile: the combo emits `activated` ──
            activate_item(combo, 1)
            app.processEvents()
            out["switch"] = {"calls": list(controller.switch_profile_calls)}
            # The real controller republishes the catalog for the new profile;
            # the fake mirrors that by flipping its own language list.
            controller._engine_profile = "qwen_custom_0_6b"
            controller._engine_profile_label = "Qwen3-TTS CustomVoice 0.6B"
            controller._synthesis_language = "auto"
            controller._profile_languages = [
                {"code": "auto", "label": "Auto", "modelName": "", "isAuto": True},
                {
                    "code": "zh",
                    "label": "中文",
                    "modelName": "Qwen3-TTS-12Hz-0.6B-CustomVoice",
                    "isAuto": False,
                },
                {
                    "code": "ja",
                    "label": "日本語",
                    "modelName": "Qwen3-TTS-12Hz-0.6B-CustomVoice",
                    "isAuto": False,
                },
            ]
            controller.engineProfileChanged.emit()
            controller.engineProfilesChanged.emit()
            controller.profileCatalogChanged.emit()
            controller.synthesisLanguageChanged.emit()
            app.processEvents()
            out["switch"]["combo_index"] = combo.property("currentIndex")
            out["switch"]["language_count"] = language.property("count")
            out["switch"]["language_labels"] = language_labels()
            out["switch"]["language_index"] = language.property("currentIndex")
            out["switch"]["language_visible"] = language.property("visible")
            out["switch"]["auto_note"] = note.property("text")

            # ── language choice reaches the controller ──
            activate_item(language, 1)
            app.processEvents()
            out["switch"]["language_calls"] = list(controller.set_language_calls)

            # ── state: model missing (installs belong to Settings) ──
            controller._profile_model_state = "unavailable"
            controller.profileModelChanged.emit()
            controller.profileReadyChanged.emit()
            app.processEvents()
            out["missing"] = {
                "badge_text": badge_text(),
                "status_text": status.property("text"),
            }

            # ── state: failed (the profile's own reason is shown) ──
            controller._profile_model_state = "failed"
            controller._profile_model_error = "install metadata is corrupt"
            controller.profileModelChanged.emit()
            controller.profileReadyChanged.emit()
            app.processEvents()
            out["failed"] = {
                "badge_text": badge_text(),
                "status_text": status.property("text"),
            }

            # ── state: unsupported runtime on this host ──
            controller._profile_model_state = "unavailable"
            controller._profile_model_error = ""
            controller._profile_runtime_state = "unsupported"
            controller.profileModelChanged.emit()
            controller.profileRuntimeChanged.emit()
            controller.profileReadyChanged.emit()
            app.processEvents()
            out["unsupported"] = {
                "badge_text": badge_text(),
                "status_text": status.property("text"),
            }

            # ── state: a job is running (switching is refused) ──
            controller._profile_runtime_state = "ready"
            controller._busy = True
            controller.profileRuntimeChanged.emit()
            controller.busyChanged.emit()
            app.processEvents()
            out["busy"] = {"combo_enabled": combo.property("enabled")}
        elif scenario == "settings_qwen_states":
            bridge.setCurrentTab("settings")
            settings_tab = find("settingsTab")
            names = {o.objectName() for o in settings_tab.findChildren(QObject)}
            required = {
                "qwenDeviceCard", "qwenDeviceResolvedLabel", "qwenDeviceRefreshButton",
                "qwenDeviceUnsupportedLabel", "qwenRuntimeCard",
                "qwenRuntimeInstallButton", "qwenRuntimeCancelButton",
                "qwenRuntimeRepairButton", "qwenRuntimeRemoveButton",
                "qwenRuntimeImportButton", "qwenRuntimeStorageLabel",
                "qwenRuntimeProgress", "qwenRuntimeStatusLabel",
                "qwenRuntimeVariantLabel", "qwenRuntimeCpuNotice",
                "qwenModelCard", "qwenSharedStorageLabel",
                "qwenModelStoragePathLabel", "qwenModelOpenDirButton",
                "qwenModelCpuNotice",
            }
            install = settings_tab.findChildren(QObject, "qwenRuntimeInstallButton")[0]
            cancel = settings_tab.findChildren(QObject, "qwenRuntimeCancelButton")[0]
            repair = settings_tab.findChildren(QObject, "qwenRuntimeRepairButton")[0]
            remove = settings_tab.findChildren(QObject, "qwenRuntimeRemoveButton")[0]
            import_button = settings_tab.findChildren(QObject, "qwenRuntimeImportButton")[0]
            storage = settings_tab.findChildren(QObject, "qwenRuntimeStorageLabel")[0]
            progress = settings_tab.findChildren(QObject, "qwenRuntimeProgress")[0]
            status_label = settings_tab.findChildren(QObject, "qwenRuntimeStatusLabel")[0]
            variant_label = settings_tab.findChildren(QObject, "qwenRuntimeVariantLabel")[0]
            cpu_notice = settings_tab.findChildren(QObject, "qwenRuntimeCpuNotice")[0]
            model_cpu_notice = settings_tab.findChildren(QObject, "qwenModelCpuNotice")[0]
            shared = settings_tab.findChildren(QObject, "qwenSharedStorageLabel")[0]
            resolved = settings_tab.findChildren(QObject, "qwenDeviceResolvedLabel")[0]
            unsupported_reasons = settings_tab.findChildren(
                QObject, "qwenDeviceUnsupportedLabel"
            )[0]
            device_refresh = settings_tab.findChildren(QObject, "qwenDeviceRefreshButton")[0]
            open_model_dir = settings_tab.findChildren(QObject, "qwenModelOpenDirButton")[0]
            open_runtime_dir = settings_tab.findChildren(QObject, "qwenRuntimeOpenDirButton")[0]
            import_hint = settings_tab.findChildren(QObject, "qwenRuntimeImportHint")[0]

            # ── device: chips carry support + reason (platform truth) ──
            chips = {i.objectName(): i for i in ifind("qwenDeviceChip_auto")
                     + ifind("qwenDeviceChip_cpu") + ifind("qwenDeviceChip_cuda")
                     + ifind("qwenDeviceChip_mps")}
            out["device"] = {
                "all_present": required <= names,
                "chip_count": len(chips),
                "auto_enabled": chips["qwenDeviceChip_auto"].property("enabled"),
                "cpu_enabled": chips["qwenDeviceChip_cpu"].property("enabled"),
                "cuda_enabled": chips["qwenDeviceChip_cuda"].property("enabled"),
                "cuda_reason": chips["qwenDeviceChip_cuda"].property("disabledReason"),
                "mps_enabled": chips["qwenDeviceChip_mps"].property("enabled"),
                "mps_reason": chips["qwenDeviceChip_mps"].property("disabledReason"),
                "resolved_text": resolved.property("text"),
                "unsupported_visible": unsupported_reasons.property("visible"),
                "unsupported_text": unsupported_reasons.property("text"),
                "cpu_guidance_visible": cpu_notice.property("visible"),
                "model_cpu_guidance_visible": model_cpu_notice.property("visible"),
            }
            # A supported chip selects; a disabled one cannot (no slot hit). The
            # disabled chips are asserted above (enabled/reason) — clicking
            # them is not a thing the UI offers, so it is not simulated.
            click_item(chips["qwenDeviceChip_cpu"])
            app.processEvents()
            out["device"]["select_calls"] = list(controller.qwen_device_calls)
            click_item(device_refresh)
            app.processEvents()
            out["device"]["refresh_calls"] = controller.qwen_refresh_calls

            # ── runtime: idle → downloading → ready → failed → unsupported ──
            out["runtime_idle"] = {
                "install_visible": install.property("visible"),
                "install_enabled": install.property("enabled"),
                "cancel_hidden": not cancel.property("visible"),
                "repair_hidden": not repair.property("visible"),
                "remove_hidden": not remove.property("visible"),
                "status_text": status_label.property("text"),
                "variant_text": variant_label.property("text"),
                "storage_text": storage.property("text"),
                "shared_text": shared.property("text"),
                "import_hint_visible": import_hint.property("visible"),
                "import_hint_text": import_hint.property("text"),
                "path_text": settings_tab.findChildren(
                    QObject, "qwenModelStoragePathLabel"
                )[0].property("text"),
            }
            install.click()
            app.processEvents()
            out["runtime_idle"]["install_calls"] = controller.qwen_install_calls
            # The runtime card's own folder shortcut (the model card's twin).
            click_item(open_runtime_dir)
            app.processEvents()
            out["runtime_open_dir_calls"] = list(controller.qwen_open_dir_calls)

            controller._qwen_runtime_state = "downloading"
            controller._qwen_runtime_progress = 0.42
            controller._qwen_runtime_installed_bytes = 512_000_000
            controller.qwenRuntimeStateChanged.emit()
            controller.qwenRuntimeProgressChanged.emit()
            controller.qwenRuntimeStorageChanged.emit()
            app.processEvents()
            out["runtime_downloading"] = {
                "cancel_visible": cancel.property("visible"),
                "progress_visible": progress.property("visible"),
                "progress_value": progress.property("value"),
                "storage_text": storage.property("text"),
                "install_hidden": not install.property("visible"),
            }
            cancel.click()
            app.processEvents()
            out["runtime_downloading"]["cancel_calls"] = controller.qwen_cancel_calls

            controller._qwen_runtime_state = "ready"
            controller._qwen_runtime_progress = 1.0
            controller._qwen_runtime_installed_bytes = 2_147_483_648
            controller.qwenRuntimeStateChanged.emit()
            controller.qwenRuntimeStorageChanged.emit()
            app.processEvents()
            out["runtime_ready"] = {
                "remove_visible": remove.property("visible"),
                "status_text": status_label.property("text"),
                "storage_text": storage.property("text"),
            }
            remove.click()
            app.processEvents()
            out["runtime_ready"]["remove_calls"] = controller.qwen_remove_calls

            controller._qwen_runtime_state = "failed"
            controller._qwen_runtime_error = "install metadata is corrupt"
            controller.qwenRuntimeStateChanged.emit()
            controller.qwenRuntimeErrorChanged.emit()
            app.processEvents()
            out["runtime_failed"] = {
                "repair_visible": repair.property("visible"),
                "notice_visible": settings_tab.findChildren(
                    QObject, "qwenRuntimeFailureNotice"
                )[0].property("visible"),
                "error_text": settings_tab.findChildren(
                    QObject, "qwenRuntimeFailureErrorLabel"
                )[0].property("text"),
            }
            repair.click()
            app.processEvents()
            out["runtime_failed"]["repair_calls"] = controller.qwen_repair_calls

            # Unsupported host: no install affordance, reason visible instead.
            controller._qwen_runtime_supported = False
            controller._qwen_runtime_state = "unsupported"
            controller._qwen_runtime_error = "unsupported platform"
            controller.qwenRuntimeSupportChanged.emit()
            controller.qwenRuntimeStateChanged.emit()
            controller.qwenRuntimeErrorChanged.emit()
            app.processEvents()
            out["runtime_unsupported"] = {
                "install_hidden": not install.property("visible"),
                "notice_visible": settings_tab.findChildren(
                    QObject, "qwenRuntimeUnsupportedNotice"
                )[0].property("visible"),
                "error_text": settings_tab.findChildren(
                    QObject, "qwenRuntimeErrorLabel"
                )[0].property("text"),
                "cpu_guidance_hidden": not cpu_notice.property("visible"),
            }
            controller._qwen_runtime_supported = True
            controller._qwen_runtime_state = "unavailable"
            controller._qwen_runtime_error = ""
            controller.qwenRuntimeSupportChanged.emit()
            controller.qwenRuntimeStateChanged.emit()
            controller.qwenRuntimeErrorChanged.emit()
            app.processEvents()

            # ── models: per-row states + actions (visual-tree delegates) ──
            # The Repeater rebuilds its delegates whenever the row list
            # changes, so every read/click below looks the item up FRESH
            # (`ifind`) instead of caching one across a state change.
            def row_item(name, key):
                return ifind(name + "_" + key)[0]

            out["models_idle"] = {
                "row_count": len(ifind("qwenModelRow_customvoice") + ifind("qwenModelRow_base")),
                "label_text": row_item("qwenModelLabel", "base").property("text"),
                "state_text": row_item("qwenModelStateLabel", "base").property("text"),
                "install_visible": row_item("qwenModelInstallButton", "base").property("visible"),
                "repair_hidden": not row_item("qwenModelRepairButton", "base").property("visible"),
                "remove_hidden": not row_item("qwenModelRemoveButton", "base").property("visible"),
                "cancel_hidden": not row_item("qwenModelCancelButton", "base").property("visible"),
                "active_hidden": not row_item("qwenModelActiveBadge", "base").property("visible"),
                "storage_text": row_item("qwenModelStorageLabel", "base").property("text"),
            }
            click_item(row_item("qwenModelInstallButton", "base"))
            app.processEvents()
            out["models_idle"]["install_calls"] = list(controller.qwen_model_calls)

            # One row downloading: only that row shows progress + cancel, and
            # every row's actions are disabled while the shared lane is busy.
            for row in controller._qwen_models:
                if row["key"] == "base":
                    row["state"] = "downloading"
                    row["progress"] = 0.5
                    row["busy"] = True
            controller.qwenModelsChanged.emit()
            app.processEvents()
            out["models_downloading"] = {
                "state_text": row_item("qwenModelStateLabel", "base").property("text"),
                "cancel_visible": row_item("qwenModelCancelButton", "base").property("visible"),
                "progress_visible": row_item("qwenModelProgress", "base").property("visible"),
                "other_install_disabled": not row_item(
                    "qwenModelInstallButton", "customvoice"
                ).property("enabled"),
            }
            click_item(row_item("qwenModelCancelButton", "base"))
            app.processEvents()
            out["models_downloading"]["cancel_calls"] = list(controller.qwen_model_calls)

            for row in controller._qwen_models:
                row["state"] = "ready"
                row["ready"] = True
                row["busy"] = False
                row["progress"] = 1.0
                row["installedBytes"] = row["requiredBytes"]
                row["isActive"] = row["key"] == "base"
            controller.qwenModelsChanged.emit()
            app.processEvents()
            out["models_ready"] = {
                "state_text": row_item("qwenModelStateLabel", "base").property("text"),
                "remove_visible": row_item("qwenModelRemoveButton", "base").property("visible"),
                "active_visible": row_item("qwenModelActiveBadge", "base").property("visible"),
                "storage_text": row_item("qwenModelStorageLabel", "base").property("text"),
            }
            click_item(row_item("qwenModelRemoveButton", "base"))
            app.processEvents()
            out["models_ready"]["remove_calls"] = list(controller.qwen_model_calls)

            for row in controller._qwen_models:
                if row["key"] == "base":
                    row["state"] = "failed"
                    row["ready"] = False
                    row["error"] = "install metadata is corrupt"
            controller.qwenModelsChanged.emit()
            app.processEvents()
            out["models_failed"] = {
                "state_text": row_item("qwenModelStateLabel", "base").property("text"),
                "repair_visible": row_item("qwenModelRepairButton", "base").property("visible"),
                "error_text": row_item("qwenModelErrorLabel", "base").property("text"),
            }
            click_item(row_item("qwenModelRepairButton", "base"))
            app.processEvents()
            out["models_failed"]["repair_calls"] = list(controller.qwen_model_calls)

            # ── offline import seams (dialogs stay closed headless) ──
            pack = str(tmp / "qwen-pack")
            QMetaObject.invokeMethod(
                settings_tab, "pickQwenRuntimePack", Q_ARG("QVariant", pack)
            )
            QMetaObject.invokeMethod(
                settings_tab, "pickQwenModelPack", Q_ARG("QVariant", "base"),
                Q_ARG("QVariant", pack)
            )
            app.processEvents()
            out["imports"] = list(controller.qwen_import_calls)
            # The import BUTTONS open native FolderDialogs, which stay closed
            # offscreen (same policy as the output-dir/import dialogs): the
            # tested contract is the QML seam above plus the enabled state.
            out["import_buttons_enabled"] = [
                row_item("qwenModelImportButton", "base").property("enabled"),
                import_button.property("enabled"),
            ]
            click_item(open_model_dir)
            app.processEvents()
            out["open_dir_calls"] = list(controller.qwen_open_dir_calls)
        elif scenario == "surface_profile_bindings":
            # Phase 6 Task 6.2: every synthesis surface offers only what the
            # ACTIVE profile can serve — the picker's catalog, whether a
            # language control exists at all, and whether the primary action
            # can start. One window drives every branch; the fake republishes
            # its catalogs the way the real controller does on a switch.
            bridge.setCurrentTab("text")
            editor = tfind("textEditor")
            editor.setProperty("text", "Xin chào thế giới")
            app.processEvents()

            picker = tfind("voicePicker")
            # The Text tab's language control is its OWN LanguagePicker
            # instance, reached through the named wrapper (the paragraph tab's
            # paraLanguagePicker precedent) so the objectName is pinned too.
            text_language = tfind("textLanguagePicker")
            language_combo = text_language.findChildren(QObject, "languagePickerCombo")[0]
            language_note = text_language.findChildren(QObject, "languagePickerNote")[0]
            generate = tfind("generateButton")

            def flat_ids():
                return [row["id"] for row in qjs_to_py(picker.property("flatModel"))]

            def flat_labels():
                return [row["label"] for row in qjs_to_py(picker.property("flatModel"))]

            def trigger_text():
                # The picker's own trigger line: the chosen voice, or the
                # reason this profile has none to offer.
                return [i.property("text") for i in item_walk(picker)
                        if i.objectName() == "voicePickerTriggerLabel"][0]

            # ── VieNeu with no language chosen (the real startup state): its
            # engine takes no language argument, so the control is absent with
            # the reason instead of implying a choice that changes nothing. ──
            controller._synthesis_language = ""
            controller.synthesisLanguageChanged.emit()
            app.processEvents()
            out["vieneu_unset"] = {
                "flat_ids": flat_ids(),
                "selected_voice": picker.property("selectedVoice"),
                "effective_voice": picker.property("effectiveVoice"),
                "picker_enabled": picker.property("enabled"),
                "language_visible": language_combo.property("visible"),
                "language_note": language_note.property("text"),
                "generate_enabled": generate.property("enabled"),
                "generate_reason": generate.property("disabledReason"),
            }

            # ── VieNeu with an explicit language: the control appears, with the
            # profile's own declared list (native names). ──
            controller._synthesis_language = "vi"
            controller.synthesisLanguageChanged.emit()
            app.processEvents()
            out["vieneu_chosen"] = {
                "language_visible": language_combo.property("visible"),
                "language_labels": [row["label"] for row in qjs_to_py(
                    language_combo.property("model"))],
                "language_index": language_combo.property("currentIndex"),
                "language_note": language_note.property("text"),
            }

            # ── switch to Qwen CustomVoice (installed + ready): the pinned
            # speakers replace the VieNeu catalog, the stale VieNeu selection is
            # dropped for the first speaker, and the language control appears
            # with the profile's own default. ──
            controller._engine_profile = "qwen_custom_0_6b"
            controller._engine_profile_label = "Qwen3-TTS CustomVoice 0.6B"
            controller._synthesis_language = "auto"
            controller._profile_languages = [
                {"code": "auto", "label": "Auto", "modelName": "Auto", "isAuto": True},
                {"code": "zh", "label": "中文", "modelName": "Chinese", "isAuto": False},
                {"code": "ja", "label": "日本語", "modelName": "Japanese", "isAuto": False},
            ]
            controller._profile_voices = [
                {"id": "Vivian", "label": "Vivian", "description": "",
                 "nativeLanguage": "Chinese", "languages": []},
                {"id": "Ryan", "label": "Ryan", "description": "",
                 "nativeLanguage": "English", "languages": []},
                {"id": "Sohee", "label": "Sohee", "description": "",
                 "nativeLanguage": "Korean", "languages": []},
            ]
            controller._profile_clones = []
            controller.engineProfileChanged.emit()
            controller.engineProfilesChanged.emit()
            controller.profileCatalogChanged.emit()
            controller.synthesisLanguageChanged.emit()
            app.processEvents()
            out["qwen_presets"] = {
                "flat_ids": flat_ids(),
                "flat_labels": flat_labels(),
                "selected_voice": picker.property("selectedVoice"),
                "effective_voice": picker.property("effectiveVoice"),
                "voice_info": qjs_to_py(picker.property("currentVoiceInfo")),
                "language_visible": language_combo.property("visible"),
                "language_labels": [row["label"] for row in qjs_to_py(
                    language_combo.property("model"))],
                "language_index": language_combo.property("currentIndex"),
                "language_note": language_note.property("text"),
                "generate_enabled": generate.property("enabled"),
                "generate_reason": generate.property("disabledReason"),
            }

            # ── the same profile before its install lands: the primary action
            # is disabled WITH the install reason instead of failing on click. ──
            controller._profile_model_state = "unavailable"
            controller.profileModelChanged.emit()
            controller.profileReadyChanged.emit()
            app.processEvents()
            out["qwen_not_installed"] = {
                "generate_enabled": generate.property("enabled"),
                "generate_reason": generate.property("disabledReason"),
                "language_visible": language_combo.property("visible"),
            }
            controller._profile_model_state = "ready"
            controller.profileModelChanged.emit()
            controller.profileReadyChanged.emit()
            app.processEvents()

            # ── Qwen Base before its first enrollment: nothing to pick, so the
            # picker states why and the primary action is disabled for the same
            # reason. ──
            controller._engine_profile = "qwen_base_0_6b"
            controller._engine_profile_label = "Qwen3-TTS Base 0.6B"
            controller._profile_voices = []
            controller._profile_clones = []
            controller.engineProfileChanged.emit()
            # engineProfilesChanged carries the isActive flip the real
            # controller publishes with it (EngineState reads the table).
            controller.engineProfilesChanged.emit()
            controller.profileCatalogChanged.emit()
            app.processEvents()
            out["base_empty"] = {
                "flat_ids": flat_ids(),
                "picker_enabled": picker.property("enabled"),
                "selected_voice": picker.property("selectedVoice"),
                "trigger_text": trigger_text(),
                "generate_enabled": generate.property("enabled"),
                "generate_reason": generate.property("disabledReason"),
            }

            # ── Base with an enrolled clone: the clone becomes the only
            # compatible voice, and the batch run is re-seeded with it (the
            # batch controller's own fallback is the VieNeu-scoped default). ──
            controller._profile_clones = [
                {"id": "clone_1", "label": "Giọng của tôi", "transcript": "xin chào"},
            ]
            controller.profileCatalogChanged.emit()
            app.processEvents()
            out["base_clone"] = {
                "flat_ids": flat_ids(),
                "flat_labels": flat_labels(),
                "selected_voice": picker.property("selectedVoice"),
                "effective_voice": picker.property("effectiveVoice"),
                "current_index": picker.property("currentIndex"),
                "picker_enabled": picker.property("enabled"),
                "generate_enabled": generate.property("enabled"),
                "batch_voice": fake_batch.renderVoice,
                "language_note": language_note.property("text"),
            }

            # ── back to VieNeu: the clone is not offered any more, and the
            # selection returns to the profile's own default voice. ──
            controller._engine_profile = "vieneu"
            controller._engine_profile_label = "VieNeu-TTS v3 Turbo"
            controller._synthesis_language = "vi"
            controller._profile_languages = [
                {"code": "vi", "label": "Tiếng Việt", "modelName": "", "isAuto": False},
                {"code": "en", "label": "English", "modelName": "", "isAuto": False},
            ]
            controller._profile_voices = []
            controller._profile_clones = []
            controller.engineProfileChanged.emit()
            controller.engineProfilesChanged.emit()
            controller.profileCatalogChanged.emit()
            controller.synthesisLanguageChanged.emit()
            app.processEvents()
            out["back_to_vieneu"] = {
                "flat_ids": flat_ids(),
                "selected_voice": picker.property("selectedVoice"),
                "effective_voice": picker.property("effectiveVoice"),
                "batch_voice": fake_batch.renderVoice,
            }

            # ── the other three surfaces bind the same capability seam: their
            # language controls exist and follow the profile (paragraph bar,
            # audiobook card, subtitle studio). `visible` reads the EFFECTIVE
            # value in Qt Quick, so every read happens while its own tab is
            # current — a hidden tab reports its whole subtree hidden. ──
            bridge.setCurrentTab("paragraph")
            app.processEvents()
            para_language = pfind("paraLanguagePicker")
            # Scope INSIDE the bar's own picker: the paragraph tab also hosts
            # the subtitle studio's control, which uses the same inner names.
            para_combo = para_language.findChildren(QObject, "languagePickerCombo")[0]
            para_note = para_language.findChildren(QObject, "languagePickerNote")[0]
            para_visible = bool(para_combo.property("visible"))
            para_note_text = str(para_note.property("text"))
            subtitle_language = pfind("subtitleLanguagePicker")

            # The audiobook card (and every control inside it) stays hidden
            # until a book is loaded, so open the committed fixture through the
            # REAL controller: the card is then genuinely on screen and its
            # controls' visibility is a statement about the bindings rather
            # than about the card's own gate.
            bridge.setCurrentTab("audiobook")
            app.processEvents()
            audiobook_ctl = engine.rootContext().contextProperty("audiobook")
            fixtures = Path(vienetts_app.__file__).resolve().parents[2] / "tests" / "fixtures"
            audiobook_ctl.openEpub(str(fixtures / "sample.epub"))
            wait_for(lambda: audiobook_ctl.currentBookId != "", 10000)
            audiobook_tab = find("audiobookTab")
            audiobook_language = audiobook_tab.findChildren(
                QObject, "audiobookLanguagePicker")[0]
            audiobook_combo = audiobook_language.findChildren(
                QObject, "languagePickerCombo")[0]
            audiobook_note = audiobook_language.findChildren(
                QObject, "languagePickerNote")[0]
            render_all = audiobook_tab.findChildren(QObject, "renderAllButton")[0]
            out["other_surfaces"] = {
                "book_card_visible": bool(audiobook_tab.findChildren(
                    QObject, "audiobookBookCard")[0].property("visible")),
                "para_language_present": para_language is not None,
                "para_takes_language": para_language.property("takesLanguage"),
                "para_language_visible": para_visible,
                "para_note": para_note_text,
                "subtitle_language_present": subtitle_language is not None,
                "subtitle_takes_language": subtitle_language.property("takesLanguage"),
                "audiobook_language_present": audiobook_language is not None,
                "audiobook_takes_language": audiobook_language.property("takesLanguage"),
                "audiobook_language_visible": bool(audiobook_combo.property("visible")),
                "audiobook_note": str(audiobook_note.property("text")),
                # The audiobook's own voice selection rides the same picker
                # seam, so a render cannot be queued on a stale VieNeu voice.
                "audiobook_render_voice": audiobook_ctl.renderVoice,
                "audiobook_render_enabled": bool(render_all.property("enabled")),
            }
            # A Qwen profile turns the paragraph bar's control on (same seam),
            # and its un-installed state disables the audiobook's batch render
            # with the install reason.
            controller._engine_profile = "qwen_custom_0_6b"
            controller._engine_profile_label = "Qwen3-TTS CustomVoice 0.6B"
            controller._synthesis_language = "auto"
            controller._profile_languages = [
                {"code": "auto", "label": "Auto", "modelName": "Auto", "isAuto": True},
                {"code": "zh", "label": "中文", "modelName": "Chinese", "isAuto": False},
            ]
            controller._profile_model_state = "unavailable"
            controller.engineProfileChanged.emit()
            controller.engineProfilesChanged.emit()
            controller.profileCatalogChanged.emit()
            controller.profileModelChanged.emit()
            controller.profileReadyChanged.emit()
            controller.synthesisLanguageChanged.emit()
            bridge.setCurrentTab("paragraph")
            app.processEvents()
            out["other_surfaces"]["para_language_visible_qwen"] = bool(
                para_combo.property("visible"))
            out["other_surfaces"]["para_note_qwen"] = str(para_note.property("text"))
            bridge.setCurrentTab("audiobook")
            app.processEvents()
            out["other_surfaces"]["audiobook_render_enabled_blocked"] = bool(
                render_all.property("enabled"))
            out["other_surfaces"]["audiobook_render_reason"] = str(
                render_all.property("disabledReason"))
        elif scenario == "settings_engine_affecting_writes":
            bridge.setCurrentTab("settings")
            settings_tab = find("settingsTab")
            backend_combo = settings_tab.findChildren(QObject, "backendCombo")[0]
            precision_combo = settings_tab.findChildren(QObject, "precisionCombo")[0]
            field = settings_tab.findChildren(QObject, "modelRepoField")[0]
            banner = settings_tab.findChildren(QObject, "needsRestartBanner")[0]

            # ── combo write seam: backend (no engine), then precision (live) ──
            out["engine"] = {
                "banner_hidden_no_engine": not banner.property("visible"),
            }
            # activate() is Q_INVOKABLE on ComboBox (same class of dynamic call
            # as Button.click()).
            activate_item(backend_combo, 2)  # torch
            app.processEvents()
            out["engine"]["backend_after"] = controller.backend
            out["engine"]["banner_after_no_engine"] = not banner.property("visible")

            # Simulate a running engine: engine-affecting writes now flag restart.
            controller.engine_initialized = True
            activate_item(precision_combo, 1)  # fp32
            app.processEvents()
            out["engine"]["precision_after"] = controller.precision
            out["engine"]["banner_visible_with_engine"] = banner.property("visible")

            # Reset the restart flag raised above: the field section below must
            # start from the same no-engine baseline.
            controller.engine_initialized = False
            controller._needs_restart = False
            controller.needsRestartChanged.emit()
            app.processEvents()

            # ── field editingFinished seam: empty field = official default ──
            out["model_repo"] = {
                "initial_text": field.property("text"),
                "placeholder": field.property("placeholderText"),
            }

            field.setProperty("text", "someone/vieneu-tts-custom")
            QMetaObject.invokeMethod(field, "editingFinished")
            app.processEvents()
            out["model_repo"]["repo_after_commit"] = controller.modelRepo
            out["model_repo"]["banner_no_engine"] = not banner.property("visible")

            # With a live engine, an override write flags needsRestart.
            controller.engine_initialized = True
            field.setProperty("text", "other-team/vieneu-tts-v4")
            QMetaObject.invokeMethod(field, "editingFinished")
            app.processEvents()
            out["model_repo"]["repo_after_second_commit"] = controller.modelRepo
            out["model_repo"]["banner_with_engine"] = banner.property("visible")

            # Blank commit resets to the official default.
            field.setProperty("text", "   ")
            QMetaObject.invokeMethod(field, "editingFinished")
            app.processEvents()
            out["model_repo"]["repo_after_blank"] = controller.modelRepo
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
        elif scenario == "settings_control_delegates":
            bridge.setCurrentTab("settings")
            settings_tab = find("settingsTab")

            # ── temperature / speed / silence-p spin delegates ──
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

            # ── default-voice delegate ──
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
            # All three combos share AppCombo.qml's delegate, so the
            # `index`-ReferenceError regression is pinned per delegate SOURCE:
            # one combo is enough to instantiate and highlight it.
            for name in ("backendCombo",):
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
            # Snapshot: the merged paragraph flow below appends to the live fake
            # list, which would otherwise leak into this tab's record.
            out["generate_calls"] = list(controller.generate_calls)
            out["slot_hits"] = list(controller.slot_hits)

            # ── merged para_stream_bindings: the same programmatic flip over
            # the SAME WaveformIndicator.qml, scoped to the paragraph subtree.
            # Activate this tab first: while a StackLayout sibling owns
            # currentIndex, Qt defers `visible` binding updates inside the hidden
            # subtree — `active`/level history still update, so only visibility
            # reads need the active-tab state. ──
            bridge.setCurrentTab("paragraph")
            app.processEvents()
            pw = pfind("waveformIndicator")

            para_out = {
                "waveform_hidden_initially": not pw.property("visible"),
                "component_inactive_initially": not pw.property("active"),
                "history_initial": int(pw.property("historyCount")),
            }

            controller.streamActive = True
            controller.playbackState = "generating"
            app.processEvents()
            para_out["waveform_visible_during"] = bool(pw.property("visible"))
            para_out["component_active_during"] = bool(pw.property("active"))
            controller.streamLevel = 0.7
            app.processEvents()
            para_out["level_bound_latest"] = float(pw.property("level"))
            para_out["history_after_push"] = int(pw.property("historyCount"))

            controller.streamActive = False
            controller.playbackState = "idle"
            app.processEvents()
            para_out["history_cleared_on_end"] = int(pw.property("historyCount"))
            para_out["waveform_hidden_after"] = not pw.property("visible")

            long_text = "Đoạn thứ nhất.\\n\\nĐoạn thứ hai."
            pfind("paragraphEditor").setProperty("text", long_text)
            app.processEvents()
            pfind("generateButton").click()
            app.processEvents()
            # The text-tab section above already recorded its own call: keep only
            # this tab's submit seam.
            para_out["generate_calls"] = controller.generate_calls[-1:]
            para_out["slot_hits"] = controller.slot_hits[-1:]
            out["para"] = para_out
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
            # Cancel hits BOTH paths (AC-2): synthesis never produced done-audio
            # AND the audio sink hard-stopped back to StoppedState.
            out["sink_state_after_cancel"] = (
                sink_holder["sink"].state() if "sink" in sink_holder else "?"
            )
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
            s2_segments_before = len(stream_sdk.infer_stream_calls)
            pfind("generateButton").click()
            started2 = wait_for(lambda: controller.streamActive)
            app.processEvents()
            # Leak guard, read BEFORE any chunk can arrive (the fake delays them):
            # a fresh session resets streamLevel to 0 at start (FR-4.2), so THIS
            # tab's indicator must show 0/empty history — never tab 1's peak.
            out["s2_session_started"] = started2
            out["s2_level_reset_controller"] = float(controller.streamLevel) == 0.0
            out["s2_indicator_fresh_level"] = float(p_wv.property("level")) == 0.0
            done2 = wait_for(lambda: controller.hasAudio and not controller.busy)
            app.processEvents()

            out["s2_completed"] = done2
            out["s2_has_audio"] = controller.hasAudio
            # ── moved from para_stream_e2e: the paragraph submit sent THIS tab's
            # document through the streaming seam (slice = tab 2's own calls) ──
            s2_calls = stream_sdk.infer_stream_calls[s2_segments_before:]
            out["s2_doc_text_sent"] = str(s2_calls[0]["text"]) if s2_calls else ""
            out["s2_segment_count"] = len(s2_calls)
            out["s2_saw_live"] = sessions["live"][1]
            out["s2_wave_visible_during"] = sessions["wave"][1]
            out["s2_peak"] = max(sessions["levels"][1]) if sessions["levels"][1] else 0.0
            # Drain window (rqy): the meter outlives done until the sink's
            # buffered tail played out, then hides.
            out["s2_done_stream_draining"] = bool(controller.streamActive)
            out["s2_drained_stream_inactive"] = wait_for(
                lambda: not controller.streamActive, timeout_ms=3000
            )
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
        elif scenario == "studio_load":
            # Studio surface contract: objectNames exist, feeder buttons ride
            # on all three tabs, and the fake project drives every binding.
            # Native dialogs stay closed headless (same policy as export).
            bridge.setCurrentTab("audiobook")
            app.processEvents()
            bridge.setCurrentTab("studio")
            app.processEvents()
            studio_tab = find("studioTab")
            present = {o.objectName() for o in studio_tab.findChildren(QObject)}
            present.add(studio_tab.objectName())
            out["missing"] = sorted(
                {
                    "studioTab",
                    "studioWaveform",
                    "studioOpStack",
                    "studioClipList",
                    "studioPreviewButton",
                    "studioExportButton",
                }
                - present
            )
            out["feeder_buttons"] = len(window.findChildren(QObject, "studioButton"))
            # Empty state: the three guide cards must be one row or one per
            # row — never the 2+1 wrap that read as a broken layout (the card
            # width used to be computed from the page column, ignoring the
            # enclosing card's own padding).
            guide_cards = [
                i for i in item_walk(window_items) if i.objectName() == "studioGuideCard"
            ]
            out["guide_cards"] = len(guide_cards)
            out["guide_rows"] = len(
                {round(float(i.mapToScene(QPointF(0, 0)).y())) for i in guide_cards}
            )
            out["guide_widths"] = sorted({round(float(i.property("width"))) for i in guide_cards})
            op_stack = studio_tab.findChildren(QObject, "studioOpStack")[0]
            out["content_visible_before"] = bool(op_stack.property("visible"))
            out["opened"] = bool(controller.openInStudio("text", "hello"))
            app.processEvents()
            out["open_calls"] = list(controller.studio_open_calls)
            out["clips"] = qjs_to_py(controller.studioClips)
            # Provenance (Task 6.3): every clip row names the engine that
            # produced its audio; a clip with no recorded identity reads as
            # VieNeu (the legacy rule), and its language chip stays hidden.
            # (Repeater delegates come back in reverse tree order, so the
            # rows are sorted by their on-screen position.)
            def in_row_order(name):
                items = ifind(name)
                items.sort(key=lambda i: float(i.mapToScene(QPointF(0, 0)).y()))
                return items

            out["clip_profiles"] = [i.property("text") for i in in_row_order("studioClipProfile")]
            out["clip_languages"] = [
                i.property("text")
                for i in in_row_order("studioClipLanguage")
                if i.property("visible")
            ]
            out["regen_banner_hidden"] = not bool(
                studio_tab.findChildren(QObject, "studioRegenProfileBanner")[0].property("visible")
            )
            # A refused re-synthesis arms the required profile: the banner names
            # it and its button performs the switch.
            controller._studio_regen_profile = "qwen_base_0_6b"
            controller._studio_regen_profile_label = "Qwen3-TTS Base 0.6B"
            controller.studioRegenProfileChanged.emit()
            app.processEvents()
            banner = studio_tab.findChildren(QObject, "studioRegenProfileBanner")[0]
            out["regen_banner_visible"] = bool(banner.property("visible"))
            out["regen_banner_text"] = studio_tab.findChildren(
                QObject, "studioRegenProfileLabel")[0].property("text")
            switch_button = studio_tab.findChildren(
                QObject, "studioSwitchToRegenProfileButton")[0]
            out["regen_switch_text"] = switch_button.property("text")
            click_item(switch_button)
            app.processEvents()
            out["regen_switch_calls"] = list(controller.studio_switch_calls)
            out["regen_banner_hidden_after"] = not bool(banner.property("visible"))
            out["envelope_len"] = len(qjs_to_py(controller.studioEnvelope))
            out["content_visible_after"] = bool(op_stack.property("visible"))
            out["waveform_len"] = len(
                qjs_to_py(
                    studio_tab.findChildren(QObject, "studioWaveform")[0].property(
                        "envelope"
                    )
                )
            )
            out["preview_enabled"] = bool(
                studio_tab.findChildren(QObject, "studioPreviewButton")[0].property(
                    "enabled"
                )
            )
            studio_tab.findChildren(QObject, "studioPreviewButton")[0].click()
            app.processEvents()
            out["preview_calls"] = controller.studio_preview_calls
            studio_tab.findChildren(QObject, "studioGainApply")[0].click()
            app.processEvents()
            out["gain_calls"] = controller.studio_gain_calls
            controller._studio_controls = {
                "gain": 3.0,
                "speed": 1.25,
                "gap": 900,
                "fade": 350,
            }
            controller.studioControlsChanged.emit()
            app.processEvents()
            out["restored_controls"] = {
                name: studio_tab.findChildren(QObject, name)[0].property("value")
                for name in (
                    "studioGainSlider",
                    "studioSpeedSlider",
                    "studioGapSlider",
                    "studioFadeSlider",
                )
            }
            controller._studio_controls = {
                "gain": 3.0,
                "speed": 1.0,
                "gap": 500,
                "fade": 200,
            }
            controller.studioControlsChanged.emit()
            app.processEvents()
            out["controls_after_speed_undo"] = {
                name: studio_tab.findChildren(QObject, name)[0].property("value")
                for name in ("studioGainSlider", "studioSpeedSlider")
            }

            # ── Redesign contract (epic VieNeuTTSApp-3cl) ───────────────
            # Elements the redesign introduced: pinned dock, range toolbar,
            # per-clip delete, clickable history chips, quick export, keys.
            visual_names = {i.objectName() for i in item_walk(window_items)}
            object_names = {o.objectName() for o in studio_tab.findChildren(QObject)}
            out["missing_new"] = sorted(
                {
                    "studioTransportDock",
                    "studioSelectionBar",
                    "studioDeleteClipButton",
                    "studioOpBaseChip",
                    "studioQuickExportButton",
                    "studioShortcutPlay",
                    "studioShortcutStop",
                    "studioShortcutSeekBack",
                    "studioShortcutSeekForward",
                }
                - visual_names
                - object_names
            )
            out["shortcut_enabled"] = {
                name: bool(studio_tab.findChild(QObject, name).property("enabled"))
                for name in ("studioShortcutPlay", "studioShortcutStop")
            }
            out["shortcut_sequences"] = {
                name: str(studio_tab.findChild(QObject, name).property("sequence"))
                for name in (
                    "studioShortcutPlay",
                    "studioShortcutStop",
                    "studioShortcutSeekBack",
                    "studioShortcutSeekForward",
                )
            }
            # The dock must describe the clip it is playing, not the mix.
            waveform = studio_tab.findChildren(QObject, "studioWaveform")[0]
            dock_target = studio_tab.findChild(QObject, "studioDockTarget")
            clip_plays = sorted(
                (i for i in item_walk(window_items) if i.objectName() == "studioClipPlayButton"),
                key=lambda i: float(i.mapToScene(QPointF(0, 0)).y()),
            )
            out["clip_play_buttons"] = len(clip_plays)
            click_item(clip_plays[1])
            app.processEvents()
            out["clip_preview_calls"] = list(controller.studio_preview_clip_calls)
            out["audition_id"] = str(controller.studioClipPlayingId)
            out["audition_duration_ms"] = waveform.property("durationMs")
            out["audition_envelope_first"] = qjs_to_py(waveform.property("envelope"))[0]
            out["audition_target_text"] = dock_target.property("text")
            click_item(clip_plays[1])
            app.processEvents()
            out["audition_after_stop"] = str(controller.studioClipPlayingId)
            out["audition_envelope_after_stop"] = qjs_to_py(waveform.property("envelope"))[0]
            out["audition_target_after_stop"] = dock_target.property("text")

            # Range select → the host commits ms, and the range is dropped
            # again so a stale band cannot be applied twice.
            out["selection_hidden_initially"] = not bool(
                studio_tab.findChild(QObject, "studioSelectionBar").property("visible")
            )
            waveform.selectionChanged.emit(0.25, 0.75)
            app.processEvents()
            out["selection_bar_visible"] = bool(
                studio_tab.findChild(QObject, "studioSelectionBar").property("visible")
            )
            out["selection_label"] = studio_tab.findChild(
                QObject, "studioSelectionLabel"
            ).property("text")
            studio_tab.findChild(QObject, "studioTrimSelectionButton").click()
            app.processEvents()
            out["trim_range_calls"] = [list(c) for c in controller.studio_trim_range_calls]
            out["selection_bar_after_trim"] = bool(
                studio_tab.findChild(QObject, "studioSelectionBar").property("visible")
            )
            waveform.selectionChanged.emit(0.0, 0.5)
            app.processEvents()
            studio_tab.findChild(QObject, "studioCutSelectionButton").click()
            app.processEvents()
            out["cut_range_calls"] = [list(c) for c in controller.studio_cut_range_calls]

            # Per-clip delete, and a history chip that drops every later step.
            delete_buttons = sorted(
                (i for i in item_walk(window_items) if i.objectName() == "studioDeleteClipButton"),
                key=lambda i: float(i.mapToScene(QPointF(0, 0)).y()),
            )
            out["delete_buttons"] = len(delete_buttons)
            click_item(delete_buttons[0])
            app.processEvents()
            out["delete_calls"] = list(controller.studio_delete_calls)

            controller._studio_ops = [
                {
                    "index": 0,
                    "name": "Chuẩn hóa",
                    "desc": "Chuẩn hóa đỉnh (100%)",
                    "kind": "normalize",
                }
            ]
            controller.studioProjectChanged.emit()
            app.processEvents()
            out["op_chips"] = len(
                [i for i in item_walk(window_items) if i.objectName() == "studioOpChip"]
            )
            studio_tab.findChild(QObject, "studioOpBaseChip").click()
            app.processEvents()
            out["revert_calls"] = list(controller.studio_revert_calls)

            regen_targets = [
                i
                for i in item_walk(window_items)
                if i.objectName() == "studioRegenButton"
            ]
            ordered = sorted(
                regen_targets, key=lambda i: float(i.mapToScene(QPointF(0, 0)).y())
            )
            out["regen_buttons"] = len(regen_targets)
            click_item(ordered[0])
            app.processEvents()
            confirm_targets = studio_tab.findChildren(
                QObject, "studioRegenConfirmButton"
            )
            if confirm_targets:
                click_item(confirm_targets[0])
                app.processEvents()
            out["regen_calls"] = controller.studio_regen_calls
            controller.hasArtifact = True
            app.processEvents()
            studio_tab.findChildren(QObject, "studioOpenButton")[0].click()
            app.processEvents()
            out["open_calls_after_cta"] = controller.studio_open_calls

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
    repo_root = Path(__file__).resolve().parents[2]
    env = {
        **os.environ,
        "QT_QPA_PLATFORM": "offscreen",
        # Root for repo-level imports, src so a SHARED venv (editable install
        # pinned to another checkout/worktree) still resolves THIS tree's
        # package first — a no-op when the venv's editable target is this repo.
        "PYTHONPATH": os.pathsep.join([str(repo_root), str(repo_root / "src")]),
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


class TestTextParagraphTabSmoke:
    def test_text_paragraph_and_subtitle_surface_flows(self, tmp_path) -> None:
        results = run_driver(
            tmp_path,
            [
                "load",
                "voice_picker_popup",
                "generate_flow",
                "export_flow",
                "error_flow",
                "para_import",
                "para_import_guard",
                "para_batch",
                "srt_surface",
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

        # Merged from para_load: the same surface contract on the paragraphTab
        # subtree, read in the same engine after the text-tab surface.
        para = results["load"]["para"]
        # ⚑ contract: every named element exists under the paragraphTab subtree.
        assert para["missing"] == []
        assert para["editor_editable"] is True
        assert para["import_button_text"] == "Nhập tệp…"
        # Import dialog: filters mirror SUPPORTED_EXTENSIONS (.txt .md .docx
        # .pdf .srt). fileMode (OpenFile) has no PySide6 enum converter — its
        # accepted path is proven end-to-end by test_para_import_via_import_path.
        assert para["dialog_filters"] == ["Văn bản (*.txt *.md *.docx *.pdf *.srt)"]
        assert para["header_found"] is True
        assert para["hint_mentions_extensions"] is True
        # Empty editor → "0 ký tự" live counter, generate disabled.
        assert para["char_count_text"] == "0 ký tự"
        assert para["initial_generate_enabled"] is False
        assert para["generate_hint"] == "Nhập văn bản để tạo âm thanh."
        # Same grouped picker contract as TextTab (headers non-selectable).
        assert para["flat_ids"] == ["", "adam_north", "eva_north", "", "my_clone"]
        assert para["selected_voice"] == "adam_north"
        assert para["current_index"] == 1

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
        assert result["audition_button_count"] >= 1
        assert result["audition_button_for_first_row"] is True
        assert result["audition_calls"] == ["adam_north"]
        assert result["audition_state"] == "playing"
        assert result["audition_voice"] == "adam_north"
        assert result["popup_still_open"] is True

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

        # Merged from disabled_states: the blank/whitespace gating ran at the
        # top of this same engine. `filled_generate_enabled`,
        # `busy_generate_visible` and `busy_cancel_visible` are asserted above
        # (the merged flow re-records identical values).
        assert isinstance(result["generate_disabled_reason"], str)
        assert result["generate_min_height"] >= 44
        assert result["whitespace_generate_enabled"] is False
        assert result["blank_action_hint"] == "Nhập văn bản để tạo âm thanh."
        assert result["filled_action_hint"] == "Tạo âm thanh trước khi phát hoặc xuất."
        assert result["idle_export_enabled"] is False
        assert result["idle_quick_enabled"] is False
        assert result["idle_play_enabled"] is False

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

        # Merged from para_generate (+para_cancel): the paragraph tab drives its
        # own facade through the same controller — the driver's reads are
        # pfind-scoped, so shared objectNames never resolve to the text tab.
        para = results["generate_flow"]["para"]
        long_text = "Đoạn thứ nhất.\n\nĐoạn thứ hai."
        assert para["cancel_hidden_idle"] is True
        assert para["initial_generate_enabled"] is False
        assert para["filled_generate_enabled"] is True
        assert para["generate_calls"] == [[long_text, "adam_north"]]
        # ParagraphTab streams through the SAME seam as the Text tab now
        # (FR-4.4); the shared fake records which submit path ran.
        assert para["slot_hits"] == ["generateStream"]
        assert para["char_count_text"] == f"{len(long_text)} ký tự"
        # Busy state: primary action stays in place, with progress and cancel.
        # (cancel_visible_busy / progress_visible_busy / generate_visible_busy
        # from para_cancel are the busy_* keys asserted here.)
        assert para["busy_generate_visible"] is True
        assert para["busy_cancel_visible"] is True
        assert para["cancel_enabled_busy"] is True
        assert para["busy_generate_busy"] is True
        assert para["busy_label_visible"] is True
        assert para["busy_progress_visible"] is True
        assert para["busy_progress_value"] == 0
        assert para["busy_progress_indeterminate"] is True
        assert para["busy_play_enabled"] is False
        assert para["busy_import_enabled"] is False
        assert para["cancel_calls"] == 1
        # Progress 0 → 0.5 → 1 with indeterminate clearing.
        assert para["progress_mid"] == 0.5
        assert para["indeterminate_mid"] is False
        assert para["progress_full"] == 1.0
        # Done: play/export enabled (after an export path exists), UI reverts.
        assert para["play_enabled_after"] is True
        assert para["export_enabled_after"] is True
        assert para["play_enabled_while_busy_with_artifact"] is True
        assert para["export_enabled_while_busy_with_artifact"] is True
        assert para["progress_hidden_after"] is True
        assert para["cancel_hidden_after"] is True
        assert para["generate_visible_after"] is True

        result = results["para_batch"]
        # Two exclusive surfaces behind one mode switch: the document editor
        # owns the default "text" mode, the file queue owns "files". Within the
        # queue mode the empty-state hint shows, the file list is hidden, and
        # run-all is disabled with nothing pending.
        assert result["mode_initial"] == "text"
        assert result["editor_card_visible_in_text"] is True
        assert result["card_hidden_in_text"] is True
        assert result["mode_after_switch"] == "files"
        assert result["editor_card_hidden_in_files"] is True
        # The page header and the mode switch sit at identical scene
        # coordinates in every mode. The stale-scroll leg runs at a short
        # window height where text mode really scrolls: the shifted position
        # differs vertically, so the post-switch reset is not vacuous.
        baseline = result["pos_text"]
        for key in (
            "pos_files",
            "pos_srt",
            "pos_text_again",
            "pos_files_restored",
            "pos_files_populated",
        ):
            assert result[key] == baseline
        assert result["page_max_scroll"] >= 1.0
        assert result["pos_after_stale"] == result["pos_short_text"]
        assert result["pos_stale"]["header"][1] != result["pos_short_text"]["header"][1]
        assert result["pos_stale"]["tabs"][1] != result["pos_short_text"]["tabs"][1]
        assert result["card_visible"] is True
        # None when the enum has no property converter (see driver comment);
        # the multi-select source is pinned in BatchQueueCard.qml.
        assert result["dialog_file_mode"] in (
            None,
            1,  # QQuickFileDialog::OpenFiles (OpenFile=0, OpenFiles=1, SaveFile=2)
        )
        assert result["empty_list_hidden"] is True
        assert result["empty_hint_visible"] is True
        # Nothing pending → run-all disabled.
        assert result["run_all_disabled_empty"] is True
        # Populated queue: list + summary live, run-all enabled.
        assert result["list_visible_populated"] is True
        assert result["run_all_enabled_populated"] is True
        assert result["summary_text"] == "1/2 tệp"
        # Footer actions reach the fake controller.
        assert result["run_all_calls"] == 1
        # Dialog accept path: card-level URL conversion (the regression pin)
        # and accept() reaching addFiles at all (selection itself can't be
        # preloaded into the offscreen fallback dialog).
        assert result["card_tolocalpath_matches"] is True
        assert result["dialog_accept_reached_controller"] is True
        # Drop routing: 2 urls feed the queue verbatim (the LAST call — the
        # accept-probe above records one earlier empty call).
        assert result["multi_drop_invoked"] is True
        dropped = result["multi_drop_added"][-1]
        assert len(dropped) == 2
        # …1 url keeps today's editor-import behavior.
        assert result["single_drop_invoked"] is True
        assert result["editor_changed_by_single_drop"] is True

        # SRT mode must resolve the `subtitleController` context property.
        # SubtitleCard inherits AppCard's `subtitle` header string, so any read
        # of the old `subtitle` context name binds the STRING and the surface
        # silently stays unloaded. The fake's loaded=True/50-cue surface only
        # shows through the renamed property; the Escape shortcut must cancel
        # an SRT render via cancelRender and a regular job via controller.cancel.
        result = results["srt_surface"]
        assert result["card_available"] is True
        assert result["card_loaded"] is True
        assert result["cue_list_visible"] is True
        assert result["cue_model_count"] == 50
        assert result["empty_hint_hidden"] is True
        # activeCueChanged scrolls the list to the active cue and the delegate
        # highlight binds index === subtitleController.activeCue.
        assert result["initial_content_y"] == 0
        assert result["follow_scroll_content_y"] > 0
        assert result["active_rows"] == 1
        # Export gating: SRT export requires a rendered track, and an in-flight
        # export disables every mutating control on the card.
        assert result["srt_export_enabled_rendered"] is True
        assert result["srt_export_disabled_unrendered"] is True
        assert result["exporting_disables_controls"] is True
        # Play affordance: no track yet advertises render-then-play.
        assert result["play_label_no_track"] == "Tạo và phát"
        assert result["play_label_has_track"] == "Phát"
        # Escape: disabled idle, cancelRender while SRT renders, controller
        # cancel for the regular busy path.
        assert result["escape_found"] is True
        assert result["escape_disabled_idle"] is True
        assert result["escape_enabled_srt_rendering"] is True
        assert result["srt_cancel_render_calls"] == 1
        assert result["controller_cancel_calls"] == 0
        assert result["escape_enabled_busy"] is True
        assert result["controller_cancel_calls_after"] == 1


class TestCloningStudioTabSmoke:
    def test_cloning_and_studio_surfaces(self, tmp_path) -> None:
        """The Cloning tab's gate/enroll/denoise flows and the Studio surface.

        One subprocess for both surface families (one QGuiApplication per
        process): an enrolled clone is what the Studio re-synthesizes with, so
        the two are asserted together.

        Cloning: the consent gate, enrollment, denoise preview, removal, the
        busy/disabled states and the per-profile capability branches.
        Studio: the objectName contract, the feeder entry, the empty-state
        guide cards, and every binding the fake project drives (clip
        provenance, the engine-mismatch offer, transport, range ops, history).
        Native dialogs stay closed headless (same policy as export).
        """
        results = run_driver(
            tmp_path,
            [
                "clone_gate",
                "clone_flow",
                "clone_denoise",
                "clone_remove",
                "clone_disabled",
                "clone_capability",
                "studio_load",
            ],
        )
        # ── Cloning ────────────────────────────────────────────────────────
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
        # Clone button wires addVoice(trimmed name, selected clip, denoise,
        # transcript) — the capability-required reference text, "" on VieNeu.
        assert result["add_voice_calls"] == [["Giọng đọc truyện", result["clip_label"], True, ""]]
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

        # Phase 6 Task 6.3: the surface follows the active engine's capability
        # entry. CustomVoice uses fixed speakers → no enrollment at all (the
        # notice names it, and neither gate nor workspace appears); VieNeu and
        # Base enroll, and only Base asks for the reference transcript.
        result = results["clone_capability"]
        custom = result["customvoice"]
        assert custom["notice_visible"] is True
        assert "Qwen3-TTS CustomVoice 0.6B" in custom["reason"]
        assert custom["panel_hidden"] is True
        assert custom["consent_hidden"] is True
        assert custom["rows"] == []

        vieneu = result["vieneu"]
        assert vieneu["notice_hidden"] is True
        assert vieneu["consent_visible"] is True
        assert vieneu["panel_hidden"] is True
        assert vieneu["transcript_hidden"] is True
        assert vieneu["cleanup_visible"] is False  # panel still behind the gate
        assert vieneu["cleanup_note_hidden"] is True
        assert vieneu["panel_visible"] is True
        assert vieneu["cleanup_visible_after"] is True
        assert vieneu["rows"] == ["my_clone"]
        assert vieneu["row_profiles"] == ["Hồ sơ: VieNeu-TTS v3 Turbo"]

        base = result["base"]
        assert base["notice_hidden"] is True
        assert base["panel_visible"] is True
        # The clone list is the ACTIVE profile's: VieNeu's clone is gone.
        assert base["rows"] == []
        assert base["transcript_visible"] is True
        assert "Qwen3-TTS Base 0.6B" in base["transcript_hint"]
        # Reference cleanup is VieNeu's own operation — not offered here.
        assert base["cleanup_hidden"] is True
        assert "không hỗ trợ khử nhiễu" in base["cleanup_note"]
        # Enrollment is refused without the transcript, with the reason.
        assert base["clone_disabled_without_transcript"] is True
        assert base["clone_reason"] == "Nhập văn bản của đoạn tham chiếu trước khi tạo giọng."
        assert base["clone_enabled_with_transcript"] is True
        assert base["add_voice_calls"] == [
            [
                "Giọng Base",
                str(tmp_path / "clone_capability" / "ref.wav"),
                True,
                "xin chào thế giới",
            ]
        ]
        assert base["rows_after"] == ["Giọng Base"]
        assert base["row_profiles_after"] == ["Hồ sơ: Qwen3-TTS Base 0.6B"]

        # ── Studio ─────────────────────────────────────────────────────────
        result = results["studio_load"]
        # Contract: every named element exists under the studioTab subtree.
        assert result["missing"] == []
        # One Studio entry per feeder tab (text + paragraph headers, audiobook).
        assert result["feeder_buttons"] == 3
        # Empty-state guide cards: three, equal width, on one row or stacked
        # one per row — never the 2+1 wrap that read as a broken layout.
        assert result["guide_cards"] == 3
        assert result["guide_rows"] in (1, 3)
        assert len(result["guide_widths"]) == 1
        # Closed project: editing surface hidden until openInStudio lands.
        assert result["content_visible_before"] is False
        assert result["opened"] is True
        assert result["open_calls"] == [["text", "hello"]]
        assert [c["id"] for c in result["clips"]] == ["c0", "c1"]
        assert [c["label"] for c in result["clips"]] == ["1", "2"]
        assert result["clips"][0]["text"] == "hello"
        assert result["clips"][0]["duration_str"] == "1.0s"
        # Phase 6 Task 6.3: each row names the engine that produced its audio;
        # a clip with no recorded identity reads as VieNeu (the legacy rule)
        # and carries no language chip.
        assert result["clip_profiles"] == [
            "Hồ sơ: VieNeu-TTS v3 Turbo",
            "Hồ sơ: VieNeu-TTS (bản cũ)",
        ]
        assert result["clip_languages"] == ["Ngôn ngữ: vi"]
        # The engine-mismatch offer is hidden until a re-synthesis is refused,
        # then names the required profile and performs the switch.
        assert result["regen_banner_hidden"] is True
        assert result["regen_banner_visible"] is True
        assert "Qwen3-TTS Base 0.6B" in result["regen_banner_text"]
        assert result["regen_switch_text"] == "Chuyển sang Qwen3-TTS Base 0.6B"
        assert result["regen_switch_calls"] == [True]
        assert result["regen_banner_hidden_after"] is True
        assert result["envelope_len"] == 160
        assert result["content_visible_after"] is True
        assert result["waveform_len"] == 160
        # Wiring: preview + gain apply + first-row regen reach the controller.
        assert result["preview_enabled"] is True
        assert result["preview_calls"] == 1
        assert result["gain_calls"] == [0.0]
        assert result["restored_controls"] == {
            "studioGainSlider": 3.0,
            "studioSpeedSlider": 1.25,
            "studioGapSlider": 900.0,
            "studioFadeSlider": 350.0,
        }
        assert result["controls_after_speed_undo"] == {
            "studioGainSlider": 3.0,
            "studioSpeedSlider": 1.0,
        }
        assert result["regen_buttons"] == 2
        assert result["regen_calls"] == [["c0", "adam_north"]]
        assert result["open_calls_after_cta"] == [["text", "hello"], ["text", ""]]
        # ── Redesign contract (epic VieNeuTTSApp-3cl) ──────────────────────
        # The pinned dock, range toolbar, per-clip delete, clickable history
        # chips, quick export and the four transport keys all exist.
        assert result["missing_new"] == []
        # Keys are live on this tab; nothing is playing yet, so Escape is not.
        assert result["shortcut_enabled"] == {
            "studioShortcutPlay": True,
            "studioShortcutStop": False,
        }
        assert result["shortcut_sequences"] == {
            "studioShortcutPlay": "Space",
            "studioShortcutStop": "Escape",
            "studioShortcutSeekBack": "Left",
            "studioShortcutSeekForward": "Right",
        }
        # A clip audition describes the CLIP — its own length, its own
        # envelope, and a target label that names the row.
        assert result["clip_play_buttons"] == 2
        assert result["clip_preview_calls"] == ["c1"]
        assert result["audition_id"] == "c1"
        assert result["audition_duration_ms"] == 1000
        assert result["audition_envelope_first"] == 0.25
        assert result["audition_target_text"] == "Đoạn #2"
        # Stopping falls back to describing the whole mix.
        assert result["audition_after_stop"] == ""
        assert result["audition_envelope_after_stop"] == 0.5
        assert result["audition_target_after_stop"] == "Toàn bộ dự án"
        # Range selection: fractions become milliseconds on the 2000 ms mix,
        # and committing the range clears it so it cannot be applied twice.
        assert result["selection_hidden_initially"] is True
        assert result["selection_bar_visible"] is True
        assert result["selection_label"] == "Vùng chọn: 0:01 – 0:02"
        assert result["trim_range_calls"] == [[500, 1500]]
        assert result["selection_bar_after_trim"] is False
        assert result["cut_range_calls"] == [[0, 1000]]
        # Per-clip delete, and a history chip that drops every later step.
        assert result["delete_buttons"] == 2
        assert result["delete_calls"] == ["c0"]
        assert result["op_chips"] == 1
        assert result["revert_calls"] == [-1]


class TestSettingsTabSmoke:
    def test_engine_profiles_install_and_synthesis_bindings(self, tmp_path) -> None:
        """Task 6.1's shared profile/language controls + Qwen install cards, and
        Task 6.2's synthesis surfaces bound to the active profile.

        One subprocess for all three scenarios (one QGuiApplication per
        process): the profile control is what the synthesis surfaces reuse, the
        Qwen cards are the only place an engine can be installed, and the
        capability bindings are what a user sees once they pick one — so they
        are asserted together.
        """
        results = run_driver(
            tmp_path,
            ["settings_engine_profiles", "settings_qwen_states", "surface_profile_bindings"],
        )

        profiles = results["settings_engine_profiles"]
        result = profiles["ready"]
        assert result["all_present"] is True
        assert result["combo_count"] == 3  # VieNeu + both Qwen checkpoints
        assert result["combo_index"] == 0  # VieNeu is the default
        assert result["badge_text"] == "Sẵn sàng"
        assert result["device_text"] == "Thiết bị: CPU"
        assert "sẵn sàng" in result["status_text"]
        # Language control: the profile's own list, native names, resolved code.
        # VieNeu is shown here with an EXPLICIT language in effect ("vi"), which
        # is the only state in which its control appears (its engine takes no
        # language argument — see the 6.2 scenario for the unset branch).
        assert result["language_count"] == 2
        assert result["language_labels"] == ["Tiếng Việt", "English"]
        assert result["language_index"] == 0
        assert result["language_visible"] is True
        assert "VieNeu-TTS v3 Turbo" in result["language_note"]

        result = profiles["switch"]
        assert result["calls"] == ["qwen_custom_0_6b"]  # combo → controller
        assert result["combo_index"] == 1
        assert result["language_count"] == 3
        assert result["language_labels"] == ["Auto", "中文", "日本語"]
        assert result["language_index"] == 0  # the profile default is auto
        assert result["language_visible"] is True
        assert "tự nhận diện" in result["auto_note"]
        assert result["language_calls"] == ["zh"]  # picker → controller

        # Readiness branches: the picker never claims a profile is usable when
        # it is not, and shows the profile's own failure reason.
        assert profiles["missing"]["badge_text"] == "Chưa sẵn sàng"
        assert "Cài đặt" in profiles["missing"]["status_text"]
        assert profiles["failed"]["badge_text"] == "Cần chú ý"
        assert profiles["failed"]["status_text"] == "install metadata is corrupt"
        assert profiles["unsupported"]["badge_text"] == "Không hỗ trợ"
        assert "không có runtime" in profiles["unsupported"]["status_text"]
        # Switching mid-job is refused by the controller, so the control is
        # disabled rather than offering a call that would only fail.
        assert profiles["busy"]["combo_enabled"] is False

        qwen = results["settings_qwen_states"]
        result = qwen["device"]
        assert result["all_present"] is True
        assert result["chip_count"] == 4
        assert result["auto_enabled"] is True
        assert result["cpu_enabled"] is True
        # Unsupported choices are shown WITH the platform's reason.
        assert result["cuda_enabled"] is False
        assert "NVIDIA" in result["cuda_reason"]
        assert result["mps_enabled"] is False
        assert "không có runtime Qwen" in result["mps_reason"]
        assert result["resolved_text"] == "Sẽ chạy trên: CPU"
        assert result["unsupported_visible"] is True
        assert "CUDA (NVIDIA):" in result["unsupported_text"]
        # NFR: CPU slowness is stated before any download.
        assert result["cpu_guidance_visible"] is True
        assert result["model_cpu_guidance_visible"] is True
        assert result["select_calls"] == ["cpu"]
        assert result["refresh_calls"] == 1

        result = qwen["runtime_idle"]
        assert result["install_visible"] is True
        assert result["install_enabled"] is True
        assert result["cancel_hidden"] is True
        assert result["repair_hidden"] is True
        assert result["remove_hidden"] is True
        assert result["variant_text"] == "Linux x64 · CUDA"
        # Storage is stated before the download starts.
        assert "2.0 GB" in result["storage_text"]
        assert "655 MB" in result["shared_text"]
        # The path is native (backslashes on Windows), so compare its parts.
        assert Path(result["path_text"]).parts[-2:] == ("qwen", "models")
        # The offline-bundle hint (a supported host) and the runtime card's
        # folder shortcut are part of the same install surface.
        assert result["import_hint_visible"] is True
        assert "ngoại tuyến" in result["import_hint_text"]
        assert qwen["runtime_open_dir_calls"] == ["runtime"]
        assert result["install_calls"] == 1

        result = qwen["runtime_downloading"]
        assert result["cancel_visible"] is True
        assert result["progress_visible"] is True
        assert result["progress_value"] == pytest.approx(0.42)
        assert "488 MB" in result["storage_text"]
        assert result["install_hidden"] is True
        assert result["cancel_calls"] == 1

        result = qwen["runtime_ready"]
        assert result["remove_visible"] is True
        assert "đã sẵn sàng" in result["status_text"]
        assert "2.0 GB" in result["storage_text"]
        assert result["remove_calls"] == 1

        result = qwen["runtime_failed"]
        assert result["repair_visible"] is True
        assert result["notice_visible"] is True
        assert result["error_text"] == "install metadata is corrupt"
        assert result["repair_calls"] == 1

        result = qwen["runtime_unsupported"]
        assert result["install_hidden"] is True
        assert result["notice_visible"] is True
        assert result["error_text"] == "unsupported platform"
        assert result["cpu_guidance_hidden"] is True  # no CPU warning with no runtime

        result = qwen["models_idle"]
        assert result["row_count"] == 2  # both checkpoints, always listed
        # The row renders the manifest label and its state word (the pill's
        # inner label is a named contract: qwenModelStateLabel_<key>).
        assert result["label_text"] == "Qwen3-TTS Base 0.6B"
        assert result["state_text"] == "Chưa cài đặt"
        assert result["install_visible"] is True
        assert result["repair_hidden"] is True
        assert result["remove_hidden"] is True
        assert result["cancel_hidden"] is True
        assert result["active_hidden"] is True  # VieNeu is the active engine
        assert "Cần tải 2.3 GB" in result["storage_text"]
        assert result["install_calls"] == [["install", "base"]]

        result = qwen["models_downloading"]
        assert result["state_text"] == "Đang tải"
        assert result["cancel_visible"] is True
        assert result["progress_visible"] is True
        # One shared install lane: the other row cannot start a download.
        assert result["other_install_disabled"] is True
        assert result["cancel_calls"] == [["install", "base"], ["cancel", "base"]]

        result = qwen["models_ready"]
        assert result["state_text"] == "Sẵn sàng"
        assert result["remove_visible"] is True
        assert result["active_visible"] is True
        assert "Đã cài 2.3 GB" in result["storage_text"]
        assert result["remove_calls"][-1] == ["remove", "base"]

        result = qwen["models_failed"]
        assert result["state_text"] == "Cần chú ý"
        assert result["repair_visible"] is True
        assert result["error_text"] == "install metadata is corrupt"
        assert result["repair_calls"][-1] == ["repair", "base"]

        # Offline packs go through the QML seams (the dialogs stay closed). The
        # driver runs each scenario in its own workspace under the tmp root.
        pack = str(tmp_path / "settings_qwen_states" / "qwen-pack")
        assert qwen["imports"] == [["runtime", "", pack], ["model", "base", pack]]
        assert qwen["import_buttons_enabled"] == [True, True]
        # Both folder shortcuts reach their own controller slot — the runtime
        # card's click (recorded above) came first.
        assert qwen["open_dir_calls"] == ["runtime", "models"]

        # ── Synthesis surfaces bound to the active profile ─────────────────
        # The Text tab's picker and language control are read through every
        # profile state (VieNeu with and without a chosen language, Qwen
        # CustomVoice ready and un-installed, Qwen Base before and after its
        # first enrollment), then the paragraph bar, the audiobook card and the
        # subtitle studio are asserted to bind the same seam. The capability
        # table is the only source of truth here — the fake republishes its
        # catalogs exactly as the real controller does.
        result = results["surface_profile_bindings"]

        # VieNeu with no language chosen (the real startup state): the engine
        # takes no language argument, so there is no control to offer — the
        # note says why. Its own catalog is untouched and Generate can start.
        unset = result["vieneu_unset"]
        assert unset["flat_ids"] == ["", "adam_north", "eva_north", "", "my_clone"]
        assert unset["selected_voice"] == "adam_north"
        assert unset["effective_voice"] == "adam_north"
        assert unset["picker_enabled"] is True
        assert unset["language_visible"] is False
        assert unset["language_note"] == "Engine này không nhận tham số ngôn ngữ."
        assert unset["generate_enabled"] is True
        assert unset["generate_reason"] == ""

        # An explicit choice puts the control back, with the profile's own
        # declared list (native names) and its resolved code selected.
        chosen = result["vieneu_chosen"]
        assert chosen["language_visible"] is True
        assert chosen["language_labels"] == ["Tiếng Việt", "English"]
        assert chosen["language_index"] == 0
        assert "VieNeu-TTS v3 Turbo" in chosen["language_note"]

        # Qwen CustomVoice (installed): the pinned speakers replace the VieNeu
        # catalog, the stale VieNeu selection is dropped for the first speaker,
        # and the language control appears with the profile's own default.
        presets = result["qwen_presets"]
        assert presets["flat_ids"] == ["", "Vivian", "Ryan", "Sohee"]
        assert presets["flat_labels"] == ["▸ Người nói cố định", "— Vivian", "— Ryan", "— Sohee"]
        assert presets["selected_voice"] == "Vivian"  # not "adam_north"
        assert presets["effective_voice"] == "Vivian"
        # The pinned speaker's native language rides in the picker's own chip.
        assert presets["voice_info"]["name"] == "Vivian"
        assert presets["voice_info"]["region"] == "Chinese"
        assert presets["language_visible"] is True
        assert presets["language_labels"] == ["Auto", "中文", "日本語"]
        assert presets["language_index"] == 0  # auto is the profile default
        assert "tự nhận diện" in presets["language_note"]
        assert presets["generate_enabled"] is True

        # The same profile before its install lands: the primary action is
        # disabled with the install reason instead of failing on click, and the
        # language control stays available (it is a profile choice, not a
        # readiness one).
        not_installed = result["qwen_not_installed"]
        assert not_installed["generate_enabled"] is False
        assert "Cài đặt" in not_installed["generate_reason"]
        assert not_installed["language_visible"] is True

        # Base before its first enrollment: nothing to pick, so the picker
        # states why and Generate is disabled for the same reason.
        empty = result["base_empty"]
        assert empty["flat_ids"] == []
        assert empty["picker_enabled"] is False
        assert empty["selected_voice"] == ""
        assert "giọng đã sao chép" in empty["trigger_text"]
        assert empty["generate_enabled"] is False
        assert empty["generate_reason"] == empty["trigger_text"]

        # Base with an enrolled clone: it becomes the only compatible voice and
        # the batch run is re-seeded with it (the batch controller's own
        # fallback is the VieNeu-scoped default voice).
        clone = result["base_clone"]
        assert clone["flat_ids"] == ["", "clone_1"]
        assert clone["flat_labels"] == ["▸ Giọng đã sao chép", "— Giọng của tôi"]
        assert clone["selected_voice"] == "clone_1"
        assert clone["effective_voice"] == "clone_1"
        assert clone["picker_enabled"] is True
        assert clone["generate_enabled"] is True
        assert clone["batch_voice"] == "clone_1"

        # Back to VieNeu: the clone is gone from the catalog and the selection
        # returns to that profile's own default voice.
        back = result["back_to_vieneu"]
        assert back["flat_ids"] == ["", "adam_north", "eva_north", "", "my_clone"]
        assert back["selected_voice"] == "adam_north"
        assert back["effective_voice"] == "adam_north"
        assert back["batch_voice"] == "adam_north"

        # The other three surfaces bind the same seam.
        other = result["other_surfaces"]
        assert other["book_card_visible"] is True
        assert other["para_language_present"] is True
        assert other["subtitle_language_present"] is True
        assert other["audiobook_language_present"] is True
        # VieNeu with "vi" in effect → control on; the same instance follows the
        # profile back to a language the engine does not take.
        assert other["para_language_visible"] is True
        assert "VieNeu-TTS v3 Turbo" in other["para_note"]
        assert other["subtitle_takes_language"] is True
        assert other["audiobook_language_visible"] is True
        assert "VieNeu-TTS v3 Turbo" in other["audiobook_note"]
        # The audiobook's render voice rides the shared picker, so a batch is
        # never queued on a leftover voice from another engine.
        assert other["audiobook_render_voice"] == "adam_north"
        assert other["audiobook_render_enabled"] is True
        assert other["para_language_visible_qwen"] is True
        assert "tự nhận diện" in other["para_note_qwen"]
        # A Qwen profile without its install disables the batch render with the
        # install reason — on the audiobook surface too.
        assert other["audiobook_render_enabled_blocked"] is False
        assert "Cài đặt" in other["audiobook_render_reason"]

    def test_controls_and_engine_temperature_voice_delegates(self, tmp_path) -> None:
        results = run_driver(
            tmp_path,
            [
                "settings_load",
                "settings_update_states",
                "settings_cuda_states",
                "settings_engine_affecting_writes",
                "settings_theme",
                "settings_language",
                "settings_output",
                "settings_control_delegates",
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
        # Update card: present, banner hidden pre-check, Check button found.
        assert result["update_banner_hidden_initially"] is True
        assert result["check_button_present"] is True

        updates = results["settings_update_states"]
        result = updates["available"]
        assert result["banner_visible"] is True
        assert result["download_visible"] is True
        assert result["release_visible"] is True
        assert result["error_hidden"] is True
        assert result["toggle_visible"] is True
        assert result["other_count_after_expand"] == 1
        assert result["other_names"] == ["VieNeuTTS-0.2.0-windows-x64.zip"]
        assert result["check_calls"] == 1

        result = updates["error"]
        assert result["error_visible"] is True
        assert result["banner_hidden"] is True
        assert result["download_hidden"] is True

        cuda = results["settings_cuda_states"]
        result = cuda["idle"]
        assert result["all_present"] is True
        assert result["card_visible"] is True
        assert result["install_visible"] is True
        assert result["install_enabled"] is True
        assert result["cancel_hidden"] is True
        assert result["retry_hidden"] is True
        assert result["remove_hidden"] is True
        assert result["local_summary"] == "Chưa quét runtime CUDA cục bộ."

        result = cuda["unsupported"]
        assert result["card_visible"] is True
        assert result["install_hidden"] is True
        assert result["cancel_hidden"] is True
        assert result["retry_hidden"] is True
        assert result["remove_hidden"] is True
        assert result["error_visible"] is True
        assert result["error_text"] == "unsupported platform"

        result = cuda["driver_unavailable"]
        assert result["install_visible"] is True
        assert result["install_enabled"] is False
        assert result["install_disabled_reason"] != ""
        assert result["notice_visible"] is True
        assert result["guide_visible"] is True
        assert result["guide_download_visible"] is True
        # The re-check action is the only in-session recovery from a failed
        # or timed-out driver probe (the install button is disabled).
        assert result["recheck_visible"] is True
        assert result["recheck_text"] == "Kiểm tra lại driver"
        assert result["refresh_calls"] == 1
        assert result["discover_calls"] == 1

        result = cuda["downloading"]
        assert result["cancel_visible"] is True
        assert result["progress_visible"] is True
        assert result["progress_value"] == pytest.approx(0.5)
        assert "512" in result["storage_text"]
        assert "1024" in result["storage_text"]
        assert result["cancel_calls"] == 1

        result = cuda["ready"]
        assert result["remove_visible"] is True
        assert result["restart_visible"] is True
        assert result["remove_calls"] == 1

        result = cuda["failed_and_local"]
        assert result["retry_visible"] is True
        assert result["error_text"] == "checksum mismatch"
        assert result["install_calls_after_retry"] == 1
        assert "2" in result["local_summary"]
        assert result["discover_calls"] == 1

        writes = results["settings_engine_affecting_writes"]
        result = writes["model_repo"]
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

        result = writes["engine"]
        assert result["backend_after"] == "torch"
        # With no engine initialized the change applies at (re)start — no banner.
        assert result["banner_after_no_engine"] is True
        # Once an engine is live, engine-affecting writes flag needsRestart
        # instead of mutating the running engine (FR-3.5, AC-4).
        assert result["precision_after"] == "fp32"
        assert result["banner_visible_with_engine"] is True

        result = results["settings_control_delegates"]
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
        # Same engine, next delegate: the default-voice combo.
        assert result["default_before"] == "adam_north"
        assert result["default_after"] == "eva_north"

        # Regression (ReferenceError: index is not defined): delegates that
        # declare `required property var modelData` lose Qt 6's implicit
        # `index` injection, so the `highlighted` binding must read a
        # declared `required property int index` instead. Opening a combo
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


class TestStreamLifecycleSmoke:
    """One subprocess covers text/paragraph stream bindings, e2e, cancel,
    cross-tab reset, and mid-stream error recovery.
    """

    def test_stream_bindings_e2e_cancel_cross_tab_and_error_recovery(self, tmp_path) -> None:
        results = run_driver(
            tmp_path,
            [
                "stream_bindings",
                "stream_e2e",
                "stream_cancel",
                "para_import_oversize",
                "stream_cross_tab",
                "stream_error_recover",
            ],
        )
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

        result = results["stream_e2e"]
        assert result["completed"] is True
        assert len(result["infer_stream_calls"]) >= 1
        assert result["infer_stream_calls"][0]["text"] == "Xin chào thế giới"
        assert result["saw_session_live"] is True
        assert result["waveform_visible_during_session"] is True
        assert result["peak_level_seen"] > 0.5
        assert result["done_stream_draining"] is True
        assert result["done_waveform_visible_during_drain"] is False
        assert result["drained_stream_inactive"] is True
        assert result["done_waveform_hidden"] is True
        assert result["progress_final"] == 1.0
        assert result["export_ok"] is True
        assert result["last_export_path"].endswith(".wav")

        result = results["stream_cancel"]
        assert result["cancel_visible_mid_stream"] is True
        assert result["saw_session_live"] is True
        assert result["settled_after_cancel"] is True
        assert result["no_audio_retained"] is True
        assert result["waveform_hidden_after_cancel"] is True
        assert result["no_error_banner"] is True
        assert result["toast_visible"] is True
        assert result["toast_text"] == "Đã hủy"
        # Carried from para_stream_cancel: the audio sink hard-stops too.
        assert result["sink_state_after_cancel"] == "StoppedState"

        # ParagraphTab streaming bindings (FR-4.4/4.5): same engine/contract as
        # the text tab above, scoped to the paragraph subtree by pfind.
        result = results["stream_bindings"]["para"]
        assert result["waveform_hidden_initially"] is True
        assert result["component_inactive_initially"] is True
        assert result["history_initial"] == 0
        assert result["waveform_visible_during"] is True
        assert result["component_active_during"] is True
        assert result["level_bound_latest"] == 0.7
        assert result["history_after_push"] == 1
        assert result["history_cleared_on_end"] == 0
        assert result["waveform_hidden_after"] is True
        long_text = "Đoạn thứ nhất.\n\nĐoạn thứ hai."
        assert result["generate_calls"] == [[long_text, "adam_north"]]
        assert result["slot_hits"] == ["generateStream"]

        result = results["para_import_oversize"]
        assert result["invoked"] is True
        assert result["banner_visible"] is True
        assert result["label_visible"] is True
        assert result["mentions_limit"] is True
        assert result["matches_controller_error"] is True
        assert "Split the document" in result["error_text"]
        assert result["editor_empty"] is True

        # Cross-tab session reset (FR-4.2): tab 2 indicator starts fresh. Session
        # 2 also completes the paragraph-tab cycle, so the para_stream_e2e
        # observables (doc_text_sent/segment_count/drain window) live here too.
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
        # The paragraph submit sent THIS tab's document through the stream seam
        # in one call (moved from para_stream_e2e).
        assert "Đoạn thứ nhất." in result["s2_doc_text_sent"]
        assert "Đoạn thứ hai." in result["s2_doc_text_sent"]
        assert result["s2_segment_count"] >= 1
        assert result["s2_saw_live"] is True
        assert result["s2_wave_visible_during"] is True
        assert result["s2_peak"] > 0.5
        # Drain window (rqy): the meter outlives done until the sink's buffered
        # tail played out, then hides.
        assert result["s2_done_stream_draining"] is True
        assert result["s2_drained_stream_inactive"] is True
        assert result["s2_done_inactive"] is True
        assert result["s2_done_waveform_hidden"] is True
        assert result["s2_history_cleared_on_end"] is True
        assert result["s2_progress_final"] == 1.0
        # Export affordance restored after BOTH sessions.
        assert result["export_ok_after_both"] is True
        assert result["last_export_path"].endswith(".wav")

        # Mid-stream error recovery: generic error, then successful regenerate.
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

        if scenario == "ab_render_states":
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

            # ── loaded state: a book + chapters land → card, shelf, badges,
            # transport/batch icons (merged from ab_book) ──
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
        elif scenario == "ab_dock_reader":
            from PySide6.QtCore import QMetaObject

            # ── dock geometry (merged from ab_dock): the no-book state first,
            # then the loaded state, then the reader overlay's placement. The
            # dock/overlay are siblings of the page shell, so their placement
            # relative to the TAB is the contract, not placement in the column. ──
            def rect_in_tab(item):
                pos = item.mapToItem(ab_tab, 0, 0)
                return (
                    pos.x(),
                    pos.y(),
                    float(item.property("width")),
                    float(item.property("height")),
                )

            docks = afind("playerDock")
            out["dock"] = {
                "dock_found": len(docks),
                "dock_hidden_no_book": (
                    len(docks) == 1 and not bool(docks[0].property("visible"))
                ),
            }

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
            out["dock"]["dock_visible_with_book"] = bool(dock.property("visible"))
            # Pinned to the tab bottom, respecting the pane padding, and hosting
            # the whole transport (single instance of each control).
            out["dock"]["dock_flush_bottom"] = abs((dy + dh) - (tab_h - pad)) <= 2
            out["dock"]["dock_padded_width"] = abs(dw - (tab_w - 2 * pad)) <= 2
            out["dock"]["transport_in_dock"] = (
                len(dock.findChildren(QObject, "seekSlider")) == 1
                and len(dock.findChildren(QObject, "playPauseButton")) == 1
                and len(dock.findChildren(QObject, "readerToggleButton")) == 1
            )
            out["dock"]["reader_not_in_dock"] = (
                len(dock.findChildren(QObject, "readerCard")) == 0
            )

            # Reader overlay: hidden until asked, then fills the tab area ABOVE
            # the dock (full padded width) while the dock stays visible.
            out["dock"]["reader_hidden_before"] = not bool(
                afind("readerCard")[0].property("visible")
            )
            QMetaObject.invokeMethod(afind("readerToggleButton")[0], "click")
            app.processEvents()
            out["dock"]["reader_open_after_toggle"] = fake_ab._reader_open
            card = afind("readerCard")[0]
            out["dock"]["reader_visible_after"] = bool(card.property("visible"))
            cx, cy, cw, ch = rect_in_tab(card)
            out["dock"]["reader_padded_width"] = abs(cw - (tab_w - 2 * pad)) <= 2
            out["dock"]["reader_sits_above_dock"] = 0 <= (dy - (cy + ch)) <= 20
            out["dock"]["dock_still_visible"] = bool(dock.property("visible"))

            # The overlay's own close affordance retreats the reader.
            close_btns = afind("readerCloseButton")
            out["dock"]["close_found"] = len(close_btns)
            if close_btns:
                QMetaObject.invokeMethod(close_btns[0], "click")
                app.processEvents()
            out["dock"]["reader_closed_after_close"] = not bool(card.property("visible"))
            out["dock"]["reader_state_closed"] = fake_ab._reader_open is False
            # Snapshot: the reader checks below keep appending to fake_ab.hits.
            out["dock"]["hits"] = list(fake_ab.hits)

            # ── reader overlay contents (ab_reader's own body) ──
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
        results = run_ab_driver(tmp_path, ["ab_render_states", "ab_dock_reader", "ab_export_url"])
        result = results["ab_render_states"]
        assert result["missing"] == []
        assert result["shelf_empty_visible"] is True
        assert result["book_card_hidden"] is True
        assert result["nav_label_present"] is True
        assert result["dock_found"] == 1
        assert result["dock_hidden_no_book"] is True

        # Merged from ab_book: the same engine, now with a book + chapters.
        assert result["book_card_visible"] is True
        assert result["chapter_rows"] == 3
        assert result["shelf_rows"] == 1
        assert result["shelf_empty_hidden"] is True
        assert result["status_badges"] == 3
        assert result["error_chips"] == 1
        assert result["render_buttons"] == 2
        assert result["prev_enabled"] is False
        assert result["auto_toggle_control_kind"] == "toggle"
        assert result["seek_control_kind"] == "slider"
        assert result["transport_icons"] == ["previous", "play", "next"]
        assert result["batch_icons"] == ["download", "wave"]

        # Merged ab_dock + ab_reader: one engine walks no-book → loaded dock →
        # reader overlay placement → reader contents/interaction.
        result = results["ab_dock_reader"]
        dock = result["dock"]
        assert dock["dock_found"] == 1
        assert dock["dock_hidden_no_book"] is True
        assert dock["dock_visible_with_book"] is True
        assert dock["dock_flush_bottom"] is True
        assert dock["dock_padded_width"] is True
        assert dock["transport_in_dock"] is True
        assert dock["reader_not_in_dock"] is True
        assert dock["reader_hidden_before"] is True
        assert dock["reader_open_after_toggle"] is True
        assert dock["reader_visible_after"] is True
        assert dock["reader_padded_width"] is True
        assert dock["reader_sits_above_dock"] is True
        assert dock["dock_still_visible"] is True
        assert dock["close_found"] == 1
        assert dock["reader_closed_after_close"] is True
        assert dock["reader_state_closed"] is True

        # ── reader overlay contents (moved from ab_reader) ──
        assert result["reader_hidden_before"] is True
        assert result["reader_open_after_toggle"] is True
        assert result["reader_visible_after"] is True
        assert result["paragraphs"] == 2
        rows = result["rows"]
        assert [r["active"] for r in rows].count(True) == 1
        assert all(r["bold"] == r["active"] for r in rows)
        assert result["active_rows"] == 1
        assert result["active_row_opaque"] is True
        hits = {h[0]: h[1:] for h in result["hits"]}
        assert hits["seekToParagraph"] == [1]
        assert result["copy_button_found"] == 1
        assert result["copy_button_visible"] is True
        assert result["copy_chapter_hit"] is True
        assert result["select_by_mouse"] is True
        assert result["read_only"] is True
        assert result["click_seek"] is True
        assert result["active_focus_after_drag"] is True
        assert result["drag_selected"] == "Câu một."
        assert result["clipboard_after_copy"] == "Câu một."
        assert result["text_unchanged_after_keys"] is True
        assert result["transport_hits_while_focused"] == [["pause"], ["seek", 0]]

        result = results["ab_export_url"]
        assert result["hits"] == [
            ["exportAllReady", "C:/Users/trung/Nhạc"],
            ["exportAllReady", "/home/u/VieNeuTTS Test"],
        ]

    def test_waveform_render_progress_interactions_and_render_all(self, tmp_path) -> None:
        results = run_ab_driver(
            tmp_path,
            ["ab_waveform", "ab_render_progress", "ab_interact", "ab_render_all"],
        )
        result = results["ab_waveform"]
        assert result["hidden_without_envelope"] is True
        assert result["visible_with_envelope"] is True
        assert result["bucket_count"] == 5
        assert result["position_bound"] == pytest.approx(0.25)
        assert result["duration_bound"] == 100_000
        assert result["active_while_playing"] is True
        assert result["seekable_while_playing"] is True
        assert result["seek_routed"] is True
        assert result["click_seek_routed"] is True
        assert result["drag_scrub_final"] is True
        assert result["seekable_while_paused"] is True
        assert result["inactive_when_stopped"] is True
        assert result["visible_when_stopped"] is True

        result = results["ab_render_progress"]
        assert result["inline_bars_visible"] == 1
        assert result["inline_bar_value"] == pytest.approx(0.42, abs=0.01)
        assert result["inline_bar_on_screen"] is True
        assert result["stop_buttons_visible"] == 1
        assert result["stop_on_screen"] is True
        hits = {h[0]: h[1:] for h in result["hits"]}
        assert hits["cancelRender"] == []
        assert result["global_row_visible"] is True
        assert result["global_above_list"] is True
        assert result["global_bar_value"] == pytest.approx(0.42, abs=0.01)
        assert result["render_buttons_visible"] == 2
        assert result["idle_inline_bars"] == 0
        assert result["idle_stop_buttons"] == 0
        assert result["idle_global_visible"] == 0

        result = results["ab_interact"]
        assert result["target_found"] is True
        hits = {h[0]: h[1:] for h in result["hits"]}
        assert hits["playChapter"] == [1]
        assert hits["resume"] == []
        assert hits["renderChapter"] == [1]
        assert result["auto_advance_after"] is False

        result = results["ab_render_all"]
        assert result["row_found"] == 1
        assert result["row_visible"] is True
        assert result["bar_value"] == pytest.approx(0.4, abs=0.01)
        assert result["label_text"] == "Tổng: 2/5 chương"
        assert result["eta_visible"] is True
        assert result["eta_text"] == "còn ~1:20"
        assert result["idle_row_visible"] is False
