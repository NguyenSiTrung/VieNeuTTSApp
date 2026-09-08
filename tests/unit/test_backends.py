"""Backend capability model: engine IDs, descriptors, validation (Phase 1 Task 1)."""

import pytest

from vienetts_app.core.backends import (
    QWEN_BASE,
    QWEN_CUSTOMVOICE,
    VIENEU,
    BackendCapabilityError,
    get_capabilities,
    list_engines,
    recommend_engine,
    validate_selection,
)


class TestEngineIds:
    def test_stable_ids(self) -> None:
        assert VIENEU == "vieneu"
        assert QWEN_CUSTOMVOICE == "qwen_customvoice"
        assert QWEN_BASE == "qwen_base"

    def test_list_engines_covers_all(self) -> None:
        assert set(list_engines()) == {"vieneu", "qwen_customvoice", "qwen_base"}

    def test_unknown_engine_raises(self) -> None:
        with pytest.raises(BackendCapabilityError, match="unknown engine"):
            get_capabilities("bogus")  # type: ignore[arg-type]


class TestCapabilities:
    def test_vieneu_supports_vi_and_cloning(self) -> None:
        caps = get_capabilities("vieneu")
        assert "vi" in caps.languages
        assert "en" in caps.languages
        assert caps.supports_cloning is True
        assert caps.supports_preset_voices is True
        assert caps.supports_instruction is False
        assert caps.native_sample_rate == 48000
        assert caps.requires_torch is False

    def test_customvoice_fixed_speakers_no_cloning(self) -> None:
        caps = get_capabilities("qwen_customvoice")
        assert caps.supports_cloning is False
        assert caps.supports_preset_voices is True
        assert caps.supports_instruction is True
        assert caps.native_sample_rate == 24000
        assert caps.requires_torch is True

    def test_base_clones_but_has_no_presets(self) -> None:
        caps = get_capabilities("qwen_base")
        assert caps.supports_cloning is True
        assert caps.supports_preset_voices is False
        assert caps.supports_instruction is False

    def test_qwen_covers_en_zh_ko(self) -> None:
        for engine in ("qwen_customvoice", "qwen_base"):
            caps = get_capabilities(engine)  # type: ignore[arg-type]
            assert {"en", "zh", "ko"} <= set(caps.languages)

    def test_qwen_excludes_vietnamese(self) -> None:
        for engine in ("qwen_customvoice", "qwen_base"):
            assert "vi" not in get_capabilities(engine).languages  # type: ignore[arg-type]

    def test_descriptors_are_frozen(self) -> None:
        caps = get_capabilities("vieneu")
        with pytest.raises(AttributeError):
            caps.supports_cloning = False  # type: ignore[misc]


class TestRecommendEngine:
    def test_vietnamese_recommends_vieneu(self) -> None:
        assert recommend_engine("vi") == "vieneu"

    def test_qwen_only_language_recommends_customvoice(self) -> None:
        assert recommend_engine("zh") == "qwen_customvoice"
        assert recommend_engine("ko") == "qwen_customvoice"

    def test_unknown_language_has_no_recommendation(self) -> None:
        assert recommend_engine("xx") is None


class TestValidateSelection:
    def test_valid_vieneu_vi_passes(self) -> None:
        validate_selection(engine="vieneu", language="vi")

    def test_valid_customvoice_en_passes(self) -> None:
        validate_selection(engine="qwen_customvoice", language="en", voice="Ryan")

    def test_qwen_vietnamese_directs_to_vieneu(self) -> None:
        with pytest.raises(BackendCapabilityError, match="VieNeu"):
            validate_selection(engine="qwen_customvoice", language="vi")

    def test_customvoice_rejects_reference_audio(self) -> None:
        with pytest.raises(BackendCapabilityError, match="[Cc]lon"):
            validate_selection(engine="qwen_customvoice", language="en", ref_audio="/tmp/ref.wav")

    def test_vieneu_rejects_instruction(self) -> None:
        with pytest.raises(BackendCapabilityError, match="[Ii]nstruction"):
            validate_selection(engine="vieneu", language="vi", instruction="cheerful")

    def test_base_requires_reference_for_cloning_voice(self) -> None:
        # A named (non-preset) voice on Base without ref audio is actionable.
        with pytest.raises(BackendCapabilityError, match="[Rr]eference"):
            validate_selection(engine="qwen_base", language="en", voice="my-clone")

    def test_error_is_value_error(self) -> None:
        assert issubclass(BackendCapabilityError, ValueError)
