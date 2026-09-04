"""GitHub Releases update check: version compare + platform asset matching.

Stdlib-only (urllib/json/platform) so the frozen CPU build gains no new
dependencies. Everything is pure except ``fetch_latest_release``; the
controller injects a fake fetcher in tests. All entry points are
total — network failures surface as ``UpdateInfo.error``, never raise —
because the app is offline-first and a failed check must stay silent.
"""

from __future__ import annotations

import json
import logging
import platform as _platform
import re
import sys
import urllib.request
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

GITHUB_OWNER = "NguyenSiTrung"
GITHUB_REPO = "VieNeuTTSApp"
LATEST_RELEASE_URL = f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}/releases/latest"
REQUEST_TIMEOUT_SECONDS = 10.0

PLATFORM_WINDOWS_X64 = "windows-x64"
PLATFORM_LINUX_X64 = "linux-x64"
PLATFORM_MACOS_ARM64 = "macos-arm64"

_VERSION_NUMERIC_PREFIX_RE = re.compile(r"(\d+(?:\.\d+)*)")


@dataclass(frozen=True)
class ReleaseAsset:
    """One downloadable file attached to a GitHub Release."""

    name: str
    url: str
    size: int = -1

    def to_dict(self) -> dict[str, object]:
        return {"name": self.name, "url": self.url, "size": self.size}


@dataclass(frozen=True)
class UpdateInfo:
    """Result of one update check (always delivered, even on failure)."""

    available: bool
    current_version: str
    latest_version: str = ""
    release_url: str = ""
    release_notes: str = ""
    platform_asset: ReleaseAsset | None = None
    other_assets: tuple[ReleaseAsset, ...] = ()
    error: str = ""

    def other_assets_dicts(self) -> list[dict[str, object]]:
        return [a.to_dict() for a in self.other_assets]


@dataclass(frozen=True)
class _ParsedRelease:
    tag_version: str
    release_url: str
    release_notes: str
    assets: tuple[ReleaseAsset, ...] = field(default=())


def current_platform_key() -> str:
    """This host's release-asset key (``windows-x64``/``linux-x64``/``macos-arm64``).

    Unknown arch/OS combos still return a ``<os>-<arch>`` key — it simply
    matches no asset, so the UI offers the other-platforms list instead of
    a wrong download (e.g. Intel Macs: the pipeline ships arm64-only DMGs).
    """
    machine = _platform.machine().lower()
    if machine in ("x86_64", "amd64"):
        arch = "x64"
    elif machine in ("arm64", "aarch64"):
        arch = "arm64"
    else:
        arch = machine
    if sys.platform == "win32":
        return f"windows-{arch}"
    if sys.platform == "darwin":
        return f"macos-{arch}"
    return f"linux-{arch}"


def platform_display_name(platform_key: str) -> str:
    """Short human label for a platform key (used for the download button)."""
    return {
        PLATFORM_WINDOWS_X64: "Windows",
        PLATFORM_LINUX_X64: "Linux",
        PLATFORM_MACOS_ARM64: "macOS",
    }.get(platform_key, platform_key)


def parse_version(text: object) -> tuple[int, ...]:
    """``v0.1.5``/``0.1.5``/``0.1.5-rc1`` → ``(0, 1, 5)``; garbage → ``()``."""
    if not isinstance(text, str):
        return ()
    match = _VERSION_NUMERIC_PREFIX_RE.search(text.strip().lstrip("vV"))
    if match is None:
        return ()
    try:
        return tuple(int(part) for part in match.group(1).split("."))
    except ValueError:
        return ()


def compare_versions(a: str, b: str) -> int:
    """-1/0/+1 comparing dotted versions; missing segments pad with zero."""
    pa, pb = parse_version(a), parse_version(b)
    width = max(len(pa), len(pb))
    pa += (0,) * (width - len(pa))
    pb += (0,) * (width - len(pb))
    return (pa > pb) - (pa < pb)


def asset_platform_key(asset_name: object) -> str | None:
    """Map a release file name to its platform key (None = unrecognized)."""
    if not isinstance(asset_name, str):
        return None
    name = asset_name.lower()
    if "windows-x64" in name or ("windows" in name and "x64" in name):
        return PLATFORM_WINDOWS_X64
    if "linux-x64" in name or ("linux" in name and "x64" in name):
        return PLATFORM_LINUX_X64
    if "macos-arm64" in name or ("mac" in name and "arm64" in name):
        return PLATFORM_MACOS_ARM64
    return None


def _parse_release_payload(data: object) -> _ParsedRelease:
    if not isinstance(data, dict):
        raise ValueError("unexpected release payload (not an object)")
    tag = data.get("tag_name")
    if not isinstance(tag, str) or not tag:
        raise ValueError("release payload has no tag_name")
    assets: list[ReleaseAsset] = []
    raw_assets = data.get("assets")
    if isinstance(raw_assets, list):
        for entry in raw_assets:
            if not isinstance(entry, dict):
                continue
            url = entry.get("browser_download_url")
            name = entry.get("name")
            if not isinstance(url, str) or not url or not isinstance(name, str) or not name:
                continue
            size = entry.get("size")
            assets.append(
                ReleaseAsset(name=name, url=url, size=size if isinstance(size, int) else -1)
            )
    notes = data.get("body")
    page = data.get("html_url")
    return _ParsedRelease(
        tag_version=tag,
        release_url=page if isinstance(page, str) else "",
        release_notes=notes if isinstance(notes, str) else "",
        assets=tuple(assets),
    )


def _http_get_json(url: str) -> dict:
    request = urllib.request.Request(
        url,
        headers={
            # GitHub API rejects requests without a User-Agent.
            "User-Agent": f"{GITHUB_REPO} update-check",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        return json.loads(response.read().decode("utf-8"))


def fetch_latest_release(fetcher=None) -> _ParsedRelease:
    """Fetch + parse the latest GitHub Release (raises on any failure)."""
    get = fetcher if fetcher is not None else _http_get_json
    return _parse_release_payload(get(LATEST_RELEASE_URL))


def check_for_updates(
    current_version: str,
    *,
    platform_key: str | None = None,
    fetcher=None,
) -> UpdateInfo:
    """Compare ``current_version`` against the latest GitHub Release.

    Never raises: transport/parse failures return ``available=False`` with
    ``error`` set, so callers can stay silent (startup auto-check) or show
    the message (manual "Check" press).
    """
    key = platform_key or current_platform_key()
    try:
        release = fetch_latest_release(fetcher)
    except Exception as exc:  # noqa: BLE001 — total function by contract
        logger.debug("update check failed: %s", exc)
        return UpdateInfo(available=False, current_version=current_version, error=str(exc))
    try:
        is_newer = compare_versions(release.tag_version, current_version) > 0
    except Exception as exc:  # noqa: BLE001 — defensive; parse/compare are pure
        return UpdateInfo(available=False, current_version=current_version, error=str(exc))
    if not is_newer:
        return UpdateInfo(
            available=False,
            current_version=current_version,
            latest_version=release.tag_version,
        )
    platform_asset: ReleaseAsset | None = None
    others: list[ReleaseAsset] = []
    for asset in release.assets:
        if platform_asset is None and asset_platform_key(asset.name) == key:
            platform_asset = asset
        else:
            others.append(asset)
    return UpdateInfo(
        available=True,
        current_version=current_version,
        latest_version=release.tag_version,
        release_url=release.release_url,
        release_notes=release.release_notes,
        platform_asset=platform_asset,
        other_assets=tuple(others),
    )
