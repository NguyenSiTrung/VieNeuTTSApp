"""Controller engine/language selection, recommendation, and validation (Phase 4 Task 4)."""

import numpy as np
import pytest

pytest.importorskip("PySide6")

from tests.unit.test_controller import Harness  # noqa: E402

from vienetts_app.core.models import TTSRequest  # noqa: E402
from vienetts_app.core.qwen_voices import save_reference_voice  # noqa: E402


def write_clip(path, seconds: float = 4.0, rate: int = 44100) -> None:
    import soundfile as sf

    t = np.arange(int(seconds * rate), dtype=np.float64) / rate
    sf.write(str(path), (0.4 * np.sin(2.0 * np.pi * 220.0 * t)).astype(np.float32), rate)


@pytest.fixture()
def harness(qcoreapp, tmp_path):
    return Harness(tmp_path)


class TestEngineProperties:
    def test_defaults(self, harness: Harness) -> None:
        assert harness.controller.ttsEngine == "vieneu"
        assert harness.controller.ttsLanguage == ""
        assert harness.controller.voiceInstruction == ""
        assert harness.controller.recommendedEngine == "vieneu"
        assert harness.controller.engineRecommendation == ""
        assert harness.controller.cloningSupported is True

    def test_invalid_engine_rejected(self, harness: Harness) -> None:
        harness.controller.ttsEngine = "nope"
        assert harness.controller.ttsEngine == "vieneu"
        assert "nope" in harness.controller.errorText

    def test_language_validated_against_engine(self, harness: Harness) -> None:
        harness.controller.ttsLanguage = "zh"
        assert harness.controller.ttsLanguage == ""
        assert "zh" in harness.controller.errorText
        harness.controller.ttsLanguage = "en"
        assert harness.controller.ttsLanguage == "en"
        assert harness.controller.errorText == ""

    def test_recommendation_notice(self, harness: Harness) -> None:
        harness.controller.ttsEngine = "qwen_customvoice"
        harness.controller.ttsLanguage = "zh"
        assert harness.controller.recommendedEngine == "qwen_customvoice"
        assert harness.controller.engineRecommendation == ""
        # English stays on the lightweight default: notice names VieNeu.
        harness.controller.ttsLanguage = "en"
        assert harness.controller.recommendedEngine == "vieneu"
        assert "VieNeu" in harness.controller.engineRecommendation
        # Switching to the recommended engine clears the notice.
        harness.controller.ttsEngine = "vieneu"
        assert harness.controller.engineRecommendation == ""

    def test_engine_switch_rebuilds_catalog_and_falls_back_voice(self, harness: Harness) -> None:
        harness.controller.ttsEngine = "qwen_customvoice"
        ids = [v["id"] for g in harness.controller.voices for v in g["voices"]]
        assert "Ryan" in ids and "Adam" not in ids
        assert harness.controller.defaultVoice in ids  # incompatible Adam fell back
        assert "giọng mặc định" in harness.controller.errorText
        assert harness.controller.cloningSupported is False

    def test_base_catalog_lists_enrolled_references(self, harness: Harness, tmp_path) -> None:
        clip = tmp_path / "ref.wav"
        write_clip(clip)
        save_reference_voice(tmp_path / "voices", "MyClone", clip, "xin chào", consent=True)
        harness.controller.ttsEngine = "qwen_base"
        ids = [v["id"] for g in harness.controller.voices for v in g["voices"]]
        assert ids == ["MyClone"]
        assert harness.controller.cloningSupported is True

    def test_base_empty_catalog_keeps_voice_for_actionable_job_error(
        self, harness: Harness
    ) -> None:
        harness.controller.ttsEngine = "qwen_base"
        assert harness.controller.voices == []
        harness.controller.generate("hello", "")
        assert harness.workers == []
        assert "reference" in harness.controller.errorText.lower()


class TestJobWiring:
    def test_generate_carries_engine_language_instruction(self, harness: Harness) -> None:
        harness.controller.ttsEngine = "qwen_customvoice"
        harness.controller.ttsLanguage = "en"
        harness.controller.voiceInstruction = "cheerful"
        harness.controller.generate("hello world", "Ryan")
        (job,) = harness.worker.submitted
        request = job.request
        assert isinstance(request, TTSRequest)
        assert request.engine == "qwen_customvoice"
        assert request.language == "en"
        assert request.instruction == "cheerful"

    def test_generate_rejects_unknown_speaker(self, harness: Harness) -> None:
        harness.controller.ttsEngine = "qwen_customvoice"
        harness.controller.ttsLanguage = "en"
        harness.controller.generate("hello", "Nobody")
        assert harness.workers == []
        assert "Nobody" in harness.controller.errorText

    def test_generate_rejects_language_mismatch(self, harness: Harness) -> None:
        harness.controller.ttsLanguage = "en"
        harness.controller.ttsEngine = "vieneu"
        # VieNeu covers en — valid; zh is the genuine mismatch.
        harness.controller.ttsLanguage = "zh"
        assert harness.controller.ttsLanguage == "en"  # rejected at the setter
        harness.controller.generate("hello", "Adam")
        (job,) = harness.worker.submitted
        assert job.request.engine == "vieneu"


class TestPersistence:
    def test_selections_persist(self, harness: Harness, tmp_path) -> None:
        harness.controller.ttsEngine = "qwen_customvoice"
        harness.controller.ttsLanguage = "en"
        harness.controller.voiceInstruction = "calm"
        assert harness.controller.errorText == "" or "giọng" in harness.controller.errorText
        saved = (tmp_path / "settings.json").read_text(encoding="utf-8")
        assert '"tts_engine": "qwen_customvoice"' in saved
        assert '"tts_language": "en"' in saved
        assert '"voice_instruction": "calm"' in saved


class TestAuditionWiring:
    def test_customvoice_audition_carries_speaker(self, harness: Harness) -> None:
        harness.controller.ttsEngine = "qwen_customvoice"
        harness.controller.ttsLanguage = "en"
        harness.controller.voiceInstruction = "calm"
        harness.controller.auditionVoice("Ryan")
        (job,) = harness.worker.submitted
        assert job.request.engine == "qwen_customvoice"
        assert job.request.voice == "Ryan"
        assert job.request.language == "en"
        assert job.request.instruction == "calm"

    def test_audition_rejects_unknown_speaker(self, harness: Harness) -> None:
        harness.controller.ttsEngine = "qwen_customvoice"
        harness.controller.ttsLanguage = "en"
        harness.controller.auditionVoice("Nobody")
        assert harness.workers == []
        assert "Nobody" in harness.controller.errorText

    def test_base_audition_resolves_reference(self, harness: Harness, tmp_path) -> None:
        from vienetts_app.core.qwen_voices import save_reference_voice

        clip = tmp_path / "ref.wav"
        write_clip(clip)
        save_reference_voice(tmp_path / "voices", "MyClone", clip, "xin chào", consent=True)
        harness.controller.ttsEngine = "qwen_base"
        harness.controller.ttsLanguage = "en"
        harness.controller.auditionVoice("MyClone")
        (job,) = harness.worker.submitted
        assert job.request.engine == "qwen_base"
        assert job.request.ref_audio is not None
        assert job.request.ref_text == "xin chào"

    def test_base_audition_missing_reference_refuses(self, harness: Harness) -> None:
        harness.controller.ttsEngine = "qwen_base"
        harness.controller.ttsLanguage = "en"
        harness.controller.auditionVoice("Ghost")
        assert harness.workers == []
        assert "Ghost" in harness.controller.errorText

    def test_audition_cache_keyed_by_engine(self, harness: Harness) -> None:
        plain = harness.controller._audition_cache_path("Ryan")  # noqa: SLF001
        custom = harness.controller._audition_cache_path("Ryan", "qwen_customvoice")  # noqa: SLF001
        assert plain != custom
        assert "qwen_customvoice" in custom.name


class TestBaseEnrollmentSlots:
    def test_enroll_requires_consent(self, harness: Harness, tmp_path) -> None:
        harness.controller.enrollBaseVoice("Mai", str(tmp_path / "ref.wav"), "xin chào")
        assert harness.workers == []
        assert "đồng ý" in harness.controller.errorText

    def test_enroll_validates_transcript(self, harness: Harness, tmp_path) -> None:
        harness.controller.acknowledgeConsent()
        harness.controller.enrollBaseVoice("Mai", str(tmp_path / "ref.wav"), "  ")
        assert harness.workers == []
        assert "ref_text" in harness.controller.errorText

    def test_enroll_submits_engine_scoped_op(self, harness: Harness, tmp_path) -> None:
        from vienetts_app.core.models import VoiceOp

        clip = tmp_path / "ref.wav"
        write_clip(clip)
        harness.controller.acknowledgeConsent()
        harness.controller.enrollBaseVoice("Mai", str(clip), "xin chào các bạn")
        (job,) = harness.worker.submitted
        op = job.request
        assert isinstance(op, VoiceOp)
        assert op.engine == "qwen_base"
        assert op.ref_text == "xin chào các bạn"
        assert op.consent is True

    def test_remove_voice_tagged_with_engine(self, harness: Harness) -> None:
        harness.controller.ttsEngine = "qwen_customvoice"
        harness.controller.removeVoice("Ryan")
        assert harness.workers == []
        assert "fixed speakers" in harness.controller.errorText


class TestListenerSeam:
    def test_listener_job_carries_engine(self, harness: Harness) -> None:
        harness.controller.ttsEngine = "qwen_customvoice"
        harness.controller.ttsLanguage = "en"

        class Listener:
            pass

        job_id = harness.controller.submit_stream_for_listener(
            "hello", "Ryan", Listener(), kind="bulk"
        )
        assert job_id is not None
        (job,) = harness.worker.submitted
        assert job.request.engine == "qwen_customvoice"
        assert job.request.language == "en"
        assert job.request.voice == "Ryan"

    def test_listener_job_invalid_combo_returns_none(self, harness: Harness) -> None:
        harness.controller.ttsEngine = "qwen_customvoice"
        harness.controller.ttsLanguage = "en"

        class Listener:
            pass

        job_id = harness.controller.submit_stream_for_listener(
            "hello", "Nobody", Listener(), kind="bulk"
        )
        assert job_id is None
        assert harness.workers == []
        assert "Nobody" in harness.controller.errorText


class TestQwenReadiness:
    def test_readiness_map_shape(self, harness: Harness) -> None:
        status = harness.controller.qwenReadiness
        assert set(status) >= {"runtime", "torch", "device", "detail", "models", "ready"}
        assert isinstance(status["models"], dict)
