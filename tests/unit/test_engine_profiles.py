"""Engine profile capabilities: identity, languages, voices, validation.

The capability model is the single source of truth for what each engine can do.
It is pure data (no Qt, no torch, no disk) so controllers, jobs and tests can
reason about engine compatibility before anything is loaded.
"""

from __future__ import annotations

import pytest

from vienetts_app.core import engine_profiles as ep


class TestCapabilityCatalog:
    def test_profile_capability_invariants(self) -> None:
        # The Qwen language list is the auto-detect sentinel plus named model
        # languages (never Vietnamese, which only VieNeu serves).
        caps = ep.get_capabilities(ep.QWEN_CUSTOM)
        codes = [lang.code for lang in caps.languages]
        assert codes[0] == "auto"
        assert caps.languages[0].is_auto is True
        assert all(lang.model_name for lang in caps.languages[1:])
        assert ep.language_model_name(caps, "zh") == "Chinese"
        assert "vi" not in codes
        assert caps.supports_cloning is False

        base = ep.get_capabilities(ep.QWEN_BASE)
        assert base.supports_cloning is True
        assert base.clone_requirements == ("reference_clip", "transcript", "consent")
        assert base.voices == ()


class TestUnknownProfile:
    def test_unknown_profile_names_the_choices(self) -> None:
        with pytest.raises(ep.EngineProfileError) as excinfo:
            ep.get_capabilities("qwen1_7b")  # type: ignore[arg-type]
        message = str(excinfo.value)
        assert "qwen1_7b" in message
        assert "vieneu" in message and "qwen_custom_0_6b" in message


class TestValidateSelection:
    def test_language_validation(self) -> None:
        for language in ("vi", "en"):
            caps = ep.validate_selection(ep.VIENEU, language=language, voice_id="Adam")
            assert caps.profile == ep.VIENEU

        with pytest.raises(ep.EngineProfileError) as excinfo:
            ep.validate_selection(ep.VIENEU, language="zh", voice_id="Adam")
        assert "zh" in str(excinfo.value)
        assert "vi" in str(excinfo.value)

        with pytest.raises(ep.EngineProfileError) as excinfo:
            ep.validate_selection(ep.QWEN_CUSTOM, language="vi", voice_id="Ryan")
        assert "VieNeu" in str(excinfo.value)

        assert ep.validate_selection(ep.QWEN_CUSTOM, language="auto", voice_id="Ryan")
        with pytest.raises(ep.EngineProfileError):
            ep.validate_selection(ep.VIENEU, language="auto", voice_id="Adam")

    def test_profile_voice_rules(self) -> None:
        with pytest.raises(ep.EngineProfileError, match="speaker"):
            ep.validate_selection(ep.QWEN_CUSTOM, language="en")
        with pytest.raises(ep.EngineProfileError, match="Ono_Anna"):
            ep.validate_selection(ep.QWEN_CUSTOM, language="en", voice_id="Nobody")
        assert ep.validate_selection(ep.QWEN_CUSTOM, language="en", voice_id="Ryan")

        with pytest.raises(ep.EngineProfileError, match="clone"):
            ep.validate_selection(ep.QWEN_CUSTOM, language="en", voice_id="Ryan", clone_id="abc123")

        with pytest.raises(ep.EngineProfileError, match="clone"):
            ep.validate_selection(ep.QWEN_BASE, language="en")
        with pytest.raises(ep.EngineProfileError, match="Ryan"):
            ep.validate_selection(ep.QWEN_BASE, language="en", voice_id="Ryan")
        caps = ep.validate_selection(ep.QWEN_BASE, language="en", clone_id="c0ffee1234")
        assert caps.profile == ep.QWEN_BASE


class TestRuntimeKey:
    """The profile→runtime-key map the controller uses to name an install."""

    def test_runtime_key_contract(self) -> None:
        from vienetts_app.core.qwen_engine import ENGINE_PROFILE_KEYS
        from vienetts_app.workers.qwen_host import PROFILE_ENGINES

        # One mapping, three readers: the model host's flag, the engine's
        # profile table and the controller's install lookup must never drift.
        assert {profile: key for profile, key in ep.QWEN_RUNTIME_KEYS.items()} == {
            engine_id: key for key, engine_id in ENGINE_PROFILE_KEYS.items()
        }
        assert dict(PROFILE_ENGINES) == dict(ENGINE_PROFILE_KEYS)
        assert ep.runtime_key(ep.VIENEU) == ""

        with pytest.raises(ep.EngineProfileError):
            ep.runtime_key("nope")  # type: ignore[arg-type]


class TestModelTag:
    def test_model_tag_contract(self) -> None:
        from vienetts_app.core.official_model_manifest import OFFICIAL_MODEL_MANIFEST as manifest

        tag = ep.model_tag(ep.VIENEU)
        assert tag == (
            f"vieneu-official:{manifest.backbone_revision[:12]}+{manifest.codec_revision[:12]}"
        )

        assert ep.model_tag(ep.VIENEU, model_repo="owner/custom") == "vieneu-custom:owner/custom"

        assert ep.model_tag(ep.QWEN_CUSTOM) == ep.get_capabilities(ep.QWEN_CUSTOM).model_revision
        assert ep.model_tag(ep.QWEN_BASE) == ep.get_capabilities(ep.QWEN_BASE).model_revision
        assert ep.model_tag(ep.QWEN_CUSTOM) != ep.model_tag(ep.QWEN_BASE)

        with pytest.raises(ep.EngineProfileError):
            ep.model_tag("nope")  # type: ignore[arg-type]


class TestHostPrecision:
    """The locked per-device precision matrix (qwen-runtime-compatibility.md §1)."""

    def test_host_precision_contract(self) -> None:
        assert ep.host_precision("cuda") == ("bfloat16", "sdpa")
        assert ep.host_precision("cpu") == ("float32", "sdpa")
        assert ep.host_precision("mps") == ("float32", "sdpa")

        for profile in (ep.QWEN_CUSTOM, ep.QWEN_BASE):
            for device in ep.get_capabilities(profile).devices:
                dtype, attention = ep.host_precision(device)
                assert dtype in ("float32", "float16", "bfloat16")
                assert attention in ("eager", "sdpa", "flash_attention_2")

        with pytest.raises(ep.EngineProfileError, match="cuda"):
            ep.host_precision("tpu")
