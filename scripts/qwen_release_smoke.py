#!/usr/bin/env python
"""Qwen real-model release validation (track Task 7.3).

OPT-IN, never part of the ordinary suite: this runs on a machine (or a release
runner) that already holds the verified packs — the runtime wheel directory and
the model tree that Phase 2 pins by checksum — and it downloads nothing.
Ordinary CI proves the same flows with the deterministic fake host (Task 7.2).

What it proves per platform and device, through the app's OWN host subprocess:

* the packs install through the app's managed installers (offline import,
  checksum-verified against the shipped manifests, so a tampered pack fails
  before anything loads);
* one bounded segment synthesizes real audio at the app's 48 kHz rate, checked
  by ``scripts/check_smoke_wav.py`` — the same checker the release pipeline
  uses for VieNeu's smoke WAV;
* cancellation settles the job and the next job still works;
* a killed host is restarted lazily by the next job (the app's crash recovery),
  which is what keeps a crashed host from taking the app down with it;
* shutdown leaves no child behind.

It records TTFR, total time, RTF, peak memory, cancellation latency and the
host restart as ONE JSON object on stdout; diagnostics go to stderr.

Usage (repository root, inside the APP environment — not the Qwen runtime):

    python scripts/qwen_release_smoke.py \
      --profile customvoice --device cpu --speaker Ryan \
      --packs build/qwen-packs --out build/qwen-smoke \
      --json-out build/qwen-smoke/customvoice-cpu.json

    python scripts/qwen_release_smoke.py \
      --profile base --device mps \
      --ref-audio build/spike/ref.wav --ref-text "Xin chào" ...

``--packs`` holds ``runtime/`` (the wheel files of one platform manifest) and
``models/`` (the model root tree: ``<profile_key>/`` plus ``shared/``).
"""

from __future__ import annotations

import argparse
import json
import os
import platform
import signal
import struct
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

if __package__ in (None, ""):  # `python scripts/qwen_release_smoke.py`
    # Running as a file puts scripts/ on sys.path, not the repository root, so
    # the `scripts` package (and the app) would not resolve. Importing it as
    # `python -m scripts.qwen_release_smoke` needs no help.
    sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts import check_smoke_wav  # noqa: E402 - after the sys.path bootstrap

SCHEMA_VERSION = 1
KIND = "qwen-release-smoke"
APP_SAMPLE_RATE = 48_000
PROFILES = ("customvoice", "base")
DEVICES = ("auto", "cpu", "cuda", "mps")
DEFAULT_DTYPE = "float32"
DEFAULT_ATTENTION = "sdpa"
DEFAULT_TIMEOUT_SECONDS = 900.0
DEFAULT_CANCEL_AFTER_MS = 500
DEFAULT_TEXT = "Xin chào, đây là bài kiểm tra phát hành của Qwen."
#: Recovery runs must stay cheap: after a cancel/restart, one short phrase is
#: enough to prove the engine is usable again.
SHORT_TEXT = "Xin chào."
DEFAULT_SPEAKER = "Ryan"
RSS_SAMPLE_SECONDS = 0.25

#: The pack's model root carries one directory per engine-profile key.
PROFILE_KEYS = {"customvoice": "qwen_custom_0_6b", "base": "qwen_base_0_6b"}


class ReleaseSmokeUsageError(ValueError):
    """The validator was invoked with an unusable combination; names the fix."""


class ReleaseSmokeError(RuntimeError):
    """A release-validation step failed; the message is the actionable reason."""


@dataclass(frozen=True)
class SmokeRequest:
    """One validated validation run."""

    profile: str
    device: str
    packs: Path
    data_dir: Path
    out_dir: Path
    runtime_pack: Path
    model_pack: Path
    dtype: str = DEFAULT_DTYPE
    attention: str = DEFAULT_ATTENTION
    text: str = DEFAULT_TEXT
    language: str = ""
    speaker: str = DEFAULT_SPEAKER
    ref_audio: str = ""
    ref_text: str = ""
    timeout: float = DEFAULT_TIMEOUT_SECONDS
    min_seconds: float = 0.5
    cancel_after_ms: int = DEFAULT_CANCEL_AFTER_MS
    check_cancel: bool = True
    check_restart: bool = True
    json_out: str = ""

    @property
    def engine_profile(self) -> str:
        return PROFILE_KEYS[self.profile]

    @property
    def wav_path(self) -> Path:
        return self.out_dir / f"qwen-{self.profile}-{self.device}.wav"


@dataclass
class RunMetrics:
    """Timings of one streaming synthesis."""

    ttfr_seconds: float = 0.0
    total_seconds: float = 0.0
    audio_seconds: float = 0.0
    chunks: int = 0
    peak_rss_bytes: int = 0
    peak_rss_source: str = "unavailable"

    @property
    def rtf(self) -> float:
        return self.audio_seconds / self.total_seconds if self.total_seconds > 0 else 0.0

    def as_json(self) -> dict[str, Any]:
        return {
            "ttfrSeconds": round(self.ttfr_seconds, 4),
            "totalSeconds": round(self.total_seconds, 4),
            "audioSeconds": round(self.audio_seconds, 4),
            "rtf": round(self.rtf, 4),
            "chunks": self.chunks,
            "peakRssBytes": self.peak_rss_bytes,
            "peakRssSource": self.peak_rss_source,
        }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="qwen_release_smoke",
        description="Validate the pinned Qwen runtime + model through the app's model host.",
    )
    parser.add_argument(
        "--profile",
        required=True,
        help=f"Model profile to validate: {', '.join(PROFILES)} (validated, not argparse-limited, "
        "so usage errors keep the one-JSON-result contract).",
    )
    parser.add_argument(
        "--packs",
        default="build/qwen-packs",
        help="Root holding runtime/ (wheels) and models/ (model root tree).",
    )
    parser.add_argument("--runtime-pack", default="", help="Override the runtime wheel directory.")
    parser.add_argument("--model-pack", default="", help="Override the model root tree.")
    parser.add_argument("--device", default="cpu", help="cpu | cuda | mps (auto resolves too)")
    parser.add_argument("--dtype", default=DEFAULT_DTYPE, help="float32 | float16 | bfloat16")
    parser.add_argument(
        "--attention", default=DEFAULT_ATTENTION, help="sdpa | eager | flash_attention_2"
    )
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument(
        "--language", default="", help="App language code (zh, en, …); default: the profile's own."
    )
    parser.add_argument("--speaker", default=DEFAULT_SPEAKER, help="CustomVoice fixed speaker.")
    parser.add_argument("--ref-audio", default="", help="Base reference clip (required for base).")
    parser.add_argument("--ref-text", default="", help="Base reference transcript (required).")
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="Per-frame/load budget in seconds; a CPU generation may exceed the app default.",
    )
    parser.add_argument("--min-seconds", type=float, default=0.5, help="Minimum audio duration.")
    parser.add_argument("--cancel-after-ms", type=int, default=DEFAULT_CANCEL_AFTER_MS)
    parser.add_argument(
        "--no-check-cancel", dest="check_cancel", action="store_false", help="Skip cancellation."
    )
    parser.add_argument(
        "--no-check-restart", dest="check_restart", action="store_false", help="Skip host restart."
    )
    parser.add_argument(
        "--data-dir",
        default="",
        help="App data dir the packs are installed into (default: <out>/data, a fresh one).",
    )
    parser.add_argument("--out", default="build/qwen-smoke", help="Output directory for the WAV.")
    parser.add_argument("--json-out", default="", help="Also write the JSON result to this path.")
    return parser.parse_args(argv)


def validate_request(args: argparse.Namespace) -> SmokeRequest:
    """Validate CLI input before anything heavy is imported or loaded."""
    if args.profile not in PROFILES:
        raise ReleaseSmokeUsageError(
            f"unknown profile {args.profile!r} — expected one of: {', '.join(PROFILES)}"
        )
    if args.device not in DEVICES:
        raise ReleaseSmokeUsageError(
            f"unknown device {args.device!r} — expected one of: {', '.join(DEVICES)}"
        )
    packs = Path(args.packs)
    runtime_pack = Path(args.runtime_pack) if args.runtime_pack else packs / "runtime"
    model_pack = Path(args.model_pack) if args.model_pack else packs / "models"
    if not runtime_pack.is_dir():
        raise ReleaseSmokeUsageError(f"runtime pack {runtime_pack} is not a directory of wheels")
    if not model_pack.is_dir():
        raise ReleaseSmokeUsageError(f"model pack {model_pack} is not a directory")
    if args.profile == "base":
        if not args.ref_audio.strip() or not Path(args.ref_audio).is_file():
            raise ReleaseSmokeUsageError(
                "--ref-audio is required for base (a 3-8 s reference clip)"
            )
        if not args.ref_text.strip():
            raise ReleaseSmokeUsageError("--ref-text is required for base (the clip's transcript)")
    elif not args.speaker.strip():
        raise ReleaseSmokeUsageError("--speaker is required for customvoice (a fixed speaker)")
    if args.language.strip():
        from vienetts_app.core import engine_profiles

        try:
            engine_profiles.language_model_name(
                engine_profiles.get_capabilities(PROFILE_KEYS[args.profile]), args.language.strip()
            )
        except engine_profiles.EngineProfileError as exc:
            raise ReleaseSmokeUsageError(str(exc)) from exc
    if args.timeout <= 0:
        raise ReleaseSmokeUsageError("--timeout must be positive")
    if args.cancel_after_ms < 0:
        raise ReleaseSmokeUsageError("--cancel-after-ms must not be negative")
    out_dir = Path(args.out)
    data_dir = Path(args.data_dir) if args.data_dir else out_dir / "data"
    return SmokeRequest(
        profile=args.profile,
        device=args.device,
        packs=packs,
        data_dir=data_dir,
        out_dir=out_dir,
        runtime_pack=runtime_pack,
        model_pack=model_pack,
        dtype=args.dtype,
        attention=args.attention,
        text=args.text,
        language=args.language,
        speaker=args.speaker.strip(),
        ref_audio=args.ref_audio,
        ref_text=args.ref_text,
        timeout=float(args.timeout),
        min_seconds=float(args.min_seconds),
        cancel_after_ms=int(args.cancel_after_ms),
        check_cancel=bool(args.check_cancel),
        check_restart=bool(args.check_restart),
        json_out=args.json_out,
    )


# --------------------------------------------------------------------------- #
# packs -> managed installs (the app's own installers, checksum-verified)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PackInstall:
    """Where the verified packs landed, exactly as the controller would find them."""

    runtime_platform_key: str
    runtime_site_packages: Path
    runtime_bytes: int
    model_profile_key: str
    model_revision: str
    model_dir: Path
    shared_dir: Path
    model_bytes: int


def install_packs(request: SmokeRequest) -> PackInstall:
    """Install both packs offline into the app data dir; raise on any mismatch.

    Nothing here reaches the network: ``install_from_offline_pack`` verifies
    every file against the pinned manifest before extracting, so the release
    run proves the SAME artifacts a user would import.
    """
    from vienetts_app.core import qwen_runtime_manifest as runtime_manifest
    from vienetts_app.core.qwen_model_manager import QwenModelManager
    from vienetts_app.core.qwen_runtime import QwenRuntimeManager

    platform_key = runtime_manifest.host_platform_key(request.device)
    manifest = runtime_manifest.manifest_for_platform(platform_key or "")
    if manifest is None:
        raise ReleaseSmokeError(
            f"this host has no pinned Qwen runtime for device {request.device!r} "
            f"(platform key {platform_key!r}) — pick another --device or platform"
        )
    runtime_manager = QwenRuntimeManager(request.data_dir / "qwen" / "runtime", manifest)
    runtime_status = runtime_manager.install_from_offline_pack(request.runtime_pack)
    if runtime_status.state != "ready" or runtime_status.location is None:
        raise ReleaseSmokeError(
            f"the runtime pack did not install ({runtime_status.state}): "
            f"{runtime_status.error or 'no location'}"
        )

    model_manager = QwenModelManager(request.data_dir / "qwen" / "models", request.engine_profile)
    model_status = model_manager.install_offline_pack(request.model_pack)
    if model_status.state != "ready" or model_status.location is None:
        raise ReleaseSmokeError(
            f"the model pack did not install ({model_status.state}): "
            f"{model_status.error or 'no location'}"
        )
    return PackInstall(
        runtime_platform_key=manifest.platform_key,
        runtime_site_packages=Path(runtime_status.location.site_packages),
        runtime_bytes=runtime_status.installed_bytes,
        model_profile_key=model_status.location.profile_key,
        model_revision=model_status.location.revision,
        model_dir=Path(model_status.location.profile_dir),
        shared_dir=Path(model_status.location.shared_dir),
        model_bytes=model_status.installed_bytes,
    )


# --------------------------------------------------------------------------- #
# engine construction and measurement
# --------------------------------------------------------------------------- #


def build_engine(request: SmokeRequest, install: PackInstall) -> Any:
    """The app's own engine object, pointed at the verified installs."""
    from vienetts_app.core.qwen_engine import QwenEngine

    return QwenEngine(
        request.engine_profile,
        model_dir=install.model_dir,
        shared_dir=install.shared_dir,
        device=request.device,
        dtype=request.dtype,
        attention=request.attention,
        runtime_dir=install.runtime_site_packages,
        load_timeout=request.timeout,
        frame_timeout=request.timeout,
        shutdown_timeout=30.0,
    )


def host_pid(engine: Any) -> int:
    """The host's pid, read from its own ``starting`` log line (public API only).

    Read it right after ``initialize()``: the engine keeps only a short stderr
    tail, and a chatty generation can push the startup line out of it.
    """
    for line in reversed(str(engine.stderr_tail()).splitlines()):
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if event.get("event") == "starting" and isinstance(event.get("pid"), int):
            return int(event["pid"])
    raise ReleaseSmokeError("the Qwen host never logged its pid — cannot check the restart path")


def process_alive(pid: int) -> bool:
    """Whether ``pid`` still exists (a zombie still counts, until it is reaped)."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def await_process_gone(pid: int, timeout: float = 2.0) -> bool:
    """Wait (bounded) for ``pid`` to be reaped; True when it is gone.

    A host that was just terminated is still visible to ``os.kill(pid, 0)`` as a
    zombie until the engine's ``wait()`` reaps it, so a liveness probe taken the
    instant a job ends would lie.
    """
    if pid <= 0:
        return True
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.01)
    return not process_alive(pid)


def sample_rss_bytes(pid: int) -> int | None:
    """Resident set size of ``pid`` in bytes, or None when this host cannot tell."""
    if pid <= 0:
        return None
    if os.name == "nt":  # pragma: no cover - Windows-only path
        import ctypes

        class _Counters(ctypes.Structure):
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

        process = ctypes.windll.kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED
        if not process:
            return None
        try:
            counters = _Counters()
            counters.cb = ctypes.sizeof(_Counters)
            ok = ctypes.windll.psapi.GetProcessMemoryInfo(
                process, ctypes.byref(counters), counters.cb
            )
            if not ok:
                return None
            return int(counters.WorkingSetSize)
        finally:
            ctypes.windll.kernel32.CloseHandle(process)
    try:  # POSIX (Linux and macOS both report KiB from ps)
        result = subprocess.run(
            ["ps", "-o", "rss=", "-p", str(pid)],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    text = result.stdout.strip()
    return int(text) * 1024 if text.isdigit() else None


class _RssSampler:
    """Peak RSS of the host process while a job runs (best effort)."""

    def __init__(self, pid: int, rss_fn: Callable[[int], int | None] = sample_rss_bytes) -> None:
        self._pid = pid
        self._rss_fn = rss_fn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.peak = 0
        self.samples = 0

    def __enter__(self) -> _RssSampler:
        self._thread = threading.Thread(target=self._run, name="qwen-rss", daemon=True)
        self._thread.start()
        return self

    def __exit__(self, *_: object) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)

    def _run(self) -> None:
        while not self._stop.is_set():
            value = self._rss_fn(self._pid)
            if value:
                self.samples += 1
                self.peak = max(self.peak, value)
            self._stop.wait(RSS_SAMPLE_SECONDS)


def stream_segment(
    engine: Any,
    request: SmokeRequest,
    *,
    job_id: str,
    rss_fn: Callable[[int], int | None] = sample_rss_bytes,
    pid: int = 0,
    on_chunk: Callable[[], None] | None = None,
) -> tuple[np.ndarray, RunMetrics]:
    """Run one bounded segment; return its 48 kHz samples and its timings."""
    chunks: list[np.ndarray] = []
    metrics = RunMetrics()
    started = time.monotonic()
    first: float | None = None
    sampler = _RssSampler(pid, rss_fn) if pid else None
    if sampler is not None:
        sampler.__enter__()
    try:
        stream: Iterator[np.ndarray] = engine.infer_stream(
            request.text,
            language=request.language,
            speaker=request.speaker,
            voice_prompt=request.ref_audio if request.profile == "base" else "",
            ref_text=request.ref_text if request.profile == "base" else "",
            job_id=job_id,
        )
        for chunk in stream:
            now = time.monotonic()
            if first is None:
                first = now
            chunks.append(np.asarray(chunk, dtype=np.float32))
            if on_chunk is not None:
                on_chunk()
    finally:
        if sampler is not None:
            sampler.__exit__()
            metrics.peak_rss_bytes = sampler.peak
            metrics.peak_rss_source = "sampled-host" if sampler.samples else "unavailable"
    metrics.total_seconds = time.monotonic() - started
    metrics.ttfr_seconds = (first - started) if first is not None else metrics.total_seconds
    audio = np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32)
    metrics.chunks = len(chunks)
    metrics.audio_seconds = float(audio.size) / APP_SAMPLE_RATE
    return audio, metrics


def check_cancellation(
    engine: Any,
    request: SmokeRequest,
    *,
    pid: int = 0,
    timer_factory: Callable[[float, Callable[[], None]], Any] = threading.Timer,
) -> dict[str, Any]:
    """Cancel a running job, then prove the engine is usable again (AC-8).

    The pinned runtime cannot interrupt a live ``generate_*`` call, so the
    engine has two honest ways to stop a job: the host settles it when the
    request lands while it is streaming, or the parent terminates the host and
    surfaces that as a cancellation (the force-cancel path). Either way the
    terminal is ``cancelled``; what differs is whether the host survived, and a
    job that completes is the finding. When the host had to go, the NEXT job
    must lazily start a clean one — that is the half users feel.
    """
    from vienetts_app.core.qwen_engine import QwenEngineCancelled

    job = "release-smoke-cancel"
    report: dict[str, Any] = {"requested": True, "jobId": job}
    fired: list[float] = []

    def request_cancel() -> None:
        fired.append(time.monotonic())
        engine.cancel(job)

    timer = timer_factory(request.cancel_after_ms / 1000.0, request_cancel)
    started = time.monotonic()
    timer.start()
    try:
        stream = engine.infer_stream(
            request.text,
            language=request.language,
            speaker=request.speaker,
            voice_prompt=request.ref_audio if request.profile == "base" else "",
            ref_text=request.ref_text if request.profile == "base" else "",
            job_id=job,
        )
        for _chunk in stream:  # pragma: no cover - a cancel that lands too late
            pass
    except QwenEngineCancelled:
        report["terminal"] = "cancelled"
    except Exception as exc:  # noqa: BLE001 — any other terminal is the finding
        report["terminal"] = "failed"
        report["error"] = f"{type(exc).__name__}: {exc}"
    else:
        report["terminal"] = "completed"
    finally:
        timer.cancel()
    settled = time.monotonic()
    report["cancelRequestedAtSeconds"] = round((fired[0] - started) if fired else 0.0, 4)
    report["latencySeconds"] = round(settled - (fired[0] if fired else started), 4)
    # `is_initialized` is the question the NEXT job cares about: a live host can
    # take the next submission as-is, a terminated one must be restarted.
    report["hostAliveAfterCancel"] = bool(engine.is_initialized)
    report["hostReaped"] = await_process_gone(pid) if pid else True

    if not report["hostAliveAfterCancel"]:
        # The host had to be terminated: the next job must still work.
        recovery_started = time.monotonic()
        audio, _metrics = stream_segment(
            engine, replace(request, text=SHORT_TEXT), job_id="release-smoke-cancel-recovery"
        )
        report["recovered"] = bool(audio.size)
        report["recoverySeconds"] = round(time.monotonic() - recovery_started, 4)
    return report


def check_restart(
    engine: Any,
    request: SmokeRequest,
    pid_fn: Callable[[], int],
    *,
    kill: Callable[[int, int], None] = os.kill,
    timer_factory: Callable[[float, Callable[[], None]], Any] = threading.Timer,
    rss_fn: Callable[[int], int | None] = sample_rss_bytes,
) -> dict[str, Any]:
    """Kill the host mid-job, then prove the next job restarts it and works.

    The kill delay mirrors ``--cancel-after-ms`` so one knob bounds both probes;
    the assertion is on the RECOVERY, not on which error the crash produced.
    """
    from vienetts_app.core.qwen_engine import QwenEngineError

    report: dict[str, Any] = {"killed": False, "recovered": False}
    job = "release-smoke-crash"
    killed: list[float] = []

    def kill_host() -> None:
        try:
            pid = pid_fn()
        except ReleaseSmokeError as exc:
            report["error"] = str(exc)
            return
        report["hostPid"] = pid
        killed.append(time.monotonic())
        kill(pid, signal.SIGTERM)
        report["killed"] = True

    timer = timer_factory(request.cancel_after_ms / 1000.0, kill_host)
    started_at = time.monotonic()
    stream = engine.infer_stream(
        request.text,
        language=request.language,
        speaker=request.speaker,
        voice_prompt=request.ref_audio if request.profile == "base" else "",
        ref_text=request.ref_text if request.profile == "base" else "",
        job_id=job,
    )
    timer.start()
    try:
        for _chunk in stream:
            pass
    except QwenEngineError as exc:
        report["crashError"] = str(exc)
    except Exception as exc:  # noqa: BLE001 — a crash must never escape as a traceback
        report["crashError"] = f"{type(exc).__name__}: {exc}"
    finally:
        timer.cancel()
        stream.close()
    report["killedAtSeconds"] = round((killed[0] - started_at) if killed else 0.0, 4)
    if not report["killed"]:
        report["error"] = report.get("error") or "the job completed before the host could be killed"
        return report

    # The engine must reap the corpse and lazily start a clean host.
    recovery_started = time.monotonic()
    audio, metrics = stream_segment(
        engine, request, job_id="release-smoke-recovery", rss_fn=rss_fn, pid=0
    )
    report["recovered"] = bool(audio.size)
    report["recoverySeconds"] = round(time.monotonic() - recovery_started, 4)
    report["ttfrSeconds"] = round(metrics.ttfr_seconds, 4)
    report["audioSeconds"] = round(metrics.audio_seconds, 4)
    return report


# --------------------------------------------------------------------------- #
# report
# --------------------------------------------------------------------------- #


def platform_report() -> dict[str, Any]:
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "processor": platform.processor(),
    }


def capabilities_report(engine: Any) -> dict[str, Any]:
    caps = engine.capabilities()
    return {
        "languages": list(getattr(caps, "languages", ())),
        "speakers": list(getattr(caps, "speakers", ())),
        "sampleRate": int(getattr(caps, "sample_rate", 0) or 0),
        "supportsClone": bool(getattr(caps, "supports_clone", False)),
    }


def validate_wav(request: SmokeRequest) -> dict[str, Any]:
    """Validate the written WAV with the release checker and return its stats."""
    path = request.wav_path
    problems = check_smoke_wav.check(path, request.min_seconds, expect_rate=APP_SAMPLE_RATE)
    stats: dict[str, Any] = {"path": str(path), "problems": problems}
    try:
        channels, rate, frames, samples = check_smoke_wav.read_wav(path)
    except (ValueError, OSError, struct.error) as exc:
        stats["unreadable"] = str(exc)
        return stats
    rms = float(np.sqrt(np.mean(np.square(samples)))) if samples else 0.0
    peak = float(max((abs(value) for value in samples), default=0.0))
    stats.update(
        {
            "sampleRate": rate,
            "channels": channels,
            "frames": frames,
            "seconds": round(frames / rate, 4) if rate else 0.0,
            "rms": round(rms, 5),
            "peak": round(peak, 5),
        }
    )
    return stats


def run(request: SmokeRequest) -> dict[str, Any]:
    """Perform the whole validation and return the report (never raises on a finding)."""
    from vienetts_app.core import engine_profiles
    from vienetts_app.core.audio import write_wav_file

    started_at = datetime.now(UTC).isoformat(timespec="seconds")
    report: dict[str, Any] = {
        "schemaVersion": SCHEMA_VERSION,
        "kind": KIND,
        "startedAt": started_at,
        "platform": platform_report(),
        "profile": request.profile,
        "engineProfile": request.engine_profile,
        "device": request.device,
        "dtype": request.dtype,
        "attention": request.attention,
        "packs": {"runtime": str(request.runtime_pack), "model": str(request.model_pack)},
        "dataDir": str(request.data_dir),
        "problems": [],
    }
    problems: list[str] = report["problems"]

    install = install_packs(request)
    report["install"] = {
        "runtimePlatformKey": install.runtime_platform_key,
        "runtimeSitePackages": str(install.runtime_site_packages),
        "runtimeBytes": install.runtime_bytes,
        "modelProfileKey": install.model_profile_key,
        "modelRevision": install.model_revision,
        "modelDir": str(install.model_dir),
        "sharedDir": str(install.shared_dir),
        "modelBytes": install.model_bytes,
    }
    language = request.language or engine_profiles.default_language(request.engine_profile)
    request = replace(request, language=language)
    report["language"] = {
        "code": language,
        "modelName": engine_profiles.language_model_name(
            engine_profiles.get_capabilities(request.engine_profile), language
        ),
    }

    engine = build_engine(request, install)
    pid = 0
    try:
        load_started = time.monotonic()
        engine.initialize()
        report["loadSeconds"] = round(time.monotonic() - load_started, 4)
        report["capabilities"] = capabilities_report(engine)
        pid = host_pid(engine)
        report["hostPid"] = pid

        audio, metrics = stream_segment(engine, request, job_id="release-smoke-main", pid=pid)
        report["synthesis"] = {"text": request.text, **metrics.as_json()}
        if not audio.size:
            problems.append("the host produced no audio")
        else:
            write_wav_file(audio, request.wav_path, APP_SAMPLE_RATE)
        report["wav"] = validate_wav(request) if audio.size else {"path": str(request.wav_path)}
        problems.extend(report["wav"].get("problems", []))

        if request.check_cancel:
            cancellation = check_cancellation(engine, request, pid=pid)
            report["cancellation"] = cancellation
            if cancellation.get("terminal") != "cancelled":
                problems.append(
                    f"cancellation did not stop the job (terminal: {cancellation.get('terminal')})"
                )
            if cancellation.get("hostAliveAfterCancel") is False and not cancellation.get(
                "recovered"
            ):
                problems.append(
                    "the engine was not usable again after the cancel terminated the host"
                )
            if cancellation.get("hostAliveAfterCancel") is False and not cancellation.get(
                "hostReaped"
            ):
                problems.append("the cancelled host was terminated but never reaped")

        if request.check_restart:
            restart = check_restart(engine, request, lambda: host_pid(engine))
            report["restart"] = restart
            if not restart.get("recovered"):
                problems.append("the host did not restart after being killed")
            if not restart.get("killed"):
                problems.append(f"could not kill the host mid-job ({restart.get('error', '')})")
    finally:
        shutdown_started = time.monotonic()
        engine.close()
        shutdown_seconds = time.monotonic() - shutdown_started
        reaped = not process_alive(pid) if pid else False
        report["shutdown"] = {
            "seconds": round(shutdown_seconds, 4),
            "reaped": reaped,
            "clean": reaped,
        }
        if pid and not reaped:
            problems.append(f"shutdown left host pid {pid} alive")
    report["finishedAt"] = datetime.now(UTC).isoformat(timespec="seconds")
    return report


def emit(report: dict[str, Any], json_out: str) -> int:
    """Print the single JSON result (stdout), mirror it to ``json_out``, return the exit code."""
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    print(encoded)
    if json_out:
        path = Path(json_out)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(encoded + "\n", encoding="utf-8")
    problems = list(report.get("problems", ()))
    for problem in problems:
        print(f"FAIL {problem}", file=sys.stderr)
    if not problems:
        synthesis = report.get("synthesis", {})
        print(
            "ok: "
            f"{report['profile']} on {report['device']} — "
            f"ttfr={synthesis.get('ttfrSeconds')}s total={synthesis.get('totalSeconds')}s "
            f"rtf={synthesis.get('rtf')}",
            file=sys.stderr,
        )
    return 1 if problems else 0


def main(argv: list[str] | None = None) -> int:
    try:
        request = validate_request(parse_args(argv))
    except ReleaseSmokeUsageError as exc:
        print(f"usage error: {exc}", file=sys.stderr)
        return 2
    try:
        report = run(request)
    except (ReleaseSmokeError, OSError) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    return emit(report, request.json_out)


if __name__ == "__main__":
    raise SystemExit(main())
