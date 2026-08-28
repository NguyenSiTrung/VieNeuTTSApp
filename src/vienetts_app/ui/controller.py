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
                      envelope of the latest streamed chunk (FR-4.5 groundwork)
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
for a second model instance. Contract:

    attach_synthesis_listener(listener)   begin routing; listener is
                                          duck-typed with on_synthesis_
                                          progress(payload)/done(audio)/
                                          error(message) plus OPTIONAL
                                          on_synthesis_chunk(chunk) (FR-A9
                                          timeline capture)
    submit_stream_for_listener(text, voice) -> bool
                                          submit a stream-mode job owned by
                                          the attached listener; REFUSES
                                          (False) while any job is in flight
                                          (busy) — the caller retries on
                                          busyChanged — so a job can never
                                          complete after attach that was not
                                          submitted by the listener
    detach_synthesis_listener()           stop routing

While attached, worker progress/done/error delegate to the listener (app-tab
audio/progress/error state is untouched; busy still flips — it is honest
engine-wide state). The listener MUST detach from within its done/error
handler: later queued jobs (e.g. a Text-tab generate submitted while a
listener job ran) then route normally again. shutdown() detaches.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import json
import logging
import os
import tempfile
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import Property, QLocale, QObject, Signal, Slot

from vienetts_app.core.audio import write_wav_file
from vienetts_app.core.engine import (
    TTSEngine,
    is_models_missing,
    preset_voices,
    saved_voice_names,
)
from vienetts_app.core.importers import DocumentImportError, import_document
from vienetts_app.core.models import TTSRequest, VoiceOp
from vienetts_app.core.settings import load_settings, save_settings
from vienetts_app.ui import playback as _playback
from vienetts_app.ui.i18n import SUPPORTED_LANGUAGES, resolve_language
from vienetts_app.ui.stream_playback import StreamPlaybackController
from vienetts_app.workers.inference_worker import CANCELLED_MESSAGE, InferenceWorker

logger = logging.getLogger(__name__)

CONSENT_FILENAME = "cloning_consent.json"
PREVIEW_FILENAME = "preview.wav"
EXPORT_PATTERN = "vienetts_%Y%m%d_%H%M%S.wav"
SAMPLE_RATE = 48_000  # synthesis audio (infer/infer_stream); denoise is 44.1 kHz
REPLAY_MEMORY_LIMIT_BYTES = 5 * 1024 * 1024  # 5 MB ~ 27s of 48kHz mono float32


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
    lastExportPathChanged = Signal()
    previewPathChanged = Signal()
    needsRestartChanged = Signal()
    consentGivenChanged = Signal()
    backendChanged = Signal()
    precisionChanged = Signal()
    defaultVoiceChanged = Signal()
    outputDirChanged = Signal()
    temperatureChanged = Signal()
    themeChanged = Signal()
    languageChanged = Signal()
    # Streaming playback (FR-4.2, FR-4.5 groundwork).
    streamActiveChanged = Signal()
    streamLevelChanged = Signal()
    replayActiveChanged = Signal()
    # Edge-case surfaces (FR-4.6a/c).
    modelsMissingChanged = Signal()
    audioAvailableChanged = Signal()
    # Transient notifications (no property payload — QML toasts on fire).
    cancelled = Signal()

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
        self._has_audio = False
        self._audio: np.ndarray | None = None
        self._last_export_path = ""
        self._preview_path = ""
        self._needs_restart = False
        self._consent = self._load_consent()
        self._voices = self._build_voices()
        self._stream_active = False
        self._stream_level = 0.0
        self._replay_active = False
        self._temp_replay_path: Path | None = None
        # Shared PlaybackController, wired post-construction by create_app
        # (temp-file replay path only; None keeps startup player-free).
        self._file_playback: Any | None = None
        # FR-4.6c: True only while the LAST error is a models-missing error;
        # recomputed inside _set_error on every transition (see docstring).
        self._models_missing = False
        # Synthesis-listener seam (FR-A8): None = normal app-tab routing.
        self._synthesis_listener: Any | None = None
        # Worker/engine pairs that outlived a shutdown() wait (a plain infer
        # call cannot be interrupted mid-way). Kept referenced so neither a
        # running QThread nor its engine is freed under the thread's feet;
        # a later shutdown() (or process exit) finishes the teardown.
        self._retired_workers: list[tuple[Any, Any]] = []

    # ── voice catalog (FR-3.1, model-free) ──────────────────────────────────

    def _build_voices(self) -> list[dict[str, Any]]:
        grouped: dict[str, list[dict[str, str]]] = {region: [] for region, _ in _REGION_GROUPS}
        grouped[FALLBACK_GROUP] = []
        for entry in self._catalog_fn():
            region = _parse_region(entry.get("description", ""))
            group = region if region in grouped else FALLBACK_GROUP
            grouped[group].append({"id": entry["name"], "label": _display_label(entry, region)})
        result = [{"label": label, "voices": grouped[region]} for region, label in _REGION_GROUPS]
        if grouped[FALLBACK_GROUP]:
            result.append({"label": FALLBACK_GROUP, "voices": grouped[FALLBACK_GROUP]})
        cloned_names = self._saved_names_fn(self._voices_dir)
        if cloned_names:
            result.append(
                {"label": CLONED_GROUP, "voices": [{"id": n, "label": n} for n in cloned_names]}
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
        return self._has_audio

    @Property(str, notify=lastExportPathChanged)
    def lastExportPath(self) -> str:
        return self._last_export_path

    @Property(str, notify=previewPathChanged)
    def previewPath(self) -> str:
        return self._preview_path

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

    def attach_synthesis_listener(self, listener: Any) -> None:
        """Route worker results to ``listener`` until detached (see class doc)."""
        self._synthesis_listener = listener

    def detach_synthesis_listener(self) -> None:
        self._synthesis_listener = None

    def submit_stream_for_listener(self, text: str, voice: str | None) -> bool:
        """Submit a listener-owned stream-mode synthesis job.

        Refuses (False) while any job is in flight — the attach/submit pair
        is then atomic w.r.t. job completion, which is what makes
        attachment-based routing safe (a job submitted BEFORE attach can
        never complete after it). The caller retries on ``busyChanged``.
        """
        if self._synthesis_listener is None:
            return False
        if not text or not text.strip():
            return False
        if self._busy:
            return False
        try:
            request = TTSRequest(
                text=text,
                voice=voice or None,
                mode="stream",
                temperature=self._settings.temperature,
            )
        except ValueError as exc:
            self._set_error(f"Invalid request: {exc}")
            return False
        worker = self._ensure_worker()
        self._stop_stream_playback_now()
        self._set_error("")
        self._set_busy(True)
        worker.submit(request)
        return True

    # ── synthesis ────────────────────────────────────────────────────────────

    def _begin_synthesis(self) -> Any:
        """Shared pre-submit sequence for generate/generateStream.

        Resets held audio + error state, drops any live streaming session
        (FR-4.2: a new request must not inherit old sink audio), and flips
        busy. Returns the worker ready to receive the submission.
        """
        worker = self._ensure_worker()
        self._stop_replay()
        self._stop_stream_playback_now()
        self._has_audio = False
        self._audio = None
        self.hasAudioChanged.emit()
        self._set_error("")
        self._set_busy(True)
        return worker

    @Slot(str, str)
    def generate(self, text: str, voice: str) -> None:
        """Submit a batch-synthesis job; blank text is a no-op (FR-3.x)."""
        if not text or not text.strip():
            return
        try:
            request = TTSRequest(
                text=text,
                voice=voice or None,
                mode="infer",
                temperature=self._settings.temperature,
            )
        except ValueError as exc:
            self._set_error(f"Invalid request: {exc}")
            return
        worker = self._begin_synthesis()
        worker.submit(request)

    @Slot(str, str)
    def generateStream(self, text: str, voice: str) -> None:
        """Submit a STREAMING job and start playing chunks as they arrive.

        Same validation/no-op rules as ``generate`` (mode="stream" request,
        temperature from settings). Full audio is still retained on done, so
        export/replay keep working; ``streamActive`` stays True until
        done/error/cancel.
        """
        if not text or not text.strip():
            return
        try:
            request = TTSRequest(
                text=text,
                voice=voice or None,
                mode="stream",
                temperature=self._settings.temperature,
            )
        except ValueError as exc:
            self._set_error(f"Invalid request: {exc}")
            return
        worker = self._begin_synthesis()
        self._start_stream_session()
        worker.submit(request)

    @Slot()
    def cancel(self) -> None:
        """Cancel synthesis AND stop stream playback immediately (FR-4.2)."""
        if self._worker is not None:
            self._worker.cancel()
        self._stop_stream_playback_now()

    @Slot(str, result=bool)
    def exportWav(self, path: str) -> bool:  # type: ignore[override]
        """Write the held audio to ``path`` (or a timestamped default).

        Returns True on success; sets errorText and returns False when there
        is nothing to export (no crash). Uses 48 kHz — the synthesis rate.
        """
        if self._audio is None or self._audio.size == 0:
            self._set_error("Nothing to export yet — generate audio first.")
            return False
        target = path.strip() or self._default_export_path()
        try:
            write_wav_file(self._audio, target, sample_rate=SAMPLE_RATE)
        except Exception as exc:  # noqa: BLE001 - export must never crash the UI
            self._set_error(f"Export failed: {exc}")
            return False
        self._last_export_path = str(target)
        self.lastExportPathChanged.emit()
        return True

    def _default_export_path(self) -> Path:
        base = self._settings.output_dir.strip()
        stamp = _dt.datetime.now().strftime(EXPORT_PATTERN)
        return (Path(base) if base else Path.home() / "Music" / "VieNeuTTS") / stamp

    # ── replay: Phát without export ──────────────────────────────────────────

    @Slot()
    def replay(self) -> None:
        """Replay held audio directly — no export, no dialog, no saved file.

        ≤ REPLAY_MEMORY_LIMIT_BYTES replays from RAM through the stream sink;
        anything larger plays from a temp WAV via the attached file player
        that is deleted as soon as the replay ends (nothing ever lands in
        the user's output folder — that stays "Lưu nhanh"/"Xuất WAV" only).
        """
        if self._audio is None or self._audio.size == 0:
            self._set_error("Nothing to play yet — generate audio first.")
            return
        self._stop_replay()
        if int(self._audio.nbytes) <= REPLAY_MEMORY_LIMIT_BYTES:
            self._replay_from_memory()
        else:
            self._replay_from_temp_file()

    @Slot()
    def stopReplay(self) -> None:
        """Stop any live replay (the Dừng side of the Phát/Dừng toggle)."""
        self._stop_replay()

    @Property(bool, notify=replayActiveChanged)
    def replayActive(self) -> bool:
        return self._replay_active

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

    def detach_file_playback(self) -> None:
        if self._file_playback is None:
            return
        finished = getattr(self._file_playback, "finished", None)
        if finished is not None and hasattr(finished, "disconnect"):
            with contextlib.suppress(RuntimeError, TypeError):
                finished.disconnect(self._on_file_replay_finished)
        error_changed = getattr(self._file_playback, "errorTextChanged", None)
        if error_changed is not None and hasattr(error_changed, "disconnect"):
            with contextlib.suppress(RuntimeError, TypeError):
                error_changed.disconnect(self._on_file_replay_error)
        self._file_playback = None

    def _replay_from_memory(self) -> None:
        player = self._ensure_stream_playback()
        if player is None:
            self._set_error("Audio playback is unavailable on this system")
            return
        if not player.play_buffer(self._audio):
            self._set_error("Audio playback is unavailable on this system")
            return
        error_text = getattr(player, "errorText", "") or ""
        if error_text:
            self._set_error(error_text)
            return
        self._set_stream_active(True)
        self._set_replay_active(True)

    def _replay_from_temp_file(self) -> None:
        playback = self._file_playback
        if playback is None or not hasattr(playback, "play"):
            self._set_error("Audio playback is unavailable on this system")
            return
        try:
            fd, name = tempfile.mkstemp(prefix="vienetts_replay_", suffix=".wav")
            os.close(fd)
            self._temp_replay_path = Path(name)
            write_wav_file(self._audio, self._temp_replay_path, sample_rate=SAMPLE_RATE)
        except Exception as exc:  # noqa: BLE001 - replay must never crash the UI
            self._set_error(f"Playback failed: {exc}")
            self._delete_temp_replay_file()
            return
        self._set_replay_active(True)
        playback.play(str(self._temp_replay_path))
        error_text = getattr(playback, "errorText", "") or ""
        if error_text:  # player rejected the file (construction/decode failure)
            self._set_error(error_text)
            self._stop_replay()

    def _stop_replay(self) -> None:
        """End any live replay on either path and drop the temp WAV."""
        if not self._replay_active and self._temp_replay_path is None:
            return
        self._set_replay_active(False)
        self._stop_stream_playback_now()
        if self._temp_replay_path is not None:  # OUR file is on the player
            playback = self._file_playback
            if playback is not None and hasattr(playback, "stop"):
                try:
                    playback.stop()
                except Exception:  # noqa: BLE001 - stopping must never raise
                    logger.exception("stopping file replay failed")
        self._delete_temp_replay_file()

    def _on_stream_replay_finished(self) -> None:
        """Drain timer fired: the RAM replay ended on its own."""
        if self._replay_active:
            self._set_replay_active(False)
            self._set_stream_active(False)

    def _on_file_replay_finished(self) -> None:
        """EndOfMedia on the shared player: close OUR replay only."""
        if self._replay_active:
            self._set_replay_active(False)
            self._delete_temp_replay_file()

    def _on_file_replay_error(self) -> None:
        """Player error while OUR temp-file replay is live: end it cleanly.

        Guarded like ``_on_file_replay_finished`` so errors from exported-file
        or preview playback riding the same shared player are ignored.
        """
        if self._replay_active and self._temp_replay_path is not None:
            self._stop_replay()

    def _delete_temp_replay_file(self) -> None:
        path, self._temp_replay_path = self._temp_replay_path, None
        if path is None:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.exception("removing replay temp file %s failed", path)

    def _set_replay_active(self, value: bool) -> None:
        if value != self._replay_active:
            self._replay_active = value
            self.replayActiveChanged.emit()

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
        level_ready = getattr(player, "levelReady", None)
        if level_ready is not None and hasattr(level_ready, "connect"):
            level_ready.connect(self._on_stream_level)
        replay_finished = getattr(player, "finished", None)
        if replay_finished is not None and hasattr(replay_finished, "connect"):
            replay_finished.connect(self._on_stream_replay_finished)
        return self._stream_playback

    def _start_stream_session(self) -> bool:
        """Open a sink session; False (unavailable) never blocks synthesis."""
        player = self._ensure_stream_playback()
        if player is None:
            self._set_error("Audio playback is unavailable on this system")
            return False
        try:
            player.start()
        except Exception:  # noqa: BLE001 - a broken backend must not stop TTS
            logger.exception("starting stream playback failed")
            self._set_error("Audio playback is unavailable on this system")
            return False
        # Surface construction failure reported by the player itself.
        error_text = getattr(player, "errorText", "") or ""
        if error_text:
            self._set_error(error_text)
        self._set_stream_active(True)
        self._set_stream_level(0.0)
        return True

    def _stop_stream_playback_now(self) -> None:
        """Hard-stop any live sink session (cancel/new request); never raises."""
        self._set_stream_active(False)
        player = self._stream_playback
        if player is None:
            return
        try:
            if getattr(player, "active", False):
                player.stop()
        except Exception:  # noqa: BLE001 - stopping audio must not raise into the UI
            logger.exception("stopping stream playback failed")

    def _finish_stream_playback(self) -> None:
        """Done path: end the UI session; let buffered audio drain naturally."""
        self._set_stream_active(False)

    def _set_stream_active(self, value: bool) -> None:
        if value != self._stream_active:
            self._stream_active = value
            self.streamActiveChanged.emit()

    def _set_stream_level(self, value: float) -> None:
        value = max(0.0, min(float(value), 1.0))
        if value != self._stream_level:
            self._stream_level = value
            self.streamLevelChanged.emit()

    # ── document import (FR-3.3) ─────────────────────────────────────────────

    @Slot(str, result=str)
    def importDocument(self, path: str) -> str:
        """Import a .txt/.md/.docx/.pdf document; returns extracted text.

        Errors are surfaced via ``errorText`` and the method returns ``""``
        (QML callers treat an empty result as failure — never a crash).
        """
        try:
            return import_document(path)
        except FileNotFoundError as exc:
            self._set_error(self.tr("Không tìm thấy tệp: {}").format(exc))
        except DocumentImportError as exc:
            self._set_error(str(exc))
        except Exception as exc:  # noqa: BLE001 - import must never crash the UI
            self._set_error(self.tr("Lỗi nhập tệp: {}").format(exc))
        return ""

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
            self._set_error("")
            self._set_busy(True)
            worker.submit(op)
        except ValueError as exc:
            self._set_error(f"Invalid voice operation: {exc}")

    # ── worker lifecycle ─────────────────────────────────────────────────────

    def _ensure_worker(self) -> Any:
        if self._worker is not None:
            return self._worker
        if self._engine is None:
            # Engine is built with the CURRENT settings; needsRestart was
            # consumed by shutdown() dropping the previous instance.
            self._engine = self._engine_factory(
                backend=self._settings.backend,
                precision=self._settings.precision,
                voices_dir=self._voices_dir,
            )
        if self._worker_factory is not None:
            self._worker = self._worker_factory(self._engine)
        else:
            self._worker = InferenceWorker(self._engine)
        self._connect_worker(self._worker)
        self._worker.start()
        return self._worker

    def _connect_worker(self, worker: Any) -> None:
        worker.progress.connect(self._on_progress)
        worker.chunk_ready.connect(self._on_chunk_ready)
        worker.done.connect(self._on_done)
        worker.error.connect(self._on_error)
        worker.voice_op_done.connect(self._on_voice_op_done)

    @Slot()
    def shutdown(self) -> None:
        """Stop the worker, stop stream playback, close the engine; safe any time.

        A worker thread stuck inside a non-cancellable SDK call is RETIRED
        (kept referenced, engine left open) instead of being dropped: freeing
        a running QThread aborts, and closing its engine under a live
        inference risks a native crash — leaking beats crashing at quit. A
        later shutdown() retries the pair once the thread has exited.
        """
        self.detach_synthesis_listener()
        self._stop_replay()
        self._stop_stream_playback_now()
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

    def _on_progress(self, payload: Any) -> None:
        if self._synthesis_listener is not None:
            self._synthesis_listener.on_synthesis_progress(payload)
            return
        total = getattr(payload, "total", 0)
        done = getattr(payload, "done", 0)
        fraction = (done / total) if total > 0 else 0.0
        if fraction != self._progress:
            self._progress = fraction
            self.progressChanged.emit()

    def _on_chunk_ready(self, chunk: Any) -> None:
        """Stream session live? Then this chunk becomes audio (FR-4.1).

        Listener-owned jobs (audiobook renders) route chunks to the attached
        listener's OPTIONAL ``on_synthesis_chunk`` instead — it counts samples
        per segment to build the chapter timeline (FR-A9) — and never feed
        the app sink in parallel. Listeners without the method are unaffected.
        """
        if self._synthesis_listener is not None:
            handler = getattr(self._synthesis_listener, "on_synthesis_chunk", None)
            if handler is not None:
                handler(chunk)
            return
        if not self._stream_active or self._stream_playback is None:
            return
        try:
            self._stream_playback.feed(chunk)
        except Exception:  # noqa: BLE001 - a feed failure must not kill the UI
            logger.exception("feeding stream playback failed")

    def _on_done(self, audio: Any) -> None:
        if self._synthesis_listener is not None:
            # Listener-owned job: app-tab audio/progress state untouched; the
            # listener detaches from inside its handler (seam contract).
            self._set_busy(False)
            self._synthesis_listener.on_synthesis_done(audio)
            return
        self._audio = np.asarray(audio)
        self._has_audio = True
        self.hasAudioChanged.emit()
        if self._progress != 1.0:
            self._progress = 1.0
            self.progressChanged.emit()
        # Session over for the UI (busy/streamActive); the sink keeps draining
        # whatever is still buffered so the tail of the audio plays out.
        self._finish_stream_playback()
        self._set_busy(False)

    def _on_error(self, message: str) -> None:
        if self._synthesis_listener is not None:
            # Listener-owned job failed/cancelled: same base reset (stop any
            # sink playback, engine not busy), then delegate — the listener
            # decides whether CANCELLED_MESSAGE is an error at all.
            self._stop_stream_playback_now()
            self._set_busy(False)
            self._synthesis_listener.on_synthesis_error(str(message))
            return
        if message == CANCELLED_MESSAGE:
            # User-initiated: stop playback immediately + reset silently and
            # notify for a toast — not an error banner (documented policy).
            # Bypasses _set_error, so modelsMissing is intentionally NOT
            # touched: a cancel is neither a new error nor a success signal.
            self._stop_stream_playback_now()
            self._set_busy(False)
            self.cancelled.emit()
            return
        self._stop_stream_playback_now()
        self._set_error(str(message))
        self._set_busy(False)

    def _on_stream_level(self, value: float) -> None:
        """Rolling peak envelope for the QML WaveformIndicator (FR-4.5)."""
        self._set_stream_level(value)

    def _on_voice_op_done(self, payload: dict[str, Any]) -> None:
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

    @Property(str, notify=defaultVoiceChanged)
    def defaultVoice(self) -> str:
        return self._settings.default_voice

    @defaultVoice.setter
    def defaultVoice(self, value: str) -> None:
        if not isinstance(value, str) or not value.strip():
            self._set_error("defaultVoice must be a non-empty string")
            return
        self._set_setting("default_voice", value)

    @Property(str, notify=outputDirChanged)
    def outputDir(self) -> str:
        return self._settings.output_dir

    @outputDir.setter
    def outputDir(self, value: str) -> None:
        if not isinstance(value, str):
            self._set_error("outputDir must be a string")
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
            self._set_error("temperature must be a number between 0.05 and 2.0")
            return
        self._set_setting("temperature", float(value))

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
            ("default_voice", self.defaultVoiceChanged),
            ("output_dir", self.outputDirChanged),
            ("temperature", self.temperatureChanged),
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
