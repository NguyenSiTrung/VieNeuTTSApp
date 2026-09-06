"""Pinned CUDA runtime manifest contracts."""

from __future__ import annotations

from dataclasses import replace

import pytest

from vienetts_app.core import cuda_runtime_manifest
from vienetts_app.core.cuda_runtime_manifest import manifest_for_platform


def test_supported_manifests_contain_unique_verified_direct_wheels() -> None:
    for platform_key in ("windows-x64", "linux-x64"):
        manifest = manifest_for_platform(platform_key)

        assert manifest is not None
        assert manifest.platform_key == platform_key
        assert manifest.python_tag == "cp313"
        assert manifest.wheels
        assert len({wheel.filename for wheel in manifest.wheels}) == len(manifest.wheels)
        for wheel in manifest.wheels:
            assert wheel.url.startswith(
                (
                    "https://download.pytorch.org/whl/cu128/",
                    "https://files.pythonhosted.org/packages/",
                )
            )
            assert wheel.url.endswith(wheel.filename)
            assert len(wheel.sha256) == 64
            assert int(wheel.sha256, 16) >= 0
            assert wheel.size_bytes > 0


def test_unsupported_platform_has_no_cuda_manifest() -> None:
    assert manifest_for_platform("macos-arm64") is None
    assert manifest_for_platform("linux-arm64") is None


@pytest.mark.parametrize(
    ("invalid_manifest", "reason"),
    [
        (
            lambda manifest: replace(manifest, platform_key="other-platform"),
            "platform key",
        ),
        (
            lambda manifest: replace(
                manifest,
                wheels=(
                    replace(manifest.wheels[0], url="http://files.pythonhosted.org/wheel.whl"),
                    *manifest.wheels[1:],
                ),
            ),
            "HTTPS",
        ),
        (
            lambda manifest: replace(
                manifest,
                wheels=(
                    replace(manifest.wheels[0], url="https://example.com/wheel.whl"),
                    *manifest.wheels[1:],
                ),
            ),
            "host",
        ),
        (
            lambda manifest: replace(
                manifest,
                wheels=(
                    replace(manifest.wheels[0], url="https://files.pythonhosted.org/packages/"),
                    *manifest.wheels[1:],
                ),
            ),
            "wheel artifact",
        ),
        (
            lambda manifest: replace(
                manifest,
                wheels=(
                    manifest.wheels[0],
                    replace(
                        manifest.wheels[1],
                        filename=manifest.wheels[0].filename,
                        url=manifest.wheels[0].url,
                    ),
                    *manifest.wheels[2:],
                ),
            ),
            "duplicate",
        ),
        (
            lambda manifest: replace(
                manifest,
                wheels=(replace(manifest.wheels[0], sha256="not-a-sha256"), *manifest.wheels[1:]),
            ),
            "SHA-256",
        ),
        (
            lambda manifest: replace(
                manifest,
                wheels=(replace(manifest.wheels[0], size_bytes=0), *manifest.wheels[1:]),
            ),
            "size",
        ),
    ],
)
def test_manifest_validator_rejects_invalid_manifest_data(invalid_manifest, reason: str) -> None:
    manifest = manifest_for_platform("linux-x64")
    assert manifest is not None

    with pytest.raises(ValueError, match=reason):
        cuda_runtime_manifest._validate_manifests({"linux-x64": invalid_manifest(manifest)})
