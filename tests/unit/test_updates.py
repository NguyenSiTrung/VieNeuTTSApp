"""Update check: version compare, platform asset matching, payload parsing.

Pure ``core.updates`` tests — no network, no Qt. The fetcher seam takes
the URL and returns decoded JSON, so every GitHub shape is a dict literal.
"""

from __future__ import annotations

from vienetts_app.core.updates import UpdateInfo, check_for_updates


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
                "name": "VieNeuTTS-0.2.0-linux-x64.zip",
                "browser_download_url": "https://example.com/lin",
                "size": 22,
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


class TestCurrentPlatformKey:
    """Only CPU release-asset keys are selected."""

    def test_current_platform_key_is_cpu_only(self, monkeypatch) -> None:
        from vienetts_app.core import updates

        for platform, machine, expected in [
            ("win32", "AMD64", "windows-x64"),
            ("linux", "x86_64", "linux-x64"),
            ("darwin", "arm64", "macos-arm64"),
        ]:
            monkeypatch.setattr(updates.sys, "platform", platform)
            monkeypatch.setattr(updates._platform, "machine", lambda m=machine: m)
            assert updates.current_platform_key() == expected


class TestCheckForUpdates:
    def test_happy_paths(self) -> None:
        # A newer release matches this platform's asset and lists the rest.
        info = check_for_updates("0.1.5", platform_key="linux-x64", fetcher=lambda url: _payload())
        assert info.available is True
        assert info.latest_version == "v0.2.0"
        assert info.platform_asset is not None
        assert info.platform_asset.name == "VieNeuTTS-0.2.0-linux-x64.zip"
        assert info.platform_asset.url == "https://example.com/lin"
        assert {a.name for a in info.other_assets} == {
            "VieNeuTTS-0.2.0-windows-x64.zip",
            "VieNeuTTS-0.2.0-macos-arm64.dmg",
        }
        assert info.release_url.startswith("https://")

        # The current version reports no update.
        info = check_for_updates("0.2.0", platform_key="linux-x64", fetcher=lambda url: _payload())
        assert info.available is False
        assert info.error == ""
        assert info.latest_version == "v0.2.0"

        # No platform match still announces, with the asset in others.
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

    def test_fetch_failures_are_errors_not_crashes(self) -> None:
        # A network failure is reported, never raised.
        def boom(url):
            raise OSError("no network")

        info = check_for_updates("0.1.5", platform_key="linux-x64", fetcher=boom)
        assert isinstance(info, UpdateInfo)
        assert info.available is False
        assert info.error == "no network"

        # A malformed payload is an error too.
        info = check_for_updates("0.1.5", platform_key="linux-x64", fetcher=lambda url: [])
        assert info.available is False
        assert info.error != ""
