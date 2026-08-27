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

    @pytest.mark.parametrize("backend", ["cuda", "onnx ", "", "Torch", None])
    def test_invalid_backend_raises(self, backend: object) -> None:
        with pytest.raises(ValueError, match="backend"):
            EngineInfo(backend=backend, device="cpu", precision="int8", cuda_version=None, note="n")

    @pytest.mark.parametrize("device", ["gpu", "cpux", ""])
    def test_invalid_device_raises(self, device: object) -> None:
        with pytest.raises(ValueError, match="device"):
            EngineInfo(backend="onnx", device=device, precision="int8", cuda_version=None, note="n")

    @pytest.mark.parametrize("precision", ["int4", "fp16", ""])
    def test_invalid_precision_raises(self, precision: object) -> None:
        with pytest.raises(ValueError, match="precision"):
            EngineInfo(
                backend="onnx", device="cpu", precision=precision, cuda_version=None, note="n"
            )

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

    def test_all_valid_backends_accepted(self) -> None:
        for backend in ("auto", "onnx", "torch"):
            assert Settings(backend=backend).backend == backend

    @pytest.mark.parametrize("backend", ["cuda", "", "AUTO"])
    def test_invalid_backend_raises(self, backend: str) -> None:
        with pytest.raises(ValueError, match="backend"):
            Settings(backend=backend)

    @pytest.mark.parametrize("precision", ["int4", "FP32", ""])
    def test_invalid_precision_raises(self, precision: str) -> None:
        with pytest.raises(ValueError, match="precision"):
            Settings(precision=precision)

    @pytest.mark.parametrize("theme", ["darkly", "System", ""])
    def test_invalid_theme_raises(self, theme: str) -> None:
        with pytest.raises(ValueError, match="theme"):
            Settings(theme=theme)

    @pytest.mark.parametrize("temperature", [-0.1, 0.0, 2.5, 99.0])
    def test_out_of_range_temperature_raises(self, temperature: float) -> None:
        with pytest.raises(ValueError, match="temperature"):
            Settings(temperature=temperature)

    def test_temperature_bounds_inclusive(self) -> None:
        assert Settings(temperature=0.05).temperature == pytest.approx(0.05)
        assert Settings(temperature=2.0).temperature == pytest.approx(2.0)

    def test_empty_default_voice_raises(self) -> None:
        with pytest.raises(ValueError, match="voice"):
            Settings(default_voice="  ")

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

    def test_full_construction(self) -> None:
        req = TTSRequest(
            text="Hello", voice="Adam", ref_audio="/tmp/ref.wav", denoise=False, mode="stream"
        )
        assert req.voice == "Adam"
        assert req.ref_audio == "/tmp/ref.wav"
        assert req.denoise is False
        assert req.mode == "stream"

    @pytest.mark.parametrize("text", ["", "   ", "\n\t"])
    def test_blank_text_raises(self, text: str) -> None:
        with pytest.raises(ValueError, match="text"):
            TTSRequest(text=text)

    @pytest.mark.parametrize("mode", ["play", "", "INFER"])
    def test_invalid_mode_raises(self, mode: str) -> None:
        with pytest.raises(ValueError, match="mode"):
            TTSRequest(text="hi", mode=mode)

    def test_ref_audio_must_be_str_or_none(self) -> None:
        with pytest.raises(TypeError):
            TTSRequest(text="hi", ref_audio=123)  # type: ignore[arg-type]

    def test_is_frozen(self) -> None:
        req = TTSRequest(text="hi")
        with pytest.raises(dataclasses.FrozenInstanceError):
            req.text = "other"  # type: ignore[misc]


class TestTTSRequestTemperature:
    """FR-3.x sampling control: temperature rides on the request (None=SDK default)."""

    def test_default_is_none(self) -> None:
        assert TTSRequest(text="hi").temperature is None

    @pytest.mark.parametrize("temperature", [0.05, 0.4, 1.0, 2.0])
    def test_in_range_accepted(self, temperature: float) -> None:
        req = TTSRequest(text="hi", temperature=temperature)
        assert req.temperature == pytest.approx(temperature)

    @pytest.mark.parametrize("temperature", [-0.1, 0.0, 2.5, 99.0, "0.4", [0.4]])
    def test_out_of_range_or_non_number_rejected(self, temperature: object) -> None:
        with pytest.raises(ValueError, match="temperature"):
            TTSRequest(text="hi", temperature=temperature)  # type: ignore[arg-type]

    def test_bool_rejected_even_though_numeric(self) -> None:
        # bool is an int subclass and True == 1.0 sits inside the range; it is
        # still a type error in spirit (QML checkbox noise), so reject it.
        with pytest.raises(ValueError, match="temperature"):
            TTSRequest(text="hi", temperature=True)  # type: ignore[arg-type]


class TestVoiceOp:
    """Voice management jobs (FR-3.4): add/remove/denoise through the worker queue."""

    def test_add_valid(self) -> None:
        op = VoiceOp(op="add", name="MyVoice", clip_path="/tmp/ref.wav")
        assert op.op == "add"
        assert op.name == "MyVoice"
        assert op.clip_path == "/tmp/ref.wav"
        assert op.denoise is True  # default

    def test_add_explicit_denoise_false(self) -> None:
        op = VoiceOp(op="add", name="V", clip_path="/r.wav", denoise=False)
        assert op.denoise is False

    @pytest.mark.parametrize("name", [None, "", "   "])
    def test_add_missing_or_blank_name_raises(self, name: str | None) -> None:
        with pytest.raises(ValueError, match="name"):
            VoiceOp(op="add", name=name, clip_path="/r.wav")  # type: ignore[arg-type]

    @pytest.mark.parametrize("clip_path", [None, "", "  "])
    def test_add_missing_or_blank_clip_raises(self, clip_path: str | None) -> None:
        with pytest.raises(ValueError, match="clip_path"):
            VoiceOp(op="add", name="V", clip_path=clip_path)  # type: ignore[arg-type]

    def test_remove_valid(self) -> None:
        op = VoiceOp(op="remove", name="MyVoice")
        assert (op.op, op.name, op.clip_path) == ("remove", "MyVoice", None)

    @pytest.mark.parametrize("name", [None, "", " \t "])
    def test_remove_requires_name(self, name: str | None) -> None:
        with pytest.raises(ValueError, match="name"):
            VoiceOp(op="remove", name=name)  # type: ignore[arg-type]

    def test_denoise_valid(self) -> None:
        op = VoiceOp(op="denoise", clip_path="/tmp/clip.wav")
        assert (op.op, op.name, op.clip_path) == ("denoise", None, "/tmp/clip.wav")

    @pytest.mark.parametrize("clip_path", [None, "", " "])
    def test_denoise_requires_clip_path(self, clip_path: str | None) -> None:
        with pytest.raises(ValueError, match="clip_path"):
            VoiceOp(op="denoise", clip_path=clip_path)  # type: ignore[arg-type]

    @pytest.mark.parametrize("op", ["play", "", "ADD", None, 1])
    def test_invalid_op_raises(self, op: object) -> None:
        with pytest.raises((ValueError, TypeError)):
            VoiceOp(op=op, name="V", clip_path="/r.wav")  # type: ignore[arg-type]

    def test_denoise_flag_must_be_bool(self) -> None:
        with pytest.raises(ValueError, match="denoise"):
            VoiceOp(op="add", name="V", clip_path="/r.wav", denoise="yes")  # type: ignore[arg-type]

    def test_name_must_be_str_or_none(self) -> None:
        with pytest.raises(TypeError):
            VoiceOp(op="remove", name=123)  # type: ignore[arg-type]

    def test_is_frozen(self) -> None:
        op = VoiceOp(op="remove", name="V")
        with pytest.raises(dataclasses.FrozenInstanceError):
            op.name = "other"  # type: ignore[misc]


class TestTTSProgress:
    def test_valid_construction(self) -> None:
        p = TTSProgress(done=1, total=4, stage="synthesizing")
        assert (p.done, p.total, p.stage) == (1, 4, "synthesizing")

    @pytest.mark.parametrize("stage", ["loading", "", "Init"])
    def test_invalid_stage_raises(self, stage: str) -> None:
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
