"""Qwen runtime manifest: pinned data, platform selection, drift validation.

The manifest data ships inside the package
(``src/vienetts_app/core/qwen_runtime_manifests.json``) and is rendered from
the Phase 0 input (``packaging/qwen-runtime-requirements.json``) by
``scripts/lock_qwen_runtime.py``, so the module stays data-driven and the lock
step is reviewable as a diff.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vienetts_app.core import qwen_runtime_manifest as qm

REQUIREMENTS = Path(__file__).parents[2] / "packaging" / "qwen-runtime-requirements.json"

EXPECTED_PLATFORMS = (
    "windows-x64-cpu",
    "windows-x64-cuda",
    "linux-x64-cpu",
    "linux-x64-cuda",
    "macos-arm64-cpu",
    "macos-arm64-mps",
)


class TestManifestData:
    def test_manifest_matrix(self) -> None:
        # The shipped JSON silently loads as {} when any platform fails
        # validation, so "every platform resolves" is the schema/URL/hash guard.
        requirements = json.loads(REQUIREMENTS.read_text(encoding="utf-8"))
        for entry in requirements["platforms"]:
            manifest = qm.manifest_for_platform(entry["key"])
            assert manifest is not None, entry["key"]
            assert manifest.platform_key == entry["key"]
            assert manifest.device == entry["device"]
            assert manifest.python_tag in requirements["pythonTags"]
            assert manifest.platform_tag == entry["platformTag"]

        assert qm.manifest_for_platform("plan9-x64-cpu") is None
        assert qm.manifest_for_platform("") is None

    def test_manifest_pins(self) -> None:
        for platform_key in EXPECTED_PLATFORMS:
            manifest = qm.manifest_for_platform(platform_key)
            assert manifest is not None
            names = {qm.distribution_name(wheel.filename) for wheel in manifest.wheels}
            assert {"qwen-tts", "transformers", "torch", "torchaudio"} <= names, platform_key
            assert manifest.pins["torch"] == manifest.torch_local_version

    def test_sox_ships_as_a_wheel_on_every_platform(self) -> None:
        # qwen-tts imports `sox` while loading its core package, so a manifest
        # without a sox wheel makes every profile fail with
        # "No module named 'sox'" inside the isolated runtime.
        for platform_key in EXPECTED_PLATFORMS:
            manifest = qm.manifest_for_platform(platform_key)
            assert manifest is not None
            wheel = manifest.wheel_for("sox")
            assert wheel is not None, platform_key
            assert not [record for record in manifest.sdist_only if record.name == "sox"], (
                platform_key
            )


class TestHostDetection:
    """Task 6.1: which pinned variant THIS host can install, per device.

    The release matrix covers Windows/Linux x64 and Apple Silicon only, so the
    host is pinned with monkeypatch instead of asserting the CI machine.
    """

    @pytest.mark.parametrize(
        ("platform", "machine", "tag"),
        [
            ("win32", "AMD64", "win_amd64"),
            ("win32", "arm64", None),
            ("darwin", "arm64", "macosx_11_0_arm64"),
            ("darwin", "x86_64", None),  # an Intel Mac has no pinned wheels
            ("linux", "x86_64", "linux_x86_64"),
            ("linux", "aarch64", None),  # e.g. the Oracle/Ampere CI box
            ("freebsd", "x86_64", None),
        ],
    )
    def test_host_platform_tag(
        self, monkeypatch: pytest.MonkeyPatch, platform: str, machine: str, tag: str | None
    ) -> None:
        monkeypatch.setattr(qm.sys, "platform", platform)
        monkeypatch.setattr(qm.platform, "machine", lambda: machine)
        assert qm.host_platform_tag() == tag

    def test_host_keys(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(qm, "host_platform_tag", lambda: "linux_x86_64")
        assert qm.host_platform_key("cpu") == "linux-x64-cpu"
        assert qm.host_platform_key("cuda") == "linux-x64-cuda"
        assert qm.host_platform_key("mps") is None  # no MPS wheels for Linux
        assert qm.host_platform_key("tpu") is None  # not a device at all

        monkeypatch.setattr(qm, "host_platform_tag", lambda: None)
        assert qm.host_platform_key("cpu") is None
        assert qm.host_devices() == ()

    def test_host_devices(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(qm, "host_platform_tag", lambda: "macosx_11_0_arm64")
        assert qm.host_devices() == ("cpu", "mps")
        monkeypatch.setattr(qm, "host_platform_tag", lambda: "win_amd64")
        assert qm.host_devices() == ("cpu", "cuda")

        for tag in ("win_amd64", "linux_x86_64", "macosx_11_0_arm64"):
            monkeypatch.setattr(qm, "host_platform_tag", lambda tag=tag: tag)
            for device in qm.host_devices():
                key = qm.host_platform_key(device)
                assert key is not None, (tag, device)
                assert qm.manifest_for_platform(key) is not None, key

        assert qm.platform_label("linux-x64-cuda") == "Linux x64 · CUDA"
        assert qm.platform_label("macos-arm64-mps") == "macOS arm64 · MPS"
        assert qm.platform_label("windows-x64-cpu") == "Windows x64 · CPU"
        assert qm.platform_label("") == ""
        assert qm.platform_label("plan9-x64-cpu") == ""


class TestDriftValidation:
    def test_drift_validation(self) -> None:
        good = qm.manifest_for_platform("linux-x64-cpu")
        assert good is not None
        wheel = good.wheel_for("torch")
        assert wheel is not None
        payload = {
            "formatVersion": good.format_version,
            "platforms": {
                "linux-x64-cpu": {
                    "platformTag": good.platform_tag,
                    "device": good.device,
                    "pythonTag": good.python_tag,
                    "torchLocalVersion": good.torch_local_version,
                    "pins": dict(good.pins),
                    "wheels": [
                        {
                            "filename": wheel.filename,
                            "url": wheel.url,
                            "sizeBytes": wheel.size_bytes,
                            "sha256": wheel.sha256,
                        }
                    ],
                    "sdistOnly": [],
                }
            },
        }

        def build(data: dict) -> dict:
            return qm.load_manifests_from_data(data)

        assert build(payload)["linux-x64-cpu"].wheels[0].sha256 == wheel.sha256

        payload["platforms"]["linux-x64-cpu"]["wheels"][0]["sha256"] = "abc"
        with pytest.raises(ValueError, match="SHA-256"):
            build(payload)

        payload["platforms"]["linux-x64-cpu"]["wheels"][0]["sha256"] = wheel.sha256
        payload["platforms"]["linux-x64-cpu"]["wheels"][0]["url"] = "http://x/y.whl"
        with pytest.raises(ValueError, match="HTTPS"):
            build(payload)

        payload["platforms"]["linux-x64-cpu"]["wheels"][0]["url"] = (
            "https://evil.example.com/packages/y.whl"
        )
        with pytest.raises(ValueError, match="host"):
            build(payload)

        good = qm.manifest_for_platform("linux-x64-cpu")
        assert good is not None
        wheel = good.wheel_for("torch")
        assert wheel is not None
        entry = {
            "platformTag": good.platform_tag,
            "device": good.device,
            "pythonTag": good.python_tag,
            "torchLocalVersion": good.torch_local_version,
            "pins": dict(good.pins),
            "wheels": [
                {
                    "filename": wheel.filename,
                    "url": wheel.url,
                    "sizeBytes": wheel.size_bytes,
                    "sha256": wheel.sha256,
                },
                {
                    "filename": wheel.filename,
                    "url": wheel.url,
                    "sizeBytes": wheel.size_bytes,
                    "sha256": wheel.sha256,
                },
            ],
            "sdistOnly": [],
        }
        with pytest.raises(ValueError, match="duplicate"):
            qm.load_manifests_from_data(
                {"formatVersion": "qwen-runtime-v1", "platforms": {"linux-x64-cpu": entry}}
            )

        entry["wheels"] = entry["wheels"][:1]
        entry["device"] = "tpu"
        with pytest.raises(ValueError, match="device"):
            qm.load_manifests_from_data(
                {"formatVersion": "qwen-runtime-v1", "platforms": {"linux-x64-cpu": entry}}
            )

        entry["device"] = "cpu"
        entry["platformTag"] = "linux_x86_64"
        with pytest.raises(ValueError, match="platform"):
            qm.load_manifests_from_data(
                {"formatVersion": "qwen-runtime-v1", "platforms": {"plan9-x64-cpu": entry}}
            )

        assert (
            qm.distribution_name("torch-2.8.0+cu128-cp312-cp312-manylinux_2_28_x86_64.whl")
            == "torch"
        )
        assert qm.distribution_name("qwen_tts-0.1.1-py3-none-any.whl") == "qwen-tts"
