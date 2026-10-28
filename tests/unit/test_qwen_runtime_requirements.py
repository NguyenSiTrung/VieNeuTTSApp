"""Qwen runtime requirements manifest: cross-file schema guards.

`packaging/qwen-runtime-requirements.json` is the maintainer input that
`scripts/lock_qwen_runtime.py` (Task 2.2) consumes to render per-platform,
checksum-pinned runtime manifests. These tests keep the two guards that have
caught real breakage without re-asserting the manifest against itself:
the probe schema version and the wheel-bearing ``sox`` pin.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from scripts.spike import qwen_runtime_probe as probe

REQUIREMENTS_PATH = Path(__file__).parents[2] / "packaging" / "qwen-runtime-requirements.json"


@pytest.fixture(scope="module")
def requirements() -> dict:
    return json.loads(REQUIREMENTS_PATH.read_text(encoding="utf-8"))


class TestMatrix:
    def test_probe_schema_version_matches_the_probe_script(self, requirements) -> None:
        assert requirements["probeSchemaVersion"] == probe.SCHEMA_VERSION

    def test_every_platform_pins_the_wheel_bearing_sox(self, requirements) -> None:
        # qwen-tts imports `sox` at module load and pysox 1.5.0 is sdist-only, so
        # dropping this pin silently empties the module out of the runtime.
        for entry in requirements["platforms"]:
            assert "sox==1.4.1" in entry["requirements"], entry["key"]
        assert requirements["directRequirements"]["sox"] == "1.4.1"
