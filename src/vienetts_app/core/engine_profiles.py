"""Immutable engine-profile capability model (Phase 1 Task 1).

One explicit, global engine profile owns synthesis at a time:

- ``vieneu`` — the Vietnamese-first default on the existing in-process worker
  (ONNX/CPU or the managed CUDA runtime), 48 kHz native.
- ``qwen_custom_0_6b`` — Qwen3-TTS CustomVoice 0.6B (10 languages + Auto, nine
  fixed speakers), 24 kHz native, served by the isolated model host.
- ``qwen_base_0_6b`` — Qwen3-TTS Base 0.6B (same languages, reference-audio
  cloning, no fixed speakers), 24 kHz native, served by the same host.

Everything here is pure data: no Qt, no torch, no disk. Controllers, jobs and
tests reason about engine compatibility from this module alone, and unsupported
combinations are rejected *before* a job is admitted. The app never guesses an
engine from text or language — the active profile is always explicit.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal

EngineId = Literal["vieneu", "qwen_custom_0_6b", "qwen_base_0_6b"]

VIENEU: EngineId = "vieneu"
QWEN_CUSTOM: EngineId = "qwen_custom_0_6b"
QWEN_BASE: EngineId = "qwen_base_0_6b"

APP_SAMPLE_RATE = 48_000
QWEN_SOURCE_RATE = 24_000

# Pinned from the Phase 0 evidence (docs/performance/qwen-runtime-compatibility.md).
QWEN_CUSTOM_REVISION = "85e237c12c027371202489a0ec509ded67b5e4b5"
QWEN_BASE_REVISION = "5d83992436eae1d760afd27aff78a71d676296fc"

VoicesSource = Literal["vieneu_catalog", "pinned", "enrollment_only"]
CloneRequirement = Literal["reference_clip", "transcript", "consent"]


class EngineProfileError(ValueError):
    """Unsupported profile/language/voice combination; the message is actionable."""


@dataclass(frozen=True)
class LanguageOption:
    """One selectable synthesis language.

    ``code`` is the app-level code used in jobs and settings; ``model_name`` is
    what the engine API needs (Qwen wants full names such as ``"Chinese"``).
    """

    code: str
    label: str  # native endonym, shown as-is in every UI locale
    model_name: str = ""
    is_auto: bool = False


@dataclass(frozen=True)
class VoiceOption:
    """One selectable preset voice (clones come from the clone store)."""

    voice_id: str
    label: str
    languages: tuple[str, ...] = ()
    description: str = ""
    native_language: str = ""


@dataclass(frozen=True)
class EngineCapabilities:
    """Immutable capability descriptor for one engine profile."""

    profile: EngineId
    label: str
    model_repo: str
    model_revision: str
    languages: tuple[LanguageOption, ...]
    voices: tuple[VoiceOption, ...]
    voices_source: VoicesSource
    supports_cloning: bool
    clone_requirements: tuple[CloneRequirement, ...]
    supports_preset_voices: bool
    supports_instruction: bool
    generation_controls: tuple[str, ...]
    source_sample_rate: int
    output_sample_rate: int
    runtime: Literal["vieneu_worker", "qwen_host"]
    devices: tuple[str, ...]
    streaming_granularity: Literal["sdk_chunk", "segment"]
    is_default: bool = False


_QWEN_LANGUAGES: tuple[LanguageOption, ...] = (
    LanguageOption("auto", "Auto", "Auto", is_auto=True),
    LanguageOption("zh", "中文", "Chinese"),
    LanguageOption("en", "English", "English"),
    LanguageOption("ja", "日本語", "Japanese"),
    LanguageOption("ko", "한국어", "Korean"),
    LanguageOption("de", "Deutsch", "German"),
    LanguageOption("fr", "Français", "French"),
    LanguageOption("ru", "Русский", "Russian"),
    LanguageOption("pt", "Português", "Portuguese"),
    LanguageOption("es", "Español", "Spanish"),
    LanguageOption("it", "Italiano", "Italian"),
)

# Official model-card speaker table for the 0.6B CustomVoice checkpoint.
QWEN_SPEAKERS: tuple[VoiceOption, ...] = (
    VoiceOption(
        "Vivian", "Vivian", description="Bright young female voice.", native_language="Chinese"
    ),
    VoiceOption(
        "Serena",
        "Serena",
        description="Warm, gentle young female voice.",
        native_language="Chinese",
    ),
    VoiceOption(
        "Uncle_Fu",
        "Uncle_Fu",
        description="Seasoned male voice, mellow timbre.",
        native_language="Chinese",
    ),
    VoiceOption(
        "Dylan",
        "Dylan",
        description="Youthful Beijing male voice.",
        native_language="Chinese (Beijing)",
    ),
    VoiceOption(
        "Eric",
        "Eric",
        description="Lively Chengdu male voice.",
        native_language="Chinese (Sichuan)",
    ),
    VoiceOption(
        "Ryan", "Ryan", description="Dynamic male voice with rhythm.", native_language="English"
    ),
    VoiceOption(
        "Aiden", "Aiden", description="Sunny American male voice.", native_language="English"
    ),
    VoiceOption(
        "Ono_Anna",
        "Ono_Anna",
        description="Playful Japanese female voice.",
        native_language="Japanese",
    ),
    VoiceOption(
        "Sohee", "Sohee", description="Warm Korean female voice.", native_language="Korean"
    ),
)

_VIENEU_LANGUAGES: tuple[LanguageOption, ...] = (
    LanguageOption("vi", "Tiếng Việt"),
    LanguageOption("en", "English"),
)

_CAPABILITIES: dict[str, EngineCapabilities] = {
    VIENEU: EngineCapabilities(
        profile=VIENEU,
        label="VieNeu-TTS v3 Turbo",
        model_repo="",
        model_revision="",
        languages=_VIENEU_LANGUAGES,
        voices=(),
        voices_source="vieneu_catalog",
        supports_cloning=True,
        clone_requirements=("reference_clip", "consent"),
        supports_preset_voices=True,
        supports_instruction=False,
        generation_controls=("temperature", "speed", "silence_p"),
        source_sample_rate=APP_SAMPLE_RATE,
        output_sample_rate=APP_SAMPLE_RATE,
        runtime="vieneu_worker",
        devices=("cpu", "cuda"),
        streaming_granularity="sdk_chunk",
        is_default=True,
    ),
    QWEN_CUSTOM: EngineCapabilities(
        profile=QWEN_CUSTOM,
        label="Qwen3-TTS CustomVoice 0.6B",
        model_repo="Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice",
        model_revision=QWEN_CUSTOM_REVISION,
        languages=_QWEN_LANGUAGES,
        voices=QWEN_SPEAKERS,
        voices_source="pinned",
        supports_cloning=False,
        clone_requirements=(),
        supports_preset_voices=True,
        # The 0.6B CustomVoice implementation ignores `instruct`; the product
        # must not expose a style control that has no effect.
        supports_instruction=False,
        generation_controls=("speed", "silence_p"),
        source_sample_rate=QWEN_SOURCE_RATE,
        output_sample_rate=APP_SAMPLE_RATE,
        runtime="qwen_host",
        devices=("cpu", "cuda", "mps"),
        streaming_granularity="segment",
    ),
    QWEN_BASE: EngineCapabilities(
        profile=QWEN_BASE,
        label="Qwen3-TTS Base 0.6B",
        model_repo="Qwen/Qwen3-TTS-12Hz-0.6B-Base",
        model_revision=QWEN_BASE_REVISION,
        languages=_QWEN_LANGUAGES,
        voices=(),
        voices_source="enrollment_only",
        supports_cloning=True,
        clone_requirements=("reference_clip", "transcript", "consent"),
        supports_preset_voices=False,
        supports_instruction=False,
        generation_controls=("speed", "silence_p"),
        source_sample_rate=QWEN_SOURCE_RATE,
        output_sample_rate=APP_SAMPLE_RATE,
        runtime="qwen_host",
        devices=("cpu", "cuda", "mps"),
        streaming_granularity="segment",
    ),
}

_QWEN_PROFILES = (QWEN_CUSTOM, QWEN_BASE)


def list_profiles() -> tuple[EngineId, ...]:
    """Stable profile IDs in display order (default first)."""
    return (VIENEU, QWEN_CUSTOM, QWEN_BASE)


def default_profile() -> EngineId:
    """Fresh installs and migrated settings resolve to VieNeu."""
    return VIENEU


def get_capabilities(profile: EngineId) -> EngineCapabilities:
    """Capabilities for ``profile``; unknown IDs raise with the valid choices."""
    try:
        return _CAPABILITIES[profile]
    except (KeyError, TypeError):
        raise EngineProfileError(
            f"unknown engine profile {profile!r} — choose one of: {', '.join(list_profiles())}"
        ) from None


def is_qwen_profile(profile: EngineId) -> bool:
    get_capabilities(profile)
    return profile in _QWEN_PROFILES


#: The name the Qwen runtime layer knows each profile by — the key its install
#: directory, its runtime manifest entry and the model host's profile flag use.
#: Mirrors ``qwen_engine.ENGINE_PROFILE_KEYS`` / ``qwen_host.PROFILE_ENGINES``
#: (asserted in tests) so the controller can name a profile's install without
#: importing the engine layer.
QWEN_RUNTIME_KEYS: Mapping[EngineId, str] = {QWEN_CUSTOM: "customvoice", QWEN_BASE: "base"}


def runtime_key(profile: EngineId) -> str:
    """The runtime/install key for ``profile`` (``""`` for the in-process one)."""
    get_capabilities(profile)
    return QWEN_RUNTIME_KEYS.get(profile, "")


def language_model_name(caps: EngineCapabilities, code: str) -> str:
    """Full engine language name for an app code (``"zh"`` → ``"Chinese"``)."""
    for option in caps.languages:
        if option.code == code:
            return option.model_name
    raise EngineProfileError(
        f"{caps.label} does not support language {code!r} — "
        f"supported: {', '.join(option.code for option in caps.languages)}"
    )


def validate_selection(
    profile: EngineId,
    *,
    language: str = "",
    voice_id: str = "",
    clone_id: str = "",
) -> EngineCapabilities:
    """Validate one profile/language/voice selection before job admission.

    Returns the profile capabilities on success and raises
    :class:`EngineProfileError` with an actionable message otherwise. Pure:
    whether a clone is actually enrolled is the clone store's business.
    """
    caps = get_capabilities(profile)
    code = (language or "").strip()
    if code:
        try:
            language_model_name(caps, code)
        except EngineProfileError:
            if code == "vi" and profile in _QWEN_PROFILES:
                raise EngineProfileError(
                    f"{caps.label} does not support Vietnamese — select VieNeu-TTS "
                    "for Vietnamese synthesis"
                ) from None
            raise
    elif caps.profile != VIENEU:
        raise EngineProfileError(
            f"{caps.label} needs an explicit synthesis language — "
            f"choose one of: {', '.join(option.code for option in caps.languages)}"
        )
    clean_voice = (voice_id or "").strip()
    clean_clone = (clone_id or "").strip()
    if clean_clone and not caps.supports_cloning:
        raise EngineProfileError(
            f"{caps.label} uses fixed speakers and cannot clone voices — "
            "remove the clone or switch to VieNeu-TTS or Qwen3-TTS Base 0.6B"
        )
    if caps.profile == QWEN_CUSTOM:
        names = [voice.voice_id for voice in caps.voices]
        if not clean_voice:
            raise EngineProfileError(
                f"{caps.label} needs a fixed speaker — choose one of: {', '.join(names)}"
            )
        if clean_voice not in names:
            raise EngineProfileError(
                f"unknown Qwen speaker {voice_id!r} — available: {', '.join(names)}"
            )
    if caps.profile == QWEN_BASE:
        if clean_voice:
            fixed = [voice.voice_id for voice in QWEN_SPEAKERS]
            if clean_voice in fixed:
                raise EngineProfileError(
                    f"{clean_voice!r} is a CustomVoice fixed speaker, not an enrolled clone — "
                    "switch the profile to Qwen3-TTS CustomVoice 0.6B or enroll a clone first"
                )
            raise EngineProfileError(
                f"{caps.label} has no preset speakers — it synthesizes only with enrolled "
                f"clones, so {clean_voice!r} cannot be selected"
            )
        if not clean_clone:
            raise EngineProfileError(
                f"{caps.label} needs an enrolled clone — enroll one from a reference clip, "
                "its transcript and a consent acknowledgement first"
            )
    return caps


def model_tag(profile: EngineId, *, model_repo: str = "") -> str:
    """Model-revision identity for cache keys, job provenance and the context.

    VieNeu uses the pinned official manifest revisions (or an explicit custom
    repo tag); Qwen profiles use their pinned checkpoint revision. The profile
    itself is always recorded separately, so a tag never has to encode it.
    """
    caps = get_capabilities(profile)
    if profile == VIENEU:
        if model_repo:
            return f"vieneu-custom:{model_repo}"
        from vienetts_app.core.official_model_manifest import (  # noqa: PLC0415 - lazy, data only
            OFFICIAL_MODEL_MANIFEST,
        )

        manifest = OFFICIAL_MODEL_MANIFEST
        return f"vieneu-official:{manifest.backbone_revision[:12]}+{manifest.codec_revision[:12]}"
    return caps.model_revision
