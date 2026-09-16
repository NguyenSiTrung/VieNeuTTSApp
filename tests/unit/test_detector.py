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
            # CUDA 12.x minor-version compatibility: any driver reporting
            # 12.0+ (R527+) runs the bundled-cudart cu128 wheels.
            (nvidia("12.6"), "torch", "cuda", "fp32", "12.6", None),
            (nvidia("12.0"), "torch", "cuda", "fp32", "12.0", None),
            (nvidia("11.8"), "onnx", "cpu", "int8", None, "11.8"),
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
            (
                # Unparseable CUDA version → cannot confirm >= 12.8 → stay on CPU.
                detect_hardware(
                    TorchProbe(installed=True, cuda_available=True, cuda_version=None),
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
            (Workload(char_count=256), "onnx"),
            (Workload(char_count=257), "torch"),
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
            raise AssertionError("detection must not import torch")
        return real_import(name, *args, **kwargs)

    class CompletedProcess:
        returncode = 0
        stdout = "NVIDIA-SMI 570.00    Driver Version: 570.00    CUDA Version: 12.8"

    monkeypatch.setattr(builtins, "__import__", tracking_import)
    monkeypatch.setattr(detector.subprocess, "run", lambda *_args, **_kwargs: CompletedProcess())

    # Neither the hardware matrix nor the driver probe may pull in torch.
    # nvml_probe=None-returning pins the subprocess path so the test stays
    # deterministic on hosts that actually have an NVIDIA driver.
    detect_hardware(system="linux", machine="x86_64", nvidia_smi=False)
    probe = probe_cuda_driver(nvml_probe=lambda: None)

    assert probe == CudaDriverProbe(available=True, cuda_version="12.8")
    assert probe.usable is True
    assert attempted == []


def test_cuda_driver_probe_prefers_nvml_over_nvidia_smi(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vienetts_app.core import detector

    def exploding_run(*_args, **_kwargs):
        raise AssertionError("nvidia-smi must not run when NVML answers")

    monkeypatch.setattr(detector.subprocess, "run", exploding_run)

    probe = probe_cuda_driver(nvml_probe=lambda: "12.6")

    assert probe == CudaDriverProbe(available=True, cuda_version="12.6")
    assert probe.usable is True


def test_cuda_driver_probe_falls_back_to_nvidia_smi_when_nvml_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vienetts_app.core import detector

    def broken_nvml() -> str | None:
        raise OSError("nvml.dll is not loadable")

    class CompletedProcess:
        returncode = 0
        stdout = "NVIDIA-SMI 570.00    Driver Version: 570.00    CUDA Version: 12.8"

    monkeypatch.setattr(detector.subprocess, "run", lambda *_args, **_kwargs: CompletedProcess())

    probe = probe_cuda_driver(nvml_probe=broken_nvml)

    assert probe == CudaDriverProbe(available=True, cuda_version="12.8")


def test_cuda_driver_probe_reports_unavailable_when_no_source_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from vienetts_app.core import detector

    def missing_run(*_args, **_kwargs):
        raise FileNotFoundError("nvidia-smi not found")

    monkeypatch.setattr(detector.subprocess, "run", missing_run)

    probe = probe_cuda_driver(nvml_probe=lambda: None)

    assert probe == CudaDriverProbe(available=False)
    assert probe.usable is False


def test_unknown_driver_cuda_version_note_never_prints_none() -> None:
    # Source install with system torch but no managed runtime: the driver
    # exists (nvidia-smi on PATH) yet no version was probed — the note must
    # not render a literal "CUDA None < ...".
    hw = detect_hardware(
        TorchProbe(installed=True),
        system="win32",
        machine="AMD64",
        nvidia_smi=True,
    )

    eng = resolve_engine(hw, Settings(), Workload(char_count=5000))

    assert eng.backend == "onnx"
    assert "None" not in eng.note
    assert "NVIDIA GPU found" in eng.note


def test_managed_cuda_runtime_readiness_controls_detection() -> None:
    hw = detect_hardware(
        TorchProbe(installed=False),
        system="win32",
        machine="AMD64",
        nvidia_smi=False,
        managed_cuda_ready=True,
        managed_cuda_version="12.8",
    )

    assert (hw.kind, hw.torch_installed, hw.cuda_version) == ("nvidia", True, "12.8")
    eng = resolve_engine(hw, Settings(), Workload(char_count=5000))
    assert (eng.backend, eng.device, eng.precision) == ("torch", "cuda", "fp32")

    hw = detect_hardware(
        TorchProbe(installed=False),
        system="win32",
        machine="AMD64",
        nvidia_smi=False,
        managed_cuda_ready=False,
        managed_cuda_version="12.8",
    )

    assert hw.kind == "none"
    eng = resolve_engine(hw, Settings(), Workload(char_count=5000))
    assert (eng.backend, eng.device) == ("onnx", "cpu")
