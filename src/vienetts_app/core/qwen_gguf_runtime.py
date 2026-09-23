"""Checksum-verified, app-managed qwentts.cpp runtime pack installation.

The GGUF engine never loads its native libraries into the GUI process: they
run in an isolated host whose pack directory this manager promotes. A pack
is a flat directory of shared objects, SONAME symlinks, license notices and
``BUILD-INFO.json`` provenance — not a Python environment — so installation
stages verified files directly instead of extracting wheels, and promotes
only after every file and link matches the locked manifest.

Status inspection verifies the installed tree against the locked digests; it
never loads the library, so ``status()`` is safe on the GUI thread.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
import tarfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.error import HTTPError
from urllib.parse import quote
from urllib.request import build_opener

from vienetts_app.core.managed_install import (
    DownloadCancelled,
    InstallPromotionError,
    NoRedirectHandler,
    RuntimeWheel,
    download_wheel_archive,
    file_matches,
    promoted_install,
    safe_remove,
)
from vienetts_app.core.qwen_gguf_runtime_manifest import (
    PackFile,
    QwenGgufRuntimePack,
    host_cell_key,
    validate_pack,
)

_DOWNLOAD_TIMEOUT_SECONDS = 30
_COPY_CHUNK = 1024 * 1024

_ARCHIVE_SUFFIXES = (".zip", ".tar.gz", ".tgz", ".tar")


class _PackArchive:
    """Read-only member view of a pack archive (zip or tar).

    Members are indexed by their normalized pack-relative path; a single
    shared top-level directory (an archive of ``pack/`` itself) is folded
    away so ``pack/libqwen.so`` and ``libqwen.so`` both resolve. Archive
    links are never trusted — only regular file members are readable, and
    the installer recreates every declared symlink itself.
    """

    def __init__(self, path: Path) -> None:
        name = path.name.lower()
        if name.endswith(".zip"):
            self._zip: zipfile.ZipFile | None = zipfile.ZipFile(path)
            self._tar = None
            raw = [(info.filename, info) for info in self._zip.infolist() if not info.is_dir()]
        elif name.endswith((".tar.gz", ".tgz", ".tar")):
            self._zip = None
            self._tar = tarfile.open(path)  # noqa: SIM115 - closed via close()
            raw = [(member.name, member) for member in self._tar.getmembers() if member.isreg()]
        else:
            raise ValueError(f"unsupported pack archive: {path.name}")
        self._members = self._index(raw)

    @staticmethod
    def _index(raw: list[tuple[str, object]]) -> dict[str, object]:
        cleaned = {
            PurePosixPath(name).as_posix().removeprefix("./"): member for name, member in raw
        }
        # A single shared top-level directory is a wrapper, not pack content.
        first_parts = {name.split("/", 1)[0] for name in cleaned}
        if len(first_parts) == 1 and all("/" in name for name in cleaned):
            prefix = first_parts.pop() + "/"
            cleaned = {name[len(prefix) :]: m for name, m in cleaned.items()}
        return cleaned

    def extract(self, record: PackFile, target: Path) -> bool:
        """Write member ``record.path`` to ``target``; False when absent."""
        member = self._members.get(record.path)
        if member is None:
            return False
        target.parent.mkdir(parents=True, exist_ok=True)
        if self._zip is not None:
            with self._zip.open(member) as source, target.open("wb") as destination:  # type: ignore[arg-type]
                shutil.copyfileobj(source, destination, _COPY_CHUNK)
        else:
            source = self._tar.extractfile(member)  # type: ignore[union-attr,arg-type]
            if source is None:
                return False
            with source, target.open("wb") as destination:
                shutil.copyfileobj(source, destination, _COPY_CHUNK)
        return True

    def close(self) -> None:
        for archive in (self._zip, self._tar):
            if archive is not None:
                archive.close()


@dataclass(frozen=True)
class QwenGgufRuntimeLocation:
    """A promoted pack: where the host finds the library it will load."""

    root: Path
    library_path: Path
    format_version: str
    cell: str
    device: str
    abi_version: int
    runtime_identity: str
    backends: tuple[str, ...]
    dependencies: tuple[str, ...]
    deployment_floor: str


@dataclass(frozen=True)
class QwenGgufRuntimeStatus:
    state: str
    cell: str = ""
    installed_bytes: int = 0
    required_bytes: int = 0
    progress: float = 0.0
    error: str = ""
    location: QwenGgufRuntimeLocation | None = None

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    @property
    def runtime_identity(self) -> str:
        return self.location.runtime_identity if self.location is not None else ""

    @property
    def library_path(self) -> Path | None:
        return self.location.library_path if self.location is not None else None


class ManagedQwenGgufRuntimeError(RuntimeError):
    """The managed qwentts.cpp runtime pack is unavailable or corrupt."""


class QwenGgufRuntimeManager:
    """Stage, verify, and atomically promote one platform cell's pack.

    Promotion has one gate — every declared file must match its locked size
    and digest and every declared link must resolve to its locked target —
    evaluated on the staged tree and again on the promoted one. A failing
    promotion restores the previous install; a cancelled one leaves verified
    staged bytes on disk so the next attempt resumes instead of restarting.

    ``verify_install`` is the Phase 4 seam for a host-level load check: the
    native library is only ever exercised by the isolated host, never in this
    process, so the default promotion gate is the digest-verified tree.
    """

    def __init__(
        self,
        root: Path,
        pack: QwenGgufRuntimePack,
        downloader: Callable[[str, Path], None] | None = None,
        disk_usage: Callable[[Path], object] = shutil.disk_usage,
        opener: Callable[..., object] | None = None,
        verify_install: Callable[[Path], tuple[bool, str]] | None = None,
    ) -> None:
        # The manifest loader validates shipped data; packs built in code go
        # through the same contract here so neither path can bypass it.
        validate_pack(pack)
        self.root = Path(root)
        self.pack = pack
        self._downloader = downloader
        self._disk_usage = disk_usage
        self._opener = opener if opener is not None else build_opener(NoRedirectHandler()).open
        self._verify_install = verify_install

    def _active_dir(self) -> Path:
        # Cells coexist on one host: a CUDA machine keeps the CPU pack too.
        return self.root / self.pack.cell / self.pack.format_version

    def _staging_dir(self) -> Path:
        return self.root / ".staging" / self.pack.cell / self.pack.format_version

    def _previous_dir(self) -> Path:
        return self.root / self.pack.cell / f"{self.pack.format_version}.previous"

    def _staging_directories(self) -> tuple[Path, Path, Path]:
        return (
            self.root / ".staging",
            self.root / ".staging" / self.pack.cell,
            self._staging_dir(),
        )

    def _prepare_staging(self) -> Path:
        root = self.root.resolve()
        for directory in self._staging_directories():
            if directory.is_symlink():
                raise OSError("staging directory must not be a symlink")
            directory.mkdir(parents=True, exist_ok=True)
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

    def _location(self, root: Path) -> QwenGgufRuntimeLocation:
        return QwenGgufRuntimeLocation(
            root,
            root / self.pack.library,
            self.pack.format_version,
            self.pack.cell,
            self.pack.device,
            self.pack.abi_version,
            self.pack.identity,
            self.pack.backends,
            self.pack.dependencies,
            self.pack.deployment_floor,
        )

    @property
    def _required_free_bytes(self) -> int:
        # Staging and the previous install coexist during promotion.
        return self.pack.total_bytes * 2

    def _status(
        self,
        state: str,
        *,
        installed_bytes: int = 0,
        error: str = "",
        location: QwenGgufRuntimeLocation | None = None,
    ) -> QwenGgufRuntimeStatus:
        total = self.pack.total_bytes
        return QwenGgufRuntimeStatus(
            state=state,
            cell=self.pack.cell,
            installed_bytes=installed_bytes,
            required_bytes=self._required_free_bytes,
            progress=installed_bytes / total if total else 1.0,
            error=error,
            location=location,
        )

    def _metadata(self) -> dict[str, object]:
        return {
            "format": self.pack.format_version,
            "cell": self.pack.cell,
            "identity": self.pack.identity,
            "library": self.pack.library,
            "abi": self.pack.abi_version,
            "files": {record.path: record.sha256 for record in self.pack.files},
            "links": {link.path: link.target for link in self.pack.links},
        }

    def _tree_verifies(self, root: Path) -> str:
        """ "" when every member matches the manifest, else the failing path."""
        for record in self.pack.files:
            path = root / record.path
            if path.is_symlink() or not file_matches(path, record.size_bytes, record.sha256):
                return record.path
        for link in self.pack.links:
            path = root / link.path
            if not path.is_symlink() or os.readlink(path) != link.target:
                return link.path
        return ""

    def status(self) -> QwenGgufRuntimeStatus:
        """Report ready only for a tree whose every member matches the lock.

        Verification is file digests and link targets — the library is never
        loaded here, so this is safe to call on the GUI thread.
        """
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
        bad_member = self._tree_verifies(active)
        if bad_member:
            return self._status(
                "failed", error=f"installed pack does not match the manifest: {bad_member}"
            )
        return self._status(
            "ready",
            installed_bytes=self.pack.total_bytes,
            location=self._location(active),
        )

    inspect = status  # parity with QwenRuntimeManager consumers

    def cancel_staging(self) -> None:
        """Discard staged files and links; the active install is untouched."""
        safe_remove(self._staging_dir())
        for parent in self._staging_directories()[:2]:
            with contextlib.suppress(OSError):
                parent.rmdir()  # prune now-empty parents, never files

    def remove(self, *, in_use: bool = False) -> QwenGgufRuntimeStatus:
        """Remove the runtime only while no host process may have it loaded."""
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

    def _install_guard(self) -> QwenGgufRuntimeStatus | None:
        """Preflight that must pass before any byte is staged.

        The pack must be this host's own cell — a Windows pack staged on
        Linux would verify perfectly and then fail every load, so the
        platform guard refuses it before any disk or network work.
        """
        host_cell = host_cell_key(self.pack.device)
        if host_cell != self.pack.cell:
            return self._status(
                "failed",
                error=(
                    f"pack targets {self.pack.cell} but this host's cell is "
                    f"{host_cell or 'unsupported'}; refusing a runtime that cannot load"
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

    def _staged_path(self, path: str) -> Path:
        return self._staging_dir() / path

    def _staged_file_valid(self, path: str, size_bytes: int, sha256: str) -> bool:
        staged = self._staged_path(path)
        return not staged.is_symlink() and file_matches(staged, size_bytes, sha256)

    def _link_terminal_record(self, path: str) -> PackFile | None:
        """The file record a declared link chain ends at (None = not a link)."""
        by_path = {link.path: link.target for link in self.pack.links}
        files = {record.path: record for record in self.pack.files}
        target = path
        visited: set[str] = set()
        while target in by_path:
            if target in visited:
                return None
            visited.add(target)
            target = by_path[target]
        return files.get(target)

    def _download_file(
        self,
        record: PackFile,
        cancelled: Callable[[], bool],
        on_bytes: Callable[[int], None] | None = None,
    ) -> str:
        """Fetch one pack file into staging; "" on success, else the error."""
        target = self._staged_path(record.path)
        if target.is_symlink():
            target.unlink()
        filename = PurePosixPath(record.path).name
        downloader = self._downloader
        last_error = f"no published runtime pack for {self.pack.cell}"
        for base_url in self.pack.downloads:
            url = f"{base_url.rstrip('/')}/{quote(record.path, safe='/')}"
            wheel = RuntimeWheel(filename, url, record.size_bytes, record.sha256)
            try:
                download_wheel_archive(
                    wheel,
                    target,
                    cancelled=cancelled,
                    opener=self._opener,
                    downloader=(None if downloader is None else lambda w, t: downloader(w.url, t)),
                    timeout=_DOWNLOAD_TIMEOUT_SECONDS,
                    on_bytes=on_bytes,
                )
            except DownloadCancelled:
                raise
            except (HTTPError, OSError, ValueError) as exc:
                last_error = f"download failed: {exc}"
                continue
            if file_matches(target, record.size_bytes, record.sha256):
                return ""
            last_error = f"checksum mismatch: {record.path}"
        return last_error

    def _stage_files(
        self,
        cancelled: Callable[[], bool],
        on_progress: Callable[[QwenGgufRuntimeStatus], None],
        pack_dir: Path | None,
        archive: _PackArchive | None = None,
        *,
        allow_downloads: bool,
    ) -> tuple[int, QwenGgufRuntimeStatus | None]:
        """Ensure every manifest file is present and verified in staging.

        Staged bytes are kept; a valid file in the active install seeds a
        repair for free; an offline pack (dir or archive) is the next source;
        downloads are last and only when the caller allowed them.
        """
        verified_bytes = 0
        active = self._active_dir()
        for record in self.pack.files:
            if cancelled():
                return verified_bytes, self._status("unavailable", installed_bytes=verified_bytes)
            if not self._staged_file_valid(record.path, record.size_bytes, record.sha256):
                staged = self._staged_path(record.path)
                staged.parent.mkdir(parents=True, exist_ok=True)
                active_source = active / record.path
                pack_source = pack_dir / record.path if pack_dir is not None else None
                if not active_source.is_symlink() and file_matches(
                    active_source, record.size_bytes, record.sha256
                ):
                    shutil.copyfile(active_source, staged)
                elif pack_source is not None:
                    if not pack_source.exists():
                        return verified_bytes, self._status(
                            "failed",
                            installed_bytes=verified_bytes,
                            error=f"offline pack is missing {record.path}",
                        )
                    if pack_source.is_symlink():
                        return verified_bytes, self._status(
                            "failed",
                            installed_bytes=verified_bytes,
                            error=f"offline pack file must not be a symlink: {record.path}",
                        )
                    if not file_matches(pack_source, record.size_bytes, record.sha256):
                        return verified_bytes, self._status(
                            "failed",
                            installed_bytes=verified_bytes,
                            error=f"offline pack file does not match the manifest: {record.path}",
                        )
                    shutil.copyfile(pack_source, staged)
                elif archive is not None:
                    try:
                        found = archive.extract(record, staged)
                    except (OSError, tarfile.TarError, zipfile.BadZipFile) as exc:
                        return verified_bytes, self._status(
                            "failed",
                            installed_bytes=verified_bytes,
                            error=f"offline pack archive is unreadable: {exc}",
                        )
                    if not found:
                        return verified_bytes, self._status(
                            "failed",
                            installed_bytes=verified_bytes,
                            error=f"offline pack is missing {record.path}",
                        )
                    if not file_matches(staged, record.size_bytes, record.sha256):
                        staged.unlink(missing_ok=True)
                        return verified_bytes, self._status(
                            "failed",
                            installed_bytes=verified_bytes,
                            error=f"offline pack file does not match the manifest: {record.path}",
                        )
                elif allow_downloads:
                    try:
                        error = self._download_file(
                            record,
                            cancelled,
                            on_bytes=lambda n, base=verified_bytes: on_progress(
                                self._status("downloading", installed_bytes=base + n)
                            ),
                        )
                    except DownloadCancelled:
                        return verified_bytes, self._status(
                            "unavailable", installed_bytes=verified_bytes
                        )
                    if error:
                        return verified_bytes, self._status(
                            "failed", installed_bytes=verified_bytes, error=error
                        )
                else:
                    return verified_bytes, self._status(
                        "failed",
                        installed_bytes=verified_bytes,
                        error=(
                            f"cannot stage {record.path}: no offline pack supplied and "
                            f"no published runtime pack for {self.pack.cell}"
                        ),
                    )
            verified_bytes += record.size_bytes
            on_progress(self._status("downloading", installed_bytes=verified_bytes))
        return verified_bytes, None

    def _finalize_staging(
        self,
        verified_bytes: int,
        cancelled: Callable[[], bool],
    ) -> QwenGgufRuntimeStatus | None:
        """Create the declared links and metadata inside the staged tree."""
        staging = self._staging_dir()
        try:
            for link in self.pack.links:
                if cancelled():
                    return self._status("unavailable", installed_bytes=verified_bytes)
                link_path = staging / link.path
                if link_path.is_symlink() and os.readlink(link_path) == link.target:
                    continue
                safe_remove(link_path)
                link_path.parent.mkdir(parents=True, exist_ok=True)
                os.symlink(link.target, link_path)
            (staging / "install.json").write_text(
                json.dumps(self._metadata(), sort_keys=True),
                encoding="utf-8",
            )
        except OSError as exc:
            return self._status("failed", installed_bytes=verified_bytes, error=str(exc))
        bad_member = self._tree_verifies(staging)
        if bad_member:
            return self._status(
                "failed",
                installed_bytes=verified_bytes,
                error=f"staged pack does not match the manifest: {bad_member}",
            )
        return None

    def _promote_staging(self) -> QwenGgufRuntimeStatus:
        staging = self._staging_dir()
        active = self._active_dir()
        previous = self._previous_dir()
        if not self._staging_is_safe():
            return self._status("failed", error="promotion failed: staging directory is unsafe")
        active.parent.mkdir(parents=True, exist_ok=True)
        if not active.exists() and previous.exists():
            with contextlib.suppress(OSError):
                os.replace(previous, active)
        try:
            with promoted_install(
                staging,
                active,
                previous,
                active_usable=lambda: self.status().ready,
            ):
                status = self.status()
                if not status.ready:
                    raise InstallPromotionError(status.error or "promotion failed")
                if self._verify_install is not None:
                    ok, detail = self._verify_install(active)
                    if not ok:
                        raise InstallPromotionError(
                            detail or "the promoted pack failed its install check"
                        )
        except InstallPromotionError as exc:
            return self._status("failed", error=str(exc))
        except OSError as exc:
            return self._status("failed", error=f"promotion failed: {exc}")
        return status

    def install_online(
        self,
        cancelled: Callable[[], bool] = lambda: False,
        on_progress: Callable[[QwenGgufRuntimeStatus], None] = lambda _status: None,
    ) -> QwenGgufRuntimeStatus:
        """Download, verify, and promote the published pack for this cell.

        Cells ship without download URLs until publication is authorized —
        that is reported as an honest failure, never a pretend install.
        """
        existing = self.status()
        if existing.ready:
            return existing
        if not self.pack.downloads:
            return self._status(
                "failed",
                error=(
                    f"no published runtime pack for {self.pack.cell}; "
                    "install from an offline pack instead"
                ),
            )
        guard = self._install_guard()
        if guard is not None:
            return guard
        verified_bytes, failure = self._stage_files(
            cancelled, on_progress, None, allow_downloads=True
        )
        if failure is not None:
            return failure
        failure = self._finalize_staging(verified_bytes, cancelled)
        if failure is not None:
            return failure
        return self._promote_staging()

    def install_from_offline_pack(
        self,
        pack_dir: Path,
        cancelled: Callable[[], bool] = lambda: False,
        on_progress: Callable[[QwenGgufRuntimeStatus], None] = lambda _status: None,
    ) -> QwenGgufRuntimeStatus:
        """Install from a pre-staged pack directory or archive (no network).

        ``pack_dir`` may be a staged pack directory, or a ``.zip``/``.tar``/
        ``.tar.gz`` archive of one (what a CI artifact download produces).
        Only manifest-declared files are read; every byte is verified against
        the lock before it is staged, so an incompatible or hand-edited pack
        cannot promote.
        """
        existing = self.status()
        if existing.ready:
            return existing
        guard = self._install_guard()
        if guard is not None:
            return guard
        source, failure = self._open_offline_source(Path(pack_dir))
        if failure is not None:
            return failure
        archive: _PackArchive | None = None
        directory: Path | None = None
        try:
            if isinstance(source, _PackArchive):
                archive = source
            else:
                directory = source
                # Directory links verify against the resolved file digest, so
                # a pack that materialized them as copies is as acceptable as
                # real symlinks.
                for link in self.pack.links:
                    record = self._link_terminal_record(link.path)
                    if record is not None and not file_matches(
                        directory / link.path, record.size_bytes, record.sha256
                    ):
                        return self._status(
                            "failed",
                            error=(f"offline pack link does not match the manifest: {link.path}"),
                        )
            verified_bytes, failure = self._stage_files(
                cancelled, on_progress, directory, archive, allow_downloads=False
            )
            if failure is not None:
                return failure
            failure = self._finalize_staging(verified_bytes, cancelled)
            if failure is not None:
                return failure
            return self._promote_staging()
        finally:
            if archive is not None:
                archive.close()

    def _open_offline_source(
        self, source: Path
    ) -> tuple[Path | _PackArchive | None, QwenGgufRuntimeStatus | None]:
        """Resolve an offline pack path to a directory or an opened archive."""
        if source.is_symlink():
            return None, self._status(
                "failed", error=f"offline pack must not be a symlink: {source}"
            )
        if source.is_dir():
            return source, None
        if source.is_file() and source.name.lower().endswith(_ARCHIVE_SUFFIXES):
            try:
                return _PackArchive(source), None
            except (OSError, ValueError, tarfile.TarError, zipfile.BadZipFile) as exc:
                return None, self._status(
                    "failed", error=f"offline pack archive is unreadable: {exc}"
                )
        return None, self._status(
            "failed", error=f"offline pack is not a directory or pack archive: {source}"
        )

    def repair(
        self,
        cancelled: Callable[[], bool] = lambda: False,
        on_progress: Callable[[QwenGgufRuntimeStatus], None] = lambda _status: None,
        pack_dir: Path | None = None,
    ) -> QwenGgufRuntimeStatus:
        """Re-install a failed pack, reusing every member that still verifies.

        Sources are tried cheapest first: staged files, then the current
        active install (a repair only replaces what is corrupt), then the
        offline pack directory or published downloads.
        """
        existing = self.status()
        if existing.ready:
            if self._verify_install is None:
                return existing
            ok, _detail = self._verify_install(self._active_dir())
            if ok:
                return existing
        guard = self._install_guard()
        if guard is not None:
            return guard
        verified_bytes, failure = self._stage_files(
            cancelled,
            on_progress,
            pack_dir,
            allow_downloads=bool(self.pack.downloads),
        )
        if failure is not None:
            return failure
        failure = self._finalize_staging(verified_bytes, cancelled)
        if failure is not None:
            return failure
        return self._promote_staging()
