"""Detector: §6.1 hardware→engine matrix, §6.2 workload heuristic, user override."""

from vienetts_app.core.detector import (
    HardwareInfo,
    TorchProbe,
    Workload,
    detect_hardware,
    resolve_engine,
)
from vienetts_app.core.models import Settings

PROBE_NVIDIA_TORCH = TorchProbe(installed=True, cuda_available=True, cuda_version="12.8")


def nvidia(version: str) -> HardwareInfo:
    return detect_hardware(
        TorchProbe(installed=True, cuda_available=True, cuda_version=version),
        system="linux",
        machine="x86_64",
        nvidia_smi=False,
    )


class TestDetectionMatrix:
    def test_nvidia_cuda_12_8_torch_present_torch_fp32(self) -> None:
        info = nvidia("12.8")
        assert info.kind == "nvidia"
        eng = resolve_engine(info, Settings(), Workload(char_count=5000))
        assert (eng.backend, eng.device, eng.precision) == ("torch", "cuda", "fp32")
        assert eng.cuda_version == "12.8"

    def test_nvidia_cuda_13_newer_than_required(self) -> None:
        eng = resolve_engine(nvidia("13.0"), Settings(), Workload(char_count=5000))
        assert eng.backend == "torch"

    def test_nvidia_cuda_too_old_falls_back_onnx_int8(self) -> None:
        eng = resolve_engine(nvidia("12.6"), Settings(), Workload(char_count=5000))
        assert (eng.backend, eng.device, eng.precision) == ("onnx", "cpu", "int8")
        assert "12.6" in eng.note

    def test_nvidia_without_torch_falls_back_onnx_with_notice(self) -> None:
        info = detect_hardware(
            TorchProbe(installed=False), system="linux", machine="x86_64", nvidia_smi=True
        )
        assert info.kind == "nvidia"
        eng = resolve_engine(info, Settings(), Workload(char_count=5000))
        assert (eng.backend, eng.device) == ("onnx", "cpu")
        assert "torch" in eng.note.lower()

    def test_apple_silicon_uses_onnx_int8(self) -> None:
        info = detect_hardware(
            TorchProbe(installed=False), system="darwin", machine="arm64", nvidia_smi=False
        )
        assert info.kind == "apple_silicon"
        eng = resolve_engine(info, Settings(), Workload(char_count=5000))
        assert (eng.backend, eng.device, eng.precision) == ("onnx", "cpu", "int8")
        assert "cpu" in eng.note.lower()

    def test_apple_intel_uses_onnx_int8(self) -> None:
        info = detect_hardware(
            TorchProbe(installed=False), system="darwin", machine="x86_64", nvidia_smi=False
        )
        assert info.kind == "apple_intel"
        eng = resolve_engine(info, Settings(), Workload(char_count=5000))
        assert (eng.backend, eng.device, eng.precision) == ("onnx", "cpu", "int8")
        # The frozen macOS download is arm64-only — the note must say so
        # instead of implying a matching artifact exists.
        assert "Apple Silicon only" in eng.note

    def test_no_gpu_uses_onnx_int8(self) -> None:
        info = detect_hardware(
            TorchProbe(installed=False), system="linux", machine="x86_64", nvidia_smi=False
        )
        assert info.kind == "none"
        eng = resolve_engine(info, Settings(), Workload(char_count=5000))
        assert (eng.backend, eng.device, eng.precision) == ("onnx", "cpu", "int8")

    def test_torch_installed_but_no_cuda_is_no_gpu(self) -> None:
        info = detect_hardware(
            TorchProbe(installed=True, cuda_available=False),
            system="linux",
            machine="x86_64",
            nvidia_smi=False,
        )
        assert info.kind == "none"


class TestWorkloadHeuristic:
    NVID = nvidia("12.8")
    CPU = detect_hardware(
        TorchProbe(installed=False), system="darwin", machine="arm64", nvidia_smi=False
    )

    def test_streaming_always_onnx_even_on_nvidia(self) -> None:
        eng = resolve_engine(self.NVID, Settings(), Workload(streaming=True, char_count=99999))
        assert eng.backend == "onnx"

    def test_short_interactive_text_prefers_onnx_on_nvidia(self) -> None:
        eng = resolve_engine(self.NVID, Settings(), Workload(char_count=200))
        assert eng.backend == "onnx"

    def test_short_text_boundary_is_max_chars(self) -> None:
        eng = resolve_engine(self.NVID, Settings(), Workload(char_count=256))
        assert eng.backend == "onnx"
        eng_long = resolve_engine(self.NVID, Settings(), Workload(char_count=257))
        assert eng_long.backend == "torch"

    def test_long_text_uses_torch_when_available(self) -> None:
        eng = resolve_engine(self.NVID, Settings(), Workload(char_count=5000))
        assert eng.backend == "torch"

    def test_batch_uses_torch_when_available(self) -> None:
        eng = resolve_engine(self.NVID, Settings(), Workload(batch=True, char_count=100))
        assert eng.backend == "torch"

    def test_long_text_on_cpu_only_stays_onnx(self) -> None:
        eng = resolve_engine(self.CPU, Settings(), Workload(char_count=50000))
        assert (eng.backend, eng.device) == ("onnx", "cpu")


class TestUserOverride:
    NVID = nvidia("12.8")

    def test_override_onnx_forces_onnx_on_nvidia_long(self) -> None:
        eng = resolve_engine(self.NVID, Settings(backend="onnx"), Workload(char_count=5000))
        assert eng.backend == "onnx"

    def test_override_precision_respected(self) -> None:
        eng = resolve_engine(self.CPU_INFO(), Settings(precision="fp32"), Workload())
        assert eng.precision == "fp32"

    def test_override_torch_without_cuda_falls_back_with_notice(self) -> None:
        cpu = detect_hardware(
            TorchProbe(installed=False), system="darwin", machine="arm64", nvidia_smi=False
        )
        eng = resolve_engine(cpu, Settings(backend="torch"), Workload())
        assert eng.backend == "onnx"
        assert "torch" in eng.note.lower()

    def test_override_torch_on_nvidia_wins(self) -> None:
        eng = resolve_engine(self.NVID, Settings(backend="torch"), Workload(char_count=50))
        assert eng.backend == "torch"

    @staticmethod
    def CPU_INFO() -> HardwareInfo:
        return detect_hardware(
            TorchProbe(installed=False), system="darwin", machine="arm64", nvidia_smi=False
        )


class TestDetectedDisplayInfo:
    def test_display_info_without_workload_on_nvidia(self) -> None:
        from vienetts_app.core.detector import detected_engine_info

        info = detected_engine_info(nvidia("12.8"))
        assert info.backend == "torch"
        assert info.cuda_version == "12.8"

    def test_display_info_on_mac(self) -> None:
        from vienetts_app.core.detector import detected_engine_info

        info = detected_engine_info(
            detect_hardware(
                TorchProbe(installed=False), system="darwin", machine="arm64", nvidia_smi=False
            )
        )
        assert (info.backend, info.device, info.precision) == ("onnx", "cpu", "int8")


def test_cuda_version_parsing_tolerates_none() -> None:
    info = detect_hardware(
        TorchProbe(installed=True, cuda_available=True, cuda_version=None),
        system="linux",
        machine="x86_64",
        nvidia_smi=False,
    )
    # Unparseable CUDA version → cannot confirm >= 12.8 → stay on CPU.
    eng = resolve_engine(info, Settings(), Workload(char_count=5000))
    assert eng.backend == "onnx"
