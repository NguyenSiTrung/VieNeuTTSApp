"""Pinned, checksum-verified Qwen model manifests.

Two engine profiles share one on-disk install root:

* ``customvoice`` — ``Qwen3-TTS-12Hz-0.6B-CustomVoice`` (speaker presets);
* ``base`` — ``Qwen3-TTS-12Hz-0.6B-Base`` (voice cloning from a reference clip).

Their tokenizer and speech-tokenizer files are byte-identical, so they are
pinned once as *shared* files: one download, one verification, and removing one
profile never discards content the other still needs.

The data ships inside the package (``qwen_model_manifests.json``) and is
rendered by ``scripts/fetch_qwen_models.py`` from the Phase 0 repository pins.
A missing or invalid data file yields no profiles at all, so the engine reports
"models not installed" instead of running against unverified weights.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

FORMAT_VERSION = "qwen-model-v1"
MANIFEST_PATH = Path(__file__).with_name("qwen_model_manifests.json")

PROFILE_KEYS = ("customvoice", "base")

# 256 MiB of headroom: a re-install keeps the previous copy until promotion.
DOWNLOAD_HEADROOM_BYTES = 256 * 1024 * 1024

_REVISION = re.compile(r"[0-9a-f]{40}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REPO = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*/[A-Za-z0-9][A-Za-z0-9._-]*")


@dataclass(frozen=True)
class QwenModelFile:
    path: str
    size_bytes: int
    sha256: str


@dataclass(frozen=True)
class QwenModelProfile:
    key: str
    repo: str
    revision: str
    files: tuple[QwenModelFile, ...]
    excluded: tuple[tuple[str, str], ...] = ()
    shared: tuple[QwenModelFile, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def own_bytes(self) -> int:
        return sum(item.size_bytes for item in self.files)

    @property
    def shared_bytes(self) -> int:
        return sum(item.size_bytes for item in self.shared)

    @property
    def total_bytes(self) -> int:
        return self.own_bytes + self.shared_bytes

    @property
    def required_free_bytes(self) -> int:
        return self.total_bytes + DOWNLOAD_HEADROOM_BYTES

    def file_for(self, relative_path: str) -> QwenModelFile | None:
        for item in self.files:
            if item.path == relative_path:
                return item
        return None

    def shared_file_for(self, relative_path: str) -> QwenModelFile | None:
        for item in self.shared:
            if item.path == relative_path:
                return item
        return None


@dataclass(frozen=True)
class QwenModelManifest:
    format_version: str
    shared_repo: str
    shared_revision: str
    profiles: Mapping[str, QwenModelProfile]

    def profile_for(self, key: str) -> QwenModelProfile | None:
        return self.profiles.get(key)


def _validate_path(path: str) -> None:
    if not path or path.startswith("/") or "\\" in path:
        raise ValueError(f"model path is unsafe: {path!r}")
    parts = path.split("/")
    if any(part in {"", ".", ".."} or ":" in part for part in parts):
        raise ValueError(f"model path is unsafe: {path!r}")


def _validate_files(records: tuple[QwenModelFile, ...], *, label: str) -> None:
    seen: set[str] = set()
    for record in records:
        _validate_path(record.path)
        if record.path in seen:
            raise ValueError(f"duplicate model file: {record.path}")
        seen.add(record.path)
        if not isinstance(record.size_bytes, int) or isinstance(record.size_bytes, bool):
            raise ValueError(f"model file size is invalid: {record.path}")
        if record.size_bytes <= 0:
            raise ValueError(f"model file size is invalid: {record.path}")
        if not _SHA256.fullmatch(record.sha256):
            raise ValueError(f"model file SHA-256 is invalid: {record.path}")
    if not records:
        raise ValueError(f"{label} has no files")


def _validate_profile(profile: QwenModelProfile) -> None:
    if not _REPO.fullmatch(profile.repo):
        raise ValueError(f"model repository is not pinned to an owner/name: {profile.repo}")
    if not _REVISION.fullmatch(profile.revision):
        raise ValueError(f"model revision is not an immutable commit: {profile.revision}")
    _validate_files(profile.files, label=f"profile {profile.key}")
    _validate_files(profile.shared, label=f"shared files for {profile.key}")
    shared_paths = {item.path for item in profile.shared}
    overlap = shared_paths.intersection(item.path for item in profile.files)
    if overlap:
        raise ValueError(f"profile files duplicate shared files: {sorted(overlap)}")
    for path, reason in profile.excluded:
        if not path or not reason:
            raise ValueError(f"excluded model file needs a path and reason: {profile.key}")


def _files_from(entries: object, *, label: str) -> tuple[QwenModelFile, ...]:
    if not isinstance(entries, list):
        raise ValueError(f"{label} is not a list")
    records = tuple(
        QwenModelFile(
            path=str(entry.get("path", "")),
            size_bytes=int(entry.get("sizeBytes", 0)),
            sha256=str(entry.get("sha256", "")),
        )
        for entry in entries
        if isinstance(entry, Mapping)
    )
    if len(records) != len(entries):
        raise ValueError(f"{label} is malformed")
    return records


def _excluded_from(entries: object) -> tuple[tuple[str, str], ...]:
    if entries is None:
        return ()
    if not isinstance(entries, list):
        raise ValueError("excluded model files are not a list")
    return tuple(
        (str(entry.get("path", "")), str(entry.get("reason", "")))
        for entry in entries
        if isinstance(entry, Mapping)
    )


def load_manifest_from_data(data: Mapping[str, object]) -> QwenModelManifest:
    """Build and validate the manifest from a rendered data file."""
    if data.get("formatVersion") != FORMAT_VERSION:
        raise ValueError(f"unsupported model manifest format: {data.get('formatVersion')!r}")
    shared = data.get("shared")
    if not isinstance(shared, Mapping):
        raise ValueError("model manifest has no shared files")
    shared_files = _files_from(shared.get("files"), label="shared files")
    _validate_files(shared_files, label="shared files")
    profiles_data = data.get("profiles")
    if not isinstance(profiles_data, Mapping) or not profiles_data:
        raise ValueError("model manifest has no profiles")
    profiles: dict[str, QwenModelProfile] = {}
    for key, entry in profiles_data.items():
        if not isinstance(entry, Mapping):
            raise ValueError(f"profile entry is not an object: {key}")
        profile = QwenModelProfile(
            key=str(key),
            repo=str(entry.get("repo", "")),
            revision=str(entry.get("revision", "")),
            files=_files_from(entry.get("files"), label=f"profile {key} files"),
            excluded=_excluded_from(entry.get("excluded")),
            shared=shared_files,
            metadata=dict(entry.get("metadata", {})),
        )
        _validate_profile(profile)
        profiles[str(key)] = profile
    missing = [key for key in PROFILE_KEYS if key not in profiles]
    if missing:
        raise ValueError(f"model manifest is missing profiles: {missing}")
    return QwenModelManifest(
        format_version=FORMAT_VERSION,
        shared_repo=str(shared.get("repo", "")),
        shared_revision=str(shared.get("revision", "")),
        profiles=profiles,
    )


def _load_shipped_manifest(path: Path = MANIFEST_PATH) -> QwenModelManifest | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if not isinstance(data, Mapping):
        return None
    try:
        return load_manifest_from_data(data)
    except (ValueError, KeyError, TypeError):
        return None


MANIFEST: QwenModelManifest | None = _load_shipped_manifest()


def profile_for(key: str) -> QwenModelProfile | None:
    """Return the verified model profile, or None when it is not installable."""
    return MANIFEST.profile_for(key) if MANIFEST is not None else None


def profile_keys() -> tuple[str, ...]:
    return tuple(key for key in PROFILE_KEYS if profile_for(key) is not None)


def total_bytes_for(key: str) -> int:
    profile = profile_for(key)
    return profile.total_bytes if profile is not None else 0
