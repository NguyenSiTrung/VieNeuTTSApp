#!/usr/bin/env python
"""Render the checksum+metadata-pinned Qwen3-TTS GGUF model manifest.

Maintainer tool. It reads the pinned repository, revision, variant files and
metadata evidence from ``packaging/qwen-gguf-runtime-requirements.json``,
lists the repository at that immutable revision through the Hugging Face API,
verifies every pinned size/SHA-256/blobId against the listing, and writes
``src/vienetts_app/core/qwen_gguf_model_manifests.json``.

The GGUF contract pairs one talker per (profile, quantization) with one
tokenizer per quantization shared across profiles, so the rendered manifest
locks four talkers plus two tokenizers — six files total, never more.

Usage:
    uv run python scripts/fetch_qwen_gguf_models.py            # re-lock
    uv run python scripts/fetch_qwen_gguf_models.py --check    # fail on drift
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_REQUIREMENTS = REPO_ROOT / "packaging" / "qwen-gguf-runtime-requirements.json"
DEFAULT_OUTPUT = REPO_ROOT / "src" / "vienetts_app" / "core" / "qwen_gguf_model_manifests.json"
FORMAT_VERSION = "qwen-gguf-model-v1"
API_ROOT = "https://huggingface.co/api/models"

PROFILES = ("base", "customvoice")
QUANTIZATIONS = ("Q8_0", "Q4_K_M")

TALKER_ARCHITECTURE = "qwen3-tts"
TOKENIZER_ARCHITECTURE = "qwen3-tts-tokenizer"
TOKENIZER_NAME = "Qwen3-TTS-Tokenizer-12Hz"
TALKER_NAME_FOR = {
    "base": "Qwen3-TTS-12Hz-0.6B-base",
    "customvoice": "Qwen3-TTS-12Hz-0.6B-custom_voice",
}
MODEL_TYPE_FOR = {"base": "base", "customvoice": "custom_voice"}
TOKENIZER_TYPE = "qwen3_tts_tokenizer_12hz"
MODEL_SIZE = "0b6"
NUM_CODE_GROUPS = 16
QUANTIZATION_VERSION = 2


class LockError(ValueError):
    """Raised when the pinned model artifacts cannot be verified exactly."""


@dataclass(frozen=True)
class BlobRecord:
    path: str
    size_bytes: int
    sha256: str
    blob_id: str


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


def parse_repo_listing(payload: Mapping[str, object]) -> dict[str, BlobRecord]:
    """Index one repo revision listing by path; every GGUF must be LFS-pinned."""
    siblings = payload.get("siblings")
    if not isinstance(siblings, list) or not siblings:
        raise LockError("repository listing has no files")
    records: dict[str, BlobRecord] = {}
    for sibling in siblings:
        if not isinstance(sibling, Mapping):
            raise LockError("repository listing entry is malformed")
        path = validate_relative_path(str(sibling.get("rfilename", "")))
        if path in records:
            raise LockError(f"duplicate model path: {path}")
        size = sibling.get("size")
        lfs = sibling.get("lfs")
        digest = lfs.get("sha256") if isinstance(lfs, Mapping) else ""
        blob_id = sibling.get("blobId", "")
        if not isinstance(size, int) or isinstance(size, bool) or size <= 0:
            raise LockError(f"model file has no usable size: {path}")
        records[path] = BlobRecord(path, size, str(digest or ""), str(blob_id or ""))
    return records


def _verified_blob(
    listing: Mapping[str, BlobRecord],
    pinned: Mapping[str, object],
    *,
    label: str,
) -> BlobRecord:
    """Check one requirements pin (file/size/sha256/blobId) against the listing."""
    path = validate_relative_path(str(pinned.get("file", "")))
    record = listing.get(path)
    if record is None:
        raise LockError(f"{label} is not in the repository listing: {path}")
    if len(record.sha256) != 64:
        raise LockError(f"{label} has no LFS SHA-256 to verify: {path}")
    if not record.blob_id:
        raise LockError(f"{label} has no immutable blob id to verify: {path}")
    expected_size = int(pinned.get("size", 0))
    if record.size_bytes != expected_size:
        raise LockError(
            f"{label} size drifted: {path} is {record.size_bytes}, expected {expected_size}"
        )
    expected_sha = str(pinned.get("sha256", ""))
    if record.sha256 != expected_sha:
        raise LockError(f"{label} SHA-256 drifted: {path}")
    expected_blob = str(pinned.get("blobId", ""))
    if expected_blob and record.blob_id != expected_blob:
        raise LockError(f"{label} blob id drifted: {path}")
    return record


def _talker_metadata(profile: str, quantization: str) -> dict[str, object]:
    return {
        "general.architecture": TALKER_ARCHITECTURE,
        "general.name": TALKER_NAME_FOR[profile],
        "general.file_type": quantization,
        "general.quantization_version": QUANTIZATION_VERSION,
        "qwen3-tts.model_type": MODEL_TYPE_FOR[profile],
        "qwen3-tts.tokenizer_type": TOKENIZER_TYPE,
        "qwen3-tts.model_size": MODEL_SIZE,
        "qwen3-tts.num_code_groups": NUM_CODE_GROUPS,
    }


def _tokenizer_metadata(quantization: str) -> dict[str, object]:
    return {
        "general.architecture": TOKENIZER_ARCHITECTURE,
        "general.name": TOKENIZER_NAME,
        "general.file_type": quantization,
        "general.quantization_version": QUANTIZATION_VERSION,
    }


def _render_file(record: BlobRecord, metadata: Mapping[str, object]) -> dict[str, object]:
    return {
        "path": record.path,
        "sizeBytes": record.size_bytes,
        "sha256": record.sha256,
        "blobId": record.blob_id,
        "metadata": dict(metadata),
    }


def render_manifest(
    requirements: Mapping[str, object],
    *,
    fetch_json: Callable[[str], object],
) -> dict[str, object]:
    """Verify the pinned variants against the live listing and render the lock."""
    upstream = requirements.get("upstream")
    if not isinstance(upstream, Mapping) or not isinstance(upstream.get("models"), Mapping):
        raise LockError("requirements have no upstream.models pin")
    models = upstream["models"]
    repo = str(models.get("repo", ""))
    revision = str(models.get("revision", ""))
    if not repo or len(revision) != 40:
        raise LockError("upstream.models must pin a repo and a 40-char revision")

    payload = fetch_json(f"{API_ROOT}/{repo}/revision/{revision}?blobs=true")
    if not isinstance(payload, Mapping):
        raise LockError(f"repository listing is malformed: {repo}")
    listing = parse_repo_listing(payload)

    variants = requirements.get("variants")
    if not isinstance(variants, list) or not variants:
        raise LockError("requirements have no variant pins")
    talkers: dict[str, dict[str, object]] = {}
    tokenizers: dict[str, dict[str, object]] = {}
    seen_variants: set[str] = set()
    for entry in variants:
        if not isinstance(entry, Mapping):
            raise LockError("variant pin is malformed")
        profile = str(entry.get("profile", ""))
        quantization = str(entry.get("quantization", ""))
        key = f"{profile}-{quantization}"
        if profile not in PROFILES or quantization not in QUANTIZATIONS:
            raise LockError(f"variant pin names an unknown variant: {key}")
        if key in seen_variants:
            raise LockError(f"duplicate variant pin: {key}")
        seen_variants.add(key)
        expected_type = str(entry.get("expectedModelType", ""))
        if expected_type != MODEL_TYPE_FOR[profile]:
            raise LockError(
                f"variant {key} expects model_type {expected_type!r}, "
                f"the manifest pins {MODEL_TYPE_FOR[profile]!r}"
            )
        talker = _verified_blob(listing, _pin(entry, "talker"), label=f"talker {key}")
        tokenizer = _verified_blob(listing, _pin(entry, "tokenizer"), label=f"tokenizer {key}")
        talkers[key] = _render_file(talker, _talker_metadata(profile, quantization))
        prior = tokenizers.get(quantization)
        if prior is not None and prior["path"] != tokenizer.path:
            raise LockError(
                f"profiles disagree on the {quantization} codec: "
                f"{prior['path']} vs {tokenizer.path}"
            )
        tokenizers.setdefault(
            quantization, _render_file(tokenizer, _tokenizer_metadata(quantization))
        )
    for profile in PROFILES:
        for quantization in QUANTIZATIONS:
            if f"{profile}-{quantization}" not in seen_variants:
                raise LockError(f"requirements are missing variant {profile}-{quantization}")
    if len(tokenizers) != len(QUANTIZATIONS):
        raise LockError("variants did not pin one tokenizer per quantization")

    return {
        "formatVersion": FORMAT_VERSION,
        "repo": repo,
        "revision": revision,
        "talkers": talkers,
        "tokenizers": tokenizers,
    }


def _pin(entry: Mapping[str, object], key: str) -> Mapping[str, object]:
    pinned = entry.get(key)
    if not isinstance(pinned, Mapping):
        raise LockError(f"variant pin {entry.get('key', '?')} is missing {key}")
    return pinned


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
        rendered = render_manifest(requirements, fetch_json=_fetch_json)
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
        for group in (rendered["talkers"], rendered["tokenizers"])  # type: ignore[union-attr]
        for entry in group.values()  # type: ignore[union-attr]
    )
    print(
        f"wrote {args.output} for {len(rendered['talkers'])} variants "  # type: ignore[arg-type]
        f"+ {len(rendered['tokenizers'])} shared codecs ({total} bytes)"  # type: ignore[arg-type]
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
