from concurrent.futures import ThreadPoolExecutor

import pytest

from vienetts_app.core.performance import PerformanceRecorder


class Clock:
    def __init__(self) -> None:
        self.value = 1_000

    def __call__(self) -> int:
        self.value += 10
        return self.value


def test_trace_uses_offsets_and_aggregates_numeric_values() -> None:
    recorder = PerformanceRecorder(enabled=True, clock_ns=Clock())
    recorder.begin("job-1", {"mode": "stream", "char_count": 42})
    recorder.mark("job-1", "submitted")
    recorder.observe_max("job-1", "retained_audio_bytes", 128)
    recorder.observe_max("job-1", "retained_audio_bytes", 96)
    recorder.increment("job-1", "chunks", 2)
    recorder.finish("job-1", "completed")

    (trace,) = recorder.snapshot("job-1")
    assert trace["job_id"] == "job-1"
    assert trace["tags"] == {"mode": "stream", "char_count": 42}
    assert trace["events"][0]["offset_ns"] >= 0
    assert trace["maxima"] == {"retained_audio_bytes": 128}
    assert trace["counters"] == {"chunks": 2}
    assert trace["outcome"] == "completed"


def test_disabled_recorder_retains_nothing() -> None:
    recorder = PerformanceRecorder(enabled=False)
    recorder.begin("job-1", {"mode": "stream"})
    recorder.mark("job-1", "submitted")
    recorder.finish("job-1", "completed")

    assert recorder.snapshot() == []


def test_tags_reject_content_bearing_keys() -> None:
    recorder = PerformanceRecorder(enabled=True)

    with pytest.raises(ValueError, match="tag key"):
        recorder.begin("job-1", {"text": "private input"})


def test_concurrent_marks_are_not_lost() -> None:
    recorder = PerformanceRecorder(enabled=True)
    recorder.begin("job-1", {"mode": "stream"})

    with ThreadPoolExecutor(max_workers=4) as pool:
        list(pool.map(lambda _: recorder.increment("job-1", "chunks"), range(500)))

    assert recorder.snapshot("job-1")[0]["counters"]["chunks"] == 500


def test_snapshot_is_detached_from_recorder_state() -> None:
    recorder = PerformanceRecorder(enabled=True)
    recorder.begin("job-1", {"mode": "stream"})
    recorder.mark("job-1", "submitted")

    snapshot = recorder.snapshot("job-1")
    snapshot[0]["tags"]["mode"] = "changed"
    snapshot[0]["events"].clear()

    fresh = recorder.snapshot("job-1")[0]
    assert fresh["tags"] == {"mode": "stream"}
    assert len(fresh["events"]) == 1


def test_blank_job_id_and_non_scalar_tag_values_are_rejected() -> None:
    recorder = PerformanceRecorder(enabled=True)

    with pytest.raises(ValueError, match="job_id"):
        recorder.begin(" ", {"mode": "stream"})
    with pytest.raises(ValueError, match="JSON scalar"):
        recorder.begin("job-1", {"char_count": []})
