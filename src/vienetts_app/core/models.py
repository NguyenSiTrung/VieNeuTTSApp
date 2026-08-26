"""Data model per PROJECT_PLAN.md §9, validated against the Phase 0 spike
contract (docs/spike-report.md §0)."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Backend = Literal["auto", "onnx", "torch"]
Device = Literal["cpu", "cuda"]
Precision = Literal["int8", "fp32"]
Theme = Literal["system", "light", "dark"]
RequestMode = Literal["infer", "stream", "batch"]
ProgressStage = Literal["init", "synthesizing", "exporting"]

_BACKENDS = frozenset(("auto", "onnx", "torch"))
_DEVICES = frozenset(("cpu", "cuda"))
_PRECISIONS = frozenset(("int8", "fp32"))
_THEMES = frozenset(("system", "light", "dark"))
_MODES = frozenset(("infer", "stream", "batch"))
_STAGES = frozenset(("init", "synthesizing", "exporting"))

# SDK exposes temperature (infer default 0.4, stream default 0.8); keep the
# app range generous but bounded so the UI can use a slider.
_TEMPERATURE_MIN = 0.05
_TEMPERATURE_MAX = 2.0


def _check_choice(field: str, value: object, allowed: frozenset[str]) -> None:
    if value not in allowed:
        raise ValueError(f"{field} must be one of {sorted(allowed)}, got {value!r}")


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
    denoise_ref: bool = True
    temperature: float = 0.4  # SDK exposes it (spike §0); infer default 0.4

    def __post_init__(self) -> None:
        _check_choice("backend", self.backend, _BACKENDS)
        _check_choice("precision", self.precision, _PRECISIONS)
        _check_choice("theme", self.theme, _THEMES)
        if not isinstance(self.default_voice, str) or not self.default_voice.strip():
            raise ValueError("default_voice must be a non-empty string")
        if not isinstance(self.denoise_ref, bool):
            raise ValueError("denoise_ref must be a bool")
        if not isinstance(self.temperature, (int, float)) or isinstance(self.temperature, bool):
            raise ValueError("temperature must be a number")
        if not _TEMPERATURE_MIN <= self.temperature <= _TEMPERATURE_MAX:
            raise ValueError(
                f"temperature must be in [{_TEMPERATURE_MIN}, {_TEMPERATURE_MAX}], "
                f"got {self.temperature}"
            )


@dataclass(frozen=True)
class TTSRequest:
    """One synthesis job (§9)."""

    text: str
    voice: str | None = None
    ref_audio: str | None = None
    denoise: bool = True
    mode: RequestMode = "infer"

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
