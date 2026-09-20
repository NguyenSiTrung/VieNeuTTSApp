"""Shared managed-install primitives (Phase 2 Task 1).

The VieNeu model installer and the managed CUDA runtime grew the same
low-level machinery independently: size/SHA-256 verification, Windows path
handling, free-space preflight, staging → active promotion with rollback,
retry-bounded removal, HTTPS-only download policy and HTTP range validation.
This module owns those primitives once so the Qwen runtime/model managers can
reuse them instead of copying them a third time.

Nothing here imports Qt, torch, or huggingface_hub.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import shutil
import stat
import time
import zipfile
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request

CHUNK_SIZE = 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 30

# Hosts a managed download may come from. Both are immutable artifact hosts
# used by the pinned manifests; anything else is rejected at manifest time.
ALLOWED_DOWNLOAD_HOSTS = frozenset({"download.pytorch.org", "files.pythonhosted.org"})

# Windows: a locked target makes os.replace/rmtree fail with ERROR_SHARING_
# VIOLATION or ERROR_ACCESS_DENIED. Retry briefly instead of failing the task.
REMOVAL_ATTEMPTS = 5
REMOVAL_RETRY_SECONDS = 0.2


class InstallPromotionError(RuntimeError):
    """Promotion of a staging directory failed; the previous install is intact."""


def sha256_of(path: Path) -> str:
    """Streaming SHA-256 of ``path`` (never loads the file into memory).

    The argument is used as given: re-wrapping it with ``Path(...)`` would pick
    the platform flavour from ``os.name`` at call time and break tests (and
    Windows-path fakes) that patch ``os.name``.
    """
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(CHUNK_SIZE), b""):
            digest.update(block)
    return digest.hexdigest()


def file_matches(path: Path, size_bytes: int, sha256: str) -> bool:
    """True when ``path`` exists with exactly ``size_bytes`` and ``sha256``.

    Missing files, wrong sizes, unreadable files and digest mismatches all
    report False — the caller treats every case as "download it again".
    """
    try:
        if not path.is_file():
            return False
        if path.stat().st_size != size_bytes:
            return False
    except OSError:
        return False
    try:
        return sha256_of(path) == sha256
    except OSError:
        return False


def normalize_windows_path(value: str) -> str:
    """Strip the extended-length prefix so two Windows spellings compare equal."""
    value = value.replace("/", "\\")
    if value.startswith("\\\\?\\UNC\\"):
        return "\\\\" + value[8:]
    if value.startswith("\\\\?\\"):
        return value[4:]
    return value


def is_same_file(first: Path, second: Path) -> bool:
    """True when both paths denote the same file, including Windows spellings."""
    if first == second:
        return True
    s1 = str(first)
    s2 = str(second)
    if (
        os.name == "nt"
        or "\\" in s1
        or "\\" in s2
        or s1.startswith("\\\\?\\")
        or s2.startswith("\\\\?\\")
    ) and normalize_windows_path(s1).lower() == normalize_windows_path(s2).lower():
        return True
    with contextlib.suppress(OSError):
        if first.resolve() == second.resolve():
            return True
    with contextlib.suppress(OSError):
        if first.is_file() and second.is_file() and os.path.samefile(first, second):
            return True
    return False


def free_space_bytes(
    path: Path,
    disk_usage: Callable[[Path], object] = shutil.disk_usage,
) -> int | None:
    """Free bytes on the volume holding ``path``; None when unmeasurable."""
    try:
        usage = disk_usage(path)
        return int(usage.free)  # type: ignore[attr-defined]
    except (OSError, TypeError, ValueError, AttributeError):
        return None


def safe_remove(path: Path) -> None:
    """Remove a file, symlink or directory tree, retrying briefly on Windows.

    A locked target is retried (``REMOVAL_ATTEMPTS``) before giving up; the
    final failure is swallowed because every caller treats removal as
    best-effort cleanup of a previous/staging install.
    """
    for attempt in range(REMOVAL_ATTEMPTS):
        try:
            if path.is_symlink() or path.is_file():
                path.unlink(missing_ok=True)
            elif path.exists():
                shutil.rmtree(path)
            return
        except OSError:
            if attempt + 1 == REMOVAL_ATTEMPTS:
                return
            time.sleep(REMOVAL_RETRY_SECONDS)


@contextmanager
def promoted_install(
    staging: Path,
    active: Path,
    previous: Path,
    *,
    active_usable: Callable[[], bool] = lambda: True,
) -> Iterator[None]:
    """Atomically swap ``staging`` into ``active``, restoring on any failure.

    ``active`` is first moved aside to ``previous`` when it exists and
    ``active_usable()`` reports it is worth keeping; an unusable active install
    is discarded. The body runs with the new install in place: raise
    :class:`InstallPromotionError` (or anything else) to roll back, or return
    normally to drop the previous install.
    """
    safe_remove(previous)
    had_previous = False
    if active.exists():
        if active_usable():
            try:
                os.replace(active, previous)
            except OSError as exc:
                raise InstallPromotionError(f"promotion failed: {exc}") from exc
            had_previous = True
        else:
            safe_remove(active)
    try:
        os.replace(staging, active)
    except OSError as exc:
        if had_previous and not active.exists() and previous.exists():
            with contextlib.suppress(OSError):
                os.replace(previous, active)
        raise InstallPromotionError(f"promotion failed: {exc}") from exc
    try:
        yield
    except BaseException:
        safe_remove(active)
        if had_previous:
            with contextlib.suppress(OSError):
                os.replace(previous, active)
        raise
    if had_previous:
        safe_remove(previous)


def check_download_url(
    url: str,
    *,
    allowed_hosts: frozenset[str] = ALLOWED_DOWNLOAD_HOSTS,
    required_path_prefixes: tuple[str, ...] = (),
    filename: str | None = None,
) -> None:
    """Reject anything that is not a direct HTTPS artifact from an allowed host.

    Used at manifest build time: a wrong host, a non-HTTPS scheme, query or
    fragment parameters, a path that is not the wheel itself, or a missing
    path prefix all raise with the offending URL in the message.
    """
    parsed = urlsplit(url)
    if parsed.scheme != "https":
        raise ValueError(f"download URL must use HTTPS: {url}")
    if parsed.hostname not in allowed_hosts:
        raise ValueError(f"download URL host is unsupported: {url}")
    if parsed.query or parsed.fragment:
        raise ValueError(f"download URL must be a direct artifact: {url}")
    if filename is not None:
        if Path(unquote(parsed.path)).name != filename:
            raise ValueError(f"download URL must point at {filename}: {url}")
    elif not unquote(parsed.path).rsplit("/", 1)[-1]:
        raise ValueError(f"download URL must be a direct artifact: {url}")
    if required_path_prefixes and not parsed.path.startswith(required_path_prefixes):
        raise ValueError(f"download URL must be a pinned artifact: {url}")


class NoRedirectHandler(HTTPRedirectHandler):
    """Turn redirects into HTTP errors rather than following a new URL."""

    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


def response_status(response: object) -> int:
    """HTTP status of a response-like object (0 when unknown)."""
    status = getattr(response, "status", None)
    if isinstance(status, int):
        return status
    getcode = getattr(response, "getcode", None)
    if callable(getcode):
        return int(getcode())
    return 0


def response_url(response: object) -> str | None:
    """Final URL of a response-like object (None when unknown)."""
    geturl = getattr(response, "geturl", None)
    if callable(geturl):
        value = geturl()
        return str(value) if value is not None else None
    value = getattr(response, "url", None)
    return str(value) if value is not None else None


def response_header(response: object, name: str) -> str | None:
    """Header value from a response-like object (None when absent)."""
    headers = getattr(response, "headers", None)
    getter = getattr(headers, "get", None)
    value = getter(name) if callable(getter) else None
    return str(value) if value is not None else None


def range_is_honored(response: object, *, offset: int, size_bytes: int, expected_url: str) -> bool:
    """True only for a correct ``206`` response resuming at ``offset``.

    A server that ignores ``Range`` (200), reports a different total size, or
    answers from a redirected URL is not resumed against — the caller restarts
    the download instead of appending to a possibly different artifact.
    """
    if response_url(response) != expected_url:
        return False
    content_range = response_header(response, "Content-Range")
    if response_status(response) != 206 or content_range is None:
        return False
    try:
        unit, values = content_range.split(" ", 1)
        byte_range, total = values.split("/", 1)
        start, end = (int(value) for value in byte_range.split("-", 1))
    except ValueError:
        return False
    return unit.lower() == "bytes" and start == offset and end >= start and total == str(size_bytes)


def stream_to_file(
    response: object,
    target: Path,
    *,
    mode: str,
    cancelled: Callable[[], bool],
    maximum_bytes: int,
    on_bytes: Callable[[int], None] | None = None,
) -> None:
    """Copy a response body to ``target`` in bounded chunks.

    Raises :class:`DownloadCancelled` when ``cancelled()`` flips mid-stream and
    ``OSError`` when the body would exceed the manifest size (a lying server
    must not fill the disk). Partial bytes stay on disk so a retry resumes.
    """
    reader = getattr(response, "read", None)
    if not callable(reader):
        raise OSError("download response is not readable")
    current_bytes = target.stat().st_size if mode == "ab" and target.exists() else 0
    with target.open(mode) as destination:
        while True:
            if cancelled():
                raise DownloadCancelled
            chunk = reader(CHUNK_SIZE)
            if not chunk:
                return
            if len(chunk) > maximum_bytes - current_bytes:
                raise OSError("download response exceeds declared artifact size")
            destination.write(chunk)
            current_bytes += len(chunk)
            if on_bytes is not None:
                on_bytes(current_bytes)
            if cancelled():
                raise DownloadCancelled


class DownloadCancelled(Exception):
    """Control flow for a cancelled streaming download."""


def is_safe_archive_member(name: str, mode: int) -> bool:
    """Reject absolute paths, drive letters, traversal, and symlink members."""
    cleaned = name.replace("\\", "/")
    if not cleaned or cleaned.startswith("/") or (len(cleaned) > 1 and cleaned[1] == ":"):
        return False
    parts = [part for part in cleaned.split("/") if part]
    if any(part == ".." for part in parts):
        return False
    return not stat.S_ISLNK(mode)


@dataclass(frozen=True)
class RuntimeWheel:
    """One pinned wheel artifact: what to download and how to verify it."""

    filename: str
    url: str
    size_bytes: int
    sha256: str


def wheel_member_destination(root: Path, member: zipfile.ZipInfo) -> tuple[Path, bool]:
    """Map a wheel member to a path inside ``root`` or reject it as unsafe."""
    name = member.filename.replace("\\", "/")
    if not is_safe_archive_member(name, member.external_attr >> 16):
        raise ValueError(f"unsafe wheel member: {member.filename}")
    destination = (root / Path(*PurePosixPath(name).parts)).resolve()
    if not destination.is_relative_to(root.resolve()):
        raise ValueError(f"unsafe wheel member: {member.filename}")
    return destination, member.is_dir()


def validate_wheel_layout(
    archive: zipfile.ZipFile,
    root: Path,
    claimed_outputs: dict[Path, bool],
) -> tuple[list[tuple[zipfile.ZipInfo, Path]], dict[Path, bool]]:
    """Validate every member before any of them is written to disk.

    Two wheels in one runtime must not silently overwrite each other: a file
    that is already claimed, or that would be replaced by a directory (or the
    other way round), rejects the whole archive.
    """
    outputs = dict(claimed_outputs)
    validated: list[tuple[zipfile.ZipInfo, Path]] = []
    for member in archive.infolist():
        destination, is_directory = wheel_member_destination(root, member)
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


def extract_wheel_archive(
    filename: str,
    archive_path: Path,
    site_packages: Path,
    claimed_outputs: dict[Path, bool],
) -> None:
    """Expand a verified wheel into ``site_packages`` without unsafe writes.

    A rejected archive is deleted: it must never be reused for a retry.
    """
    try:
        with zipfile.ZipFile(archive_path) as archive:
            validated, validated_outputs = validate_wheel_layout(
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
                    shutil.copyfileobj(source, output, CHUNK_SIZE)
            claimed_outputs.clear()
            claimed_outputs.update(validated_outputs)
    except (OSError, ValueError, zipfile.BadZipFile) as exc:
        archive_path.unlink(missing_ok=True)
        raise OSError(str(exc)) from exc


def download_wheel_archive(
    wheel: RuntimeWheel,
    target: Path,
    *,
    cancelled: Callable[[], bool],
    opener: Callable[..., object],
    downloader: Callable[[RuntimeWheel, Path], None] | None = None,
    timeout: float = DOWNLOAD_TIMEOUT_SECONDS,
    on_bytes: Callable[[int], None] | None = None,
) -> None:
    """Download one pinned artifact, resuming a partial ``target`` when safe.

    ``downloader`` replaces the HTTP path (tests, offline mirrors). Otherwise a
    ``Range`` request resumes, and any response that is not a verified ``206``
    for the manifest URL restarts the download from zero — an ignored range or
    a redirect must never be appended to.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if downloader is not None:
        downloader(wheel, target)
        return

    try:
        offset = target.stat().st_size
    except OSError:
        offset = 0
    if offset >= wheel.size_bytes:
        target.unlink(missing_ok=True)
        offset = 0

    headers = {"Range": f"bytes={offset}-"} if offset else {}
    with opener(Request(wheel.url, headers=headers), timeout=timeout) as response:
        if response_url(response) != wheel.url:
            raise OSError("download response redirected away from the manifest URL")
        if offset and range_is_honored(
            response,
            offset=offset,
            size_bytes=wheel.size_bytes,
            expected_url=wheel.url,
        ):
            stream_to_file(
                response,
                target,
                mode="ab",
                cancelled=cancelled,
                maximum_bytes=wheel.size_bytes,
                on_bytes=on_bytes,
            )
            return
        if response_status(response) == 200:
            stream_to_file(
                response,
                target,
                mode="wb",
                cancelled=cancelled,
                maximum_bytes=wheel.size_bytes,
                on_bytes=on_bytes,
            )
            return
    # A malformed 206 response cannot safely be treated as a full download.
    with opener(Request(wheel.url), timeout=timeout) as response:
        if response_url(response) != wheel.url or response_status(response) != 200:
            raise OSError("server did not honor a safe full-download restart")
        stream_to_file(
            response,
            target,
            mode="wb",
            cancelled=cancelled,
            maximum_bytes=wheel.size_bytes,
            on_bytes=on_bytes,
        )
