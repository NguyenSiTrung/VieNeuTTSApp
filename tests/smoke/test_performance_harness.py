import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]


def run_benchmark(output: Path, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "scripts.benchmarks.run_once",
            "--engine",
            "fake",
            "--scenario",
            "vi_50",
            "--mode",
            "stream",
            "--output",
            str(output),
            *arguments,
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )


def run_module(module: str, *arguments: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", module, *arguments],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
    )


def read_one_record(path: Path) -> dict[str, object]:
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    return json.loads(lines[0])


def test_fake_pipeline_emits_one_content_safe_record(tmp_path: Path) -> None:
    output = tmp_path / "record.jsonl"

    proc = run_benchmark(output)

    assert proc.returncode == 0, proc.stderr
    payload = read_one_record(output)
    assert payload["schema_version"] == 1
    assert payload["scenario"]["scenario_id"] == "vi_50"
    assert payload["trace"]["outcome"] == "completed"
    names = [event["name"] for event in payload["trace"]["events"]]
    assert "worker_first_chunk" in names
    assert "controller_first_chunk" in names
    assert "audio_first_buffer_append" in names
    assert "audio_first_sink_pull" in names
    assert payload["resources"]["sample_count"] >= 1
    assert payload["event_loop"]["sample_count"] >= 1
    serialized = json.dumps(payload, ensure_ascii=False)
    assert "Đây là câu" not in serialized


def test_slow_sink_records_buffer_high_water(tmp_path: Path) -> None:
    output = tmp_path / "slow.jsonl"

    proc = run_benchmark(output, "--sink-rate", "0.1")

    assert proc.returncode == 0, proc.stderr
    payload = read_one_record(output)
    assert payload["trace"]["maxima"]["audio_buffer_bytes"] > 0
    assert payload["elapsed_ms"] < 1000


def test_in_flight_cancellation_records_terminal_events(tmp_path: Path) -> None:
    output = tmp_path / "cancelled.jsonl"

    proc = run_benchmark(output, "--cancel-after-first-chunk")

    assert proc.returncode == 0, proc.stderr
    payload = read_one_record(output)
    assert payload["trace"]["outcome"] == "cancelled"
    names = [event["name"] for event in payload["trace"]["events"]]
    assert "cancel_requested" in names
    assert "worker_cancelled" in names


def test_fake_direct_engine_record_has_no_controller_events(tmp_path: Path) -> None:
    output = tmp_path / "direct.jsonl"

    proc = run_module(
        "scripts.benchmarks.run_engine",
        "--engine",
        "fake",
        "--scenario",
        "vi_50",
        "--mode",
        "stream",
        "--output",
        str(output),
    )

    assert proc.returncode == 0, proc.stderr
    payload = read_one_record(output)
    names = [event["name"] for event in payload["trace"]["events"]]
    assert payload["trace"]["tags"]["run_kind"] == "direct_engine"
    assert "controller_first_chunk" not in names
    assert "audio_first_sink_pull" not in names
    assert "engine_first_chunk" in names


def test_direct_runner_emits_each_measured_iteration_after_warmup(tmp_path: Path) -> None:
    output = tmp_path / "direct-iterations.jsonl"

    proc = run_module(
        "scripts.benchmarks.run_engine",
        "--engine",
        "fake",
        "--scenario",
        "vi_50",
        "--warmup-iterations",
        "1",
        "--iterations",
        "2",
        "--output",
        str(output),
    )

    assert proc.returncode == 0, proc.stderr
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    assert all(record["trace"]["outcome"] == "completed" for record in records)


def test_fake_matrix_and_summary(tmp_path: Path) -> None:
    raw = tmp_path / "raw.jsonl"
    summary = tmp_path / "summary.json"

    matrix = run_module(
        "scripts.benchmarks.run_matrix",
        "--engine",
        "fake",
        "--scenario",
        "vi_50",
        "--cold-iterations",
        "2",
        "--warm-iterations",
        "0",
        "--output",
        str(raw),
    )
    assert matrix.returncode == 0, matrix.stderr

    summarized = run_module(
        "scripts.benchmarks.summarize",
        str(raw),
        "--output",
        str(summary),
    )
    assert summarized.returncode == 0, summarized.stderr
    assert len(raw.read_text(encoding="utf-8").splitlines()) == 2
    payload = json.loads(summary.read_text(encoding="utf-8"))
    assert payload["count"] == 2
    assert "ttfc_ms" in payload["distributions"]


def test_fake_startup_benchmark_emits_timing_record(tmp_path: Path) -> None:
    output = tmp_path / "startup.jsonl"

    proc = run_module(
        "scripts.benchmarks.run_startup",
        "--engine",
        "fake",
        "--iterations",
        "1",
        "--hardware-class",
        "fake-ci",
        "--output",
        str(output),
    )

    assert proc.returncode == 0, proc.stderr
    payload = read_one_record(output)
    assert payload["trace"]["tags"]["run_kind"] == "startup"
    names = [event["name"] for event in payload["trace"]["events"]]
    assert "process_started" in names
    assert "qml_loaded" in names


def test_fake_ui_benchmark_emits_frame_or_unsupported_record(tmp_path: Path) -> None:
    output = tmp_path / "ui.jsonl"

    proc = run_module(
        "scripts.benchmarks.run_ui",
        "--engine",
        "fake",
        "--scenario",
        "vi_50",
        "--hardware-class",
        "fake-ci",
        "--output",
        str(output),
    )

    assert proc.returncode == 0, proc.stderr
    payload = read_one_record(output)
    assert payload["run_kind"] == "ui_pipeline"
    assert payload["event_loop"]["sample_count"] >= 1
    assert payload["frames"]["supported"] in {True, False}
    if payload["frames"]["supported"]:
        assert payload["frames"]["sample_count"] >= 1


def test_pipeline_runner_accepts_real_benchmark_options() -> None:
    from scripts.benchmarks.run_once import _parser

    args = _parser().parse_args(
        [
            "--engine",
            "real",
            "--backend",
            "onnx",
            "--precision",
            "int8",
            "--threads",
            "0",
            "--max-batch-size",
            "1",
            "--sink",
            "null",
            "--warmup-iterations",
            "1",
            "--iterations",
            "2",
        ]
    )

    assert args.engine == "real"
    assert args.sink == "null"
    assert args.threads == 0
    assert args.max_batch_size == 1
    assert args.warmup_iterations == 1
    assert args.iterations == 2


def test_pipeline_runner_rejects_invalid_tuning_values() -> None:
    from scripts.benchmarks.run_once import _parser

    with pytest.raises(SystemExit):
        _parser().parse_args(["--threads", "-1"])
    with pytest.raises(SystemExit):
        _parser().parse_args(["--max-batch-size", "0"])


def test_pipeline_runner_emits_each_measured_iteration_after_warmup(tmp_path: Path) -> None:
    output = tmp_path / "iterations.jsonl"

    proc = run_benchmark(
        output,
        "--sink",
        "null",
        "--warmup-iterations",
        "1",
        "--iterations",
        "2",
    )

    assert proc.returncode == 0, proc.stderr
    records = [json.loads(line) for line in output.read_text(encoding="utf-8").splitlines()]
    assert len(records) == 2
    assert all(record["trace"]["outcome"] == "completed" for record in records)


def test_matrix_groups_warm_iterations_in_one_child(tmp_path: Path, monkeypatch) -> None:
    from scripts.benchmarks import run_matrix

    commands: list[list[str]] = []

    def fake_run_child(command: list[str], _output: Path) -> list[dict[str, object]]:
        commands.append(command)
        return []

    monkeypatch.setattr(run_matrix, "_run_child", fake_run_child)
    args = run_matrix._parser().parse_args(
        [
            "--engine",
            "fake",
            "--scenario",
            "vi_50",
            "--cold-iterations",
            "0",
            "--warm-iterations",
            "2",
            "--output",
            str(tmp_path / "matrix.jsonl"),
        ]
    )

    assert run_matrix.run(args) == 0
    assert len(commands) == 1
    assert "--warmup-iterations" in commands[0]
    assert commands[0][commands[0].index("--warmup-iterations") + 1] == "1"
    assert "--iterations" in commands[0]
    assert commands[0][commands[0].index("--iterations") + 1] == "2"
