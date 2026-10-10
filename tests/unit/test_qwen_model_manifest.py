"""Qwen model manifest: pins, shared files, drift validation."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from vienetts_app.core import qwen_model_manifest as qm

REQUIREMENTS = Path(__file__).parents[2] / "packaging" / "qwen-runtime-requirements.json"


class TestShippedManifest:
    def test_both_profiles_are_pinned_to_immutable_revisions(self) -> None:
        requirements = json.loads(REQUIREMENTS.read_text(encoding="utf-8"))["modelPins"]
        for key in qm.PROFILE_KEYS:
            profile = qm.profile_for(key)
            assert profile is not None, key
            assert profile.repo == requirements[key]["repo"]
            assert profile.revision == requirements[key]["revision"]
            assert profile.files
            assert profile.shared

    def test_shared_files_are_identical_for_both_profiles(self) -> None:
        customvoice = qm.profile_for("customvoice")
        base = qm.profile_for("base")
        assert customvoice is not None and base is not None
        assert customvoice.shared == base.shared

    def test_shared_revision_follows_the_customvoice_profile(self) -> None:
        assert qm.MANIFEST is not None
        assert qm.MANIFEST.shared_revision == qm.profile_for("customvoice").revision

    def test_unknown_profile_key_has_no_profile_and_no_bytes(self) -> None:
        assert qm.profile_for("nope") is None
        assert qm.total_bytes_for("nope") == 0


class TestValidation:
    def _payload(self) -> dict:
        profile = qm.profile_for("base")
        assert profile is not None
        return {
            "formatVersion": qm.FORMAT_VERSION,
            "shared": {
                "repo": "Qwen/shared",
                "revision": "a" * 40,
                "files": [
                    {"path": "vocab.json", "sizeBytes": 10, "sha256": "b" * 64},
                ],
            },
            "profiles": {
                "customvoice": {
                    "repo": "Qwen/customvoice",
                    "revision": "c" * 40,
                    "files": [{"path": "config.json", "sizeBytes": 5, "sha256": "d" * 64}],
                },
                "base": {
                    "repo": "Qwen/base",
                    "revision": "e" * 40,
                    "files": [{"path": "config.json", "sizeBytes": 5, "sha256": "f" * 64}],
                },
            },
        }

    def test_valid_payload_loads_with_shared_files_attached(self) -> None:
        manifest = qm.load_manifest_from_data(self._payload())
        assert manifest.profile_for("base").shared[0].path == "vocab.json"
        assert manifest.shared_revision == "a" * 40

    def test_drift_is_rejected(self) -> None:
        payload = self._payload()
        payload["profiles"]["base"]["revision"] = "main"
        with pytest.raises(ValueError, match="immutable commit"):
            qm.load_manifest_from_data(payload)

        payload = self._payload()
        payload["profiles"]["base"]["files"][0]["sha256"] = "xyz"
        with pytest.raises(ValueError, match="SHA-256"):
            qm.load_manifest_from_data(payload)

        payload = self._payload()
        payload["profiles"]["base"]["files"][0]["path"] = "../escape.safetensors"
        with pytest.raises(ValueError, match="unsafe"):
            qm.load_manifest_from_data(payload)

        payload = self._payload()
        payload["profiles"]["base"]["files"][0]["path"] = "vocab.json"
        with pytest.raises(ValueError, match="duplicate shared"):
            qm.load_manifest_from_data(payload)

    def test_missing_profile_and_bad_format_are_rejected(self) -> None:
        payload = self._payload()
        del payload["profiles"]["base"]
        with pytest.raises(ValueError, match="missing profiles"):
            qm.load_manifest_from_data(payload)

        payload = self._payload()
        payload["formatVersion"] = "qwen-model-v0"
        with pytest.raises(ValueError, match="unsupported"):
            qm.load_manifest_from_data(payload)
