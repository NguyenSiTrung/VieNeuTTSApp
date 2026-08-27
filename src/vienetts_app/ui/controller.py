"""AppController: QML-facing application state (FR-3.1, FR-3.4, FR-3.5).

Registered by app.py as the QML context property ``controller``. Owns the
voice catalog (built model-free from the SDK asset JSON), synthesis jobs,
voice-management jobs, and the Settings seam. EVERY dependency is injectable
(data_dir, engine factory, worker factory, catalog function) — and
construction must never initialize the engine or start the worker (NFR-3.1:
no model load at startup; the worker is lazily created on first submission).

Playback is deliberately NOT here — it lives in a separate Phase 4 module.

Cancellation UX: the worker reports a user cancel as ``error("Cancelled by
user")``. The controller treats that message specially: busy is reset and
``errorText`` stays empty, with a transient ``cancelled()`` signal QML can
toast. Documented choice: silent reset + notification, no scary error banner.

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
    backend / precision / defaultVoice / outputDir / temperature / theme —
                      NOTIFY-backed settings mirrors; invalid writes are
                      ignored with errorText feedback (never a crash)
    generate(text, voice) @Slot(str, str)
    cancel() @Slot()
    exportWav(path) @Slot(str) -> bool
    addVoice(name, clip_path, denoise) @Slot(str, str, bool)
    removeVoice(name) @Slot(str)
    denoisePreview(clip_path) @Slot(str)
    refreshVoices() @Slot()
    shutdown() @Slot()
    acknowledgeConsent() @Slot()
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import Property, QObject, Signal, Slot

from vienetts_app.core.audio import write_wav_file
from vienetts_app.core.engine import (
    TTSEngine,
    preset_voices,
    saved_voice_names,
)
from vienetts_app.core.importers import DocumentImportError, import_document
from vienetts_app.core.models import TTSRequest, VoiceOp
from vienetts_app.core.settings import load_settings, save_settings
from vienetts_app.workers.inference_worker import CANCELLED_MESSAGE, InferenceWorker

logger = logging.getLogger(__name__)

CONSENT_FILENAME = "cloning_consent.json"
PREVIEW_FILENAME = "preview.wav"
EXPORT_PATTERN = "vienetts_%Y%m%d_%H%M%S.wav"
SAMPLE_RATE = 48_000  # synthesis audio (infer/infer_stream); denoise is 44.1 kHz

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

        self._settings = load_settings(self._data_dir)
        self._worker: InferenceWorker | Any | None = None
        self._engine: TTSEngine | Any | None = None

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

    def _set_busy(self, value: bool) -> None:
        if value != self._busy:
            self._busy = value
            self.busyChanged.emit()

    def _set_error(self, message: str) -> None:
        if message != self._error_text:
            self._error_text = message
            self.errorTextChanged.emit()

    # ── synthesis ────────────────────────────────────────────────────────────

    @Slot(str, str)
    def generate(self, text: str, voice: str) -> None:
        """Submit a synthesis job; blank text is a no-op (FR-3.x)."""
        if not text or not text.strip():
            return
        worker = self._ensure_worker()
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
        self._has_audio = False
        self._audio = None
        self.hasAudioChanged.emit()
        self._set_error("")
        self._set_busy(True)
        worker.submit(request)

    @Slot()
    def cancel(self) -> None:
        if self._worker is not None:
            self._worker.cancel()

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
            self._set_error(f"Không tìm thấy tệp: {exc}")
        except DocumentImportError as exc:
            self._set_error(str(exc))
        except Exception as exc:  # noqa: BLE001 - import must never crash the UI
            self._set_error(f"Lỗi nhập tệp: {exc}")
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
        worker.done.connect(self._on_done)
        worker.error.connect(self._on_error)
        worker.voice_op_done.connect(self._on_voice_op_done)

    @Slot()
    def shutdown(self) -> None:
        """Stop the worker and close the engine; safe to call any time."""
        if self._worker is not None:
            try:
                self._worker.stop()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.exception("error stopping inference worker")
            self._worker = None
        if self._engine is not None:
            engine = self._engine
            self._engine = None
            try:
                engine.close()
            except Exception:  # noqa: BLE001
                logger.exception("error closing engine")
        self._set_busy(False)
        if self._needs_restart:
            self._needs_restart = False
            self.needsRestartChanged.emit()

    # ── worker signal handlers (queued to the main thread) ──────────────────

    def _on_progress(self, payload: Any) -> None:
        total = getattr(payload, "total", 0)
        done = getattr(payload, "done", 0)
        fraction = (done / total) if total > 0 else 0.0
        if fraction != self._progress:
            self._progress = fraction
            self.progressChanged.emit()

    def _on_done(self, audio: Any) -> None:
        self._audio = np.asarray(audio)
        self._has_audio = True
        self.hasAudioChanged.emit()
        if self._progress != 1.0:
            self._progress = 1.0
            self.progressChanged.emit()
        self._set_busy(False)

    def _on_error(self, message: str) -> None:
        if message == CANCELLED_MESSAGE:
            # User-initiated: reset silently and notify for a toast — not an
            # error banner (documented controller policy).
            self._set_busy(False)
            self.cancelled.emit()
            return
        self._set_error(str(message))
        self._set_busy(False)

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
        self._set_error("")
        for name, signal in (
            ("backend", self.backendChanged),
            ("precision", self.precisionChanged),
            ("default_voice", self.defaultVoiceChanged),
            ("output_dir", self.outputDirChanged),
            ("temperature", self.temperatureChanged),
            ("theme", self.themeChanged),
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
