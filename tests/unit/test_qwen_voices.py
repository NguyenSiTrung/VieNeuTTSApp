"""Qwen Base reference-voice cloning adapter (Phase 4 Task 3)."""

import numpy as np
import pytest

from vienetts_app.core.qwen_voices import (
    QwenVoiceError,
    list_reference_voices,
    load_reference_voice,
    remove_reference_voice,
    save_reference_voice,
    validate_reference_clip,
)


def write_clip(path, seconds: float = 4.0, rate: int = 44100) -> None:
    import soundfile as sf

    t = np.arange(int(seconds * rate), dtype=np.float64) / rate
    sf.write(str(path), (0.4 * np.sin(2.0 * np.pi * 220.0 * t)).astype(np.float32), rate)


class TestValidateClip:
    def test_valid_clip_returns_audio(self, tmp_path) -> None:
        clip = tmp_path / "ref.wav"
        write_clip(clip)
        audio, rate = validate_reference_clip(clip)
        assert rate == 44100
        assert audio.dtype == np.float32 and audio.ndim == 1
        assert 3.0 <= len(audio) / rate <= 8.0

    def test_missing_file_rejected(self, tmp_path) -> None:
        with pytest.raises(QwenVoiceError, match="[Ee]xists|not found|missing"):
            validate_reference_clip(tmp_path / "nope.wav")

    def test_too_short_rejected(self, tmp_path) -> None:
        clip = tmp_path / "short.wav"
        write_clip(clip, seconds=1.0)
        with pytest.raises(QwenVoiceError, match="3"):
            validate_reference_clip(clip)

    def test_too_long_rejected(self, tmp_path) -> None:
        clip = tmp_path / "long.wav"
        write_clip(clip, seconds=10.0)
        with pytest.raises(QwenVoiceError, match="8"):
            validate_reference_clip(clip)

    def test_undecodable_rejected(self, tmp_path) -> None:
        clip = tmp_path / "junk.wav"
        clip.write_bytes(b"not audio at all")
        with pytest.raises(QwenVoiceError, match="[Dd]ecode|read|audio"):
            validate_reference_clip(clip)


class TestSaveLoad:
    def test_round_trip(self, tmp_path) -> None:
        clip = tmp_path / "ref.wav"
        write_clip(clip)
        store = tmp_path / "voices"
        saved = save_reference_voice(store, "Mai", clip, "xin chào các bạn", consent=True)
        assert saved.is_file()
        audio_path, ref_text = load_reference_voice(store, "Mai")
        assert audio_path.is_file()
        assert ref_text == "xin chào các bạn"
        assert list_reference_voices(store) == ["Mai"]

    def test_consent_required(self, tmp_path) -> None:
        clip = tmp_path / "ref.wav"
        write_clip(clip)
        with pytest.raises(QwenVoiceError, match="[Cc]onsent"):
            save_reference_voice(tmp_path / "voices", "Mai", clip, "hi", consent=False)

    def test_transcript_required(self, tmp_path) -> None:
        clip = tmp_path / "ref.wav"
        write_clip(clip)
        with pytest.raises(QwenVoiceError, match="[Tt]ranscript|ref_text"):
            save_reference_voice(tmp_path / "voices", "Mai", clip, "  ", consent=True)

    def test_blank_name_rejected(self, tmp_path) -> None:
        clip = tmp_path / "ref.wav"
        write_clip(clip)
        with pytest.raises(QwenVoiceError, match="name"):
            save_reference_voice(tmp_path / "voices", "  ", clip, "hi", consent=True)

    def test_missing_voice_load_rejected(self, tmp_path) -> None:
        with pytest.raises(QwenVoiceError, match="[Ee]nroll|not found"):
            load_reference_voice(tmp_path / "voices", "Ghost")

    def test_engine_isolation(self, tmp_path) -> None:
        clip = tmp_path / "ref.wav"
        write_clip(clip)
        store = tmp_path / "voices"
        store.mkdir(parents=True, exist_ok=True)
        (store / "voices.json").write_text('{"presets": {"Adam": {}}}', encoding="utf-8")
        save_reference_voice(store, "Adam", clip, "same name, other engine", consent=True)
        # VieNeu registry untouched; Qwen store lists only Qwen voices.
        assert "Adam" in (store / "voices.json").read_text(encoding="utf-8")
        assert list_reference_voices(store) == ["Adam"]
        assert list_reference_voices(store, engine="vieneu") == []

    def test_remove(self, tmp_path) -> None:
        clip = tmp_path / "ref.wav"
        write_clip(clip)
        store = tmp_path / "voices"
        save_reference_voice(store, "Temp", clip, "hi there", consent=True)
        remove_reference_voice(store, "Temp")
        assert list_reference_voices(store) == []
        with pytest.raises(QwenVoiceError, match="[Nn]o .*voice|not found"):
            remove_reference_voice(store, "Temp")

    def test_corrupt_store_load_rejected(self, tmp_path) -> None:
        clip = tmp_path / "ref.wav"
        write_clip(clip)
        store = tmp_path / "voices"
        save_reference_voice(store, "Half", clip, "hi there", consent=True)
        ref_dir = store / "qwen_base" / "Half"
        (ref_dir / "reference.wav").unlink()
        with pytest.raises(QwenVoiceError, match="[Cc]orrupt|missing|incomplete"):
            load_reference_voice(store, "Half")
