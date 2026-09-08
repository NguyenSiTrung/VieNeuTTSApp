"""Explicit engine/language selection labels and job-readiness validation.

Spec FR Engine/language selection: the user picks one engine per job
(VieNeu, Qwen CustomVoice, Qwen Base) plus a synthesis language.
Recommendation comes from :func:`backends.recommend_engine` (single source);
this module adds display labels and the strict pre-job gate (fixed-speaker
names, Base enrollment) on top of :func:`backends.validate_selection`.
"""

from __future__ import annotations

from collections.abc import Callable

from vienetts_app.core.backends import (
    QWEN_BASE,
    QWEN_CUSTOMVOICE,
    VIENEU,
    BackendCapabilityError,
    get_capabilities,
)

ENGINE_LABELS: dict[str, str] = {
    VIENEU: "VieNeu (tiếng Việt)",
    QWEN_CUSTOMVOICE: "Qwen CustomVoice (đa ngữ)",
    QWEN_BASE: "Qwen Base (nhân bản)",
}

ENGINE_IDS: tuple[str, str, str] = (VIENEU, QWEN_CUSTOMVOICE, QWEN_BASE)


def validate_selection(
    engine: str,
    language: str | None,
    voice: str | None,
    *,
    is_reference_enrolled: Callable[[str], bool] | None = None,
) -> None:
    """Validate an engine/language/voice combination before a job starts.

    Raises :class:`BackendCapabilityError` with an actionable message naming
    the compatible choices. Pure: engine readiness (model installed) and
    reference enrollment default to pass-through so unit tests and the
    settings-restore path can validate shape without disk state; the
    controller passes a store-backed ``is_reference_enrolled`` for Base.
    """
    try:
        caps = get_capabilities(engine)
    except BackendCapabilityError:
        raise BackendCapabilityError(
            f"unknown TTS engine {engine!r} — choose one of: {', '.join(ENGINE_IDS)}"
        ) from None
    code = (language or "").strip().lower()
    if code and code not in caps.languages:
        raise BackendCapabilityError(
            f"{caps.label} does not support language {language!r} — "
            f"supported: {sorted(caps.languages)}"
        )
    clean_voice = (voice or "").strip()
    if engine == QWEN_CUSTOMVOICE:
        from vienetts_app.core.voices import QWEN_SPEAKERS  # noqa: PLC0415 - lazy, one direction

        names = [speaker.name for speaker in QWEN_SPEAKERS]
        if not clean_voice:
            raise BackendCapabilityError(
                f"Qwen3-TTS CustomVoice needs a fixed speaker — choose one of: {', '.join(names)}"
            )
        if clean_voice not in names:
            raise BackendCapabilityError(
                f"unknown Qwen speaker {voice!r} — available: {', '.join(names)}"
            )
    elif engine == QWEN_BASE:
        if not clean_voice:
            raise BackendCapabilityError(
                "Qwen3-TTS Base needs an enrolled reference voice — "
                "clone a voice from a 3-8 s clip first"
            )
        enrolled = is_reference_enrolled(clean_voice) if is_reference_enrolled else True
        if not enrolled:
            from vienetts_app.core.voices import QWEN_SPEAKERS  # noqa: PLC0415 - lazy

            fixed = {speaker.name for speaker in QWEN_SPEAKERS}
            if clean_voice in fixed:
                raise BackendCapabilityError(
                    f"{clean_voice!r} is a CustomVoice fixed speaker, not an enrolled "
                    "Base reference — switch engine to Qwen CustomVoice or enroll it first"
                )
            raise BackendCapabilityError(
                f"no Qwen Base reference voice named {clean_voice!r} — "
                "enroll it from a 3-8 s clip first"
            )
