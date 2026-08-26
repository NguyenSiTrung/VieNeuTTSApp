"""§9 data models: construction, defaults, and input validation."""

import dataclasses

import pytest

from vienetts_app.core.models import EngineInfo, Settings, TTSProgress, TTSRequest


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
