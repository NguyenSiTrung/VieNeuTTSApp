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
