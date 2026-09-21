"""Data model per PROJECT_PLAN.md §9, validated against the Phase 0 spike
contract (docs/spike-report.md §0)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from vienetts_app.core import engine_profiles
from vienetts_app.core.engine_profiles import EngineId
from vienetts_app.core.synthesis_context import (
    SynthesisContext,
)
from vienetts_app.core.synthesis_context import (
    check_silence_p as _check_silence_p,
)
from vienetts_app.core.synthesis_context import (
    check_speed as _check_speed,
)
from vienetts_app.core.synthesis_context import (
    check_temperature as _check_temperature,
)

Backend = Literal["auto", "onnx", "torch"]
Device = Literal["cpu", "cuda"]
Precision = Literal["int8", "fp32"]
Theme = Literal["system", "light", "dark"]
ExportFormat = Literal["wav", "mp3"]
RequestMode = Literal["infer", "stream", "batch"]
ProgressStage = Literal["init", "synthesizing", "exporting"]
VoiceOperation = Literal["add", "remove", "denoise"]

_BACKENDS = frozenset(("auto", "onnx", "torch"))
_DEVICES = frozenset(("cpu", "cuda"))
_PRECISIONS = frozenset(("int8", "fp32"))
_THEMES = frozenset(("system", "light", "dark"))
# UI display languages: "vi" is the qsTr source language (no catalog needed).
_LANGUAGES = frozenset(("system", "vi", "en"))
_EXPORT_FORMATS = frozenset(("wav", "mp3"))
_MODES = frozenset(("infer", "stream", "batch"))
_STAGES = frozenset(("init", "synthesizing", "exporting"))
_VOICE_OPS = frozenset(("add", "remove", "denoise"))
# Global engine profile IDs live in core.engine_profiles; models imports the
# IDs (not the capability data) so Settings validation can never drift from it.
_ENGINE_PROFILES = frozenset(engine_profiles.list_profiles())
_QWEN_DEVICES = frozenset(("auto", "cpu", "cuda", "mps"))


def _check_choice(field: str, value: object, allowed: frozenset[str]) -> None:
    if value not in allowed:
        raise ValueError(f"{field} must be one of {sorted(allowed)}, got {value!r}")


def _check_optional_path(field: str, value: object) -> None:
    if value is not None and not isinstance(value, str):
        raise TypeError(f"{field} must be a string path or None")
    if value is not None and not value.strip():
        raise ValueError(f"{field} must be a non-empty, non-blank string")


# Hugging Face repo id form: exactly one "/" with non-empty, whitespace-free
# owner and name segments (e.g. "pnnbao-ump/VieNeu-TTS-v3-Turbo").
_REPO_ID_RE = re.compile(r"^[^\s/]+/[^\s/]+$")


def _check_model_repo(value: object) -> None:
    """Validate the backbone repo override; "" = official SDK default."""
    if not isinstance(value, str):
        raise TypeError(f"model_repo must be a string, got {type(value).__name__}")
    if value and not _REPO_ID_RE.match(value):
        raise ValueError(
            f"model_repo must be empty (official default) or an 'owner/name' "
            f"Hugging Face repo id without whitespace, got {value!r}"
        )


@dataclass(frozen=True)
class EngineInfo:
    """Resolved engine description shown in the UI (§9)."""

    backend: Backend
    device: Device
    precision: Precision
    cuda_version: str | None
    note: str  # human-readable, e.g. "ONNX Runtime CPU · int8"

    def __post_init__(self) -> None:
        _check_choice("backend", self.backend, _BACKENDS)
        _check_choice("device", self.device, _DEVICES)
        _check_choice("precision", self.precision, _PRECISIONS)
        if not isinstance(self.note, str) or not self.note.strip():
            raise ValueError("note must be a non-empty string")
        if self.cuda_version is not None and not isinstance(self.cuda_version, str):
            raise ValueError("cuda_version must be a string or None")


@dataclass
class Settings:
    """Persisted user settings (§9); stored as JSON in the platform data dir."""

    backend: str = "auto"
    precision: str = "int8"
    default_voice: str = "Adam"
    output_dir: str = ""  # empty → ~/Music/VieNeuTTS at use site
    export_format: str = "wav"  # batch + audiobook output container; dialogs pick per-file
    theme: str = "system"
    language: str = "system"  # resolved at startup; applied after restart
    denoise_ref: bool = True
    temperature: float = 0.4  # SDK exposes it (spike §0); infer default 0.4
    speed: float = 1.0  # speech rate multiplier [0.5, 2.0]
    silence_p: float = 0.15  # pause length between sentences/paragraphs in seconds [0.0, 2.0]
    live_preview: bool = False  # ON = hear chunks live; OFF = silent, then auto-replay from start
    model_repo: str = ""  # empty → SDK default (pnnbao-ump/VieNeu-TTS-v3-Turbo)
    model_cache_enabled: bool = True
    engine_profile: str = "vieneu"  # global active profile (engine_profiles.EngineId)
    qwen_device: str = "auto"  # Qwen compute device: auto | cpu | cuda | mps
    # Synthesis language for the active profile ("" = the profile's own default:
    # VieNeu's SDK default, or Qwen's Auto). Profile-scoped, so load_settings
    # clamps a code the active profile does not support.
    synthesis_language: str = ""
    # placed → the shell centers with its default 1120×740 size.
    window_x: int | None = None
    window_y: int | None = None
    window_width: int | None = None
    window_height: int | None = None
    window_maximized: bool = False

    def __post_init__(self) -> None:
        _check_choice("backend", self.backend, _BACKENDS)
        _check_choice("precision", self.precision, _PRECISIONS)
        _check_choice("export_format", self.export_format, _EXPORT_FORMATS)
        _check_choice("theme", self.theme, _THEMES)
        _check_choice("language", self.language, _LANGUAGES)
        if not isinstance(self.default_voice, str) or not self.default_voice.strip():
            raise ValueError("default_voice must be a non-empty string")
        if not isinstance(self.denoise_ref, bool):
            raise ValueError("denoise_ref must be a bool")
        if not isinstance(self.live_preview, bool):
            raise ValueError("live_preview must be a bool")
        _check_temperature(self.temperature, allow_none=False)
        _check_speed(self.speed, allow_none=False)
        _check_silence_p(self.silence_p, allow_none=False)
        _check_model_repo(self.model_repo)
        if not isinstance(self.model_cache_enabled, bool):
            raise ValueError("model_cache_enabled must be a bool")
        _check_choice("engine_profile", self.engine_profile, _ENGINE_PROFILES)
        _check_choice("qwen_device", self.qwen_device, _QWEN_DEVICES)
        if not isinstance(self.synthesis_language, str):
            raise ValueError("synthesis_language must be a string")
        for field in ("window_x", "window_y", "window_width", "window_height"):
            value = getattr(self, field)
            if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
                raise ValueError(f"{field} must be an integer or None")
        if not isinstance(self.window_maximized, bool):
            raise ValueError("window_maximized must be a bool")


@dataclass(frozen=True)
class TTSRequest:
    """One synthesis job (§9)."""

    text: str
    voice: str | None = None
    ref_audio: str | None = None
    denoise: bool = True
    mode: RequestMode = "infer"
    temperature: float | None = None  # None → SDK default (0.4 for infer)
    speed: float | None = None  # None → Settings default (1.0)
    silence_p: float | None = None  # None → Settings default (0.15)
    job_id: str | None = None
    # Immutable engine identity snapshot. ``None`` = legacy/unspecified request
    # (the VieNeu path keeps working while callers migrate in Phase 5); when
    # present, the profile/language/voice/clone must be self-consistent and
    # must not contradict the request's own voice/ref_audio fields.
    context: SynthesisContext | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("text must be a non-empty, non-blank string")
        _check_choice("mode", self.mode, _MODES)
        if self.voice is not None and (not isinstance(self.voice, str) or not self.voice.strip()):
            raise ValueError("voice must be a non-empty string or None")
        if self.ref_audio is not None and not isinstance(self.ref_audio, str):
            raise TypeError("ref_audio must be a string path or None")
        if not isinstance(self.denoise, bool):
            raise ValueError("denoise must be a bool")
        _check_temperature(self.temperature, allow_none=True)
        _check_speed(self.speed, allow_none=True)
        _check_silence_p(self.silence_p, allow_none=True)
        if self.job_id is not None:
            if not isinstance(self.job_id, str):
                raise TypeError("job_id must be a string or None")
            if not self.job_id.strip():
                raise ValueError("job_id must be a non-empty, non-blank string")
        self._check_context()

    def _check_context(self) -> None:
        """Reject a context that contradicts this request's own fields.

        The context is the job's engine identity; a request that carries both
        (e.g. a preset voice on a cloned context) would make the worker and the
        caches disagree about what produced the audio.
        """
        context = self.context
        if context is None:
            return
        if not isinstance(context, SynthesisContext):
            raise TypeError(
                f"context must be a SynthesisContext or None, got {type(context).__name__}"
            )
        if context.clone_id:
            if self.voice is not None:
                raise ValueError(
                    "a cloned context must not also carry a preset voice — "
                    "drop the voice or clear the clone"
                )
            return
        if self.voice is not None and context.voice_id and self.voice != context.voice_id:
            raise ValueError(
                f"voice {self.voice!r} contradicts the context voice {context.voice_id!r}"
            )
        if self.ref_audio is not None and engine_profiles.is_qwen_profile(context.profile):
            raise ValueError(
                "ref_audio is a VieNeu cloning field — Qwen clones are enrolled in the "
                "clone store and referenced by clone_id"
            )


@dataclass(frozen=True)
class WarmupOp:
    """Model-load-only job for the worker queue (background prewarm).

    Loads the engine without synthesizing so the FIRST user request finds a
    warm model (the 1.4–1.6 s cold load otherwise lands inside that request).
    Carries no payload: success and failure are both silent — a failed warmup
    surfaces its actionable error only when a real request hits the same
    condition.
    """


@dataclass(frozen=True)
class VoiceOp:
    """One voice-management job (FR-3.4), serialized through the worker queue.

    ``add`` enrolls ``clip_path`` under ``name``; ``remove`` drops ``name``
    (a clone id is accepted too); ``denoise`` cleans ``clip_path`` for preview.
    Both add/denoise respect the ``denoise`` reference-cleanup flag
    (Settings.denoise_ref mirrors it).

    ``profile`` is the engine whose catalog the operation touches (``None`` =
    the worker's active/default engine, which is what pre-multi-engine callers
    pass). ``transcript``/``consent`` carry the enrollment data profiles that
    need them require — Qwen3-TTS Base needs both, VieNeu needs neither — and
    the engine provider is what enforces its own profile's requirements.
    """

    op: VoiceOperation
    name: str | None = None
    clip_path: str | None = None
    denoise: bool = True
    profile: EngineId | None = None
    transcript: str = ""
    consent: bool = False

    def __post_init__(self) -> None:
        _check_choice("op", self.op, _VOICE_OPS)
        if self.name is not None and not isinstance(self.name, str):
            raise TypeError("name must be a string or None")
        _check_optional_path("clip_path", self.clip_path)
        if not isinstance(self.denoise, bool):
            raise ValueError("denoise must be a bool")
        if self.profile is not None:
            _check_choice("profile", self.profile, _ENGINE_PROFILES)
        if not isinstance(self.transcript, str):
            raise TypeError("transcript must be a string")
        if not isinstance(self.consent, bool):
            raise ValueError("consent must be a bool")
        if self.op == "add":
            if self.name is None or not self.name.strip():
                raise ValueError("op 'add' requires a non-blank name")
            if self.clip_path is None:
                raise ValueError("op 'add' requires clip_path")
        elif self.op == "remove":
            if self.name is None or not self.name.strip():
                raise ValueError("op 'remove' requires a non-blank name")
            if self.transcript.strip() or self.consent:
                raise ValueError("transcript/consent apply to op 'add' only")
        else:
            if self.clip_path is None:
                raise ValueError("op 'denoise' requires clip_path")
            if self.transcript.strip() or self.consent:
                raise ValueError("transcript/consent apply to op 'add' only")


@dataclass(frozen=True)
class TTSProgress:
    """Progress signal payload (§9)."""

    done: int
    total: int
    stage: ProgressStage

    def __post_init__(self) -> None:
        if not isinstance(self.done, int) or isinstance(self.done, bool):
            raise ValueError("done must be an int")
        if not isinstance(self.total, int) or isinstance(self.total, bool):
            raise ValueError("total must be an int")
        if self.done < 0:
            raise ValueError(f"done must be >= 0, got {self.done}")
        if self.total < 0:
            raise ValueError(f"total must be >= 0, got {self.total}")
        if self.done > self.total:
            raise ValueError(f"done ({self.done}) must not exceed total ({self.total})")
        _check_choice("stage", self.stage, _STAGES)
