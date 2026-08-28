from scripts.benchmarks.summarize import summarize_records


def _record(*, run_kind: str, elapsed_ms: float) -> dict[str, object]:
    events = [
        {"name": "engine_call_started", "offset_ns": 10_000_000},
        {"name": "engine_first_chunk", "offset_ns": 30_000_000},
        {"name": "engine_initialize_started", "offset_ns": 1_000_000},
        {"name": "engine_initialize_completed", "offset_ns": 9_000_000},
    ]
    trace: dict[str, object] = {"events": events}
    if run_kind == "direct_engine":
        trace["run_kind"] = run_kind
    else:
        trace["events"] = [
            {"name": "submitted", "offset_ns": 10_000_000},
            {"name": "worker_first_chunk", "offset_ns": 30_000_000},
        ]
    return {
        "scenario": {
            "scenario_id": "vi_50",
            "backend": "onnx",
            "precision": "int8",
            "sink_kind": "none" if run_kind == "direct_engine" else "fake",
        },
        "trace": trace,
        "elapsed_ms": elapsed_ms,
    }


def test_summary_groups_direct_and_pipeline_metrics() -> None:
    payload = summarize_records(
        [
            _record(run_kind="direct_engine", elapsed_ms=20.0),
            _record(run_kind="pipeline", elapsed_ms=40.0),
        ]
    )

    groups = payload["groups"]
    assert isinstance(groups, list)
    assert {group["key"]["path"] for group in groups} == {"direct", "pipeline"}
    direct = next(group for group in groups if group["key"]["path"] == "direct")
    assert direct["distributions"]["ttfc_ms"]["median"] == 20.0
    assert direct["distributions"]["model_initialization_ms"]["median"] == 8.0
