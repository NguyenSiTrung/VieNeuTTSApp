"""Detector: §6.1 hardware→engine matrix, §6.2 workload heuristic, user override."""

import builtins

import pytest

from vienetts_app.core.detector import (
    CudaDriverProbe,
    HardwareInfo,
    TorchProbe,
    Workload,
    detect_hardware,
    probe_cuda_driver,
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


def test_default_detection_does_not_import_torch(monkeypatch: pytest.MonkeyPatch) -> None:
    attempted: list[str] = []
    real_import = builtins.__import__

    def tracking_import(name: str, *args, **kwargs):
        if name == "torch":
            attempted.append(name)
            raise AssertionError("hardware detection must not import torch")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", tracking_import)

    detect_hardware(system="linux", machine="x86_64", nvidia_smi=False)

    assert attempted == []


def test_metadata_probe_never_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    from vienetts_app.core import detector

    def corrupt_metadata(_name: str) -> str:
        raise ValueError

    monkeypatch.setattr(detector.metadata, "version", corrupt_metadata)

    assert detector.probe_torch() == TorchProbe(installed=False)


def test_cuda_driver_probe_reports_compatible_nvidia_without_importing_torch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vienetts_app.core import detector

    attempted: list[str] = []
    real_import = builtins.__import__

    def tracking_import(name: str, *args, **kwargs):
        if name == "torch":
            attempted.append(name)
            raise AssertionError("CUDA driver probing must not import torch")
        return real_import(name, *args, **kwargs)

    class CompletedProcess:
        returncode = 0
        stdout = "NVIDIA-SMI 570.00    Driver Version: 570.00    CUDA Version: 12.8"

    monkeypatch.setattr(builtins, "__import__", tracking_import)
    monkeypatch.setattr(detector.subprocess, "run", lambda *_args, **_kwargs: CompletedProcess())

    probe = probe_cuda_driver()

    assert probe == CudaDriverProbe(available=True, cuda_version="12.8")
    assert probe.usable is True
    assert attempted == []
