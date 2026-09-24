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

    submit_stream_for_listener(text, voice, listener, *, kind="requested_chapter",
                               context=None) -> str | None
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
import hashlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
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

from vienetts_app.core import (
    engine_profiles,
    qwen_model_manager,
    qwen_model_manifest,
    qwen_variants,
)
from vienetts_app.core import qwen_gguf_model_manifest as qwen_gguf_models_manifest
from vienetts_app.core import qwen_gguf_models as qwen_gguf_model_store
from vienetts_app.core import qwen_gguf_runtime_manifest as qwen_gguf_manifest
from vienetts_app.core import qwen_runtime_manifest as qwen_manifest
from vienetts_app.core.artifacts import InteractiveArtifactStore, SynthesisArtifact
from vienetts_app.core.audio import compute_waveform_envelope_from_wav, read_wav, write_wav_file
from vienetts_app.core.audiobook import CHAPTER_CHAR_LIMIT
from vienetts_app.core.cuda_runtime import (
    CudaRuntimeLocation,
    CudaRuntimeManager,
    CudaRuntimeStatus,
    LocalCudaRuntime,
    discover_local_cuda_runtimes,
)
from vienetts_app.core.cuda_runtime_manifest import manifest_for_platform
from vienetts_app.core.detector import (
    CudaDriverProbe,
    HardwareInfo,
    TorchProbe,
    detect_hardware,
    detected_engine_info,
    probe_cuda_driver,
    probe_torch,
)
from vienetts_app.core.engine import (
    EngineProviders,
    TTSEngine,
    is_models_missing,
    preset_voices,
    resolve_model_source,
    saved_voice_names,
)
from vienetts_app.core.engine_profiles import EngineId
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
from vienetts_app.core.paths import (
    is_empty_path,
    normalize_local_path,
    path_to_file_url,
    sanitize_filename,
)
from vienetts_app.core.pcm_transport import BoundedPcmTransport
from vienetts_app.core.performance import PerformanceRecorder
from vienetts_app.core.settings import load_settings, save_settings
from vienetts_app.core.synthesis_context import (
    GenerationSettings,
    SynthesisContext,
    context_for,
    same_engine,
)
from vienetts_app.core.text_metrics import count_words, estimate_duration_seconds
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


def _default_cuda_runtime_manager(data_dir: Path) -> CudaRuntimeManager | None:
    """Build a platform-pinned manager without inspecting or downloading it."""
    manifest = manifest_for_platform(current_platform_key())
    if manifest is None:
        return None
    return CudaRuntimeManager(Path(data_dir) / "runtime" / "cuda", manifest)


def _default_qwen_model_manager(data_dir: Path, profile_key: str) -> Any:
    """One Qwen profile's install inside the shared Qwen model root."""
    from vienetts_app.core.qwen_model_manager import QwenModelManager

    return QwenModelManager(Path(data_dir) / "qwen" / "models", profile_key)


def _default_qwen_runtime_manager(data_dir: Path) -> Any | None:
    """Build a platform-pinned Qwen runtime manager without inspecting it.

    Kept for callers that only need the host's default variant; the controller
    builds its own device-pinned manager (``_build_default_qwen_runtime_manager``)
    because ``Settings.qwen_device`` decides WHICH pinned variant is installed.
    """
    manifest = qwen_manifest.manifest_for_platform(qwen_manifest.host_platform_key("cpu") or "")
    if manifest is None:
        return None
    from vienetts_app.core.qwen_runtime import QwenRuntimeManager

    return QwenRuntimeManager(Path(data_dir) / "qwen" / "runtime", manifest)


def _qwen_model_profile(key: str) -> Any | None:
    """Pinned manifest entry for one Qwen checkpoint (``None`` = unknown)."""
    manifest = qwen_model_manifest.MANIFEST
    return manifest.profile_for(key) if manifest is not None else None


def _qwen_shared_model_bytes() -> int:
    """Bytes of tokenizer content both Qwen checkpoints reuse.

    The shared files are pinned byte-identical across profiles, so the tree is
    one download no matter how many checkpoints are installed; the largest
    pinned set is the honest size of it.
    """
    sizes = [
        profile.shared_bytes
        for key in qwen_model_manifest.PROFILE_KEYS
        if (profile := _qwen_model_profile(key)) is not None
    ]
    return max(sizes, default=0)


def _qwen_runtime_manager_for_key(key: str, data_dir: Path) -> Any | None:
    """Manager for one pinned runtime key (usable from a background lane)."""
    manifest = qwen_manifest.manifest_for_platform(key)
    if manifest is None:
        return None
    from vienetts_app.core.qwen_runtime import QwenRuntimeManager

    return QwenRuntimeManager(Path(data_dir) / "qwen" / "runtime", manifest)


def _default_clone_store(data_dir: Path) -> Any:
    """The profile-scoped clone catalog (Task 4.1) for this app data dir."""
    from vienetts_app.core.voice_profiles import CloneStore

    return CloneStore(Path(data_dir) / "clones")


def _default_qwen_engine_factory(**kwargs: Any) -> Any:
    """Build the isolated-host engine object (never spawns or loads here)."""
    from vienetts_app.core.qwen_engine import QwenEngine

    return QwenEngine(**kwargs)


def _default_qwen_gguf_runtime_manager(data_dir: Path, cell: str) -> Any | None:
    """The managed qwentts.cpp pack manager for one platform cell."""
    manifest = qwen_gguf_manifest.manifest_for_cell(cell)
    if manifest is None:
        return None
    from vienetts_app.core.qwen_gguf_runtime import QwenGgufRuntimeManager

    return QwenGgufRuntimeManager(Path(data_dir) / "qwen" / "gguf-runtime", manifest)


def _default_qwen_gguf_model_manager(data_dir: Path, variant: Any) -> Any:
    """One Qwen variant's GGUF install inside the shared GGUF model root."""
    from vienetts_app.core.qwen_gguf_models import QwenGgufModelManager

    return QwenGgufModelManager(Path(data_dir) / "qwen" / "gguf-models", variant)


def _default_qwen_gguf_engine_factory(**kwargs: Any) -> Any:
    """Build the managed-native engine object (never spawns or loads here)."""
    from vienetts_app.core.qwen_gguf_engine import QwenGgufEngine

    return QwenGgufEngine(**kwargs)


@dataclass(frozen=True)
class ProfileReadiness:
    """Normalized model/runtime readiness for one engine profile (Task 5.1).

    The two managers behind it disagree on shape (``ModelStatus`` vs
    ``QwenModelStatus``/``QwenRuntimeStatus``) and the VieNeu engine has no
    managed runtime at all, so the controller normalizes them into this one
    value the UI can bind to. ``location`` stays the manager's own object.
    """

    state: str = "checking"
    ready: bool = False
    installed_bytes: int = 0
    required_bytes: int = 0
    progress: float = 0.0
    error: str = ""
    location: Any | None = None


def resolve_profile_device(profile: EngineId, hardware: HardwareInfo, qwen_device: str) -> str:
    """The device ``profile`` will actually run on (truthful, best-effort).

    VieNeu follows the same capability readout the Settings page shows (CUDA
    only when torch/the managed runtime can serve it). Qwen honors its own
    setting; ``auto`` resolves the way the model host picks — CUDA when the
    runtime is usable, MPS on Apple silicon, else CPU. The host re-resolves at
    load time, so this is the pre-load truth, not a promise.
    """
    if profile != engine_profiles.VIENEU:
        if qwen_device != "auto":
            return qwen_device
        if hardware.kind == "nvidia" and hardware.torch_installed:
            return "cuda"
        if hardware.kind == "apple_silicon":
            return "mps"
        return "cpu"
    return detected_engine_info(hardware).device


def _resolve_gguf_device(hardware: HardwareInfo, preference: str) -> str:
    """The ggml device a GGUF ``auto`` choice resolves to on this host.

    ``installable_devices`` is already best-first and only names devices
    whose cell actually ships a pack — a matrix entry without a published
    manifest never wins (e.g. CUDA on a host whose cell ships no pack
    resolves CPU, never a runtime that cannot be installed). Accelerators
    still require their hardware; ``cpu`` needs none and is the fallback.
    """
    if preference != "auto":
        return preference
    for device in qwen_gguf_manifest.installable_devices():
        if device == "cuda" and hardware.kind != "nvidia":
            continue
        if device == "metal" and hardware.kind != "apple_silicon":
            continue
        return device
    return "cpu"


logger = logging.getLogger(__name__)


def _unwrap_bg_result(result: Any) -> Any:
    """Unwrap a bg_ops outcome envelope when one arrives raw.

    The pool bridge normally unwraps ``(ok, payload)`` before calling
    ``on_done`` — but a runner from before the envelope change (or a test
    double calling ``done`` directly) may hand the envelope through. Payloads
    the app itself produces never start with a bool (export/import tuples
    lead with a path/text, generations are ints), so a leading bool safely
    marks an envelope.
    """
    if isinstance(result, tuple) and len(result) == 2 and isinstance(result[0], bool):
        return result[1]
    return result


def _readiness_from(status: Any) -> ProfileReadiness:
    """Normalize a manager's status object into :class:`ProfileReadiness`.

    ``ModelStatus``, ``QwenModelStatus`` and ``QwenRuntimeStatus`` all carry
    the same readiness fields with different annotations (and a different
    ``location`` type), so the profile view reads them structurally and derives
    ``ready`` from the one success state they share.
    """
    state = str(getattr(status, "state", "") or "checking")
    return ProfileReadiness(
        state=state,
        ready=state == "ready",
        installed_bytes=int(getattr(status, "installed_bytes", 0) or 0),
        required_bytes=int(getattr(status, "required_bytes", 0) or 0),
        progress=float(getattr(status, "progress", 0.0) or 0.0),
        error=str(getattr(status, "error", "") or ""),
        location=getattr(status, "location", None),
    )


def _inspect_readiness(manager: Any) -> ProfileReadiness:
    """Inspect one managed install, turning a raised failure into a state.

    The Qwen managers raise on a corrupt or unreadable install instead of
    returning a status; a profile must never stay stuck on ``checking``
    because of that, so the failure becomes the state the UI reports.
    """
    try:
        return _readiness_from(manager.inspect())
    except Exception as exc:  # noqa: BLE001 - the UI needs a state, not a traceback
        logger.warning("profile inspection failed: %s", exc)
        return ProfileReadiness(state="failed", error=str(exc))


CONSENT_FILENAME = "cloning_consent.json"
PREVIEW_FILENAME = "preview.wav"
EXPORT_PATTERN = "vienetts_%Y%m%d_%H%M%S.wav"
SAMPLE_RATE = 48_000  # synthesis audio (infer/infer_stream); denoise is 44.1 kHz
# Voice-preset audition sample (VoicePicker pre-listen): ONE fixed sentence
# per synthesis language, short enough to synthesize in ~seconds on CPU. The
# preview must be spoken in the language the engine will actually render — a
# Vietnamese sentence under a Qwen profile (which has no Vietnamese) or under
# VieNeu's English selection produced a preview in the wrong language. A
# fixed per-language text keeps back-to-back voice compares fair and the disk
# cache key small; the current editor text is deliberately NOT used (it would
# couple the picker popup to tab state and make cache keys unbounded).
AUDITION_SAMPLE_TEXTS: dict[str, str] = {
    "vi": "Xin chào, đây là giọng đọc mẫu của VieNeu TTS.",
    "en": "Hello, this is a sample voice from VieNeu TTS.",
    "zh": "你好，这是 VieNeu TTS 的示例声音。",
    "ja": "こんにちは、これは VieNeu TTS のサンプル音声です。",
    "ko": "안녕하세요, 이것은 VieNeu TTS의 샘플 목소리입니다.",
    "de": "Hallo, dies ist eine Beispielstimme von VieNeu TTS.",
    "fr": "Bonjour, ceci est une voix d'exemple de VieNeu TTS.",
    "ru": "Здравствуйте, это образец голоса VieNeu TTS.",
    "pt": "Olá, esta é uma voz de exemplo do VieNeu TTS.",
    "es": "Hola, esta es una voz de muestra de VieNeu TTS.",
    "it": "Ciao, questa è una voce di esempio di VieNeu TTS.",
}
AUDITION_CACHE_DIRNAME = "auditions"


def audition_sample_text(language: str) -> str:
    """The fixed audition sentence for a resolved synthesis language.

    ``""`` is VieNeu's unset default (Vietnamese-first); Qwen's ``auto`` and
    any code without a curated sentence fall back to English, which every
    profile renders — the model reads the language off the text itself.
    """
    code = (language or "").strip().lower()
    if not code or code == "vi":
        return AUDITION_SAMPLE_TEXTS["vi"]
    return AUDITION_SAMPLE_TEXTS.get(code) or AUDITION_SAMPLE_TEXTS["en"]


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
    exportFormatChanged = Signal()
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
    replayPausedChanged = Signal()
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
    # Engine profiles (Task 5.1): the active profile, its capability catalog,
    # its resolved device, and the model/runtime readiness the UI gates on.
    engineProfileChanged = Signal()
    engineProfilesChanged = Signal()
    engineDeviceChanged = Signal()
    profileCatalogChanged = Signal()
    profileModelChanged = Signal()
    profileRuntimeChanged = Signal()
    profileReadyChanged = Signal()
    synthesisLanguageChanged = Signal()
    # Managed CUDA runtime: explicit installation / diagnostics only.
    cudaRuntimeStateChanged = Signal()
    cudaRuntimeProgressChanged = Signal()
    cudaRuntimeStorageChanged = Signal()
    cudaRuntimeErrorChanged = Signal()
    cudaRuntimeSupportedChanged = Signal()
    cudaRuntimeDriverChanged = Signal()
    localCudaRuntimesChanged = Signal()
    _cuda_runtime_status_signal = Signal(object)
    # Managed Qwen runtime + models (Task 6.1): the runtime is ONE platform+
    # device-pinned install shared by both Qwen profiles; each profile has its
    # own model install under one shared model root. The rows carry state,
    # storage, error and progress, so one signal per axis keeps a download
    # progress tick from re-rendering the storage rows.
    qwenRuntimeStateChanged = Signal()
    qwenRuntimeProgressChanged = Signal()
    qwenRuntimeStorageChanged = Signal()
    qwenRuntimeErrorChanged = Signal()
    qwenRuntimeSupportChanged = Signal()
    qwenModelsChanged = Signal()
    qwenDeviceChanged = Signal()
    # The selected (format, quantization) pair — model rows, runtime card,
    # device picker and the engine a submission builds all follow it.
    qwenVariantChanged = Signal()
    _qwen_status_signal = Signal(object)
    # Foreground synthesis job (Phase 2 Task 3): QML binds action state here,
    # never to the worker's global queue.
    foregroundJobIdChanged = Signal()
    foregroundJobStateChanged = Signal()
    # Voice-preset audition (VoicePicker pre-listen): a non-busy sample lane
    # that streams audition_sample_text() for the resolved language in a
    # hovered/selected voice. QML binds the per-row play/stop icon + spinner
    # here, never to busy (an audition must not dim the generate/export
    # surface).
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
    # Mini studio (post-synthesis polish + single-segment re-gen).
    studioProjectChanged = Signal()
    studioEnvelopeChanged = Signal()
    studioControlsChanged = Signal()
    studioBusyChanged = Signal()
    # Per-clip audition: which clip is sounding ("" = the master mix is the
    # thing being replayed, or nothing is). Kept separate from the project so
    # the transport dock can switch its waveform + timecode without rebinding
    # the whole clip list on every audition start/stop.
    studioAuditionChanged = Signal()
    # Profile a refused Studio re-synthesis needs ("" = none pending): the
    # clip's audio came from another engine, so the UI offers the switch
    # instead of the app silently re-synthesizing with the active one.
    studioRegenProfileChanged = Signal()

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
        bg_runner: Callable[..., None] | None = None,
        model_manager_factory: Callable[[Path], Any] | None = None,
        update_checker: Callable[..., UpdateInfo] | None = None,
        app_version: str | None = None,
        update_platform_key: str | None = None,
        torch_probe: Callable[[], TorchProbe] | None = None,
        cuda_runtime_manager_factory: Callable[[Path], Any | None] | None = None,
        local_cuda_discovery: Callable[[], list[LocalCudaRuntime]] | None = None,
        cuda_driver_probe: Callable[[], CudaDriverProbe] | None = None,
        qwen_model_manager_factory: Callable[[Path, str], Any] | None = None,
        qwen_runtime_manager_factory: Callable[[Path], Any | None] | None = None,
        qwen_engine_factory: Callable[..., Any] | None = None,
        qwen_gguf_runtime_manager_factory: Callable[[Path, str], Any | None] | None = None,
        qwen_gguf_model_manager_factory: Callable[[Path, Any], Any] | None = None,
        qwen_gguf_engine_factory: Callable[..., Any] | None = None,
        clone_store_factory: Callable[[Path], Any] | None = None,
        hardware_probe: Callable[..., HardwareInfo] | None = None,
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
        # Torch/CUDA availability (backend truthfulness): injectable probe,
        # evaluated LAZYLY on first property read — construction never imports
        # torch (NFR-2.1). CPU-only packaged builds report False so the UI can
        # disable the CUDA backend option instead of silently falling back.
        self._torch_probe = torch_probe or probe_torch
        self._torch_available: bool | None = None
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
        # Mini-studio project (clips + op stack; None = nothing opened).
        self._studio_project: Any | None = None
        self._studio_regen_clip_id: str | None = None
        self._studio_regen_clip_text: str | None = None
        # Engine identity of the re-synthesis in flight: spliced onto the clip
        # when it lands, so a clip's provenance always names its actual engine.
        self._studio_regen_context: SynthesisContext | None = None
        # Profile a refused re-synthesis needs ("" = none pending) — the target
        # of the switch action the UI offers instead of a silent substitution.
        self._studio_regen_profile: str = ""
        # The clip's own recorded context behind that offer: the switch must
        # restore profile AND (model format, quantization) — a same-profile
        # variant mismatch is refused exactly like a cross-profile one.
        self._studio_regen_required: SynthesisContext | None = None
        self._studio_duration_ms: int = 0
        self._studio_envelope: list[float] = []
        # Overview render (mix + duration + envelope) runs off the GUI thread:
        # _studio_busy while a render is in flight, _studio_seq drops stale
        # results when ops stack up faster than renders finish.
        self._studio_busy: bool = False
        self._studio_seq: int = 0
        self._studio_pending: dict[int, str] = {}
        self._studio_preview_path = ""
        # Clip audition (see studioAuditionChanged): the dock shows THIS clip's
        # envelope + length instead of the master mix, so the waveform, the
        # timecode and the highlighted row always describe the same audio.
        self._studio_clip_playing_id: str = ""
        self._studio_clip_duration_ms: int = 0
        self._studio_clip_envelope: list[float] = []
        self._artifact_store = InteractiveArtifactStore(self._data_dir)
        self._current_artifact: SynthesisArtifact | None = None
        # Engine identity that produced the CURRENT artifact (and the
        # foreground job's frozen identity while it runs). Studio stamps it on
        # every clip it loads from an artifact, so a clip's provenance names
        # the engine that actually produced its audio.
        self._current_artifact_context: SynthesisContext | None = None
        self._foreground_context: SynthesisContext | None = None
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
        self._replay_paused = False
        self._replay_artifact: SynthesisArtifact | None = None
        # PlaybackWaveform state: overview of the committed artifact + live playhead.
        self._waveform_envelope: list[float] = []
        self._replay_position = 0.0
        self._replay_base_ms = 0
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
        # ── engine profiles (Task 5.1) ───────────────────────────────────────
        # The ACTIVE profile comes from settings (unknown ids are clamped to
        # VieNeu at load, so migration is the settings module's job). Nothing
        # here inspects a model: refreshProfileState() owns the first
        # filesystem inspect and runs off the GUI thread after first paint.
        self._active_profile: EngineId = self._settings.engine_profile
        self._qwen_model_manager_factory = qwen_model_manager_factory or _default_qwen_model_manager
        # The DEFAULT runtime factory is a bound method, not the module-level
        # helper: the pinned variant follows Settings.qwen_device, which only
        # the controller knows. Injected factories keep their (data_dir) shape.
        # Whether one WAS injected is recorded here, not re-tested later: a
        # fresh read of a bound method is never the same object as the stored
        # one, so `is` would call the default an injected factory and hand it
        # the device key this controller has not resolved yet (see
        # _start_qwen_inspection).
        self._qwen_runtime_factory_injected = qwen_runtime_manager_factory is not None
        self._qwen_runtime_manager_factory = (
            qwen_runtime_manager_factory or self._build_default_qwen_runtime_manager
        )
        self._qwen_engine_factory = qwen_engine_factory or _default_qwen_engine_factory
        self._qwen_gguf_runtime_manager_factory = (
            qwen_gguf_runtime_manager_factory or _default_qwen_gguf_runtime_manager
        )
        self._qwen_gguf_model_manager_factory = (
            qwen_gguf_model_manager_factory or _default_qwen_gguf_model_manager
        )
        self._qwen_gguf_engine_factory = (
            qwen_gguf_engine_factory or _default_qwen_gguf_engine_factory
        )
        self._clone_store_factory = clone_store_factory or _default_clone_store
        self._clone_store: Any | None = None
        self._hardware_probe = hardware_probe or detect_hardware
        # Last probed hardware, cached for the device cards: the probe shells
        # out to nvidia-smi, so it only runs on a background lane.
        self._hardware_status: HardwareInfo | None = None
        self._profile_generation = 0
        self._profile_device = ""
        self._profile_model_status = ProfileReadiness()
        # The in-process VieNeu engine needs no managed runtime; only a Qwen
        # profile can be "not ready" on this axis.
        self._profile_runtime_status = ProfileReadiness(
            state="ready" if self._active_profile == engine_profiles.VIENEU else "checking",
            ready=self._active_profile == engine_profiles.VIENEU,
        )
        # Managed CUDA setup is deliberately construction-only: the factory
        # selects the platform manifest but never inspects disk, downloads, or
        # imports torch. run_gui schedules the first inspect after first paint.
        cuda_factory = cuda_runtime_manager_factory or _default_cuda_runtime_manager
        self._cuda_runtime_manager = cuda_factory(self._data_dir)
        self._cuda_runtime_supported = self._cuda_runtime_manager is not None
        self._cuda_driver_probe = cuda_driver_probe or probe_cuda_driver
        self._cuda_runtime_driver_checked = False
        self._cuda_runtime_driver_ready = False
        self._cuda_runtime_driver_version: str | None = None
        self._cuda_runtime_status = (
            CudaRuntimeStatus("checking")
            if self._cuda_runtime_supported
            else self._unsupported_cuda_status()
        )
        self._cuda_runtime_generation = 0
        self._cuda_runtime_cancel = threading.Event()
        self._cuda_runtime_installing = False
        # Manager calls share one serialized lane: inspect, install, and
        # removal all read/write the same versioned runtime directory.
        self._cuda_runtime_operation: str | None = None
        # Native libraries cannot be unloaded safely. This latches after
        # observing an engine activation and deliberately outlives engine
        # teardown/reconfiguration until the process exits.
        self._cuda_runtime_activated = False
        self._local_cuda_discovery = local_cuda_discovery or discover_local_cuda_runtimes
        self._local_cuda_runtimes: list[dict[str, object]] = []
        self._local_cuda_generation = 0
        self._cuda_runtime_status_signal.connect(self._on_cuda_runtime_status_signal)
        # ── managed Qwen runtime + models (Task 6.1) ─────────────────────────
        # The runtime key is the pinned manifest for this host + the chosen
        # device; an explicit device resolves it here, ``auto`` resolves during
        # the post-paint inspection (a probe must not run on the GUI thread).
        # Statuses stay raw manager objects; the normalized readiness the
        # profile view gates on is published from them.
        self._qwen_runtime_key = ""
        self._qwen_runtime_status: Any | None = None
        self._qwen_model_statuses: dict[str, Any] = {}
        self._qwen_generation = 0
        self._qwen_cancel = threading.Event()
        # One operation at a time across the runtime and both models: the
        # installers write the same shared Qwen root.
        self._qwen_operation: str | None = None
        self._qwen_models_busy = ""
        self._qwen_resolved_device = ""
        # The device preference is per-format: ``qwen_device`` speaks PyTorch
        # names, ``qwen_gguf_device`` speaks ggml names, and switching formats
        # must never overwrite the inactive one. An explicit pick resolves the
        # runtime key now; ``auto`` waits for the post-paint inspection (a
        # probe must not run on the GUI thread).
        pref = self._qwen_device_pref()
        if pref != "auto":
            self._qwen_runtime_key = self._qwen_key_for("", pref)
            self._qwen_resolved_device = pref
        self._qwen_status_signal.connect(self._on_qwen_status_signal)
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
        # The context the in-flight audition was submitted under: the cache
        # write at completion keys by THIS identity, so a settings change
        # mid-render can never file audio under a selection it was not
        # rendered for.
        self._audition_context: SynthesisContext | None = None
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
        # The profile-scoped catalog moves with it: an enrollment lands in the
        # clone store (or the SDK registry), and the shared picker's groups and
        # the Cloning tab's list are both fed by profileVoices/profileClones.
        self.profileCatalogChanged.emit()

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
            gen, info = _unwrap_bg_result(result)
            if gen != self._update_generation:
                return
            self._set_update_checking(False)
            self._publish_update_info(info, announce_errors=announce_errors)

        def on_error(exc: BaseException) -> None:
            if generation != self._update_generation:
                return
            self._set_update_checking(False)
            logger.warning("update check failed: %s", exc)
            if announce_errors:
                self._set_error(self.tr("Không kiểm tra được bản cập nhật: {}").format(exc))

        self._run_bg(work, on_done, self, on_error=on_error)

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

    # Byte counts are qlonglong, never `int`: Qt's int is 32-bit and a
    # >2 GiB size makes the QML property read raise OverflowError.
    @Property("qlonglong", notify=modelStorageChanged)
    def modelInstalledBytes(self) -> int:
        return int(self._model_status.installed_bytes)

    @Property("qlonglong", notify=modelStorageChanged)
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

    # ── engine profiles (Task 5.1) ───────────────────────────────────────────

    @Property(str, notify=engineProfileChanged)
    def engineProfile(self) -> str:
        """The ACTIVE engine profile (VieNeu, Qwen CustomVoice, Qwen Base)."""
        return str(self._active_profile)

    @Property(str, notify=engineProfileChanged)
    def engineProfileLabel(self) -> str:
        """Human label for the active profile (native model name)."""
        return engine_profiles.get_capabilities(self._active_profile).label

    @Property(bool, notify=engineProfileChanged)
    def engineProfileIsQwen(self) -> bool:
        """True while a Qwen profile is active — Settings' engine cards bind this.

        Cards that only make sense under one engine family (Qwen device/runtime/
        models vs. VieNeu backend/model-source/CUDA) show and hide on this
        single boolean instead of each card re-deriving it from the profile id.
        """
        return engine_profiles.is_qwen_profile(self._active_profile)

    @Property(str, notify=synthesisLanguageChanged)
    def synthesisLanguage(self) -> str:
        """The language every submission snapshots (the RESOLVED code).

        Empty means the active profile takes no language argument at all
        (VieNeu's Vietnamese-first SDK); otherwise this is the code a
        submission would use right now — the stored choice, or the profile's
        own default (``auto`` for the Qwen profiles) when the user has not
        chosen one. A picker binds to this directly.
        """
        return self._effective_language()

    @Slot(str, result=bool)
    def setSynthesisLanguage(self, language: str) -> bool:
        """Choose the synthesis language for the ACTIVE profile.

        ``""`` resets to the profile default. A code the profile does not
        support is refused with the capability table's own reason and nothing
        is persisted — the stored value is always something a submission can
        use. The stored code is profile-scoped: switching profiles drops one the
        incoming profile cannot serve (``switchEngineProfile``).
        """
        code = str(language or "").strip()
        caps = engine_profiles.get_capabilities(self._active_profile)
        supported = [option.code for option in caps.languages]
        if code and code not in supported:
            self._set_error(
                self.tr("{} không hỗ trợ ngôn ngữ {} — chọn một trong: {}").format(
                    caps.label, code, ", ".join(supported)
                )
            )
            return False
        if code != self._settings.synthesis_language:
            self._settings = replace(self._settings, synthesis_language=code)
            try:
                save_settings(self._settings, self._data_dir)
            except OSError as exc:  # noqa: BLE001 - the live choice still applies
                self._set_error(self.tr("Không thể lưu cài đặt: {}").format(exc))
            else:
                self._set_error("")
            self.synthesisLanguageChanged.emit()
        return True

    def _effective_language(self) -> str:
        """The code a submission uses: the stored one, else the profile default."""
        stored = str(self._settings.synthesis_language or "").strip()
        if stored:
            return stored
        return engine_profiles.default_language(self._active_profile)

    def _generation_settings(self) -> GenerationSettings:
        """The waveform-affecting settings this profile actually applies.

        A control the profile does not declare is recorded as ``None`` (engine
        default) rather than as a user value the engine ignores, so a fingerprint
        never varies with a setting that cannot change the audio.
        """
        controls = set(engine_profiles.get_capabilities(self._active_profile).generation_controls)
        settings = self._settings
        return GenerationSettings(
            temperature=settings.temperature if "temperature" in controls else None,
            speed=settings.speed if "speed" in controls else None,
            silence_p=settings.silence_p if "silence_p" in controls else None,
        )

    def submission_context_for(self, voice: str, *, report: bool = True) -> SynthesisContext | None:
        """The immutable engine context for a submission (``None`` = refused).

        This is the one gate every synthesis submission passes through: the
        active profile, its resolved language, the selected preset voice or
        enrolled clone, and the generation settings are validated against the
        capability table BEFORE a job exists, and the frozen result is what the
        request carries. The worker resolves its provider from it, and caches
        and Studio compare it — nothing re-derives it at render time, so a
        settings change can never alter a job that is already queued.

        Refusals are actionable and localized: an unsupported combination is
        reported here instead of being silently replaced by another engine.
        ``report=False`` answers the same question WITHOUT touching
        ``errorText`` — the read-only probe a cache check uses to decide
        whether a stored render is still compatible, where a refusal is not yet
        an error the user asked for.
        """
        profile = self._active_profile
        voice_id, clone_id = self._voice_selection(voice)
        # The selected model format resolves to one engine, and the context
        # stamps it: the worker's provider routing refuses a context whose
        # engine the active provider does not serve — a GGUF job can never be
        # silently rendered by PyTorch, nor the reverse. Install gaps surface
        # at engine build time, the same place the official variant reports
        # them.
        variant = qwen_variants.resolve_variant(self._settings)
        # The stamped device follows the variant's own selection field: the
        # PyTorch host reads ``qwen_device``, the qwentts.cpp host reads
        # ``qwen_gguf_device`` (native ggml names; "auto" normalizes away).
        resolved_device = ""
        runtime_identity = model_identity = tokenizer_identity = ""
        if variant is not None:
            if variant.model_format == qwen_variants.MODEL_FORMAT_GGUF:
                # The stamp names the device the engine will actually run on:
                # an explicit pick resolves directly, ``auto`` takes the
                # device the last inspection resolved — and a PyTorch-era
                # ``mps`` spelling never reaches the context (``metal``).
                (
                    resolved_device,
                    runtime_identity,
                    model_identity,
                    tokenizer_identity,
                ) = self._gguf_context_identities(variant)
            else:
                resolved_device = self._settings.qwen_device
        try:
            context = context_for(
                profile,
                language=self._effective_language(),
                voice_id=voice_id,
                clone_id=clone_id,
                generation=self._generation_settings(),
                model_repo=self._settings.model_repo,
                variant=variant,
                resolved_device=resolved_device,
                runtime_identity=runtime_identity,
                model_identity=model_identity,
                tokenizer_identity=tokenizer_identity,
            )
        except ValueError as exc:  # EngineProfileError is one; message is the reason
            if report:
                self._set_error(str(exc))
            return None
        return context

    def _gguf_context_identities(
        self, variant: qwen_variants.QwenVariant
    ) -> tuple[str, str, str, str]:
        """``(device, runtime, model, tokenizer)`` identity for a GGUF context.

        Model and codec identities come from the shipped model manifest —
        content-addressed pins, so two installs of the same files share them
        and a recipe change never does. The device is resolved the way
        :meth:`_build_qwen_gguf_engine` resolves it — an explicit
        ``qwen_gguf_device`` pick wins, ``auto`` takes the device the last
        inspection resolved, and the PyTorch-era ``mps`` spelling maps to
        ``metal`` — then keys the runtime cell whose pack recipe supplies the
        runtime identity. An unshipped cell leaves the identity empty:
        provenance records what is verified, never a guessed name.
        """
        recipe = qwen_gguf_models_manifest.recipe_for_variant(variant)
        model_identity = recipe.model_identity if recipe is not None else ""
        tokenizer_identity = recipe.tokenizer_identity if recipe is not None else ""
        device = str(self._settings.qwen_gguf_device or "auto")
        if device == "mps":  # PyTorch-era spelling never reaches the cell table
            device = "metal"
        if device == "auto" or device not in qwen_gguf_manifest.DEVICES:
            device = str(self._qwen_resolved_device or "")
            if device == "mps":
                device = "metal"
            if device not in qwen_gguf_manifest.DEVICES:
                device = ""
        cell = qwen_gguf_manifest.host_cell_key(device) if device else None
        pack = qwen_gguf_manifest.manifest_for_cell(cell) if cell else None
        return (
            device,
            pack.identity if pack is not None else "",
            model_identity,
            tokenizer_identity,
        )

    def _voice_selection(self, voice: str) -> tuple[str, str]:
        """Map a QML voice string to ``(voice_id, clone_id)`` for the active profile.

        A name or id of a clone enrolled for the ACTIVE Qwen profile becomes a
        clone id (Base renders enrolled clones only); everything else stays a
        voice id, so the capability table itself produces the refusal for an
        unknown speaker, a fixed speaker offered to Base, or a missing clone.
        VieNeu voices are always SDK registry names — presets and enrolled
        clones alike — and never become context clone ids.
        """
        picked = str(voice or "").strip()
        if not picked or not engine_profiles.is_qwen_profile(self._active_profile):
            return picked, ""
        for clone in self._profile_clone_rows():
            if picked in (clone["id"], clone["label"]):
                return "", str(clone["id"])
        return picked, ""

    @Property("QVariantList", notify=engineProfilesChanged)
    def engineProfiles(self) -> list[dict[str, Any]]:
        """Every selectable profile with the capabilities the UI gates on.

        This is the capability table itself (engine_profiles), not a UI copy:
        unsupported controls are hidden/disabled from these flags, and the
        profile's own clone requirements decide what the Cloning tab asks for.
        """
        entries: list[dict[str, Any]] = []
        for profile in engine_profiles.list_profiles():
            caps = engine_profiles.get_capabilities(profile)
            entries.append(
                {
                    "id": profile,
                    "label": caps.label,
                    "isDefault": caps.is_default,
                    "isActive": profile == self._active_profile,
                    "supportsCloning": caps.supports_cloning,
                    "supportsPresetVoices": caps.supports_preset_voices,
                    "supportsInstruction": caps.supports_instruction,
                    "supportsEmotionTags": caps.supports_emotion_tags,
                    "voicesSource": caps.voices_source,
                    "cloneRequirements": list(caps.clone_requirements),
                    "runtime": caps.runtime,
                    "devices": list(caps.devices),
                    "generationControls": list(caps.generation_controls),
                    "modelRepo": caps.model_repo,
                    "modelRevision": caps.model_revision,
                    "sourceSampleRate": caps.source_sample_rate,
                    "outputSampleRate": caps.output_sample_rate,
                    "streamingGranularity": caps.streaming_granularity,
                    "languageCount": len(caps.languages),
                    "voiceCount": len(caps.voices),
                }
            )
        return entries

    @Property("QVariantList", notify=profileCatalogChanged)
    def profileLanguages(self) -> list[dict[str, Any]]:
        """Languages the active profile accepts (``code`` is the job-level id)."""
        caps = engine_profiles.get_capabilities(self._active_profile)
        return [
            {
                "code": option.code,
                "label": option.label,
                "modelName": option.model_name,
                "isAuto": option.is_auto,
            }
            for option in caps.languages
        ]

    @Property("QVariantList", notify=profileCatalogChanged)
    def profileVoices(self) -> list[dict[str, Any]]:
        """Preset voices the active profile offers (empty for enrollment-only)."""
        caps = engine_profiles.get_capabilities(self._active_profile)
        return [
            {
                "id": voice.voice_id,
                "label": voice.label,
                "description": voice.description,
                "nativeLanguage": voice.native_language,
                "languages": list(voice.languages),
            }
            for voice in caps.voices
        ]

    @Property("QVariantList", notify=profileCatalogChanged)
    def profileClones(self) -> list[dict[str, Any]]:
        """Enrolled clones of the ACTIVE profile only (catalogs stay separate).

        VieNeu's catalog is its SDK voice registry (the app-owned persisted
        names); a Qwen profile's is the profile-scoped clone store, so a clone
        enrolled for one engine can never be offered for another.
        """
        return self._profile_clone_rows()

    def _profile_clone_rows(self) -> list[dict[str, Any]]:
        """The active profile's clones as UI rows (empty when it has none)."""
        if self._active_profile == engine_profiles.VIENEU:
            return [
                {"id": name, "label": name, "transcript": ""}
                for name in self._saved_names_fn(self._voices_dir)
            ]
        store = self._clone_store_instance()
        if store is None:
            return []
        return [
            {"id": clone.clone_id, "label": clone.name, "transcript": clone.transcript}
            for clone in store.list(profile=self._active_profile)
        ]

    @Property(str, notify=engineDeviceChanged)
    def engineDevice(self) -> str:
        """Resolved compute device for the active profile (``checking`` until
        the post-paint inspection lands; the model host re-resolves at load)."""
        return self._profile_device or "checking"

    @Property(str, notify=profileModelChanged)
    def profileModelState(self) -> str:
        """Active profile's model readiness (VieNeu mirrors its own status)."""
        return str(self._profile_model_status.state)

    @Property(bool, notify=profileModelChanged)
    def profileModelReady(self) -> bool:
        return bool(self._profile_model_status.ready)

    @Property(str, notify=profileModelChanged)
    def profileModelError(self) -> str:
        return str(self._profile_model_status.error)

    # Byte counts are qlonglong, never `int`: Qt's int is 32-bit and a
    # >2 GiB model install makes the QML property read raise OverflowError.
    @Property("qlonglong", notify=profileModelChanged)
    def profileModelInstalledBytes(self) -> int:
        return int(self._profile_model_status.installed_bytes)

    @Property("qlonglong", notify=profileModelChanged)
    def profileModelRequiredBytes(self) -> int:
        return int(self._profile_model_status.required_bytes)

    @Property(str, notify=profileRuntimeChanged)
    def profileRuntimeState(self) -> str:
        """Active profile's runtime readiness (VieNeu's is in-process: ready)."""
        return str(self._profile_runtime_status.state)

    @Property(bool, notify=profileRuntimeChanged)
    def profileRuntimeReady(self) -> bool:
        return bool(self._profile_runtime_status.ready)

    @Property(str, notify=profileRuntimeChanged)
    def profileRuntimeError(self) -> str:
        return str(self._profile_runtime_status.error)

    @Property(bool, notify=profileReadyChanged)
    def profileReady(self) -> bool:
        """Both axes ready — the only state a synthesis may start from."""
        return bool(self._profile_model_status.ready and self._profile_runtime_status.ready)

    @Slot(str, result=bool)
    def switchEngineProfile(self, profile: str) -> bool:
        """Activate ``profile``, shutting the current engine owner down FIRST.

        Refused while a job is running or queued (two engines must never own
        the process or the model footprint) and for unknown ids. The teardown
        runs before the new profile is persisted or activated, so a failed
        switch leaves no half-owned engine behind; the engine itself is rebuilt
        lazily by the next use, with the new profile's settings.
        """
        target = str(profile or "").strip()
        if target not in engine_profiles.list_profiles():
            self._set_error(self.tr("Hồ sơ engine không hợp lệ: {}").format(target or "(trống)"))
            return False
        if target == self._active_profile:
            self._disarm_satisfied_regen_offer()
            return True
        blockers = self._profile_switch_blockers()
        if blockers:
            self._set_error(
                self.tr("Không thể đổi engine khi đang xử lý: {}").format(", ".join(blockers))
            )
            return False
        # Shut the current owner down BEFORE activation: the old worker stops
        # (its queued work was already refused above) and its engine closes,
        # so the two profiles never hold a process or a model at once.
        self.shutdown()
        self._active_profile = target
        self._settings = replace(self._settings, engine_profile=target)
        # The stored language is profile-scoped: a code the incoming profile
        # cannot serve is dropped (back to that profile's default) rather than
        # carried over and refused on the next submission.
        self._clear_unsupported_language()
        self._reset_profile_state()
        self._set_error("")
        try:
            save_settings(self._settings, self._data_dir)
        except OSError as exc:  # noqa: BLE001 - the live switch still applies
            self._set_error(self.tr("Không thể lưu cài đặt: {}").format(exc))
        self.engineProfileChanged.emit()
        self.engineProfilesChanged.emit()
        self.profileCatalogChanged.emit()
        self.voicesChanged.emit()
        # Landing on the profile an armed Studio offer names may satisfy it
        # outright (same variant) — a satisfied offer disarms here.
        self._disarm_satisfied_regen_offer()
        # Resolve the new profile's readiness/device off the GUI thread.
        self.refreshProfileState()
        return True

    @Slot()
    def refreshProfileState(self) -> None:
        """Resolve the ACTIVE profile's device + model/runtime readiness.

        Filesystem-only inspection, always off the GUI thread (the hardware
        probe shells out to nvidia-smi): no Hub import, no download, no model
        load. Called after first paint by ``run_gui`` and after every switch.

        The Qwen cards are refreshed on EVERY pass, whichever profile is
        active: they exist to install Qwen before switching to it, so they
        cannot wait for a switch to learn their state. One background job
        probes the machine once and publishes the active profile's device plus
        the Qwen runtime/model cards.
        """
        self._profile_generation += 1
        generation = self._profile_generation
        profile = self._active_profile
        if profile == engine_profiles.VIENEU:
            # The official baseline install is engine-independent and its
            # inspect is expensive (it re-hashes every model file), so this
            # branch never kicks a second one: it mirrors the status that
            # refreshModelState already publishes (startup and retry). The
            # in-process runtime is ready by construction.
            self._publish_profile_runtime(ProfileReadiness(state="ready", ready=True))
            self._publish_profile_model(_readiness_from(self._model_status))
        # A Qwen profile's device + model + runtime readiness IS the Qwen card
        # state, so one inspection feeds both views.
        self._start_qwen_inspection(profile=profile, generation=generation)

    def _publish_profile_device(self, device: str, generation: int) -> None:
        """Publish the ACTIVE profile's resolved device (generation-guarded)."""
        if not device or generation != self._profile_generation:
            return
        if device != self._profile_device:
            self._profile_device = device
            self.engineDeviceChanged.emit()

    def _publish_profile_model(self, readiness: ProfileReadiness) -> None:
        """Publish model readiness (emits only what actually changed)."""
        if readiness == self._profile_model_status:
            return
        was_ready = self.profileReady
        self._profile_model_status = readiness
        self.profileModelChanged.emit()
        if self.profileReady != was_ready:
            self.profileReadyChanged.emit()

    def _publish_profile_runtime(self, readiness: ProfileReadiness) -> None:
        """Publish runtime readiness (emits only what actually changed)."""
        if readiness == self._profile_runtime_status:
            return
        was_ready = self.profileReady
        self._profile_runtime_status = readiness
        self.profileRuntimeChanged.emit()
        if self.profileReady != was_ready:
            self.profileReadyChanged.emit()

    def _clear_unsupported_language(self) -> None:
        """Drop a stored language the ACTIVE profile cannot serve.

        Called after a switch: the language is profile-scoped by definition, so
        an incompatible leftover becomes "not chosen" and the profile default
        applies again. Emits only when something actually changed.
        """
        stored = str(self._settings.synthesis_language or "").strip()
        if not stored:
            return
        supported = {
            option.code
            for option in engine_profiles.get_capabilities(self._active_profile).languages
        }
        if stored in supported:
            return
        logger.info(
            "dropping synthesis language %r for %s; using its default",
            stored,
            self._active_profile,
        )
        self._settings = replace(self._settings, synthesis_language="")
        self.synthesisLanguageChanged.emit()

    def _reset_profile_state(self) -> None:
        """Drop per-profile state after a switch (catalogs + readiness)."""
        self._clone_store = None
        self._profile_device = ""
        if self._active_profile == engine_profiles.VIENEU:
            # The official install is engine-independent, so its already
            # published status stays truthful across the switch; the profile
            # view is seeded from it instead of blanking to "checking".
            self._profile_model_status = _readiness_from(self._model_status)
            self._profile_runtime_status = ProfileReadiness(state="ready", ready=True)
        else:
            self._profile_model_status = ProfileReadiness()
            self._profile_runtime_status = ProfileReadiness(state="checking")
        self._voices = self._build_voices()
        # Unconditional emits: a switch rebinds the whole profile view even
        # when a value happens to coincide with the previous profile's.
        self.engineDeviceChanged.emit()
        self.profileModelChanged.emit()
        self.profileRuntimeChanged.emit()
        self.profileReadyChanged.emit()

    def _profile_switch_blockers(self) -> list[str]:
        """Reasons a profile switch must wait (empty list = free to switch)."""
        blockers: list[str] = []
        if self._busy or self._foreground_job_id is not None:
            blockers.append("a foreground job is running")
        probe = getattr(self._worker, "has_pending_work", None)
        if callable(probe):
            try:
                if probe():
                    blockers.append("the inference worker still has queued work")
            except Exception:  # noqa: BLE001 - a broken probe must not block switching
                logger.exception("worker pending-work probe failed")
        return blockers

    def _clone_store_instance(self) -> Any | None:
        """The profile-scoped clone store, built lazily (never at construction)."""
        if self._clone_store is None:
            try:
                self._clone_store = self._clone_store_factory(self._data_dir)
            except Exception:  # noqa: BLE001 - an unusable store must not break the shell
                logger.exception("could not open the clone store")
                return None
        return self._clone_store

    @Slot(str, result=str)
    def copyText(self, text: str) -> str:
        """Copy arbitrary text (a path, a driver command) to the clipboard.

        Always returns the copied string so QML can bind a confirmation to
        the call. Clipboard-less environments (headless test harnesses) are
        not an error — the seam is a convenience, never a failure path.
        """
        value = str(text)
        try:
            from PySide6.QtGui import QGuiApplication

            inst = QGuiApplication.instance()
            if isinstance(inst, QGuiApplication):
                clipboard = inst.clipboard()
                if clipboard is not None:
                    clipboard.setText(value)
        except Exception:  # noqa: BLE001 — headless/test harness has no clipboard
            pass
        return value

    @Slot(result=str)
    def copyModelDir(self) -> str:
        """Copy the model dir path to the clipboard; always returns the path."""
        return self.copyText(self.modelDir)

    @Slot(result=bool)
    def openModelDir(self) -> bool:
        """Create (if needed) and reveal the model dir in the file manager."""
        return self._reveal_dir(self.modelDir, self.tr("Không mở được thư mục mô hình: {}"))

    def _reveal_dir(self, path: str, failure_message: str) -> bool:
        """Create (if needed) and reveal ``path`` in the OS file manager."""
        target = Path(path)
        try:
            target.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._set_error(failure_message.format(exc))
            return False
        try:
            from PySide6.QtCore import QUrl
            from PySide6.QtGui import QDesktopServices

            return bool(QDesktopServices.openUrl(QUrl.fromLocalFile(str(target))))
        except Exception as exc:  # noqa: BLE001
            self._set_error(failure_message.format(exc))
            return False

    @Slot(str)
    def importOfflinePack(self, source: str) -> None:
        """Validate + promote a manual offline pack (backbone/ + codec/)."""
        clean = normalize_local_path(source)
        if is_empty_path(clean):
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
        raw = str(clean)
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

        def on_error(exc: BaseException) -> None:
            if generation != self._model_generation:
                return
            self._publish_model_status(ModelStatus(state="failed", error=str(exc), location=None))
            self._model_downloading = False

        self._run_bg(work, on_done, self, on_error=on_error)

    def _publish_model_status(self, status: ModelStatus) -> None:
        previous = self._model_status
        self._model_status = status
        if self._active_profile == engine_profiles.VIENEU:
            # The profile view mirrors the official baseline's own status, so a
            # UI binding reads one shape whichever engine owns synthesis.
            self._publish_profile_model(_readiness_from(status))
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

    # ── managed CUDA runtime ─────────────────────────────────────────────────

    @staticmethod
    def _unsupported_cuda_status() -> CudaRuntimeStatus:
        return CudaRuntimeStatus(
            "unavailable",
            error="Managed CUDA runtime is supported only on Windows and Linux x64.",
        )

    @Property(str, notify=cudaRuntimeStateChanged)
    def cudaRuntimeState(self) -> str:
        return str(self._cuda_runtime_status.state)

    @Property(float, notify=cudaRuntimeProgressChanged)
    def cudaRuntimeProgress(self) -> float:
        return float(self._cuda_runtime_status.progress)

    # qlonglong: the linux-x64 manifest needs 7,923,412,460 bytes — a 32-bit
    # Qt int makes the QML read raise OverflowError and kill the tab switch.
    @Property("qlonglong", notify=cudaRuntimeStorageChanged)
    def cudaRuntimeInstalledBytes(self) -> int:
        return int(self._cuda_runtime_status.installed_bytes)

    @Property("qlonglong", notify=cudaRuntimeStorageChanged)
    def cudaRuntimeRequiredBytes(self) -> int:
        return int(self._cuda_runtime_status.required_bytes)

    @Property(str, notify=cudaRuntimeErrorChanged)
    def cudaRuntimeError(self) -> str:
        return str(self._cuda_runtime_status.error)

    @Property(bool, notify=cudaRuntimeSupportedChanged)
    def cudaRuntimeSupported(self) -> bool:
        return self._cuda_runtime_supported

    @Property(bool, notify=cudaRuntimeDriverChanged)
    def cudaRuntimeDriverChecked(self) -> bool:
        return self._cuda_runtime_driver_checked

    @Property(bool, notify=cudaRuntimeDriverChanged)
    def cudaRuntimeDriverReady(self) -> bool:
        return self._cuda_runtime_driver_ready

    @Property(bool, notify=cudaRuntimeDriverChanged)
    def cudaRuntimeInstallAllowed(self) -> bool:
        return self._cuda_runtime_supported and self._cuda_runtime_driver_ready

    @Property(bool, notify=cudaRuntimeStateChanged)
    def cudaRuntimeReady(self) -> bool:
        return self._cuda_runtime_status.state == "ready"

    @Property("QVariantList", notify=localCudaRuntimesChanged)
    def localCudaRuntimes(self) -> list[dict[str, object]]:
        return self._local_cuda_runtimes

    def _publish_cuda_runtime_status(self, status: CudaRuntimeStatus) -> None:
        previous = self._cuda_runtime_status
        self._cuda_runtime_status = status
        if status.state != previous.state:
            self.cudaRuntimeStateChanged.emit()
            if status.state == "ready" or previous.state == "ready":
                # Managed CUDA readiness feeds torchAvailable: drop the cache
                # so the next read re-probes instead of serving a stale answer.
                self._torch_available = None
                self.torchAvailableChanged.emit()
        if status.progress != previous.progress:
            self.cudaRuntimeProgressChanged.emit()
        if status.error != previous.error:
            self.cudaRuntimeErrorChanged.emit()
        if (
            status.installed_bytes != previous.installed_bytes
            or status.required_bytes != previous.required_bytes
        ):
            self.cudaRuntimeStorageChanged.emit()

    def _on_cuda_runtime_status_signal(self, payload: object) -> None:
        try:
            generation, status = payload  # type: ignore[misc]
        except (TypeError, ValueError):
            return
        if (
            generation != self._cuda_runtime_generation
            or self._cuda_runtime_operation not in ("install", "inspect")
            or not isinstance(status, CudaRuntimeStatus)
        ):
            return
        self._publish_cuda_runtime_status(status)

    def _publish_cuda_runtime_driver_probe(self, probe: CudaDriverProbe) -> None:
        """Publish a lightweight NVIDIA driver probe without importing torch."""
        # The version lands before the signal: slots re-resolving the engine
        # readout run while the signal is emitted and must see the fresh value.
        self._cuda_runtime_driver_version = probe.cuda_version
        ready_changed = probe.usable != self._cuda_runtime_driver_ready
        if not self._cuda_runtime_driver_checked or ready_changed:
            self._cuda_runtime_driver_checked = True
            self._cuda_runtime_driver_ready = probe.usable
            self.cudaRuntimeDriverChanged.emit()
        if ready_changed:
            self._torch_available = None
            self.torchAvailableChanged.emit()

    def managed_cuda_for_detection(self) -> tuple[bool, str | None]:
        """(ready, cuda_version) feeding the detector readout.

        True only when the managed runtime is installed AND the retained
        driver probe says usable — the same gate the engine build uses
        (``resolve_model_source``), so the Settings readout and the actual
        backend can no longer disagree.
        """
        status = self._cuda_runtime_status
        ready = (
            self._cuda_runtime_supported
            and status.state == "ready"
            and isinstance(status.location, CudaRuntimeLocation)
            and self._cuda_runtime_driver_ready
        )
        return (ready, self._cuda_runtime_driver_version if ready else None)

    def managed_cuda_engine_state(self) -> bool | None:
        """Tri-state gate for the engine's deferred ``auto`` resolution.

        ``None`` while the managed-runtime inspect or the NVIDIA driver probe
        is still in flight (the engine waits briefly); True only when the
        managed runtime is installed AND the driver probe says usable — the
        same gate ``managed_cuda_for_detection`` and ``resolve_model_source``
        use, so the built engine cannot disagree with the readout.
        """
        if not self._cuda_runtime_supported:
            return False
        status = self._cuda_runtime_status
        if status.state == "checking":
            return None
        if status.state != "ready" or not isinstance(status.location, CudaRuntimeLocation):
            return False
        if not self._cuda_runtime_driver_checked:
            return None
        return self._cuda_runtime_driver_ready

    def _inspect_cuda_runtime_driver(self) -> CudaDriverProbe:
        """Probe the NVIDIA driver in the CUDA manager's background lane."""
        try:
            return self._cuda_driver_probe()
        except Exception:  # noqa: BLE001 - failed probes keep CUDA installation safe
            return CudaDriverProbe(available=False)

    @Slot()
    def refreshCudaRuntimeState(self) -> None:
        """Inspect CUDA driver and managed runtime off-thread; never imports torch."""
        manager = self._cuda_runtime_manager
        if manager is None:
            self._publish_cuda_runtime_status(self._unsupported_cuda_status())
            return
        if self._cuda_runtime_operation is not None:
            return
        self._cuda_runtime_generation += 1
        generation = self._cuda_runtime_generation
        self._cuda_runtime_operation = "inspect"

        def work() -> tuple[CudaDriverProbe, CudaRuntimeStatus]:
            try:
                status = manager.inspect()
            except Exception as exc:  # noqa: BLE001 - filesystem errors are UI state
                status = CudaRuntimeStatus("failed", error=str(exc))
            # Publish the (fast) filesystem result before the driver probe:
            # nvidia-smi can take seconds on a sleeping dGPU, and the engine
            # build needs the runtime state to know whether to wait for the
            # probe instead of resolving "auto" to ONNX prematurely.
            self._cuda_runtime_status_signal.emit((generation, status))
            return self._inspect_cuda_runtime_driver(), status

        def on_done(result: tuple[CudaDriverProbe, CudaRuntimeStatus]) -> None:
            if (
                generation == self._cuda_runtime_generation
                and self._cuda_runtime_operation == "inspect"
            ):
                probe, status = _unwrap_bg_result(result)
                self._publish_cuda_runtime_driver_probe(probe)
                self._publish_cuda_runtime_status(status)
                self._cuda_runtime_operation = None

        def on_error(exc: BaseException) -> None:
            if (
                generation != self._cuda_runtime_generation
                or self._cuda_runtime_operation != "inspect"
            ):
                return
            logger.warning("CUDA runtime inspect failed: %s", exc)
            self._publish_cuda_runtime_status(CudaRuntimeStatus("failed", error=str(exc)))
            self._cuda_runtime_operation = None

        self._run_bg(work, on_done, self, on_error=on_error)

    @Slot()
    def discoverLocalCudaRuntimes(self) -> None:
        """Run the explicit, diagnostic-only local scan off the GUI thread."""
        self._local_cuda_generation += 1
        generation = self._local_cuda_generation

        def work() -> list[LocalCudaRuntime]:
            try:
                return self._local_cuda_discovery()
            except Exception:  # noqa: BLE001 - diagnostics must not break the UI
                logger.exception("local CUDA runtime discovery failed")
                return []

        def on_done(found: list[LocalCudaRuntime]) -> None:
            if generation != self._local_cuda_generation:
                return
            self._local_cuda_runtimes = [
                {
                    "label": str(runtime.label),
                    "compatible": bool(runtime.compatible),
                    "reason": str(runtime.reason),
                }
                for runtime in found
            ]
            self.localCudaRuntimesChanged.emit()

        self._run_bg(work, on_done, self)

    @Slot()
    def installCudaRuntime(self) -> None:
        """Start the explicit managed-runtime install without blocking QML."""
        manager = self._cuda_runtime_manager
        if manager is None:
            self._publish_cuda_runtime_status(self._unsupported_cuda_status())
            return
        if self._cuda_runtime_operation is not None:
            return
        if not self._cuda_runtime_driver_checked:
            # nvidia-smi can take seconds: probe in the manager lane, then
            # re-enter. The second pass finds the driver checked and starts
            # the install; a concurrent refresh/cancel wins the generation.
            self._cuda_runtime_generation += 1
            generation = self._cuda_runtime_generation
            self._cuda_runtime_operation = "driver-probe"

            def probe_work() -> CudaDriverProbe:
                return self._inspect_cuda_runtime_driver()

            def probe_done(probe: CudaDriverProbe) -> None:
                if (
                    generation != self._cuda_runtime_generation
                    or self._cuda_runtime_operation != "driver-probe"
                ):
                    return
                self._publish_cuda_runtime_driver_probe(probe)
                self._cuda_runtime_operation = None
                self.installCudaRuntime()

            def probe_error(exc: BaseException) -> None:
                if (
                    generation != self._cuda_runtime_generation
                    or self._cuda_runtime_operation != "driver-probe"
                ):
                    return
                logger.warning("CUDA driver probe failed: %s", exc)
                self._cuda_runtime_operation = None

            self._run_bg(probe_work, probe_done, self, on_error=probe_error)
            return
        if not self.cudaRuntimeInstallAllowed:
            return
        self._cuda_runtime_generation += 1
        generation = self._cuda_runtime_generation
        cancelled = threading.Event()
        self._cuda_runtime_cancel = cancelled
        self._cuda_runtime_installing = True
        self._cuda_runtime_operation = "install"
        previous = self._cuda_runtime_status
        self._publish_cuda_runtime_status(
            CudaRuntimeStatus(
                "downloading",
                platform_key=previous.platform_key,
                installed_bytes=previous.installed_bytes,
                required_bytes=previous.required_bytes,
            )
        )

        def work() -> CudaRuntimeStatus:
            try:
                return manager.install(
                    cancelled=cancelled.is_set,
                    on_progress=lambda status: self._cuda_runtime_status_signal.emit(
                        (generation, status)
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - installation failures are UI state
                return CudaRuntimeStatus("failed", error=str(exc))

        def on_done(status: CudaRuntimeStatus) -> None:
            if generation != self._cuda_runtime_generation:
                if (
                    self._cuda_runtime_operation == "install"
                    and self._cuda_runtime_cancel is cancelled
                ):
                    self._cuda_runtime_installing = False
                    self._cuda_runtime_operation = None
                return
            self._publish_cuda_runtime_status(status)
            self._cuda_runtime_installing = False
            self._cuda_runtime_operation = None

        def on_error(exc: BaseException) -> None:
            if generation != self._cuda_runtime_generation:
                return
            logger.warning("CUDA runtime install failed: %s", exc)
            self._publish_cuda_runtime_status(CudaRuntimeStatus("failed", error=str(exc)))
            self._cuda_runtime_installing = False
            self._cuda_runtime_operation = None

        self._run_bg(work, on_done, self, on_error=on_error)

    @Slot()
    def cancelCudaRuntimeInstall(self) -> None:
        """Request cooperative cancellation and invalidate queued callbacks."""
        if self._cuda_runtime_operation == "driver-probe":
            # Install has not started: drop the stale probe landing so it
            # cannot start one after the user cancelled.
            self._cuda_runtime_generation += 1
            self._cuda_runtime_operation = None
            previous = self._cuda_runtime_status
            self._publish_cuda_runtime_status(
                CudaRuntimeStatus(
                    "unavailable",
                    platform_key=previous.platform_key,
                    required_bytes=previous.required_bytes,
                )
            )
            return
        if not self._cuda_runtime_installing:
            return
        self._cuda_runtime_cancel.set()
        self._cuda_runtime_generation += 1
        previous = self._cuda_runtime_status
        self._publish_cuda_runtime_status(
            CudaRuntimeStatus(
                "unavailable",
                platform_key=previous.platform_key,
                required_bytes=previous.required_bytes,
            )
        )

    def _cuda_runtime_in_use(self) -> bool:
        """True only after this process has activated a managed CUDA runtime."""
        if bool(getattr(self._engine, "_cuda_activation", None)):
            self._cuda_runtime_activated = True
        return self._cuda_runtime_activated

    @Slot()
    def removeCudaRuntime(self) -> None:
        """Remove an inactive managed runtime without blocking the GUI thread."""
        manager = self._cuda_runtime_manager
        if manager is None or self._cuda_runtime_operation is not None:
            return
        self._cuda_runtime_generation += 1
        generation = self._cuda_runtime_generation
        in_use = self._cuda_runtime_in_use()
        self._cuda_runtime_operation = "remove"

        def work() -> CudaRuntimeStatus:
            try:
                return manager.remove(in_use=in_use)
            except Exception as exc:  # noqa: BLE001 - removal failures are UI state
                return CudaRuntimeStatus("failed", error=str(exc))

        def on_done(status: CudaRuntimeStatus) -> None:
            if (
                generation == self._cuda_runtime_generation
                and self._cuda_runtime_operation == "remove"
            ):
                self._publish_cuda_runtime_status(status)
                self._cuda_runtime_operation = None

        def on_error(exc: BaseException) -> None:
            if (
                generation != self._cuda_runtime_generation
                or self._cuda_runtime_operation != "remove"
            ):
                return
            logger.warning("CUDA runtime removal failed: %s", exc)
            self._publish_cuda_runtime_status(CudaRuntimeStatus("failed", error=str(exc)))
            self._cuda_runtime_operation = None

        self._run_bg(work, on_done, self, on_error=on_error)

    # ── managed Qwen runtime + models (Task 6.1) ─────────────────────────────

    def _build_default_qwen_runtime_manager(self, data_dir: Path) -> Any | None:
        """The manager for the platform key the device choice resolves to.

        Never inspects, downloads, or imports torch — construction only, like
        the CUDA default. Returns ``None`` when this host has no pinned runtime
        for the chosen device (an unsupported platform, or ``auto`` before the
        first inspection has resolved the device).
        """
        return _qwen_runtime_manager_for_key(self._qwen_runtime_key, data_dir)

    def _qwen_runtime_manager(self) -> Any | None:
        """The runtime manager for the selected format + resolved key.

        Official factories take just ``data_dir``; GGUF factories are
        cell-keyed — a still-unresolved ``auto`` choice has no cell, so it
        reports "no managed runtime" instead of guessing one.
        """
        if self._is_gguf_selected():
            if not self._qwen_runtime_key:
                return None
            return self._qwen_gguf_runtime_manager_factory(self._data_dir, self._qwen_runtime_key)
        return self._qwen_runtime_manager_factory(self._data_dir)

    def _qwen_model_manager(self, key: str) -> Any:
        """The model manager a row key names — either keyspace.

        A GGUF variant key (``{profile}-{quantization}``) is dispatched to the
        managed-GGUF manager built for that exact variant; a bare profile key
        (``base``/``customvoice``) goes to the official checkpoint manager.
        The key — not the current selection — decides, so a row action always
        addresses the install it names.
        """
        parsed = qwen_gguf_models_manifest.parse_variant_key(key)
        if parsed is not None:
            profile_key, quantization = parsed
            engine_id = qwen_gguf_models_manifest.engine_for_profile_key(profile_key)
            if engine_id is None:
                raise KeyError(f"unknown GGUF model profile: {profile_key}")
            variant = qwen_variants.variant_for(
                engine_id, qwen_variants.MODEL_FORMAT_GGUF, quantization
            )
            return self._qwen_gguf_model_manager_factory(self._data_dir, variant)
        return self._qwen_model_manager_factory(self._data_dir, key)

    def _qwen_host_supported(self) -> bool:
        """Whether this host is a platform the pinned manifests cover at all."""
        return qwen_manifest.host_platform_tag() is not None

    def _is_gguf_selected(self) -> bool:
        """Whether the settings select the managed-native (GGUF) variant."""
        return self._settings.qwen_model_format == qwen_variants.MODEL_FORMAT_GGUF

    @Property(bool, notify=qwenRuntimeSupportChanged)
    def qwenRuntimeSupported(self) -> bool:
        """Whether this host + device has a pinned Qwen runtime to install.

        False means "no managed runtime exists for this machine" (an
        unsupported OS/arch, or a device the platform has no wheels for) — the
        card shows that instead of an install button that could only fail.
        Under GGUF the same question is asked of the qwentts.cpp cell matrix:
        a resolved key must name a cell that actually ships a pack.
        """
        if self._is_gguf_selected():
            if self._qwen_runtime_key:
                return qwen_gguf_manifest.manifest_for_cell(self._qwen_runtime_key) is not None
            # No resolved cell yet: an explicit device that maps to no cell on
            # this host can never install; ``auto`` is supported when at least
            # one device actually ships a pack for this platform.
            if self._qwen_device_pref() != "auto":
                return False
            return bool(qwen_gguf_manifest.installable_devices())
        return self._qwen_host_supported() and (
            not self._qwen_runtime_key
            or qwen_manifest.manifest_for_platform(self._qwen_runtime_key) is not None
        )

    @Property(str, notify=qwenRuntimeSupportChanged)
    def qwenRuntimePlatformKey(self) -> str:
        """Pinned manifest key of the runtime being installed ("" = unknown).

        A wheel-matrix platform key under the official variant (e.g.
        ``linux-x64-cuda``) or a qwentts.cpp cell under GGUF (e.g.
        ``linux-x64-cpu``) — the keyspace follows the selection.
        """
        return self._qwen_runtime_key

    @Property(str, notify=qwenRuntimeSupportChanged)
    def qwenRuntimeVariantLabel(self) -> str:
        """Display name of the runtime variant (e.g. "Linux x64 · CUDA")."""
        if self._is_gguf_selected():
            return qwen_gguf_manifest.cell_label(self._qwen_runtime_key)
        return qwen_manifest.platform_label(self._qwen_runtime_key)

    @Property(str, notify=qwenRuntimeStateChanged)
    def qwenRuntimeState(self) -> str:
        """Runtime lifecycle state (``checking`` until the first inspect)."""
        status = self._qwen_runtime_status
        if status is None:
            return "checking" if self.qwenRuntimeSupported else "unsupported"
        return str(getattr(status, "state", "") or "checking")

    @Property(float, notify=qwenRuntimeProgressChanged)
    def qwenRuntimeProgress(self) -> float:
        return float(getattr(self._qwen_runtime_status, "progress", 0.0) or 0.0)

    # qlonglong: the CUDA-closure runtime is multi-gigabyte, and Qt's int is
    # 32-bit — a >2 GiB read would raise OverflowError inside QML.
    @Property("qlonglong", notify=qwenRuntimeStorageChanged)
    def qwenRuntimeInstalledBytes(self) -> int:
        return int(getattr(self._qwen_runtime_status, "installed_bytes", 0) or 0)

    @Property("qlonglong", notify=qwenRuntimeStorageChanged)
    def qwenRuntimeRequiredBytes(self) -> int:
        """The pinned runtime's payload — the download size the card reports.

        ``status.required_bytes`` is the installer's *disk-space* preflight
        (twice the payload: archives plus the extracted tree), so using it as
        the download size made a finished 410 MB download read "Downloaded
        410 MB of 820 MB" and look stuck at half. The pinned manifest is the
        authority — the wheel matrix under official, the cell manifest under
        GGUF; the status is only the fallback for a key with no manifest.
        """
        if self._is_gguf_selected():
            payload = qwen_gguf_manifest.manifest_bytes_for_cell(self._qwen_runtime_key)
        else:
            payload = qwen_manifest.manifest_bytes_for_platform(self._qwen_runtime_key)
        if payload:
            return payload
        return int(getattr(self._qwen_runtime_status, "required_bytes", 0) or 0)

    @Property(str, notify=qwenRuntimeErrorChanged)
    def qwenRuntimeError(self) -> str:
        return str(getattr(self._qwen_runtime_status, "error", "") or "")

    @Property(bool, notify=qwenRuntimeStateChanged)
    def qwenRuntimeReady(self) -> bool:
        return self.qwenRuntimeState == "ready"

    @Property(bool, notify=qwenRuntimeStateChanged)
    def qwenRuntimeBusy(self) -> bool:
        """Whether an operation owns the Qwen lane (buttons disable on it)."""
        return self._qwen_operation is not None

    @Property(str, notify=qwenRuntimeStorageChanged)
    def qwenRuntimeStoragePath(self) -> str:
        """Where the runtime lives on disk (for the storage row + reveal)."""
        status = self._qwen_runtime_status
        location = getattr(status, "location", None)
        root = getattr(location, "root", None)
        if root:
            return str(root)
        leaf = "gguf-runtime" if self._is_gguf_selected() else "runtime"
        return str(self._data_dir / "qwen" / leaf)

    @Property("QVariantList", notify=qwenModelsChanged)
    def qwenModels(self) -> list[dict[str, Any]]:
        """Model install rows for the selected format, always present.

        Official lists one checkpoint per Qwen profile; GGUF lists the full
        per-variant matrix (every profile × quantization pair the manifest
        ships), since each pair is a separately verified install. The rows
        join the capability table with whatever the last inspection learned,
        so the card lists everything — and its shared tokenizer storage —
        before any of it is installed.
        """
        rows: list[dict[str, Any]] = []
        gguf = self._is_gguf_selected()
        selected_quant = self._settings.qwen_gguf_quantization
        for profile in engine_profiles.list_profiles():
            if not engine_profiles.is_qwen_profile(profile):
                continue
            profile_key = engine_profiles.runtime_key(profile)
            caps = engine_profiles.get_capabilities(profile)
            quantizations = qwen_variants.GGUF_QUANTIZATIONS if gguf else ("",)
            for quantization in quantizations:
                if gguf:
                    key = qwen_gguf_models_manifest.variant_key_for(profile_key, quantization)
                    recipe = qwen_gguf_models_manifest.recipe_for(profile_key, quantization)
                    required = int(recipe.total_bytes) if recipe is not None else 0
                    label = f"{caps.label} · {quantization}"
                    engine = qwen_variants.ENGINE_QWENTTS_CPP
                else:
                    key = profile_key
                    pinned = _qwen_model_profile(key)
                    # The row shows the real download size: a status's
                    # ``required_bytes`` carries the re-install headroom used
                    # for the disk-space preflight, not the payload.
                    required = int(pinned.total_bytes) if pinned is not None else 0
                    label = caps.label
                    engine = qwen_variants.ENGINE_PYTORCH
                status = self._qwen_model_statuses.get(key)
                if not required:
                    required = int(getattr(status, "required_bytes", 0) or 0)
                rows.append(
                    {
                        "key": key,
                        "profile": profile,
                        "label": label,
                        "format": "gguf" if gguf else "official",
                        "quantization": quantization,
                        "engine": engine,
                        "state": str(getattr(status, "state", "") or "checking"),
                        "ready": str(getattr(status, "state", "") or "") == "ready",
                        "installedBytes": int(getattr(status, "installed_bytes", 0) or 0),
                        "requiredBytes": required,
                        "progress": float(getattr(status, "progress", 0.0) or 0.0),
                        "error": str(getattr(status, "error", "") or ""),
                        "busy": self._qwen_models_busy == key,
                        "isActive": profile == self._active_profile,
                        "isSelected": (not gguf) or quantization == selected_quant,
                    }
                )
        return rows

    @Property("qlonglong", notify=qwenModelsChanged)
    def qwenSharedBytes(self) -> int:
        """Bytes of shared tokenizer content the model installs reuse.

        Official checkpoints share one tokenizer tree; under GGUF each
        quantization has its own codec shared across the two profiles.
        """
        if self._is_gguf_selected():
            return qwen_gguf_models_manifest.shared_codec_bytes(
                self._settings.qwen_gguf_quantization
            )
        return _qwen_shared_model_bytes()

    @Property(bool, notify=qwenModelsChanged)
    def qwenModelBusy(self) -> bool:
        return bool(self._qwen_models_busy)

    @Property(str, notify=qwenModelsChanged)
    def qwenModelStoragePath(self) -> str:
        """The shared Qwen model root (all of the format's variants live under it)."""
        leaf = "gguf-models" if self._is_gguf_selected() else "models"
        return str(self._data_dir / "qwen" / leaf)

    def _qwen_device_pref(self) -> str:
        """The stored device preference for the SELECTED model format.

        ``qwen_device`` is PyTorch vocabulary (``mps``), ``qwen_gguf_device``
        is ggml vocabulary (``metal``) — one preference per format so switching
        formats never rewrites the inactive format's choice.
        """
        if self._is_gguf_selected():
            return str(self._settings.qwen_gguf_device or "auto")
        return str(self._settings.qwen_device or "auto")

    @Property(str, notify=qwenDeviceChanged)
    def qwenDevice(self) -> str:
        """Chosen compute device for the selected format (``auto``/…)."""
        return self._qwen_device_pref()

    @Property("QVariantList", notify=qwenDeviceChanged)
    def qwenDeviceOptions(self) -> list[dict[str, Any]]:
        """Device choices with support + the reason a choice is unavailable.

        Support is platform truth, not preference: a device is offered when
        the selected format's manifest matrix ships a runtime for it on this
        host, and CUDA/MPS/Metal additionally require the hardware the host
        would need to run them. The list itself is the format's vocabulary —
        ``mps`` under official, ``metal`` under GGUF.
        """
        gguf = self._is_gguf_selected()
        labels = {
            "auto": self.tr("Tự động (khuyến nghị)"),
            "cpu": "CPU",
            "cuda": "CUDA (NVIDIA)",
            "mps": "MPS (Apple Silicon)",
            "metal": "Metal (Apple Silicon)",
        }
        pref = self._qwen_device_pref()
        options: list[dict[str, Any]] = []
        for value in qwen_variants.device_choices_for(
            qwen_variants.MODEL_FORMAT_GGUF if gguf else qwen_variants.MODEL_FORMAT_OFFICIAL
        ):
            supported, reason = self._qwen_device_support(value)
            options.append(
                {
                    "value": value,
                    "label": labels.get(value, value),
                    "supported": supported,
                    "reason": reason,
                    "active": value == pref,
                    "resolved": self._qwen_device_resolution(value),
                }
            )
        return options

    def _qwen_device_support(self, device: str) -> tuple[bool, str]:
        """(supported, reason) for one device choice on this host."""
        if device == "auto":
            return True, ""
        if self._is_gguf_selected():
            # A device is installable only when the host has a cell AND that
            # cell ships a verified pack — a matrix entry without a manifest
            # is truthful "no managed runtime", not an installable choice.
            cell = qwen_gguf_manifest.host_cell_key(device)
            if cell is None or qwen_gguf_manifest.manifest_for_cell(cell) is None:
                return False, self.tr("Nền tảng này không có runtime Qwen cho thiết bị đã chọn.")
        elif device not in qwen_manifest.host_devices():
            return False, self.tr("Nền tảng này không có runtime Qwen cho thiết bị đã chọn.")
        hardware = self._hardware_status
        if hardware is None:
            # No probe yet: the platform answer stands on its own, and the
            # resolved-device readout reports what actually runs once it lands.
            return True, ""
        if device == "cuda" and hardware.kind != "nvidia":
            return False, self.tr("Không phát hiện GPU NVIDIA trên máy này.")
        if device == "mps" and hardware.kind != "apple_silicon":
            return False, self.tr("MPS chỉ có trên Apple Silicon.")
        if device == "metal" and hardware.kind != "apple_silicon":
            return False, self.tr("Metal chỉ có trên Apple Silicon.")
        return True, ""

    def _qwen_device_resolution(self, device: str) -> str:
        """What a choice resolves to on this host ("" when it cannot run)."""
        if device != "auto":
            return device
        return self._qwen_resolved_device or ""

    @Property(str, notify=qwenDeviceChanged)
    def qwenCpuGuidance(self) -> str:
        """CPU performance warning, shown before a CPU download/run (NFR).

        Empty while the device is still resolving: the guidance must describe
        the device that will actually run, never a guess.
        """
        resolved = self._qwen_device_resolution(self._qwen_device_pref())
        if resolved != "cpu":
            return ""
        return self.tr(
            "Chạy Qwen trên CPU sẽ rất chậm (chậm hơn nhiều lần so với GPU). "
            "Hãy cài runtime CPU nếu máy không có GPU, và dùng văn bản ngắn để thử trước."
        )

    @Slot(str, result=bool)
    def setQwenDevice(self, device: str) -> bool:
        """Choose the compute device for the SELECTED format (next engine init).

        The pick is written to that format's own settings field — a Metal
        choice under GGUF never rewrites the PyTorch ``mps`` preference and
        vice versa — and resolves the format's own runtime key (wheel-matrix
        platform under official, qwentts.cpp cell under GGUF).
        """
        value = str(device or "").strip()
        gguf = self._is_gguf_selected()
        choices = qwen_variants.device_choices_for(
            qwen_variants.MODEL_FORMAT_GGUF if gguf else qwen_variants.MODEL_FORMAT_OFFICIAL
        )
        if value not in choices:
            self._set_error(self.tr("Thiết bị Qwen không hợp lệ: {}").format(value or "(trống)"))
            return False
        if value == self._qwen_device_pref():
            return True
        if self._qwen_operation is not None:
            self._set_error(self.tr("Đang cài đặt Qwen — vui lòng đợi."))
            return False
        self._settings = (
            replace(self._settings, qwen_gguf_device=value)
            if gguf
            else replace(self._settings, qwen_device=value)
        )
        try:
            save_settings(self._settings, self._data_dir)
        except OSError as exc:  # noqa: BLE001 - the live choice still applies
            self._set_error(self.tr("Không thể lưu cài đặt: {}").format(exc))
        else:
            self._set_error("")
        if value != "auto":
            # The pinned variant follows the explicit choice immediately.
            self._qwen_runtime_key = self._qwen_key_for("", value)
            self._qwen_resolved_device = value
        else:
            self._qwen_runtime_key = ""
            self._qwen_resolved_device = ""
        self._qwen_runtime_status = None
        self.qwenDeviceChanged.emit()
        self.qwenRuntimeSupportChanged.emit()
        self.qwenRuntimeStateChanged.emit()
        self.engineDeviceChanged.emit()
        # A different variant is a different install: re-inspect, and let the
        # running engine rebuild on next use (needsRestart covers the banner).
        self.refreshQwenState()
        if self._engine is not None:
            self._needs_restart = True
            self.needsRestartChanged.emit()
        return True

    # ── model-format variant selection (Task 5.1, GGUF track) ────────────────

    @Property(str, notify=qwenVariantChanged)
    def qwenModelFormat(self) -> str:
        """The selected weight format (``official`` full weights | ``gguf``)."""
        return self._settings.qwen_model_format

    @Property(str, notify=qwenVariantChanged)
    def qwenGgufQuantization(self) -> str:
        """The stored GGUF quantization (``Q8_0``/``Q4_K_M``).

        Kept live while the official format is selected — it is the inactive
        preference a later GGUF switch restores, not a property of full
        weights.
        """
        return self._settings.qwen_gguf_quantization

    @Property(str, notify=qwenVariantChanged)
    def qwenEngine(self) -> str:
        """The engine id the selected format routes to (``pytorch``/``qwentts_cpp``)."""
        return qwen_variants.engine_for_format(self._settings.qwen_model_format)

    @Property(str, notify=qwenVariantChanged)
    def qwenEngineLabel(self) -> str:
        """Display name of the selected engine ("PyTorch"/"qwentts.cpp")."""
        return qwen_variants.engine_label(self.qwenEngine)

    @Property("QVariantList", notify=qwenVariantChanged)
    def qwenVariantOptions(self) -> list[dict[str, Any]]:
        """Every installable (format, quantization) combination.

        Ids name the exact selection — ``official`` or ``{format}-{quant}`` —
        so the UI can bind a radio row per installable variant without
        recomputing the matrix itself.
        """
        selected_format = self._settings.qwen_model_format
        selected_quant = self._settings.qwen_gguf_quantization
        entries = [
            (qwen_variants.MODEL_FORMAT_OFFICIAL, ""),
            *[
                (qwen_variants.MODEL_FORMAT_GGUF, quant)
                for quant in qwen_variants.GGUF_QUANTIZATIONS
            ],
        ]
        options: list[dict[str, Any]] = []
        for fmt, quant in entries:
            engine = qwen_variants.engine_for_format(fmt)
            option_id = fmt if fmt == qwen_variants.MODEL_FORMAT_OFFICIAL else f"{fmt}-{quant}"
            label = (
                self.tr("Trọng lượng đầy đủ (PyTorch)")
                if fmt == qwen_variants.MODEL_FORMAT_OFFICIAL
                else f"GGUF {quant} · qwentts.cpp"
            )
            options.append(
                {
                    "id": option_id,
                    "format": fmt,
                    "quantization": quant,
                    "engine": engine,
                    "engineLabel": qwen_variants.engine_label(engine),
                    "label": label,
                    "active": fmt == selected_format
                    and (fmt == qwen_variants.MODEL_FORMAT_OFFICIAL or quant == selected_quant),
                }
            )
        return options

    @Slot(str, str, result=bool)
    def setQwenVariant(self, model_format: str, quantization: str = "") -> bool:
        """Select the Qwen weight format (+ quantization under GGUF).

        Same idle-only rule as a profile switch: refused while a job is
        running or queued, while a cancellation is in flight, or while an
        install owns the Qwen lane — an in-flight inspection is superseded
        instead (its result arrives stamped with the old generation and is
        dropped). An accepted switch persists both fields, tears a built Qwen
        engine down while idle so the next submission builds the new variant,
        and re-resolves the format's own runtime cell and model rows.
        """
        fmt = str(model_format or "").strip()
        quant = str(quantization or "").strip()
        if fmt not in qwen_variants.MODEL_FORMATS:
            self._set_error(
                self.tr("Định dạng mô hình Qwen không hợp lệ: {}").format(fmt or "(trống)")
            )
            return False
        if fmt == qwen_variants.MODEL_FORMAT_OFFICIAL:
            if quant:
                self._set_error(
                    self.tr("Trọng lượng đầy đủ không có lượng tử hóa — chọn định dạng GGUF.")
                )
                return False
        else:
            # A blank quantization means "the remembered GGUF choice" — the
            # spec defaults the FIRST GGUF selection to Q8_0 but keeps the
            # user's later pick, so the stored field wins over the default.
            quant = (
                quant
                or self._settings.qwen_gguf_quantization
                or qwen_variants.DEFAULT_GGUF_QUANTIZATION
            )
            if quant not in qwen_variants.GGUF_QUANTIZATIONS:
                self._set_error(self.tr("Lượng tử hóa GGUF không hợp lệ: {}").format(quant))
                return False
        current_format = self._settings.qwen_model_format
        current_quant = self._settings.qwen_gguf_quantization
        if fmt == current_format and (
            fmt == qwen_variants.MODEL_FORMAT_OFFICIAL or quant == current_quant
        ):
            self._disarm_satisfied_regen_offer()
            return True
        blockers = self._profile_switch_blockers()
        if self._qwen_operation not in (None, "inspect"):
            blockers.append("a Qwen install is still running")
        if blockers:
            self._set_error(
                self.tr("Không thể đổi biến thể khi đang xử lý: {}").format(", ".join(blockers))
            )
            return False
        # Invalidate every in-flight Qwen result BEFORE teardown: shutdown's
        # pool drain can land a still-current inspection mid-call, and it must
        # not publish the previous format's state on the way out.
        self._qwen_generation += 1
        # A built Qwen engine was constructed under the previous variant —
        # tear it down while idle so the next submission builds what the new
        # selection names. VieNeu's in-process engine is format-independent.
        if self._engine is not None and engine_profiles.is_qwen_profile(self._active_profile):
            self.shutdown()
        self._settings = (
            replace(
                self._settings,
                qwen_model_format=fmt,
                qwen_gguf_quantization=quant,
            )
            if fmt == qwen_variants.MODEL_FORMAT_GGUF
            else replace(self._settings, qwen_model_format=fmt)
        )
        try:
            save_settings(self._settings, self._data_dir)
        except OSError as exc:  # noqa: BLE001 - the live choice still applies
            self._set_error(self.tr("Không thể lưu cài đặt: {}").format(exc))
        else:
            self._set_error("")
        # The device preference is per-format: resolve the NEW format's pref
        # against its own vocabulary — an explicit pick keys the runtime now,
        # ``auto`` waits for the background probe.
        pref = self._qwen_device_pref()
        if pref != "auto":
            self._qwen_runtime_key = self._qwen_key_for("", pref)
            self._qwen_resolved_device = pref
        else:
            self._qwen_runtime_key = ""
            self._qwen_resolved_device = ""
        self._qwen_runtime_status = None
        # A Qwen profile's readiness is variant-dependent: reset it to
        # checking until the fresh inspection lands.
        if engine_profiles.is_qwen_profile(self._active_profile):
            self._profile_device = ""
            self._profile_model_status = ProfileReadiness()
            self._profile_runtime_status = ProfileReadiness(state="checking")
            self.engineDeviceChanged.emit()
            self.profileModelChanged.emit()
            self.profileRuntimeChanged.emit()
            self.profileReadyChanged.emit()
        self.qwenVariantChanged.emit()
        self.qwenDeviceChanged.emit()
        self.qwenRuntimeSupportChanged.emit()
        self.qwenRuntimeStateChanged.emit()
        self.qwenRuntimeProgressChanged.emit()
        self.qwenRuntimeStorageChanged.emit()
        self.qwenRuntimeErrorChanged.emit()
        self.qwenModelsChanged.emit()
        # Selecting the variant an armed Studio offer names satisfies it — a
        # satisfied offer disarms instead of waiting for a refusal to recur.
        self._disarm_satisfied_regen_offer()
        self.refreshProfileState()
        return True

    @Slot()
    def refreshQwenState(self) -> None:
        """Re-inspect the runtime + both models off the GUI thread (retry seam)."""
        self._start_qwen_inspection()

    def _start_qwen_inspection(
        self, *, profile: EngineId | None = None, generation: int = 0
    ) -> None:
        """Inspect the pinned runtime and every Qwen model install.

        One background job: the hardware probe that resolves ``auto`` is the
        expensive part, so the active profile's device, the Qwen device, the
        pinned runtime variant and both model statuses are resolved together
        and published on the GUI thread. ``generation`` is the profile
        generation this pass belongs to (0 = a standalone Qwen refresh).
        """
        if self._qwen_operation is not None and self._qwen_operation != "inspect":
            # An install/import/removal owns the lane: a refresh would race it.
            # A newer INSPECT supersedes an older one instead (bumping the
            # generation drops the older result, which is exactly what a
            # profile switch needs while a previous pass is still in flight).
            return
        self._qwen_generation += 1
        qwen_generation = self._qwen_generation
        self._qwen_operation = "inspect"
        hardware_probe = self._hardware_probe
        gguf = self._is_gguf_selected()
        device_setting = self._qwen_device_pref()
        active = profile or self._active_profile
        profile_generation = generation or self._profile_generation
        # The device the CARDS describe is always a Qwen device, even while
        # VieNeu is active — the cards exist to install Qwen before switching.
        qwen_profile = (
            active if engine_profiles.is_qwen_profile(active) else engine_profiles.QWEN_CUSTOM
        )
        if gguf:
            # The matrix is per-variant: every profile × quantization pair is
            # a separately verified install, so all four rows are inspected —
            # not just the selected one.
            model_keys = [
                qwen_gguf_models_manifest.variant_key_for(
                    engine_profiles.runtime_key(p), quantization
                )
                for p in engine_profiles.list_profiles()
                if engine_profiles.is_qwen_profile(p)
                for quantization in qwen_variants.GGUF_QUANTIZATIONS
            ]
        else:
            model_keys = [
                engine_profiles.runtime_key(p)
                for p in engine_profiles.list_profiles()
                if engine_profiles.is_qwen_profile(p)
            ]
        # An INJECTED factory resolves its own install (tests pin a fake);
        # the default one reads `_qwen_runtime_key`, which is still unset on
        # the first pass whenever the device choice is `auto` — so the pass
        # resolves the manager from the key it just computed instead, or a
        # machine with the pinned runtime installed would be reported as an
        # unsupported platform and never become ready.
        injected = self._qwen_runtime_factory_injected
        injected_factory = self._qwen_runtime_manager_factory
        gguf_runtime_factory = self._qwen_gguf_runtime_manager_factory
        data_dir = self._data_dir

        def work() -> tuple[int, int, str, str, str, Any | None, dict[str, Any], HardwareInfo]:
            try:
                hardware = hardware_probe()
            except Exception:  # noqa: BLE001 - an unknown host must not fail the cards
                logger.warning("hardware probe failed for the Qwen cards", exc_info=True)
                hardware = HardwareInfo(kind="none", torch_installed=False, cuda_version=None)
            if gguf:
                # ggml vocabulary: ``auto`` resolves to the best installable
                # cell (a matrix entry without a published pack never wins),
                # and the runtime manager is keyed by that cell.
                qwen_device = _resolve_gguf_device(hardware, device_setting)
                key = self._qwen_key_for(qwen_device, device_setting)
                manager = gguf_runtime_factory(data_dir, key) if key else None
            else:
                qwen_device = resolve_profile_device(qwen_profile, hardware, device_setting)
                key = self._qwen_key_for(qwen_device, device_setting)
                manager = (
                    injected_factory(data_dir)
                    if injected
                    else _qwen_runtime_manager_for_key(key, data_dir)
                )
            runtime = (
                _inspect_readiness(manager)
                if manager is not None
                else ProfileReadiness(state="unsupported", ready=False)
            )
            models: dict[str, Any] = {}
            for model_key in model_keys:
                try:
                    status = self._qwen_model_manager(model_key).inspect()
                except Exception as exc:  # noqa: BLE001 - the UI needs a state
                    logger.warning("Qwen model inspection failed: %s", exc)
                    models[model_key] = ProfileReadiness(state="failed", error=str(exc))
                else:
                    models[model_key] = _readiness_from(status)
            return (
                qwen_generation,
                profile_generation,
                active,
                qwen_device,
                key,
                runtime,
                models,
                hardware,
            )

        def on_done(result: Any) -> None:
            (
                gen,
                profile_gen,
                done_profile,
                qwen_device,
                key,
                runtime,
                models,
                hardware,
            ) = _unwrap_bg_result(result)
            if gen != self._qwen_generation or self._qwen_operation != "inspect":
                return
            if done_profile != self._active_profile:
                return  # a stale pass for a profile the user already left
            self._qwen_operation = None
            self._hardware_status = hardware
            self._publish_qwen_device(qwen_device)
            self._publish_qwen_runtime_key(key)
            self._publish_qwen_runtime(runtime)
            self._publish_qwen_models(models)
            if engine_profiles.is_qwen_profile(done_profile):
                self._publish_profile_device(qwen_device, profile_gen)
            else:
                # VieNeu's own device comes from the same probe.
                self._publish_profile_device(
                    resolve_profile_device(done_profile, hardware, device_setting),
                    profile_gen,
                )

        def on_error(exc: BaseException) -> None:
            if qwen_generation != self._qwen_generation:
                return
            logger.warning("Qwen inspection failed: %s", exc)
            self._qwen_operation = None
            self._publish_qwen_runtime(None, error=str(exc))
            self.qwenModelsChanged.emit()

        self._run_bg(work, on_done, self, on_error=on_error)

    def _qwen_key_for(self, device: str, device_setting: str) -> str:
        """The pinned key for a resolved device (explicit choice wins).

        The keyspace follows the selected format: official resolves a
        wheel-matrix platform key (``linux-x64-cuda``); GGUF resolves a
        qwentts.cpp pack cell (``linux-x64-cpu``) in ggml vocabulary.
        """
        picked = device_setting if device_setting != "auto" else device
        if self._is_gguf_selected():
            return qwen_gguf_manifest.host_cell_key(picked) or ""
        return qwen_manifest.host_platform_key(picked) or ""

    def _publish_qwen_device(self, device: str) -> None:
        """Publish the resolved Qwen device (the active profile's readout is
        published separately by :meth:`_publish_profile_device`)."""
        if not device:
            return
        changed = device != self._qwen_resolved_device
        self._qwen_resolved_device = device
        if changed:
            self.qwenDeviceChanged.emit()

    def _publish_qwen_runtime_key(self, key: str) -> None:
        if key == self._qwen_runtime_key:
            return
        self._qwen_runtime_key = key
        self.qwenRuntimeSupportChanged.emit()

    def _publish_qwen_runtime(self, readiness: ProfileReadiness | None, *, error: str = "") -> None:
        """Publish the runtime card + the profile view from one inspection."""
        if error and readiness is None:
            readiness = ProfileReadiness(state="failed", error=error)
        self._qwen_runtime_status = readiness
        if readiness is not None and engine_profiles.is_qwen_profile(self._active_profile):
            self._publish_profile_runtime(readiness)
        self.qwenRuntimeStateChanged.emit()
        self.qwenRuntimeProgressChanged.emit()
        self.qwenRuntimeStorageChanged.emit()
        self.qwenRuntimeErrorChanged.emit()

    def _publish_incomplete_runtime(self) -> None:
        """Fold a runtime-incomplete host failure into the runtime card.

        The job banner carries the message; the runtime card is where the user
        can act on it, so a load failure caused by the RUNTIME (a promoted
        runtime that cannot import its stack) is published there too — Repair is
        already enabled on it — instead of leaving the card claiming a ready
        runtime and letting the next job fail the same way. Model and device
        failures are left alone: those are fixed by another model or profile,
        not by reinstalling the runtime.
        """
        engine = self._engine
        code_reader = getattr(engine, "last_error_code", None)
        if not callable(code_reader):
            return
        from vienetts_app.core.qwen_protocol import (  # noqa: PLC0415 - lazy seam
            RUNTIME_INCOMPLETE_CODE,
        )

        if str(code_reader() or "") != RUNTIME_INCOMPLETE_CODE:
            return
        message_reader = getattr(engine, "last_error_message", None)
        message = str(message_reader() or "") if callable(message_reader) else ""
        self._publish_qwen_runtime(
            ProfileReadiness(
                state="failed",
                error=message or "the managed Qwen runtime is incomplete",
            )
        )

    def _qwen_status_key_for(self, profile: EngineId) -> str:
        """The model-status key the ACTIVE selection resolves ``profile`` to.

        Under GGUF the readiness that gates the profile is the selected
        quantization's row — a ready Q8_0 install must never mark a Q4_K_M
        selection ready, nor the reverse.
        """
        profile_key = engine_profiles.runtime_key(profile)
        if self._is_gguf_selected():
            return qwen_gguf_models_manifest.variant_key_for(
                profile_key, self._settings.qwen_gguf_quantization
            )
        return profile_key

    def _publish_qwen_models(self, statuses: dict[str, Any]) -> None:
        """Publish the model rows + the active Qwen profile's readiness."""
        self._qwen_model_statuses = dict(statuses)
        self.qwenModelsChanged.emit()
        if not engine_profiles.is_qwen_profile(self._active_profile):
            return
        readiness = statuses.get(self._qwen_status_key_for(self._active_profile))
        if isinstance(readiness, ProfileReadiness):
            self._publish_profile_model(readiness)

    def _begin_qwen_operation(self, name: str) -> tuple[int, threading.Event] | None:
        """Claim the single Qwen lane (``None`` = one is already running)."""
        if self._qwen_operation is not None:
            return None
        self._qwen_generation += 1
        self._qwen_operation = name
        self._qwen_cancel = threading.Event()
        return self._qwen_generation, self._qwen_cancel

    def _on_qwen_status_signal(self, payload: object) -> None:
        """Progress from the background lane (tagged by generation)."""
        try:
            generation, kind, key, status = payload  # type: ignore[misc]
        except (TypeError, ValueError):
            return
        if generation != self._qwen_generation:
            return
        if kind == "runtime":
            self._publish_qwen_runtime(_readiness_from(status))
            return
        statuses = dict(self._qwen_model_statuses)
        statuses[str(key)] = _readiness_from(status)
        self._qwen_models_busy = str(key)
        self._publish_qwen_models(statuses)

    def _qwen_runtime_readiness(self, state: str, *, error: str = "") -> ProfileReadiness:
        """Runtime readiness for a state this controller sets itself.

        The card pairs the progress bar with "downloaded N of M", so bytes
        already verified on disk (a resumed install) must carry their ratio:
        publishing them with a zero progress left the bar empty next to a
        non-zero size, which reads as a stalled download.
        """
        installed = self.qwenRuntimeInstalledBytes
        required = self.qwenRuntimeRequiredBytes
        return ProfileReadiness(
            state=state,
            installed_bytes=installed,
            required_bytes=required,
            progress=min(installed / required, 1.0) if required else 0.0,
            error=error,
        )

    def _qwen_runtime_work(self, name: str, action: str, *, start_state: str) -> None:
        """Run one runtime manager call on the shared lane with progress.

        ``action`` is the semantic operation; the GGUF pack manager spells
        its online install ``install_online`` (offline packs go through
        ``install_from_offline_pack`` like the official one), so the concrete
        method is resolved against the selected format's API.
        """
        manager = self._qwen_runtime_manager()
        if manager is None:
            self._set_error(self.tr("Nền tảng này không có runtime Qwen được hỗ trợ."))
            self.qwenRuntimeSupportChanged.emit()
            return
        call_name = action
        if self._is_gguf_selected() and action == "install":
            call_name = "install_online"
        claim = self._begin_qwen_operation(name)
        if claim is None:
            return
        generation, cancelled = claim
        if start_state:
            self._publish_qwen_runtime(self._qwen_runtime_readiness(start_state))

        def work() -> ProfileReadiness:
            call = getattr(manager, call_name)
            try:
                if action == "remove":
                    # remove() takes no cancellation/progress arguments.
                    status = call()
                else:
                    status = call(
                        cancelled=cancelled.is_set,
                        on_progress=lambda s: self._qwen_status_signal.emit(
                            (generation, "runtime", "", s)
                        ),
                    )
            except Exception as exc:  # noqa: BLE001 - failures are UI state
                return ProfileReadiness(state="failed", error=str(exc))
            return _readiness_from(status)

        def on_done(readiness: ProfileReadiness) -> None:
            if generation != self._qwen_generation:
                return
            self._qwen_operation = None
            self._publish_qwen_runtime(readiness)
            if self._active_profile != engine_profiles.VIENEU:
                # The engine's runtime location may have changed under it.
                self.refreshProfileState()

        def on_error(exc: BaseException) -> None:
            if generation != self._qwen_generation:
                return
            logger.warning("Qwen runtime %s failed: %s", action, exc)
            self._qwen_operation = None
            self._publish_qwen_runtime(ProfileReadiness(state="failed", error=str(exc)))

        self._run_bg(work, on_done, self, on_error=on_error)

    @Slot()
    def installQwenRuntime(self) -> None:
        """Download + verify the pinned runtime variant for the chosen device."""
        self._qwen_runtime_work("runtime-install", "install", start_state="downloading")

    @Slot()
    def repairQwenRuntime(self) -> None:
        """Re-install a failed/corrupt runtime, resuming valid archives."""
        self._qwen_runtime_work("runtime-repair", "repair", start_state="downloading")

    @Slot()
    def cancelQwenRuntimeInstall(self) -> None:
        """Request cooperative cancellation and drop queued callbacks."""
        if self._qwen_operation not in ("runtime-install", "runtime-repair"):
            return
        self._qwen_cancel.set()
        self._qwen_generation += 1
        self._qwen_operation = None
        self._publish_qwen_runtime(self._qwen_runtime_readiness("unavailable"))

    @Slot()
    def removeQwenRuntime(self) -> None:
        """Remove the managed runtime when no Qwen engine has loaded it.

        A built Qwen engine means a live model host holds the runtime
        directory, so removal is refused with the manager's own reason (the
        user can switch to VieNeu, which tears the host down, and retry).
        """
        manager = self._qwen_runtime_manager()
        if manager is None:
            self._set_error(self.tr("Nền tảng này không có runtime Qwen được hỗ trợ."))
            self.qwenRuntimeSupportChanged.emit()
            return
        in_use = self._engine is not None and engine_profiles.is_qwen_profile(self._active_profile)
        claim = self._begin_qwen_operation("runtime-remove")
        if claim is None:
            return
        generation, _cancelled = claim

        def work() -> ProfileReadiness:
            try:
                status = manager.remove(in_use=in_use)
            except Exception as exc:  # noqa: BLE001 - failures are UI state
                return ProfileReadiness(state="failed", error=str(exc))
            return _readiness_from(status)

        def on_done(readiness: ProfileReadiness) -> None:
            if generation != self._qwen_generation:
                return
            self._qwen_operation = None
            self._publish_qwen_runtime(readiness)
            if self._active_profile != engine_profiles.VIENEU:
                self.refreshProfileState()

        def on_error(exc: BaseException) -> None:
            if generation != self._qwen_generation:
                return
            logger.warning("Qwen runtime removal failed: %s", exc)
            self._qwen_operation = None
            self._publish_qwen_runtime(ProfileReadiness(state="failed", error=str(exc)))

        self._run_bg(work, on_done, self, on_error=on_error)

    @Slot(str)
    def importQwenRuntimePack(self, source: str) -> None:
        """Install the runtime from a verified offline wheel pack (no network)."""
        clean = normalize_local_path(source)
        if is_empty_path(clean):
            self._publish_qwen_runtime(
                self._qwen_runtime_readiness(
                    self.qwenRuntimeState,
                    error=self.tr("Chọn thư mục chứa các tệp wheel của runtime Qwen."),
                )
            )
            return
        manager = self._qwen_runtime_manager()
        if manager is None:
            self._set_error(self.tr("Nền tảng này không có runtime Qwen được hỗ trợ."))
            return
        claim = self._begin_qwen_operation("runtime-import")
        if claim is None:
            return
        generation, cancelled = claim
        self._publish_qwen_runtime(self._qwen_runtime_readiness("downloading"))

        def work() -> ProfileReadiness:
            try:
                status = manager.install_from_offline_pack(
                    Path(clean),
                    cancelled=cancelled.is_set,
                    on_progress=lambda s: self._qwen_status_signal.emit(
                        (generation, "runtime", "", s)
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - failures are UI state
                return ProfileReadiness(state="failed", error=str(exc))
            return _readiness_from(status)

        def on_done(readiness: ProfileReadiness) -> None:
            if generation != self._qwen_generation:
                return
            self._qwen_operation = None
            self._publish_qwen_runtime(readiness)
            self.refreshProfileState()

        def on_error(exc: BaseException) -> None:
            if generation != self._qwen_generation:
                return
            logger.warning("Qwen offline runtime import failed: %s", exc)
            self._qwen_operation = None
            self._publish_qwen_runtime(ProfileReadiness(state="failed", error=str(exc)))

        self._run_bg(work, on_done, self, on_error=on_error)

    def _qwen_model_work(self, key: str, action: str, *, start_state: str) -> None:
        """Run one model manager call on the shared lane with progress.

        ``key`` is the row key the operation addresses — a bare profile key
        under official, a ``{profile}-{quantization}`` variant key under
        GGUF — and the manager is resolved from the key itself, so the call
        always lands on the install it names.
        """
        claim = self._begin_qwen_operation(f"model-{action}:{key}")
        if claim is None:
            return
        generation, cancelled = claim
        self._qwen_models_busy = key
        if start_state:
            statuses = dict(self._qwen_model_statuses)
            statuses[key] = ProfileReadiness(state=start_state)
            self._publish_qwen_models(statuses)

        def work() -> ProfileReadiness:
            manager = self._qwen_model_manager(key)
            call = getattr(manager, action)
            try:
                if action == "remove":
                    # remove() takes no cancellation/progress arguments; the
                    # shared tree goes only when no other install needs it.
                    # The GGUF manager spells the flag ``drop_shared``.
                    in_use, drop_shared = self._qwen_model_removal_flags(key)
                    if qwen_gguf_models_manifest.parse_variant_key(key) is not None:
                        status = call(in_use=in_use, drop_shared=drop_shared)
                    else:
                        status = call(in_use=in_use, remove_shared=drop_shared)
                else:
                    status = call(
                        cancelled=cancelled.is_set,
                        on_progress=lambda s: self._qwen_status_signal.emit(
                            (generation, "model", key, s)
                        ),
                    )
            except Exception as exc:  # noqa: BLE001 - failures are UI state
                return ProfileReadiness(state="failed", error=str(exc))
            return _readiness_from(status)

        def on_done(readiness: ProfileReadiness) -> None:
            if generation != self._qwen_generation:
                return
            self._qwen_operation = None
            self._qwen_models_busy = ""
            statuses = dict(self._qwen_model_statuses)
            statuses[key] = readiness
            self._publish_qwen_models(statuses)
            self.refreshProfileState()

        def on_error(exc: BaseException) -> None:
            if generation != self._qwen_generation:
                return
            logger.warning("Qwen model %s failed for %s: %s", action, key, exc)
            self._qwen_operation = None
            self._qwen_models_busy = ""
            statuses = dict(self._qwen_model_statuses)
            statuses[key] = ProfileReadiness(state="failed", error=str(exc))
            self._publish_qwen_models(statuses)

        self._run_bg(work, on_done, self, on_error=on_error)

    @Slot(str)
    def installQwenModel(self, profile_key: str) -> None:
        """Download + verify one Qwen checkpoint (shared files reused)."""
        if not self._qwen_model_key(profile_key):
            return
        self._qwen_model_work(profile_key, "install", start_state="downloading")

    @Slot(str)
    def repairQwenModel(self, profile_key: str) -> None:
        """Re-install one Qwen checkpoint, keeping files that still verify."""
        if not self._qwen_model_key(profile_key):
            return
        self._qwen_model_work(profile_key, "repair", start_state="downloading")

    @Slot(str)
    def cancelQwenModelDownload(self, profile_key: str) -> None:
        """Request cooperative cancellation of one model download."""
        if self._qwen_models_busy != profile_key:
            return
        self._qwen_cancel.set()
        self._qwen_generation += 1
        self._qwen_operation = None
        self._qwen_models_busy = ""
        statuses = dict(self._qwen_model_statuses)
        statuses[profile_key] = ProfileReadiness(state="unavailable")
        self._publish_qwen_models(statuses)

    @Slot(str)
    def removeQwenModel(self, profile_key: str) -> None:
        """Remove one Qwen checkpoint (shared files kept while still used)."""
        if not self._qwen_model_key(profile_key):
            return
        self._qwen_model_work(profile_key, "remove", start_state="")

    @Slot(str, str)
    def importQwenModelPack(self, profile_key: str, source: str) -> None:
        """Install one Qwen checkpoint from a verified offline pack."""
        if not self._qwen_model_key(profile_key):
            return
        clean = normalize_local_path(source)
        if is_empty_path(clean):
            statuses = dict(self._qwen_model_statuses)
            statuses[profile_key] = ProfileReadiness(
                state=str(getattr(statuses.get(profile_key), "state", "") or "unavailable"),
                error=self.tr("Chọn thư mục chứa mô hình Qwen ngoại tuyến."),
            )
            self._publish_qwen_models(statuses)
            return
        claim = self._begin_qwen_operation(f"model-import:{profile_key}")
        if claim is None:
            return
        generation, cancelled = claim
        self._qwen_models_busy = profile_key
        statuses = dict(self._qwen_model_statuses)
        statuses[profile_key] = ProfileReadiness(state="downloading")
        self._publish_qwen_models(statuses)

        def work() -> ProfileReadiness:
            manager = self._qwen_model_manager(profile_key)
            # The GGUF manager spells its offline entry point ``install_offline``.
            call = (
                manager.install_offline
                if qwen_gguf_models_manifest.parse_variant_key(profile_key) is not None
                else manager.install_offline_pack
            )
            try:
                status = call(
                    Path(clean),
                    cancelled=cancelled.is_set,
                    on_progress=lambda s: self._qwen_status_signal.emit(
                        (generation, "model", profile_key, s)
                    ),
                )
            except Exception as exc:  # noqa: BLE001 - failures are UI state
                return ProfileReadiness(state="failed", error=str(exc))
            return _readiness_from(status)

        def on_done(readiness: ProfileReadiness) -> None:
            if generation != self._qwen_generation:
                return
            self._qwen_operation = None
            self._qwen_models_busy = ""
            statuses = dict(self._qwen_model_statuses)
            statuses[profile_key] = readiness
            self._publish_qwen_models(statuses)
            self.refreshProfileState()

        def on_error(exc: BaseException) -> None:
            if generation != self._qwen_generation:
                return
            logger.warning("Qwen offline model import failed for %s: %s", profile_key, exc)
            self._qwen_operation = None
            self._qwen_models_busy = ""
            statuses = dict(self._qwen_model_statuses)
            statuses[profile_key] = ProfileReadiness(state="failed", error=str(exc))
            self._publish_qwen_models(statuses)

        self._run_bg(work, on_done, self, on_error=on_error)

    def _qwen_model_removal_flags(self, key: str) -> tuple[bool, bool]:
        """``(in_use, drop_shared)`` for removing one model row.

        A built Qwen engine holds the ACTIVE selection's model, so exactly
        that variant cannot be deleted under it — a sibling quantization, the
        other profile, or the other format's install all stay removable (the
        engine id pins which format the live engine belongs to). The shared
        codec/tokenizer tree goes only once no other install of the same
        format still references it. Runs on the background lane, where the
        on-disk truth is read instead of the (possibly stale) UI statuses.
        """
        gguf_key = qwen_gguf_models_manifest.parse_variant_key(key) is not None
        in_use = False
        engine = self._engine
        if engine is not None and engine_profiles.is_qwen_profile(self._active_profile):
            engine_gguf = getattr(engine, "engine_id", "") == qwen_variants.ENGINE_QWENTTS_CPP
            in_use = engine_gguf == gguf_key and (
                key == self._qwen_status_key_for(self._active_profile)
            )
        if gguf_key:
            root = self._data_dir / "qwen" / "gguf-models"
            quantization = qwen_gguf_models_manifest.parse_variant_key(key)[1]  # type: ignore[index]
            try:
                others = {
                    variant
                    for variant in qwen_gguf_model_store.installed_variants(root)
                    if variant.endswith(f"-{quantization}")
                } - {key}
            except Exception:  # noqa: BLE001 - unreadable root: keep the codec
                logger.warning("could not read installed GGUF variants", exc_info=True)
                return in_use, False
            return in_use, not others
        root = self._data_dir / "qwen" / "models"
        try:
            others = set(qwen_model_manager.installed_profiles(root)) - {key}
        except Exception:  # noqa: BLE001 - unreadable root: keep the shared tree
            logger.warning("could not read installed Qwen profiles", exc_info=True)
            return in_use, False
        return in_use, not others

    def _qwen_model_key(self, profile_key: str) -> str:
        """Validate a model row key from QML ("" = refused, error already set).

        Both keyspaces are accepted: a bare profile key names an official
        checkpoint, a ``{profile}-{quantization}`` key names one verified
        GGUF variant — the key, not the current selection, picks the manager.
        """
        key = str(profile_key or "").strip()
        if key in {engine_profiles.runtime_key(p) for p in engine_profiles.list_profiles()}:
            return key
        if qwen_gguf_models_manifest.parse_variant_key(key) is not None:
            return key
        self._set_error(self.tr("Hồ sơ Qwen không hợp lệ: {}").format(key or "(trống)"))
        return ""

    @Slot(result=bool)
    def openQwenRuntimeDir(self) -> bool:
        """Create (if needed) and reveal the managed Qwen runtime dir."""
        return self._reveal_dir(
            self.qwenRuntimeStoragePath,
            self.tr("Không mở được thư mục runtime Qwen: {}"),
        )

    @Slot(result=bool)
    def openQwenModelDir(self) -> bool:
        """Create (if needed) and reveal the shared Qwen model dir."""
        return self._reveal_dir(
            self.qwenModelStoragePath,
            self.tr("Không mở được thư mục mô hình Qwen: {}"),
        )

    @Slot()
    def refreshModelState(self) -> None:
        """Re-inspect the managed install off the GUI thread (retry seam)."""
        self._model_generation += 1
        generation = self._model_generation
        manager = self._model_manager

        def work() -> tuple[int, ModelStatus]:
            return (generation, manager.inspect())

        def on_done(result: tuple[int, ModelStatus]) -> None:
            gen, status = _unwrap_bg_result(result)
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

        def on_error(exc: BaseException) -> None:
            if generation != self._model_generation:
                return
            logger.warning("model download failed: %s", exc)
            self._publish_model_status(ModelStatus(state="failed", error=str(exc), location=None))
            self._model_downloading = False

        self._run_bg(work, on_done, self, on_error=on_error)

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
        context: SynthesisContext | None = None,
    ) -> str | None:
        """Submit a listener-owned stream-mode synthesis job.

        Registers ``listener`` for exactly one job and returns its job ID;
        returns ``None`` without registering anything when validation or
        worker admission fails. Listener jobs never touch the foreground
        action state (``busy`` stays false) — the worker serializes them
        behind/in front of interactive jobs and tagged events route each
        delivery to its owner.

        ``context`` is a caller-held snapshot (the batch queue persists one per
        entry so a settings change cannot rewrite queued work); when it is
        omitted the context is built — and therefore validated — from the
        current profile, language and voice at submission time.
        """
        if listener is None:
            return None
        if not text or not text.strip():
            return None
        if context is None:
            context = self.submission_context_for(str(voice or ""))
            if context is None:
                return None
        try:
            request = TTSRequest(
                text=text,
                voice=context.voice_id or None,
                mode="stream",
                temperature=context.generation.temperature,
                speed=context.generation.speed,
                silence_p=context.generation.silence_p,
                context=context,
            )
        except ValueError as exc:
            self._set_error(self.tr("Yêu cầu không hợp lệ: {}").format(exc))
            return None
        job = new_synthesis_job("audiobook", kind, request)  # type: ignore[arg-type]
        job = replace(job, artifact_path=self._artifact_store.allocate(job.id))
        try:
            worker = self._ensure_worker()
        except Exception as exc:  # noqa: BLE001 - a refused engine is an actionable error
            logger.exception("failed to start the engine for a listener job")
            self._set_error(str(exc))
            return None
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
        context = self.submission_context_for(voice)
        if context is None:
            return  # the gate already reported the unsupported combination
        try:
            request = TTSRequest(
                text=text,
                voice=context.voice_id or None,
                mode=mode,  # type: ignore[arg-type]
                temperature=context.generation.temperature,
                speed=context.generation.speed,
                silence_p=context.generation.silence_p,
                context=context,
            )
        except ValueError as exc:
            self._set_error(self.tr("Yêu cầu không hợp lệ: {}").format(exc))
            return
        # The artifact this job produces carries this identity: Studio stamps it
        # on the clips it loads (a Qwen artifact must never read as VieNeu's).
        # Set only once the request itself is valid, so a rejected submission
        # cannot relabel a job that is already in flight.
        self._foreground_context = context
        job = new_synthesis_job(owner, "interactive", request)  # type: ignore[arg-type]
        job = replace(job, artifact_path=self._artifact_store.allocate(job.id))
        self._begin_foreground_trace(job_id=job.id, text=text, mode=mode)
        try:
            worker = self._begin_synthesis()
        except Exception as exc:
            logger.exception("failed to begin synthesis")
            # The trace already stamped this job id — release every foreground
            # marker, or the unadmitted job would keep profile switches
            # blocked (and an armed Studio splice waiting) forever.
            self._foreground_job_id = None
            self._foreground_is_voice_op = False
            self._foreground_live = False
            self._foreground_context = None
            self._set_foreground_job_state("idle")
            self.foregroundJobIdChanged.emit()
            self._set_error(self.tr("Không thể khởi động bộ tổng hợp giọng nói: {}").format(exc))
            self._set_busy(False)
            return
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
            self._foreground_context = None
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
        (``auditions/<profile>/<voice>_<language>_<speed>_<sample>.wav``) plays
        instantly; otherwise the fixed audition sample FOR THE RESOLVED
        LANGUAGE (``audition_sample_text``) is synthesized SILENTLY through
        the shared worker as an ``audition=True`` job (no live transport —
        chunks never reach the speaker) and played once from the finished
        file. The lane never flips busy, never touches progress, and never
        commits an artifact. No-op when a foreground synthesis owns the worker
        (busy) or the voice is blank.
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
        # The context resolves BEFORE the cache check: the file name is keyed
        # by the render identity itself, so a refused combination — or a file
        # another variant produced — can never be replayed for this request.
        context = self.submission_context_for(voice)
        if context is None:
            return
        cached = self._audition_cache_path(voice, context)
        if cached.is_file():
            self._set_audition_state(voice, "playing")
            self._play_audition_file(voice, cached)
            return
        sample_text = audition_sample_text(context.language)
        try:
            request = TTSRequest(
                text=sample_text,
                voice=context.voice_id or None,
                mode="stream",  # type: ignore[arg-type]
                temperature=context.generation.temperature,
                speed=context.generation.speed,
                silence_p=context.generation.silence_p,
                context=context,
            )
        except ValueError as exc:
            self._set_error(self.tr("Yêu cầu không hợp lệ: {}").format(exc))
            return
        job = new_synthesis_job("text", "interactive", request, audition=True)  # type: ignore[arg-type]
        job = replace(job, artifact_path=self._artifact_store.allocate(job.id))
        try:
            worker = self._ensure_worker()
        except Exception as exc:
            logger.exception("failed to initialize worker for audition")
            self._set_error(self.tr("Không thể khởi động bộ tổng hợp giọng nói: {}").format(exc))
            self._reset_audition_tracking()
            return
        self._set_audition_state(voice, "loading")
        self._audition_job_id = job.id
        self._audition_context = context
        self._performance.begin(
            job.id,
            {"char_count": len(sample_text), "mode": "stream", "streaming": True},
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

    def _audition_cache_path(self, voice: str, context: SynthesisContext | None = None) -> Path:
        """Cache file for one voice, keyed by the render identity itself.

        The file name carries a digest of the context's fingerprint payload —
        profile, model revision, language, voice/clone, generation settings
        and every stamped variant field (format, quantization, engine,
        resolved device, runtime/model/tokenizer identities) — plus a digest
        of the resolved audition sample, so re-wording a sample never replays
        stale audio. Temperature is the one generation field excluded: it only
        varies sampling noise, so auditions stay comparable and cache-stable
        across temperature tweaks. A refused combination answers a sentinel
        path the writer never targets — stale files are unreachable for it.
        """
        if context is None:
            context = self.submission_context_for(voice, report=False)
        safe = "".join(c if c.isalnum() or c in ("-", "_") else "_" for c in voice.strip())
        if context is None:
            return self._data_dir / AUDITION_CACHE_DIRNAME / "_refused" / f"{safe or 'voice'}.wav"
        code = context.language
        sample = hashlib.sha1(  # fixed non-crypto digest: invalidates stale cache keys
            audition_sample_text(code).encode("utf-8")
        ).hexdigest()[:8]
        payload = context.fingerprint_payload()
        generation = dict(payload.get("generation") or {})
        generation.pop("temperature", None)
        identity = hashlib.sha1(
            json.dumps(
                {**payload, "generation": generation},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:12]
        return (
            self._data_dir
            / AUDITION_CACHE_DIRNAME
            / str(context.profile)
            / f"{safe or 'voice'}_{identity}_{sample}.wav"
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
        self._audition_context = None
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
            try:
                refused = playback.play(str(path), on_released=self._on_audition_released)
            except TypeError:
                refused = playback.play(str(path))
        except Exception:  # noqa: BLE001 - file playback must never crash the UI
            logger.exception("audition playback failed")
            refused = False
        if refused is False:
            self._reset_audition_tracking()

    def _complete_audition(self, job_id: str, value: Any) -> None:
        """Audition synthesis done: cache + auto-play, never commit artifact."""
        self._performance.mark(job_id, "controller_done")
        self._performance.finish(job_id, "completed")
        if not isinstance(value, SynthesisArtifact) or value.job_id != job_id:
            self._fail_audition(job_id, self.tr("Tệp âm thanh không hợp lệ."))
        voice = self._audition_voice_id
        # The write keys by the SUBMITTED context — the identity this audio
        # was actually rendered under — never the settings of the moment.
        target = self._audition_cache_path(voice, self._audition_context)
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
            if self._busy:
                self._set_busy(False)
            return
        self._set_foreground_job_state("cancel_requested")
        self._performance.mark(job_id, "cancel_requested")
        if self._worker is not None:
            self._worker.cancel_job(job_id)
        else:
            self._set_foreground_job_state("cancelled")
            self._set_busy(False)
            self.cancelled.emit()
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
        try:
            worker = self._ensure_worker()
        except Exception as exc:  # noqa: BLE001 - warmup is best-effort by contract
            # A profile whose installs are incomplete refuses the engine build
            # (e.g. a Qwen profile without its managed runtime). The first real
            # submission surfaces the actionable error; the warmup just skips.
            logger.info("engine prewarm skipped: %s", exc)
            return
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
        return self._start_export(path, force_wav=True, default_format="wav")

    @Slot(str, result=bool)
    def exportAudio(self, path: str) -> bool:
        """Copy the committed artifact to ``path``; format follows its suffix.

        ``.mp3`` (any case) encodes MPEG Layer III via libsndfile — no ffmpeg
        needed; any other or missing suffix writes standard 16-bit PCM WAV
        (a missing suffix is completed with the ``exportFormat`` setting).
        Empty ``path`` exports to the timestamped default in that same setting
        format. Otherwise identical machinery to :meth:`exportWav`
        (off-thread encode, ``exportFinished(path, ok)`` on completion).
        """
        return self._start_export(
            path, force_wav=False, default_format=self._settings.export_format
        )

    def _start_export(self, path: str, *, force_wav: bool, default_format: str) -> bool:
        """Shared off-thread export behind :meth:`exportWav`/:meth:`exportAudio`."""
        from vienetts_app.core.audio import export_format_for

        artifact = self._current_artifact
        if artifact is None or not artifact.path.is_file():
            self._set_error(self.tr("Chưa có gì để xuất — hãy tổng hợp âm thanh trước."))
            return False
        if self._exporting:
            self._set_error(self.tr("Đang xuất một tệp khác — vui lòng đợi."))
            return False
        if path and path.strip():
            target = normalize_local_path(path)
            # Guard Windows-hostile basenames the Save dialog lets through
            # (reserved device names like CON/AUX, trailing dots); the parent
            # directory is never rewritten.
            target = target.parent / sanitize_filename(target.name, max_len=255)
            if force_wav:
                export_format = "wav"
            else:
                if target.suffix.lower() not in (".wav", ".mp3"):
                    target = target.parent / f"{target.name}.{default_format}"
                export_format = export_format_for(target)
        else:
            export_format = "wav" if force_wav else default_format
            target = self._default_export_path(export_format)
        source = artifact.path
        label = "MP3" if export_format == "mp3" else "WAV"

        def work() -> tuple[str, str]:
            try:
                if export_format == "mp3":
                    from vienetts_app.core.audio import export_audio_file

                    export_audio_file(source, target)
                else:
                    from vienetts_app.core.audio import export_wav_file

                    export_wav_file(source, target, subtype="PCM_16")
                return str(target), ""
            except PermissionError as exc:
                return "", self.tr("Tệp đang được sử dụng bởi ứng dụng khác: {}").format(exc)
            except OSError as exc:
                return "", self.tr("Xuất {} thất bại: {}").format(label, exc)
            except Exception as exc:  # noqa: BLE001
                return "", self.tr("Xuất {} thất bại: {}").format(label, exc)

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
                self._on_export_finished(_unwrap_bg_result(result))
            finally:
                release_once()

        def on_error(exc: BaseException) -> None:
            try:
                self._set_exporting(False)
                self._set_error(self.tr("Xuất WAV thất bại: {}").format(exc))
                self.exportFinished.emit("", False)
            finally:
                release_once()

        self._set_exporting(True)
        try:
            self._run_bg(work, done, self, on_error=on_error)
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

    # ── mini studio: polish + single-segment re-gen ──────────────────────────

    @Property(bool, notify=studioProjectChanged)
    def hasStudioProject(self) -> bool:
        return self._studio_project is not None

    @Property("QVariantList", notify=studioProjectChanged)
    def studioClips(self) -> list[dict[str, Any]]:
        project = self._studio_project
        if project is None:
            return []
        return [
            {
                "id": c.id,
                "label": c.label,
                "text": c.text,
                "duration": len(c.audio) / 48000.0 if len(c.audio) else 0.0,
                "duration_str": f"{len(c.audio) / 48000.0:.1f}s" if len(c.audio) else "0.0s",
                # Provenance: which engine produced this clip's audio ("" when
                # the audio predates engine provenance). The Studio UI shows it
                # and gates "Tạo lại" on it. For a Qwen clip the row also names
                # the weight variant — format, quantization and engine — so a
                # GGUF render is never displayed as an official-weights one.
                "profile": c.context.profile if c.context is not None else "",
                "profileLabel": (
                    engine_profiles.get_capabilities(c.context.profile).label
                    if c.context is not None
                    else ""
                ),
                "modelFormat": c.context.model_format if c.context is not None else "",
                "quantization": c.context.quantization if c.context is not None else "",
                "engine": c.context.engine if c.context is not None else "",
                "variantLabel": self._context_variant_label(c.context),
                "language": c.context.language if c.context is not None else "",
            }
            for c in project.clips
        ]

    @Property("QVariantList", notify=studioEnvelopeChanged)
    def studioEnvelope(self) -> list[float]:
        return list(self._studio_envelope)

    @Property("QVariantList", notify=studioProjectChanged)
    def studioOps(self) -> list[dict[str, Any]]:
        project = self._studio_project
        if project is None or not project.ops:
            return []
        from vienetts_app.core.studio import (
            CutOp,
            FadeOp,
            GainOp,
            GapOp,
            NormalizeOp,
            SilenceTrimOp,
            SpeedOp,
            TrimOp,
        )

        def range_label(start_frame: int, end_frame: int) -> str:
            """Frames are meaningless to a listener — label ranges in seconds."""
            end = self.tr("cuối") if end_frame == -1 else f"{end_frame / 48000:.2f}s"
            return f"{start_frame / 48000:.2f}s – {end}"

        ops_info: list[dict[str, Any]] = []
        for idx, op in enumerate(project.ops):
            if isinstance(op, GainOp):
                label = f"{op.db:+.1f} dB" if op.db != 0 else "0.0 dB"
                name = self.tr("Khuếch đại")
                desc = self.tr("Khuếch đại {}").format(label)
                kind = "gain"
            elif isinstance(op, FadeOp):
                edge_label = self.tr("vào") if op.edge == "in" else self.tr("ra")
                name = self.tr("Mờ dần")
                desc = self.tr("Mờ {} {} ms").format(edge_label, op.ms)
                kind = "fade"
            elif isinstance(op, SpeedOp):
                name = self.tr("Tốc độ")
                desc = self.tr("Tốc độ {:.2f}×").format(op.factor)
                kind = "speed"
            elif isinstance(op, NormalizeOp):
                name = self.tr("Chuẩn hóa")
                desc = self.tr("Chuẩn hóa đỉnh ({:.0%})").format(op.peak)
                kind = "normalize"
            elif isinstance(op, SilenceTrimOp):
                name = self.tr("Cắt lặng")
                desc = self.tr("Cắt khoảng lặng ({:.0f} dB)").format(op.threshold_db)
                kind = "silence"
            elif isinstance(op, GapOp):
                name = self.tr("Khoảng lặng")
                desc = self.tr("Khoảng lặng {} ms").format(op.ms)
                kind = "gap"
            elif isinstance(op, TrimOp):
                name = self.tr("Giữ đoạn")
                desc = self.tr("Giữ {}").format(range_label(op.start_frame, op.end_frame))
                kind = "trim"
            elif isinstance(op, CutOp):
                name = self.tr("Bỏ đoạn")
                desc = self.tr("Bỏ {}").format(range_label(op.start_frame, op.end_frame))
                kind = "cut"
            else:
                name = self.tr("Hiệu ứng")
                desc = str(op)
                kind = "custom"
            ops_info.append({"index": idx, "name": name, "desc": desc, "kind": kind})
        return ops_info

    @Property("QVariantMap", notify=studioControlsChanged)
    def studioControls(self) -> dict[str, float | int]:
        """The rack values the mix actually has (never the last op alone).

        ``effective_controls`` folds the op stack the same way
        ``render_project`` does — gain sums, speed multiplies — so the sliders
        cannot drift from the audio. Applying a rack value goes through
        ``set_parameter_op``, which replaces the previous op of that kind
        instead of stacking it, so the round trip is stable.
        """
        from vienetts_app.core.studio import effective_controls

        controls: dict[str, float | int] = {
            "gain": 0.0,
            "speed": 1.0,
            "gap": 500,
            "fade": 200,
            "fadeIn": 0,
            "fadeOut": 0,
        }
        project = self._studio_project
        if project is None:
            return controls
        controls.update(effective_controls(project))
        return controls

    @Property(int, notify=studioProjectChanged)
    def studioDurationMs(self) -> int:
        return self._studio_duration_ms

    @Property(str, notify=studioAuditionChanged)
    def studioClipPlayingId(self) -> str:
        """Id of the clip being auditioned, "" when the master mix is the target.

        The dock keys its waveform, duration and timecode off this so a clip
        audition never paints the whole-mix envelope under a clip-length label.
        """
        return self._studio_clip_playing_id

    @Property(int, notify=studioAuditionChanged)
    def studioClipDurationMs(self) -> int:
        return self._studio_clip_duration_ms

    @Property("QVariantList", notify=studioAuditionChanged)
    def studioClipEnvelope(self) -> list[float]:
        """Peak-normalized overview of the auditioned clip (QML bars)."""
        return list(self._studio_clip_envelope)

    def _set_studio_audition(self, clip_id: str, duration_ms: int, envelope: list[float]) -> None:
        """Publish (or clear) the clip audition state in one notification."""
        clip_id = clip_id or ""
        envelope = list(envelope) if clip_id else []
        if (
            clip_id == self._studio_clip_playing_id
            and int(duration_ms) == self._studio_clip_duration_ms
            and envelope == self._studio_clip_envelope
        ):
            return
        self._studio_clip_playing_id = clip_id
        self._studio_clip_duration_ms = int(duration_ms) if clip_id else 0
        self._studio_clip_envelope = envelope
        self.studioAuditionChanged.emit()

    def _clear_studio_audition(self) -> None:
        self._set_studio_audition("", 0, [])

    @Property(str, notify=studioProjectChanged)
    def studioRegenClipId(self) -> str:
        return self._studio_regen_clip_id or ""

    @Property(str, notify=studioRegenProfileChanged)
    def studioRegenProfile(self) -> str:
        """Profile a refused re-synthesis needs ("" = none pending).

        Set when ``studioRegenClip`` refused because the clip's audio came from
        another engine; the UI shows "chuyển sang <profile>" and calls
        ``studioSwitchToRegenProfile``. Cleared on a successful re-synthesis, on
        opening a project, and after the switch.
        """
        return self._studio_regen_profile

    def _context_variant_label(self, context: SynthesisContext | None) -> str:
        """The stamped variant's display label for a Qwen context ("" else).

        Same wording the pickers show — ``GGUF Q8_0 · qwentts.cpp`` for a
        quantized native render, the full-weights label for an official one —
        so a clip, a refusal banner and the Settings selection all spell the
        same identity the same way.
        """
        if context is None or not context.model_format:
            return ""
        if context.model_format == qwen_variants.MODEL_FORMAT_GGUF:
            return f"GGUF {context.quantization} · {qwen_variants.engine_label(context.engine)}"
        return self.tr("Trọng lượng đầy đủ ({})").format(
            qwen_variants.engine_label(context.engine or qwen_variants.ENGINE_PYTORCH)
        )

    @Property(str, notify=studioRegenProfileChanged)
    def studioRegenProfileLabel(self) -> str:
        """Display name of the armed offer ("" when none pending).

        Names the SELECTION the clip needs, not just the profile: a GGUF clip
        under an official selection arms "Qwen3-TTS … · GGUF Q8_0 · qwentts.cpp",
        so the switch button never promises a no-op same-profile move.
        """
        if not self._studio_regen_profile:
            return ""
        label = engine_profiles.get_capabilities(self._studio_regen_profile).label
        variant = self._context_variant_label(self._studio_regen_required)
        return f"{label} · {variant}" if variant else label

    def _set_studio_regen_profile(self, profile: str) -> None:
        profile = str(profile or "")
        if profile != self._studio_regen_profile:
            self._studio_regen_profile = profile
            self.studioRegenProfileChanged.emit()

    def _set_studio_regen_required(self, context: SynthesisContext | None) -> None:
        """Arm/clear the recorded context behind the regen offer.

        The label depends on it even when the profile id is unchanged
        (a same-profile variant mismatch), so a change re-emits the notify.
        """
        if context is not self._studio_regen_required:
            self._studio_regen_required = context
            self.studioRegenProfileChanged.emit()

    def _disarm_satisfied_regen_offer(self) -> None:
        """Drop the armed offer once the selection already matches it.

        A user who lands on the required profile+variant through Settings —
        or via the offer's own switch — leaves nothing armed: the banner only
        exists while the current selection would still be refused.
        """
        armed = self._studio_regen_profile
        if not armed or self._active_profile != armed:
            return
        required = self._studio_regen_required
        if required is not None and required.model_format:
            if self._settings.qwen_model_format != required.model_format:
                return
            if (
                required.model_format == qwen_variants.MODEL_FORMAT_GGUF
                and self._settings.qwen_gguf_quantization != required.quantization
            ):
                return
        self._studio_regen_required = None
        self._set_studio_regen_profile("")

    @Slot(result=bool)
    def studioSwitchToRegenProfile(self) -> bool:
        """Switch to the selection a refused re-synthesis needs.

        Restores the recorded profile AND its model format/quantization — a
        clip stamped ``GGUF Q4_K_M`` gets exactly that back, never whatever
        variant happens to be selected now. Returns False when nothing is
        pending or either step is refused (a running job blocks both) — the
        pending offer then stays on screen.
        """
        target = self._studio_regen_profile
        if not target:
            return False
        required = self._studio_regen_required
        if (
            required is not None
            and engine_profiles.is_qwen_profile(required.profile)
            and required.model_format
            and not self.setQwenVariant(required.model_format, required.quantization)
        ):
            return False
        if not self.switchEngineProfile(target):
            return False
        self._studio_regen_required = None
        self._set_studio_regen_profile("")
        return True

    @Property(bool, notify=studioBusyChanged)
    def studioBusy(self) -> bool:
        return self._studio_busy

    def _set_studio_busy(self, value: bool) -> None:
        if value != self._studio_busy:
            self._studio_busy = value
            self.studioBusyChanged.emit()

    @Property(str, notify=studioBusyChanged)
    def studioBusyKind(self) -> str:
        """Which studio action is rendering ("gain", "speed", …; "" when idle).

        Each Apply button spins only for its own kind — one shared boolean lit
        every spinner at once.
        """
        if not self._studio_busy or not self._studio_pending:
            return ""
        return self._studio_pending.get(self._studio_seq, "")

    def _studio_render_start(self, kind: str) -> int:
        """Track a new off-thread render; returns its generation."""
        self._studio_seq += 1
        seq = self._studio_seq
        self._studio_pending[seq] = kind
        self._set_studio_busy(True)
        self.studioBusyChanged.emit()
        return seq

    def _studio_render_settled(self, seq: int) -> bool:
        """Drop a finished generation; True when nothing is in flight."""
        self._studio_pending.pop(seq, None)
        if self._studio_pending:
            self.studioBusyChanged.emit()
            return False
        self._set_studio_busy(False)
        self.studioBusyChanged.emit()
        return True

    def _queue_studio_overview(
        self,
        project: Any,
        *,
        kind: str,
        play_after: bool = False,
        surface_errors: bool = False,
    ) -> bool:
        """Render one project snapshot, optionally writing and playing it."""
        seq = self._studio_render_start(kind)
        preview = self._data_dir / "studio_preview.wav"

        def work() -> tuple[int, int, list[float]]:
            from vienetts_app.core.studio import render_overview

            mix, duration_ms, envelope = render_overview(project)
            if play_after:
                from vienetts_app.core.audio import write_wav_file

                write_wav_file(mix, preview)
            return seq, duration_ms, envelope

        def on_done(result: Any) -> None:
            unwrapped = _unwrap_bg_result(result)
            rseq, duration_ms, envelope = unwrapped
            if rseq != self._studio_seq:
                self._studio_render_settled(rseq)
                return  # stale: a newer op already queued its own render
            self._studio_duration_ms = int(duration_ms)
            self._studio_envelope = list(envelope)
            self._studio_render_settled(rseq)
            self.studioProjectChanged.emit()
            self.studioEnvelopeChanged.emit()
            if play_after:
                self._start_preview_playback(str(preview), int(duration_ms))

        def on_error(exc: BaseException) -> None:
            if seq != self._studio_seq:
                self._studio_render_settled(seq)
                return
            self._studio_envelope = []
            self._studio_duration_ms = 0
            self._studio_render_settled(seq)
            self.studioProjectChanged.emit()
            self.studioEnvelopeChanged.emit()
            if surface_errors:
                message = (
                    str(exc)
                    if isinstance(exc, ValueError | OSError)
                    else self.tr("Nghe thử thất bại: {}").format(exc)
                )
                self._set_error(message)

        try:
            self._run_bg(work, on_done, self, on_error=on_error)
        except Exception as exc:  # noqa: BLE001 - pool rejection must not stick busy
            if seq != self._studio_seq:
                self._studio_render_settled(seq)
                return False
            self._studio_envelope = []
            self._studio_duration_ms = 0
            self._studio_render_settled(seq)
            self.studioProjectChanged.emit()
            self.studioEnvelopeChanged.emit()
            if surface_errors:
                self._set_error(self.tr("Nghe thử thất bại: {}").format(exc))
            return False
        return True

    def _emit_studio(self, *, kind: str = "") -> None:
        """Publish state now, then render its overview off the GUI thread.

        Rendering never auditions: an edit updates the waveform and the
        timecode, and the always-visible Nghe thử in the dock is the one
        deliberate way to hear the result (Apply and Undo behave alike).
        """
        project = self._studio_project
        if project is None:
            self._studio_seq += 1
            self._studio_pending.clear()
            self._studio_envelope = []
            self._studio_duration_ms = 0
            self._set_studio_busy(False)
            self.studioBusyChanged.emit()
            self.studioProjectChanged.emit()
            self.studioEnvelopeChanged.emit()
            self.studioControlsChanged.emit()
            return
        self.studioProjectChanged.emit()
        self.studioControlsChanged.emit()
        self._queue_studio_overview(project, kind=kind)

    def _invalidate_studio_preview(self) -> None:
        """Stop playback of a mix that is about to change.

        The rendered preview file is stale the moment an op is pushed, so
        playback has to end — and with it any clip audition, whose envelope and
        length describe audio that no longer exists.
        """
        self._studio_seq += 1
        if self._replay_active:
            self._stop_replay()
        self._clear_studio_audition()
        self._set_replay_duration_ms(0)
        self._studio_preview_path = ""

    def _require_studio(self) -> Any | None:
        if self._studio_project is None:
            self._set_error(self.tr("Chưa có dự án studio — hãy mở âm thanh trong Studio trước."))
            return None
        return self._studio_project

    @Slot(str, str, result=bool)
    def openInStudio(self, owner: str, text: str) -> bool:
        """Load the current artifact as a paragraph-clip project."""
        from vienetts_app.core.studio import load_project_from_artifact

        self._invalidate_studio_preview()
        artifact = self._current_artifact
        if artifact is None or not artifact.path.is_file():
            self._set_error(self.tr("Chưa có gì để xuất — hãy tổng hợp âm thanh trước."))
            return False
        try:
            self._studio_project = load_project_from_artifact(
                str(artifact.path), text or "", context=self._current_artifact_context
            )
        except ValueError as exc:
            self._set_error(str(exc))
            return False
        except OSError as exc:
            self._set_error(str(exc))
            return False
        self._reset_studio_regen()
        self._emit_studio()
        return True

    @Slot(str, int, result=bool)
    def openChapterInStudio(self, book_id: str, index: int) -> bool:
        """Load one rendered chapter as a single-clip project.

        The clip inherits the chapter's recorded render provenance, so Studio
        knows which engine produced the audio it is about to edit.
        """
        from vienetts_app.core.audiobook import AudiobookError, AudiobookLibrary
        from vienetts_app.core.studio import load_project_from_chapters

        self._invalidate_studio_preview()
        try:
            library = AudiobookLibrary(self._data_dir / "audiobooks")
            state = library.load_book(book_id)
        except AudiobookError as exc:
            self._set_error(str(exc))
            return False
        chapters = [c for c in state.chapters if c.index == index]
        if not chapters:
            self._set_error(self.tr("Không tìm thấy chương này trong sách."))
            return False
        try:
            self._studio_project = load_project_from_chapters(
                library, book_id, [index], [chapters[0].text], contexts=state.contexts
            )
        except (ValueError, OSError) as exc:
            self._set_error(str(exc))
            return False
        self._reset_studio_regen()
        self._emit_studio()
        return True

    def _reset_studio_regen(self) -> None:
        """Drop any armed/pending re-synthesis (a new project replaces it)."""
        self._studio_regen_clip_id = None
        self._studio_regen_clip_text = None
        self._studio_regen_context = None
        self._set_studio_regen_required(None)
        self._set_studio_regen_profile("")

    def _push_studio_op(self, op: Any) -> bool:
        from vienetts_app.core.studio import (
            FadeOp,
            GainOp,
            GapOp,
            NormalizeOp,
            SilenceTrimOp,
            SpeedOp,
            TrimOp,
            set_parameter_op,
        )

        project = self._require_studio()
        if project is None:
            return False
        self._invalidate_studio_preview()
        try:
            # Rack values are absolute settings, not deltas: re-applying gain
            # or speed replaces that setting instead of compounding it (the
            # sliders can only show one number, and it must be the true one).
            # One-shot edits (normalize/silence/trim) still append.
            self._studio_project = set_parameter_op(project, op)
        except ValueError as exc:
            self._set_error(str(exc))
            return False
        if isinstance(op, GainOp):
            kind = "gain"
        elif isinstance(op, FadeOp):
            kind = "fade"
        elif isinstance(op, NormalizeOp):
            kind = "normalize"
        elif isinstance(op, SpeedOp):
            kind = "speed"
        elif isinstance(op, SilenceTrimOp):
            kind = "silence"
        elif isinstance(op, GapOp):
            kind = "gap"
        elif isinstance(op, TrimOp):
            kind = "trim"
        else:
            kind = ""
        self._emit_studio(kind=kind)
        return True

    @Slot(float, result=bool)
    def studioPushGain(self, db: float) -> bool:
        from vienetts_app.core.studio import GainOp

        try:
            op: Any = GainOp(db=float(db))
        except ValueError as exc:
            self._set_error(str(exc))
            return False
        return self._push_studio_op(op)

    @Slot(str, int, result=bool)
    def studioPushFade(self, edge: str, ms: int) -> bool:
        from vienetts_app.core.studio import FadeOp

        try:
            op: Any = FadeOp(edge=edge, ms=int(ms))
        except ValueError as exc:
            self._set_error(str(exc))
            return False
        return self._push_studio_op(op)

    @Slot(result=bool)
    def studioPushNormalize(self) -> bool:
        from vienetts_app.core.studio import NormalizeOp

        return self._push_studio_op(NormalizeOp())

    @Slot(float, result=bool)
    def studioPushSpeed(self, factor: float) -> bool:
        from vienetts_app.core.studio import SpeedOp

        try:
            op: Any = SpeedOp(factor=float(factor))
        except ValueError as exc:
            self._set_error(str(exc))
            return False
        return self._push_studio_op(op)

    @Slot(result=bool)
    def studioPushSilenceTrim(self) -> bool:
        from vienetts_app.core.studio import SilenceTrimOp

        return self._push_studio_op(SilenceTrimOp())

    @Slot(int, result=bool)
    def studioPushGap(self, ms: int) -> bool:
        from vienetts_app.core.studio import GapOp

        try:
            op: Any = GapOp(ms=int(ms))
        except ValueError as exc:
            self._set_error(str(exc))
            return False
        return self._push_studio_op(op)

    @Slot(int, int, result=bool)
    def studioPushTrim(self, start: int, end: int) -> bool:
        from vienetts_app.core.studio import TrimOp

        try:
            op: Any = TrimOp(start_frame=int(start), end_frame=int(end))
        except ValueError as exc:
            self._set_error(str(exc))
            return False
        return self._push_studio_op(op)

    @Slot(str, int, result=bool)
    def studioMoveClip(self, clip_id: str, index: int) -> bool:
        from vienetts_app.core.studio import move_clip

        project = self._require_studio()
        if project is None:
            return False
        self._invalidate_studio_preview()
        try:
            self._studio_project = move_clip(project, clip_id, int(index))
        except ValueError as exc:
            self._set_error(str(exc))
            return False
        self._emit_studio()
        return True

    @Slot(result=bool)
    def studioUndo(self) -> bool:
        from vienetts_app.core.studio import pop_op

        project = self._require_studio()
        if project is None:
            return False
        try:
            self._studio_project = pop_op(project)
        except ValueError:
            return False
        self._invalidate_studio_preview()
        # Silent, exactly like Apply: the dock's Nghe thử is always on screen,
        # so auditioning is one deliberate click instead of a side effect that
        # only some of the edit actions have.
        self._emit_studio(kind="undo")
        return True

    @Slot(result=bool)
    def studioReset(self) -> bool:
        """Reset all applied studio operations back to the original audio."""
        from vienetts_app.core.studio import reset_ops

        project = self._require_studio()
        if project is None:
            return False
        if not project.ops:
            return False
        self._invalidate_studio_preview()
        self._studio_project = reset_ops(project)
        self._emit_studio(kind="reset")
        return True

    @Slot(int, result=bool)
    def studioRevertTo(self, index: int) -> bool:
        """Drop every op after ``index``; index < 0 returns to the original take.

        Backs the clickable op-stack chips: popping one step at a time is the
        only undo the history used to offer, so getting back five steps meant
        five clicks.
        """
        from vienetts_app.core.studio import StudioProject

        project = self._require_studio()
        if project is None:
            return False
        keep = int(index) + 1
        if keep >= len(project.ops):
            return False  # nothing would change
        self._invalidate_studio_preview()
        self._studio_project = StudioProject(clips=project.clips, ops=project.ops[: max(0, keep)])
        self._emit_studio(kind="revert")
        return True

    @Slot(str, result=bool)
    def studioDeleteClip(self, clip_id: str) -> bool:
        """Drop one clip from the project (the last clip cannot be deleted)."""
        from vienetts_app.core.studio import delete_clip

        project = self._require_studio()
        if project is None:
            return False
        self._invalidate_studio_preview()
        try:
            self._studio_project = delete_clip(project, clip_id)
        except ValueError as exc:
            self._set_error(str(exc))
            return False
        self._emit_studio(kind="delete")
        return True

    def _push_studio_range(self, start_ms: int, end_ms: int, *, keep: bool) -> bool:
        """Shared body of the waveform-selection trims (ms → 48 kHz frames)."""
        from vienetts_app.core.studio import SAMPLE_RATE, cut_range, trim_range

        project = self._require_studio()
        if project is None:
            return False
        try:
            start = max(0, int(round(int(start_ms) * SAMPLE_RATE / 1000)))
            end = -1 if int(end_ms) < 0 else int(round(int(end_ms) * SAMPLE_RATE / 1000))
            self._studio_project = (
                trim_range(project, start, end) if keep else cut_range(project, start, end)
            )
        except (TypeError, ValueError) as exc:
            self._set_error(str(exc))
            return False
        self._invalidate_studio_preview()
        self._emit_studio(kind="trim" if keep else "cut")
        return True

    @Slot(int, int, result=bool)
    def studioPushTrimRange(self, start_ms: int, end_ms: int) -> bool:
        """Keep only the selected range of the rendered mix (end_ms < 0 = end)."""
        return self._push_studio_range(start_ms, end_ms, keep=True)

    @Slot(int, int, result=bool)
    def studioPushCutRange(self, start_ms: int, end_ms: int) -> bool:
        """Delete the selected range of the rendered mix (end_ms < 0 = end)."""
        return self._push_studio_range(start_ms, end_ms, keep=False)

    @Slot(result=bool)
    def studioPreview(self) -> bool:
        """Render the op stack to a temp file and play it (existing player).

        Render + WAV write run off the GUI thread behind ``studioBusy`` — on a
        long mix they cost seconds and used to freeze the window. Playback
        starts in ``on_done`` back on the GUI thread.
        """
        project = self._require_studio()
        if project is None:
            return False
        if self._studio_busy:
            self._set_error(self.tr("Đang xử lý studio — vui lòng đợi."))
            return False
        self._invalidate_studio_preview()
        return self._queue_studio_overview(
            project,
            kind="preview",
            play_after=True,
            surface_errors=True,
        )

    def _start_preview_playback(self, preview: str, duration_ms: int) -> None:
        """Play an already-rendered studio preview file (GUI thread)."""
        self._studio_preview_path = preview
        # The master deck is about to sound the whole mix, so any clip audition
        # state (clip envelope + clip length) is stale.
        self._clear_studio_audition()
        playback = self._file_playback
        if playback is None or not hasattr(playback, "play"):
            self._set_error(self.tr("Hệ thống này không phát được âm thanh."))
            return
        self._set_replay_active(True)
        self._set_replay_duration_ms(duration_ms)
        self._begin_replay_position(duration_ms)
        try:
            refused = playback.play(preview)
        except TypeError:
            try:
                refused = playback.play(preview)
            except Exception:  # noqa: BLE001 - preview must never crash the UI
                refused = False
        except Exception:  # noqa: BLE001 - preview must never crash the UI
            refused = False
        if refused is False:
            self._set_replay_active(False)
            self._end_replay_position()
            self._set_error(self.tr("Hệ thống này không phát được âm thanh."))

    @Slot(str, result=bool)
    def studioPreviewClip(self, clip_id: str) -> bool:
        """Play a single clip's audio through the player.

        Publishes the audition (``studioClipPlayingId`` + that clip's own
        envelope and length) so the transport dock switches from the master mix
        to the clip: the playhead, the timecode and the highlighted row then all
        describe the audio you are actually hearing.
        """
        from vienetts_app.core.audio import write_wav_file
        from vienetts_app.core.studio import envelope_for

        project = self._require_studio()
        if project is None:
            return False
        clips = [c for c in project.clips if c.id == clip_id]
        if not clips:
            self._set_error(self.tr("Không tìm thấy đoạn này trong Studio."))
            return False
        if self._replay_active:
            self._stop_replay()
        clip = clips[0]
        clip_audio = clip.audio
        preview = self._data_dir / f"studio_clip_{clip_id}.wav"
        try:
            write_wav_file(clip_audio, preview)
        except OSError as exc:
            self._set_error(str(exc))
            return False
        self._studio_preview_path = str(preview)
        playback = self._file_playback
        if playback is None or not hasattr(playback, "play"):
            self._set_error(self.tr("Hệ thống này không phát được âm thanh."))
            return False
        duration_ms = int(len(clip_audio) * 1000 / 48000)
        self._set_studio_audition(clip_id, duration_ms, envelope_for(clip_audio))
        self._set_replay_active(True)
        self._set_replay_duration_ms(duration_ms)
        self._begin_replay_position(duration_ms)
        try:
            refused = playback.play(str(preview))
        except Exception:  # noqa: BLE001 - preview must never crash the UI
            refused = False
        if refused is False:
            self._set_replay_active(False)
            self._end_replay_position()
            self._clear_studio_audition()
            self._set_error(self.tr("Hệ thống này không phát được âm thanh."))
            return False
        return True

    @Slot(str, result=bool)
    def studioExport(self, path: str) -> bool:
        """Render the op stack and export it (same WAV/MP3 dispatch as export).

        The full-mix render + staging WAV write used to run inside this slot —
        seconds of frozen UI on a long mix. Both now run in ``work`` off the
        GUI thread; only the target-path resolution stays inline.
        """
        from vienetts_app.core.audio import export_format_for

        project = self._require_studio()
        if project is None:
            return False
        if self._exporting:
            self._set_error(self.tr("Đang xuất một tệp khác — vui lòng đợi."))
            return False
        if self._studio_busy:
            self._set_error(self.tr("Đang xử lý studio — vui lòng đợi."))
            return False
        if path and path.strip():
            target = normalize_local_path(path)
            target = target.parent / sanitize_filename(target.name, max_len=255)
            if target.suffix.lower() not in (".wav", ".mp3"):
                target = target.parent / f"{target.name}.{self._settings.export_format}"
            export_format = export_format_for(target)
        else:
            export_format = self._settings.export_format
            target = self._default_export_path(export_format)
        snapshot = project
        source = self._data_dir / "studio_render.wav"
        label = "MP3" if export_format == "mp3" else "WAV"

        def work() -> tuple[str, str]:
            try:
                from vienetts_app.core.audio import write_wav_file
                from vienetts_app.core.studio import render_project

                mix = render_project(snapshot)
                write_wav_file(mix, source)
                if export_format == "mp3":
                    from vienetts_app.core.audio import export_audio_file

                    export_audio_file(source, target)
                else:
                    from vienetts_app.core.audio import export_wav_file

                    export_wav_file(source, target, subtype="PCM_16")
                return str(target), ""
            except ValueError as exc:
                return "", str(exc)
            except PermissionError as exc:
                return "", self.tr("Tệp đang được sử dụng bởi ứng dụng khác: {}").format(exc)
            except OSError as exc:
                return "", self.tr("Xuất {} thất bại: {}").format(label, exc)
            except Exception as exc:  # noqa: BLE001
                return "", self.tr("Xuất {} thất bại: {}").format(label, exc)

        def done(result: Any) -> None:
            self._on_export_finished(_unwrap_bg_result(result))

        def on_error(exc: BaseException) -> None:
            self._set_exporting(False)
            self._set_error(self.tr("Xuất WAV thất bại: {}").format(exc))
            self.exportFinished.emit("", False)

        self._set_exporting(True)
        try:
            self._run_bg(work, done, self, on_error=on_error)
        except Exception as exc:  # noqa: BLE001 - a rejected pool must not leak state
            self._set_exporting(False)
            self._set_error(self.tr("Xuất WAV thất bại: {}").format(exc))
            self.exportFinished.emit("", False)
            return False
        return True

    @Slot(str, str, result=bool)
    @Slot(str, str, str, result=bool)
    def studioRegenClip(self, clip_id: str, voice: str, new_text: str = "") -> bool:
        """Re-synthesize one clip's text through the shared worker (splice on done).

        Re-synthesis is the ONE Studio action that needs an engine, so it is the
        one that checks provenance: the clip's audio must have come from the
        active engine (``synthesis_context.same_engine``). A clip another
        profile produced — or one that predates provenance and therefore came
        from VieNeu — is refused with the profile it needs, and the app offers
        the switch (``studioRegenProfile`` / ``studioSwitchToRegenProfile``)
        instead of quietly re-synthesizing with a different engine. Editing and
        export stay engine-independent: ops never consult provenance.
        """
        project = self._require_studio()
        if project is None:
            return False
        clips = [c for c in project.clips if c.id == clip_id]
        if not clips:
            self._set_error(self.tr("Không tìm thấy đoạn này trong Studio."))
            return False
        if self._busy:
            self._set_error(self.tr("Đang tổng hợp — vui lòng đợi."))
            return False
        context = self.submission_context_for(voice)
        if context is None:
            return False  # the gate already reported the unsupported combination
        clip = clips[0]
        if not same_engine(clip.context, context):
            required = clip.context.profile if clip.context is not None else engine_profiles.VIENEU
            # Arm the clip's own recorded context, not just its profile id:
            # a same-profile format/quantization mismatch is refused here too
            # (same_engine), so the switch must restore the exact variant the
            # audio was rendered under — never the variant selected right now.
            self._set_studio_regen_required(clip.context)
            self._set_studio_regen_profile(required)
            label = engine_profiles.get_capabilities(required).label
            variant = self._context_variant_label(clip.context)
            if variant:
                label = f"{label} · {variant}"
            self._set_error(
                self.tr(
                    "Đoạn này được tạo bằng {profile}. Hãy chuyển sang hồ sơ đó để tạo lại."
                ).format(profile=label)
            )
            return False
        self._set_studio_regen_required(None)
        self._set_studio_regen_profile("")
        text_to_synth = new_text.strip() if new_text and new_text.strip() else clip.text
        self._studio_regen_clip_id = clip_id
        self._studio_regen_clip_text = text_to_synth
        self._studio_regen_context = context
        self.studioProjectChanged.emit()
        # A stale foregroundJobId from an earlier job must not pass for this
        # submission — only a NEW id proves a job was actually admitted.
        previous_job = self.foregroundJobId
        self.generateStream(text_to_synth, voice)
        if not self.foregroundJobId or self.foregroundJobId == previous_job:
            # The submission never admitted a job (closing worker, refused
            # engine, install gap): leave nothing armed, so a later synthesis
            # cannot be spliced into this clip.
            self._studio_regen_clip_id = None
            self._studio_regen_clip_text = None
            self._studio_regen_context = None
            self.studioProjectChanged.emit()
            return False
        return True

    def _maybe_splice_regen(self, value: Any) -> None:
        """Splice a finished regen job into its clip (10 ms crossfade)."""
        clip_id = self._studio_regen_clip_id
        new_text = self._studio_regen_clip_text
        context = self._studio_regen_context
        if clip_id is None:
            return
        self._studio_regen_clip_id = None
        self._studio_regen_clip_text = None
        self._studio_regen_context = None
        project = self._studio_project
        if project is None:
            return
        if not isinstance(value, SynthesisArtifact) or not value.path.is_file():
            self.studioProjectChanged.emit()
            return
        from vienetts_app.core.audio import read_wav
        from vienetts_app.core.studio import splice_clip_audio

        try:
            audio, sr = read_wav(value.path)
        except OSError:
            self.studioProjectChanged.emit()
            return
        if sr != 48_000:
            self.studioProjectChanged.emit()
            return
        try:
            self._studio_project = splice_clip_audio(
                project, clip_id, audio, new_text=new_text, context=context
            )
        except ValueError:
            self.studioProjectChanged.emit()
            return
        self._invalidate_studio_preview()
        self._emit_studio()

    def _default_export_path(self, format: str = "wav") -> Path:
        base = self._settings.output_dir.strip()
        stamp = _dt.datetime.now().strftime(EXPORT_PATTERN)
        if format == "mp3":
            stamp = stamp[: -len(".wav")] + ".mp3"
        if base:
            return Path(base) / stamp
        # QStandardPaths follows OneDrive redirection (common consumer
        # Windows) and localized XDG music dirs; ~/Music is the fallback for
        # headless/unknown-desktop runs where it resolves empty.
        music = QStandardPaths.writableLocation(QStandardPaths.StandardLocation.MusicLocation)
        return (Path(music) if music else Path.home() / "Music") / "VieNeuTTS" / stamp

    # ── replay: Phát without export ──────────────────────────────────────────

    @Slot(result=bool)
    def replay(self) -> bool:
        """Replay the current managed artifact with the attached file player.

        Returns True when the replay started (or a source swap was queued on
        the shared player); False when there is nothing to replay, no player
        is attached, or the player refused the file — replayActive is always
        False on a False return so the Phát/Dừng toggle cannot stick on Dừng.
        """
        artifact = self._current_artifact
        if artifact is None or not artifact.path.is_file():
            self._set_error(self.tr("Chưa có gì để phát — hãy tổng hợp âm thanh trước."))
            return False
        self._stop_replay()
        playback = self._file_playback
        if playback is None or not hasattr(playback, "play"):
            self._set_error(self.tr("Hệ thống này không phát được âm thanh."))
            return False
        self._artifact_store.protect(artifact)
        self._replay_artifact = artifact
        self._set_replay_active(True)
        self._begin_replay_position(artifact.duration_ms)
        try:
            refused = playback.play(
                str(artifact.path),
                on_released=lambda: self._release_artifact_after_playback(artifact),
            )
        except TypeError:
            # Legacy player without the on_released keyword (pre-bool era
            # fakes): retry positionally, then fall through to the check.
            try:
                refused = playback.play(str(artifact.path))
            except Exception:  # noqa: BLE001 - file playback must never crash the UI
                refused = False
        except Exception:  # noqa: BLE001 - file playback must never crash the UI
            refused = False
        if refused is False:
            self._release_artifact_after_playback(artifact)
            self._set_error(self.tr("Hệ thống này không phát được âm thanh."))
            return False
        return True

    @Slot()
    def stopReplay(self) -> None:
        """Stop any live replay and park the playhead at the start."""
        self._stop_replay()

    @Slot()
    def pauseReplay(self) -> None:
        """Pause a live replay, keeping the playhead where it is.

        Studio's transport uses this for Tạm dừng (resume via
        ``resumeReplay``); full stop-and-rewind stays on ``stopReplay``.
        Players without ``pause`` (old test fakes) are a silent no-op.
        """
        if not self._replay_active or self._replay_paused:
            return
        playback = self._file_playback
        if playback is None or not hasattr(playback, "pause"):
            return
        try:
            playback.pause()
        except Exception:  # noqa: BLE001 - pausing must never raise
            logger.exception("pausing file replay failed")
            return
        self._pause_replay_position()
        self._set_replay_paused(True)

    @Slot()
    def resumeReplay(self) -> None:
        """Resume a paused replay from the kept playhead position."""
        if not self._replay_active or not self._replay_paused:
            return
        playback = self._file_playback
        if playback is None or not hasattr(playback, "resume"):
            return
        try:
            playback.resume()
        except Exception:  # noqa: BLE001 - resuming must never raise
            logger.exception("resuming file replay failed")
            return
        self._resume_replay_position()
        self._set_replay_paused(False)

    @Slot(float, result=bool)
    def seekReplay(self, fraction: float) -> bool:
        """Jump the live replay to ``fraction`` of its duration (0..1).

        Feeds the Studio waveform's seek requests; no-op while idle.
        """
        if not self._replay_active or self._replay_duration_ms <= 0:
            return False
        try:
            clamped = max(0.0, min(float(fraction), 1.0))
        except (TypeError, ValueError):
            return False
        ms = int(clamped * self._replay_duration_ms)
        playback = self._file_playback
        if playback is not None and hasattr(playback, "seek"):
            try:
                playback.seek(ms)
            except Exception:  # noqa: BLE001 - a dead backend must not raise
                logger.exception("seeking file replay failed")
        self._rebase_replay_position(ms)
        return True

    @Property(bool, notify=replayActiveChanged)
    def replayActive(self) -> bool:
        return self._replay_active

    @Property(bool, notify=replayPausedChanged)
    def replayPaused(self) -> bool:
        return self._replay_paused

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
        self._clear_studio_audition()
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
        self._clear_studio_audition()
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
            with contextlib.suppress(Exception):
                if self._artifact_store.remove_if_unprotected(artifact):
                    self._retired_artifacts.discard(artifact)

    def _set_replay_active(self, value: bool) -> None:
        if value != self._replay_active:
            self._replay_active = value
            self.replayActiveChanged.emit()

    def _set_replay_paused(self, value: bool) -> None:
        if value != self._replay_paused:
            self._replay_paused = value
            self.replayPausedChanged.emit()

    def _replay_elapsed_ms(self) -> int:
        """Milliseconds played since the last (re)base: offset + clock."""
        return self._replay_base_ms + int(self._replay_clock.elapsed())

    # ── replay playhead (PlaybackWaveform position feed) ─────────────────────

    def _begin_replay_position(self, duration_ms: int, start_ms: int = 0) -> None:
        """Arm the playhead for a starting replay (duration 0 = unknown yet)."""
        self._replay_pos_timer.stop()
        self._set_replay_duration_ms(max(0, int(duration_ms)))
        self._replay_base_ms = max(0, int(start_ms))
        if self._replay_duration_ms > 0:
            self._set_replay_position(min(self._replay_base_ms / self._replay_duration_ms, 1.0))
        else:
            self._set_replay_position(0.0)
        if duration_ms > 0:
            self._replay_clock.start()
            self._replay_pos_timer.start()

    def _end_replay_position(self) -> None:
        """Stop advancing and park the playhead back at the start."""
        self._replay_pos_timer.stop()
        self._replay_base_ms = 0
        self._set_replay_paused(False)
        self._set_replay_position(0.0)

    def _pause_replay_position(self) -> None:
        """Freeze the playhead clock; the position value is kept, not parked."""
        self._replay_pos_timer.stop()
        if self._replay_duration_ms > 0:
            self._replay_base_ms = min(self._replay_elapsed_ms(), self._replay_duration_ms)
            self._set_replay_position(self._replay_base_ms / self._replay_duration_ms)

    def _resume_replay_position(self) -> None:
        """Restart the clock from the kept offset after a pause."""
        if self._replay_duration_ms <= 0:
            return
        self._replay_clock.start()
        self._replay_pos_timer.start()

    def _rebase_replay_position(self, ms: int) -> None:
        """Jump the playhead clock to ``ms`` (seek); keeps running if playing."""
        self._replay_base_ms = max(0, int(ms))
        if self._replay_duration_ms > 0:
            self._set_replay_position(min(self._replay_base_ms / self._replay_duration_ms, 1.0))
            if self._replay_pos_timer.isActive() or (
                self._replay_active and not self._replay_paused
            ):
                self._replay_clock.start()
                if not self._replay_pos_timer.isActive():
                    self._replay_pos_timer.start()

    def _on_replay_position_tick(self) -> None:
        if self._replay_duration_ms <= 0:
            return
        position = min(self._replay_elapsed_ms() / self._replay_duration_ms, 1.0)
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

        def on_done(result: tuple[str, str]) -> None:
            self._on_document_imported(path, _unwrap_bg_result(result))

        def on_error(exc: BaseException) -> None:
            self._set_importing(False)
            message = self.tr("Lỗi nhập tệp: {}").format(exc)
            self._set_error(message)
            self.documentImported.emit(path, "")

        self._run_bg(work, on_done, self, on_error=on_error)
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
    @Slot(str, str, bool, str)
    def addVoice(self, name: str, clip_path: str, denoise: bool, transcript: str = "") -> None:
        """Enroll a clone on the ACTIVE profile (FR-3.4).

        ``transcript`` is the reference clip's own text: the capability table
        requires it for Qwen3-TTS Base (the clone store refuses an empty one)
        and VieNeu's SDK ignores it. Consent is the acknowledgement this
        session already recorded — the panel that guards the Cloning tab — and
        the profile is snapshotted here, so a queued enrollment can never land
        in another engine's catalog after a switch.
        """
        raw = (clip_path or "").strip()
        if raw.startswith("file://"):
            raw = str(normalize_local_path(raw))
        elif (raw.startswith('"') and raw.endswith('"')) or (
            raw.startswith("'") and raw.endswith("'")
        ):
            raw = raw[1:-1].strip()
        self._submit_voice_op(
            VoiceOp(
                op="add",
                name=name,
                clip_path=raw,
                denoise=denoise,
                profile=self._active_profile,
                transcript=transcript,
                consent=self._consent,
            )
        )

    @Slot(str)
    def removeVoice(self, name: str) -> None:
        self._submit_voice_op(VoiceOp(op="remove", name=name, profile=self._active_profile))

    @Slot(str)
    def denoisePreview(self, clip_path: str) -> None:
        raw = (clip_path or "").strip()
        if raw.startswith("file://"):
            raw = str(normalize_local_path(raw))
        elif (raw.startswith('"') and raw.endswith('"')) or (
            raw.startswith("'") and raw.endswith("'")
        ):
            raw = raw[1:-1].strip()
        self._submit_voice_op(VoiceOp(op="denoise", clip_path=raw))

    def _submit_voice_op(self, op: VoiceOp) -> None:
        try:
            worker = self._ensure_worker()
            job = new_synthesis_job("cloning", "voice_op", op)
        except ValueError as exc:
            self._set_error(f"Invalid voice operation: {exc}")
            return
        except Exception as exc:
            logger.exception("failed to initialize worker for voice operation")
            self._set_error(self.tr("Không thể khởi động mô hình: {}").format(exc))
            return
        self._stop_audition_session()
        self._reset_audition_tracking()
        self._stop_replay()
        if self._file_playback is not None and hasattr(self._file_playback, "stop"):
            with contextlib.suppress(Exception):
                self._file_playback.stop()
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
            if engine_profiles.is_qwen_profile(self._active_profile):
                # A Qwen profile is served by its own isolated host, built from
                # the verified installs (filesystem-only; the child is spawned
                # by initialize() on the worker thread).
                self._engine = self._build_qwen_engine()
            else:
                self._engine = self._build_vieneu_engine()
        providers = self._providers_for(self._engine)
        if self._worker_factory is not None:
            # The factory takes the engine alone for the single-engine
            # (VieNeu) posture and (engine, providers) when the active profile
            # needs an explicit provider set.
            self._worker = (
                self._worker_factory(self._engine)
                if providers is None
                else self._worker_factory(self._engine, providers)
            )
        else:
            self._worker = InferenceWorker(
                self._engine, performance_recorder=self._performance, providers=providers
            )
        self._connect_worker(self._worker)
        self._worker.start()
        return self._worker

    def _build_vieneu_engine(self) -> Any:
        """The in-process VieNeu engine, built with the CURRENT settings."""
        # Engine is built with the CURRENT settings; needsRestart was
        # consumed by shutdown() dropping the previous instance.
        # Official CPU baseline resolves auto→onnx with local SDK paths on
        # a clean CUDA-capable machine (Phase 1 Task 3).
        managed = self._model_status.location
        managed_cuda = (
            self._cuda_runtime_status.location
            if (
                self._cuda_runtime_status.state == "ready"
                and isinstance(self._cuda_runtime_status.location, CudaRuntimeLocation)
            )
            else None
        )
        backend, managed_model = resolve_model_source(
            self._settings,
            managed,
            managed_cuda=managed_cuda,
            cuda_driver_ready=self.cudaRuntimeDriverReady,
        )
        # While the runtime inspect or the driver probe is still in
        # flight, hand "auto" to the engine unresolved: the engine waits
        # for the probe at first init (bounded) instead of this build
        # guessing ONNX — a slow nvidia-smi must not pin the whole
        # session to CPU.
        defer_auto = self._settings.backend == "auto" and (
            managed_cuda is not None and not self._cuda_runtime_driver_checked
        )
        if defer_auto:
            engine_backend: str = "auto"
            cuda_runtime = managed_cuda
            driver_state: Callable[[], bool | None] | None = self.managed_cuda_engine_state
        else:
            engine_backend = backend
            cuda_runtime = managed_cuda if backend == "torch" else None
            driver_state = None
        return self._engine_factory(
            backend=engine_backend,
            precision=self._settings.precision,
            voices_dir=self._voices_dir,
            model_repo=self._settings.model_repo,
            managed_model=managed_model,
            cuda_runtime=cuda_runtime,
            cuda_driver_state=driver_state,
        )

    def _build_qwen_engine(self) -> Any:
        """The isolated-host engine for the ACTIVE Qwen profile.

        Filesystem-only: it reads the verified model + runtime installs and
        never contacts the Hub, downloads, or loads weights — the host
        subprocess is spawned by ``initialize()`` on the worker thread. Raises
        with an actionable reason when the profile is not ready to run, so the
        submission fails with what the user must install instead of with an
        opaque host error.
        """
        from vienetts_app.core.qwen_engine import QwenEngineError  # noqa: PLC0415 - lazy seam

        profile = self._active_profile
        caps = engine_profiles.get_capabilities(profile)
        variant = qwen_variants.variant_for(
            profile,
            model_format=self._settings.qwen_model_format,
            quantization=(
                self._settings.qwen_gguf_quantization
                if self._settings.qwen_model_format == qwen_variants.MODEL_FORMAT_GGUF
                else ""
            ),
        )
        if variant.model_format == qwen_variants.MODEL_FORMAT_GGUF:
            return self._build_qwen_gguf_engine(profile, caps, variant)
        manager = self._qwen_model_manager_factory(
            self._data_dir, engine_profiles.runtime_key(profile)
        )
        status = manager.inspect()
        location = getattr(status, "location", None)
        if str(getattr(status, "state", "")) != "ready" or location is None:
            raise QwenEngineError(
                f"{caps.label} is not installed — install the model from Settings first "
                f"(state: {getattr(status, 'state', 'unknown')})"
            )
        runtime_manager = self._qwen_runtime_manager_factory(self._data_dir)
        runtime_location = None
        if runtime_manager is not None:
            runtime_location = getattr(runtime_manager.inspect(), "location", None)
        if runtime_location is None:
            raise QwenEngineError(
                f"the managed Qwen runtime is not installed — install it from Settings before "
                f"synthesizing with {caps.label}"
            )
        return self._qwen_engine_factory(
            profile=profile,
            model_dir=Path(location.profile_dir),
            shared_dir=Path(location.shared_dir),
            device=self._qwen_engine_device(caps),
            runtime_dir=Path(runtime_location.site_packages),
        )

    def _qwen_engine_device(self, caps: Any) -> str:
        """The device to hand the model host (never probed on the GUI thread).

        An explicit setting wins; otherwise the value the post-paint profile
        inspection already resolved is used. While that is still in flight the
        submission is refused (and the resolve is kicked again) rather than
        guessing a device or blocking the GUI thread on nvidia-smi.
        """
        from vienetts_app.core.qwen_engine import QwenEngineError  # noqa: PLC0415 - lazy seam

        setting = str(self._settings.qwen_device or "auto")
        if setting in caps.devices:
            return setting
        if self._profile_device in caps.devices:
            return self._profile_device
        self.refreshProfileState()
        raise QwenEngineError(
            f"{caps.label} is still resolving its compute device — try again in a moment"
        )

    def _build_qwen_gguf_engine(self, profile: EngineId, caps: Any, variant: Any) -> Any:
        """The managed-native engine for a GGUF Qwen variant.

        Same filesystem-only posture as the official build: the verified pack
        (keyed by the resolved native device) and the verified model pair come
        from the Phase 3 installers; the child is spawned by ``initialize()``
        on the worker thread. Missing pieces raise with the install reason.
        """
        from vienetts_app.core.qwen_engine import QwenEngineError  # noqa: PLC0415 - lazy seam

        device = self._qwen_gguf_device(variant)
        cell = qwen_gguf_manifest.host_cell_key(device)
        runtime_manager = (
            self._qwen_gguf_runtime_manager_factory(self._data_dir, cell)
            if cell is not None
            else None
        )
        runtime_status = runtime_manager.inspect() if runtime_manager is not None else None
        runtime_location = getattr(runtime_status, "location", None)
        if runtime_location is None:
            raise QwenEngineError(
                f"the managed qwentts.cpp runtime for {device} is not installed — "
                f"install it from Settings before synthesizing with {caps.label}"
            )
        model_manager = self._qwen_gguf_model_manager_factory(self._data_dir, variant)
        model_status = model_manager.inspect()
        if not getattr(model_status, "ready", False):
            raise QwenEngineError(
                f"{caps.label} ({variant.quantization} GGUF) is not installed — "
                f"install the model from Settings first "
                f"(state: {getattr(model_status, 'state', 'unknown')})"
            )
        return self._qwen_gguf_engine_factory(
            profile=profile,
            runtime_dir=Path(runtime_location.root),
            talker_path=Path(model_status.talker_path),
            codec_path=Path(model_status.tokenizer_path),
            quantization=variant.quantization,
            device=device,
        )

    def _qwen_gguf_device(self, variant: Any) -> str:
        """The native ggml device id for the GGUF host (``mps`` → ``metal``).

        Same contract as ``_qwen_engine_device`` — an explicit setting wins,
        else the resolved inspection device, else the submission is refused
        and the resolve re-kicked — but the GGUF preference is its own field
        (``qwen_gguf_device``, independent of the PyTorch ``qwen_device``)
        and the wire speaks ggml's vocabulary, so ``mps`` maps to ``metal``.
        """
        from vienetts_app.core.qwen_engine import QwenEngineError  # noqa: PLC0415 - lazy seam

        setting = str(self._settings.qwen_gguf_device or "auto")
        if setting in variant.devices:
            return setting
        if setting == "mps":
            return "metal"
        resolved = "metal" if self._profile_device == "mps" else self._profile_device
        if resolved in variant.devices:
            return resolved
        self.refreshProfileState()
        raise QwenEngineError(
            f"{variant.capabilities.label} is still resolving its compute device — "
            "try again in a moment"
        )

    def _providers_for(self, engine: Any) -> EngineProviders | None:
        """The worker's provider set for the ACTIVE profile.

        ``None`` keeps the worker's own single-engine default: the in-process
        VieNeu engine serves every job, which is the posture the app has always
        had. A Qwen profile gets an explicit set holding exactly ONE provider —
        the app never keeps two model stacks resident — and that provider is
        also the default (prewarm target, context-less legacy jobs).
        """
        if not engine_profiles.is_qwen_profile(self._active_profile):
            return None
        from vienetts_app.core.qwen_engine import QwenEngineProvider  # noqa: PLC0415 - lazy seam

        provider = QwenEngineProvider(engine, clone_store=self._clone_store_instance())
        return EngineProviders(by_profile={self._active_profile: provider}, default=provider)

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
        # Preserve this process-level fact before `_engine` is retired or
        # cleared: native torch/CUDA modules remain loaded after close().
        self._cuda_runtime_in_use()
        # Any outstanding inspect/discovery/install callback is stale once
        # teardown starts. Install cancellation remains cooperative so the
        # manager can preserve only verified resumable archives.
        self._cuda_runtime_generation += 1
        self._local_cuda_generation += 1
        self._cuda_runtime_cancel.set()
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
        if event.state == "failed":
            # Before any dispatch: a runtime-incomplete failure is a property of
            # the engine, not of the job, so the runtime card learns about it
            # whether the job was a foreground one, a batch item or an audiobook
            # chapter.
            self._publish_incomplete_runtime()
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
        context = self._foreground_context
        self._foreground_job_id = None
        self._foreground_is_voice_op = False
        self._foreground_context = None
        self._chunk_seen_by_job_id.discard(job_id)
        self.foregroundJobIdChanged.emit()
        if event.state == "completed":
            self._set_foreground_job_state("completed")
            if is_voice_op:
                self._complete_voice_op(event.value)
            else:
                self._complete_foreground_audio(job_id, event.value, context=context)
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

    def _complete_foreground_audio(
        self, job_id: str, value: Any, *, context: SynthesisContext | None = None
    ) -> None:
        """Commit a finished foreground artifact (``context`` = its identity)."""
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
        # Studio reads this to stamp clip provenance: the identity travels with
        # the artifact, never re-derived from the settings a later edit changes.
        self._current_artifact_context = context
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
        self._maybe_splice_regen(value)

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
            job_id, envelope = _unwrap_bg_result(result)
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
        if self._studio_regen_clip_id is not None:
            self._studio_regen_clip_id = None
            self._studio_regen_clip_text = None
            # Drop the pending engine identity too: nothing is armed, so a
            # later synthesis can never be spliced onto this clip.
            self._studio_regen_context = None
            self.studioProjectChanged.emit()
        self._set_busy(False)
        self._performance.finish(job_id, "cancelled")
        self.cancelled.emit()

    def _fail_foreground_audio(self, job_id: str, message: str) -> None:
        self._foreground_live = False
        self._stop_stream_playback_now()
        if self._studio_regen_clip_id is not None:
            self._studio_regen_clip_id = None
            self._studio_regen_clip_text = None
            # Same disarm as cancel: a failed job must not leave a splice armed.
            self._studio_regen_context = None
            self.studioProjectChanged.emit()
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
            # Unique per-completion name: the shared file player may still
            # hold the previous preview (stop() releases the backend
            # asynchronously), so reusing one fixed preview.wav races its
            # teardown with os.replace on Windows (WinError 32). The
            # previous file is removed best-effort once the new one lands.
            previous = self._preview_path
            if self._file_playback is not None and hasattr(self._file_playback, "stop"):
                with contextlib.suppress(Exception):
                    self._file_playback.stop()
            try:
                stem = PREVIEW_FILENAME.rsplit(".", 1)[0]
                target = self._data_dir / f"{stem}_{time.time_ns()}.wav"
                write_wav_file(np.asarray(audio), target, sample_rate=sample_rate)
            except Exception as exc:  # noqa: BLE001
                self._set_error(f"Preview failed: {exc}")
                self._set_busy(False)
                return
            self._remove_preview_file(previous)
            self._preview_path = str(target)
            self.previewPathChanged.emit()
        else:
            self.refreshVoices()
        self._set_busy(False)

    def _remove_preview_file(self, path: str) -> None:
        """Best-effort cleanup of a superseded preview (never a user file)."""
        if not path:
            return
        try:
            candidate = Path(path)
            if candidate.parent != self._data_dir:
                return
            name = candidate.name
            if name != PREVIEW_FILENAME and not (
                name.startswith(f"{PREVIEW_FILENAME.rsplit('.', 1)[0]}_")
                and candidate.suffix == ".wav"
            ):
                return
            candidate.unlink(missing_ok=True)
        except OSError:
            # Still held by the player (or an AV scan): the orphan is a
            # seconds-long clip; the next completion retries the removal.
            logger.debug("superseded preview cleanup deferred", exc_info=True)

    def _on_stream_level(self, value: float) -> None:
        """Rolling peak envelope for the QML WaveformIndicator (FR-4.5)."""
        self._set_stream_level(value)

    # (Voice-op terminals land in _on_terminal → _complete_voice_op.)

    # ── settings seam (FR-3.5) ──────────────────────────────────────────────

    torchAvailableChanged = Signal()

    @Property(bool, notify=torchAvailableChanged)
    def torchAvailable(self) -> bool:
        """True only when this install can actually run the PyTorch/CUDA engine.

        Lazy: first read pays the torch metadata probe, cached thereafter.
        True for a usable system torch install OR a ready managed runtime
        behind a usable NVIDIA driver. The cache resets whenever managed
        readiness flips (see ``_publish_cuda_runtime_status``).
        """
        if self._torch_available is None:
            self._torch_available = self._probe_torch_availability()
        return self._torch_available

    def _probe_torch_availability(self) -> bool:
        """System torch probe OR managed-runtime readiness (never imports torch)."""
        probe = self._torch_probe()
        if probe.installed and probe.cuda_available:
            return True
        ready, _version = self.managed_cuda_for_detection()
        return ready

    def resolveTorchAvailabilityAsync(self) -> None:
        """Pre-warm the torch probe off-thread (app.py, post-first-paint).

        Runs the same probe as the ``torchAvailable`` getter on a daemon
        thread so the QML binding never pays the 1-3 s torch import on GPU
        installs. Re-emits ``torchAvailableChanged`` once resolved; a probe
        already done is a no-op. Same RuntimeError-drop discipline as the
        bridge's async engine note (app may quit mid-probe).
        """
        if self._torch_available is not None:
            return

        def _work() -> None:
            self._torch_available = self._probe_torch_availability()
            with contextlib.suppress(RuntimeError):
                self.torchAvailableChanged.emit()

        threading.Thread(target=_work, name="vienetts-torch-probe", daemon=True).start()

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

    @Property(str, notify=outputDirChanged)
    def outputDirUrl(self) -> str:
        """file:// URL for the configured (or default) output directory."""
        dir_path = self._settings.output_dir.strip()
        if not dir_path:
            dir_path = str(self._default_export_path().parent)
        return path_to_file_url(dir_path)

    @Slot(str, result=str)
    def pathToUrl(self, path: str) -> str:
        """Convert a local path into a valid file:// URL for QML dialogs."""
        return path_to_file_url(path)

    @Slot(str, result=int)
    def wordCount(self, text: str) -> int:
        """Script-aware word count for the editor metric chips.

        Han/kana characters count one each (zh/ja write without spaces, so a
        whitespace split would collapse a paragraph to one "word"); Hangul
        keeps its space-delimited eojeol; every other script keeps whitespace
        tokens. See :mod:`vienetts_app.core.text_metrics`.
        """
        return count_words(text)

    @Slot(str, result=int)
    def estimateDurationSeconds(self, text: str) -> int:
        """Estimated spoken duration in seconds at per-script speech rates."""
        return estimate_duration_seconds(text)

    @Property(str, notify=exportFormatChanged)
    def exportFormat(self) -> str:
        """Batch + audiobook output container ("wav" | "mp3"); Save dialogs pick per-file."""
        return self._settings.export_format

    @exportFormat.setter
    def exportFormat(self, value: str) -> None:
        if not isinstance(value, str):
            self._set_error(self.tr("exportFormat phải là chuỗi ký tự."))
            return
        self._set_setting("export_format", value.strip().lower(), allowed={"wav", "mp3"})

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
            ("export_format", self.exportFormatChanged),
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
