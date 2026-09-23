"""Model-format variants of the Qwen profiles (track qwen_gguf_engine_20260923).

A *variant* is one selection dimension on top of the existing semantic
profiles — it never mints a new ``EngineId``:

- ``official`` — full weights, served by the isolated PyTorch model host
  (``qwen_host``); devices ``cpu`` / ``cuda`` / ``mps`` with the locked
  precision matrix (CPU/MPS float32, CUDA bfloat16).
- ``gguf`` — quantized GGUF, served by the managed ``qwentts.cpp`` host;
  quantizations ``Q8_0`` / ``Q4_K_M``; devices ``cpu`` / ``cuda`` / ``metal``
  using ggml's native names (``Metal``, never PyTorch's ``mps``).

Pure data — no Qt, no native loads, no disk.  Device *availability* is a
runtime/hardware question resolved elsewhere; this module only pins which
vocabulary each engine speaks.  Capabilities come from the shared
``engine_profiles`` table — narrowed by Phase 1 evidence (the GGUF codec
reports the same ten languages + Auto and the same nine CustomVoice
speakers), never duplicated here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from vienetts_app.core.engine_profiles import (
    EngineCapabilities,
    EngineId,
    EngineProfileError,
    get_capabilities,
    is_qwen_profile,
)

if TYPE_CHECKING:
    from vienetts_app.core.models import Settings

MODEL_FORMAT_OFFICIAL = "official"
MODEL_FORMAT_GGUF = "gguf"
MODEL_FORMATS = (MODEL_FORMAT_OFFICIAL, MODEL_FORMAT_GGUF)

GGUF_QUANTIZATIONS = ("Q8_0", "Q4_K_M")
DEFAULT_GGUF_QUANTIZATION = "Q8_0"

ENGINE_PYTORCH = "pytorch"
ENGINE_QWENTTS_CPP = "qwentts_cpp"
ENGINES = (ENGINE_PYTORCH, ENGINE_QWENTTS_CPP)

# Engine-scoped device vocabularies: the PyTorch host speaks mps, the native
# host speaks Metal — they are different backends even on the same hardware.
OFFICIAL_DEVICES = ("cpu", "cuda", "mps")
GGUF_DEVICES = ("cpu", "cuda", "metal")

_DEVICE_LABELS = {
    "auto": "Auto",
    "cpu": "CPU",
    "cuda": "CUDA",
    "mps": "MPS",
    "metal": "Metal",
}


class VariantError(ValueError):
    """Unsupported profile/format/quantization combination."""


@dataclass(frozen=True)
class QwenVariant:
    """One resolved (profile, format, quantization) selection."""

    profile: EngineId
    model_format: str
    quantization: str
    engine: str
    devices: tuple[str, ...]

    @property
    def capabilities(self) -> EngineCapabilities:
        """The shared semantic capability table for this profile."""
        return get_capabilities(self.profile)


def device_label(device: str) -> str:
    """Display label for a device id (``metal`` → ``Metal``, ``mps`` → ``MPS``)."""
    return _DEVICE_LABELS.get(device, device)


def variant_for(
    profile: EngineId,
    model_format: str = MODEL_FORMAT_OFFICIAL,
    quantization: str = "",
) -> QwenVariant:
    """Resolve the variant for ``profile``; invalid combinations raise.

    ``official`` takes no quantization.  ``gguf`` defaults an empty
    quantization to ``Q8_0`` — the default variant on first selection —
    and rejects anything outside ``Q8_0``/``Q4_K_M``.
    """
    try:
        qwen = is_qwen_profile(profile)
    except EngineProfileError as exc:
        raise VariantError(str(exc)) from exc
    if not qwen:
        raise VariantError(
            f"{profile!r} is not a Qwen profile — variants exist only for "
            "qwen_custom_0_6b and qwen_base_0_6b"
        )
    if model_format not in MODEL_FORMATS:
        raise VariantError(
            f"unknown model format {model_format!r} — expected one of: {', '.join(MODEL_FORMATS)}"
        )
    if model_format == MODEL_FORMAT_OFFICIAL:
        if quantization:
            raise VariantError(
                "official full weights take no quantization — clear it or select the GGUF format"
            )
        return QwenVariant(
            profile=profile,
            model_format=MODEL_FORMAT_OFFICIAL,
            quantization="",
            engine=ENGINE_PYTORCH,
            devices=OFFICIAL_DEVICES,
        )
    quant = quantization or DEFAULT_GGUF_QUANTIZATION
    if quant not in GGUF_QUANTIZATIONS:
        raise VariantError(
            f"unknown GGUF quantization {quant!r} — expected one of: "
            f"{', '.join(GGUF_QUANTIZATIONS)}"
        )
    return QwenVariant(
        profile=profile,
        model_format=MODEL_FORMAT_GGUF,
        quantization=quant,
        engine=ENGINE_QWENTTS_CPP,
        devices=GGUF_DEVICES,
    )


def resolve_variant(settings: Settings) -> QwenVariant | None:
    """The variant the settings describe — ``None`` for the in-process profile."""
    profile = settings.engine_profile
    if not is_qwen_profile(profile):
        return None
    # The quantization field is an inactive preference while the official
    # format is selected — it is remembered, not part of that variant.
    return variant_for(
        profile,
        model_format=settings.qwen_model_format,
        quantization=(
            settings.qwen_gguf_quantization
            if settings.qwen_model_format == MODEL_FORMAT_GGUF
            else ""
        ),
    )
