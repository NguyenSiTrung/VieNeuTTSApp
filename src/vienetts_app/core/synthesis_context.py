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

from vienetts_app.core import engine_profiles, qwen_variants
from vienetts_app.core.engine_profiles import EngineId

#: Payload schema for ``SynthesisContext.fingerprint_payload``. v1 is the
#: pre-variant shape (no ``contextVersion`` key); v2 adds the model-format
#: block (``modelFormat``/``quantization``/``engine``/``resolvedDevice`` and
#: the runtime/model/tokenizer identities). v2 is emitted only when a variant
#: field carries data, so VieNeu and unstamped-official renders keep the v1
#: bytes — and every fingerprint — they always had.
CONTEXT_PAYLOAD_VERSION = 2

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
    # Model-format variant identity (core/qwen_variants.py). Qwen profiles
    # normalize an empty model_format to "official"; a non-Qwen profile must
    # leave every variant field empty. Identities (runtime/model/tokenizer)
    # are stamped when the artifacts are resolved — "" means unresolved and
    # can never exact-match a stamped render.
    model_format: str = ""
    quantization: str = ""
    engine: str = ""
    resolved_device: str = ""
    runtime_identity: str = ""
    model_identity: str = ""
    tokenizer_identity: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.model_revision, str) or not self.model_revision.strip():
            raise ValueError("model_revision must be a non-empty, non-blank string")
        engine_profiles.validate_selection(
            self.profile,
            language=self.language,
            voice_id=self.voice_id,
            clone_id=self.clone_id,
        )
        self._normalize_variant()

    def _normalize_variant(self) -> None:
        """Pin the variant triple to one deterministic vocabulary.

        Qwen contexts resolve through :func:`qwen_variants.variant_for` so an
        invalid (format, quantization) pair or a contradictory ``engine`` is
        rejected here, before the context can reach a job or a cache key.
        ``resolved_device`` is the concrete device the render used — "auto"
        is a selection, not a resolution, and normalizes to "".
        """
        if not engine_profiles.is_qwen_profile(self.profile):
            if any(
                (
                    self.model_format,
                    self.quantization,
                    self.engine,
                    self.resolved_device,
                    self.runtime_identity,
                    self.model_identity,
                    self.tokenizer_identity,
                )
            ):
                raise ValueError("model-format variant fields apply to Qwen profiles only")
            return
        variant = qwen_variants.variant_for(
            self.profile,
            model_format=self.model_format or qwen_variants.MODEL_FORMAT_OFFICIAL,
            quantization=self.quantization,
        )
        engine = self.engine or variant.engine
        if engine != variant.engine:
            raise ValueError(
                f"engine {engine!r} contradicts the {variant.model_format} "
                f"format — it is served by {variant.engine!r}"
            )
        device = "" if self.resolved_device == "auto" else self.resolved_device
        if device and device not in variant.devices:
            raise ValueError(
                f"resolved_device {device!r} is not a {variant.engine} device — "
                f"expected one of: {', '.join(variant.devices)}"
            )
        object.__setattr__(self, "model_format", variant.model_format)
        object.__setattr__(self, "quantization", variant.quantization)
        object.__setattr__(self, "engine", engine)
        object.__setattr__(self, "resolved_device", device)

    @property
    def variant(self) -> qwen_variants.QwenVariant | None:
        """The resolved model-format variant (``None`` for non-Qwen profiles)."""
        if not engine_profiles.is_qwen_profile(self.profile):
            return None
        return qwen_variants.variant_for(
            self.profile, model_format=self.model_format, quantization=self.quantization
        )

    @property
    def capabilities(self) -> engine_profiles.EngineCapabilities:
        return engine_profiles.get_capabilities(self.profile)

    def _carries_variant_identity(self) -> bool:
        """Whether any variant field holds data worth recording.

        The v2 block is emitted exactly then: an unstamped official context
        serializes identically to a pre-variant one, so existing renders keep
        their fingerprints instead of being invalidated by a schema change.
        """
        return self.model_format == qwen_variants.MODEL_FORMAT_GGUF or bool(
            self.resolved_device
            or self.runtime_identity
            or self.model_identity
            or self.tokenizer_identity
        )

    def fingerprint_payload(self) -> dict[str, Any]:
        """Everything that changes the rendered audio, as a plain mapping."""
        payload = {
            "profile": self.profile,
            "modelRevision": self.model_revision,
            "language": self.language,
            "voiceId": self.voice_id,
            "cloneId": self.clone_id,
            "generation": self.generation.payload(),
        }
        if self._carries_variant_identity():
            payload["contextVersion"] = CONTEXT_PAYLOAD_VERSION
            payload["modelFormat"] = self.model_format
            payload["quantization"] = self.quantization
            payload["engine"] = self.engine
            payload["resolvedDevice"] = self.resolved_device
            payload["runtimeIdentity"] = self.runtime_identity
            payload["modelIdentity"] = self.model_identity
            payload["tokenizerIdentity"] = self.tokenizer_identity
        return payload

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
    variant: qwen_variants.QwenVariant | None = None,
    resolved_device: str = "",
    runtime_identity: str = "",
    model_identity: str = "",
    tokenizer_identity: str = "",
) -> SynthesisContext:
    """Build a context for ``profile`` with its pinned model-revision tag.

    ``model_repo`` applies to VieNeu only (custom backbone override); Qwen
    profiles always use their pinned checkpoint revision. ``variant`` is the
    selected model format for a Qwen profile (``None`` = official weights);
    the three identities stamp the exact artifacts once they are resolved.
    """
    if variant is not None and variant.profile != profile:
        raise ValueError(f"variant for {variant.profile!r} cannot stamp a {profile!r} context")
    return SynthesisContext(
        profile=profile,
        model_revision=engine_profiles.model_tag(profile, model_repo=model_repo),
        language=language,
        voice_id=voice_id,
        clone_id=clone_id,
        generation=generation or GenerationSettings(),
        model_format=variant.model_format if variant is not None else "",
        quantization=variant.quantization if variant is not None else "",
        engine=variant.engine if variant is not None else "",
        resolved_device=resolved_device,
        runtime_identity=runtime_identity,
        model_identity=model_identity,
        tokenizer_identity=tokenizer_identity,
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
    # Payload schema: absent ``contextVersion`` means the pre-variant shape
    # (v1) — every Qwen render it could name was produced by the official
    # host, so it decodes as ``official`` through the constructor's
    # normalization. A version this code does not know can never decode —
    # an unreadable identity, not a guessed one. Variant keys on a v1
    # payload are ignored: they cannot smuggle in a GGUF identity.
    version = payload.get("contextVersion", 1)
    if (
        isinstance(version, bool)
        or not isinstance(version, int)
        or version not in (1, CONTEXT_PAYLOAD_VERSION)
    ):
        return None
    raw_generation = payload.get("generation")
    generation = raw_generation if isinstance(raw_generation, dict) else {}
    variant_keys = (
        {
            "model_format": str(payload.get("modelFormat") or ""),
            "quantization": str(payload.get("quantization") or ""),
            "engine": str(payload.get("engine") or ""),
            "resolved_device": str(payload.get("resolvedDevice") or ""),
            "runtime_identity": str(payload.get("runtimeIdentity") or ""),
            "model_identity": str(payload.get("modelIdentity") or ""),
            "tokenizer_identity": str(payload.get("tokenizerIdentity") or ""),
        }
        if version == CONTEXT_PAYLOAD_VERSION
        else {}
    )
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
            **variant_keys,
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


def same_engine(stored: SynthesisContext | None, requested: SynthesisContext) -> bool:
    """Whether ``requested`` belongs to the ENGINE that produced a stored render.

    Looser than :func:`context_matches`, for the one flow where the user is
    explicitly asking for new audio: an interactive re-synthesis (Studio's
    "Tạo lại" on a clip) chooses a new voice and text on purpose, so language
    and generation settings are the user's to change — the ENGINE is the part
    that must never be substituted silently. A stored render with no recorded
    identity was produced by VieNeu, the only engine the app had then.

    "Engine" includes the model-format variant: a Q8_0 render and a Q4_K_M
    render come from different artifacts, and official vs GGUF are different
    engines — a quantization or format switch is a new engine as far as an
    existing render slot is concerned.
    """
    if stored is None:
        return legacy_render_compatible(requested)
    return (
        stored.profile,
        stored.model_format,
        stored.quantization,
        stored.engine,
    ) == (
        requested.profile,
        requested.model_format,
        requested.quantization,
        requested.engine,
    )


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
