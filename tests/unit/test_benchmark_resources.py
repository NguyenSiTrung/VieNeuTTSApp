import builtins
import time

import pytest
from scripts.benchmarks import resources
from scripts.benchmarks.resources import (
    CudaMemorySample,
    ResourceSampler,
    _parse_proc_status,
    _parse_ps_rss,
)


def test_parse_linux_proc_status() -> None:
    text = "Name:\tpython\nVmRSS:\t  12345 kB\nVmHWM:\t  23456 kB\n"

    current, peak = _parse_proc_status(text)

    assert current == 12_345 * 1024
    assert peak == 23_456 * 1024


def test_parse_proc_status_uses_current_when_peak_is_missing() -> None:
    current, peak = _parse_proc_status("VmRSS:\t64 kB\n")

    assert current == 64 * 1024
    assert peak == current


def test_parse_ps_rss_uses_kib() -> None:
    assert _parse_ps_rss(" 2048\n") == 2 * 1024 * 1024


def test_sampler_preserves_last_and_maximum_samples(monkeypatch) -> None:
    rss_values = iter([100, 250, 175])
    peak_values = iter([120, 270, 200])
    cpu_values = iter([10, 25, 55])

    monkeypatch.setattr(resources, "current_rss_bytes", lambda pid=None: next(rss_values))
    monkeypatch.setattr(resources, "peak_rss_bytes", lambda: next(peak_values))
    monkeypatch.setattr(time, "process_time_ns", lambda: next(cpu_values))

    sampler = ResourceSampler(interval_seconds=0.001)
    sampler.start()
    time.sleep(0.01)
    sampler.stop()
    sampler.stop()

    result = sampler.result()
    assert result.sample_count >= 1
    assert result.current_rss_bytes == result.samples[-1].current_rss_bytes
    assert result.max_current_rss_bytes == max(
        sample.current_rss_bytes for sample in result.samples
    )
    assert result.peak_rss_bytes == max(sample.peak_rss_bytes for sample in result.samples)
    assert result.process_cpu_delta_ns >= 0


def test_sampler_start_stop_are_idempotent() -> None:
    sampler = ResourceSampler(interval_seconds=0.001)

    sampler.start()
    sampler.start()
    time.sleep(0.003)
    sampler.stop()
    sampler.stop()

    assert sampler.result().sample_count >= 1


def test_sampler_reports_cuda_values_from_injected_probe() -> None:
    expected = CudaMemorySample(
        allocated_bytes=1,
        reserved_bytes=2,
        maximum_allocated_bytes=3,
        maximum_reserved_bytes=4,
    )
    sampler = ResourceSampler(
        interval_seconds=0.001,
        sample_cuda=True,
        cuda_probe=lambda: expected,
    )

    sampler.start()
    time.sleep(0.003)
    sampler.stop()

    result = sampler.result()
    assert result.samples
    assert result.samples[-1].cuda == expected


def test_sampler_without_cuda_does_not_import_torch(monkeypatch) -> None:
    original_import = builtins.__import__

    def reject_torch(name, *args, **kwargs):
        if name == "torch" or name.startswith("torch."):
            raise AssertionError("torch must not be imported")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", reject_torch)
    sampler = ResourceSampler(interval_seconds=0.001, sample_cuda=False)

    sampler.start()
    time.sleep(0.003)
    sampler.stop()

    assert sampler.result().sample_count >= 1


def test_cuda_memory_sample_rejects_negative_values() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        CudaMemorySample(
            allocated_bytes=-1,
            reserved_bytes=0,
            maximum_allocated_bytes=0,
            maximum_reserved_bytes=0,
        )
