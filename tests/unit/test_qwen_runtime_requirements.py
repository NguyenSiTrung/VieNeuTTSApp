"""Qwen runtime requirements manifest: schema, resolver inputs, probe evidence.

`packaging/qwen-runtime-requirements.json` is the maintainer input that
`scripts/lock_qwen_runtime.py` (Task 2.2) consumes to render per-platform,
checksum-pinned runtime manifests. These tests keep it honest without ever
installing Qwen into the base environment:

- the platform matrix may not be silently reduced (spec: revise the spec first),
- every platform must pin the runtime it resolves against,
- any platform marked ``verified`` must point at a real probe result that
  matches the probe schema from Task 0.2.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.spike import qwen_runtime_probe as probe

REQUIREMENTS_PATH = Path(__file__).parents[2] / "packaging" / "qwen-runtime-requirements.json"

# The complete release matrix from the spec. Exactly these keys, nothing else.
REQUIRED_PLATFORMS = {
    "windows-x64-cpu": ("win_amd64", "cpu"),
    "windows-x64-cuda": ("win_amd64", "cuda"),
    "linux-x64-cpu": ("linux_x86_64", "cpu"),
    "linux-x64-cuda": ("linux_x86_64", "cuda"),
    "macos-arm64-cpu": ("macosx_11_0_arm64", "cpu"),
    "macos-arm64-mps": ("macosx_11_0_arm64", "mps"),
}
PYTHON_TAGS = ("cp310", "cp311", "cp312", "cp313")
# Local version suffix carried by wheels from each index.
INDEX_SUFFIX = {"pypi": "", "torchCpu": "+cpu", "torchCu128": "+cu128"}


@pytest.fixture(scope="module")
def requirements() -> dict:
    return json.loads(REQUIREMENTS_PATH.read_text(encoding="utf-8"))


class TestMatrix:
    def test_matrix_is_not_reduced_and_covers_required_platforms(self, requirements) -> None:
        platforms = {entry["key"]: entry for entry in requirements["platforms"]}
        assert set(platforms) == set(REQUIRED_PLATFORMS)
        for key, (tag, device) in REQUIRED_PLATFORMS.items():
            assert platforms[key]["platformTag"] == tag
            assert platforms[key]["device"] == device

    def test_python_tags_and_runtime_pins_are_recorded(self, requirements) -> None:
        assert tuple(requirements["pythonTags"]) == PYTHON_TAGS
        assert requirements["pythonRequires"] == ">=3.10,<3.14"
        assert requirements["probeSchemaVersion"] == probe.SCHEMA_VERSION

    def test_every_platform_pins_qwen_and_the_platform_torch(self, requirements) -> None:
        indexes = requirements["indexes"]
        for entry in requirements["platforms"]:
            assert entry["index"] in indexes, entry["key"]
            pins = " ".join(entry["requirements"])
            assert "qwen-tts==0.1.1" in pins
            assert "transformers==4.57.3" in pins
            assert "accelerate==1.12.0" in pins
            suffix = INDEX_SUFFIX[entry["index"]]
            assert f"torch==2.8.0{suffix}" in pins, entry["key"]
            assert f"torchaudio==2.8.0{suffix}" in pins, entry["key"]
            assert entry["attention"] in ("sdpa", "flash_attention_2")
            assert entry["dtype"] in ("float32", "bfloat16")

    def test_cuda_platforms_use_the_cu128_index_and_others_do_not(self, requirements) -> None:
        for entry in requirements["platforms"]:
            if entry["device"] == "cuda":
                assert entry["index"] == "torchCu128"
            elif entry["platformTag"].startswith("macosx"):
                assert entry["index"] == "pypi"
            else:
                assert entry["index"] == "torchCpu"


class TestEvidence:
    def test_pending_list_matches_the_platform_statuses(self, requirements) -> None:
        pending = [
            entry["key"]
            for entry in requirements["platforms"]
            if entry["evidence"]["status"] == "pending"
        ]
        assert requirements["pendingPlatforms"] == pending
        assert set(pending) <= set(REQUIRED_PLATFORMS)

    def test_every_platform_records_a_probe_command_and_evidence_path(self, requirements) -> None:
        for entry in requirements["platforms"]:
            evidence = entry["evidence"]
            assert evidence["status"] in ("verified", "pending")
            assert "--profile customvoice" in evidence["probeCommand"]
            assert "--profile base" in evidence["probeCommand"]
            assert evidence["evidencePath"].endswith(".json")

    def test_verified_platforms_point_at_real_probe_results(self, requirements) -> None:
        for entry in requirements["platforms"]:
            evidence = entry["evidence"]
            if evidence["status"] != "verified":
                continue
            path = REQUIREMENTS_PATH.parents[1] / evidence["evidencePath"]
            assert path.is_file(), f"{entry['key']} claims verified but {path} is missing"
            payload = json.loads(path.read_text(encoding="utf-8"))
            assert payload["schemaVersion"] == probe.SCHEMA_VERSION
            assert payload["errors"] == []
            assert payload["metrics"]["audioSeconds"] > 0
            assert payload["metrics"]["peakRssMb"] > 0
            assert payload["shutdown"]["clean"] is True
