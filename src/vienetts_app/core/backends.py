"""Backend-neutral TTS capability model (Phase 1 Task 1).

Stable engine/profile identifiers plus immutable capability descriptors.
Keeps backend-specific behavior behind data + validation instead of
repository-string checks scattered through controllers and QML.

Engines:
- ``vieneu``: VieNeu-TTS v3 Turbo (ONNX/CPU default, Vietnamese-first,
  20 preset voices + reference-audio cloning, native 48 kHz).
- ``qwen_customvoice``: Qwen3-TTS CustomVoice (fixed speakers +
  natural-language style instructions, native 24 kHz, torch runtime).
- ``qwen_base``: Qwen3-TTS Base (reference-audio cloning, no fixed
  preset speakers, native 24 kHz, torch runtime).

Qwen does not support Vietnamese; validation directs those users to VieNeu.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

EngineId = Literal["vieneu", "qwen_customvoice", "qwen_base"]

VIENEU: EngineId = "vieneu"
QWEN_CUSTOMVOICE: EngineId = "qwen_customvoice"
QWEN_BASE: EngineId = "qwen_base"

_ENGINES = frozenset((VIENEU, QWEN_CUSTOMVOICE, QWEN_BASE))

# Languages Qwen3-TTS documents support (subset relevant to the app).
# Vietnamese is deliberately absent — Qwen must reject it with a
# targeted message pointing at VieNeu (spec AC-5).
_QWEN_LANGUAGES = ("en", "zh", "ja", "ko", "de", "fr", "ru", "es", "it", "pt")


class BackendCapabilityError(ValueError):
    """Engine/language/voice combination is unsupported; message is actionable."""


@dataclass(frozen=True)
class BackendCapabilities:
    """Immutable capability descriptor for one engine/profile."""

    engine: EngineId
    label: str
    languages: tuple[str, ...]
    supports_cloning: bool
    supports_preset_voices: bool
    supports_instruction: bool
    runtime: str  # "onnx" (torch-free) or "torch" (optional Qwen runtime)
    native_sample_rate: int
    requires_torch: bool
    is_default: bool = False


_CAPABILITIES: dict[str, BackendCapabilities] = {
    VIENEU: BackendCapabilities(
        engine=VIENEU,
        label="VieNeu-TTS v3 Turbo",
        languages=("vi", "en"),
        supports_cloning=True,
        supports_preset_voices=True,
        supports_instruction=False,
        runtime="onnx",
        native_sample_rate=48000,
        requires_torch=False,
        is_default=True,
    ),
    QWEN_CUSTOMVOICE: BackendCapabilities(
        engine=QWEN_CUSTOMVOICE,
        label="Qwen3-TTS CustomVoice",
        languages=_QWEN_LANGUAGES,
        supports_cloning=False,
        supports_preset_voices=True,
        supports_instruction=True,
        runtime="torch",
        native_sample_rate=24000,
        requires_torch=True,
    ),
    QWEN_BASE: BackendCapabilities(
        engine=QWEN_BASE,
        label="Qwen3-TTS Base",
        languages=_QWEN_LANGUAGES,
        supports_cloning=True,
        supports_preset_voices=False,
        supports_instruction=False,
        runtime="torch",
        native_sample_rate=24000,
        requires_torch=True,
    ),
}


def list_engines() -> tuple[EngineId, ...]:
    """Stable engine IDs in display order (default first)."""
    return (VIENEU, QWEN_CUSTOMVOICE, QWEN_BASE)


def get_capabilities(engine: EngineId) -> BackendCapabilities:
    """Return the capability descriptor for ``engine``."""
    try:
        return _CAPABILITIES[engine]
    except KeyError:
        raise BackendCapabilityError(
            f"unknown engine {engine!r}; expected one of {sorted(_ENGINES)}"
        ) from None


def recommend_engine(language: str) -> EngineId | None:
    """Recommend an engine for ``language`` without switching anything.

    Vietnamese → VieNeu; Qwen-only languages → Qwen CustomVoice;
    English works on the lightweight default → VieNeu; anything else
    has no recommendation (None) so callers keep the user selection.
    """
    if language == "vi":
        return VIENEU
    if language == "en":
        return VIENEU
    if language in _QWEN_LANGUAGES:
        return QWEN_CUSTOMVOICE
    return None


def validate_selection(
    *,
    engine: EngineId,
    language: str,
    voice: str | None = None,
    ref_audio: str | None = None,
    instruction: str | None = None,
) -> BackendCapabilities:
    """Validate an engine/language/voice combination before a job starts.

    Returns the engine's capabilities on success; raises
    :class:`BackendCapabilityError` with an actionable message otherwise.
    """
    caps = get_capabilities(engine)
    if language not in caps.languages:
        if engine in (QWEN_CUSTOMVOICE, QWEN_BASE) and language == "vi":
            raise BackendCapabilityError(
                "Qwen does not support Vietnamese — select VieNeu-TTS for Vietnamese synthesis"
            )
        raise BackendCapabilityError(
            f"{caps.label} does not support language {language!r}; "
            f"supported: {', '.join(caps.languages)}"
        )
    if ref_audio is not None and not caps.supports_cloning:
        raise BackendCapabilityError(
            f"{caps.label} uses fixed speakers and does not support "
            "voice cloning — remove the reference audio or switch to "
            "VieNeu-TTS or Qwen3-TTS Base"
        )
    if instruction is not None and instruction.strip() and not caps.supports_instruction:
        raise BackendCapabilityError(
            f"{caps.label} does not support style instructions — "
            "clear the instruction field or switch to Qwen3-TTS CustomVoice"
        )
    if engine == QWEN_BASE and voice is not None and voice.strip() and ref_audio is None:
        raise BackendCapabilityError(
            "Qwen3-TTS Base has no preset speakers — "
            "provide reference audio to enroll a voice first"
        )
    return caps


def default_model_tag(engine: EngineId, model_repo: str = "") -> str:
    """Model-revision tag for cache identity.

    VieNeu official baseline → pinned backbone+codec short revisions from
    the frozen manifest; a custom backbone repo → ``vieneu-custom:<repo>``.
    Qwen profiles have no pinned revision yet (Phase 3 registers real
    loaders) → an explicit ``unmanaged`` marker that still separates the
    profiles from each other and from VieNeu.
    """
    get_capabilities(engine)  # raises BackendCapabilityError on unknown engine
    if engine == VIENEU:
        if model_repo:
            return f"vieneu-custom:{model_repo}"
        from vienetts_app.core.official_model_manifest import OFFICIAL_MODEL_MANIFEST

        manifest = OFFICIAL_MODEL_MANIFEST
        return f"vieneu-official:{manifest.backbone_revision[:12]}+{manifest.codec_revision[:12]}"
    return f"{engine}:unmanaged"
