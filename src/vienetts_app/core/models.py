"""Data model per PROJECT_PLAN.md §9, validated against the Phase 0 spike
contract (docs/spike-report.md §0)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

Backend = Literal["auto", "onnx", "torch"]
Device = Literal["cpu", "cuda"]
Precision = Literal["int8", "fp32"]
Theme = Literal["system", "light", "dark"]
RequestMode = Literal["infer", "stream", "batch"]
ProgressStage = Literal["init", "synthesizing", "exporting"]
VoiceOperation = Literal["add", "remove", "denoise"]

_BACKENDS = frozenset(("auto", "onnx", "torch"))
_DEVICES = frozenset(("cpu", "cuda"))
_PRECISIONS = frozenset(("int8", "fp32"))
_THEMES = frozenset(("system", "light", "dark"))
# UI display languages: "vi" is the qsTr source language (no catalog needed).
_LANGUAGES = frozenset(("system", "vi", "en"))
_MODES = frozenset(("infer", "stream", "batch"))
_STAGES = frozenset(("init", "synthesizing", "exporting"))
_VOICE_OPS = frozenset(("add", "remove", "denoise"))

# SDK exposes temperature (infer default 0.4, stream default 0.8); keep the
# app range generous but bounded so the UI can use a slider.
_TEMPERATURE_MIN = 0.05
_TEMPERATURE_MAX = 2.0


def _check_choice(field: str, value: object, allowed: frozenset[str]) -> None:
    if value not in allowed:
        raise ValueError(f"{field} must be one of {sorted(allowed)}, got {value!r}")


def _check_temperature(value: object, *, allow_none: bool) -> float | None:
    """Validate a temperature against the shared Settings bounds.

    Numbers (bool excluded) in ``[_TEMPERATURE_MIN, _TEMPERATURE_MAX]`` pass;
    ``None`` passes only when ``allow_none`` (TTSRequest: None = SDK default).
    """
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"temperature must be a number or None, got {value!r}")
    if not _TEMPERATURE_MIN <= value <= _TEMPERATURE_MAX:
        raise ValueError(
            f"temperature must be in [{_TEMPERATURE_MIN}, {_TEMPERATURE_MAX}], got {value}"
        )
    return float(value)


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
    theme: str = "system"
    language: str = "system"  # resolved at startup; applied after restart
    denoise_ref: bool = True
    temperature: float = 0.4  # SDK exposes it (spike §0); infer default 0.4
    model_repo: str = ""  # empty → SDK default (pnnbao-ump/VieNeu-TTS-v3-Turbo)
    # Window placement (restored on launch, saved on close). None = never
    # placed → the shell centers with its default 1120×740 size.
    window_x: int | None = None
    window_y: int | None = None
    window_width: int | None = None
    window_height: int | None = None
    window_maximized: bool = False

    def __post_init__(self) -> None:
        _check_choice("backend", self.backend, _BACKENDS)
        _check_choice("precision", self.precision, _PRECISIONS)
        _check_choice("theme", self.theme, _THEMES)
        _check_choice("language", self.language, _LANGUAGES)
        if not isinstance(self.default_voice, str) or not self.default_voice.strip():
            raise ValueError("default_voice must be a non-empty string")
        if not isinstance(self.denoise_ref, bool):
            raise ValueError("denoise_ref must be a bool")
        _check_temperature(self.temperature, allow_none=False)
        _check_model_repo(self.model_repo)
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
    job_id: str | None = None

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
        if self.job_id is not None:
            if not isinstance(self.job_id, str):
                raise TypeError("job_id must be a string or None")
            if not self.job_id.strip():
                raise ValueError("job_id must be a non-empty, non-blank string")


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

    ``add`` enrolls ``clip_path`` under ``name``; ``remove`` drops ``name``;
    ``denoise`` cleans ``clip_path`` for preview. Both add/denoise respect the
    ``denoise`` reference-cleanup flag (Settings.denoise_ref mirrors it).
    """

    op: VoiceOperation
    name: str | None = None
    clip_path: str | None = None
    denoise: bool = True

    def __post_init__(self) -> None:
        _check_choice("op", self.op, _VOICE_OPS)
        if self.name is not None and not isinstance(self.name, str):
            raise TypeError("name must be a string or None")
        _check_optional_path("clip_path", self.clip_path)
        if not isinstance(self.denoise, bool):
            raise ValueError("denoise must be a bool")
        if self.op == "add":
            if self.name is None or not self.name.strip():
                raise ValueError("op 'add' requires a non-blank name")
            if self.clip_path is None:
                raise ValueError("op 'add' requires clip_path")
        elif self.op == "remove":
            if self.name is None or not self.name.strip():
                raise ValueError("op 'remove' requires a non-blank name")
        elif self.clip_path is None:
            raise ValueError("op 'denoise' requires clip_path")


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
