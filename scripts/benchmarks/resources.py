"""Portable local process resource sampling for benchmark runs."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

try:
    import resource
except ImportError:  # pragma: no cover - only Windows lacks resource
    resource = None  # type: ignore[assignment]


@dataclass(frozen=True)
class CudaMemorySample:
    allocated_bytes: int
    reserved_bytes: int
    maximum_allocated_bytes: int
    maximum_reserved_bytes: int

    def __post_init__(self) -> None:
        for name in (
            "allocated_bytes",
            "reserved_bytes",
            "maximum_allocated_bytes",
            "maximum_reserved_bytes",
        ):
            value = getattr(self, name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{name} must be a non-negative integer")


@dataclass(frozen=True)
class ResourceSample:
    monotonic_ns: int
    current_rss_bytes: int
    peak_rss_bytes: int
    process_cpu_ns: int
    cuda: CudaMemorySample | None = None


@dataclass(frozen=True)
class ResourceResult:
    samples: tuple[ResourceSample, ...]
    sample_count: int
    current_rss_bytes: int | None
    max_current_rss_bytes: int | None
    peak_rss_bytes: int | None
    process_cpu_delta_ns: int
    cpu_utilization_percent: float | None
    normalized_cpu_utilization_percent: float | None
    wall_seconds: float
    error: str | None = None


def _parse_proc_status(text: str) -> tuple[int, int]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        key, separator, rest = line.partition(":")
        if separator and key in {"VmRSS", "VmHWM"}:
            values[key] = int(rest.split()[0]) * 1024
    if "VmRSS" not in values:
        raise RuntimeError("VmRSS missing from proc status")
    return values["VmRSS"], values.get("VmHWM", values["VmRSS"])


def _parse_ps_rss(text: str) -> int:
    return int(text.strip()) * 1024


def _read_proc_status(pid: int) -> str:
    with open(f"/proc/{pid}/status", encoding="utf-8") as status_file:
        return status_file.read()


def _windows_process_memory(pid: int) -> tuple[int, int]:
    class ProcessMemoryCounters(ctypes.Structure):
        _fields_ = [
            ("cb", ctypes.c_ulong),
            ("PageFaultCount", ctypes.c_ulong),
            ("PeakWorkingSetSize", ctypes.c_size_t),
            ("WorkingSetSize", ctypes.c_size_t),
            ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPagedPoolUsage", ctypes.c_size_t),
            ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
            ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
            ("PagefileUsage", ctypes.c_size_t),
            ("PeakPagefileUsage", ctypes.c_size_t),
        ]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    process = kernel32.OpenProcess(0x1000, False, pid)
    if not process:
        raise OSError(ctypes.get_last_error(), "OpenProcess failed")
    try:
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            process,
            ctypes.byref(counters),
            counters.cb,
        ):
            raise OSError(ctypes.get_last_error(), "GetProcessMemoryInfo failed")
        return int(counters.WorkingSetSize), int(counters.PeakWorkingSetSize)
    finally:
        kernel32.CloseHandle(process)


def current_rss_bytes(pid: int | None = None) -> int:
    pid = os.getpid() if pid is None else pid
    if sys.platform.startswith("linux"):
        return _parse_proc_status(_read_proc_status(pid))[0]
    if sys.platform == "darwin":
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            check=True,
            capture_output=True,
            text=True,
        )
        return _parse_ps_rss(result.stdout)
    if sys.platform == "win32":
        return _windows_process_memory(pid)[0]
    raise RuntimeError(f"unsupported platform: {sys.platform}")


def peak_rss_bytes(pid: int | None = None) -> int:
    pid = os.getpid() if pid is None else pid
    if sys.platform.startswith("linux"):
        return _parse_proc_status(_read_proc_status(pid))[1]
    if sys.platform == "darwin":
        usage = resource.getrusage(resource.RUSAGE_SELF)
        return int(usage.ru_maxrss)
    if sys.platform == "win32":
        return _windows_process_memory(pid)[1]
    raise RuntimeError(f"unsupported platform: {sys.platform}")


def cuda_memory_bytes() -> CudaMemorySample | None:
    """Return current CUDA allocator counters, without importing torch eagerly."""
    try:
        import torch
    except ImportError:
        return None
    if not torch.cuda.is_available() or not torch.cuda.is_initialized():
        return None
    return CudaMemorySample(
        allocated_bytes=int(torch.cuda.memory_allocated()),
        reserved_bytes=int(torch.cuda.memory_reserved()),
        maximum_allocated_bytes=int(torch.cuda.max_memory_allocated()),
        maximum_reserved_bytes=int(torch.cuda.max_memory_reserved()),
    )


def reset_cuda_peak_memory_stats() -> None:
    """Reset CUDA peak counters when an initialized CUDA device is available."""
    try:
        import torch
    except ImportError:
        return
    if torch.cuda.is_available() and torch.cuda.is_initialized():
        torch.cuda.reset_peak_memory_stats()


class ResourceSampler:
    """Sample process memory and CPU usage on a daemon thread."""

    def __init__(
        self,
        interval_seconds: float = 0.1,
        sample_cuda: bool = False,
        *,
        current_rss_probe: Callable[[], int] | None = None,
        peak_rss_probe: Callable[[], int] | None = None,
        cpu_time_probe: Callable[[], int] | None = None,
        cuda_probe: Callable[[], CudaMemorySample | None] | None = None,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval_seconds must be positive")
        self.interval_seconds = float(interval_seconds)
        self.sample_cuda = bool(sample_cuda)
        self._current_rss_probe = current_rss_probe or current_rss_bytes
        self._peak_rss_probe = peak_rss_probe or peak_rss_bytes
        self._cpu_time_probe = cpu_time_probe or time.process_time_ns
        self._cuda_probe = cuda_probe or cuda_memory_bytes
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._samples: list[ResourceSample] = []
        self._errors: list[str] = []
        self._started_ns: int | None = None
        self._start_cpu_ns: int | None = None
        self._stopped_ns: int | None = None

    def __enter__(self) -> ResourceSampler:
        self.start()
        return self

    def __exit__(self, _exc_type, _exc_value, _traceback) -> None:
        self.stop()

    def start(self) -> None:
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if self._thread is not None:
                return
            self._started_ns = time.monotonic_ns()
            self._stop_event.clear()
            self._capture_sample()
            with self._lock:
                if self._samples:
                    self._start_cpu_ns = self._samples[0].process_cpu_ns
            self._thread = threading.Thread(
                target=self._run,
                name="benchmark-resource-sampler",
                daemon=True,
            )
            self._thread.start()

    def stop(self) -> None:
        thread = self._thread
        if thread is None:
            return
        self._stop_event.set()
        if thread is not threading.current_thread():
            thread.join(timeout=max(1.0, self.interval_seconds * 10))
        with self._lock:
            if self._stopped_ns is None:
                self._stopped_ns = time.monotonic_ns()

    def result(self) -> ResourceResult:
        with self._lock:
            samples = tuple(self._samples)
            errors = tuple(self._errors)
            started_ns = self._started_ns
            stopped_ns = self._stopped_ns
            start_cpu_ns = self._start_cpu_ns
        if started_ns is None:
            return ResourceResult(
                samples=(),
                sample_count=0,
                current_rss_bytes=None,
                max_current_rss_bytes=None,
                peak_rss_bytes=None,
                process_cpu_delta_ns=0,
                cpu_utilization_percent=None,
                normalized_cpu_utilization_percent=None,
                wall_seconds=0.0,
                error=None,
            )
        end_ns = stopped_ns or time.monotonic_ns()
        wall_seconds = max(0.0, (end_ns - started_ns) / 1_000_000_000)
        cpu_delta_ns = 0
        if samples:
            cpu_delta_ns = max(0, samples[-1].process_cpu_ns - (start_cpu_ns or 0))
        cpu_seconds = cpu_delta_ns / 1_000_000_000
        utilization = None
        normalized = None
        if wall_seconds > 0:
            utilization = cpu_seconds / wall_seconds * 100
            normalized = utilization / max(1, os.cpu_count() or 1)
        return ResourceResult(
            samples=samples,
            sample_count=len(samples),
            current_rss_bytes=samples[-1].current_rss_bytes if samples else None,
            max_current_rss_bytes=(
                max(sample.current_rss_bytes for sample in samples) if samples else None
            ),
            peak_rss_bytes=max((sample.peak_rss_bytes for sample in samples), default=None),
            process_cpu_delta_ns=cpu_delta_ns,
            cpu_utilization_percent=utilization,
            normalized_cpu_utilization_percent=normalized,
            wall_seconds=wall_seconds,
            error="; ".join(errors) if errors else None,
        )

    def _run(self) -> None:
        while not self._stop_event.wait(self.interval_seconds):
            self._capture_sample()

    def _capture_sample(self) -> None:
        try:
            sample = ResourceSample(
                monotonic_ns=time.monotonic_ns(),
                current_rss_bytes=int(self._current_rss_probe()),
                peak_rss_bytes=int(self._peak_rss_probe()),
                process_cpu_ns=int(self._cpu_time_probe()),
                cuda=self._cuda_probe() if self.sample_cuda else None,
            )
        except Exception as exc:  # noqa: BLE001 - metrics must not stop a run
            with self._lock:
                self._errors.append(type(exc).__name__)
            return
        with self._lock:
            self._samples.append(sample)
