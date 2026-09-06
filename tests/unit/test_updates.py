"""Update check: version compare, platform asset matching, payload parsing.

Pure ``core.updates`` tests — no network, no Qt. The fetcher seam takes
the URL and returns decoded JSON, so every GitHub shape is a dict literal.
"""

from __future__ import annotations

import pytest

from vienetts_app.core.updates import (
    UpdateInfo,
    asset_platform_key,
    check_for_updates,
    compare_versions,
    parse_version,
    platform_display_name,
)


def _payload(**overrides):
    base = {
        "tag_name": "v0.2.0",
        "html_url": "https://github.com/NguyenSiTrung/VieNeuTTSApp/releases/tag/v0.2.0",
        "body": "notes",
        "assets": [
            {
                "name": "VieNeuTTS-0.2.0-windows-x64.zip",
                "browser_download_url": "https://example.com/win",
                "size": 11,
            },
            {
                "name": "VieNeuTTS-0.2.0-windows-x64-cuda.zip",
                "browser_download_url": "https://example.com/win-cuda",
                "size": 44,
            },
            {
                "name": "VieNeuTTS-0.2.0-linux-x64.zip",
                "browser_download_url": "https://example.com/lin",
                "size": 22,
            },
            {
                "name": "VieNeuTTS-0.2.0-linux-x64-cuda.zip",
                "browser_download_url": "https://example.com/lin-cuda",
                "size": 55,
            },
            {
                "name": "VieNeuTTS-0.2.0-macos-arm64.dmg",
                "browser_download_url": "https://example.com/mac",
                "size": 33,
            },
        ],
    }
    base.update(overrides)
    return base


class TestParseVersion:
    @pytest.mark.parametrize(
        ("text", "expected"),
        [
            ("v0.1.5", (0, 1, 5)),
            ("0.1.5", (0, 1, 5)),
            ("V0.1.5", (0, 1, 5)),
            ("v0.1.10", (0, 1, 10)),
            ("0.1.5-rc1", (0, 1, 5)),
            ("garbage", ()),
            ("", ()),
            ("v", ()),
            (None, ()),
            (42, ()),
        ],
    )
    def test_parse(self, text, expected) -> None:
        assert parse_version(text) == expected


class TestCompareVersions:
    @pytest.mark.parametrize(
        ("a", "b", "expected"),
        [
            ("v0.1.5", "v0.1.4", 1),
            ("v0.1.5", "v0.1.5", 0),
            ("v0.2.0", "v0.1.9", 1),
            ("v0.1.4", "v0.1.5", -1),
            ("v0.1", "v0.1.0", 0),  # missing segments pad with zero
            ("v0.1.10", "v0.1.9", 1),  # numeric, not lexicographic
        ],
    )
    def test_compare(self, a, b, expected) -> None:
        assert compare_versions(a, b) == expected


class TestAssetPlatformKey:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("VieNeuTTS-0.2.0-windows-x64.zip", "windows-x64"),
            ("VieNeuTTS-0.2.0-linux-x64.zip", "linux-x64"),
            ("VieNeuTTS-0.2.0-macos-arm64.dmg", "macos-arm64"),
            # CUDA variants are distinct platform keys: a CUDA build must be
            # offered the CUDA artifact, never silently the CPU zip.
            ("VieNeuTTS-0.2.0-windows-x64-cuda.zip", "windows-x64-cuda"),
            ("VieNeuTTS-0.2.0-linux-x64-cuda.zip", "linux-x64-cuda"),
            ("checksums.txt", None),
            ("README.md", None),
            (None, None),
        ],
    )
    def test_match(self, name, expected) -> None:
        assert asset_platform_key(name) == expected

    def test_display_names(self) -> None:
        assert platform_display_name("windows-x64") == "Windows"
        assert platform_display_name("linux-x64") == "Linux"
        assert platform_display_name("macos-arm64") == "macOS"
        assert platform_display_name("windows-x64-cuda") == "Windows (CUDA)"
        assert platform_display_name("linux-x64-cuda") == "Linux (CUDA)"

    def test_cuda_variant_not_swallowed_by_plain_key(self) -> None:
        # Ordering matters: "-cuda" contains the plain substring, so a plain
        # match would steal the CUDA artifact and offer the wrong download.
        assert asset_platform_key("VieNeuTTS-0.2.0-windows-x64-cuda.zip") != "windows-x64"
        assert asset_platform_key("VieNeuTTS-0.2.0-linux-x64-cuda.zip") != "linux-x64"


class TestCurrentPlatformKey:
    """CUDA marker file flips the release-asset key (frozen GPU builds)."""

    def test_cuda_marker_adds_suffix_on_windows(self, monkeypatch) -> None:
        from vienetts_app.core import updates

        monkeypatch.setattr(updates, "_is_cuda_build", lambda: True)
        monkeypatch.setattr(updates.sys, "platform", "win32")
        monkeypatch.setattr(updates._platform, "machine", lambda: "AMD64")
        assert updates.current_platform_key() == "windows-x64-cuda"

    def test_plain_key_without_marker_on_windows(self, monkeypatch) -> None:
        from vienetts_app.core import updates

        monkeypatch.setattr(updates, "_is_cuda_build", lambda: False)
        monkeypatch.setattr(updates.sys, "platform", "win32")
        monkeypatch.setattr(updates._platform, "machine", lambda: "AMD64")
        assert updates.current_platform_key() == "windows-x64"

    def test_cuda_marker_adds_suffix_on_linux(self, monkeypatch) -> None:
        from vienetts_app.core import updates

        monkeypatch.setattr(updates, "_is_cuda_build", lambda: True)
        monkeypatch.setattr(updates.sys, "platform", "linux")
        monkeypatch.setattr(updates._platform, "machine", lambda: "x86_64")
        assert updates.current_platform_key() == "linux-x64-cuda"

    def test_macos_never_cuda(self, monkeypatch) -> None:
        from vienetts_app.core import updates

        monkeypatch.setattr(updates, "_is_cuda_build", lambda: True)
        monkeypatch.setattr(updates.sys, "platform", "darwin")
        monkeypatch.setattr(updates._platform, "machine", lambda: "arm64")
        assert updates.current_platform_key() == "macos-arm64"


class TestCheckForUpdates:
    def test_newer_release_matches_platform_asset(self) -> None:
        info = check_for_updates("0.1.5", platform_key="linux-x64", fetcher=lambda url: _payload())
        assert info.available is True
        assert info.latest_version == "v0.2.0"
        assert info.platform_asset is not None
        assert info.platform_asset.name == "VieNeuTTS-0.2.0-linux-x64.zip"
        assert info.platform_asset.url == "https://example.com/lin"
        assert {a.name for a in info.other_assets} == {
            "VieNeuTTS-0.2.0-windows-x64.zip",
            "VieNeuTTS-0.2.0-windows-x64-cuda.zip",
            "VieNeuTTS-0.2.0-linux-x64-cuda.zip",
            "VieNeuTTS-0.2.0-macos-arm64.dmg",
        }
        assert info.release_url.startswith("https://")

    def test_each_platform_gets_its_own_file(self) -> None:
        for key, suffix in [
            ("windows-x64", "windows-x64.zip"),
            ("linux-x64", "linux-x64.zip"),
            ("macos-arm64", "macos-arm64.dmg"),
            ("windows-x64-cuda", "windows-x64-cuda.zip"),
            ("linux-x64-cuda", "linux-x64-cuda.zip"),
        ]:
            info = check_for_updates("0.1.5", platform_key=key, fetcher=lambda url: _payload())
            assert info.platform_asset is not None
            assert info.platform_asset.name.endswith(suffix)

    def test_current_version_reports_no_update(self) -> None:
        info = check_for_updates("0.2.0", platform_key="linux-x64", fetcher=lambda url: _payload())
        assert info.available is False
        assert info.error == ""
        assert info.latest_version == "v0.2.0"

    def test_no_platform_match_still_announces_with_others(self) -> None:
        payload = _payload(
            assets=[
                {
                    "name": "VieNeuTTS-0.2.0-windows-x64.zip",
                    "browser_download_url": "https://example.com/win",
                }
            ]
        )
        info = check_for_updates("0.1.5", platform_key="linux-x64", fetcher=lambda url: payload)
        assert info.available is True
        assert info.platform_asset is None
        assert len(info.other_assets) == 1

    def test_network_failure_is_total_not_raising(self) -> None:
        def boom(url):
            raise OSError("no network")

        info = check_for_updates("0.1.5", platform_key="linux-x64", fetcher=boom)
        assert isinstance(info, UpdateInfo)
        assert info.available is False
        assert info.error == "no network"

    def test_malformed_payload_is_an_error(self) -> None:
        info = check_for_updates("0.1.5", platform_key="linux-x64", fetcher=lambda url: [])
        assert info.available is False
        assert info.error != ""
