"""Checksum-verified, app-managed Qwen runtime installation.

The Qwen engine never imports from the app environment: it runs in an isolated
model host whose ``site-packages`` is the directory this manager promotes. The
runtime is a whole dependency closure (87-102 wheels, 273 MB-4.2 GB depending
on platform), so installation is staged, resumable, and only ever exposed
after every wheel has been verified and extracted.
"""

from __future__ import annotations

import json
import os
import shutil
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import build_opener

from vienetts_app.core.managed_install import (
    DownloadCancelled as _DownloadCancelled,
)
from vienetts_app.core.managed_install import (
    InstallPromotionError,
    download_wheel_archive,
    extract_wheel_archive,
    file_matches,
    promoted_install,
    safe_remove,
)
from vienetts_app.core.managed_install import (
    NoRedirectHandler as _NoRedirect,
)
from vienetts_app.core.managed_install import (
    RuntimeWheel as _Wheel,
)
from vienetts_app.core.qwen_runtime_manifest import (
    QwenRuntimeManifest,
    validate_wheel_url,
)

_DOWNLOAD_TIMEOUT_SECONDS = 30


def _running_python_tag() -> str:
    """This interpreter's wheel tag (``cp313``); the manifests are pinned per tag."""
    version = sys.version_info
    return f"cp{version.major}{version.minor}"


@dataclass(frozen=True)
class QwenRuntimeLocation:
    root: Path
    site_packages: Path
    format_version: str
    platform_key: str
    python_tag: str


@dataclass(frozen=True)
class QwenRuntimeStatus:
    state: str
    platform_key: str = ""
    installed_bytes: int = 0
    required_bytes: int = 0
    progress: float = 0.0
    error: str = ""
    location: QwenRuntimeLocation | None = None


class ManagedQwenRuntimeError(RuntimeError):
    """The app-managed Qwen runtime is unavailable, corrupt, or incomplete."""


class QwenRuntimeManager:
    """Stage, verify, and atomically promote the isolated Qwen runtime."""

    def __init__(
        self,
        root: Path,
        manifest: QwenRuntimeManifest,
        downloader: Callable[[_Wheel, Path], None] | None = None,
        disk_usage: Callable[[Path], object] = shutil.disk_usage,
        opener: Callable[..., object] | None = None,
    ) -> None:
        for wheel in manifest.wheels:
            validate_wheel_url(wheel)
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

    def _archive_path(self, wheel: _Wheel) -> Path:
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

    def _location(self, root: Path) -> QwenRuntimeLocation:
        return QwenRuntimeLocation(
            root,
            root / "site-packages",
            self.manifest.format_version,
            self.manifest.platform_key,
            self.manifest.python_tag,
        )

    @property
    def _total_bytes(self) -> int:
        return self.manifest.total_bytes

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
        location: QwenRuntimeLocation | None = None,
    ) -> QwenRuntimeStatus:
        return QwenRuntimeStatus(
            state=state,
            platform_key=self.manifest.platform_key,
            installed_bytes=installed_bytes,
            required_bytes=self._required_free_bytes,
            progress=installed_bytes / self._total_bytes if self._total_bytes else 1.0,
            error=error,
            location=location,
        )

    def _archive_validates(self, wheel: _Wheel, path: Path) -> bool:
        return file_matches(path, wheel.size_bytes, wheel.sha256)

    def _metadata(self) -> dict[str, object]:
        return {
            "format": self.manifest.format_version,
            "platform": self.manifest.platform_key,
            "python_tag": self.manifest.python_tag,
            "wheels": {wheel.filename: wheel.sha256 for wheel in self.manifest.wheels},
        }

    def inspect(self) -> QwenRuntimeStatus:
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
        safe_remove(self._staging_dir())

    def remove(self, *, in_use: bool = False) -> QwenRuntimeStatus:
        """Remove the managed runtime only when no Qwen engine has loaded it."""
        if in_use:
            return self._status("failed", error="runtime is in use; restart the app before removal")
        active = self._active_dir()
        if not active.exists():
            return self._status("unavailable")
        try:
            safe_remove(active)
        except OSError as exc:
            return self._status("failed", error=f"could not remove runtime: {exc}")
        return self._status("unavailable")

    def repair(
        self,
        cancelled: Callable[[], bool] = lambda: False,
        on_progress: Callable[[QwenRuntimeStatus], None] = lambda _status: None,
    ) -> QwenRuntimeStatus:
        """Re-install a failed runtime, keeping every archive that still verifies.

        Only archives whose size or digest no longer matches are discarded, so a
        repair after a dropped connection resumes instead of re-downloading the
        multi-gigabyte torch wheel.
        """
        existing = self.inspect()
        if existing.state == "ready":
            return existing
        for wheel in self.manifest.wheels:
            archive_path = self._archive_path(wheel)
            if archive_path.exists() and not self._archive_validates(wheel, archive_path):
                archive_path.unlink(missing_ok=True)
        return self.install(cancelled, on_progress)

    def _install_guard(self) -> QwenRuntimeStatus | None:
        """Preflight that must pass before any byte is downloaded."""
        running_tag = _running_python_tag()
        if self.manifest.python_tag != running_tag:
            return self._status(
                "failed",
                error=(
                    f"managed Qwen runtime targets Python {self.manifest.python_tag} but "
                    f"this app runs on {running_tag}; refusing a doomed "
                    f"{self._total_bytes}-byte download"
                ),
            )
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
            self._prepare_staging()
        except OSError as exc:
            return self._status("failed", error=f"staging setup failed: {exc}")
        return None

    def _download_archives(
        self,
        cancelled: Callable[[], bool],
        on_progress: Callable[[QwenRuntimeStatus], None],
    ) -> tuple[int, QwenRuntimeStatus | None]:
        verified_bytes = 0
        for wheel in self.manifest.wheels:
            if cancelled():
                return verified_bytes, self._status("unavailable", installed_bytes=verified_bytes)
            archive_path = self._archive_path(wheel)
            if archive_path.is_symlink():
                archive_path.unlink()
            if not self._archive_validates(wheel, archive_path):
                try:
                    download_wheel_archive(
                        wheel,
                        archive_path,
                        cancelled=cancelled,
                        opener=self._opener,
                        downloader=self._downloader,
                        timeout=_DOWNLOAD_TIMEOUT_SECONDS,
                        on_bytes=self._wheel_progress_reporter(wheel, verified_bytes, on_progress),
                    )
                except _DownloadCancelled:
                    if not self._archive_validates(wheel, archive_path):
                        archive_path.unlink(missing_ok=True)
                    return verified_bytes, self._status(
                        "unavailable", installed_bytes=verified_bytes
                    )
                except (HTTPError, OSError, ValueError) as exc:
                    archive_path.unlink(missing_ok=True)
                    return verified_bytes, self._status(
                        "failed", installed_bytes=verified_bytes, error=f"download failed: {exc}"
                    )
                if not self._archive_validates(wheel, archive_path):
                    archive_path.unlink(missing_ok=True)
                    return verified_bytes, self._status(
                        "failed",
                        installed_bytes=verified_bytes,
                        error=f"checksum mismatch: {wheel.filename}",
                    )
            verified_bytes += wheel.size_bytes
            on_progress(self._status("downloading", installed_bytes=verified_bytes))
        return verified_bytes, None

    def _finalize_install(
        self,
        verified_bytes: int,
        cancelled: Callable[[], bool],
        on_progress: Callable[[QwenRuntimeStatus], None],
    ) -> QwenRuntimeStatus:
        if cancelled():
            return self._status("unavailable", installed_bytes=verified_bytes)
        staging = self._staging_dir()
        on_progress(self._status("validating", installed_bytes=verified_bytes))
        site_packages = staging / "site-packages"
        safe_remove(site_packages)
        try:
            claimed_outputs: dict[Path, bool] = {}
            for wheel in self.manifest.wheels:
                if cancelled():
                    return self._status("unavailable", installed_bytes=verified_bytes)
                extract_wheel_archive(
                    wheel.filename,
                    self._archive_path(wheel),
                    site_packages,
                    claimed_outputs,
                )
            (staging / "install.json").write_text(
                json.dumps(self._metadata(), sort_keys=True),
                encoding="utf-8",
            )
        except OSError as exc:
            safe_remove(site_packages)
            return self._status("failed", installed_bytes=verified_bytes, error=str(exc))
        if cancelled():
            return self._status("unavailable", installed_bytes=verified_bytes)
        return self._promote_staging()

    def install(
        self,
        cancelled: Callable[[], bool] = lambda: False,
        on_progress: Callable[[QwenRuntimeStatus], None] = lambda _status: None,
    ) -> QwenRuntimeStatus:
        """Download, validate, extract, and atomically promote the runtime."""
        existing = self.inspect()
        if existing.state == "ready":
            return existing
        guard = self._install_guard()
        if guard is not None:
            return guard
        verified_bytes, failure = self._download_archives(cancelled, on_progress)
        if failure is not None:
            return failure
        return self._finalize_install(verified_bytes, cancelled, on_progress)

    def install_from_offline_pack(
        self,
        pack_dir: Path,
        cancelled: Callable[[], bool] = lambda: False,
        on_progress: Callable[[QwenRuntimeStatus], None] = lambda _status: None,
    ) -> QwenRuntimeStatus:
        """Install from a directory of pre-downloaded wheels (no network).

        Every file is verified against the manifest before extraction, so an
        offline pack cannot smuggle in a different build than the pinned one.
        """
        existing = self.inspect()
        if existing.state == "ready":
            return existing
        guard = self._install_guard()
        if guard is not None:
            return guard
        pack_dir = Path(pack_dir)
        verified_bytes = 0
        for wheel in self.manifest.wheels:
            if cancelled():
                return self._status("unavailable", installed_bytes=verified_bytes)
            source = pack_dir / wheel.filename
            if not source.is_file():
                return self._status(
                    "failed",
                    installed_bytes=verified_bytes,
                    error=f"offline pack is missing {wheel.filename}",
                )
            if not file_matches(source, wheel.size_bytes, wheel.sha256):
                return self._status(
                    "failed",
                    installed_bytes=verified_bytes,
                    error=f"offline pack file does not match the manifest: {wheel.filename}",
                )
            archive_path = self._archive_path(wheel)
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            if not self._archive_validates(wheel, archive_path):
                shutil.copyfile(source, archive_path)
            verified_bytes += wheel.size_bytes
            on_progress(self._status("downloading", installed_bytes=verified_bytes))
        return self._finalize_install(verified_bytes, cancelled, on_progress)

    def _recover_previous_if_needed(self) -> None:
        active = self._active_dir()
        previous = self._previous_dir()
        if not active.exists() and previous.exists():
            os.replace(previous, active)

    def _promote_staging(self) -> QwenRuntimeStatus:
        staging = self._staging_dir()
        active = self._active_dir()
        previous = self._previous_dir()
        if not self._staging_is_safe():
            return self._status("failed", error="promotion failed: staging directory is unsafe")
        try:
            self._recover_previous_if_needed()
            with promoted_install(staging, active, previous):
                status = self.inspect()
                if status.state != "ready":
                    raise InstallPromotionError(status.error or "promotion failed")
        except InstallPromotionError as exc:
            return self._status("failed", error=str(exc))
        except OSError as exc:
            return self._status("failed", error=f"promotion failed: {exc}")
        return status

    def _wheel_progress_reporter(
        self,
        wheel: _Wheel,
        base_bytes: int,
        on_progress: Callable[[QwenRuntimeStatus], None],
    ) -> Callable[[int], None]:
        """Throttle per-chunk download bytes into ``downloading`` statuses.

        torch is most of the closure (73 MB on macOS, 3.4 GB on Windows CUDA):
        without this the bar sits still for the whole download. Reports at most
        every half percent of the wheel, so a 3.4 GB download emits ~200
        updates instead of thousands.
        """
        last_fraction = 0.0

        def _report(wheel_bytes: int) -> None:
            nonlocal last_fraction
            fraction = wheel_bytes / wheel.size_bytes if wheel.size_bytes else 1.0
            if fraction - last_fraction >= 0.005 or wheel_bytes >= wheel.size_bytes:
                last_fraction = fraction
                on_progress(self._status("downloading", installed_bytes=base_bytes + wheel_bytes))

        return _report
