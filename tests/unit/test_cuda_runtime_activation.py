"""Managed CUDA runtime discovery and activation contracts."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from vienetts_app.core.cuda_runtime import CudaRuntimeLocation
from vienetts_app.core.cuda_runtime_manifest import manifest_for_platform


def _metadata(location: CudaRuntimeLocation) -> dict[str, object]:
    manifest = manifest_for_platform(location.platform_key)
    assert manifest is not None
    return {
        "format": manifest.format_version,
        "platform": manifest.platform_key,
        "python_tag": manifest.python_tag,
        "wheels": {wheel.filename: wheel.sha256 for wheel in manifest.wheels},
    }


def ready_location(tmp_path: Path, platform_key: str = "linux-x64") -> CudaRuntimeLocation:
    manifest = manifest_for_platform(platform_key)
    assert manifest is not None
    root = tmp_path / manifest.format_version
    site_packages = root / "site-packages"
    location = CudaRuntimeLocation(
        root=root,
        site_packages=site_packages,
        format_version=manifest.format_version,
        platform_key=manifest.platform_key,
        python_tag=manifest.python_tag,
    )
    (site_packages / "torch" / "lib").mkdir(parents=True)
    (site_packages / "torch" / "__init__.py").write_text("", encoding="utf-8")
    (site_packages / "torch" / "_C.pyd").write_text("", encoding="utf-8")
    native_name = "torch_cuda.dll" if platform_key == "windows-x64" else "libtorch_cuda.so"
    (site_packages / "torch" / "lib" / native_name).write_text("", encoding="utf-8")
    cuda_dir = site_packages / "nvidia" / "cuda_runtime"
    cuda_dir /= "bin" if platform_key == "windows-x64" else "lib"
    cuda_dir.mkdir(parents=True)
    cuda_name = "cudart64_12.dll" if platform_key == "windows-x64" else "libcudart.so.12"
    (cuda_dir / cuda_name).write_text("", encoding="utf-8")
    (root / "install.json").write_text(json.dumps(_metadata(location)), encoding="utf-8")
    return location


def make_compatible_layout(tmp_path: Path) -> Path:
    site_packages = tmp_path / "site-packages"
    (site_packages / "torch" / "lib").mkdir(parents=True)
    (site_packages / "torch" / "_C.cpython-313-x86_64-linux-gnu.so").write_text(
        "", encoding="utf-8"
    )
    (site_packages / "torch" / "lib" / "libtorch_cuda.so").write_text("", encoding="utf-8")
    metadata = site_packages / "torch-2.8.0+cu128.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: torch\nVersion: 2.8.0+cu128\n", encoding="utf-8"
    )
    return site_packages


def test_activation_prepends_verified_site_packages_before_factory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from vienetts_app.core.engine import TTSEngine

    location = ready_location(tmp_path)
    seen: list[str] = []
    monkeypatch.setattr(sys, "path", list(sys.path))
    engine = TTSEngine(
        backend="torch",
        cuda_runtime=location,
        factory=lambda **_kw: seen.append(sys.path[0]) or object(),
    )

    engine.initialize()

    assert seen == [str(location.site_packages)]


def test_activation_rejects_invalid_metadata_without_changing_process_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from vienetts_app.core.cuda_runtime import ManagedCudaRuntimeError, activate_cuda_runtime

    location = ready_location(tmp_path)
    (location.root / "install.json").write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sys, "path", list(sys.path))
    original_path = list(sys.path)

    with pytest.raises(ManagedCudaRuntimeError, match="metadata"):
        activate_cuda_runtime(location)

    assert sys.path == original_path


def test_activation_rejects_symlinked_site_packages_without_changing_process_path(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from vienetts_app.core.cuda_runtime import ManagedCudaRuntimeError, activate_cuda_runtime

    location = ready_location(tmp_path)
    external = tmp_path / "external"
    location.site_packages.replace(external)
    location.site_packages.symlink_to(external, target_is_directory=True)
    monkeypatch.setattr(sys, "path", list(sys.path))
    original_path = list(sys.path)

    with pytest.raises(ManagedCudaRuntimeError, match="location"):
        activate_cuda_runtime(location)

    assert sys.path == original_path


def test_activation_adds_windows_dll_directories_before_factory(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from vienetts_app.core import cuda_runtime
    from vienetts_app.core import engine as engine_module
    from vienetts_app.core.engine import TTSEngine

    location = ready_location(tmp_path, platform_key="windows-x64")
    events: list[tuple[str, str]] = []
    monkeypatch.setattr(sys, "path", list(sys.path))

    class Handle:
        def close(self) -> None:
            events.append(("close", "handle"))

    def add_dll_directory(path: str) -> Handle:
        events.append(("dll", path))
        return Handle()

    monkeypatch.setattr(
        engine_module,
        "activate_cuda_runtime",
        lambda value: cuda_runtime.activate_cuda_runtime(
            value, add_dll_directory=add_dll_directory
        ),
    )
    engine = TTSEngine(
        backend="torch",
        cuda_runtime=location,
        factory=lambda **_kw: events.append(("factory", sys.path[0])) or object(),
    )
    engine.initialize()

    assert events[:2] == [
        ("dll", str(location.site_packages / "torch" / "lib")),
        ("dll", str(location.site_packages / "nvidia" / "cuda_runtime" / "bin")),
    ]
    assert engine._cuda_activation is not None
    assert engine._cuda_activation.dll_directory_handles
    assert events[-1] == ("factory", str(location.site_packages))


def test_local_discovery_is_diagnostic_only(tmp_path: Path) -> None:
    from vienetts_app.core.cuda_runtime import discover_local_cuda_runtimes

    site_packages = make_compatible_layout(tmp_path)

    found = discover_local_cuda_runtimes(
        environ={"VIRTUAL_ENV": str(tmp_path)}, roots=[site_packages]
    )

    assert found[0].compatible is True
    assert str(tmp_path) not in found[0].label


def test_local_discovery_uses_only_declared_environment_and_roots(tmp_path: Path) -> None:
    from vienetts_app.core.cuda_runtime import discover_local_cuda_runtimes

    undeclared = make_compatible_layout(tmp_path / "undeclared")

    found = discover_local_cuda_runtimes(environ={}, roots=[])

    assert found == []
    assert undeclared.is_dir()


def test_local_discovery_recognizes_compatible_windows_layout(tmp_path: Path) -> None:
    from vienetts_app.core.cuda_runtime import discover_local_cuda_runtimes

    site_packages = tmp_path / "Lib" / "site-packages"
    torch = site_packages / "torch"
    (torch / "lib").mkdir(parents=True)
    (torch / "_C.cp313-win_amd64.pyd").write_text("", encoding="utf-8")
    (torch / "lib" / "torch_cuda.dll").write_text("", encoding="utf-8")
    metadata = site_packages / "torch-2.8.0+cu128.dist-info"
    metadata.mkdir()
    (metadata / "METADATA").write_text(
        "Metadata-Version: 2.1\nName: torch\nVersion: 2.8.0+cu128\n", encoding="utf-8"
    )

    found = discover_local_cuda_runtimes(environ={"VIRTUAL_ENV": str(tmp_path)}, roots=[])

    assert len(found) == 1
    assert found[0].compatible is True


@pytest.mark.parametrize("backend", ["onnx", "auto"])
def test_non_torch_engine_never_activates_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, backend: str
) -> None:
    from vienetts_app.core import engine as engine_module
    from vienetts_app.core.engine import TTSEngine

    activated: list[CudaRuntimeLocation] = []
    monkeypatch.setattr(engine_module, "activate_cuda_runtime", activated.append)

    TTSEngine(
        backend=backend, cuda_runtime=ready_location(tmp_path), factory=lambda **_kw: object()
    ).initialize()

    assert activated == []


def test_torch_driver_error_is_actionable(tmp_path: Path) -> None:
    from vienetts_app.core.engine import TTSEngine, TTSEngineError

    def factory(**_kwargs: Any) -> object:
        raise OSError("libcuda.so.1: cannot open shared object file")

    engine = TTSEngine(backend="torch", cuda_runtime=ready_location(tmp_path), factory=factory)

    with pytest.raises(TTSEngineError, match="NVIDIA driver"):
        engine.initialize()


def test_torch_dynamic_loader_import_error_is_actionable(tmp_path: Path) -> None:
    from vienetts_app.core.engine import TTSEngine, TTSEngineError

    def factory(**_kwargs: Any) -> object:
        raise ImportError("libtorch_cuda.so: cannot open shared object file")

    engine = TTSEngine(backend="torch", cuda_runtime=ready_location(tmp_path), factory=factory)

    with pytest.raises(TTSEngineError, match="NVIDIA driver"):
        engine.initialize()


def test_torch_windows_loader_import_error_is_actionable(tmp_path: Path) -> None:
    from vienetts_app.core.engine import TTSEngine, TTSEngineError

    def factory(**_kwargs: Any) -> object:
        raise ImportError(
            "DLL load failed while importing _C: The specified module could not be found."
        )

    engine = TTSEngine(backend="torch", cuda_runtime=ready_location(tmp_path), factory=factory)

    with pytest.raises(TTSEngineError, match="NVIDIA driver"):
        engine.initialize()


def test_onnx_import_error_is_not_misclassified_as_driver_failure() -> None:
    from vienetts_app.core.engine import TTSEngine, TTSEngineError

    def factory(**_kwargs: Any) -> object:
        raise ImportError("missing optional tokenizer")

    with pytest.raises(TTSEngineError, match="Engine initialization failed") as excinfo:
        TTSEngine(backend="onnx", factory=factory).initialize()

    assert "NVIDIA driver" not in str(excinfo.value)
