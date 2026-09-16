"""Managed CUDA runtime lookup for benchmark harnesses.

The application's CUDA engine only ever runs from the checksum-verified
managed runtime installed by the Settings CUDA card; the benchmark harnesses
reuse exactly that install so a maintainer measures the same engine the app
runs. ``--cuda-runtime DIR`` overrides the runtime root.
"""

from __future__ import annotations

from pathlib import Path

from vienetts_app.core.cuda_runtime import CudaRuntimeLocation, CudaRuntimeManager
from vienetts_app.core.cuda_runtime_manifest import manifest_for_platform
from vienetts_app.core.settings import default_data_dir
from vienetts_app.core.updates import current_platform_key


def default_cuda_runtime_root() -> Path:
    """The app's standard managed-runtime root (``<data_dir>/runtime/cuda``)."""
    return default_data_dir() / "runtime" / "cuda"


def resolve_managed_cuda_location(root: Path | None = None) -> CudaRuntimeLocation | None:
    """Return the ready managed runtime under ``root`` (default: the app's).

    ``root`` may be either the runtime manager directory (containing the
    versioned install dir) or the versioned install directory itself.
    Returns ``None`` when the platform has no manifest or the runtime is not
    ready; engine activation re-validates the files before anything loads.
    """
    manifest = manifest_for_platform(current_platform_key())
    if manifest is None:
        return None
    base = default_cuda_runtime_root() if root is None else Path(root)
    status = CudaRuntimeManager(base, manifest).inspect()
    if status.state == "ready":
        return status.location
    if (base / "install.json").is_file():
        # ``root`` may be the versioned install directory itself; inspect the
        # parent so install.json is validated the same way either way.
        status = CudaRuntimeManager(base.parent, manifest).inspect()
        if status.state == "ready":
            return status.location
    return None


def cuda_runtime_for_backend(backend: str, root: Path | None = None) -> CudaRuntimeLocation | None:
    """Managed runtime location for an explicit benchmark backend.

    ``onnx`` needs no runtime (returns ``None``); ``torch`` without a ready
    managed runtime exits with an actionable message instead of silently
    producing failed records.
    """
    if backend != "torch":
        return None
    location = resolve_managed_cuda_location(root)
    if location is None:
        where = str(default_cuda_runtime_root()) if root is None else str(root)
        raise SystemExit(
            f"No ready managed CUDA runtime found under {where}. Install it from the "
            "app's Settings CUDA runtime card, or pass --cuda-runtime DIR."
        )
    return location
