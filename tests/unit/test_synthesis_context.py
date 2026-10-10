"""Synthesis context: immutable job identity, validation, stable fingerprints.

The context is the single provenance record for a synthesized artifact: which
profile, which model revision, which language, which voice or clone, and which
generation settings produced it. Caches key on it; Studio re-synthesis compares
against it; nothing may substitute one engine for another silently.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from vienetts_app.core import engine_profiles as ep
from vienetts_app.core import qwen_variants as qv
from vienetts_app.core.synthesis_context import (
    GenerationSettings,
    SynthesisContext,
    context_for,
    context_from_payload,
    context_matches,
    legacy_render_compatible,
    same_engine,
)


class TestGenerationSettings:
    def test_contract_and_bounds(self) -> None:
        assert GenerationSettings(temperature=0.05).temperature == pytest.approx(0.05)
        assert GenerationSettings(temperature=2.0).temperature == pytest.approx(2.0)
        assert GenerationSettings(speed=0.5).speed == pytest.approx(0.5)
        assert GenerationSettings(silence_p=2.0).silence_p == pytest.approx(2.0)
        for bad in (0.0, 2.5, -1.0):
            with pytest.raises(ValueError, match="temperature"):
                GenerationSettings(temperature=bad)
        for bad in (0.4, 2.5, -1.0):
            with pytest.raises(ValueError, match="speed"):
                GenerationSettings(speed=bad)
        for bad in (-0.1, 2.5):
            with pytest.raises(ValueError, match="silence_p"):
                GenerationSettings(silence_p=bad)
        for bad_type in ("0.5", True):
            with pytest.raises(ValueError, match="speed"):
                GenerationSettings(speed=bad_type)  # type: ignore[arg-type]


class TestConstruction:
    def test_incompatible_combinations_are_rejected(self) -> None:
        with pytest.raises(ep.EngineProfileError):
            SynthesisContext(profile="qwen9", model_revision="x")
        with pytest.raises(ep.EngineProfileError):
            SynthesisContext(
                profile=ep.QWEN_CUSTOM, model_revision="x", language="vi", voice_id="Ryan"
            )
        with pytest.raises(ep.EngineProfileError):
            SynthesisContext(profile=ep.QWEN_CUSTOM, model_revision="x", language="zh")
        with pytest.raises(ep.EngineProfileError):
            SynthesisContext(profile=ep.QWEN_BASE, model_revision="x", language="en")

    def test_validation(self) -> None:
        with pytest.raises(ValueError, match="model_revision"):
            SynthesisContext(profile=ep.VIENEU, model_revision="   ")


class TestFingerprint:
    def test_fingerprint_determinism(self) -> None:
        context = context_for(
            ep.QWEN_CUSTOM,
            language="zh",
            voice_id="Vivian",
            generation=GenerationSettings(speed=1.25, silence_p=0.2),
        )
        payload = context.fingerprint_payload()
        assert payload == {
            "profile": "qwen_custom_0_6b",
            "modelRevision": ep.get_capabilities(ep.QWEN_CUSTOM).model_revision,
            "language": "zh",
            "voiceId": "Vivian",
            "cloneId": "",
            "generation": {"temperature": None, "speed": 1.25, "silenceP": 0.2},
        }
        assert json.loads(json.dumps(payload)) == payload

        first = context_for(ep.VIENEU, language="vi", voice_id="Adam")
        second = context_for(ep.VIENEU, language="vi", voice_id="Adam")
        assert first.fingerprint() == second.fingerprint()

        official = context_for(ep.VIENEU, language="vi", voice_id="Adam")
        custom = context_for(ep.VIENEU, language="vi", voice_id="Adam", model_repo="owner/custom")
        assert official.fingerprint() != custom.fingerprint()

    def test_every_identity_field_changes_the_fingerprint(self) -> None:
        base = context_for(ep.QWEN_CUSTOM, language="zh", voice_id="Vivian")
        variants = [
            context_for(ep.QWEN_BASE, language="zh", clone_id="c0ffee"),
            context_for(ep.QWEN_CUSTOM, language="en", voice_id="Vivian"),
            context_for(ep.QWEN_CUSTOM, language="zh", voice_id="Ryan"),
            dataclasses.replace(base, model_revision="qwen_custom_0_6b@other"),
            context_for(
                ep.QWEN_CUSTOM,
                language="zh",
                voice_id="Vivian",
                generation=GenerationSettings(speed=1.5),
            ),
        ]
        fingerprints = {base.fingerprint()} | {variant.fingerprint() for variant in variants}
        assert len(fingerprints) == len(variants) + 1


class TestContextFor:
    def test_context_for_contracts(self) -> None:
        context = context_for(ep.QWEN_BASE, language="ja", clone_id="clone-1")
        assert context.model_revision == ep.model_tag(ep.QWEN_BASE)

        assert context_for(ep.VIENEU).model_revision == ep.model_tag(ep.VIENEU)

        with pytest.raises(ep.EngineProfileError):
            context_for("qwen1_7b")  # type: ignore[arg-type]


class TestPayloadRoundTrip:
    """Task 5.3: persisted provenance is the payload, read back fail-soft."""

    def test_payload_decode_edge_cases(self) -> None:
        context = context_for(
            ep.QWEN_CUSTOM,
            language="zh",
            voice_id="Vivian",
            generation=GenerationSettings(temperature=0.9, speed=1.2, silence_p=0.1),
        )
        assert context_from_payload(context.fingerprint_payload()) == context
        # Through JSON exactly as the workspaces persist it.
        payload = json.loads(json.dumps(context.fingerprint_payload()))
        assert context_from_payload(payload) == context

        context = context_from_payload(
            {"profile": ep.VIENEU, "modelRevision": "v", "generation": "junk"}
        )
        assert context == SynthesisContext(profile=ep.VIENEU, model_revision="v")

        context = context_from_payload(
            {"profile": ep.VIENEU, "modelRevision": "v", "generation": {"speed": 1.5}}
        )
        assert context is not None
        assert context.generation == GenerationSettings(speed=1.5)

    @pytest.mark.parametrize(
        "payload",
        [
            None,
            5,
            "vieneu",
            [],
            {},
            {"profile": "qwen_9b", "modelRevision": "x"},  # unknown profile
            {"profile": ep.VIENEU},  # no revision
            {"profile": ep.VIENEU, "modelRevision": 5},  # wrong type
            {"profile": ep.QWEN_CUSTOM, "modelRevision": "x", "language": "vi"},
            {"profile": ep.QWEN_CUSTOM, "modelRevision": "x", "language": "zh"},  # no speaker
        ],
    )
    def test_a_malformed_payload_is_an_unknown_identity(self, payload) -> None:
        assert context_from_payload(payload) is None


class TestRenderCompatibility:
    """Task 5.3: which stored render identity may serve which request."""

    def test_render_compatibility_contract(self) -> None:
        assert legacy_render_compatible(context_for(ep.VIENEU)) is True
        assert (
            legacy_render_compatible(context_for(ep.QWEN_CUSTOM, language="zh", voice_id="Vivian"))
            is False
        )
        assert (
            legacy_render_compatible(context_for(ep.QWEN_BASE, language="zh", clone_id="c1"))
            is False
        )

        first = context_for(ep.QWEN_CUSTOM, language="zh", voice_id="Vivian")
        second = context_for(ep.QWEN_CUSTOM, language="zh", voice_id="Vivian")
        assert context_matches(first, second) is True

        stored = context_for(ep.QWEN_CUSTOM, language="zh", voice_id="Vivian")
        variants = [
            context_for(ep.QWEN_BASE, language="zh", clone_id="c0ffee"),
            context_for(ep.QWEN_CUSTOM, language="en", voice_id="Vivian"),
            context_for(ep.QWEN_CUSTOM, language="zh", voice_id="Ryan"),
            dataclasses.replace(stored, model_revision="qwen_custom_0_6b@other"),
            context_for(
                ep.QWEN_CUSTOM,
                language="zh",
                voice_id="Vivian",
                generation=GenerationSettings(speed=1.5),
            ),
        ]
        for variant in variants:
            assert context_matches(stored, variant) is False
            assert context_matches(variant, stored) is False

        known = context_for(ep.VIENEU)
        assert context_matches(None, None) is True
        assert context_matches(known, None) is False
        # A legacy render (no identity) serves a VieNeu request...
        assert context_matches(None, known) is True
        # ...and never a Qwen one.
        assert (
            context_matches(None, context_for(ep.QWEN_BASE, language="zh", clone_id="c1")) is False
        )


class TestSameEngine:
    """Task 5.4: an explicit re-synthesis needs the ENGINE, not the exact request.

    Studio's "Tạo lại" picks a new voice and text on purpose, so language and
    generation settings are the user's to change — the engine is the part that
    must never be substituted silently.
    """

    def test_same_engine_contract(self) -> None:
        stored = context_for(ep.VIENEU, language="vi", voice_id="Adam")
        assert same_engine(stored, context_for(ep.VIENEU, voice_id="Hà Vy")) is True
        assert (
            same_engine(
                stored,
                context_for(ep.VIENEU, voice_id="Adam", generation=GenerationSettings(speed=1.5)),
            )
            is True
        )
        assert (
            same_engine(stored, context_for(ep.QWEN_CUSTOM, language="zh", voice_id="Vivian"))
            is False
        )
        assert same_engine(stored, context_for(ep.QWEN_BASE, language="zh", clone_id="c1")) is False

        assert same_engine(None, context_for(ep.VIENEU)) is True
        assert (
            same_engine(None, context_for(ep.QWEN_CUSTOM, language="zh", voice_id="Vivian"))
            is False
        )

        stored = context_for(ep.VIENEU, language="vi", voice_id="Adam")
        changed = context_for(ep.VIENEU, voice_id="Hà Vy")
        assert same_engine(stored, changed) is True  # same engine, new request
        assert context_matches(stored, changed) is False  # not a cache hit


class TestVariantProvenance:
    """Task 2.2 (qwen_gguf_engine_20260923): model-format identity in the context.

    A variant is part of WHAT produced the audio — Q8_0 and Q4_K_M are
    different artifacts and can never share a cache entry or a render slot —
    while VieNeu keeps its pre-variant payload byte-for-byte.
    """

    def test_variant_identity_isolation(self) -> None:
        context = context_for(
            ep.QWEN_BASE,
            language="zh",
            clone_id="c1",
            variant=qv.variant_for(ep.QWEN_BASE, model_format="gguf", quantization="Q4_K_M"),
        )
        assert context.model_format == "gguf"
        assert context.engine == "qwentts_cpp"
        assert context.quantization == "Q4_K_M"

        q8 = context_for(
            ep.QWEN_BASE,
            language="zh",
            clone_id="c1",
            variant=qv.variant_for(ep.QWEN_BASE, model_format="gguf", quantization="Q8_0"),
        )
        q4 = context_for(
            ep.QWEN_BASE,
            language="zh",
            clone_id="c1",
            variant=qv.variant_for(ep.QWEN_BASE, model_format="gguf", quantization="Q4_K_M"),
        )
        official = context_for(ep.QWEN_BASE, language="zh", clone_id="c1")
        assert q8.fingerprint() != q4.fingerprint()
        assert not context_matches(q8, q4)
        assert not context_matches(q8, official)
        assert not context_matches(official, q8)

        base_q8 = qv.variant_for(ep.QWEN_BASE, model_format="gguf", quantization="Q8_0")
        base_q4 = qv.variant_for(ep.QWEN_BASE, model_format="gguf", quantization="Q4_K_M")
        q8 = context_for(ep.QWEN_BASE, language="zh", clone_id="c1", variant=base_q8)
        q4 = context_for(ep.QWEN_BASE, language="zh", clone_id="c1", variant=base_q4)
        official = context_for(ep.QWEN_BASE, language="zh", clone_id="c1")
        assert not same_engine(q8, q4)
        assert not same_engine(q8, official)
        assert not same_engine(official, q8)
        assert same_engine(
            q8, context_for(ep.QWEN_BASE, language="zh", clone_id="c2", variant=base_q8)
        )
        # VieNeu is unchanged: one engine, one variant.
        assert same_engine(context_for(ep.VIENEU), context_for(ep.VIENEU, voice_id="Adam"))

    def test_variant_payload_versioning(self) -> None:
        # VieNeu and unstamped-official contexts keep the v1 payload shape so
        # every fingerprint written before variants existed still verifies.
        for context in (
            context_for(ep.VIENEU),
            context_for(ep.QWEN_BASE, language="zh", clone_id="c1"),
        ):
            payload = context.fingerprint_payload()
            assert "contextVersion" not in payload
            assert "modelFormat" not in payload

        context = context_for(
            ep.QWEN_CUSTOM,
            language="zh",
            voice_id="Vivian",
            variant=qv.variant_for(ep.QWEN_CUSTOM, model_format="gguf", quantization="Q4_K_M"),
            resolved_device="cpu",
            runtime_identity="qwentts.cpp@0cbde9b+ggml@0af0d7d",
            model_identity="sha256:model",
            tokenizer_identity="sha256:tok",
        )
        payload = json.loads(json.dumps(context.fingerprint_payload()))
        assert payload["contextVersion"] == 2
        assert payload["modelFormat"] == "gguf"
        assert payload["engine"] == "qwentts_cpp"
        assert payload["quantization"] == "Q4_K_M"
        assert payload["resolvedDevice"] == "cpu"
        assert payload["runtimeIdentity"] == "qwentts.cpp@0cbde9b+ggml@0af0d7d"
        assert context_from_payload(payload) == context

        # Payloads written before variants existed have no version — every
        # Qwen render that exists was produced by the official host.
        legacy = {
            "profile": ep.QWEN_CUSTOM,
            "modelRevision": "qwen_custom_0_6b@deadbeef",
            "language": "zh",
            "voiceId": "Vivian",
            "cloneId": "",
            "generation": {"temperature": None, "speed": None, "silenceP": None},
        }
        context = context_from_payload(legacy)
        assert context is not None
        assert context.model_format == "official"
        assert context.engine == "pytorch"
        # ...and it matches a fresh official request exactly (it IS one).
        fresh = SynthesisContext(
            profile=ep.QWEN_CUSTOM,
            model_revision="qwen_custom_0_6b@deadbeef",
            language="zh",
            voice_id="Vivian",
        )
        assert context == fresh
        assert context_matches(context, fresh)

    def test_forged_payloads_decode_official(self) -> None:
        payload = context_for(
            ep.QWEN_BASE,
            language="zh",
            clone_id="c1",
            variant=qv.variant_for(ep.QWEN_BASE, model_format="gguf"),
        ).fingerprint_payload()
        for version in (99, "two", -1):
            assert context_from_payload({**payload, "contextVersion": version}) is None
        # A dropped decode can never fabricate a GGUF identity: neither a
        # cache hit nor an engine match against a real GGUF request.
        gguf = context_for(
            ep.QWEN_BASE,
            language="zh",
            clone_id="c1",
            variant=qv.variant_for(ep.QWEN_BASE, model_format="gguf"),
        )
        assert not context_matches(None, gguf)
        assert not same_engine(None, gguf)

        # Hand-edited variant keys WITHOUT the version marker decode as the
        # legacy schema: the keys are ignored, never a GGUF identity.
        forged = context_for(ep.QWEN_BASE, language="zh", clone_id="c1").fingerprint_payload()
        forged["modelFormat"] = "gguf"
        forged["quantization"] = "Q4_K_M"
        context = context_from_payload(forged)
        assert context is not None
        assert context.model_format == "official"

        variant = qv.variant_for(ep.QWEN_BASE, model_format="gguf")
        unstamped = context_for(ep.QWEN_BASE, language="zh", clone_id="c1", variant=variant)
        stamped = context_for(
            ep.QWEN_BASE,
            language="zh",
            clone_id="c1",
            variant=variant,
            runtime_identity="pack-abc",
            model_identity="sha256:m",
            tokenizer_identity="sha256:t",
            resolved_device="cpu",
        )
        assert not context_matches(unstamped, stamped)
        assert not context_matches(stamped, unstamped)

    def test_variant_field_validation(self) -> None:
        # Variant fields are Qwen-only.
        with pytest.raises(ValueError):
            SynthesisContext(profile=ep.VIENEU, model_revision="v", model_format="official")
        with pytest.raises(ValueError):
            SynthesisContext(profile=ep.VIENEU, model_revision="v", resolved_device="cpu")
        # An engine that contradicts the format is a contradiction, not a choice.
        with pytest.raises(ValueError):
            SynthesisContext(
                profile=ep.QWEN_BASE,
                model_revision="x",
                language="zh",
                clone_id="c1",
                model_format="gguf",
                engine="pytorch",
            )
        # An unknown quantization or a device outside the variant vocabulary.
        with pytest.raises(ValueError):
            context_for(
                ep.QWEN_BASE,
                language="zh",
                clone_id="c1",
                variant=qv.variant_for(ep.QWEN_BASE, model_format="gguf"),
                resolved_device="mps",  # native host speaks Metal, not mps
            )

    def test_auto_device_is_a_selection_not_a_resolution(self) -> None:
        context = context_for(
            ep.QWEN_BASE,
            language="zh",
            clone_id="c1",
            variant=qv.variant_for(ep.QWEN_BASE, model_format="gguf"),
            resolved_device="auto",
        )
        assert context.resolved_device == ""  # auto resolves later, at the host
