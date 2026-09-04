"""AppController: QML-facing application state (FR-3.1, FR-3.4, FR-3.5, FR-4.2).

Registered by app.py as the QML context property ``controller``. Owns the
voice catalog (built model-free from the SDK asset JSON), synthesis jobs,
voice-management jobs, streaming playback (a lazily built StreamPlayback-
Controller fed from ``worker.chunk_ready``), and the Settings seam. EVERY
dependency is injectable (data_dir, engine factory, worker factory, catalog
function, stream-playback factory, audio probe) — and construction must never
initialize the engine or start the worker (NFR-3.1: no model load at startup;
the worker is lazily created on first submission) nor touch the audio stack
(NFR-2.1: the audio probe runs on first READ, never in ``__init__``).

Cancellation UX: the worker reports a user cancel as ``error("Cancelled by
user")``. The controller treats that message specially: busy is reset,
playback is stopped immediately (FR-4.2: cancel halts synthesis AND audio),
and ``errorText`` stays empty with a transient ``cancelled()`` signal QML can
toast. Documented choice: silent reset + notification, no scary error banner.
Because the cancel path bypasses ``_set_error``, it never (re)classifies
``modelsMissing`` either.

Edge-case surfaces (FR-4.6a/c):

    modelsMissing  bool, NOTIFY modelsMissingChanged — True ONLY while the
                   LAST error routed through ``_set_error`` matches
                   ``is_models_missing()`` from core.engine (the marker-based
                   string seam, because worker errors travel as plain text).
                   Lifecycle: any successful op start calls ``_set_error("")``
                   which re-evaluates to False (generating again clears it);
                   a fresh models-missing error sets it again. CANCELLED_
                   MESSAGE never sets or clears it (it skips ``_set_error``).
                   QML shows the models-missing overlay while True; "Retry"
                   dismisses locally and the next submit re-evaluates.
    audioAvailable bool, NOTIFY audioAvailableChanged — lazily probed device
                   availability via the injectable ``audio_probe`` (default:
                   ``playback.audio_output_available``, itself QtMultimedia-
                   lazy). Evaluated on FIRST PROPERTY READ, NOT in
                   ``__init__``: constructing real QtMultimedia objects at
                   startup would violate NFR-2.1. In practice QML evaluates
                   its bindings as soon as Main.qml loads (after the
                   controller is constructed and the app object exists — a
                   pure device enumeration, no player/output construction);
                   that once-per-startup read is deliberate and documented.
                   The value is cached afterwards; hot-plug recovery goes
                   through the explicit ``refreshAudioAvailability()`` slot,
                   which re-probes and emits NOTIFY unconditionally (a rare
                   user/system-driven action beats change-only emissions).
    refreshAudioAvailability() @Slot() — re-run the probe, emit NOTIFY.

QML surface (context property ``controller``):
    voices            QVariantList, NOTIFY voicesChanged — grouped catalog
    busy              bool, NOTIFY busyChanged
    progress          float 0..1, NOTIFY progressChanged
    errorText         str, NOTIFY errorTextChanged
    hasAudio          bool, NOTIFY hasAudioChanged
    lastExportPath    str, NOTIFY lastExportPathChanged
    previewPath       str, NOTIFY previewPathChanged
    needsRestart      bool, NOTIFY needsRestartChanged
    consentGiven      bool, NOTIFY consentGivenChanged
    modelsMissing     bool, NOTIFY modelsMissingChanged — see above
    audioAvailable    bool, NOTIFY audioAvailableChanged — see above
    streamActive      bool, NOTIFY streamActiveChanged — streaming session live
                      (generateStream until done/error/cancel)
    streamLevel       float 0..1, NOTIFY streamLevelChanged — rolling peak
                      envelope of the latest streamed ~120 ms window (FR-4.5)
    replayActive      bool, NOTIFY replayActiveChanged — Phát/Dừng toggle state
    waveformEnvelope  QVariantList[float 0..1], NOTIFY waveformEnvelopeChanged —
                      peak-normalized overview buckets of the held audio for
                      PlaybackWaveform (empty until the first synthesis done)
    replayPosition    float 0..1, NOTIFY replayPositionChanged — live playhead
                      of the current replay (RAM path: audio-paced QTimer;
                      temp-file path: mirrored player position); 0 when idle
    replayDurationMs  int, NOTIFY replayDurationMsChanged — length of the
                      audio being (or last) replayed, for the time labels
    backend / precision / defaultVoice / outputDir / temperature / theme —
                      NOTIFY-backed settings mirrors; invalid writes are
                      ignored with errorText feedback (never a crash)
    generate(text, voice) @Slot(str, str)
    generateStream(text, voice) @Slot(str, str) — streaming playback as the
                      chunks arrive; full audio still retained on done
    cancel() @Slot()  stops worker queue AND any live stream playback
    refreshAudioAvailability() @Slot() — re-probe audio devices (hot-plug)
    exportWav(path) @Slot(str) -> bool
    addVoice(name, clip_path, denoise) @Slot(str, str, bool)
    removeVoice(name) @Slot(str)
    denoisePreview(clip_path) @Slot(str)
    refreshVoices() @Slot()
    shutdown() @Slot()
    acknowledgeConsent() @Slot()

Synthesis-listener seam (audiobook track FR-A8): a SECOND controller (the
AudiobookController) reuses this one's worker/engine pair instead of paying
for a second model instance. Contract (Phase 2 Task 3: job-ID ownership, no
global attachment):

    submit_stream_for_listener(text, voice, listener, *, kind="requested_chapter") -> str | None
                                        register ``listener`` for one stream-mode
                                        job and admit it; returns the job ID, or
                                        None when validation/admission fails
                                        (nothing registered then). Listener jobs
                                        never touch app-tab audio/progress/error
                                        state, nor the text action's ``busy``.
    cancel_job(job_id)                  forward targeted cancellation for a
                                        listener-owned render.

The listener is duck-typed with on_synthesis_progress(event)/
on_synthesis_chunk(event)/on_synthesis_terminal(event) (FR-A9 timeline
capture reads JobProgress/JobChunk fields). The mapping pops BEFORE the
terminal is delivered, so a reentrant submit from inside the handler cannot
receive the finished job's late events. Foreground (text/paragraph/cloning)
events route by _foreground_job_id instead; any event owned by neither is
stale and dropped.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import (
    Property,
    QElapsedTimer,
    QLocale,
    QObject,
    QStandardPaths,
    QTimer,
    Signal,
    Slot,
)

from vienetts_app.core.artifacts import InteractiveArtifactStore, SynthesisArtifact
from vienetts_app.core.audio import compute_waveform_envelope_from_wav, read_wav, write_wav_file
from vienetts_app.core.audiobook import CHAPTER_CHAR_LIMIT
from vienetts_app.core.engine import (
    TTSEngine,
    is_models_missing,
    preset_voices,
    resolve_model_source,
    saved_voice_names,
)
from vienetts_app.core.importers import DocumentImportError, import_document
from vienetts_app.core.jobs import (
    JobChunk,
    JobKind,
    JobOwner,
    JobProgress,
    JobTerminal,
    new_synthesis_job,
)
from vienetts_app.core.model_manager import ModelManager, ModelStatus
from vienetts_app.core.models import TTSRequest, VoiceOp, WarmupOp
from vienetts_app.core.pcm_transport import BoundedPcmTransport
from vienetts_app.core.performance import PerformanceRecorder
from vienetts_app.core.settings import load_settings, save_settings
from vienetts_app.core.updates import (
    UpdateInfo,
    check_for_updates,
    current_platform_key,
    platform_display_name,
)
from vienetts_app.ui import playback as _playback
from vienetts_app.ui.bg_ops import drain_thread_pool, run_on_thread_pool
from vienetts_app.ui.i18n import SUPPORTED_LANGUAGES, resolve_language
from vienetts_app.ui.stream_playback import StreamPlaybackController
from vienetts_app.workers.inference_worker import InferenceWorker


def _default_model_manager(data_dir: Path) -> ModelManager:
    return ModelManager(Path(data_dir) / "models")


logger = logging.getLogger(__name__)

CONSENT_FILENAME = "cloning_consent.json"
PREVIEW_FILENAME = "preview.wav"
EXPORT_PATTERN = "vienetts_%Y%m%d_%H%M%S.wav"
SAMPLE_RATE = 48_000  # synthesis audio (infer/infer_stream); denoise is 44.1 kHz
# Voice-preset audition sample (VoicePicker pre-listen): fixed Vietnamese
# sentence, short enough to synthesize in ~seconds on CPU. A fixed text
# keeps back-to-back voice compares fair and the disk cache key small; the
# current editor text is deliberately NOT used (it would couple the picker
# popup to tab state and make cache keys unbounded).
AUDITION_SAMPLE_TEXT = "Xin chào, đây là giọng đọc mẫu của VieNeu TTS."
AUDITION_CACHE_DIRNAME = "auditions"
# Interactive synthesis cap: the worker retains a finished job's full audio in
# RAM (chunk list + concatenate + held result), so a document-scale paste can
# OOM an 8 GB machine (200k chars ≈ 2.4+ GB of float32). Mirrors the
# audiobook chapter limit, which exists for the same worker handoff.
GENERATE_CHAR_LIMIT = CHAPTER_CHAR_LIMIT

# PlaybackWaveform overview + playhead (see waveformEnvelope/replayPosition):
WAVEFORM_ENVELOPE_BUCKETS = 160  # fixed count → shape stable across widths
REPLAY_POSITION_TICK_MS = 80  # memory-replay playhead advance cadence
# Done-path drain allowance on top of the buffer's real-time duration —
# mirrors stream_playback.REPLAY_DRAIN_MARGIN_MS (same class of estimate).


# Catalog groups, fixed order (FR-3.1: North/Central/South + fallback +
# cloned). Display labels are Vietnamese per the UI language.
_REGION_GROUPS: tuple[tuple[str, str], ...] = (
    ("Bắc", "Bắc"),
    ("Trung", "Trung"),
    ("Nam", "Nam"),
)
FALLBACK_GROUP = "Khác"
CLONED_GROUP = "Đã sao chép"


def _default_engine_factory(**kwargs: Any) -> TTSEngine:
    return TTSEngine(**kwargs)


def _default_stream_playback_factory() -> StreamPlaybackController:
    """Production seam: real StreamPlaybackController (lazy QtMultimedia)."""
    return StreamPlaybackController()


def _default_audio_probe() -> bool:
    """Production seam: real audio-device probe (FR-4.6a).

    ``playback.audio_output_available`` imports QtMultimedia lazily INSIDE the
    call, so merely importing this module and constructing AppController stays
    audio-stack-free (NFR-2.1) — the probe itself runs on first read of
    ``audioAvailable``.
    """
    return _playback.audio_output_available()


class AppController(QObject):
    """Application state exposed to QML; every dependency is injectable."""

    voicesChanged = Signal()
    busyChanged = Signal()
    progressChanged = Signal()
    errorTextChanged = Signal()
    hasAudioChanged = Signal()
    hasArtifactChanged = Signal()
    artifactPathChanged = Signal()
    playbackStateChanged = Signal()
    lastExportPathChanged = Signal()
    previewPathChanged = Signal()
    needsRestartChanged = Signal()
    consentGivenChanged = Signal()
    backendChanged = Signal()
    precisionChanged = Signal()
    modelRepoChanged = Signal()
    defaultVoiceChanged = Signal()
    outputDirChanged = Signal()
    temperatureChanged = Signal()
    speedChanged = Signal()
    silencePChanged = Signal()
    themeChanged = Signal()
    languageChanged = Signal()
    livePreviewChanged = Signal()
    # Streaming playback (FR-4.2, FR-4.5 groundwork).
    streamActiveChanged = Signal()
    streamLevelChanged = Signal()
    replayActiveChanged = Signal()
    # PlaybackWaveform overview + playhead (replay visualization).
    waveformEnvelopeChanged = Signal()
    replayPositionChanged = Signal()
    replayDurationMsChanged = Signal()
    # Edge-case surfaces (FR-4.6a/c).
    modelsMissingChanged = Signal()
    audioAvailableChanged = Signal()
    # App updates (GitHub Releases check): non-blocking, silent on failure.
    updateAvailableChanged = Signal()
    updateCheckingChanged = Signal()
    updateInfoChanged = Signal()
    # Managed model setup (Phase 1 Task 4): truthful readiness, not optimistic.
    modelStateChanged = Signal()
    modelProgressChanged = Signal()
    modelErrorChanged = Signal()
    modelStorageChanged = Signal()
    modelDirChanged = Signal()
    _model_status_signal = Signal(object)
    # Foreground synthesis job (Phase 2 Task 3): QML binds action state here,
    # never to the worker's global queue.
    foregroundJobIdChanged = Signal()
    foregroundJobStateChanged = Signal()
    # Voice-preset audition (VoicePicker pre-listen): a non-busy sample lane
    # that streams the fixed AUDITION_SAMPLE_TEXT in a hovered/selected
    # voice. QML binds the per-row play/stop icon + spinner here, never to
    # busy (an audition must not dim the generate/export surface).
    auditionVoiceIdChanged = Signal()
    auditionStateChanged = Signal()
    # Transient notifications (no property payload — QML toasts on fire).
    cancelled = Signal()
    # Off-thread import/export (bead 12k): path + extracted text ("" = error,
    # see errorText) / path + success. importing/exporting expose busy state.
    documentImported = Signal(str, str)
    exportFinished = Signal(str, bool)
    importingChanged = Signal()
    exportingChanged = Signal()
    srtKeepTimestampsChanged = Signal()

    def __init__(
        self,
        data_dir: Path | None = None,
        engine_factory: Callable[..., TTSEngine | Any] | None = None,
        worker_factory: Callable[[Any], InferenceWorker | Any] | None = None,
        catalog: Callable[[], list[dict[str, str]]] | None = None,
        saved_names: Callable[[Any], list[str]] | None = None,
        consent_path: Path | None = None,
        stream_playback_factory: Callable[[], StreamPlaybackController | Any] | None = None,
        audio_probe: Callable[[], bool] | None = None,
        performance_recorder: PerformanceRecorder | None = None,
        bg_runner: Callable[[Callable[[], Any], Callable[[Any], None], Any], None] | None = None,
        model_manager_factory: Callable[[Path], Any] | None = None,
        update_checker: Callable[..., UpdateInfo] | None = None,
        app_version: str | None = None,
        update_platform_key: str | None = None,
    ) -> None:
        super().__init__()
        from vienetts_app.core.settings import default_data_dir

        self._data_dir = default_data_dir() if data_dir is None else Path(data_dir)
        self._engine_factory = engine_factory or _default_engine_factory
        self._worker_factory = worker_factory
        self._catalog_fn = catalog or preset_voices
        self._saved_names_fn = saved_names or saved_voice_names
        self._consent_path = (
            self._data_dir / CONSENT_FILENAME if consent_path is None else Path(consent_path)
        )
        self._voices_dir = self._data_dir / "voices"
        self._stream_playback_factory = (
            _default_stream_playback_factory
            if stream_playback_factory is None
            else stream_playback_factory
        )
        # FR-4.6a: probe is injectable; evaluation is LAZY (first property
        # read) — None marks "not probed yet" so __init__ stays off the audio
        # stack (NFR-2.1), exactly like the engine/worker lazy posture.
        self._audio_probe = _default_audio_probe if audio_probe is None else audio_probe
        self._audio_available: bool | None = None
        self._performance = performance_recorder or PerformanceRecorder()
        # Import/export run off the GUI thread (pool in production, inline in
        # tests) — a multi-second PDF parse or a large WAV write must never
        # freeze the shell (bead 12k).
        self._run_bg = bg_runner if bg_runner is not None else run_on_thread_pool
        self._importing = False
        self._exporting = False
        self._srt_keep_timestamps = False

        self._settings = load_settings(self._data_dir)
        # UI language is resolved ONCE here (restart-to-apply): the bootstrap
        # installs the translator from `appliedLanguage`, and the captured
        # system locale keeps later needs-restart comparisons consistent with
        # what startup resolved — including on hosts whose locale changes.
        self._system_locale = QLocale.system().name()
        self._applied_language = resolve_language(self._settings.language, self._system_locale)
        self._worker: InferenceWorker | Any | None = None
        self._engine: TTSEngine | Any | None = None
        self._stream_playback: StreamPlaybackController | Any | None = None

        self._busy = False
        self._progress = 0.0
        self._error_text = ""
        self._artifact_store = InteractiveArtifactStore(self._data_dir)
        self._current_artifact: SynthesisArtifact | None = None
        self._retired_artifacts: set[SynthesisArtifact] = set()
        self._active_live_transport: BoundedPcmTransport | None = None
        self._live_playback_job_id: str | None = None
        self._playback_state = "idle"
        self._last_export_path = ""
        self._preview_path = ""
        self._needs_restart = False
        self._consent = self._load_consent()
        self._voices = self._build_voices()
        self._stream_active = False
        self._stream_level = 0.0
        self._replay_active = False
        self._replay_artifact: SynthesisArtifact | None = None
        # PlaybackWaveform state: overview of the committed artifact + live playhead.
        self._waveform_envelope: list[float] = []
        self._replay_position = 0.0
        self._replay_duration_ms = 0
        # Memory-replay playhead: QElapsedTimer feeds an 80 ms QTimer so the
        # QML playhead glides at audio pace (the sink offers no position API).
        # The temp-file path instead mirrors the player's positionChanged.
        self._replay_clock = QElapsedTimer()
        self._replay_pos_timer = QTimer(self)
        self._replay_pos_timer.setInterval(REPLAY_POSITION_TICK_MS)
        self._replay_pos_timer.timeout.connect(self._on_replay_position_tick)
        # Done-path drain window: keeps streamActive (the live meter) on
        # until the sink's buffered tail actually played (bead rqy).
        self._stream_drain_timer = QTimer(self)
        self._stream_drain_timer.setSingleShot(True)
        self._stream_drain_timer.timeout.connect(self._on_stream_drain_finished)
        # Shared PlaybackController, wired post-construction by create_app
        # (temp-file replay path only; None keeps startup player-free).
        self._file_playback: Any | None = None
        # FR-4.6c: True only while the LAST error is a models-missing error;
        # recomputed inside _set_error on every transition (see docstring).
        self._models_missing = False
        # Managed model setup (Phase 1 Task 4): constructed here, never scanned
        # here — refreshModelState() owns the first filesystem inspect, off the
        # GUI thread, after first paint (run_gui). No Hub import on this path.
        factory = model_manager_factory or _default_model_manager
        self._model_manager = factory(self._data_dir)
        self._model_status: ModelStatus = ModelStatus(
            state="checking", installed_bytes=0, required_bytes=0, progress=0.0, error=""
        )
        self._model_generation = 0
        self._model_cancel = threading.Event()
        self._model_downloading = False
        self._model_status_signal.connect(self._on_model_status_signal)
        # Foreground job ownership (Phase 2 Task 3, FR-A8): the interactive
        # text/paragraph/cloning job owned by this controller, plus one
        # listener entry per audiobook render job. Tagged worker events route
        # by ID; anything unowned is stale and dropped.
        self._foreground_job_id: str | None = None
        self._foreground_job_state = "idle"
        self._foreground_is_voice_op = False
        self._foreground_live = False
        self._listener_by_job_id: dict[str, Any] = {}
        self._chunk_seen_by_job_id: set[str] = set()
        # Voice-preset audition lane: voice id being auditioned ("" = none)
        # and "idle" | "loading" | "playing". Lives beside — never inside —
        # the foreground job id: auditions stream through the same worker but
        # must not flip busy, consume progress, or commit an artifact.
        self._audition_job_id: str | None = None
        self._audition_voice_id = ""
        self._audition_state = "idle"
        self._audition_playing_path: Path | None = None
        # Worker/engine pairs that outlived a shutdown() wait (a plain infer
        # call cannot be interrupted mid-way). Kept referenced so neither a
        # running QThread nor its engine is freed under the thread's feet;
        # a later shutdown() (or process exit) finishes the teardown.
        self._retired_workers: list[tuple[Any, Any]] = []
        # App updates (GitHub Releases): version/platform pinned at
        # construction (build-stamped or package __version__); the check
        # itself runs off the GUI thread via _run_bg, results land on
        # _update_info (change-only NOTIFY) with _update_available sticky.
        from vienetts_app import __version__ as _pkg_version
        from vienetts_app._version import get_version

        self._app_version = app_version or get_version(_pkg_version)
        self._update_platform_key = update_platform_key or current_platform_key()
        self._update_checker = update_checker or check_for_updates
        self._update_info: UpdateInfo | None = None
        self._update_available = False
        self._update_checking = False
        self._update_generation = 0

    # ── voice catalog (FR-3.1, model-free) ──────────────────────────────────

    def _build_voices(self) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, str]]] = {region: [] for region, _ in _REGION_GROUPS}
        grouped[FALLBACK_GROUP] = []
        for entry in self._catalog_fn():
            region = _parse_region(entry.get("description", ""))
            group = region if region in grouped else FALLBACK_GROUP
            grouped[group].append({"id": entry["name"], "label": _display_label(entry, region)})
        # Each group carries a stable "id" (independent of the translated
        # label) — QML must never identity-match display strings.
        result = [
            {"id": region, "label": label, "voices": grouped[region]}
            for region, label in _REGION_GROUPS
        ]
        if grouped[FALLBACK_GROUP]:
            result.append(
                {"id": "fallback", "label": FALLBACK_GROUP, "voices": grouped[FALLBACK_GROUP]}
            )
        cloned_names = self._saved_names_fn(self._voices_dir)
        if cloned_names:
            result.append(
                {
                    "id": "cloned",
                    "label": CLONED_GROUP,
                    "voices": [{"id": n, "label": n} for n in cloned_names],
                }
            )
        # Empty groups are dropped: a QML picker must not offer empty headers.
        return [g for g in result if g["voices"]]

    @Property("QVariantList", notify=voicesChanged)
    def voices(self) -> list[dict[str, Any]]:
        return self._voices

    @Slot()
    def refreshVoices(self) -> None:
        self._voices = self._build_voices()
        self.voicesChanged.emit()

    # ── busy / progress / error / audio state ───────────────────────────────

    @Property(str, notify=foregroundJobIdChanged)
    def foregroundJobId(self) -> str:
        """ID of the owned interactive job ("" when none)."""
        return self._foreground_job_id or ""

    @Property(str, notify=foregroundJobStateChanged)
    def foregroundJobState(self) -> str:
        """idle | queued | generating | cancel_requested | completed | cancelled | failed."""
        return self._foreground_job_state

    @Property(bool, notify=busyChanged)
    def busy(self) -> bool:
        return self._busy

    @Property(float, notify=progressChanged)
    def progress(self) -> float:
        return self._progress

    @Property(str, notify=errorTextChanged)
    def errorText(self) -> str:
        return self._error_text

    @Property(bool, notify=hasAudioChanged)
    def hasAudio(self) -> bool:
        """Compatibility alias; callers should bind ``hasArtifact``."""
        return self.hasArtifact

    @Property(bool, notify=hasArtifactChanged)
    def hasArtifact(self) -> bool:
        return self._current_artifact is not None and self._current_artifact.path.is_file()

    @Property(str, notify=artifactPathChanged)
    def artifactPath(self) -> str:
        artifact = self._current_artifact
        return str(artifact.path) if artifact is not None else ""

    @Property(str, notify=playbackStateChanged)
    def playbackState(self) -> str:
        return self._playback_state

    @Property(str, notify=lastExportPathChanged)
    def lastExportPath(self) -> str:
        return self._last_export_path

    @Property(str, notify=previewPathChanged)
    def previewPath(self) -> str:
        return self._preview_path

    @Property(str, notify=auditionVoiceIdChanged)
    def auditionVoiceId(self) -> str:
        """Voice id of the live audition ("" when no audition is active)."""
        return self._audition_voice_id

    @Property(str, notify=auditionStateChanged)
    def auditionState(self) -> str:
        """idle | loading | playing — QML binds the row play/stop/spinner here."""
        return self._audition_state

    @Property(bool, notify=needsRestartChanged)
    def needsRestart(self) -> bool:
        return self._needs_restart

    @Property(bool, notify=consentGivenChanged)
    def consentGiven(self) -> bool:
        return self._consent

    @Property(bool, notify=streamActiveChanged)
    def streamActive(self) -> bool:
        return self._stream_active

    @Property(float, notify=streamLevelChanged)
    def streamLevel(self) -> float:
        return self._stream_level

    @Property(bool, notify=modelsMissingChanged)
    def modelsMissing(self) -> bool:
        """True while the LAST ``_set_error`` message is a models-missing error.

        Semantics (FR-4.6c): the flag mirrors ``is_models_missing`` on the
        most recent error text — never a sticky "seen once" latch. See the
        module docstring for the full lifecycle.
        """
        return self._models_missing

    @Property(bool, notify=audioAvailableChanged)
    def audioAvailable(self) -> bool:
        """Lazily probed device availability (FR-4.6a); cached after first read."""
        if self._audio_available is None:
            try:
                value = bool(self._audio_probe())
            except Exception:  # noqa: BLE001 - a broken probe means "unavailable"
                logger.exception("audio availability probe failed")
                value = False
            self._audio_available = value
        return self._audio_available

    @Slot()
    def refreshAudioAvailability(self) -> None:
        """Re-run the audio-device probe and notify QML unconditionally.

        Hot-plug seam (FR-4.6a): devices attached after startup don't raise a
        Python signal, so QML/settings can call this to re-check. Emits
        NOTIFY even when the result is unchanged — an explicit refresh is a
        rare, user/system-driven action, and the guarantee ("bindings re-read
        a FRESH probe") beats change-only emission bookkeeping here.
        """
        self._audio_available = None  # drop the cache; force a fresh probe
        self.audioAvailable  # noqa: B018 - intentional read-through-property
        self.audioAvailableChanged.emit()

    # ── app updates (GitHub Releases) ────────────────────────────────────────

    @Property(str, constant=True)
    def appVersion(self) -> str:
        """Build-stamped version shown in Settings (package __version__ fallback)."""
        return self._app_version

    @Property(str, constant=True)
    def updatePlatformKey(self) -> str:
        """This host's asset key, e.g. ``windows-x64`` (drives the suggested file)."""
        return self._update_platform_key

    @Property(str, constant=True)
    def updatePlatformLabel(self) -> str:
        """Short display name for the host platform (``Windows``/``Linux``/``macOS``)."""
        return platform_display_name(self._update_platform_key)

    @Property(bool, notify=updateAvailableChanged)
    def updateAvailable(self) -> bool:
        """Sticky: True once ANY check found a newer release (never auto-clears)."""
        return self._update_available

    @Property(bool, notify=updateCheckingChanged)
    def updateChecking(self) -> bool:
        return self._update_checking

    @Property(str, notify=updateInfoChanged)
    def updateLatestVersion(self) -> str:
        return self._update_info.latest_version if self._update_info else ""

    @Property(str, notify=updateInfoChanged)
    def updateReleaseUrl(self) -> str:
        return self._update_info.release_url if self._update_info else ""

    @Property(str, notify=updateInfoChanged)
    def updateAssetName(self) -> str:
        info = self._update_info
        if info is not None and info.platform_asset is not None:
            return info.platform_asset.name
        return ""

    @Property(str, notify=updateInfoChanged)
    def updateAssetUrl(self) -> str:
        info = self._update_info
        if info is not None and info.platform_asset is not None:
            return info.platform_asset.url
        return ""

    @Property("QVariantList", notify=updateInfoChanged)
    def updateOtherAssets(self) -> list[dict[str, object]]:
        if self._update_info is None:
            return []
        return self._update_info.other_assets_dicts()

    @Property(str, notify=updateInfoChanged)
    def updateError(self) -> str:
        """Last check failure ("" = none); manual Check surfaces it, auto-check stays silent."""
        return self._update_info.error if self._update_info else ""

    @Slot()
    def checkForUpdates(self) -> None:
        """Manual refresh (Settings button); failures surface via updateError."""
        self._start_update_check(announce_errors=True)

    def checkForUpdatesStartup(self) -> None:
        """Silent auto-check after first paint; failures stay invisible."""
        self._start_update_check(announce_errors=False)

    def _set_update_checking(self, value: bool) -> None:
        if value != self._update_checking:
            self._update_checking = value
            self.updateCheckingChanged.emit()

    def _start_update_check(self, *, announce_errors: bool) -> None:
        if self._update_checking:
            return
        self._update_generation += 1
        generation = self._update_generation
        self._set_update_checking(True)
        checker = self._update_checker
        version = self._app_version
        platform_key = self._update_platform_key

        def work() -> tuple[int, UpdateInfo]:
            return (generation, checker(version, platform_key=platform_key))

        def on_done(result: tuple[int, UpdateInfo]) -> None:
            gen, info = result
            if gen != self._update_generation:
                return
            self._set_update_checking(False)
            self._publish_update_info(info, announce_errors=announce_errors)

        self._run_bg(work, on_done, self)

    def _publish_update_info(self, info: UpdateInfo, *, announce_errors: bool) -> None:
        self._update_info = info
        self.updateInfoChanged.emit()
        if info.available and not self._update_available:
            self._update_available = True
            self.updateAvailableChanged.emit()
        if announce_errors and not info.available and info.error:
            self._set_error(self.tr("Không kiểm tra được bản cập nhật: {}").format(info.error))

    # ── managed model setup (Phase 1 Task 4) ────────────────────────────────

    @Property(str, notify=modelStateChanged)
    def modelState(self) -> str:
        return str(self._model_status.state)

    @Property(float, notify=modelProgressChanged)
    def modelProgress(self) -> float:
        return float(self._model_status.progress)

    @Property(str, notify=modelErrorChanged)
    def modelError(self) -> str:
        return str(self._model_status.error)

    @Property(int, notify=modelStorageChanged)
    def modelInstalledBytes(self) -> int:
        return int(self._model_status.installed_bytes)

    @Property(int, notify=modelStorageChanged)
    def modelRequiredBytes(self) -> int:
        return int(self._model_status.required_bytes)

    @Property(bool, notify=modelStateChanged)
    def modelReady(self) -> bool:
        return self._model_status.state == "ready"

    @Property(str, notify=modelDirChanged)
    def modelDir(self) -> str:
        """Active install dir the app scans (offline-pack destination)."""
        manager = self._model_manager
        for attr in ("model_dir", "_active_dir"):
            candidate = getattr(manager, attr, None)
            if callable(candidate):
                try:
                    return str(Path(candidate()).resolve())
                except Exception:  # noqa: BLE001
                    continue
            elif candidate is not None:
                try:
                    return str(Path(candidate).resolve())
                except Exception:  # noqa: BLE001
                    continue
        root = getattr(manager, "root", None)
        if root is not None:
            try:
                from vienetts_app.core.official_model_manifest import OFFICIAL_MODEL_FORMAT

                return str(Path(root, OFFICIAL_MODEL_FORMAT).resolve())
            except Exception:  # noqa: BLE001
                return str(Path(root))
        return str(Path(self._data_dir, "models", "official-v1").resolve())

    @Slot(result=str)
    def copyModelDir(self) -> str:
        """Copy the model dir path to the clipboard; always returns the path."""
        path = self.modelDir
        try:
            from PySide6.QtGui import QGuiApplication

            inst = QGuiApplication.instance()
            if isinstance(inst, QGuiApplication):
                clipboard = inst.clipboard()
                if clipboard is not None:
                    clipboard.setText(path)
        except Exception:  # noqa: BLE001 — headless/test harness has no clipboard
            pass
        return path

    @Slot(result=bool)
    def openModelDir(self) -> bool:
        """Create (if needed) and reveal the model dir in the file manager."""
        path = Path(self.modelDir)
        try:
            path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._set_error(self.tr("Không mở được thư mục mô hình: {}").format(exc))
            return False
        try:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(path))))
        except Exception as exc:  # noqa: BLE001
            self._set_error(self.tr("Không mở được thư mục mô hình: {}").format(exc))
            return False

    @Slot(str)
    def importOfflinePack(self, source: str) -> None:
        """Validate + promote a manual offline pack (backbone/ + codec/)."""
        raw = (source or "").strip()
        if raw.startswith("file://"):
            try:
                from PySide6.QtCore import QUrl

                local = QUrl(raw).toLocalFile()
                if local:
                    raw = local
            except Exception:  # noqa: BLE001
                pass
        if not raw:
            self._publish_model_status(
                ModelStatus(
                    state=self._model_status.state,
                    installed_bytes=self._model_status.installed_bytes,
                    required_bytes=self._model_status.required_bytes,
                    progress=self._model_status.progress,
                    error="Chọn thư mục chứa backbone/ và codec/ của gói ngoại tuyến.",
                    location=self._model_status.location,
                )
            )
            return
        install_offline = getattr(self._model_manager, "install_offline_pack", None)
        if not callable(install_offline):
            self._publish_model_status(
                ModelStatus(
                    state=self._model_status.state,
                    installed_bytes=self._model_status.installed_bytes,
                    required_bytes=self._model_status.required_bytes,
                    progress=self._model_status.progress,
                    error="Phiên bản này chưa hỗ trợ nhập gói ngoại tuyến.",
                    location=self._model_status.location,
                )
            )
            return
        if self._model_downloading:
            return
        self.shutdown()
        self._model_downloading = True
        self._model_cancel.clear()
        self._model_generation += 1
        generation = self._model_generation
        src = Path(raw)
        self._publish_model_status(
            ModelStatus(
                state="validating",
                installed_bytes=self._model_status.installed_bytes,
                required_bytes=self._model_status.required_bytes,
                progress=0.0,
                error="",
                location=None,
            )
        )

        def work() -> ModelStatus:
            return install_offline(src)

        def on_done(status: ModelStatus) -> None:
            if generation != self._model_generation:
                return
            self._publish_model_status(status)
            self._model_downloading = False

        self._run_bg(work, on_done, self)

    def _publish_model_status(self, status: ModelStatus) -> None:
        previous = self._model_status
        self._model_status = status
        if status.state != previous.state:
            self.modelStateChanged.emit()
        if status.progress != previous.progress:
            self.modelProgressChanged.emit()
        if status.error != previous.error:
            self.modelErrorChanged.emit()
        if (
            status.installed_bytes != previous.installed_bytes
            or status.required_bytes != previous.required_bytes
        ):
            self.modelStorageChanged.emit()

    def _on_model_status_signal(self, payload: object) -> None:
        try:
            generation, status = payload  # type: ignore[misc]
        except (TypeError, ValueError):
            return
        if generation != self._model_generation:
            return
        if not isinstance(status, ModelStatus):
            return
        self._publish_model_status(status)
        if status.state in ("ready", "failed", "unavailable"):
            self._model_downloading = False

    @Slot()
    def refreshModelState(self) -> None:
        """Re-inspect the managed install off the GUI thread (retry seam)."""
        self._model_generation += 1
        generation = self._model_generation
        manager = self._model_manager

        def work() -> tuple[int, ModelStatus]:
            return (generation, manager.inspect())

        def on_done(result: tuple[int, ModelStatus]) -> None:
            gen, status = result
            if gen != self._model_generation:
                return
            self._publish_model_status(status)

        self._run_bg(work, on_done, self)

    @Slot()
    def downloadOfficialModel(self) -> None:
        """Download + validate the official baseline without blocking the UI."""
        if self._settings.model_repo != "":
            self._publish_model_status(
                ModelStatus(
                    state=self._model_status.state,
                    installed_bytes=self._model_status.installed_bytes,
                    required_bytes=self._model_status.required_bytes,
                    progress=self._model_status.progress,
                    error=(
                        "Custom model source selected: clear the advanced model "
                        "repository to use the official baseline download."
                    ),
                    location=self._model_status.location,
                )
            )
            return
        if self._model_downloading:
            return
        self.shutdown()
        self._model_downloading = True
        self._model_cancel.clear()
        self._model_generation += 1
        generation = self._model_generation
        manager = self._model_manager
        cancelled = self._model_cancel
        self._publish_model_status(
            ModelStatus(
                state="downloading",
                installed_bytes=self._model_status.installed_bytes,
                required_bytes=self._model_status.required_bytes,
                progress=0.0,
                error="",
                location=None,
            )
        )

        def work() -> ModelStatus:
            return manager.install(
                cancelled=cancelled.is_set,
                on_progress=lambda s: self._model_status_signal.emit((generation, s)),
            )

        def on_done(status: ModelStatus) -> None:
            if generation != self._model_generation:
                return
            self._publish_model_status(status)
            self._model_downloading = False

        self._run_bg(work, on_done, self)

    @Slot()
    def cancelModelDownload(self) -> None:
        """Request cooperative cancellation of an in-flight official download."""
        self._model_cancel.set()

    def _set_busy(self, value: bool) -> None:
        if value != self._busy:
            self._busy = value
            self.busyChanged.emit()

    def _set_error(self, message: str) -> None:
        if message != self._error_text:
            self._error_text = message
            self.errorTextChanged.emit()
        # FR-4.6c: reclassify on EVERY transition through the error seam —
        # including the successful-op-start clear ("" → False) and any fresh
        # error (marker prefix → True). Cancel bypasses this method, so a
        # user cancel never changes the flag (see _on_error).
        missing = is_models_missing(message)
        if missing != self._models_missing:
            self._models_missing = missing
            self.modelsMissingChanged.emit()

    # ── synthesis-listener seam (FR-A8, audiobook track) ────────────────────

    def submit_stream_for_listener(
        self,
        text: str,
        voice: str | None,
        listener: Any,
        *,
        kind: JobKind = "requested_chapter",
    ) -> str | None:
        """Submit a listener-owned stream-mode synthesis job.

        Registers ``listener`` for exactly one job and returns its job ID;
        returns ``None`` without registering anything when validation or
        worker admission fails. Listener jobs never touch the foreground
        action state (``busy`` stays false) — the worker serializes them
        behind/in front of interactive jobs and tagged events route each
        delivery to its owner.
        """
        if listener is None:
            return None
        if not text or not text.strip():
            return None
        try:
            request = TTSRequest(
                text=text,
                voice=voice or None,
                mode="stream",
                temperature=self._settings.temperature,
                speed=self._settings.speed,
                silence_p=self._settings.silence_p,
            )
        except ValueError as exc:
            self._set_error(self.tr("Yêu cầu không hợp lệ: {}").format(exc))
            return None
        job = new_synthesis_job("audiobook", kind, request)  # type: ignore[arg-type]
        job = replace(job, artifact_path=self._artifact_store.allocate(job.id))
        worker = self._ensure_worker()
        self._listener_by_job_id[job.id] = listener
        if not worker.submit(job):
            self._listener_by_job_id.pop(job.id, None)
            return None
        self._performance.begin(
            job.id,
            {"char_count": len(text), "mode": "stream", "streaming": True},
        )
        self._performance.mark(job.id, "submitted")
        return job.id

    def cancel_job(self, job_id: str | None) -> bool:
        """Forward targeted cancellation for a listener-owned job."""
        if not job_id or self._worker is None:
            return False
        return bool(self._worker.cancel_job(job_id))

    # ── synthesis ────────────────────────────────────────────────────────────

    def _begin_synthesis(self) -> Any:
        """Shared pre-submit sequence for generate/generateStream.

        Keeps the last committed artifact, drops any live streaming session
        (FR-4.2: a new request must not inherit old sink audio), and flips
        busy. Returns the worker ready to receive the submission.
        """
        worker = self._ensure_worker()
        self._stop_replay()
        self._stop_audition_session()
        self._reset_audition_tracking()
        self._stop_stream_playback_now()
        self._set_error("")
        self._set_busy(True)
        return worker

    def _reject_oversize(self, text: str) -> bool:
        """Interactive length cap (OOM guard, mirrors the audiobook chapter
        limit — the worker handoff retains the finished audio in RAM)."""
        if len(text) <= GENERATE_CHAR_LIMIT:
            return False
        self._set_error(
            self.tr(
                "Bản văn quá dài ({chars:,} ký tự, giới hạn {limit:,}). "
                "Hãy dùng tab Sách nói (EPUB) để tạo văn bản dài theo từng chương."
            ).format(chars=len(text), limit=GENERATE_CHAR_LIMIT)
        )
        return True

    def _submit_text_job(
        self, text: str, voice: str, *, mode: str, live: bool = False, owner: JobOwner = "text"
    ) -> None:
        """Validate, own, and admit one interactive synthesis job."""
        if not text or not text.strip():
            return
        if self._reject_oversize(text):
            return
        try:
            request = TTSRequest(
                text=text,
                voice=voice or None,
                mode=mode,  # type: ignore[arg-type]
                temperature=self._settings.temperature,
                speed=self._settings.speed,
                silence_p=self._settings.silence_p,
            )
        except ValueError as exc:
            self._set_error(self.tr("Yêu cầu không hợp lệ: {}").format(exc))
            return
        job = new_synthesis_job(owner, "interactive", request)  # type: ignore[arg-type]
        job = replace(job, artifact_path=self._artifact_store.allocate(job.id))
        self._begin_foreground_trace(job_id=job.id, text=text, mode=mode)
        worker = self._begin_synthesis()
        self._foreground_live = False
        if live:
            transport = self._start_stream_session(job.id)
            if transport is not None:
                job = replace(job, live_transport=transport)
                self._foreground_live = True
        if not worker.submit(job):
            self._foreground_job_id = None
            self._foreground_is_voice_op = False
            self._foreground_live = False
            self._set_foreground_job_state("idle")
            self.foregroundJobIdChanged.emit()
            self._set_busy(False)
            self._set_error(self.tr("Không thể thêm tác vụ vì ứng dụng đang đóng."))

    @Slot(str, str)
    def generate(self, text: str, voice: str) -> None:
        """Submit a batch-synthesis job; blank text is a no-op (FR-3.x)."""
        self._submit_text_job(text, voice, mode="stream")

    @Slot(str, str)
    def generateStream(self, text: str, voice: str) -> None:
        """Submit a STREAMING job; live audio follows the livePreview setting.

        Same validation/no-op rules as ``generate`` (mode="stream" request,
        temperature from settings). Full audio is still retained on terminal,
        so export/replay keep working; ``streamActive`` stays True until the
        job's terminal event or cancel. With livePreview OFF the job runs
        silent and auto-replays the finished artifact from the start.
        """
        self._submit_text_job(text, voice, mode="stream", live=self._settings.live_preview)

    @Slot(str)
    def auditionVoice(self, voice: str) -> None:
        """Pre-listen one preset voice (VoicePicker per-row play button).

        Toggle semantics: calling with the currently auditioning voice stops
        it. A different voice preempts the running audition. A disk cache hit
        (``auditions/<voice>_<speed>.wav``) plays instantly; otherwise the
        fixed AUDITION_SAMPLE_TEXT is synthesized SILENTLY through the shared
        worker as an ``audition=True`` job (no live transport — chunks never
        reach the speaker) and played once from the finished file. The lane
        never flips busy, never touches progress, and never commits an
        artifact. No-op when a foreground synthesis owns the worker (busy)
        or the voice is blank.
        """
        voice = (voice or "").strip()
        if not voice:
            return
        if voice == self._audition_voice_id and self._audition_state != "idle":
            self.stopAudition()
            return
        if self._busy:
            return
        self._stop_audition_session()
        self._set_error("")
        cached = self._audition_cache_path(voice)
        if cached.is_file():
            self._set_audition_state(voice, "playing")
            self._play_audition_file(voice, cached)
            return
        try:
            request = TTSRequest(
                text=AUDITION_SAMPLE_TEXT,
                voice=voice,
                mode="stream",  # type: ignore[arg-type]
                temperature=self._settings.temperature,
                speed=self._settings.speed,
                silence_p=self._settings.silence_p,
            )
        except ValueError as exc:
            self._set_error(self.tr("Yêu cầu không hợp lệ: {}").format(exc))
            return
        job = new_synthesis_job("text", "interactive", request, audition=True)  # type: ignore[arg-type]
        job = replace(job, artifact_path=self._artifact_store.allocate(job.id))
        worker = self._ensure_worker()
        self._set_audition_state(voice, "loading")
        self._audition_job_id = job.id
        self._performance.begin(
            job.id,
            {"char_count": len(AUDITION_SAMPLE_TEXT), "mode": "stream", "streaming": True},
        )
        self._performance.mark(job.id, "submitted")
        if not worker.submit(job):
            self._reset_audition_tracking()
            self._set_error(self.tr("Không thể thêm tác vụ vì ứng dụng đang đóng."))

    @Slot()
    def stopAudition(self) -> None:
        """Stop any live audition (row stop button / popup close / preempt)."""
        if self._audition_job_id is None and self._audition_state == "idle":
            return
        self._stop_audition_session()
        self._reset_audition_tracking()

    def _audition_cache_path(self, voice: str) -> Path:
        """Cache file for a voice at the current speed (temperature excluded).

        Speed changes the PCM (worker-side time stretch), so it keys the
        file; temperature only varies sampling noise, so auditions stay
        comparable and cache-stable across temperature tweaks.
        """
        safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in voice.strip())
        return (
            self._data_dir
            / AUDITION_CACHE_DIRNAME
            / f"{safe or 'voice'}_{self._settings.speed}.wav"
        )

    def _set_audition_state(self, voice_id: str, state: str) -> None:
        if state not in {"idle", "loading", "playing"}:
            raise ValueError(f"invalid audition state: {state}")
        if voice_id != self._audition_voice_id:
            self._audition_voice_id = voice_id
            self.auditionVoiceIdChanged.emit()
        if state != self._audition_state:
            self._audition_state = state
            self.auditionStateChanged.emit()

    def _reset_audition_tracking(self) -> None:
        self._audition_job_id = None
        self._audition_playing_path = None
        self._set_audition_state("", "idle")

    def _stop_audition_session(self) -> None:
        """Cancel the in-flight audition job and stop its file playback."""
        self._cancel_audition_job()
        self._audition_job_id = None
        playback = self._file_playback
        playing = self._audition_playing_path
        self._audition_playing_path = None
        if (
            playback is not None
            and hasattr(playback, "stop")
            and playing is not None
            and getattr(playback, "sourcePath", "") == str(playing)
        ):
            with contextlib.suppress(Exception):
                playback.stop()

    def _cancel_audition_job(self) -> None:
        job_id, self._audition_job_id = self._audition_job_id, None
        if job_id is None:
            return
        if self._worker is not None:
            with contextlib.suppress(Exception):
                self._worker.cancel_job(job_id)
        self._performance.finish(job_id, "cancelled")

    def _on_audition_released(self) -> None:
        if self._audition_state == "playing":
            self._reset_audition_tracking()

    def _play_audition_file(self, voice: str, path: Path) -> None:
        """Play a finished/cached audition file; failure resets lane state."""
        playback = self._file_playback
        if playback is None or not hasattr(playback, "play"):
            self._reset_audition_tracking()
            return
        self._audition_job_id = None
        self._audition_playing_path = Path(path)
        try:
            with contextlib.suppress(TypeError):
                playback.play(str(path), on_released=self._on_audition_released)
                return
            playback.play(str(path))
        except Exception:  # noqa: BLE001 - file playback must never crash the UI
            logger.exception("audition playback failed")
            self._reset_audition_tracking()

    def _complete_audition(self, job_id: str, value: Any) -> None:
        """Audition synthesis done: cache + auto-play, never commit artifact."""
        self._performance.mark(job_id, "controller_done")
        self._performance.finish(job_id, "completed")
        if not isinstance(value, SynthesisArtifact) or value.job_id != job_id:
            self._fail_audition(job_id, self.tr("Tệp âm thanh không hợp lệ."))
        voice = self._audition_voice_id
        target = self._audition_cache_path(voice)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            data, _rate = read_wav(value.path)
            write_wav_file(np.asarray(data), target, sample_rate=SAMPLE_RATE)
        except Exception as exc:  # noqa: BLE001
            logger.warning("could not cache audition for %s (%s)", voice, exc)
            target = Path(value.path)
        self._audition_job_id = None
        self._set_audition_state(voice, "playing")
        self._play_audition_file(voice, target)
        with contextlib.suppress(Exception):
            self._artifact_store.remove_if_unprotected(value)

    def _cancel_audition(self, job_id: str) -> None:
        self._performance.finish(job_id, "cancelled")
        self._stop_audition_session()
        self._reset_audition_tracking()

    def _fail_audition(self, job_id: str, message: str) -> None:
        self._performance.mark(job_id, "controller_error")
        self._performance.finish(job_id, "failed")
        self._stop_audition_session()
        self._reset_audition_tracking()
        self._set_error(message)

    @Slot()
    def cancel(self) -> None:
        """Cancel the foreground job AND stop stream playback now (FR-4.2)."""
        self._stop_audition_session()
        self._reset_audition_tracking()
        job_id = self._foreground_job_id
        if job_id is None:
            self._stop_stream_playback_now()
            return
        self._set_foreground_job_state("cancel_requested")
        self._performance.mark(job_id, "cancel_requested")
        if self._worker is not None:
            self._worker.cancel_job(job_id)
        self._stop_stream_playback_now()

    def prewarm_engine(self) -> None:
        """Load the model in the background so the first click is warm.

        Startup itself stays model-free (NFR-3.1): run_gui schedules this
        AFTER the QML shell is interactive, and the load happens on the
        worker thread through the normal queue (single-owner contract
        intact). Measured effect: the first request's 1.4–1.6 s cold load
        collapses to the ~70 ms warm TTFC. No-op when the engine is already
        initialized; a failed warmup is silent (the first real request
        surfaces the actionable error when the user actually wants audio).
        """
        if self._worker is not None:
            engine = getattr(self._worker, "engine", None)
            if getattr(engine, "is_initialized", False):
                return
        worker = self._ensure_worker()
        if worker is not None:
            worker.submit(WarmupOp())

    @Slot(str, result=bool)
    def exportWav(self, path: str) -> bool:  # type: ignore[override]
        """Copy the committed artifact to ``path`` (or a timestamped default).

        The write runs off the GUI thread (a cap-length document is a
        multi-hundred-MB encode): returns True when the export started;
        completion lands on ``exportFinished(path, ok)`` (ok also flips
        ``lastExportPath`` for the existing toast). Nothing to export fails
        fast with errorText. Uses 48 kHz — the synthesis rate.
        """
        artifact = self._current_artifact
        if artifact is None or not artifact.path.is_file():
            self._set_error(self.tr("Chưa có gì để xuất — hãy tổng hợp âm thanh trước."))
            return False
        if self._exporting:
            self._set_error(self.tr("Đang xuất một tệp khác — vui lòng đợi."))
            return False
        target = Path(path.strip()) if path.strip() else self._default_export_path()
        source = artifact.path

        def work() -> tuple[str, str]:
            try:
                import shutil

                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(source, target)
                return str(target), ""
            except OSError as exc:
                return "", self.tr("Xuất WAV thất bại: {}").format(exc)

        self._artifact_store.protect(artifact)
        released = False

        def release_once() -> None:
            nonlocal released
            if released:
                return
            released = True
            self._release_artifact_after_export(artifact)

        def done(result: Any) -> None:
            try:
                self._on_export_finished(result)
            finally:
                release_once()

        self._set_exporting(True)
        try:
            self._run_bg(work, done, self)
        except Exception as exc:  # noqa: BLE001 - a rejected pool must not leak protection
            self._set_exporting(False)
            release_once()
            self._set_error(self.tr("Xuất WAV thất bại: {}").format(exc))
            self.exportFinished.emit("", False)
            return False
        return True

    def _on_export_finished(self, result: Any) -> None:
        """Export landed (pool thread → GUI thread): (path, error)."""
        self._set_exporting(False)
        path, error = result
        if error:
            self._set_error(error)
            self.exportFinished.emit("", False)
            return
        self._last_export_path = path
        self.lastExportPathChanged.emit()
        self.exportFinished.emit(path, True)

    def _release_artifact_after_export(self, artifact: SynthesisArtifact) -> None:
        self._artifact_store.release(artifact)
        self.release_retired_artifacts()

    def _set_exporting(self, value: bool) -> None:
        if value != self._exporting:
            self._exporting = value
            self.exportingChanged.emit()

    @Property(bool, notify=exportingChanged)
    def exporting(self) -> bool:
        return self._exporting

    def _default_export_path(self) -> Path:
        base = self._settings.output_dir.strip()
        stamp = _dt.datetime.now().strftime(EXPORT_PATTERN)
        if base:
            return Path(base) / stamp
        # QStandardPaths follows OneDrive redirection (common consumer
        # Windows) and localized XDG music dirs; ~/Music is the fallback for
        # headless/unknown-desktop runs where it resolves empty.
        music = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MusicLocation)
        return (Path(music) if music else Path.home() / "Music") / "VieNeuTTS" / stamp

    # ── replay: Phát without export ──────────────────────────────────────────

    @Slot()
    def replay(self) -> None:
        """Replay the current managed artifact with the attached file player."""
        artifact = self._current_artifact
        if artifact is None or not artifact.path.is_file():
            self._set_error(self.tr("Chưa có gì để phát — hãy tổng hợp âm thanh trước."))
            return
        self._stop_replay()
        playback = self._file_playback
        if playback is None or not hasattr(playback, "play"):
            self._set_error(self.tr("Hệ thống này không phát được âm thanh."))
            return
        self._artifact_store.protect(artifact)
        self._replay_artifact = artifact
        self._set_replay_active(True)
        self._begin_replay_position(artifact.duration_ms)
        try:
            playback.play(
                str(artifact.path),
                on_released=lambda: self._release_artifact_after_playback(artifact),
            )
        except Exception:  # noqa: BLE001 - file playback must never crash the UI
            self._release_artifact_after_playback(artifact)
            self._set_error(self.tr("Hệ thống này không phát được âm thanh."))

    @Slot()
    def stopReplay(self) -> None:
        """Stop any live replay (the Dừng side of the Phát/Dừng toggle)."""
        self._stop_replay()

    @Property(bool, notify=replayActiveChanged)
    def replayActive(self) -> bool:
        return self._replay_active

    @Property("QVariantList", notify=waveformEnvelopeChanged)
    def waveformEnvelope(self) -> list[float]:
        """Peak-normalized 0..1 overview buckets of the held audio (QML bars)."""
        return self._waveform_envelope

    @Property(float, notify=replayPositionChanged)
    def replayPosition(self) -> float:
        """Live replay playhead, 0..1; 0 whenever no replay is running."""
        return self._replay_position

    @Property(int, notify=replayDurationMsChanged)
    def replayDurationMs(self) -> int:
        """Length of the audio being (or last) replayed — for time labels."""
        return self._replay_duration_ms

    def attach_file_playback(self, playback: Any) -> None:
        """Wire the shared PlaybackController (large-audio replay path).

        create_app owns the QML ``playback`` context object; this seam hands
        it to the controller without coupling construction. ``finished``
        (EndOfMedia) closes OUR temp-file replay only — guarded on
        replayActive so exported-file/preview playback riding the same
        player is untouched.
        """
        self.detach_file_playback()
        self._file_playback = playback
        finished = getattr(playback, "finished", None)
        if finished is not None and hasattr(finished, "connect"):
            finished.connect(self._on_file_replay_finished)
        # A backend/decode error mid-replay must clear replayActive too, or
        # the Phát/Dừng toggle sticks on "Dừng" forever (finished never comes).
        error_changed = getattr(playback, "errorTextChanged", None)
        if error_changed is not None and hasattr(error_changed, "connect"):
            error_changed.connect(self._on_file_replay_error)
        # Playhead feed for the temp-file replay path (optional per the fake
        # contract — players without position signals simply leave it at 0).
        position_changed = getattr(playback, "positionChanged", None)
        if position_changed is not None and hasattr(position_changed, "connect"):
            position_changed.connect(self._on_file_replay_position)
        duration_changed = getattr(playback, "durationChanged", None)
        if duration_changed is not None and hasattr(duration_changed, "connect"):
            duration_changed.connect(self._on_file_replay_duration)

    def detach_file_playback(self) -> None:
        if self._file_playback is None:
            return
        for signal_name, handler in (
            ("finished", self._on_file_replay_finished),
            ("errorTextChanged", self._on_file_replay_error),
            ("positionChanged", self._on_file_replay_position),
            ("durationChanged", self._on_file_replay_duration),
        ):
            signal = getattr(self._file_playback, signal_name, None)
            if signal is not None and hasattr(signal, "disconnect"):
                with contextlib.suppress(RuntimeError, TypeError):
                    signal.disconnect(handler)
        self._file_playback = None

    def _stop_replay(self) -> None:
        """End file replay; release callback performs managed cleanup."""
        if not self._replay_active:
            return
        self._set_replay_active(False)
        self._end_replay_position()
        playback = self._file_playback
        if playback is not None and hasattr(playback, "stop"):
            try:
                playback.stop()
            except Exception:  # noqa: BLE001 - stopping must never raise
                logger.exception("stopping file replay failed")

    def _on_file_replay_finished(self) -> None:
        """EndOfMedia on the shared player: close OUR replay UI state."""
        if self._replay_active:
            self._set_replay_active(False)
            self._end_replay_position()
        if self._audition_state == "playing":
            self._reset_audition_tracking()

    def _on_stream_replay_finished(self) -> None:
        """Legacy stream-player completion hook (live replay is file-backed)."""
        self._set_stream_active(False)
        self._set_playback_state("idle")

    def _on_file_replay_error(self) -> None:
        """Player error while OUR temp-file replay is live: end it cleanly.

        Guarded like ``_on_file_replay_finished`` so errors from exported-file
        or preview playback riding the same shared player are ignored.
        """
        if self._replay_active:
            self._stop_replay()
        if self._audition_state == "playing":
            self._reset_audition_tracking()

    def _release_artifact_after_playback(self, artifact: SynthesisArtifact) -> None:
        if self._replay_artifact == artifact:
            self._replay_artifact = None
            self._set_replay_active(False)
            self._end_replay_position()
            self._set_replay_duration_ms(0)
        self._artifact_store.release(artifact)
        self.release_retired_artifacts()

    def release_retired_artifacts(self) -> None:
        for artifact in tuple(self._retired_artifacts):
            if self._artifact_store.remove_if_unprotected(artifact):
                self._retired_artifacts.discard(artifact)

    def _set_replay_active(self, value: bool) -> None:
        if value != self._replay_active:
            self._replay_active = value
            self.replayActiveChanged.emit()

    # ── replay playhead (PlaybackWaveform position feed) ─────────────────────

    def _begin_replay_position(self, duration_ms: int) -> None:
        """Arm the playhead for a starting replay (duration 0 = unknown yet)."""
        self._replay_pos_timer.stop()
        self._set_replay_duration_ms(max(0, int(duration_ms)))
        self._set_replay_position(0.0)
        if duration_ms > 0:
            self._replay_clock.start()
            self._replay_pos_timer.start()

    def _end_replay_position(self) -> None:
        """Stop advancing and park the playhead back at the start."""
        self._replay_pos_timer.stop()
        self._set_replay_position(0.0)

    def _on_replay_position_tick(self) -> None:
        if self._replay_duration_ms <= 0:
            return
        position = min(self._replay_clock.elapsed() / self._replay_duration_ms, 1.0)
        self._set_replay_position(position)
        if position >= 1.0:
            self._replay_pos_timer.stop()

    def _on_file_replay_position(self, ms: int) -> None:
        """QMediaPlayer progress → replayPosition for the current artifact."""
        if not self._replay_active:
            return
        if self._replay_duration_ms > 0:
            self._set_replay_position(min(max(ms / self._replay_duration_ms, 0.0), 1.0))

    def _on_file_replay_duration(self, ms: int) -> None:
        """Player resolved the artifact WAV's length → label + position scaling."""
        if not self._replay_active or ms <= 0:
            return
        self._set_replay_duration_ms(int(ms))

    def _set_replay_position(self, value: float) -> None:
        value = max(0.0, min(float(value), 1.0))
        if value != self._replay_position:
            self._replay_position = value
            self.replayPositionChanged.emit()

    def _set_replay_duration_ms(self, value: int) -> None:
        value = max(0, int(value))
        if value != self._replay_duration_ms:
            self._replay_duration_ms = value
            self.replayDurationMsChanged.emit()

    def _set_waveform_envelope(self, buckets: list[float]) -> None:
        if buckets != self._waveform_envelope:
            self._waveform_envelope = buckets
            self.waveformEnvelopeChanged.emit()

    # ── streaming playback session (FR-4.1, FR-4.2) ─────────────────────────

    def _ensure_stream_playback(self) -> StreamPlaybackController | Any | None:
        """Lazily build + wire the StreamPlaybackController (injectable seam)."""
        if self._stream_playback is not None:
            return self._stream_playback
        try:
            player = self._stream_playback_factory()
        except Exception:  # noqa: BLE001 - playback must never crash synthesis
            logger.exception("stream playback construction failed")
            return None
        self._stream_playback = player
        set_recorder = getattr(player, "set_performance_recorder", None)
        if set_recorder is not None:
            set_recorder(self._performance)
        level_ready = getattr(player, "levelReady", None)
        if level_ready is not None and hasattr(level_ready, "connect"):
            level_ready.connect(self._on_stream_level)
        replay_finished = getattr(player, "finished", None)
        if replay_finished is not None and hasattr(replay_finished, "connect"):
            replay_finished.connect(self._on_stream_replay_finished)
        live_playback_failed = getattr(player, "livePlaybackFailed", None)
        if live_playback_failed is not None and hasattr(live_playback_failed, "connect"):
            live_playback_failed.connect(self._on_live_playback_failed)
        return self._stream_playback

    def _start_stream_session(self, job_id: str) -> BoundedPcmTransport | None:
        """Open a transport-backed sink session without blocking synthesis."""
        self._stream_drain_timer.stop()  # a pending drain flip must not fire into this session
        player = self._ensure_stream_playback()
        if player is None:
            self._set_error(self.tr("Hệ thống này không phát được âm thanh."))
            return None
        transport = BoundedPcmTransport()
        try:
            begin_trace = getattr(player, "begin_trace", None)
            if begin_trace is not None:
                begin_trace(job_id)
            player.start(transport, job_id)
        except Exception:  # noqa: BLE001 - a broken backend must not stop TTS
            logger.exception("starting stream playback failed")
            self._set_error(self.tr("Hệ thống này không phát được âm thanh."))
            return None
        # Surface construction failure reported by the player itself.
        error_text = getattr(player, "errorText", "") or ""
        if error_text:
            self._set_error(error_text)
            player.stop()
            return None
        self._active_live_transport = transport
        self._live_playback_job_id = job_id
        self._set_playback_state("prebuffering")
        self._set_stream_active(True)
        self._set_stream_level(0.0)
        return transport

    def _stop_stream_playback_now(self) -> None:
        """Hard-stop any live sink session (cancel/new request); never raises."""
        self._stream_drain_timer.stop()
        self._set_stream_active(False)
        transport, self._active_live_transport = self._active_live_transport, None
        self._live_playback_job_id = None
        if transport is not None:
            transport.close(discard=True)
        self._set_playback_state("idle")
        player = self._stream_playback
        if player is None:
            return
        try:
            if getattr(player, "active", False):
                player.stop()
        except Exception:  # noqa: BLE001 - stopping audio must not raise into the UI
            logger.exception("stopping stream playback failed")

    def _finish_stream_playback(self) -> None:
        """Done path: end the UI session only once the sink drained its tail.

        The worker finishing does not mean the sound finished — up to a few
        hundred ms (chunk-scale) of audio can still sit in the sink's buffer.
        The meter (``streamActive``) used to die with the worker, visibly
        ahead of the last audible sample. Keep the session flagged for the
        buffered real-time duration (+ margin, mirroring play_buffer's drain
        allowance), then flip; cancel/new-request paths still stop it NOW.
        """
        player = self._stream_playback
        had_live_session = self._live_playback_job_id is not None and self._stream_active
        remaining_ms = 0
        if player is not None:
            begin_drain = getattr(player, "begin_drain", None)
            if callable(begin_drain):
                begin_drain()
            drain_ms = getattr(player, "buffered_drain_ms", None)
            if callable(drain_ms):
                remaining_ms = max(0, int(drain_ms()))
        if had_live_session and remaining_ms > 0:
            self._set_playback_state("draining")
            self._stream_drain_timer.start(max(remaining_ms + 300, 300))
        else:
            self._set_stream_active(False)
            self._set_playback_state("idle")
        self._active_live_transport = None
        self._live_playback_job_id = None

    def _on_stream_drain_finished(self) -> None:
        player = self._stream_playback
        if player is not None:
            with contextlib.suppress(Exception):
                player.stop()
        self._set_stream_active(False)
        self._set_playback_state("idle")

    def _on_live_playback_failed(self) -> None:
        """The player now discards transport bytes; synthesis stays artifact-first."""
        self._set_stream_active(False)
        self._set_playback_state("idle")

    def _set_stream_active(self, value: bool) -> None:
        if value != self._stream_active:
            self._stream_active = value
            self.streamActiveChanged.emit()

    def _set_stream_level(self, value: float) -> None:
        value = max(0.0, min(float(value), 1.0))
        if value != self._stream_level:
            self._stream_level = value
            self.streamLevelChanged.emit()

    def _set_playback_state(self, value: str) -> None:
        if value not in {"prebuffering", "generating", "draining", "idle"}:
            raise ValueError(f"invalid playback state: {value}")
        if value != self._playback_state:
            self._playback_state = value
            self.playbackStateChanged.emit()

    # ── document import (FR-3.3) ─────────────────────────────────────────────

    @Slot(str, result=bool)
    def importDocument(self, path: str) -> bool:
        """Import a .txt/.md/.docx/.pdf/.srt document off the GUI thread.

        Returns True when the import was accepted for parsing; the extracted
        text (or the failure, via ``errorText``) arrives on
        ``documentImported(path, text)`` — QML binds ``importing`` for the
        busy state. Errors never crash the UI.
        """
        if self._importing:
            self._set_error(self.tr("Đang nhập một tệp khác — vui lòng đợi."))
            return False
        self._set_error("")
        self._set_importing(True)
        keep_srt_raw = self._srt_keep_timestamps

        def work() -> tuple[str, str]:
            try:
                return import_document(path, keep_srt_raw=keep_srt_raw), ""
            except FileNotFoundError as exc:
                return "", self.tr("Không tìm thấy tệp: {}").format(exc)
            except DocumentImportError as exc:
                return "", str(exc)
            except Exception as exc:  # noqa: BLE001 - import must never crash
                return "", self.tr("Lỗi nhập tệp: {}").format(exc)

        self._run_bg(work, lambda result: self._on_document_imported(path, result), self)
        return True

    def _on_document_imported(self, path: str, result: Any) -> None:
        """Import landed (pool thread → GUI thread): (text, error)."""
        self._set_importing(False)
        text, error = result
        if error:
            self._set_error(error)
        self.documentImported.emit(path, text)

    def _set_importing(self, value: bool) -> None:
        if value != self._importing:
            self._importing = value
            self.importingChanged.emit()

    @Property(bool, notify=importingChanged)
    def importing(self) -> bool:
        return self._importing

    @Property(bool, notify=srtKeepTimestampsChanged)
    def srtKeepTimestamps(self) -> bool:
        """Whether .srt imports keep timecodes verbatim (default: clean text)."""
        return self._srt_keep_timestamps

    @srtKeepTimestamps.setter
    def srtKeepTimestamps(self, value: bool) -> None:  # noqa: F811
        value = bool(value)
        if value != self._srt_keep_timestamps:
            self._srt_keep_timestamps = value
            self.srtKeepTimestampsChanged.emit()

    # ── voice operations (FR-3.4) ────────────────────────────────────────────

    @Slot(str, str, bool)
    def addVoice(self, name: str, clip_path: str, denoise: bool) -> None:
        self._submit_voice_op(VoiceOp(op="add", name=name, clip_path=clip_path, denoise=denoise))

    @Slot(str)
    def removeVoice(self, name: str) -> None:
        self._submit_voice_op(VoiceOp(op="remove", name=name))

    @Slot(str)
    def denoisePreview(self, clip_path: str) -> None:
        self._submit_voice_op(VoiceOp(op="denoise", clip_path=clip_path))

    def _submit_voice_op(self, op: VoiceOp) -> None:
        try:
            worker = self._ensure_worker()
            job = new_synthesis_job("cloning", "voice_op", op)
        except ValueError as exc:
            self._set_error(f"Invalid voice operation: {exc}")
            return
        self._stop_audition_session()
        self._reset_audition_tracking()
        self._set_error("")
        self._set_busy(True)
        self._foreground_job_id = job.id
        self._foreground_is_voice_op = True
        self._set_foreground_job_state("queued")
        self.foregroundJobIdChanged.emit()
        if not worker.submit(job):
            self._foreground_job_id = None
            self._foreground_is_voice_op = False
            self._set_foreground_job_state("idle")
            self.foregroundJobIdChanged.emit()
            self._set_busy(False)
            self._set_error(self.tr("Không thể thêm tác vụ vì ứng dụng đang đóng."))

    # ── worker lifecycle ─────────────────────────────────────────────────────

    def _ensure_worker(self) -> Any:
        if self._worker is not None:
            return self._worker
        if self._engine is None:
            # Engine is built with the CURRENT settings; needsRestart was
            # consumed by shutdown() dropping the previous instance.
            # Official CPU baseline resolves auto→onnx with local SDK paths on
            # a clean CUDA-capable machine (Phase 1 Task 3).
            managed = self._model_status.location
            backend, managed_model = resolve_model_source(self._settings, managed)
            self._engine = self._engine_factory(
                backend=backend,
                precision=self._settings.precision,
                voices_dir=self._voices_dir,
                model_repo=self._settings.model_repo,
                managed_model=managed_model,
            )
        if self._worker_factory is not None:
            self._worker = self._worker_factory(self._engine)
        else:
            self._worker = InferenceWorker(self._engine, performance_recorder=self._performance)
        self._connect_worker(self._worker)
        self._worker.start()
        return self._worker

    def _connect_worker(self, worker: Any) -> None:
        worker.progress.connect(self._on_job_progress)
        worker.chunk_ready.connect(self._on_job_chunk)
        worker.terminal.connect(self._on_terminal)

    @Slot()
    def shutdown(self) -> None:
        """Stop the worker, stop stream playback, close the engine; safe any time.

        A worker thread stuck inside a non-cancellable SDK call is RETIRED
        (kept referenced, engine left open) instead of being dropped: freeing
        a running QThread aborts, and closing its engine under a live
        inference risks a native crash — leaking beats crashing at quit. A
        later shutdown() retries the pair once the thread has exited.
        """
        self._stop_replay()
        self._stop_audition_session()
        self._reset_audition_tracking()
        self._stop_stream_playback_now()
        # Bounded drain: an in-flight export/import write finishes before
        # teardown returns (callbacks may no longer run once exec() exits).
        drain_thread_pool()
        self._retry_retired_workers()
        if self._worker is not None:
            worker, self._worker = self._worker, None
            self._retire_worker(worker, self._engine)
            self._engine = None
        elif self._engine is not None:
            self._close_engine_quietly(self._engine)
            self._engine = None
        self._set_busy(False)
        if self._needs_restart:
            self._needs_restart = False
            self.needsRestartChanged.emit()

    def _retire_worker(self, worker: Any, engine: Any) -> None:
        """Stop ``worker``; close ``engine`` only once the thread is gone."""
        try:
            worker.stop()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            logger.exception("error stopping inference worker")
        if bool(getattr(worker, "isRunning", lambda: False)()):
            logger.warning("inference worker still running; deferring engine close")
            self._retired_workers.append((worker, engine))
            return
        self._close_engine_quietly(engine)

    def _retry_retired_workers(self) -> None:
        still_running: list[tuple[Any, Any]] = []
        for worker, engine in self._retired_workers:
            try:
                worker.stop()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.exception("error stopping retired inference worker")
            if bool(getattr(worker, "isRunning", lambda: False)()):
                still_running.append((worker, engine))
            else:
                self._close_engine_quietly(engine)
        self._retired_workers = still_running

    @staticmethod
    def _close_engine_quietly(engine: Any) -> None:
        if engine is None:
            return
        try:
            engine.close()
        except Exception:  # noqa: BLE001
            logger.exception("error closing engine")

    # ── worker signal handlers (queued to the main thread) ──────────────────

    def _set_foreground_job_state(self, state: str) -> None:
        if state != self._foreground_job_state:
            self._foreground_job_state = state
            self.foregroundJobStateChanged.emit()

    def _begin_foreground_trace(self, *, job_id: str, text: str, mode: str) -> None:
        self._foreground_job_id = job_id
        self._foreground_is_voice_op = False
        self._set_foreground_job_state("queued")
        self.foregroundJobIdChanged.emit()
        self._performance.begin(
            job_id,
            {
                "char_count": len(text),
                "mode": mode,
                "streaming": mode == "stream",
            },
        )
        self._performance.mark(job_id, "submitted")

    def _on_job_progress(self, event: JobProgress) -> None:
        job_id = getattr(event, "job_id", None)
        if job_id is not None and job_id == self._foreground_job_id:
            total = getattr(event, "total", 0)
            done = getattr(event, "done", 0)
            fraction = (done / total) if total > 0 else 0.0
            # The worker picked the job up: it is synthesizing, not waiting
            # behind the queue (e.g. an in-flight audiobook render). Only a
            # queued job promotes: late progress from a superseded delivery
            # must never clobber cancel_requested or a terminal state.
            if self._foreground_job_state == "queued":
                self._set_foreground_job_state("generating")
            if fraction != self._progress:
                self._progress = fraction
                self.progressChanged.emit()
            return
        listener = self._listener_by_job_id.get(job_id)
        if listener is not None:
            listener.on_synthesis_progress(event)
        # Else: stale delivery for a superseded job — drop.

    def _on_job_chunk(self, event: JobChunk) -> None:
        """Metadata events update live state; PCM stays in the transport."""
        job_id = getattr(event, "job_id", None)
        if job_id is not None and job_id == self._audition_job_id:
            # Silent audition: no live transport, so chunks only mark
            # progress acoustically — the row stays "loading" until the
            # finished file plays once.
            self._performance.mark(job_id, "controller_first_chunk")
            return
        if job_id is not None and job_id == self._foreground_job_id:
            if self._foreground_job_state == "queued":
                self._set_foreground_job_state("generating")
            if job_id not in self._chunk_seen_by_job_id:
                self._chunk_seen_by_job_id.add(job_id)
                self._performance.mark(job_id, "controller_first_chunk")
            self._set_stream_level(float(getattr(event, "peak", 0.0)))
            if job_id == self._live_playback_job_id:
                self._set_playback_state("generating")
            return
        listener = self._listener_by_job_id.get(job_id)
        if listener is not None:
            listener.on_synthesis_chunk(event)
        # Else: stale delivery for a superseded job — drop.

    def _on_terminal(self, event: JobTerminal) -> None:
        job_id = event.job_id
        # The mapping pops BEFORE delivery so a reentrant submit from inside
        # the handler cannot receive the finished job's late events.
        listener = self._listener_by_job_id.pop(job_id, None)
        if listener is not None:
            listener.on_synthesis_terminal(event)
            return
        if job_id == self._audition_job_id:
            self._audition_job_id = None
            self._chunk_seen_by_job_id.discard(job_id)
            if event.state == "completed":
                self._complete_audition(job_id, event.value)
            elif event.state == "cancelled":
                self._cancel_audition(job_id)
            else:
                self._fail_audition(job_id, str(event.error))
            return
        if job_id != self._foreground_job_id:
            return  # stale delivery for a superseded foreground job
        is_voice_op = self._foreground_is_voice_op
        self._foreground_job_id = None
        self._foreground_is_voice_op = False
        self._chunk_seen_by_job_id.discard(job_id)
        self.foregroundJobIdChanged.emit()
        if event.state == "completed":
            self._set_foreground_job_state("completed")
            if is_voice_op:
                self._complete_voice_op(event.value)
            else:
                self._complete_foreground_audio(job_id, event.value)
        elif event.state == "cancelled":
            self._set_foreground_job_state("cancelled")
            self._cancel_foreground_audio(job_id)
        else:
            self._set_foreground_job_state("failed")
            if is_voice_op:
                self._set_error(str(event.error))
                self._set_busy(False)
            else:
                self._fail_foreground_audio(job_id, str(event.error))

    def _complete_foreground_audio(self, job_id: str, value: Any) -> None:
        self._performance.mark(job_id, "controller_done")
        # Fake workers never finish traces; the real worker already finished
        # this job before emitting (same outcome — Task 4 hardens finish to
        # first-wins for genuinely divergent writers).
        self._performance.finish(job_id, "completed")
        if not isinstance(value, SynthesisArtifact) or value.job_id != job_id:
            self._fail_foreground_audio(job_id, self.tr("Tệp âm thanh không hợp lệ."))
            return
        previous = self._current_artifact
        self._current_artifact = value
        if previous is not None and previous != value:
            self._retired_artifacts.add(previous)
        self.hasArtifactChanged.emit()
        self.hasAudioChanged.emit()
        self.artifactPathChanged.emit()
        self._set_waveform_envelope([])
        self._schedule_waveform(value)
        self.release_retired_artifacts()
        if self._progress != 1.0:
            self._progress = 1.0
            self.progressChanged.emit()
        # Silent-by-setting synthesis (livePreview OFF at submit AND at done):
        # no live audio played, so replay the finished artifact from start.
        # Live jobs (incl. live fallbacks) keep today's drain-tail behavior —
        # replaying there would overlap the tail still playing out.
        silent_job = not self._foreground_live
        self._foreground_live = False
        # Session over for the UI (busy/streamActive); the sink keeps draining
        # whatever is still buffered so the tail of the audio plays out.
        self._finish_stream_playback()
        self._set_busy(False)
        if silent_job and not self._settings.live_preview:
            self._auto_replay_after_silent_synthesis()

    def _auto_replay_after_silent_synthesis(self) -> None:
        """Replay the finished artifact after silent synthesis (livePreview OFF).

        Guarded quiet: no-audio machines and player-less contexts (tests,
        export-only flows) complete silently instead of raising an error
        banner. replay() itself handles the play call and its own failures.
        """
        if not self.audioAvailable:
            return
        playback = self._file_playback
        if playback is None or not hasattr(playback, "play"):
            return
        self.replay()

    def _schedule_waveform(self, artifact: SynthesisArtifact) -> None:
        def work() -> tuple[str, list[float] | None]:
            try:
                return artifact.job_id, compute_waveform_envelope_from_wav(artifact.path)
            except Exception:  # noqa: BLE001 - artifact stays usable without an overview
                logger.exception("computing artifact waveform envelope failed")
                return artifact.job_id, None

        def done(result: tuple[str, list[float] | None]) -> None:
            job_id, envelope = result
            current = self._current_artifact
            if current is None or current.job_id != job_id or envelope is None:
                return
            self._set_waveform_envelope(envelope)

        self._run_bg(work, done, self)

    def _cancel_foreground_audio(self, job_id: str) -> None:
        # User-initiated: stop playback immediately + reset silently and
        # notify for a toast — not an error banner (documented policy).
        # Bypasses _set_error, so modelsMissing is intentionally NOT
        # touched: a cancel is neither a new error nor a success signal.
        self._foreground_live = False
        self._stop_stream_playback_now()
        self._set_busy(False)
        self._performance.finish(job_id, "cancelled")
        self.cancelled.emit()

    def _fail_foreground_audio(self, job_id: str, message: str) -> None:
        self._foreground_live = False
        self._stop_stream_playback_now()
        self._performance.mark(job_id, "controller_error")
        self._performance.finish(job_id, "failed")
        self._set_error(message)
        self._set_busy(False)

    def _complete_voice_op(self, value: Any) -> None:
        payload = value if isinstance(value, dict) else {}
        op = payload.get("op")
        if op == "denoise":
            audio = payload.get("audio")
            sample_rate = int(payload.get("sample_rate") or 44_100)
            target = self._data_dir / PREVIEW_FILENAME
            try:
                write_wav_file(np.asarray(audio), target, sample_rate=sample_rate)
            except Exception as exc:  # noqa: BLE001
                self._set_error(f"Preview failed: {exc}")
                self._set_busy(False)
                return
            self._preview_path = str(target)
            self.previewPathChanged.emit()
        else:
            self.refreshVoices()
        self._set_busy(False)

    def _on_stream_level(self, value: float) -> None:
        """Rolling peak envelope for the QML WaveformIndicator (FR-4.5)."""
        self._set_stream_level(value)

    # (Voice-op terminals land in _on_terminal → _complete_voice_op.)

    # ── settings seam (FR-3.5) ──────────────────────────────────────────────

    @Property(str, notify=backendChanged)
    def backend(self) -> str:
        return self._settings.backend

    @backend.setter
    def backend(self, value: str) -> None:
        self._set_setting(
            "backend", value, allowed={"auto", "onnx", "torch"}, engine_affecting=True
        )

    @Property(str, notify=precisionChanged)
    def precision(self) -> str:
        return self._settings.precision

    @precision.setter
    def precision(self, value: str) -> None:
        self._set_setting("precision", value, allowed={"int8", "fp32"}, engine_affecting=True)

    @Property(str, notify=modelRepoChanged)
    def modelRepo(self) -> str:
        return self._settings.model_repo

    @modelRepo.setter
    def modelRepo(self, value: str) -> None:
        if not isinstance(value, str):
            self._set_error(self.tr("modelRepo phải là chuỗi ký tự."))
            return
        # Blank → "" = official default repo (settings validation rejects the
        # pattern only for non-empty values).
        self._set_setting("model_repo", value.strip(), engine_affecting=True)

    @Property(str, notify=defaultVoiceChanged)
    def defaultVoice(self) -> str:
        return self._settings.default_voice

    @defaultVoice.setter
    def defaultVoice(self, value: str) -> None:
        if not isinstance(value, str) or not value.strip():
            self._set_error(self.tr("defaultVoice phải là chuỗi ký tự không trống."))
            return
        self._set_setting("default_voice", value)

    @Property(str, notify=outputDirChanged)
    def outputDir(self) -> str:
        return self._settings.output_dir

    @outputDir.setter
    def outputDir(self, value: str) -> None:
        if not isinstance(value, str):
            self._set_error(self.tr("outputDir phải là chuỗi ký tự."))
            return
        self._set_setting("output_dir", value)

    @Property(float, notify=temperatureChanged)
    def temperature(self) -> float:
        return float(self._settings.temperature)

    @temperature.setter
    def temperature(self, value: float) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0.05 <= value <= 2.0
        ):
            self._set_error(self.tr("temperature phải là số trong khoảng 0.05 đến 2.0."))
            return
        self._set_setting("temperature", float(value))

    @Property(float, notify=speedChanged)
    def speed(self) -> float:
        return float(self._settings.speed)

    @speed.setter
    def speed(self, value: float) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0.5 <= value <= 2.0
        ):
            self._set_error(self.tr("speed phải là số trong khoảng 0.5 đến 2.0."))
            return
        self._set_setting("speed", float(value))

    @Property(float, notify=silencePChanged)
    def silenceP(self) -> float:
        return float(self._settings.silence_p)

    @silenceP.setter
    def silenceP(self, value: float) -> None:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not 0.0 <= value <= 2.0
        ):
            self._set_error(self.tr("silence_p phải là số trong khoảng 0.0 đến 2.0."))
            return
        self._set_setting("silence_p", float(value))

    @Property(bool, notify=livePreviewChanged)
    def livePreview(self) -> bool:
        """Play chunks live while generating (OFF = silent, then replay from start)."""
        return self._settings.live_preview

    @livePreview.setter
    def livePreview(self, value: bool) -> None:  # noqa: F811
        value = bool(value)
        if value == self._settings.live_preview:
            return
        self._set_setting("live_preview", value)

    @Property(str, notify=themeChanged)
    def theme(self) -> str:
        return self._settings.theme

    @theme.setter
    def theme(self, value: str) -> None:
        # Theme applies immediately (the QML Theme singleton reads it live).
        self._set_setting("theme", value, allowed={"system", "light", "dark"})

    @Property(str, notify=languageChanged)
    def language(self) -> str:
        return self._settings.language

    @language.setter
    def language(self, value: str) -> None:
        # Persists + notifies; the bootstrap's languageChanged handler does
        # the LIVE apply (translator swap + engine.retranslate()).
        self._set_setting("language", value, allowed=set(SUPPORTED_LANGUAGES))

    @Property(str, constant=True)
    def appliedLanguage(self) -> str:
        """Concrete language the UI was STARTED with ("vi"/"en").

        Frozen at construction — the initial translator choice. Later
        switches apply live, so this is startup history, not current state.
        """
        return self._applied_language

    def _set_setting(
        self,
        key: str,
        value: Any,
        allowed: set[str] | None = None,
        engine_affecting: bool = False,
    ) -> None:
        if allowed is not None and value not in allowed:
            self._set_error(f"{key} must be one of {sorted(allowed)}, got {value!r}")
            return
        if getattr(self._settings, key) == value:
            return
        try:
            self._settings = replace(self._settings, **{key: value})
            save_settings(self._settings, self._data_dir)
        except ValueError as exc:
            self._set_error(f"Invalid {key}: {exc}")
            return
        except OSError as exc:  # noqa: BLE001 - disk-full/read-only must not raise into a slot
            # The in-memory value still applies live; only persistence failed.
            self._set_error(f"Could not save settings: {exc}")
            return
        self._set_error("")
        for name, signal in (
            ("backend", self.backendChanged),
            ("precision", self.precisionChanged),
            ("model_repo", self.modelRepoChanged),
            ("default_voice", self.defaultVoiceChanged),
            ("output_dir", self.outputDirChanged),
            ("speed", self.speedChanged),
            ("live_preview", self.livePreviewChanged),
            ("silence_p", self.silencePChanged),
            ("theme", self.themeChanged),
            ("language", self.languageChanged),
        ):
            if name == key:
                signal.emit()
        if engine_affecting and self._engine is not None:
            # The running engine was built with the old value; the change
            # applies on next engine init (after shutdown/restart).
            self._needs_restart = True
            self.needsRestartChanged.emit()

    # ── consent gate (FR-3.6) ────────────────────────────────────────────────

    @Slot()
    def acknowledgeConsent(self) -> None:
        self._consent = True
        try:
            self._consent_path.parent.mkdir(parents=True, exist_ok=True)
            self._consent_path.write_text(json.dumps({"consent": True}), encoding="utf-8")
        except OSError as exc:
            logger.warning("could not persist cloning consent (%s)", exc)
        self.consentGivenChanged.emit()

    def _load_consent(self) -> bool:
        try:
            data = json.loads(self._consent_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return False
        return isinstance(data, dict) and data.get("consent") is True


def _parse_region(description: str) -> str | None:
    """Extract the region token from ``"Nam · Bắc · Phong cách ..."``.

    The middle ``·``-separated token is the region (Bắc/Trung/Nam). Returns
    None when the description does not match the pattern.
    """
    parts = [p.strip() for p in description.split("·")]
    if len(parts) != 3:
        return None
    return parts[1] if parts[1] in {"Bắc", "Trung", "Nam"} else None


def _display_label(entry: dict[str, str], region: str | None) -> str:
    """Human label for a preset: prefer the full description, else the name."""
    if region is not None:
        return f"{entry['name']} — {entry['description']}"
    return entry.get("description") or entry["name"]
