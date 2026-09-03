"""Detector: §6.1 hardware→engine matrix, §6.2 workload heuristic, user override."""

import pytest

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
    @pytest.mark.parametrize(
        ("hardware", "backend", "device", "precision", "cuda_ver", "note_match"),
        [
            (nvidia("12.8"), "torch", "cuda", "fp32", "12.8", None),
            (nvidia("13.0"), "torch", "cuda", "fp32", "13.0", None),
            (nvidia("12.6"), "onnx", "cpu", "int8", None, "12.6"),
            (
                detect_hardware(
                    TorchProbe(installed=False), system="linux", machine="x86_64", nvidia_smi=True
                ),
                "onnx",
                "cpu",
                "int8",
                None,
                "torch",
            ),
            (
                detect_hardware(
                    TorchProbe(installed=False), system="darwin", machine="arm64", nvidia_smi=False
                ),
                "onnx",
                "cpu",
                "int8",
                None,
                "cpu",
            ),
            (
                detect_hardware(
                    TorchProbe(installed=False), system="darwin", machine="x86_64", nvidia_smi=False
                ),
                "onnx",
                "cpu",
                "int8",
                None,
                "Apple Silicon only",
            ),
            (
                detect_hardware(
                    TorchProbe(installed=False), system="linux", machine="x86_64", nvidia_smi=False
                ),
                "onnx",
                "cpu",
                "int8",
                None,
                None,
            ),
            (
                detect_hardware(
                    TorchProbe(installed=True, cuda_available=False),
                    system="linux",
                    machine="x86_64",
                    nvidia_smi=False,
                ),
                "onnx",
                "cpu",
                "int8",
                None,
                None,
            ),
        ],
    )
    def test_detection_matrix(
        self,
        hardware: HardwareInfo,
        backend: str,
        device: str,
        precision: str,
        cuda_ver: str | None,
        note_match: str | None,
    ) -> None:
        eng = resolve_engine(hardware, Settings(), Workload(char_count=5000))
        assert (eng.backend, eng.device, eng.precision) == (backend, device, precision)
        if cuda_ver is not None:
            assert eng.cuda_version == cuda_ver
        if note_match is not None:
            assert note_match.lower() in eng.note.lower()


class TestWorkloadHeuristic:
    NVID = nvidia("12.8")
    CPU = detect_hardware(
        TorchProbe(installed=False), system="darwin", machine="arm64", nvidia_smi=False
    )

    @pytest.mark.parametrize(
        ("workload", "expected_backend"),
        [
            (Workload(streaming=True, char_count=99999), "onnx"),
            (Workload(char_count=200), "onnx"),
            (Workload(char_count=256), "onnx"),
            (Workload(char_count=257), "torch"),
            (Workload(char_count=5000), "torch"),
            (Workload(batch=True, char_count=100), "torch"),
        ],
    )
    def test_workload_heuristic_on_nvidia(self, workload: Workload, expected_backend: str) -> None:
        eng = resolve_engine(self.NVID, Settings(), workload)
        assert eng.backend == expected_backend

    def test_long_text_on_cpu_only_stays_onnx(self) -> None:
        eng = resolve_engine(self.CPU, Settings(), Workload(char_count=50000))
        assert (eng.backend, eng.device) == ("onnx", "cpu")


class TestUserOverride:
    NVID = nvidia("12.8")
    CPU = detect_hardware(
        TorchProbe(installed=False), system="darwin", machine="arm64", nvidia_smi=False
    )

    @pytest.mark.parametrize(
        ("hardware", "settings", "workload", "expected_backend", "expected_precision"),
        [
            (NVID, Settings(backend="onnx"), Workload(char_count=5000), "onnx", "int8"),
            (CPU, Settings(precision="fp32"), Workload(), "onnx", "fp32"),
            (CPU, Settings(backend="torch"), Workload(), "onnx", "int8"),
            (NVID, Settings(backend="torch"), Workload(char_count=50), "torch", "int8"),
        ],
    )
    def test_user_override(
        self,
        hardware: HardwareInfo,
        settings: Settings,
        workload: Workload,
        expected_backend: str,
        expected_precision: str,
    ) -> None:
        eng = resolve_engine(hardware, settings, workload)
        assert eng.backend == expected_backend
        assert eng.precision == expected_precision


class TestDetectedDisplayInfo:
    def test_display_info(self) -> None:
        from vienetts_app.core.detector import detected_engine_info

        nvidia_info = detected_engine_info(nvidia("12.8"))
        assert nvidia_info.backend == "torch"
        assert nvidia_info.cuda_version == "12.8"

        mac_info = detected_engine_info(
            detect_hardware(
                TorchProbe(installed=False), system="darwin", machine="arm64", nvidia_smi=False
            )
        )
        assert (mac_info.backend, mac_info.device, mac_info.precision) == ("onnx", "cpu", "int8")

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
