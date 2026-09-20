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
    def test_every_matrix_platform_has_a_manifest(self) -> None:
        for platform_key in EXPECTED_PLATFORMS:
            manifest = qm.manifest_for_platform(platform_key)
            assert manifest is not None, platform_key
            assert manifest.platform_key == platform_key
            assert manifest.wheels, platform_key

    def test_platforms_match_the_requirements_input(self) -> None:
        requirements = json.loads(REQUIREMENTS.read_text(encoding="utf-8"))
        for entry in requirements["platforms"]:
            manifest = qm.manifest_for_platform(entry["key"])
            assert manifest is not None, entry["key"]
            assert manifest.device == entry["device"]
            assert manifest.python_tag in requirements["pythonTags"]
            assert manifest.platform_tag == entry["platformTag"]

    def test_unknown_platform_is_none(self) -> None:
        assert qm.manifest_for_platform("plan9-x64-cpu") is None
        assert qm.manifest_for_platform("") is None

    def test_wheels_are_https_pinned_artifacts(self) -> None:
        for platform_key in EXPECTED_PLATFORMS:
            manifest = qm.manifest_for_platform(platform_key)
            assert manifest is not None
            filenames = [wheel.filename for wheel in manifest.wheels]
            assert len(filenames) == len(set(filenames)), platform_key
            for wheel in manifest.wheels:
                assert wheel.url.startswith("https://")
                assert len(wheel.sha256) == 64
                assert wheel.size_bytes > 0
                assert wheel.filename.endswith(".whl")
            assert manifest.total_bytes == sum(wheel.size_bytes for wheel in manifest.wheels)

    def test_each_manifest_pins_the_required_runtime_packages(self) -> None:
        for platform_key in EXPECTED_PLATFORMS:
            manifest = qm.manifest_for_platform(platform_key)
            assert manifest is not None
            names = {qm.distribution_name(wheel.filename) for wheel in manifest.wheels}
            assert "qwen-tts" in names, platform_key
            assert "transformers" in names, platform_key
            assert "torch" in names, platform_key
            assert "torchaudio" in names, platform_key
            assert manifest.pins["qwen-tts"] == "0.1.1"
            assert manifest.pins["transformers"] == "4.57.3"
            assert manifest.pins["torch"] == manifest.torch_local_version

    def test_cuda_manifests_pin_cu128_wheels_and_cpu_ones_do_not(self) -> None:
        for platform_key in EXPECTED_PLATFORMS:
            manifest = qm.manifest_for_platform(platform_key)
            assert manifest is not None
            torch_wheel = manifest.wheel_for("torch")
            assert torch_wheel is not None
            if manifest.device == "cuda":
                assert manifest.torch_local_version == "2.8.0+cu128"
                assert "cu128" in torch_wheel.filename
                assert torch_wheel.url.startswith("https://download.pytorch.org/whl/cu128/")
            elif manifest.platform_tag.startswith("macosx"):
                assert manifest.torch_local_version == "2.8.0"
                assert "+" not in torch_wheel.filename
            else:
                assert manifest.torch_local_version == "2.8.0+cpu"
                assert "cpu" in torch_wheel.filename

    def test_sdist_only_packages_are_recorded_rather_than_silently_dropped(self) -> None:
        for platform_key in EXPECTED_PLATFORMS:
            manifest = qm.manifest_for_platform(platform_key)
            assert manifest is not None
            for record in manifest.sdist_only:
                assert record.name
                assert record.reason


class TestDriftValidation:
    def test_validation_rejects_url_and_digest_drift(self) -> None:
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

    def test_validation_rejects_duplicate_wheels_and_unknown_devices(self) -> None:
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

    def test_distribution_name_parses_wheel_filenames(self) -> None:
        assert (
            qm.distribution_name("torch-2.8.0+cu128-cp312-cp312-manylinux_2_28_x86_64.whl")
            == "torch"
        )
        assert qm.distribution_name("qwen_tts-0.1.1-py3-none-any.whl") == "qwen-tts"
