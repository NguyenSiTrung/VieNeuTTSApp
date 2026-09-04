"""§9 data models: construction, defaults, and input validation."""

import dataclasses

import pytest

from vienetts_app.core.models import EngineInfo, Settings, TTSProgress, TTSRequest, VoiceOp


class TestEngineInfo:
    def test_valid_construction(self) -> None:
        info = EngineInfo(
            backend="onnx", device="cpu", precision="int8", cuda_version=None, note="ONNX CPU int8"
        )
        assert info.backend == "onnx"
        assert info.device == "cpu"
        assert info.precision == "int8"
        assert info.cuda_version is None
        assert info.note == "ONNX CPU int8"

    def test_invalid_fields_raise(self) -> None:
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

    def test_is_frozen(self) -> None:
        info = EngineInfo(
            backend="onnx", device="cpu", precision="int8", cuda_version=None, note="n"
        )
        with pytest.raises(dataclasses.FrozenInstanceError):
            info.backend = "torch"  # type: ignore[misc]


class TestSettings:
    def test_defaults_per_section_9(self) -> None:
        s = Settings()
        assert s.backend == "auto"
        assert s.precision == "int8"
        assert s.default_voice == "Adam"
        assert s.output_dir == ""
        assert s.theme == "system"
        assert s.denoise_ref is True
        # SDK exposes temperature (spike §0): default matches SDK infer default.
        assert s.temperature == pytest.approx(0.4)
        assert s.speed == pytest.approx(1.0)
        assert s.silence_p == pytest.approx(0.15)
        assert s.model_repo == ""

    def test_valid_settings_and_bounds(self) -> None:
        for backend in ("auto", "onnx", "torch"):
            assert Settings(backend=backend).backend == backend
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
        assert Settings(model_repo="").model_repo == ""

    def test_invalid_settings_raise(self) -> None:
        for backend in ("cuda", "", "AUTO"):
            with pytest.raises(ValueError, match="backend"):
                Settings(backend=backend)
        for precision in ("int4", "FP32", ""):
            with pytest.raises(ValueError, match="precision"):
                Settings(precision=precision)
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

    def test_is_mutable(self) -> None:
        s = Settings()
        s.theme = "dark"
        assert s.theme == "dark"


class TestTTSRequest:
    def test_valid_construction_and_defaults(self) -> None:
        req = TTSRequest(text="Xin chào")
        assert req.text == "Xin chào"
        assert req.voice is None
        assert req.ref_audio is None
        assert req.denoise is True
        assert req.mode == "infer"
        assert req.speed is None
        assert req.silence_p is None
        assert req.temperature is None
        assert req.job_id is None

    def test_full_construction(self) -> None:
        req = TTSRequest(
            text="Hello",
            voice="Adam",
            ref_audio="/tmp/ref.wav",
            denoise=False,
            mode="stream",
            temperature=0.8,
            job_id="job-123",
        )
        assert req.voice == "Adam"
        assert req.ref_audio == "/tmp/ref.wav"
        assert req.denoise is False
        assert req.mode == "stream"
        assert req.temperature == pytest.approx(0.8)
        assert req.job_id == "job-123"

    def test_invalid_inputs_raise(self) -> None:
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

    def test_is_frozen(self) -> None:
        req = TTSRequest(text="hi")
        with pytest.raises(dataclasses.FrozenInstanceError):
            req.text = "other"  # type: ignore[misc]


class TestVoiceOp:
    """Voice management jobs (FR-3.4): add/remove/denoise through the worker queue."""

    def test_valid_voice_ops(self) -> None:
        op1 = VoiceOp(op="add", name="MyVoice", clip_path="/tmp/ref.wav")
        assert op1.op == "add"
        assert op1.name == "MyVoice"
        assert op1.clip_path == "/tmp/ref.wav"
        assert op1.denoise is True  # default
        op2 = VoiceOp(op="add", name="V", clip_path="/r.wav", denoise=False)
        assert op2.denoise is False

    def test_invalid_voice_ops_raise(self) -> None:
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

    def test_is_frozen(self) -> None:
        op = VoiceOp(op="add", name="V", clip_path="/r.wav")
        with pytest.raises(dataclasses.FrozenInstanceError):
            op.name = "other"  # type: ignore[misc]


class TestTTSProgress:
    def test_valid_construction(self) -> None:
        p = TTSProgress(done=1, total=4, stage="synthesizing")
        assert (p.done, p.total, p.stage) == (1, 4, "synthesizing")

    def test_invalid_stage_raises(self) -> None:
        for stage in ("loading", "", "Init"):
            with pytest.raises(ValueError, match="stage"):
                TTSProgress(done=0, total=1, stage=stage)

    def test_negative_counts_raise(self) -> None:
        with pytest.raises(ValueError, match="done"):
            TTSProgress(done=-1, total=1, stage="init")
        with pytest.raises(ValueError, match="total"):
            TTSProgress(done=0, total=-1, stage="init")

    def test_done_above_total_raises(self) -> None:
        with pytest.raises(ValueError, match="total"):
            TTSProgress(done=2, total=1, stage="exporting")


class TestModelCacheEnabled:
    def test_model_cache_enabled(self) -> None:
        assert Settings().model_cache_enabled is True
        assert Settings(model_cache_enabled=False).model_cache_enabled is False
        for bad in (None, 1, 0, "true", "false", [], {}):
            with pytest.raises(ValueError, match="model_cache_enabled"):
                Settings(model_cache_enabled=bad)  # type: ignore[arg-type]
