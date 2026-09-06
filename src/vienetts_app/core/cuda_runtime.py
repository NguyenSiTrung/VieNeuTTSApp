"""Checksum-verified, app-managed CUDA runtime installation."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import shutil
import stat
import sys
import zipfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any
from urllib.error import HTTPError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from vienetts_app.core.cuda_runtime_manifest import (
    CudaRuntimeManifest,
    RuntimeWheel,
    manifest_for_platform,
)

_CHUNK_SIZE = 1024 * 1024
_DOWNLOAD_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class CudaRuntimeLocation:
    root: Path
    site_packages: Path
    format_version: str
    platform_key: str
    python_tag: str


@dataclass(frozen=True)
class CudaRuntimeStatus:
    state: str
    platform_key: str = ""
    installed_bytes: int = 0
    required_bytes: int = 0
    progress: float = 0.0
    error: str = ""
    location: CudaRuntimeLocation | None = None


class ManagedCudaRuntimeError(RuntimeError):
    """The app-managed CUDA runtime is unavailable, corrupt, or incomplete."""


@dataclass(frozen=True)
class LocalCudaRuntime:
    """Read-only diagnostic result for an explicitly supplied local root."""

    label: str
    compatible: bool
    reason: str = ""


@dataclass(frozen=True)
class RuntimeActivation:
    """References that must outlive imports from a managed Windows runtime."""

    site_packages: Path
    dll_directory_handles: tuple[object, ...] = ()


def _manifest_metadata(manifest: CudaRuntimeManifest) -> dict[str, object]:
    return {
        "format": manifest.format_version,
        "platform": manifest.platform_key,
        "python_tag": manifest.python_tag,
        "wheels": {wheel.filename: wheel.sha256 for wheel in manifest.wheels},
    }


def _read_install_metadata(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ManagedCudaRuntimeError("managed runtime metadata is missing or corrupt") from exc
    if not isinstance(payload, dict):
        raise ManagedCudaRuntimeError("managed runtime metadata is corrupt")
    return payload


def _native_library_directories(site_packages: Path, platform_key: str) -> tuple[Path, ...]:
    torch_lib = site_packages / "torch" / "lib"
    torch_pattern = "torch_cuda.dll" if platform_key == "windows-x64" else "libtorch_cuda.so*"
    if not torch_lib.is_dir() or not any(torch_lib.glob(torch_pattern)):
        raise ManagedCudaRuntimeError("managed runtime is missing the torch CUDA libraries")

    cuda_runtime = site_packages / "nvidia" / "cuda_runtime"
    library_dir = cuda_runtime / ("bin" if platform_key == "windows-x64" else "lib")
    cuda_pattern = "cudart64*.dll" if platform_key == "windows-x64" else "libcudart.so*"
    if not library_dir.is_dir() or not any(library_dir.glob(cuda_pattern)):
        raise ManagedCudaRuntimeError("managed runtime is missing the CUDA runtime libraries")

    if platform_key != "windows-x64":
        return ()
    native_dirs = [torch_lib]
    for package_dir in (site_packages / "nvidia").glob("*"):
        bin_dir = package_dir / "bin"
        if bin_dir.is_dir() and any(bin_dir.iterdir()):
            native_dirs.append(bin_dir)
    return tuple(native_dirs)


def activate_cuda_runtime(
    location: CudaRuntimeLocation,
    *,
    add_dll_directory: Callable[[str], object] | None = None,
) -> RuntimeActivation:
    """Validate and activate one app-managed runtime before importing torch.

    This intentionally accepts only a location returned by the manager.  It
    never considers system installs, copies files, or executes discovered
    content.  Validation completes before mutating ``sys.path``.
    """
    manifest = manifest_for_platform(location.platform_key)
    expected_site_packages = location.root / "site-packages"
    if (
        manifest is None
        or location.root.is_symlink()
        or location.site_packages != expected_site_packages
        or location.site_packages.is_symlink()
        or location.format_version != manifest.format_version
        or location.python_tag != manifest.python_tag
        or not location.root.is_dir()
        or not location.site_packages.is_dir()
    ):
        raise ManagedCudaRuntimeError("managed runtime location is invalid or incomplete")
    if _read_install_metadata(location.root / "install.json") != _manifest_metadata(manifest):
        raise ManagedCudaRuntimeError(
            "managed runtime metadata does not match the verified install"
        )
    torch_package = location.site_packages / "torch"
    if not torch_package.is_dir() or not any(torch_package.glob("_C.*")):
        raise ManagedCudaRuntimeError("managed runtime is missing the torch package files")

    native_dirs = _native_library_directories(location.site_packages, location.platform_key)
    handles: tuple[object, ...] = ()
    if native_dirs:
        adder = add_dll_directory if add_dll_directory is not None else os.add_dll_directory
        try:
            handles = tuple(adder(str(directory)) for directory in native_dirs)
        except (AttributeError, OSError) as exc:
            raise ManagedCudaRuntimeError(
                "could not register managed CUDA library directories"
            ) from exc

    site_packages = str(location.site_packages)
    if not sys.path or sys.path[0] != site_packages:
        with contextlib.suppress(ValueError):
            sys.path.remove(site_packages)
        sys.path.insert(0, site_packages)
    return RuntimeActivation(location.site_packages, handles)


def _candidate_site_packages(root: Path) -> tuple[Path, ...]:
    """Return direct, conventional site-package locations without scanning."""
    candidates = [root]
    candidates.extend(
        (
            root / "site-packages",
            root / "Lib" / "site-packages",
            root / "lib" / "python3.13" / "site-packages",
        )
    )
    return tuple(candidates)


def _has_compatible_local_torch(site_packages: Path) -> bool:
    """Inspect package files only; never import or execute a local runtime."""
    if not site_packages.is_dir():
        return False
    metadata = next(site_packages.glob("torch-*.dist-info/METADATA"), None)
    if metadata is None:
        return False
    try:
        fields = {
            line.partition(":")[0].lower(): line.partition(":")[2].strip()
            for line in metadata.read_text(encoding="utf-8").splitlines()
            if ":" in line
        }
    except (OSError, UnicodeDecodeError):
        return False
    torch = site_packages / "torch"
    return (
        fields.get("name", "").lower() == "torch"
        and fields.get("version", "").startswith("2.8.0+cu128")
        and any(
            extension.name.startswith(("_C.cp313", "_C.cpython-313"))
            for extension in torch.glob("_C.*")
        )
        and (
            any((torch / "lib").glob("libtorch_cuda.so*"))
            or any((torch / "lib").glob("torch_cuda.dll"))
        )
    )


def discover_local_cuda_runtimes(
    environ: Mapping[str, str] | None = None,
    roots: Sequence[Path] = (),
) -> list[LocalCudaRuntime]:
    """Return opaque, read-only diagnostics for explicitly declared roots."""
    environment = os.environ if environ is None else environ
    candidates: list[Path] = []
    for name in ("VIRTUAL_ENV", "CONDA_PREFIX"):
        value = environment.get(name)
        if value:
            candidates.extend(_candidate_site_packages(Path(value)))
    python_path = environment.get("PYTHONPATH", "")
    candidates.extend(Path(value) for value in python_path.split(os.pathsep) if value)
    candidates.extend(Path(root) for root in roots)

    seen: set[Path] = set()
    results: list[LocalCudaRuntime] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved in seen:
            continue
        seen.add(resolved)
        if _has_compatible_local_torch(resolved):
            results.append(
                LocalCudaRuntime(
                    label=f"Local CUDA runtime {len(results) + 1}",
                    compatible=True,
                    reason="PyTorch CUDA 12.8 for Python 3.13 detected",
                )
            )
    return results


class _DownloadCancelled(Exception):
    """Internal control flow for a cancelled streaming download."""


class _NoRedirect(HTTPRedirectHandler):
    """Turn redirects into HTTP errors rather than following a new URL."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class CudaRuntimeManager:
    """Stage checksum-verified wheels before exposing an active runtime."""

    def __init__(
        self,
        root: Path,
        manifest: CudaRuntimeManifest,
        downloader: Callable[[RuntimeWheel, Path], None] | None = None,
        disk_usage: Callable[[Path], object] = shutil.disk_usage,
        opener: Callable[..., object] | None = None,
    ) -> None:
        self.root = Path(root)
        self.manifest = manifest
        self._downloader = downloader
        self._disk_usage = disk_usage
        self._opener = opener if opener is not None else build_opener(_NoRedirect()).open

    def _active_dir(self) -> Path:
        return self.root / self.manifest.format_version

    def _staging_dir(self) -> Path:
        return self.root / ".staging" / self.manifest.format_version

    def _previous_dir(self) -> Path:
        return self.root / f"{self.manifest.format_version}.previous"

    def _archive_path(self, wheel: RuntimeWheel) -> Path:
        return self._staging_dir() / "wheels" / f"{wheel.filename}.part"

    def _staging_directories(self) -> tuple[Path, Path, Path]:
        staging = self._staging_dir()
        return self.root / ".staging", staging, staging / "wheels"

    def _prepare_staging(self) -> Path:
        root = self.root.resolve()
        for directory in self._staging_directories():
            if directory.is_symlink():
                raise OSError("staging directory must not be a symlink")
            directory.mkdir(exist_ok=True)
            if (
                directory.is_symlink()
                or not directory.is_dir()
                or not directory.resolve().is_relative_to(root)
            ):
                raise OSError("staging directory is unsafe")
        return self._staging_dir()

    def _staging_is_safe(self) -> bool:
        root = self.root.resolve()
        for directory in self._staging_directories():
            if (
                directory.is_symlink()
                or not directory.is_dir()
                or not directory.resolve().is_relative_to(root)
            ):
                return False
        return True

    def _location(self, root: Path) -> CudaRuntimeLocation:
        return CudaRuntimeLocation(
            root,
            root / "site-packages",
            self.manifest.format_version,
            self.manifest.platform_key,
            self.manifest.python_tag,
        )

    @property
    def _total_bytes(self) -> int:
        return sum(wheel.size_bytes for wheel in self.manifest.wheels)

    @property
    def _required_free_bytes(self) -> int:
        # Archives remain present while their contents are expanded into staging.
        return self._total_bytes * 2

    def _status(
        self,
        state: str,
        *,
        installed_bytes: int = 0,
        error: str = "",
        location: CudaRuntimeLocation | None = None,
    ) -> CudaRuntimeStatus:
        return CudaRuntimeStatus(
            state=state,
            platform_key=self.manifest.platform_key,
            installed_bytes=installed_bytes,
            required_bytes=self._required_free_bytes,
            progress=installed_bytes / self._total_bytes if self._total_bytes else 1.0,
            error=error,
            location=location,
        )

    def _archive_validates(self, wheel: RuntimeWheel, path: Path) -> bool:
        try:
            if not path.is_file() or path.stat().st_size != wheel.size_bytes:
                return False
            digest = hashlib.sha256()
            with path.open("rb") as archive:
                for chunk in iter(lambda: archive.read(_CHUNK_SIZE), b""):
                    digest.update(chunk)
            return digest.hexdigest() == wheel.sha256
        except OSError:
            return False

    def _metadata(self) -> dict[str, object]:
        return {
            "format": self.manifest.format_version,
            "platform": self.manifest.platform_key,
            "python_tag": self.manifest.python_tag,
            "wheels": {wheel.filename: wheel.sha256 for wheel in self.manifest.wheels},
        }

    def inspect(self) -> CudaRuntimeStatus:
        """Report only a complete runtime whose immutable metadata matches."""
        active = self._active_dir()
        if active.is_symlink():
            return self._status("failed", error="active runtime must not be a symlink")
        if not active.is_dir():
            return self._status("unavailable")
        install_path = active / "install.json"
        if not install_path.is_file():
            return self._status("failed", error="install metadata is missing")
        try:
            metadata = json.loads(install_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return self._status("failed", error="install metadata is corrupt")
        if metadata != self._metadata():
            return self._status("failed", error="install metadata does not match the manifest")
        location = self._location(active)
        if not location.site_packages.is_dir():
            return self._status("failed", error="active runtime is incomplete")
        return self._status("ready", installed_bytes=self._total_bytes, location=location)

    def cancel_staging(self) -> None:
        """Discard resumable archives and any incomplete staged extraction."""
        self._remove_staged_path(self._staging_dir())

    def remove(self, *, in_use: bool = False) -> CudaRuntimeStatus:
        """Remove the managed runtime only when no CUDA engine has loaded it."""
        if in_use:
            return self._status("failed", error="runtime is in use; restart the app before removal")
        active = self._active_dir()
        if not active.exists():
            return self._status("unavailable")
        try:
            self._remove_staged_path(active)
        except OSError as exc:
            return self._status("failed", error=f"could not remove runtime: {exc}")
        return self._status("unavailable")

    def _response_status(self, response: object) -> int:
        status = getattr(response, "status", None)
        if isinstance(status, int):
            return status
        getcode = getattr(response, "getcode", None)
        if callable(getcode):
            return int(getcode())
        return 0

    def _response_url(self, response: object) -> str | None:
        geturl = getattr(response, "geturl", None)
        if callable(geturl):
            value = geturl()
            return str(value) if value is not None else None
        value = getattr(response, "url", None)
        return str(value) if value is not None else None

    def _response_header(self, response: object, name: str) -> str | None:
        headers = getattr(response, "headers", None)
        getter = getattr(headers, "get", None)
        value = getter(name) if callable(getter) else None
        return str(value) if value is not None else None

    def _range_is_honored(self, response: object, offset: int, wheel: RuntimeWheel) -> bool:
        content_range = self._response_header(response, "Content-Range")
        if self._response_status(response) != 206 or content_range is None:
            return False
        try:
            unit, values = content_range.split(" ", 1)
            byte_range, total = values.split("/", 1)
            start, end = (int(value) for value in byte_range.split("-", 1))
            return (
                unit.lower() == "bytes"
                and start == offset
                and end >= start
                and total == str(wheel.size_bytes)
            )
        except ValueError:
            return False

    def _stream_response(
        self,
        response: object,
        target: Path,
        mode: str,
        cancelled: Callable[[], bool],
        maximum_bytes: int,
    ) -> None:
        reader = getattr(response, "read", None)
        if not callable(reader):
            raise OSError("download response is not readable")
        current_bytes = target.stat().st_size if mode == "ab" and target.exists() else 0
        with target.open(mode) as destination:
            while True:
                if cancelled():
                    raise _DownloadCancelled
                chunk = reader(_CHUNK_SIZE)
                if not chunk:
                    return
                if len(chunk) > maximum_bytes - current_bytes:
                    raise OSError("download response exceeds declared wheel size")
                destination.write(chunk)
                current_bytes += len(chunk)
                if cancelled():
                    raise _DownloadCancelled

    def _stream_from_response(
        self,
        response: object,
        wheel: RuntimeWheel,
        target: Path,
        offset: int,
        cancelled: Callable[[], bool],
    ) -> bool:
        response_url = self._response_url(response)
        if response_url != wheel.url:
            raise OSError("download response redirected away from the manifest URL")
        if offset and self._range_is_honored(response, offset, wheel):
            self._stream_response(response, target, "ab", cancelled, wheel.size_bytes)
            return True
        if self._response_status(response) == 200:
            self._stream_response(response, target, "wb", cancelled, wheel.size_bytes)
            return True
        return False

    def _open(self, request: Request) -> object:
        return self._opener(request, timeout=_DOWNLOAD_TIMEOUT_SECONDS)

    def _download_with_http(
        self,
        wheel: RuntimeWheel,
        target: Path,
        cancelled: Callable[[], bool],
    ) -> None:
        try:
            offset = target.stat().st_size
        except OSError:
            offset = 0
        if offset >= wheel.size_bytes:
            target.unlink(missing_ok=True)
            offset = 0

        headers = {"Range": f"bytes={offset}-"} if offset else {}
        request = Request(wheel.url, headers=headers)
        with self._open(request) as response:
            if self._stream_from_response(response, wheel, target, offset, cancelled):
                return
        # A malformed 206 response cannot safely be treated as a full download.
        fresh_request = Request(wheel.url)
        with self._open(fresh_request) as response:
            if self._response_url(response) != wheel.url or self._response_status(response) != 200:
                raise OSError("server did not honor a safe full-download restart")
            self._stream_response(response, target, "wb", cancelled, wheel.size_bytes)

    def _download_archive(
        self,
        wheel: RuntimeWheel,
        target: Path,
        cancelled: Callable[[], bool],
    ) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if self._downloader is not None:
            self._downloader(wheel, target)
        else:
            self._download_with_http(wheel, target, cancelled)

    def _member_destination(self, root: Path, member: zipfile.ZipInfo) -> tuple[Path, bool]:
        name = member.filename.replace("\\", "/")
        posix_path = PurePosixPath(name)
        windows_path = PureWindowsPath(name)
        mode = member.external_attr >> 16
        if (
            not name
            or posix_path.is_absolute()
            or windows_path.is_absolute()
            or windows_path.drive
            or ".." in posix_path.parts
            or stat.S_ISLNK(mode)
        ):
            raise ValueError(f"unsafe wheel member: {member.filename}")
        destination = (root / Path(*posix_path.parts)).resolve()
        if not destination.is_relative_to(root.resolve()):
            raise ValueError(f"unsafe wheel member: {member.filename}")
        return destination, member.is_dir()

    def _validate_wheel_layout(
        self,
        archive: zipfile.ZipFile,
        root: Path,
        claimed_outputs: dict[Path, bool],
    ) -> tuple[list[tuple[zipfile.ZipInfo, Path]], dict[Path, bool]]:
        outputs = dict(claimed_outputs)
        validated: list[tuple[zipfile.ZipInfo, Path]] = []
        for member in archive.infolist():
            destination, is_directory = self._member_destination(root, member)
            existing = outputs.get(destination)
            if existing is not None and (not existing or not is_directory):
                raise ValueError(f"unsafe wheel member: {member.filename}")
            for parent in destination.parents:
                if parent == root.parent:
                    break
                if outputs.get(parent) is False:
                    raise ValueError(f"unsafe wheel member: {member.filename}")
            if not is_directory and any(
                path != destination and not output_is_directory and path.is_relative_to(destination)
                for path, output_is_directory in outputs.items()
            ):
                raise ValueError(f"unsafe wheel member: {member.filename}")
            outputs[destination] = is_directory or existing is True
            validated.append((member, destination))
        return validated, outputs

    def _extract_wheel(
        self,
        wheel: RuntimeWheel,
        archive_path: Path,
        site_packages: Path,
        claimed_outputs: dict[Path, bool],
    ) -> None:
        try:
            with zipfile.ZipFile(archive_path) as archive:
                validated, validated_outputs = self._validate_wheel_layout(
                    archive,
                    site_packages,
                    claimed_outputs,
                )
                for member, destination in validated:
                    if member.is_dir():
                        destination.mkdir(parents=True, exist_ok=True)
                        continue
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with archive.open(member) as source, destination.open("wb") as output:
                        shutil.copyfileobj(source, output, _CHUNK_SIZE)
                claimed_outputs.clear()
                claimed_outputs.update(validated_outputs)
        except (OSError, ValueError, zipfile.BadZipFile) as exc:
            archive_path.unlink(missing_ok=True)
            raise OSError(str(exc)) from exc

    def _remove_staged_path(self, path: Path) -> None:
        if path.is_symlink() or path.is_file():
            path.unlink(missing_ok=True)
        else:
            shutil.rmtree(path, ignore_errors=True)

    def _recover_previous_if_needed(self) -> None:
        active = self._active_dir()
        previous = self._previous_dir()
        if not active.exists() and previous.exists():
            os.replace(previous, active)

    def _promote_staging(self) -> CudaRuntimeStatus:
        staging = self._staging_dir()
        active = self._active_dir()
        previous = self._previous_dir()
        try:
            if not self._staging_is_safe():
                return self._status("failed", error="promotion failed: staging directory is unsafe")
            self._recover_previous_if_needed()
            if previous.exists():
                shutil.rmtree(previous)
            had_active = active.exists()
            if had_active:
                os.replace(active, previous)
            try:
                os.replace(staging, active)
            except OSError as exc:
                if had_active and not active.exists() and previous.exists():
                    with contextlib.suppress(OSError):
                        os.replace(previous, active)
                return self._status("failed", error=f"promotion failed: {exc}")
            status = self.inspect()
            if status.state == "ready":
                if previous.exists():
                    shutil.rmtree(previous, ignore_errors=True)
                return status
            if had_active:
                shutil.rmtree(active, ignore_errors=True)
                with contextlib.suppress(OSError):
                    os.replace(previous, active)
            return self._status("failed", error=status.error or "promotion failed")
        except OSError as exc:
            return self._status("failed", error=f"promotion failed: {exc}")

    def install(
        self,
        cancelled: Callable[[], bool] = lambda: False,
        on_progress: Callable[[CudaRuntimeStatus], None] = lambda _status: None,
    ) -> CudaRuntimeStatus:
        """Download, validate, extract, and atomically promote a runtime."""
        existing = self.inspect()
        if existing.state == "ready":
            return existing
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            free = int(self._disk_usage(self.root).free)  # type: ignore[attr-defined]
        except (OSError, TypeError, ValueError, AttributeError) as exc:
            return self._status("failed", error=f"could not determine free disk space: {exc}")
        if free < self._required_free_bytes:
            return self._status(
                "failed",
                error=(
                    f"insufficient disk space: need {self._required_free_bytes} bytes, free {free}"
                ),
            )

        try:
            staging = self._prepare_staging()
        except OSError as exc:
            return self._status("failed", error=f"staging setup failed: {exc}")
        site_packages = staging / "site-packages"
        verified_bytes = 0

        for wheel in self.manifest.wheels:
            if cancelled():
                return self._status("unavailable", installed_bytes=verified_bytes)
            archive_path = self._archive_path(wheel)
            if archive_path.is_symlink():
                archive_path.unlink()
            if not self._archive_validates(wheel, archive_path):
                try:
                    self._download_archive(wheel, archive_path, cancelled)
                except _DownloadCancelled:
                    if not self._archive_validates(wheel, archive_path):
                        archive_path.unlink(missing_ok=True)
                    return self._status("unavailable", installed_bytes=verified_bytes)
                except (HTTPError, OSError, ValueError) as exc:
                    archive_path.unlink(missing_ok=True)
                    return self._status(
                        "failed", installed_bytes=verified_bytes, error=f"download failed: {exc}"
                    )
                if not self._archive_validates(wheel, archive_path):
                    archive_path.unlink(missing_ok=True)
                    return self._status(
                        "failed",
                        installed_bytes=verified_bytes,
                        error=f"checksum mismatch: {wheel.filename}",
                    )
            verified_bytes += wheel.size_bytes
            on_progress(self._status("downloading", installed_bytes=verified_bytes))
            if cancelled():
                return self._status("unavailable", installed_bytes=verified_bytes)

        if cancelled():
            return self._status("unavailable", installed_bytes=verified_bytes)
        on_progress(self._status("validating", installed_bytes=verified_bytes))
        self._remove_staged_path(site_packages)
        try:
            claimed_outputs: dict[Path, bool] = {}
            for wheel in self.manifest.wheels:
                if cancelled():
                    return self._status("unavailable", installed_bytes=verified_bytes)
                self._extract_wheel(
                    wheel,
                    self._archive_path(wheel),
                    site_packages,
                    claimed_outputs,
                )
            (staging / "install.json").write_text(
                json.dumps(self._metadata(), sort_keys=True),
                encoding="utf-8",
            )
        except OSError as exc:
            self._remove_staged_path(site_packages)
            return self._status("failed", installed_bytes=verified_bytes, error=str(exc))
        if cancelled():
            return self._status("unavailable", installed_bytes=verified_bytes)
        return self._promote_staging()
