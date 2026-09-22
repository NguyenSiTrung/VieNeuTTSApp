"""Engine profile capabilities: identity, languages, voices, validation.

The capability model is the single source of truth for what each engine can do.
It is pure data (no Qt, no torch, no disk) so controllers, jobs and tests can
reason about engine compatibility before anything is loaded.
"""

from __future__ import annotations

import dataclasses

import pytest

from vienetts_app.core import engine_profiles as ep


class TestCapabilityCatalog:
    def test_canonical_profile_ids_and_default(self) -> None:
        assert ep.VIENEU == "vieneu"
        assert ep.QWEN_CUSTOM == "qwen_custom_0_6b"
        assert ep.QWEN_BASE == "qwen_base_0_6b"
        assert ep.list_profiles() == (ep.VIENEU, ep.QWEN_CUSTOM, ep.QWEN_BASE)
        assert ep.default_profile() == ep.VIENEU
        assert ep.get_capabilities(ep.VIENEU).is_default is True

    def test_vieneu_stays_vietnamese_first_on_the_existing_runtime(self) -> None:
        caps = ep.get_capabilities(ep.VIENEU)
        assert [lang.code for lang in caps.languages] == ["vi", "en"]
        assert caps.supports_cloning is True
        assert caps.supports_preset_voices is True
        assert caps.supports_instruction is False
        assert caps.runtime == "vieneu_worker"
        assert caps.source_sample_rate == 48_000
        assert caps.output_sample_rate == 48_000
        assert set(caps.devices) == {"cpu", "cuda"}
        assert caps.voices_source == "vieneu_catalog"
        assert caps.voices == ()
        assert caps.model_repo == ""

    def test_qwen_customvoice_exposes_the_model_reported_language_list(self) -> None:
        caps = ep.get_capabilities(ep.QWEN_CUSTOM)
        codes = [lang.code for lang in caps.languages]
        assert codes[0] == "auto"
        assert codes[1:] == ["zh", "en", "ja", "ko", "de", "fr", "ru", "pt", "es", "it"]
        assert caps.languages[0].is_auto is True
        assert all(lang.model_name for lang in caps.languages[1:])
        assert ep.language_model_name(caps, "zh") == "Chinese"
        assert ep.language_model_name(caps, "pt") == "Portuguese"
        assert "vi" not in codes

    def test_qwen_customvoice_speakers_are_pinned_and_carry_native_languages(self) -> None:
        caps = ep.get_capabilities(ep.QWEN_CUSTOM)
        names = [voice.voice_id for voice in caps.voices]
        assert names == [
            "Vivian",
            "Serena",
            "Uncle_Fu",
            "Dylan",
            "Eric",
            "Ryan",
            "Aiden",
            "Ono_Anna",
            "Sohee",
        ]
        assert caps.voices_source == "pinned"
        assert caps.supports_cloning is False
        assert caps.supports_instruction is False
        assert caps.clone_requirements == ()
        assert caps.source_sample_rate == 24_000
        assert caps.output_sample_rate == 48_000
        assert caps.runtime == "qwen_host"
        assert set(caps.devices) == {"cpu", "cuda", "mps"}
        assert caps.model_revision == "85e237c12c027371202489a0ec509ded67b5e4b5"

    def test_qwen_base_clones_from_clip_transcript_and_consent(self) -> None:
        caps = ep.get_capabilities(ep.QWEN_BASE)
        assert caps.supports_cloning is True
        assert caps.clone_requirements == ("reference_clip", "transcript", "consent")
        assert caps.supports_preset_voices is False
        assert caps.voices == ()
        assert caps.voices_source == "enrollment_only"
        assert caps.model_revision == "5d83992436eae1d760afd27aff78a71d676296fc"

    def test_capabilities_are_immutable_and_tuple_backed(self) -> None:
        caps = ep.get_capabilities(ep.QWEN_CUSTOM)
        with pytest.raises(dataclasses.FrozenInstanceError):
            caps.supports_cloning = True  # type: ignore[misc]
        assert isinstance(caps.languages, tuple)
        assert isinstance(caps.voices, tuple)
        assert isinstance(caps.generation_controls, tuple)

    def test_vieneu_generation_controls_are_the_existing_ones(self) -> None:
        caps = ep.get_capabilities(ep.VIENEU)
        assert caps.generation_controls == ("temperature", "speed", "silence_p")
        assert ep.get_capabilities(ep.QWEN_CUSTOM).generation_controls == ("speed", "silence_p")
        assert ep.get_capabilities(ep.QWEN_BASE).generation_controls == ("speed", "silence_p")

    def test_streaming_granularity_is_segment_bounded_for_qwen(self) -> None:
        assert ep.get_capabilities(ep.VIENEU).streaming_granularity == "sdk_chunk"
        assert ep.get_capabilities(ep.QWEN_CUSTOM).streaming_granularity == "segment"


class TestUnknownProfile:
    def test_unknown_profile_names_the_choices(self) -> None:
        with pytest.raises(ep.EngineProfileError) as excinfo:
            ep.get_capabilities("qwen1_7b")  # type: ignore[arg-type]
        message = str(excinfo.value)
        assert "qwen1_7b" in message
        assert "vieneu" in message and "qwen_custom_0_6b" in message


class TestValidateSelection:
    def test_vieneu_accepts_vietnamese_and_english(self) -> None:
        for language in ("vi", "en"):
            caps = ep.validate_selection(ep.VIENEU, language=language, voice_id="Adam")
            assert caps.profile == ep.VIENEU

    def test_vieneu_rejects_qwen_only_languages_without_substituting_an_engine(self) -> None:
        with pytest.raises(ep.EngineProfileError) as excinfo:
            ep.validate_selection(ep.VIENEU, language="zh", voice_id="Adam")
        assert "zh" in str(excinfo.value)
        assert "vi" in str(excinfo.value)

    def test_qwen_rejects_vietnamese_and_points_at_vieneu(self) -> None:
        with pytest.raises(ep.EngineProfileError) as excinfo:
            ep.validate_selection(ep.QWEN_CUSTOM, language="vi", voice_id="Ryan")
        assert "VieNeu" in str(excinfo.value)

    def test_auto_language_is_qwen_only(self) -> None:
        assert ep.validate_selection(ep.QWEN_CUSTOM, language="auto", voice_id="Ryan")
        with pytest.raises(ep.EngineProfileError):
            ep.validate_selection(ep.VIENEU, language="auto", voice_id="Adam")

    def test_customvoice_requires_a_known_fixed_speaker(self) -> None:
        with pytest.raises(ep.EngineProfileError, match="speaker"):
            ep.validate_selection(ep.QWEN_CUSTOM, language="en")
        with pytest.raises(ep.EngineProfileError, match="Ono_Anna"):
            ep.validate_selection(ep.QWEN_CUSTOM, language="en", voice_id="Nobody")
        assert ep.validate_selection(ep.QWEN_CUSTOM, language="en", voice_id="Ryan")

    def test_customvoice_refuses_cloning(self) -> None:
        with pytest.raises(ep.EngineProfileError, match="clone"):
            ep.validate_selection(ep.QWEN_CUSTOM, language="en", voice_id="Ryan", clone_id="abc123")

    def test_base_requires_an_enrolled_clone_and_rejects_fixed_speakers(self) -> None:
        with pytest.raises(ep.EngineProfileError, match="clone"):
            ep.validate_selection(ep.QWEN_BASE, language="en")
        with pytest.raises(ep.EngineProfileError, match="Ryan"):
            ep.validate_selection(ep.QWEN_BASE, language="en", voice_id="Ryan")
        caps = ep.validate_selection(ep.QWEN_BASE, language="en", clone_id="c0ffee1234")
        assert caps.profile == ep.QWEN_BASE


class TestRuntimeKey:
    """The profile→runtime-key map the controller uses to name an install."""

    def test_qwen_profiles_map_to_the_host_profile_keys(self) -> None:
        from vienetts_app.core.qwen_engine import ENGINE_PROFILE_KEYS
        from vienetts_app.workers.qwen_host import PROFILE_ENGINES

        # One mapping, three readers: the model host's flag, the engine's
        # profile table and the controller's install lookup must never drift.
        assert {profile: key for profile, key in ep.QWEN_RUNTIME_KEYS.items()} == {
            engine_id: key for key, engine_id in ENGINE_PROFILE_KEYS.items()
        }
        assert dict(PROFILE_ENGINES) == dict(ENGINE_PROFILE_KEYS)
        assert ep.runtime_key(ep.QWEN_CUSTOM) == "customvoice"
        assert ep.runtime_key(ep.QWEN_BASE) == "base"

    def test_the_in_process_profile_has_no_runtime_key(self) -> None:
        assert ep.runtime_key(ep.VIENEU) == ""

    def test_unknown_profile_raises(self) -> None:
        with pytest.raises(ep.EngineProfileError):
            ep.runtime_key("nope")  # type: ignore[arg-type]


class TestModelTag:
    def test_vieneu_official_tag_uses_the_pinned_manifest_revisions(self) -> None:
        from vienetts_app.core.official_model_manifest import OFFICIAL_MODEL_MANIFEST as manifest

        tag = ep.model_tag(ep.VIENEU)
        assert tag == (
            f"vieneu-official:{manifest.backbone_revision[:12]}+{manifest.codec_revision[:12]}"
        )

    def test_vieneu_custom_repo_tag_is_explicit(self) -> None:
        assert ep.model_tag(ep.VIENEU, model_repo="owner/custom") == "vieneu-custom:owner/custom"

    def test_qwen_tags_carry_the_pinned_revision(self) -> None:
        assert ep.model_tag(ep.QWEN_CUSTOM) == ep.get_capabilities(ep.QWEN_CUSTOM).model_revision
        assert ep.model_tag(ep.QWEN_BASE) == ep.get_capabilities(ep.QWEN_BASE).model_revision
        assert ep.model_tag(ep.QWEN_CUSTOM) != ep.model_tag(ep.QWEN_BASE)

    def test_unknown_profile_raises(self) -> None:
        with pytest.raises(ep.EngineProfileError):
            ep.model_tag("nope")  # type: ignore[arg-type]


class TestHostPrecision:
    """The locked per-device precision matrix (qwen-runtime-compatibility.md §1)."""

    def test_cuda_runs_bfloat16_while_cpu_and_mps_run_float32(self) -> None:
        assert ep.host_precision("cuda") == ("bfloat16", "sdpa")
        assert ep.host_precision("cpu") == ("float32", "sdpa")
        assert ep.host_precision("mps") == ("float32", "sdpa")

    def test_every_supported_device_has_a_locked_precision(self) -> None:
        for profile in (ep.QWEN_CUSTOM, ep.QWEN_BASE):
            for device in ep.get_capabilities(profile).devices:
                dtype, attention = ep.host_precision(device)
                assert dtype in ("float32", "float16", "bfloat16")
                assert attention in ("eager", "sdpa", "flash_attention_2")

    def test_unknown_device_names_the_supported_ones(self) -> None:
        with pytest.raises(ep.EngineProfileError, match="cuda"):
            ep.host_precision("tpu")
