"""§9 data models: construction, defaults, and input validation."""

import pytest

from vienetts_app.core.models import EngineInfo, Settings, TTSProgress, TTSRequest, VoiceOp


class TestEngineInfo:
    def test_engine_info_contract(self) -> None:
        for backend in ("cuda", "onnx ", "", "Torch", None):
            with pytest.raises(ValueError, match="backend"):
                EngineInfo(
                    backend=backend, device="cpu", precision="int8", cuda_version=None, note="n"
                )  # type: ignore[arg-type]
        for device in ("gpu", "cpux", ""):
            with pytest.raises(ValueError, match="device"):
                EngineInfo(
                    backend="onnx", device=device, precision="int8", cuda_version=None, note="n"
                )  # type: ignore[arg-type]
        for precision in ("int4", "fp16", ""):
            with pytest.raises(ValueError, match="precision"):
                EngineInfo(
                    backend="onnx", device="cpu", precision=precision, cuda_version=None, note="n"
                )  # type: ignore[arg-type]


class TestSettings:
    def test_settings_contract(self) -> None:
        assert Settings(temperature=0.05).temperature == pytest.approx(0.05)
        assert Settings(temperature=2.0).temperature == pytest.approx(2.0)
        assert Settings(speed=0.5).speed == pytest.approx(0.5)
        assert Settings(speed=2.0).speed == pytest.approx(2.0)
        assert Settings(silence_p=0.0).silence_p == pytest.approx(0.0)
        assert Settings(silence_p=2.0).silence_p == pytest.approx(2.0)
        assert (
            Settings(model_repo="pnnbao-ump/VieNeu-TTS-v3-Turbo").model_repo
            == "pnnbao-ump/VieNeu-TTS-v3-Turbo"
        )
        for backend in ("cuda", "", "AUTO"):
            with pytest.raises(ValueError, match="backend"):
                Settings(backend=backend)
        for precision in ("int4", "FP32", ""):
            with pytest.raises(ValueError, match="precision"):
                Settings(precision=precision)
        for export_format in ("ogg", "WAV", "", "mp4"):
            with pytest.raises(ValueError, match="export_format"):
                Settings(export_format=export_format)
        for theme in ("darkly", "System", ""):
            with pytest.raises(ValueError, match="theme"):
                Settings(theme=theme)
        for temperature in (-0.1, 0.0, 2.5, 99.0):
            with pytest.raises(ValueError, match="temperature"):
                Settings(temperature=temperature)
        for bad in (0.49, 2.01, -1.0, 99.0):
            with pytest.raises(ValueError, match="speed"):
                Settings(speed=bad)
        for bad_type in ("1.0", None, True, False):
            with pytest.raises(ValueError, match="speed"):
                Settings(speed=bad_type)  # type: ignore[arg-type]
        for bad in (-0.01, 2.01, -1.0, 10.0):
            with pytest.raises(ValueError, match="silence_p"):
                Settings(silence_p=bad)
        for bad_type in ("0.15", None, True, False):
            with pytest.raises(ValueError, match="silence_p"):
                Settings(silence_p=bad_type)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="voice"):
            Settings(default_voice="  ")
        for bad in ("no-slash", "a/b/c", "owner/", "/repo", "a b/c", "  ", "a\nb"):
            with pytest.raises(ValueError, match="model_repo"):
                Settings(model_repo=bad)
        with pytest.raises(TypeError, match="model_repo"):
            Settings(model_repo=5)  # type: ignore[arg-type]

    def test_engine_profile_fields(self) -> None:
        from vienetts_app.core.engine_profiles import list_profiles

        for profile in list_profiles():
            assert Settings(engine_profile=profile).engine_profile == profile
        for device in ("auto", "cpu", "cuda", "mps"):
            assert Settings(qwen_device=device).qwen_device == device
        # backend/precision stay scoped to VieNeu: a Qwen profile does not
        # invalidate them and they keep working when the profile switches back.
        qwen = Settings(engine_profile="qwen_custom_0_6b", backend="onnx", precision="fp32")
        assert qwen.backend == "onnx" and qwen.precision == "fp32"

        for bad in ("qwen_customvoice", "qwen1_7b", "VIENEU", ""):
            with pytest.raises(ValueError, match="engine_profile"):
                Settings(engine_profile=bad)
        for bad in ("tpu", "CUDA", "gpu", ""):
            with pytest.raises(ValueError, match="qwen_device"):
                Settings(qwen_device=bad)


class TestTTSRequest:
    def test_request_construction(self) -> None:
        TTSRequest(text="Hello", mode="stream", temperature=0.8, job_id="job-123")

        for text in ("", "   ", "\n\t"):
            with pytest.raises(ValueError, match="text"):
                TTSRequest(text=text)
        for mode in ("play", "", "INFER"):
            with pytest.raises(ValueError, match="mode"):
                TTSRequest(text="hi", mode=mode)
        with pytest.raises(TypeError):
            TTSRequest(text="hi", ref_audio=123)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="job_id"):
            TTSRequest(text="hi", job_id=" ")
        with pytest.raises(TypeError, match="job_id"):
            TTSRequest(text="hi", job_id=123)  # type: ignore[arg-type]
        for temperature in (-0.1, 0.0, 2.5, 99.0, "0.4", [0.4], True):
            with pytest.raises(ValueError, match="temperature"):
                TTSRequest(text="hi", temperature=temperature)  # type: ignore[arg-type]

    def test_request_context_rules(self) -> None:
        from vienetts_app.core import engine_profiles as ep
        from vienetts_app.core.synthesis_context import context_for

        vie = context_for(ep.VIENEU, language="vi", voice_id="Adam")
        assert TTSRequest(text="Xin chào", voice="Adam", context=vie).context is vie

        qwen = context_for(ep.QWEN_CUSTOM, language="zh", voice_id="Vivian")
        assert TTSRequest(text="你好", voice="Vivian", context=qwen).context is qwen

        from vienetts_app.core import engine_profiles as ep
        from vienetts_app.core.synthesis_context import context_for

        qwen = context_for(ep.QWEN_CUSTOM, language="zh", voice_id="Vivian")
        with pytest.raises(ValueError, match="voice"):
            TTSRequest(text="你好", voice="Ryan", context=qwen)
        with pytest.raises(ValueError, match="ref_audio"):
            TTSRequest(text="你好", voice="Vivian", ref_audio="/tmp/ref.wav", context=qwen)

        clone = context_for(ep.QWEN_BASE, language="zh", clone_id="clone-1")
        with pytest.raises(ValueError, match="clone"):
            TTSRequest(text="你好", voice="Vivian", context=clone)

        with pytest.raises(TypeError, match="context"):
            TTSRequest(text="hi", context={"profile": "vieneu"})  # type: ignore[arg-type]


class TestVoiceOp:
    """Voice management jobs (FR-3.4): add/remove/denoise through the worker queue."""

    def test_voice_op_contract(self) -> None:
        VoiceOp(op="add", name="V", clip_path="/r.wav", denoise=False)
        for name in (None, "", "   ", 123):
            with pytest.raises((ValueError, TypeError)):
                VoiceOp(op="add", name=name, clip_path="/r.wav")  # type: ignore[arg-type]
        for clip_path in (None, "", "  "):
            with pytest.raises(ValueError, match="clip_path"):
                VoiceOp(op="add", name="V", clip_path=clip_path)  # type: ignore[arg-type]
        for name in (None, "", " \t "):
            with pytest.raises(ValueError, match="name"):
                VoiceOp(op="remove", name=name)  # type: ignore[arg-type]
        for clip_path in (None, "", " "):
            with pytest.raises(ValueError, match="clip_path"):
                VoiceOp(op="denoise", clip_path=clip_path)  # type: ignore[arg-type]
        for op in ("play", "", "ADD", None, 1):
            with pytest.raises((ValueError, TypeError)):
                VoiceOp(op=op, name="V", clip_path="/r.wav")  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="denoise"):
            VoiceOp(op="add", name="V", clip_path="/r.wav", denoise="yes")  # type: ignore[arg-type]

        op = VoiceOp(
            op="add",
            name="V",
            clip_path="/r.wav",
            profile="qwen_base_0_6b",
            transcript="Xin chào.",
            consent=True,
        )

        assert (op.profile, op.transcript, op.consent) == ("qwen_base_0_6b", "Xin chào.", True)

        for profile in ("qwen_omni", "", "VIENEU"):
            with pytest.raises(ValueError, match="profile"):
                VoiceOp(op="add", name="V", clip_path="/r.wav", profile=profile)  # type: ignore[arg-type]

        with pytest.raises(ValueError, match="apply to op 'add' only"):
            VoiceOp(op="remove", name="V", transcript="Xin chào.")
        with pytest.raises(ValueError, match="apply to op 'add' only"):
            VoiceOp(op="denoise", clip_path="/r.wav", consent=True)

        with pytest.raises(TypeError, match="transcript"):
            VoiceOp(op="add", name="V", clip_path="/r.wav", transcript=1)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="consent"):
            VoiceOp(op="add", name="V", clip_path="/r.wav", consent="yes")  # type: ignore[arg-type]


class TestTTSProgress:
    def test_progress_contract(self) -> None:
        TTSProgress(done=1, total=4, stage="synthesizing")

        for stage in ("loading", "", "Init"):
            with pytest.raises(ValueError, match="stage"):
                TTSProgress(done=0, total=1, stage=stage)

        with pytest.raises(ValueError, match="done"):
            TTSProgress(done=-1, total=1, stage="init")
        with pytest.raises(ValueError, match="total"):
            TTSProgress(done=0, total=-1, stage="init")
        with pytest.raises(ValueError, match="total"):
            TTSProgress(done=2, total=1, stage="exporting")


class TestModelCacheEnabled:
    def test_model_cache_enabled(self) -> None:
        for bad in (None, 1, 0, "true", "false", [], {}):
            with pytest.raises(ValueError, match="model_cache_enabled"):
                Settings(model_cache_enabled=bad)  # type: ignore[arg-type]
