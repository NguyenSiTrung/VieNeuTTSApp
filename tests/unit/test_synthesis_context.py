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
    def test_defaults_are_engine_defaults(self) -> None:
        generation = GenerationSettings()
        assert generation.temperature is None
        assert generation.speed is None
        assert generation.silence_p is None

    def test_bounds_match_the_settings_contract(self) -> None:
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

    def test_is_frozen(self) -> None:
        with pytest.raises(dataclasses.FrozenInstanceError):
            GenerationSettings().speed = 1.5  # type: ignore[misc]


class TestConstruction:
    def test_vieneu_context_needs_no_language_or_voice(self) -> None:
        context = SynthesisContext(profile=ep.VIENEU, model_revision="vieneu-official:abc+def")
        assert context.language == ""
        assert context.voice_id == ""
        assert context.clone_id == ""
        assert context.generation == GenerationSettings()

    def test_qwen_customvoice_context_carries_language_and_speaker(self) -> None:
        context = SynthesisContext(
            profile=ep.QWEN_CUSTOM,
            model_revision="qwen_custom_0_6b@deadbeef",
            language="zh",
            voice_id="Vivian",
        )
        assert context.language == "zh"
        assert context.voice_id == "Vivian"

    def test_qwen_base_context_carries_a_clone(self) -> None:
        context = SynthesisContext(
            profile=ep.QWEN_BASE,
            model_revision="qwen_base_0_6b@deadbeef",
            language="ko",
            clone_id="c0ffee1234",
        )
        assert context.clone_id == "c0ffee1234"

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

    def test_model_revision_is_required(self) -> None:
        with pytest.raises(ValueError, match="model_revision"):
            SynthesisContext(profile=ep.VIENEU, model_revision="   ")

    def test_is_frozen(self) -> None:
        context = SynthesisContext(profile=ep.VIENEU, model_revision="v")
        with pytest.raises(dataclasses.FrozenInstanceError):
            context.language = "en"  # type: ignore[misc]


class TestFingerprint:
    def test_payload_is_a_deterministic_plain_mapping(self) -> None:
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

    def test_equal_contexts_share_a_fingerprint(self) -> None:
        first = context_for(ep.VIENEU, language="vi", voice_id="Adam")
        second = context_for(ep.VIENEU, language="vi", voice_id="Adam")
        assert first.fingerprint() == second.fingerprint()
        assert len(first.fingerprint()) == 64
        assert first.fingerprint() == first.fingerprint()

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

    def test_vieneu_model_repo_changes_the_fingerprint(self) -> None:
        official = context_for(ep.VIENEU, language="vi", voice_id="Adam")
        custom = context_for(ep.VIENEU, language="vi", voice_id="Adam", model_repo="owner/custom")
        assert official.fingerprint() != custom.fingerprint()


class TestContextFor:
    def test_builds_the_pinned_qwen_revision_from_capabilities(self) -> None:
        context = context_for(ep.QWEN_BASE, language="ja", clone_id="clone-1")
        assert context.model_revision == ep.model_tag(ep.QWEN_BASE)
        assert context.profile == ep.QWEN_BASE

    def test_builds_the_vieneu_official_tag(self) -> None:
        assert context_for(ep.VIENEU).model_revision == ep.model_tag(ep.VIENEU)

    def test_rejects_an_unknown_profile(self) -> None:
        with pytest.raises(ep.EngineProfileError):
            context_for("qwen1_7b")  # type: ignore[arg-type]


class TestPayloadRoundTrip:
    """Task 5.3: persisted provenance is the payload, read back fail-soft."""

    def test_a_context_round_trips_through_its_payload(self) -> None:
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

    def test_a_non_mapping_generation_block_reads_as_engine_defaults(self) -> None:
        context = context_from_payload(
            {"profile": ep.VIENEU, "modelRevision": "v", "generation": "junk"}
        )
        assert context == SynthesisContext(profile=ep.VIENEU, model_revision="v")

    def test_a_partial_generation_block_keeps_what_it_can(self) -> None:
        context = context_from_payload(
            {"profile": ep.VIENEU, "modelRevision": "v", "generation": {"speed": 1.5}}
        )
        assert context is not None
        assert context.generation == GenerationSettings(speed=1.5)


class TestRenderCompatibility:
    """Task 5.3: which stored render identity may serve which request."""

    def test_a_render_with_no_identity_is_reusable_by_vieneu_only(self) -> None:
        assert legacy_render_compatible(context_for(ep.VIENEU)) is True
        assert (
            legacy_render_compatible(context_for(ep.QWEN_CUSTOM, language="zh", voice_id="Vivian"))
            is False
        )
        assert (
            legacy_render_compatible(context_for(ep.QWEN_BASE, language="zh", clone_id="c1"))
            is False
        )

    def test_identical_identities_match(self) -> None:
        first = context_for(ep.QWEN_CUSTOM, language="zh", voice_id="Vivian")
        second = context_for(ep.QWEN_CUSTOM, language="zh", voice_id="Vivian")
        assert context_matches(first, second) is True

    def test_any_different_identity_is_a_mismatch(self) -> None:
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

    def test_an_unknown_request_matches_only_an_unknown_render(self) -> None:
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

    def test_a_known_render_needs_its_own_engine(self) -> None:
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

    def test_a_render_with_no_identity_needs_vieneu(self) -> None:
        assert same_engine(None, context_for(ep.VIENEU)) is True
        assert (
            same_engine(None, context_for(ep.QWEN_CUSTOM, language="zh", voice_id="Vivian"))
            is False
        )

    def test_it_is_looser_than_context_matches(self) -> None:
        stored = context_for(ep.VIENEU, language="vi", voice_id="Adam")
        changed = context_for(ep.VIENEU, voice_id="Hà Vy")
        assert same_engine(stored, changed) is True  # same engine, new request
        assert context_matches(stored, changed) is False  # not a cache hit
