"""Engine/language selection, recommendation, and validation (Phase 4 Task 4)."""

import pytest

from vienetts_app.core.backends import BackendCapabilityError
from vienetts_app.core.engine_selection import recommend_engine, validate_selection


class TestRecommendEngine:
    def test_vietnamese_recommends_vieneu(self) -> None:
        assert recommend_engine("vi") == "vieneu"

    def test_empty_language_recommends_vieneu(self) -> None:
        assert recommend_engine("") == "vieneu"
        assert recommend_engine(None) == "vieneu"

    @pytest.mark.parametrize("code", ["en", "zh", "ko", "ja", "fr"])
    def test_qwen_supported_recommends_customvoice(self, code: str) -> None:
        assert recommend_engine(code) == "qwen_customvoice"

    def test_unsupported_language_falls_back_to_vieneu(self) -> None:
        assert recommend_engine("xx") == "vieneu"


class TestValidateSelection:
    def test_vieneu_accepts_empty_language_and_voice(self) -> None:
        validate_selection("vieneu", "", "Adam")
        validate_selection("vieneu", "vi", None)

    def test_unknown_engine_rejected_with_choices(self) -> None:
        with pytest.raises(BackendCapabilityError, match="vieneu"):
            validate_selection("nope", "en", None)

    def test_language_mismatch_rejected(self) -> None:
        # VieNeu covers vi + en; Chinese is genuinely unsupported there.
        with pytest.raises(BackendCapabilityError, match="[Ss]upport"):
            validate_selection("vieneu", "zh", "Adam")

    def test_customvoice_requires_known_speaker(self) -> None:
        validate_selection("qwen_customvoice", "en", "Ryan")
        with pytest.raises(BackendCapabilityError, match="Ryan"):
            validate_selection("qwen_customvoice", "en", None)
        with pytest.raises(BackendCapabilityError, match="Vivian"):
            validate_selection("qwen_customvoice", "en", "Nobody")

    def test_customvoice_rejects_unsupported_language(self) -> None:
        with pytest.raises(BackendCapabilityError, match="[Ss]upport"):
            validate_selection("qwen_customvoice", "vi", "Ryan")

    def test_base_requires_reference_name(self) -> None:
        validate_selection("qwen_base", "en", "MyClone")
        with pytest.raises(BackendCapabilityError, match="[Rr]eference"):
            validate_selection("qwen_base", "en", None)

    def test_base_rejects_fixed_speaker_as_reference(self) -> None:
        # Fixed speakers belong to CustomVoice; a Base voice must be an enrolled reference.
        with pytest.raises(BackendCapabilityError, match="[Rr]eference|[Ee]nroll"):
            validate_selection("qwen_base", "en", "Ryan", is_reference_enrolled=lambda name: False)

    def test_base_accepts_enrolled_reference(self) -> None:
        validate_selection("qwen_base", "en", "MyClone", is_reference_enrolled=lambda name: True)
