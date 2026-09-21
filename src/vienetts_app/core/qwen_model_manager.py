"""Staging-only managed installer for the Qwen engine profiles.

One install root holds both profiles side by side plus one shared tree:

    <root>/customvoice/…        profile weights, config and tokenizer
    <root>/base/…
    <root>/shared/…             byte-identical tokenizer + speech tokenizer

Filesystem-only inspection; network and full-file verification run through the
injected downloader seam (``hf_hub_download`` by default). No Qt, and no
top-level Hub import, so the GUI and startup paths never pay for it.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from vienetts_app.core.managed_install import (
    InstallPromotionError,
    file_matches,
    free_space_bytes,
    is_same_file,
    normalize_windows_path,
    promoted_install,
    safe_remove,
    sampled_progress,
    tree_size_bytes,
)
from vienetts_app.core.qwen_model_manifest import (
    FORMAT_VERSION,
    MANIFEST,
    PROFILE_KEYS,
    QwenModelFile,
    QwenModelManifest,
    QwenModelProfile,
    profile_for,
)

SHARED_DIR_NAME = "shared"


@dataclass(frozen=True)
class QwenModelLocation:
    root: Path
    profile_dir: Path
    shared_dir: Path
    format_version: str
    profile_key: str
    revision: str


@dataclass(frozen=True)
class QwenModelStatus:
    state: str
    profile_key: str = ""
    installed_bytes: int = 0
    required_bytes: int = 0
    progress: float = 0.0
    error: str = ""
    location: QwenModelLocation | None = None


class ManagedQwenModelError(RuntimeError):
    """The managed Qwen model install is unavailable, corrupt, or incomplete."""


class QwenModelManager:
    """Owns one profile's install inside a shared Qwen model root."""

    def __init__(
        self,
        root: Path,
        profile_key: str,
        profile: QwenModelProfile | None = None,
        downloader: Callable[..., Path] | None = None,
        disk_usage: Callable[[Path], object] = shutil.disk_usage,
        progress_interval_seconds: float = 0.25,
    ) -> None:
        resolved = profile if profile is not None else profile_for(profile_key)
        if resolved is None:
            raise ManagedQwenModelError(f"no verified manifest for profile {profile_key!r}")
        if resolved.key != profile_key:
            raise ManagedQwenModelError(
                f"profile manifest {resolved.key!r} does not match {profile_key!r}"
            )
        self.root = Path(root)
        self.profile_key = profile_key
        self.profile = resolved
        self._downloader = downloader
        self._disk_usage = disk_usage
        self._progress_interval_seconds = progress_interval_seconds

    def _active_dir(self) -> Path:
        return self.root / self.profile_key

    def _shared_dir(self) -> Path:
        return self.root / SHARED_DIR_NAME

    def _staging_dir(self) -> Path:
        return self.root / ".staging" / self.profile_key

    def _shared_staging_dir(self) -> Path:
        return self.root / ".staging" / SHARED_DIR_NAME

    def _previous_dir(self) -> Path:
        return self.root / f"{self.profile_key}.previous"

    def _staging_path(self, record: QwenModelFile) -> Path:
        return self._staging_dir() / record.path

    def _shared_staging_path(self, record: QwenModelFile) -> Path:
        return self._shared_staging_dir() / record.path

    def _location(self, active: Path) -> QwenModelLocation:
        return QwenModelLocation(
            root=active,
            profile_dir=active,
            shared_dir=self._shared_dir(),
            format_version=FORMAT_VERSION,
            profile_key=self.profile_key,
            revision=self.profile.revision,
        )

    @property
    def _total_bytes(self) -> int:
        return self.profile.total_bytes

    def _status(
        self,
        state: str,
        *,
        installed_bytes: int = 0,
        error: str = "",
        location: QwenModelLocation | None = None,
    ) -> QwenModelStatus:
        return QwenModelStatus(
            state=state,
            profile_key=self.profile_key,
            installed_bytes=installed_bytes,
            required_bytes=self.profile.required_free_bytes,
            progress=installed_bytes / self._total_bytes if self._total_bytes else 1.0,
            error=error,
            location=location,
        )

    def _metadata(self) -> dict[str, object]:
        return {
            "format": FORMAT_VERSION,
            "profile": self.profile_key,
            "repo": self.profile.repo,
            "revision": self.profile.revision,
            "files": {item.path: item.sha256 for item in self.profile.files},
            "shared": {item.path: item.sha256 for item in self.profile.shared},
        }

    def _missing_files(self) -> tuple[QwenModelFile, ...]:
        """Files that are absent or corrupt in the active and shared trees."""
        missing = [
            item
            for item in self.profile.files
            if not file_matches(self._active_dir() / item.path, item.size_bytes, item.sha256)
        ]
        missing += [
            item
            for item in self.profile.shared
            if not file_matches(self._shared_dir() / item.path, item.size_bytes, item.sha256)
        ]
        return tuple(missing)

    def inspect(self) -> QwenModelStatus:
        """Report only a complete profile whose metadata matches the manifest."""
        active = self._active_dir()
        if active.is_symlink():
            return self._status("failed", error="profile directory must not be a symlink")
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
        missing = self._missing_files()
        if missing:
            return self._status("failed", error=f"profile is incomplete: {missing[0].path}")
        return self._status(
            "ready", installed_bytes=self._total_bytes, location=self._location(active)
        )

    def cancel_staging(self) -> None:
        """Discard this profile's staged files; shared staging is left alone."""
        safe_remove(self._staging_dir())

    def remove(self, *, in_use: bool = False, remove_shared: bool = False) -> QwenModelStatus:
        """Remove one profile; shared content survives unless explicitly removed.

        The shared tokenizer tree is byte-identical for both profiles, so it is
        kept by default: deleting it would force a 686 MB re-download the next
        time either profile is installed.
        """
        if in_use:
            return self._status("failed", error="model is in use; restart the app before removal")
        active = self._active_dir()
        if not active.exists():
            return self._status("unavailable")
        try:
            safe_remove(active)
            if remove_shared:
                safe_remove(self._shared_dir())
        except OSError as exc:
            return self._status("failed", error=f"could not remove model: {exc}")
        return self._status("unavailable")

    def repair(
        self,
        cancelled: Callable[[], bool] = lambda: False,
        on_progress: Callable[[QwenModelStatus], None] = lambda _status: None,
    ) -> QwenModelStatus:
        """Re-install a failed profile, keeping every file that still verifies."""
        existing = self.inspect()
        if existing.state == "ready":
            return existing
        for record in (*self.profile.files, *self.profile.shared):
            for candidate in (self._staging_path(record), self._shared_staging_path(record)):
                if candidate.exists() and not file_matches(
                    candidate, record.size_bytes, record.sha256
                ):
                    candidate.unlink(missing_ok=True)
        return self.install(cancelled, on_progress)

    def _default_downloader(self) -> Callable[..., Path]:
        from huggingface_hub import hf_hub_download  # lazy: never on inspect path

        return hf_hub_download  # type: ignore[return-value]

    def _download_into(
        self,
        record: QwenModelFile,
        destination: Path,
        repo: str,
        revision: str,
        downloader: Callable[..., Path],
    ) -> tuple[Path, str]:
        """Fetch one file into ``destination`` and verify it. Returns (path, error)."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        local_dir = destination.parent
        local_dir_str = str(local_dir)
        if os.name == "nt":
            resolved = str(local_dir.resolve())
            if (
                len(resolved) >= 240
                and not resolved.startswith("\\\\?\\")
                and resolved[1:3] == ":\\"
            ):
                local_dir_str = "\\\\?\\" + resolved
        try:
            result = downloader(
                repo_id=repo,
                filename=record.path,
                revision=revision,
                local_dir=local_dir_str,
                local_dir_use_symlinks=False,
            )
        except Exception as exc:  # noqa: BLE001 - downloader seam reports anything
            return destination, f"download failed: {exc}"

        candidate = Path(str(result)) if result is not None else destination
        if os.name == "nt" and str(candidate).startswith("\\\\?\\"):
            stripped = Path(normalize_windows_path(str(candidate)))
            if stripped.is_file():
                candidate = stripped
        if not candidate.is_file():
            candidate = destination
        if (
            candidate != destination
            and candidate.is_file()
            and not is_same_file(candidate, destination)
        ):
            try:
                shutil.copyfile(candidate, destination)
            except OSError as exc:
                return destination, f"could not stage verified file: {exc}"
            candidate = destination
        if not file_matches(candidate, record.size_bytes, record.sha256):
            with contextlib.suppress(OSError):
                candidate.unlink(missing_ok=True)
            if candidate != destination:
                with contextlib.suppress(OSError):
                    destination.unlink(missing_ok=True)
            return destination, f"checksum mismatch for {record.path}"
        return destination, ""

    def _stage_files(
        self,
        cancelled: Callable[[], bool],
        on_progress: Callable[[QwenModelStatus], None],
    ) -> tuple[int, QwenModelStatus | None]:
        downloader = (
            self._downloader if self._downloader is not None else self._default_downloader()
        )
        verified_bytes = 0
        # Shared files already promoted for a sibling profile never enter this
        # staging tree, so they are tracked separately and added on top of the
        # measured bytes. Everything else — verified staged files plus the
        # downloader's in-flight *.incomplete partials — lives under the two
        # staging roots and is summed off the disk: real bytes, no estimate.
        reused_bytes = 0
        staging_roots = (self._staging_dir(), self._shared_staging_dir())

        def measured_bytes() -> int:
            return min(self._total_bytes, reused_bytes + tree_size_bytes(staging_roots))

        def report_sampled(value: int) -> None:
            on_progress(self._status("downloading", installed_bytes=value))

        with sampled_progress(
            measured_bytes,
            report_sampled,
            interval_seconds=self._progress_interval_seconds,
        ):
            for record, destination, repo, revision in (
                *(
                    (item, self._staging_path(item), self.profile.repo, self.profile.revision)
                    for item in self.profile.files
                ),
                *(
                    (
                        item,
                        self._shared_staging_path(item),
                        self.profile.repo,
                        self.profile.revision,
                    )
                    for item in self.profile.shared
                ),
            ):
                if cancelled():
                    return verified_bytes, self._status(
                        "unavailable", installed_bytes=verified_bytes
                    )
                if file_matches(self._shared_dir() / record.path, record.size_bytes, record.sha256):
                    # Already promoted for another profile: one download, both use it.
                    verified_bytes += record.size_bytes
                    reused_bytes += record.size_bytes
                    on_progress(self._status("downloading", installed_bytes=verified_bytes))
                    continue
                if not file_matches(destination, record.size_bytes, record.sha256):
                    _, error = self._download_into(record, destination, repo, revision, downloader)
                    if error:
                        return verified_bytes, self._status(
                            "failed", installed_bytes=verified_bytes, error=error
                        )
                verified_bytes += record.size_bytes
                on_progress(self._status("downloading", installed_bytes=verified_bytes))
        return verified_bytes, None

    def _promote_shared(self) -> str:
        """Move verified shared files into place; returns an error message."""
        for record in self.profile.shared:
            staged = self._shared_staging_path(record)
            target = self._shared_dir() / record.path
            if file_matches(target, record.size_bytes, record.sha256):
                continue
            if not file_matches(staged, record.size_bytes, record.sha256):
                return f"shared file is missing or corrupt: {record.path}"
            try:
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(staged, target)
            except OSError as exc:
                return f"could not promote shared file {record.path}: {exc}"
        return ""

    def _promote_staging(self) -> QwenModelStatus:
        staging = self._staging_dir()
        active = self._active_dir()
        previous = self._previous_dir()
        shared_error = self._promote_shared()
        if shared_error:
            return self._status("failed", error=shared_error)
        try:
            if not active.exists() and previous.exists():
                os.replace(previous, active)
            with promoted_install(staging, active, previous):
                status = self.inspect()
                if status.state != "ready":
                    raise InstallPromotionError(status.error or "promotion failed")
        except InstallPromotionError as exc:
            return self._status("failed", error=str(exc))
        except OSError as exc:
            return self._status("failed", error=f"promotion failed: {exc}")
        return status

    def _install_guard(self) -> QwenModelStatus | None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return self._status("failed", error=f"could not create model root: {exc}")
        free = free_space_bytes(self.root, self._disk_usage)
        if free is not None and free < self.profile.required_free_bytes:
            return self._status(
                "failed",
                error=(
                    f"insufficient disk space: need {self.profile.required_free_bytes} bytes, "
                    f"free {free}"
                ),
            )
        for directory in (self._staging_dir(), self._shared_staging_dir()):
            if directory.is_symlink():
                return self._status("failed", error="staging directory must not be a symlink")
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return self._status("failed", error=f"staging setup failed: {exc}")
            if (
                directory.is_symlink()
                or not directory.is_dir()
                or not directory.resolve().is_relative_to(self.root.resolve())
            ):
                return self._status("failed", error="staging directory is unsafe")
        return None

    def install(
        self,
        cancelled: Callable[[], bool] = lambda: False,
        on_progress: Callable[[QwenModelStatus], None] = lambda _status: None,
    ) -> QwenModelStatus:
        """Download, verify, and atomically promote one profile."""
        existing = self.inspect()
        if existing.state == "ready":
            return existing
        guard = self._install_guard()
        if guard is not None:
            return guard
        verified_bytes, failure = self._stage_files(cancelled, on_progress)
        if failure is not None:
            return failure
        if cancelled():
            return self._status("unavailable", installed_bytes=verified_bytes)
        on_progress(self._status("validating", installed_bytes=verified_bytes))
        try:
            (self._staging_dir() / "install.json").write_text(
                json.dumps(self._metadata(), sort_keys=True),
                encoding="utf-8",
            )
        except OSError as exc:
            return self._status(
                "failed", installed_bytes=verified_bytes, error=f"could not write metadata: {exc}"
            )
        return self._promote_staging()

    def install_offline_pack(
        self,
        source: Path,
        cancelled: Callable[[], bool] = lambda: False,
        on_progress: Callable[[QwenModelStatus], None] = lambda _status: None,
    ) -> QwenModelStatus:
        """Install from a directory tree laid out exactly like the model root."""
        existing = self.inspect()
        if existing.state == "ready":
            return existing
        src = Path(source)
        if not src.is_dir():
            return self._status("failed", error="offline pack must be a directory")
        allowed = {item.path for item in self.profile.files}
        shared_allowed = {item.path for item in self.profile.shared}
        for path in sorted(src.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(src).as_posix()
            if relative == f"{self.profile_key}/install.json":
                continue
            if relative.startswith(f"{self.profile_key}/"):
                candidate = relative[len(self.profile_key) + 1 :]
                if candidate not in allowed:
                    return self._status(
                        "failed", error=f"offline pack contains unexpected path: {relative}"
                    )
                continue
            if relative.startswith(f"{SHARED_DIR_NAME}/"):
                candidate = relative[len(SHARED_DIR_NAME) + 1 :]
                if candidate not in shared_allowed:
                    return self._status(
                        "failed", error=f"offline pack contains unexpected path: {relative}"
                    )
                continue
            return self._status(
                "failed", error=f"offline pack contains unexpected path: {relative}"
            )

        for record in self.profile.files:
            candidate = src / self.profile_key / record.path
            if not file_matches(candidate, record.size_bytes, record.sha256):
                return self._status(
                    "failed", error=f"offline pack is missing or corrupt: {record.path}"
                )
        for record in self.profile.shared:
            candidate = src / SHARED_DIR_NAME / record.path
            if not file_matches(candidate, record.size_bytes, record.sha256):
                return self._status(
                    "failed", error=f"offline pack is missing or corrupt: {record.path}"
                )
        guard = self._install_guard()
        if guard is not None:
            return guard
        try:
            for record in self.profile.files:
                destination = self._staging_path(record)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src / self.profile_key / record.path, destination)
            for record in self.profile.shared:
                destination = self._shared_staging_path(record)
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src / SHARED_DIR_NAME / record.path, destination)
            (self._staging_dir() / "install.json").write_text(
                json.dumps(self._metadata(), sort_keys=True),
                encoding="utf-8",
            )
        except OSError as exc:
            safe_remove(self._staging_dir())
            return self._status("failed", error=f"could not stage offline pack: {exc}")
        on_progress(self._status("validating", installed_bytes=self._total_bytes))
        return self._promote_staging()


def installed_profiles(
    root: Path,
    manifest: QwenModelManifest | None = None,
) -> tuple[str, ...]:
    """Profiles with a ready install under ``root`` (shared files included)."""
    manifest = manifest if manifest is not None else MANIFEST
    if manifest is None:
        return ()
    ready: list[str] = []
    for key in PROFILE_KEYS:
        profile = manifest.profile_for(key)
        if profile is None:
            continue
        if QwenModelManager(root, key, profile).inspect().state == "ready":
            ready.append(key)
    return tuple(ready)


def shared_is_used(root: Path, manifest: QwenModelManifest | None = None) -> bool:
    """True while any installed profile still needs the shared tree."""
    return bool(installed_profiles(root, manifest))


def remove_shared_files(root: Path) -> None:
    """Delete the shared tree; callers must check :func:`shared_is_used` first."""
    safe_remove(Path(root) / SHARED_DIR_NAME)


def profile_bytes(profile: QwenModelProfile, *, shared: bool = True) -> Mapping[str, int]:
    """Byte breakdown used by the install UI."""
    return {
        "own": profile.own_bytes,
        "shared": profile.shared_bytes if shared else 0,
        "total": profile.total_bytes if shared else profile.own_bytes,
    }
