"""Pinned, checksum-verified Qwen runtime wheel manifests.

Unlike the CUDA runtime (two platforms, 26 wheels pinned in source), the Qwen
runtime is a full dependency closure across six platform/device combinations,
so the data lives in ``qwen_runtime_manifests.json`` next to this module and is
rendered by ``scripts/lock_qwen_runtime.py`` (Task 2.2). Keeping it as data
makes the maintainer re-lock a reviewable diff instead of a 4000-line source
edit, and the loader validates it exactly as strictly as the CUDA one.

A missing or invalid data file yields no manifests at all: the engine reports
"Qwen runtime unavailable" instead of importing a half-verified runtime. The
unit tests fail loudly if the shipped data file drifts from the contract.
"""

from __future__ import annotations

import json
import platform
import re
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path

from vienetts_app.core.managed_install import RuntimeWheel, check_download_url

FORMAT_VERSION = "qwen-runtime-v1"
MANIFEST_PATH = Path(__file__).with_name("qwen_runtime_manifests.json")

# Platform key -> (wheel platform tag, device). Keys are the ones used by
# packaging/qwen-runtime-requirements.json and the probe evidence paths.
PLATFORM_MATRIX: Mapping[str, tuple[str, str]] = {
    "windows-x64-cpu": ("win_amd64", "cpu"),
    "windows-x64-cuda": ("win_amd64", "cuda"),
    "linux-x64-cpu": ("linux_x86_64", "cpu"),
    "linux-x64-cuda": ("linux_x86_64", "cuda"),
    "macos-arm64-cpu": ("macosx_11_0_arm64", "cpu"),
    "macos-arm64-mps": ("macosx_11_0_arm64", "mps"),
}

DEVICES = frozenset({"cpu", "cuda", "mps"})

#: Display names for the host part of a platform key (see :func:`platform_label`).
_HOST_PLATFORM_LABELS: Mapping[str, str] = {
    "windows-x64": "Windows x64",
    "linux-x64": "Linux x64",
    "macos-arm64": "macOS arm64",
}
_PYTHON_TAG = re.compile(r"cp3\d{2}")
_SHA256 = re.compile(r"[0-9a-f]{64}")
_REQUIRED_PINS = ("qwen-tts", "transformers", "torch", "torchaudio", "sox")
_DISTRIBUTION = re.compile(r"^(?P<name>[A-Za-z0-9._]+?)-(?P<version>[0-9][^-]*)-")


@dataclass(frozen=True)
class SdistOnlyRecord:
    """A declared dependency with no wheel for the platform.

    The runtime installer is wheel-only, so a dependency that publishes no
    wheel for a platform is excluded and recorded here with the reason instead
    of silently disappearing from the closure. ``sox`` used to land here for
    every platform (only an sdist on PyPI) until the closure pinned 1.4.1,
    whose universal wheel the host needs: ``qwen-tts`` imports ``sox`` at
    module load.
    """

    name: str
    version: str
    reason: str


@dataclass(frozen=True)
class QwenRuntimeManifest:
    format_version: str
    platform_key: str
    platform_tag: str
    device: str
    python_tag: str
    torch_local_version: str
    pins: Mapping[str, str]
    wheels: tuple[RuntimeWheel, ...]
    sdist_only: tuple[SdistOnlyRecord, ...] = ()
    metadata: Mapping[str, object] = field(default_factory=dict)

    @property
    def total_bytes(self) -> int:
        return sum(wheel.size_bytes for wheel in self.wheels)

    def wheel_for(self, distribution: str) -> RuntimeWheel | None:
        for wheel in self.wheels:
            if distribution_name(wheel.filename) == distribution:
                return wheel
        return None


def distribution_name(filename: str) -> str:
    """Distribution name of a wheel filename, normalised for comparisons."""
    match = _DISTRIBUTION.match(filename)
    if match is None:
        return ""
    return match.group("name").replace("_", "-").lower()


def validate_wheel_url(wheel: RuntimeWheel) -> None:
    """Reject a wheel that is not a direct artifact from an allowlisted host.

    Called at manifest load time *and* by the runtime manager, so a manifest
    built in code (or by a future caller) cannot bypass the download policy.
    """
    if not wheel.filename.endswith(".whl"):
        raise ValueError(f"runtime artifact must be a wheel: {wheel.filename}")
    check_download_url(
        wheel.url,
        required_path_prefixes=("/whl/" if "pytorch.org" in wheel.url else "/packages/",),
        filename=wheel.filename,
    )
    if not _SHA256.fullmatch(wheel.sha256):
        raise ValueError(f"wheel SHA-256 is invalid: {wheel.filename}")
    if not isinstance(wheel.size_bytes, int) or isinstance(wheel.size_bytes, bool):
        raise ValueError(f"wheel size is invalid: {wheel.filename}")
    if wheel.size_bytes <= 0:
        raise ValueError(f"wheel size is invalid: {wheel.filename}")


def validate_manifest(manifest: QwenRuntimeManifest) -> None:
    """Validate a manifest object against the platform matrix and URL policy."""
    _validate_manifest(manifest.platform_key, manifest)


def _validate_wheel(manifest: QwenRuntimeManifest, wheel: RuntimeWheel) -> None:
    validate_wheel_url(wheel)

    if manifest.device == "cuda":
        if "download.pytorch.org/whl/cu128/" not in wheel.url and "torch" in wheel.filename:
            raise ValueError(f"CUDA runtime must pin cu128 torch wheels: {wheel.url}")
    elif manifest.device in {"cpu", "mps"} and "download.pytorch.org/whl/cu128/" in wheel.url:
        raise ValueError(f"non-CUDA runtime must not pin cu128 wheels: {wheel.url}")


def _validate_manifest(platform_key: str, manifest: QwenRuntimeManifest) -> None:
    expected = PLATFORM_MATRIX.get(platform_key)
    if expected is None:
        raise ValueError(f"unsupported platform key: {platform_key}")
    platform_tag, device = expected
    if manifest.platform_key != platform_key:
        raise ValueError(f"manifest platform key mismatch: {platform_key}")
    if manifest.platform_tag != platform_tag:
        raise ValueError(f"manifest platform tag mismatch: {platform_key}")
    if manifest.device not in DEVICES or manifest.device != device:
        raise ValueError(f"manifest device mismatch: {platform_key}")
    if not _PYTHON_TAG.fullmatch(manifest.python_tag):
        raise ValueError(f"manifest python tag is invalid: {manifest.python_tag}")
    if not manifest.wheels:
        raise ValueError(f"manifest has no wheels: {platform_key}")

    torch_wheel = manifest.wheel_for("torch")
    if torch_wheel is None:
        raise ValueError(f"manifest is missing torch: {platform_key}")
    if f"-{manifest.torch_local_version}-" not in torch_wheel.filename:
        raise ValueError(f"torch pin does not match the torch wheel: {platform_key}")
    if platform_tag.startswith("macosx") and "+" in manifest.torch_local_version:
        raise ValueError(f"macOS torch pins come from PyPI without a local version: {platform_key}")

    for name in _REQUIRED_PINS:
        if name not in manifest.pins:
            raise ValueError(f"manifest is missing the {name} pin: {platform_key}")

    filenames: set[str] = set()
    for wheel in manifest.wheels:
        if wheel.filename in filenames:
            raise ValueError(f"duplicate wheel filename: {wheel.filename}")
        filenames.add(wheel.filename)
        _validate_wheel(manifest, wheel)

    for record in manifest.sdist_only:
        if not record.name or not record.reason:
            raise ValueError(f"sdist-only record needs a name and reason: {platform_key}")


def load_manifests_from_data(data: Mapping[str, object]) -> dict[str, QwenRuntimeManifest]:
    """Build and validate every manifest in a rendered data file."""
    if data.get("formatVersion") != FORMAT_VERSION:
        raise ValueError(f"unsupported manifest format: {data.get('formatVersion')!r}")
    platforms = data.get("platforms")
    if not isinstance(platforms, Mapping) or not platforms:
        raise ValueError("manifest data has no platforms")
    manifests: dict[str, QwenRuntimeManifest] = {}
    for platform_key, entry in platforms.items():
        if not isinstance(entry, Mapping):
            raise ValueError(f"manifest entry is not an object: {platform_key}")
        raw_wheels = entry.get("wheels")
        if not isinstance(raw_wheels, list):
            raise ValueError(f"manifest wheels are not a list: {platform_key}")
        wheels = tuple(
            RuntimeWheel(
                filename=str(wheel["filename"]),
                url=str(wheel["url"]),
                size_bytes=int(wheel["sizeBytes"]),
                sha256=str(wheel["sha256"]),
            )
            for wheel in raw_wheels
            if isinstance(wheel, Mapping)
        )
        if len(wheels) != len(raw_wheels):
            raise ValueError(f"manifest wheels are malformed: {platform_key}")
        raw_sdist = entry.get("sdistOnly", [])
        if not isinstance(raw_sdist, list):
            raise ValueError(f"manifest sdistOnly is not a list: {platform_key}")
        sdist_only = tuple(
            SdistOnlyRecord(
                name=str(record.get("name", "")),
                version=str(record.get("version", "")),
                reason=str(record.get("reason", "")),
            )
            for record in raw_sdist
            if isinstance(record, Mapping)
        )
        manifest = QwenRuntimeManifest(
            format_version=FORMAT_VERSION,
            platform_key=str(platform_key),
            platform_tag=str(entry.get("platformTag", "")),
            device=str(entry.get("device", "")),
            python_tag=str(entry.get("pythonTag", "")),
            torch_local_version=str(entry.get("torchLocalVersion", "")),
            pins={str(name): str(version) for name, version in dict(entry.get("pins", {})).items()},
            wheels=wheels,
            sdist_only=sdist_only,
            metadata=dict(entry.get("metadata", {})),
        )
        _validate_manifest(str(platform_key), manifest)
        manifests[str(platform_key)] = manifest
    return manifests


def _load_shipped_manifests(path: Path = MANIFEST_PATH) -> dict[str, QwenRuntimeManifest]:
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


MANIFESTS: Mapping[str, QwenRuntimeManifest] = _load_shipped_manifests()


def manifest_for_platform(platform_key: str) -> QwenRuntimeManifest | None:
    """Return the verified Qwen runtime manifest for a supported platform."""
    return MANIFESTS.get(platform_key)


def platform_key_for(platform_tag: str, device: str) -> str | None:
    """Platform key for a wheel tag and device, preferring exact device match.

    ``macos-arm64-cpu`` and ``macos-arm64-mps`` share one wheel set; a request
    for device ``cpu`` on Apple Silicon may still run on MPS, so the caller
    decides which key it wants and this helper only maps unambiguous cases.
    """
    candidates = [
        key
        for key, (tag, known_device) in PLATFORM_MATRIX.items()
        if tag == platform_tag and known_device == device
    ]
    return candidates[0] if len(candidates) == 1 else None


def host_platform_tag() -> str | None:
    """This host's wheel tag in :data:`PLATFORM_MATRIX` (``None`` = unsupported).

    Only the release platforms the pinned manifests cover answer with a tag, so
    a linux-arm64 host or an Intel Mac reports "no managed runtime" instead of
    a key whose wheels could never load.
    """
    machine = platform.machine().lower()
    if sys.platform == "win32":
        return "win_amd64" if machine in ("x86_64", "amd64") else None
    if sys.platform == "darwin":
        return "macosx_11_0_arm64" if machine in ("arm64", "aarch64") else None
    if sys.platform.startswith("linux"):
        return "linux_x86_64" if machine in ("x86_64", "amd64") else None
    return None


def host_platform_key(device: str) -> str | None:
    """The pinned manifest key for this host running on ``device``."""
    tag = host_platform_tag()
    if tag is None or device not in DEVICES:
        return None
    return platform_key_for(tag, device)


def host_devices() -> tuple[str, ...]:
    """Devices this host has a pinned runtime for, best first as declared."""
    tag = host_platform_tag()
    if tag is None:
        return ()
    return tuple(device for known_tag, device in PLATFORM_MATRIX.values() if known_tag == tag)


def platform_label(platform_key: str) -> str:
    """Human-readable "platform · device" label for a key ("" when unknown)."""
    entry = PLATFORM_MATRIX.get(platform_key)
    if entry is None:
        return ""
    host = platform_key.rsplit("-", 1)[0]
    return f"{_HOST_PLATFORM_LABELS.get(host, host)} · {entry[1].upper()}"


def torch_local_version_for_platform(platform_key: str) -> str | None:
    manifest = manifest_for_platform(platform_key)
    return manifest.torch_local_version if manifest is not None else None


def manifest_bytes_for_platform(platform_key: str) -> int:
    manifest = manifest_for_platform(platform_key)
    return manifest.total_bytes if manifest is not None else 0
