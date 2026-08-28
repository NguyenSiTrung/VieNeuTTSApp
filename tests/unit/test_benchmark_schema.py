import json
from pathlib import Path

import pytest
from scripts.benchmarks.corpus import get_corpus_entry
from scripts.benchmarks.schema import (
    BenchmarkRecord,
    BenchmarkScenario,
    environment_manifest,
    write_jsonl,
)


def test_environment_excludes_identity_fields() -> None:
    manifest = environment_manifest()
    payload = manifest.to_dict()
    serialized = json.dumps(payload)
    forbidden = {"hostname", "serial", "hardware_uuid", "username", "home"}

    assert not forbidden.intersection(payload)
    assert str(Path.home()) not in serialized


def test_record_contains_corpus_identity_not_text() -> None:
    entry = get_corpus_entry("vi_50")
    scenario = BenchmarkScenario.from_entry(
        entry,
        backend="onnx",
        precision="int8",
        mode="stream",
    )

    payload = scenario.to_dict()

    assert payload["scenario_id"] == "vi_50"
    assert payload["text_sha256"] == entry.sha256
    assert payload["char_count"] == len(entry.text)
    assert entry.text not in json.dumps(payload, ensure_ascii=False)


def test_scenario_preserves_resolved_backend() -> None:
    entry = get_corpus_entry("vi_20")
    scenario = BenchmarkScenario.from_entry(
        entry,
        backend="onnx",
        resolved_backend="onnx",
        precision="int8",
        mode="infer",
    )

    assert scenario.to_dict()["resolved_backend"] == "onnx"


def test_write_jsonl_emits_one_valid_json_object_per_line(tmp_path: Path) -> None:
    entry = get_corpus_entry("vi_20")
    scenario = BenchmarkScenario.from_entry(
        entry,
        backend="onnx",
        precision="int8",
        mode="infer",
    )
    record = BenchmarkRecord(
        environment=environment_manifest(hardware_class="fake-ci"),
        scenario=scenario,
        trace={"outcome": "completed"},
        resources={"sample_count": 1},
        elapsed_ns=10,
        audio_duration_ms=2,
    )
    output = tmp_path / "records.jsonl"

    write_jsonl([record, record], output)

    lines = output.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert all(json.loads(line)["schema_version"] == 1 for line in lines)


def test_record_rejects_negative_durations_or_bytes() -> None:
    entry = get_corpus_entry("vi_20")
    scenario = BenchmarkScenario.from_entry(
        entry,
        backend="onnx",
        precision="int8",
        mode="infer",
    )

    with pytest.raises(ValueError, match="non-negative"):
        BenchmarkRecord(
            environment=environment_manifest(),
            scenario=scenario,
            trace={"outcome": "completed"},
            resources={"max_rss_bytes": -1},
            elapsed_ns=10,
            audio_duration_ms=2,
        )
