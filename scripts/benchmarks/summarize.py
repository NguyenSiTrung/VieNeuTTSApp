"""Summarize raw benchmark JSONL without exposing benchmark content."""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

from scripts.benchmarks.statistics import summarize


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("inputs", nargs="+", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    return parser


def _event_offset(trace: dict[str, object], name: str) -> float | None:
    for event in trace.get("events", []):
        if isinstance(event, dict) and event.get("name") == name:
            value = event.get("offset_ns")
            if isinstance(value, (int, float)):
                return float(value) / 1_000_000
    return None


def _event_delta(
    trace: dict[str, object],
    start_name: str,
    end_name: str,
) -> float | None:
    start = _event_offset(trace, start_name)
    end = _event_offset(trace, end_name)
    if start is None or end is None or end < start:
        return None
    return end - start


def _path_for_record(payload: dict[str, object]) -> str:
    trace = payload.get("trace")
    if isinstance(trace, dict):
        run_kind = trace.get("run_kind")
        if run_kind is None and isinstance(trace.get("tags"), dict):
            run_kind = trace["tags"].get("run_kind")
        if run_kind == "direct_engine":
            return "direct"
        if run_kind == "ui_pipeline":
            return "ui"
    return "pipeline"


def _group_key(payload: dict[str, object]) -> dict[str, object]:
    scenario = payload.get("scenario")
    scenario = scenario if isinstance(scenario, dict) else {}
    return {
        "path": _path_for_record(payload),
        "scenario_id": scenario.get("scenario_id"),
        "backend": scenario.get("backend"),
        "precision": scenario.get("precision"),
        "sink_kind": scenario.get("sink_kind"),
        "matrix_run_kind": payload.get("matrix_run_kind", "unspecified"),
    }


def _distribution_payload(
    records: list[dict[str, object]],
) -> dict[str, dict[str, object]]:
    record_metrics = [_metric_values(record) for record in records]
    metric_names = sorted({metric for metrics in record_metrics for metric in metrics})
    distributions: dict[str, dict[str, object]] = {}
    for metric in metric_names:
        values = [metrics[metric] for metrics in record_metrics if metric in metrics]
        distribution = summarize(values)
        distributions[metric] = {
            "count": distribution.count,
            "minimum": distribution.minimum,
            "median": distribution.median,
            "p90": distribution.p90,
            "p95": distribution.p95,
            "maximum": distribution.maximum,
            "mad": distribution.mad,
            "missing_count": len(records) - distribution.count,
        }
    return distributions


def _metric_values(payload: dict[str, object]) -> dict[str, float]:
    values: dict[str, float] = {}
    trace = payload.get("trace")
    if not isinstance(trace, dict):
        return values
    if _path_for_record(payload) == "direct":
        ttfc = _event_delta(trace, "engine_call_started", "engine_first_chunk")
        if ttfc is not None:
            values["ttfc_ms"] = ttfc
    initialization = _event_delta(
        trace,
        "engine_initialize_started",
        "engine_initialize_completed",
    )
    if initialization is not None:
        values["model_initialization_ms"] = initialization
    pairs = {
        "ttfc_ms": ("submitted", "worker_first_chunk"),
        "controller_first_chunk_ms": ("submitted", "controller_first_chunk"),
        "first_buffer_append_ms": ("submitted", "audio_first_buffer_append"),
        "first_sink_pull_ms": ("submitted", "audio_first_sink_pull"),
        "worker_completion_ms": ("submitted", "worker_completed"),
    }
    for metric, (start_name, end_name) in pairs.items():
        start = _event_offset(trace, start_name)
        end = _event_offset(trace, end_name)
        if start is not None and end is not None and end >= start:
            values[metric] = end - start
    startup = trace.get("startup")
    if isinstance(startup, dict):
        for metric in ("process_cold_startup_ms", "in_process_qml_boot_ms"):
            value = startup.get(metric)
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value >= 0:
                values[metric] = float(value)
    for key, payload_key in (
        ("rtf", "rtf"),
        ("elapsed_ms", "elapsed_ms"),
    ):
        value = payload.get(payload_key)
        if isinstance(value, (int, float)):
            values[key] = float(value)
    resources = payload.get("resources")
    if isinstance(resources, dict):
        for key in (
            "max_current_rss_bytes",
            "peak_rss_bytes",
            "cpu_utilization_percent",
            "normalized_cpu_utilization_percent",
            "process_cpu_delta_ns",
        ):
            value = resources.get(key)
            if isinstance(value, (int, float)):
                values[key] = float(value)
    maxima = trace.get("maxima")
    if isinstance(maxima, dict):
        for key in (
            "retained_chunk_bytes",
            "concatenated_audio_bytes",
            "audio_buffer_bytes",
        ):
            value = maxima.get(key)
            if isinstance(value, (int, float)):
                values[key] = float(value)
    counters = trace.get("counters")
    if isinstance(counters, dict) and isinstance(counters.get("audio_restarts"), (int, float)):
        values["audio_restarts"] = float(counters["audio_restarts"])
    event_loop = payload.get("event_loop")
    if isinstance(event_loop, dict):
        for source, target in (
            ("median_delay_ms", "event_loop_median_delay_ms"),
            ("p95_delay_ms", "event_loop_p95_delay_ms"),
            ("maximum_delay_ms", "event_loop_maximum_delay_ms"),
        ):
            value = event_loop.get(source)
            if isinstance(value, (int, float)):
                values[target] = float(value)
    return values


def summarize_records(records: list[dict[str, object]]) -> dict[str, object]:
    grouped: dict[tuple[object, ...], tuple[dict[str, object], list[dict[str, object]]]] = {}
    for record in records:
        key = _group_key(record)
        key_tuple = tuple(key.values())
        if key_tuple not in grouped:
            grouped[key_tuple] = (key, [])
        grouped[key_tuple][1].append(record)
    groups = [
        {
            "key": key,
            "count": len(group_records),
            "distributions": _distribution_payload(group_records),
        }
        for key, group_records in grouped.values()
    ]
    return {
        "schema_version": 1,
        "count": len(records),
        "distributions": _distribution_payload(records),
        "groups": groups,
    }


def run(args: argparse.Namespace) -> int:
    records: list[dict[str, object]] = []
    for input_path in args.inputs:
        for line in input_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                records.append(json.loads(line))
    payload = summarize_records(records)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        dir=args.output.parent,
        prefix=f".{args.output.name}.",
        suffix=".tmp",
        delete=False,
    ) as temporary:
        json.dump(payload, temporary, ensure_ascii=False, indent=2, sort_keys=True)
        temporary.write("\n")
        temporary_name = temporary.name
    Path(temporary_name).replace(args.output)
    print(args.output)
    return 0


def main(argv: list[str] | None = None) -> int:
    return run(_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
