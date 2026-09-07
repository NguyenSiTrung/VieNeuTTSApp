"""Hardware → engine detection (§6).

Mirrors the SDK's auto-detection for *display* and applies the workload
heuristic + user override (§6.2/§6.3). The SDK remains the source of truth
for the actual engine pick; this module never loads a model.
"""

from __future__ import annotations

import platform
import re
import subprocess
import sys
from dataclasses import dataclass
from importlib import metadata
from typing import Any, Literal

from vienetts_app.core.models import EngineInfo, Settings

HardwareKind = Literal["nvidia", "apple_silicon", "apple_intel", "none"]

REQUIRED_CUDA = (12, 8)
# A single SDK chunk (max_chars=256) is the interactive/short boundary.
SHORT_TEXT_CHARS = 256


@dataclass(frozen=True)
class TorchProbe:
    """Result of probing the (optional) torch installation."""

    installed: bool
    cuda_available: bool = False
    cuda_version: str | None = None


@dataclass(frozen=True)
class CudaDriverProbe:
    """Read-only NVIDIA driver readiness result, without importing torch."""

    available: bool
    cuda_version: str | None = None

    @property
    def usable(self) -> bool:
        return self.available and _cuda_at_least(self.cuda_version, REQUIRED_CUDA)


@dataclass(frozen=True)
class HardwareInfo:
    kind: HardwareKind
    torch_installed: bool
    cuda_version: str | None


@dataclass(frozen=True)
class Workload:
    streaming: bool = False
    char_count: int = 0
    batch: bool = False


def probe_torch() -> TorchProbe:
    """Inspect installed torch metadata without importing native extensions."""
    try:
        metadata.version("torch")
    except Exception:  # package metadata can be absent or corrupt
        return TorchProbe(installed=False)
    return TorchProbe(installed=True, cuda_available=False, cuda_version=None)


def _cuda_at_least(version: str | None, required: tuple[int, int]) -> bool:
    if not version:
        return False
    try:
        parts = tuple(int(p) for p in str(version).split(".")[:2])
    except ValueError:
        return False
    return parts >= required


def _which_nvidia_smi() -> bool:
    import shutil

    return shutil.which("nvidia-smi") is not None


def probe_cuda_driver() -> CudaDriverProbe:
    """Inspect ``nvidia-smi`` output without loading torch or native Python code."""
    run_kwargs: dict[str, Any] = {}
    if sys.platform == "win32":
        # Windowed/frozen builds must not flash a console window for the probe.
        creation_flag = getattr(subprocess, "CREATE_NO_WINDOW", 0)
        if creation_flag:
            run_kwargs["creationflags"] = creation_flag
    try:
        completed = subprocess.run(
            ["nvidia-smi"],
            capture_output=True,
            check=False,
            text=True,
            timeout=3,
            **run_kwargs,
        )
    except (OSError, subprocess.SubprocessError):
        return CudaDriverProbe(available=False)
    if completed.returncode != 0:
        return CudaDriverProbe(available=False)
    match = re.search(r"CUDA Version:\s*(\d+\.\d+)", completed.stdout)
    return CudaDriverProbe(
        available=match is not None, cuda_version=match.group(1) if match else None
    )


def detect_hardware(
    probe: TorchProbe | None = None,
    system: str | None = None,
    machine: str | None = None,
    nvidia_smi: bool | None = None,
    *,
    managed_cuda_ready: bool = False,
    managed_cuda_version: str | None = None,
) -> HardwareInfo:
    """Classify hardware per §6.1. All inputs injectable for tests."""
    probe = probe_torch() if probe is None else probe
    system = sys.platform if system is None else system
    machine = platform.machine() if machine is None else machine
    if nvidia_smi is None:
        nvidia_smi = _which_nvidia_smi()

    if probe.cuda_available:
        return HardwareInfo("nvidia", probe.installed, probe.cuda_version)
    if managed_cuda_ready:
        # App-managed runtime plus a usable NVIDIA driver: torch serves CUDA
        # from the managed site-packages even though no system torch exists
        # (frozen builds never install one). The caller owns the readiness
        # claim — this stays import-free.
        return HardwareInfo("nvidia", True, managed_cuda_version)
    if nvidia_smi and system != "darwin":
        # NVIDIA driver present without usable torch/CUDA (§11 notice case).
        return HardwareInfo("nvidia", probe.installed, probe.cuda_version)
    if system == "darwin":
        kind: HardwareKind = "apple_silicon" if machine == "arm64" else "apple_intel"
        return HardwareInfo(kind, probe.installed, None)
    # AMD / Intel Arc / iGPU / unknown discrete GPUs are not detectable
    # without torch, and the SDK has no CUDA path for them anyway (§4).
    return HardwareInfo("none", probe.installed, None)


def detected_engine_info(hw: HardwareInfo) -> EngineInfo:
    """The §7.4 settings readout: the hardware's best engine (capability view)."""
    return resolve_engine(hw, Settings(), Workload(char_count=SHORT_TEXT_CHARS + 1))


def resolve_engine(hw: HardwareInfo, settings: Settings, workload: Workload) -> EngineInfo:
    """Apply §6.2: user override over workload heuristic over hardware matrix."""
    cuda_ok = (
        hw.kind == "nvidia"
        and hw.torch_installed
        and _cuda_at_least(hw.cuda_version, REQUIRED_CUDA)
    )

    if settings.backend == "onnx":
        backend = "onnx"
    elif settings.backend == "torch":
        backend = "torch" if cuda_ok else "onnx"
    else:  # auto
        if workload.streaming:
            backend = "onnx"  # streaming stays on ONNX by design (§6.2)
        elif workload.batch or workload.char_count > SHORT_TEXT_CHARS:
            backend = "torch" if cuda_ok else "onnx"
        else:
            backend = "onnx"  # short interactive text is fastest on CPU

    precision = settings.precision
    if backend == "torch" and cuda_ok:
        precision = "fp32" if settings.backend == "auto" else settings.precision

    note = _describe(hw, backend, settings, cuda_ok)
    return EngineInfo(
        backend=backend,  # type: ignore[arg-type]
        device="cuda" if backend == "torch" else "cpu",
        precision=precision,  # type: ignore[arg-type]
        cuda_version=hw.cuda_version if backend == "torch" else None,
        note=note,
    )


def _describe(hw: HardwareInfo, backend: str, settings: Settings, cuda_ok: bool) -> str:
    if backend == "torch":
        return f"PyTorch · CUDA {hw.cuda_version} · batched"
    if hw.kind == "nvidia" and not hw.torch_installed:
        return "ONNX Runtime CPU · int8 · CUDA GPU found but torch not installed"
    if hw.kind == "nvidia" and not cuda_ok:
        need = f"{REQUIRED_CUDA[0]}.{REQUIRED_CUDA[1]}"
        return f"ONNX Runtime CPU · int8 · CUDA {hw.cuda_version} < {need}"
    if settings.backend == "torch":
        return "ONNX Runtime CPU · torch requested but no usable CUDA"
    if hw.kind == "apple_intel":
        # No GPU acceleration, and the frozen macOS download is arm64-only —
        # Intel Macs must run from source (README: Releases).
        return (
            "ONNX Runtime CPU · Intel Mac · no GPU; "
            "the macOS download is Apple Silicon only (run from source)"
        )
    label = {"apple_silicon": "Apple Silicon", "none": "CPU"}.get(hw.kind, "CPU")
    return f"ONNX Runtime CPU · {label} · fastest available engine here"
