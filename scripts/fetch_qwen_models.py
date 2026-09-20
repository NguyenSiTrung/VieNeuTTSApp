#!/usr/bin/env python
"""Render checksum-pinned Qwen model manifests for both engine profiles.

Maintainer tool.  It reads the pinned repositories and revisions from
``packaging/qwen-runtime-requirements.json``, lists every file at that revision
through the Hugging Face API, and records each file's size and SHA-256 into
``src/vienetts_app/core/qwen_model_manifests.json``.  Large files publish their
digest as LFS metadata; small text files are downloaded once to hash them.
The application runtime neither runs this script nor imports huggingface_hub.

Usage:
    uv run python scripts/fetch_qwen_models.py            # re-lock both profiles
    uv run python scripts/fetch_qwen_models.py --check    # fail on drift (CI)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REQUIREMENTS = REPO_ROOT / "packaging" / "qwen-runtime-requirements.json"
DEFAULT_OUTPUT = REPO_ROOT / "src" / "vienetts_app" / "core" / "qwen_model_manifests.json"
FORMAT_VERSION = "qwen-model-v1"
API_ROOT = "https://huggingface.co/api/models"
RESOLVE_ROOT = "https://huggingface.co"

# Git bookkeeping that no inference path reads; recorded as excluded, not
# silently dropped.
EXCLUDED_PATHS = {
    ".gitattributes": "git LFS pointer metadata; never read by the model host",
}


class LockError(ValueError):
    """Raised when a repository listing cannot be pinned exactly."""


@dataclass(frozen=True)
class ModelFileRecord:
    path: str
    size_bytes: int
    sha256: str


def validate_relative_path(path: str) -> str:
    """Reject absolute, traversal, and Windows-reserved model paths."""
    if not path or path.startswith("/") or "\\" in path:
        raise LockError(f"unsafe model path: {path!r}")
    parts = path.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise LockError(f"unsafe model path: {path!r}")
    if any(":" in part for part in parts):
        raise LockError(f"unsafe model path: {path!r}")
    return path


def parse_repo_listing(payload: Mapping[str, object]) -> tuple[ModelFileRecord | None, ...]:
    """Turn an API listing into records; ``None`` marks a file without a digest.

    Files stored in git (not LFS) publish no SHA-256 in the listing, so the
    caller must download and hash them.
    """
    siblings = payload.get("siblings")
    if not isinstance(siblings, list) or not siblings:
        raise LockError("repository listing has no files")
    records: list[ModelFileRecord | None] = []
    seen: set[str] = set()
    for sibling in siblings:
        if not isinstance(sibling, Mapping):
            raise LockError("repository listing entry is malformed")
        path = validate_relative_path(str(sibling.get("rfilename", "")))
        if path in seen:
            raise LockError(f"duplicate model path: {path}")
        seen.add(path)
        size = sibling.get("size")
        lfs = sibling.get("lfs")
        digest = lfs.get("sha256") if isinstance(lfs, Mapping) else None
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise LockError(f"model file has no usable size: {path}")
        if digest is None:
            records.append(None)
            continue
        records.append(ModelFileRecord(path, size, str(digest)))
    return tuple(records)


def sha256_of_bytes(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def resolve_listing(
    repo: str,
    revision: str,
    *,
    fetch_json: Callable[[str], object],
    fetch_bytes: Callable[[str], bytes],
) -> tuple[ModelFileRecord, ...]:
    """Pin every file of one repository revision, hashing non-LFS files."""
    payload = fetch_json(f"{API_ROOT}/{repo}/revision/{revision}?blobs=true")
    if not isinstance(payload, Mapping):
        raise LockError(f"repository listing is malformed: {repo}")
    records: list[ModelFileRecord] = []
    for index, record in enumerate(parse_repo_listing(payload)):
        if record is not None:
            records.append(record)
            continue
        # A file without LFS metadata is small enough to hash directly.
        path = _unpinned_path(payload, index)
        content = fetch_bytes(f"{RESOLVE_ROOT}/{repo}/resolve/{revision}/{path}")
        if len(content) != _listed_size(payload, index):
            raise LockError(f"file size drifted while hashing: {path}")
        records.append(ModelFileRecord(path, len(content), sha256_of_bytes(content)))
    return tuple(records)


def _listed_size(payload: Mapping[str, object], index: int) -> int:
    siblings = payload["siblings"]
    assert isinstance(siblings, list)
    return int(siblings[index]["size"])  # type: ignore[index]


def _unpinned_path(payload: Mapping[str, object], index: int) -> str:
    siblings = payload["siblings"]
    assert isinstance(siblings, list)
    return validate_relative_path(str(siblings[index]["rfilename"]))


def split_shared(
    first: Sequence[ModelFileRecord],
    second: Sequence[ModelFileRecord],
) -> tuple[tuple[ModelFileRecord, ...], tuple[ModelFileRecord, ...], tuple[ModelFileRecord, ...]]:
    """Split two revisions into (first only, second only, shared) records.

    Shared means same path, size *and* digest: the tokenizer and speech
    tokenizer content the two profiles reuse instead of downloading twice.
    """
    by_path = {record.path: record for record in second}
    first_only: list[ModelFileRecord] = []
    shared: list[ModelFileRecord] = []
    for record in first:
        other = by_path.get(record.path)
        if (
            other is not None
            and other.size_bytes == record.size_bytes
            and other.sha256 == record.sha256
        ):
            shared.append(record)
        else:
            first_only.append(record)
    shared_paths = {record.path for record in shared}
    second_only = [record for record in second if record.path not in shared_paths]
    return tuple(first_only), tuple(second_only), tuple(shared)


def _render_files(
    records: Sequence[ModelFileRecord],
) -> tuple[list[dict[str, object]], list[dict[str, str]]]:
    files: list[dict[str, object]] = []
    excluded: list[dict[str, str]] = []
    for record in sorted(records, key=lambda item: item.path):
        reason = EXCLUDED_PATHS.get(record.path)
        if reason is not None:
            excluded.append({"path": record.path, "reason": reason})
            continue
        files.append({"path": record.path, "sizeBytes": record.size_bytes, "sha256": record.sha256})
    return files, excluded


def render_manifest(
    requirements: Mapping[str, object],
    *,
    fetch_json: Callable[[str], object],
    fetch_bytes: Callable[[str], bytes],
) -> dict[str, object]:
    """Render the manifest data for the CustomVoice and Base profiles."""
    pins = requirements["modelPins"]
    if not isinstance(pins, Mapping):
        raise LockError("requirements have no model pins")
    listings: dict[str, tuple[ModelFileRecord, ...]] = {}
    for key in ("customvoice", "base"):
        pin = pins[key]
        if not isinstance(pin, Mapping):
            raise LockError(f"model pin is malformed: {key}")
        listings[key] = resolve_listing(
            str(pin["repo"]),
            str(pin["revision"]),
            fetch_json=fetch_json,
            fetch_bytes=fetch_bytes,
        )

    customvoice_only, base_only, shared = split_shared(listings["customvoice"], listings["base"])
    if not shared:
        raise LockError("the two profiles share no verified tokenizer content")
    customvoice_files, customvoice_excluded = _render_files(customvoice_only)
    base_files, base_excluded = _render_files(base_only)
    shared_files, shared_excluded = _render_files(shared)

    return {
        "formatVersion": FORMAT_VERSION,
        "shared": {
            "repo": str(pins["customvoice"]["repo"]),  # type: ignore[index]
            "revision": str(pins["customvoice"]["revision"]),  # type: ignore[index]
            "files": shared_files,
            "excluded": shared_excluded,
        },
        "profiles": {
            "customvoice": {
                "repo": str(pins["customvoice"]["repo"]),  # type: ignore[index]
                "revision": str(pins["customvoice"]["revision"]),  # type: ignore[index]
                "files": customvoice_files,
                "excluded": customvoice_excluded,
            },
            "base": {
                "repo": str(pins["base"]["repo"]),  # type: ignore[index]
                "revision": str(pins["base"]["revision"]),  # type: ignore[index]
                "files": base_files,
                "excluded": base_excluded,
            },
        },
    }


def _fetch_bytes(url: str) -> bytes:
    request = Request(url, headers={"User-Agent": "vienetts-app-lock/1.0"})
    try:
        with urlopen(request, timeout=120) as response:  # noqa: S310 - pinned HF host
            return response.read()
    except (HTTPError, URLError) as exc:
        raise LockError(f"could not fetch {url}: {exc}") from exc


def _fetch_json(url: str) -> object:
    return json.loads(_fetch_bytes(url))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--check", action="store_true", help="fail when the lock is stale")
    args = parser.parse_args(argv)

    try:
        requirements = json.loads(args.requirements.read_text(encoding="utf-8"))
        rendered = render_manifest(requirements, fetch_json=_fetch_json, fetch_bytes=_fetch_bytes)
    except (LockError, HTTPError, URLError, OSError, KeyError) as exc:
        print(f"model lock failed: {exc}", file=sys.stderr)
        return 1

    payload = json.dumps(rendered, indent=2, sort_keys=True) + "\n"
    if args.check:
        current = args.output.read_text(encoding="utf-8") if args.output.is_file() else ""
        if current != payload:
            print(f"{args.output} is stale; re-run without --check", file=sys.stderr)
            return 1
        print(f"{args.output} is up to date")
        return 0

    args.output.write_text(payload, encoding="utf-8")
    total = sum(
        int(entry["sizeBytes"])
        for group in (rendered["shared"], *rendered["profiles"].values())  # type: ignore[union-attr]
        for entry in group["files"]  # type: ignore[index]
    )
    print(f"wrote {args.output} for {len(rendered['profiles'])} profiles ({total} bytes)")  # type: ignore[arg-type]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
