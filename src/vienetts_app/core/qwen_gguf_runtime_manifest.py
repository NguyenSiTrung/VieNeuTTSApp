"""Pinned, checksum-verified qwentts.cpp native runtime pack manifests.

The GGUF engine runs in an isolated native host whose libraries come from a
managed pack: a flat directory of shared objects, SONAME symlinks, license
notices and ``BUILD-INFO.json`` provenance, built by
``scripts/build_qwen_gguf_runtime.py`` and locked into
``packaging/qwen-gguf-pack-manifests.json``. The shipped data file next to
this module carries the verified subset of that lock plus the install-time
contract (backend inventory, declared OS dependencies, download locations).

Only cells that were actually built, probed, and locked ship an install
recipe — a missing cell reports "no managed runtime" instead of guessing at
a pack that was never verified. Download URLs are likewise only present once
publication is explicitly authorized; an empty list is a truthful "offline
pack only" cell, not a schema gap.

A missing or invalid data file yields no manifests at all, and the loader
validates the data exactly as strictly as the wheel manifest does.
"""

from __future__ import annotations

import hashlib
import json
import platform
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from vienetts_app.core.managed_install import check_download_url

FORMAT_VERSION = "qwen-gguf-runtime-v1"
MANIFEST_PATH = Path(__file__).with_name("qwen_gguf_runtime_manifests.json")

#: The audited ``qwen.h`` ABI (ABI 5 is the minimum the host will load).
ABI_MIN_VERSION = 5

# cell -> (os, arch, device, abi library name). Mirrors
# packaging/qwen-gguf-runtime-requirements.json.
CELL_MATRIX: Mapping[str, tuple[str, str, str, str]] = {
    "windows-x64-cpu": ("windows", "x86_64", "cpu", "qwen.dll"),
    "windows-x64-cuda": ("windows", "x86_64", "cuda", "qwen.dll"),
    "linux-x64-cpu": ("linux", "x86_64", "cpu", "libqwen.so"),
    "linux-x64-cuda": ("linux", "x86_64", "cuda", "libqwen.so"),
    "macos-arm64-cpu": ("macos", "arm64", "cpu", "libqwen.dylib"),
    "macos-arm64-metal": ("macos", "arm64", "metal", "libqwen.dylib"),
}

DEVICES = frozenset({"cpu", "cuda", "metal"})

#: Artifact hosts a published pack may be served from. Publication is a
#: deferred release action; this allowlist exists so a future manifest can
#: pin release assets without relaxing the HTTPS/no-redirect policy.
PACK_DOWNLOAD_HOSTS = frozenset({"github.com", "objects.githubusercontent.com"})

_SHA256 = re.compile(r"[0-9a-f]{64}")
_COMMIT = re.compile(r"[0-9a-f]{40}")


@dataclass(frozen=True)
class PackFile:
    """One pinned file inside a pack: what to fetch and how to verify it."""

    path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class PackLink:
    """A declared symlink inside a pack (SONAME chains like libggml.so.0)."""

    path: str
    target: str


@dataclass(frozen=True)
class QwenGgufRuntimePack:
    """The install contract for one platform cell's native runtime pack."""

    cell: str
    device: str
    ggml_backend: str
    abi_version: int
    library: str
    upstream_repo: str
    upstream_commit: str
    ggml_commit: str
    deployment_floor: str
    backends: tuple[str, ...]
    dependencies: tuple[str, ...]
    files: tuple[PackFile, ...]
    links: tuple[PackLink, ...] = ()
    downloads: tuple[str, ...] = ()

    @property
    def format_version(self) -> str:
        return FORMAT_VERSION

    @property
    def total_bytes(self) -> int:
        return sum(record.size_bytes for record in self.files)

    @property
    def identity(self) -> str:
        """Content-addressed identity stamped into synthesis provenance.

        The upstream pins name the build recipe; the digest pins the exact
        file set — two packs built from the same commit with different bytes
        (a changed toolchain, a patched backend) must not share provenance.
        """
        digest = hashlib.sha256(
            json.dumps(
                {
                    "files": [[f.path, f.size_bytes, f.sha256] for f in self.files],
                    "links": [[link.path, link.target] for link in self.links],
                },
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()[:16]
        return (
            f"qwentts.cpp@{self.upstream_commit[:9]}"
            f"+ggml@{self.ggml_commit[:9]}"
            f":{self.cell}:{digest}"
        )


def _check_pack_path(value: str, cell: str) -> str:
    """Reject absolute paths, drive letters, separators and traversal."""
    cleaned = value.replace("\\", "/")
    parts = PurePosixPath(cleaned).parts
    if (
        not cleaned
        or cleaned.startswith("/")
        or (len(cleaned) > 1 and cleaned[1] == ":")
        or any(part == ".." for part in parts)
        or cleaned.endswith("/")
    ):
        raise ValueError(f"unsafe pack path for {cell}: {value!r}")
    return cleaned


def validate_pack(pack: QwenGgufRuntimePack) -> None:
    """Validate a pack object against the cell matrix and safety policy.

    Called at manifest load time *and* by the runtime manager, so a pack
    built in code (tests, a future caller) cannot bypass the contract.
    """
    _validate_pack(pack)


def _check_link_path(value: str, cell: str) -> str:
    try:
        return _check_pack_path(value, cell)
    except ValueError as exc:
        raise ValueError(f"invalid pack link for {cell}: {exc}") from exc


def _validate_pack(pack: QwenGgufRuntimePack) -> None:
    expected = CELL_MATRIX.get(pack.cell)
    if expected is None:
        raise ValueError(f"unsupported cell: {pack.cell}")
    _os, _arch, device, library = expected
    if pack.device != device:
        raise ValueError(f"cell device mismatch: {pack.cell} -> {pack.device}")
    if pack.abi_version < ABI_MIN_VERSION:
        raise ValueError(f"cell abi below the supported floor: {pack.cell}")
    if pack.library != library:
        raise ValueError(f"cell library mismatch: {pack.cell} -> {pack.library}")
    if not _COMMIT.fullmatch(pack.upstream_commit) or not _COMMIT.fullmatch(pack.ggml_commit):
        raise ValueError(f"upstream commits must be full 40-hex pins: {pack.cell}")
    if not pack.upstream_repo or not pack.ggml_backend or not pack.deployment_floor:
        raise ValueError(f"cell is missing required metadata: {pack.cell}")
    if not pack.backends or not all(pack.backends):
        raise ValueError(f"cell must declare its backend inventory: {pack.cell}")

    seen: set[str] = set()
    for record in pack.files:
        path = _check_pack_path(record.path, pack.cell)
        if path in seen:
            raise ValueError(f"duplicate pack file path for {pack.cell}: {path}")
        seen.add(path)
        if not _SHA256.fullmatch(record.sha256):
            raise ValueError(f"pack file sha256 is invalid for {pack.cell}: {path}")
        if (
            not isinstance(record.size_bytes, int)
            or isinstance(record.size_bytes, bool)
            or record.size_bytes <= 0
        ):
            raise ValueError(f"pack file size is invalid for {pack.cell}: {path}")
    if pack.library not in seen:
        raise ValueError(f"pack does not contain its ABI library {pack.library}: {pack.cell}")

    declared = set(seen)
    link_paths: set[str] = set()
    for link in pack.links:
        path = _check_link_path(link.path, pack.cell)
        if path in declared or path in link_paths:
            raise ValueError(f"link collides with a declared name for {pack.cell}: {path}")
        link_paths.add(path)
    for link in pack.links:
        target = _check_link_path(link.target, pack.cell)
        if target not in declared and target not in link_paths:
            raise ValueError(
                f"pack link target is not a declared pack member for {pack.cell}: "
                f"{link.path} -> {target}"
            )
    # Chained links (lib.so -> lib.so.0 -> lib.so.0.23.0) must terminate on a
    # real file: walk each chain to prove it, and catch cycles at the same time.
    by_path = {link.path: link.target for link in pack.links}
    for link in pack.links:
        visited = {link.path}
        target = link.target
        while target in by_path:
            if target in visited:
                raise ValueError(f"cyclic pack link for {pack.cell}: {link.path}")
            visited.add(target)
            target = by_path[target]
        if target not in declared:
            raise ValueError(f"pack link does not reach a file for {pack.cell}: {link.path}")

    for base_url in pack.downloads:
        check_download_url(
            base_url.rstrip("/") + "/x",
            allowed_hosts=PACK_DOWNLOAD_HOSTS,
            filename="x",
        )
    for dependency in pack.dependencies:
        if not dependency:
            raise ValueError(f"cell dependencies must be non-empty: {pack.cell}")


def load_manifests_from_data(data: Mapping[str, object]) -> dict[str, QwenGgufRuntimePack]:
    """Build and validate every pack recipe in a manifest data file."""
    if data.get("formatVersion") != FORMAT_VERSION:
        raise ValueError(f"unsupported manifest format: {data.get('formatVersion')!r}")
    cells = data.get("cells")
    if not isinstance(cells, Mapping) or not cells:
        raise ValueError("manifest data has no cells")
    packs: dict[str, QwenGgufRuntimePack] = {}
    for cell_key, entry in cells.items():
        cell = str(cell_key)
        if not isinstance(entry, Mapping):
            raise ValueError(f"manifest entry is not an object: {cell}")
        raw_files = entry.get("files")
        if not isinstance(raw_files, list) or not all(
            isinstance(record, Mapping) for record in raw_files
        ):
            raise ValueError(f"manifest files are malformed: {cell}")
        raw_links = entry.get("links", [])
        if not isinstance(raw_links, list) or not all(
            isinstance(record, Mapping) for record in raw_links
        ):
            raise ValueError(f"manifest links are malformed: {cell}")
        upstream = entry.get("upstream")
        if not isinstance(upstream, Mapping):
            raise ValueError(f"manifest upstream is not an object: {cell}")
        pack = QwenGgufRuntimePack(
            cell=str(entry.get("cell", "")),
            device=str(entry.get("device", "")),
            ggml_backend=str(entry.get("ggmlBackend", "")),
            abi_version=int(entry.get("abiVersion", 0)),
            library=str(entry.get("library", "")),
            upstream_repo=str(upstream.get("repo", "")),
            upstream_commit=str(upstream.get("commit", "")),
            ggml_commit=str(upstream.get("ggmlSubmoduleCommit", "")),
            deployment_floor=str(entry.get("deploymentFloor", "")),
            backends=tuple(str(name) for name in entry.get("backends", ())),
            dependencies=tuple(str(name) for name in entry.get("dependencies", ())),
            files=tuple(
                PackFile(
                    path=str(record["path"]),
                    size_bytes=int(record["size"]),
                    sha256=str(record["sha256"]),
                )
                for record in raw_files
            ),
            links=tuple(
                PackLink(path=str(record["path"]), target=str(record["target"]))
                for record in raw_links
            ),
            downloads=tuple(str(url) for url in entry.get("downloads", ())),
        )
        if pack.cell != cell:
            raise ValueError(f"cell key does not match its entry: {cell}")
        _validate_pack(pack)
        packs[cell] = pack
    return packs


def _load_shipped_manifests(path: Path = MANIFEST_PATH) -> dict[str, QwenGgufRuntimePack]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}
    if not isinstance(data, Mapping):
        return {}
    try:
        return load_manifests_from_data(data)
    except (ValueError, KeyError, TypeError):
        return {}


MANIFESTS: Mapping[str, QwenGgufRuntimePack] = _load_shipped_manifests()


def manifest_for_cell(cell: str) -> QwenGgufRuntimePack | None:
    """The verified pack recipe for a platform cell (``None`` = unshipped)."""
    return MANIFESTS.get(cell)


def host_cell_key(device: str) -> str | None:
    """The pack cell for this host on ``device`` (``None`` = unsupported).

    Only release platforms answer with a cell, so an arm64 Linux host or an
    Intel Mac reports "no managed runtime" instead of a key whose pack could
    never load. ``device`` uses the ggml vocabulary — ``cpu``, ``cuda``,
    ``metal`` — never the PyTorch ``mps`` spelling.
    """
    if device not in DEVICES:
        return None
    machine = platform.machine().lower()
    os_arch: str | None = None
    if sys.platform == "win32":
        os_arch = "windows-x64" if machine in ("x86_64", "amd64") else None
    elif sys.platform == "darwin":
        os_arch = "macos-arm64" if machine in ("arm64", "aarch64") else None
    elif sys.platform.startswith("linux"):
        os_arch = "linux-x64" if machine in ("x86_64", "amd64") else None
    if os_arch is None:
        return None
    cell = f"{os_arch}-{device}"
    return cell if cell in CELL_MATRIX else None
