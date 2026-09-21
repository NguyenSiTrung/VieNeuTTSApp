"""Immutable synthesis context: job identity, provenance and cache identity.

Every synthesis job snapshots *what* produced its audio — engine profile,
pinned model revision, language, preset voice or clone, and the generation
settings that change the waveform. Caches, render provenance and Studio
re-synthesis compare these records, so an artifact from one engine/profile/
model revision can never be reused by another, and no engine is ever
substituted silently.

Validation bounds live here (not in ``core.models``) so the context and the
user-facing ``Settings``/``TTSRequest`` contracts cannot drift apart.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from typing import Any

from vienetts_app.core import engine_profiles
from vienetts_app.core.engine_profiles import EngineId

TEMPERATURE_MIN = 0.05
TEMPERATURE_MAX = 2.0
SPEED_MIN = 0.5
SPEED_MAX = 2.0
SILENCE_P_MIN = 0.0
SILENCE_P_MAX = 2.0


def check_temperature(value: object, *, allow_none: bool) -> float | None:
    """Validate temperature against the shared bounds (``None`` = engine default)."""
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"temperature must be a number or None, got {value!r}")
    if not TEMPERATURE_MIN <= value <= TEMPERATURE_MAX:
        raise ValueError(
            f"temperature must be in [{TEMPERATURE_MIN}, {TEMPERATURE_MAX}], got {value}"
        )
    return float(value)


def check_speed(value: object, *, allow_none: bool) -> float | None:
    """Validate speech rate against bounds [0.5, 2.0]."""
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"speed must be a number or None, got {value!r}")
    if not SPEED_MIN <= value <= SPEED_MAX:
        raise ValueError(f"speed must be in [{SPEED_MIN}, {SPEED_MAX}], got {value}")
    return float(value)


def check_silence_p(value: object, *, allow_none: bool) -> float | None:
    """Validate pause length against bounds [0.0, 2.0]."""
    if value is None and allow_none:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"silence_p must be a number or None, got {value!r}")
    if not SILENCE_P_MIN <= value <= SILENCE_P_MAX:
        raise ValueError(f"silence_p must be in [{SILENCE_P_MIN}, {SILENCE_P_MAX}], got {value}")
    return float(value)


@dataclass(frozen=True)
class GenerationSettings:
    """Generation parameters that change the rendered waveform.

    ``None`` means "use the engine default" (VieNeu's SDK defaults, or the
    Qwen host's fixed sampling settings); values are already validated here so
    a context can never carry an out-of-range setting into a cache key.
    """

    temperature: float | None = None
    speed: float | None = None
    silence_p: float | None = None

    def __post_init__(self) -> None:
        check_temperature(self.temperature, allow_none=True)
        check_speed(self.speed, allow_none=True)
        check_silence_p(self.silence_p, allow_none=True)

    def payload(self) -> dict[str, Any]:
        """Deterministic mapping for fingerprints and provenance records."""
        return {
            "temperature": self.temperature,
            "speed": self.speed,
            "silenceP": self.silence_p,
        }


@dataclass(frozen=True)
class SynthesisContext:
    """Frozen identity of one synthesis request.

    Compatibility (language / speaker / clone) is validated at construction
    through :func:`engine_profiles.validate_selection`, so an incompatible
    combination is rejected before the job reaches the queue.
    """

    profile: EngineId
    model_revision: str
    language: str = ""
    voice_id: str = ""
    clone_id: str = ""
    generation: GenerationSettings = field(default_factory=GenerationSettings)

    def __post_init__(self) -> None:
        if not isinstance(self.model_revision, str) or not self.model_revision.strip():
            raise ValueError("model_revision must be a non-empty, non-blank string")
        engine_profiles.validate_selection(
            self.profile,
            language=self.language,
            voice_id=self.voice_id,
            clone_id=self.clone_id,
        )

    @property
    def capabilities(self) -> engine_profiles.EngineCapabilities:
        return engine_profiles.get_capabilities(self.profile)

    def fingerprint_payload(self) -> dict[str, Any]:
        """Everything that changes the rendered audio, as a plain mapping."""
        return {
            "profile": self.profile,
            "modelRevision": self.model_revision,
            "language": self.language,
            "voiceId": self.voice_id,
            "cloneId": self.clone_id,
            "generation": self.generation.payload(),
        }

    def fingerprint(self) -> str:
        """Stable SHA-256 over :meth:`fingerprint_payload` (cache identity)."""
        encoded = json.dumps(
            self.fingerprint_payload(),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(encoded).hexdigest()


def context_for(
    profile: EngineId,
    *,
    language: str = "",
    voice_id: str = "",
    clone_id: str = "",
    generation: GenerationSettings | None = None,
    model_repo: str = "",
) -> SynthesisContext:
    """Build a context for ``profile`` with its pinned model-revision tag.

    ``model_repo`` applies to VieNeu only (custom backbone override); Qwen
    profiles always use their pinned checkpoint revision.
    """
    return SynthesisContext(
        profile=profile,
        model_revision=engine_profiles.model_tag(profile, model_repo=model_repo),
        language=language,
        voice_id=voice_id,
        clone_id=clone_id,
        generation=generation or GenerationSettings(),
    )


def context_from_payload(payload: Any) -> SynthesisContext | None:
    """Rebuild a context from :meth:`SynthesisContext.fingerprint_payload`.

    The inverse of ``fingerprint_payload``, used by the persisted render
    workspaces (audiobook chapter state, subtitle projects). Fail-soft: a
    payload that is not well formed returns ``None`` — "unknown engine
    identity" — instead of raising, so a corrupt or hand-edited project file
    degrades the way every other workspace reader does.
    """
    if not isinstance(payload, dict):
        return None
    raw_generation = payload.get("generation")
    generation = raw_generation if isinstance(raw_generation, dict) else {}
    try:
        return SynthesisContext(
            profile=payload.get("profile"),  # type: ignore[arg-type]
            model_revision=payload.get("modelRevision"),  # type: ignore[arg-type]
            language=str(payload.get("language") or ""),
            voice_id=str(payload.get("voiceId") or ""),
            clone_id=str(payload.get("cloneId") or ""),
            generation=GenerationSettings(
                temperature=generation.get("temperature"),
                speed=generation.get("speed"),
                silence_p=generation.get("silenceP"),
            ),
        )
    except (TypeError, ValueError):
        return None


def legacy_render_compatible(requested: SynthesisContext) -> bool:
    """Whether a render with NO recorded identity may serve ``requested``.

    Renders written before engine provenance existed were produced by the
    pre-multi-engine app: VieNeu, the only engine it had. Any other profile
    must re-render rather than reuse audio it cannot vouch for.
    """
    return requested.profile == engine_profiles.VIENEU


def context_matches(stored: SynthesisContext | None, requested: SynthesisContext | None) -> bool:
    """Whether a stored render identity may serve a requested one.

    The truth table is deliberately conservative about unknown identities:

    - both known → identical payloads only (profile, model revision, language,
      voice/clone and generation settings all change the audio);
    - stored unknown → reusable only for a VieNeu request
      (:func:`legacy_render_compatible`);
    - requested unknown (no identity seam on the app, or a combination the
      capability table refused) → only another unknown identity may reuse it.

    A mismatch means the cached render is INVALID for the request, never that
    the engine may be substituted silently.
    """
    if requested is None:
        return stored is None
    if stored is None:
        return legacy_render_compatible(requested)
    return stored.fingerprint_payload() == requested.fingerprint_payload()
