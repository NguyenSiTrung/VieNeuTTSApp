"""Engine/language/instruction selection on requests + settings (Phase 1 Task 2)."""

import pytest

from vienetts_app.core.backends import BackendCapabilityError
from vienetts_app.core.models import Settings, TTSRequest


class TestRequestDefaults:
    def test_backward_compatible_defaults(self) -> None:
        req = TTSRequest(text="Xin chào")
        assert req.engine == "vieneu"
        assert req.language is None
        assert req.instruction is None
        assert req.voice_source is None

    def test_voice_source_choices(self) -> None:
        assert TTSRequest(text="hi", voice_source="preset").voice_source == "preset"
        assert TTSRequest(text="hi", voice_source="cloned").voice_source == "cloned"
        with pytest.raises(ValueError, match="voice_source"):
            TTSRequest(text="hi", voice_source="magic")  # type: ignore[arg-type]


class TestRequestValidation:
    def test_unknown_engine_rejected(self) -> None:
        with pytest.raises(BackendCapabilityError, match="unknown engine"):
            TTSRequest(text="hi", engine="bogus")  # type: ignore[arg-type]

    def test_qwen_vietnamese_rejected_with_vieneu_hint(self) -> None:
        with pytest.raises(BackendCapabilityError, match="VieNeu"):
            TTSRequest(text="chào", engine="qwen_customvoice", language="vi")

    def test_customvoice_cloning_rejected(self) -> None:
        with pytest.raises(BackendCapabilityError, match="[Cc]lon"):
            TTSRequest(text="hi", engine="qwen_customvoice", ref_audio="/tmp/r.wav")

    def test_vieneu_instruction_rejected(self) -> None:
        with pytest.raises(BackendCapabilityError, match="[Ii]nstruction"):
            TTSRequest(text="chào", instruction="cheerful")

    def test_customvoice_instruction_accepted(self) -> None:
        req = TTSRequest(
            text="hi", engine="qwen_customvoice", language="en", instruction="cheerful"
        )
        assert req.instruction == "cheerful"
        assert req.language == "en"


class TestRequestCacheIdentity:
    def test_identity_covers_engine_language_instruction_voice(self) -> None:
        base = TTSRequest(text="hi")
        assert (
            base.cache_identity()
            != TTSRequest(text="hi", engine="qwen_customvoice", language="en").cache_identity()
        )
        assert (
            base.cache_identity()
            != TTSRequest(text="hi", voice="Adam", voice_source="preset").cache_identity()
        )
        assert (
            TTSRequest(
                text="hi", engine="qwen_customvoice", language="en", instruction="calm"
            ).cache_identity()
            != TTSRequest(
                text="hi", engine="qwen_customvoice", language="en", instruction="cheerful"
            ).cache_identity()
        )

    def test_identity_stable_and_hashable(self) -> None:
        req = TTSRequest(text="hi")
        assert req.cache_identity() == TTSRequest(text="hi").cache_identity()
        hash(req.cache_identity())


class TestSettingsSelection:
    def test_defaults_preserve_vieneu(self) -> None:
        s = Settings()
        assert s.tts_engine == "vieneu"
        assert s.tts_language == ""
        assert s.voice_instruction == ""

    def test_unknown_engine_rejected(self) -> None:
        with pytest.raises(BackendCapabilityError, match="unknown engine"):
            Settings(tts_engine="bogus")

    def test_unsupported_language_rejected(self) -> None:
        with pytest.raises(BackendCapabilityError, match="language"):
            Settings(tts_engine="vieneu", tts_language="zh")

    def test_revalidate_clamps_stale_combo(self) -> None:
        s = Settings.__new__(Settings)
        object.__setattr__(s, "tts_engine", "qwen_customvoice")
        object.__setattr__(s, "tts_language", "vi")
        s.revalidate_engine_selection()
        assert s.tts_engine == "qwen_customvoice"
        assert s.tts_language == ""

    def test_revalidate_resets_unknown_engine(self) -> None:
        s = Settings.__new__(Settings)
        object.__setattr__(s, "tts_engine", "bogus")
        object.__setattr__(s, "tts_language", "en")
        s.revalidate_engine_selection()
        assert s.tts_engine == "vieneu"


class TestSettingsRestore:
    def test_old_file_without_new_fields_loads(self, tmp_path) -> None:
        from vienetts_app.core.settings import load_settings, save_settings

        old = Settings()
        saved = save_settings(old, data_dir=tmp_path)
        assert saved.is_file()
        loaded = load_settings(data_dir=tmp_path)
        assert loaded.tts_engine == "vieneu"

    def test_stale_combo_on_disk_is_revalidated(self, tmp_path) -> None:
        import json

        from vienetts_app.core.settings import SETTINGS_FILENAME, load_settings

        data = {"tts_engine": "qwen_customvoice", "tts_language": "vi"}
        (tmp_path / SETTINGS_FILENAME).write_text(json.dumps(data), encoding="utf-8")
        loaded = load_settings(data_dir=tmp_path)
        assert loaded.tts_engine == "qwen_customvoice"
        assert loaded.tts_language == ""
