"""Update check: version compare, platform asset matching, payload parsing.

Pure ``core.updates`` tests — no network, no Qt. The fetcher seam takes
the URL and returns decoded JSON, so every GitHub shape is a dict literal.
"""

from __future__ import annotations

import http.client
import urllib.error
import urllib.request

import pytest

from vienetts_app.core import updates
from vienetts_app.core.updates import UpdateInfo, check_for_updates

_RELEASE_DL = "https://github.com/NguyenSiTrung/VieNeuTTSApp/releases/download/v0.2.0"


def _payload(**overrides):
    base = {
        "tag_name": "v0.2.0",
        "html_url": "https://github.com/NguyenSiTrung/VieNeuTTSApp/releases/tag/v0.2.0",
        "body": "notes",
        "assets": [
            {
                "name": "VieNeuTTS-0.2.0-windows-x64.zip",
                "browser_download_url": f"{_RELEASE_DL}/VieNeuTTS-0.2.0-windows-x64.zip",
                "size": 11,
            },
            {
                "name": "VieNeuTTS-0.2.0-linux-x64.zip",
                "browser_download_url": f"{_RELEASE_DL}/VieNeuTTS-0.2.0-linux-x64.zip",
                "size": 22,
            },
            {
                "name": "VieNeuTTS-0.2.0-macos-arm64.dmg",
                "browser_download_url": f"{_RELEASE_DL}/VieNeuTTS-0.2.0-macos-arm64.dmg",
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
        assert info.platform_asset.url == f"{_RELEASE_DL}/VieNeuTTS-0.2.0-linux-x64.zip"
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
                    "browser_download_url": f"{_RELEASE_DL}/VieNeuTTS-0.2.0-windows-x64.zip",
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


class TestUpdateSecurityHardening:
    def test_url_allowlist_accepts_github_and_rejects_untrusted(self) -> None:
        payload = _payload(
            html_url="https://evil.example.com/NguyenSiTrung/VieNeuTTSApp/releases/tag/v0.2.0",
            assets=[
                {
                    "name": "VieNeuTTS-0.2.0-linux-x64.zip",
                    "browser_download_url": "https://objects.githubusercontent.com/github-production-release-asset-1/a",
                    "size": 10,
                },
                {
                    "name": "VieNeuTTS-0.2.0-windows-x64.zip",
                    "browser_download_url": "https://release-assets.githubusercontent.com/github-production-release-asset-2/b",
                    "size": 20,
                },
                {
                    "name": "VieNeuTTS-0.2.0-macos-arm64.dmg",
                    "browser_download_url": "https://evil.example.com/mac.dmg",
                    "size": 30,
                },
                {
                    "name": "downgrade-windows-x64.zip",
                    "browser_download_url": "http://github.com/NguyenSiTrung/VieNeuTTSApp/releases/download/v0.2.0/x.zip",
                },
                {
                    "name": "wrong-repo-windows-x64.zip",
                    "browser_download_url": "https://github.com/Attacker/Fork/releases/download/v0.2.0/x.zip",
                },
                {
                    "name": "file-scheme-windows-x64.zip",
                    "browser_download_url": "file:///etc/passwd",
                },
                {
                    "name": "traversal-windows-x64.zip",
                    "browser_download_url": "https://github.com/NguyenSiTrung/VieNeuTTSApp/releases/../../Attacker/x.zip",
                },
            ],
        )
        info = check_for_updates("0.1.5", platform_key="linux-x64", fetcher=lambda _u: payload)
        assert info.available is True
        assert info.release_url == ""
        assert info.platform_asset is not None
        assert info.platform_asset.url.startswith("https://objects.githubusercontent.com/")
        assert [a.name for a in info.other_assets] == ["VieNeuTTS-0.2.0-windows-x64.zip"]

    def test_http_get_json_caps_size_and_catches_decode_and_http_errors(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        class FakeResponse:
            def __init__(self, payload: bytes | Exception, url: str = updates.LATEST_RELEASE_URL):
                self._payload = payload
                self._url = url

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def geturl(self) -> str:
                return self._url

            def read(self, n: int = -1) -> bytes:
                if isinstance(self._payload, Exception):
                    raise self._payload
                return self._payload if n < 0 else self._payload[:n]

        class FakeOpener:
            def __init__(self, resp: FakeResponse):
                self._resp = resp

            def open(self, req, timeout=None):
                return self._resp

        # Oversized response (> 2 MiB) is rejected.
        oversized = b"x" * (2 * 1024 * 1024 + 10)
        monkeypatch.setattr(
            urllib.request, "build_opener", lambda *_a: FakeOpener(FakeResponse(oversized))
        )
        with pytest.raises(ValueError, match="oversized"):
            updates._http_get_json(updates.LATEST_RELEASE_URL)

        # Invalid UTF-8 bytes raise ValueError instead of uncaught UnicodeDecodeError.
        monkeypatch.setattr(
            urllib.request, "build_opener", lambda *_a: FakeOpener(FakeResponse(b"\xff\xfe\x80"))
        )
        with pytest.raises(ValueError, match="unexpected response"):
            updates._http_get_json(updates.LATEST_RELEASE_URL)

        # http.client.HTTPException (e.g. IncompleteRead) becomes ConnectionError.
        monkeypatch.setattr(
            urllib.request,
            "build_opener",
            lambda *_a: FakeOpener(FakeResponse(http.client.IncompleteRead(b"part"))),
        )
        with pytest.raises(ConnectionError, match="no connection"):
            updates._http_get_json(updates.LATEST_RELEASE_URL)

    def test_redirect_handler_blocks_https_downgrade(self) -> None:
        handler = updates._HttpsOnlyRedirectHandler()
        req = urllib.request.Request(updates.LATEST_RELEASE_URL)
        with pytest.raises(urllib.error.URLError, match="non-HTTPS"):
            handler.redirect_request(
                req, None, 302, "Found", {}, "http://api.github.com/repos/x/y/releases/latest"
            )
        with pytest.raises(urllib.error.URLError, match="non-HTTPS"):
            handler.redirect_request(req, None, 302, "Found", {}, "file:///etc/passwd")
        allowed = handler.redirect_request(
            req, None, 302, "Found", {}, "https://api.github.com/repositories/1/releases/latest"
        )
        assert allowed is not None
        assert allowed.full_url == "https://api.github.com/repositories/1/releases/latest"
