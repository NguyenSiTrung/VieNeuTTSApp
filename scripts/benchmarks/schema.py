"""Versioned, content-safe JSONL benchmark records."""

from __future__ import annotations

import contextlib
import datetime as _datetime
import importlib.metadata
import json
import os
import platform
import re
import subprocess
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path

from scripts.benchmarks.corpus import CorpusEntry

SCHEMA_VERSION = 1
COMMAND_VERSION = "stage1-v1"
_HARDWARE_CLASS_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,63}\Z")


@dataclass(frozen=True)
class BenchmarkEnvironment:
    schema_version: int
    run_timestamp_utc: str
    python_implementation: str
    python_version: str
    os_system: str
    os_release: str
    os_version: str
    machine: str
    logical_cpu_count: int | None
    total_ram_bytes: int | None
    hardware_class: str
    package_versions: dict[str, str | None]
    git_commit_sha: str | None
    git_dirty: bool | None
    command_version: str

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


@dataclass(frozen=True)
class BenchmarkScenario:
    scenario_id: str
    text_sha256: str
    char_count: int
    language_class: str
    backend: str
    precision: str
    mode: str
    streaming: bool
    intra_op_threads: int | None = None
    max_batch_size: int | None = None
    sink_kind: str | None = None
    resolved_backend: str | None = None

    @classmethod
    def from_entry(
        cls,
        entry: CorpusEntry,
        *,
        backend: str,
        precision: str,
        mode: str,
        intra_op_threads: int | None = None,
        max_batch_size: int | None = None,
        sink_kind: str | None = None,
        resolved_backend: str | None = None,
    ) -> BenchmarkScenario:
        return cls(
            **entry.identity(),
            backend=backend,
            precision=precision,
            mode=mode,
            streaming=mode == "stream",
            intra_op_threads=intra_op_threads,
            max_batch_size=max_batch_size,
            sink_kind=sink_kind,
            resolved_backend=resolved_backend,
        )

    def to_dict(self) -> dict[str, object]:
        return asdict(self)


def _validate_hardware_class(value: str) -> str:
    if not isinstance(value, str) or _HARDWARE_CLASS_RE.fullmatch(value) is None:
        raise ValueError(
            "hardware_class must match [a-z0-9][a-z0-9-]{0,63} and identify capability only"
        )
    return value


def _total_ram_bytes() -> int | None:
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        page_count = os.sysconf("SC_PHYS_PAGES")
    except (AttributeError, OSError, ValueError):
        return None
    return int(page_size) * int(page_count)


def _package_versions() -> dict[str, str | None]:
    names = (
        "vienetts-app",
        "vieneu",
        "onnxruntime",
        "numpy",
        "PySide6",
        "torch",
        "torchaudio",
    )
    versions: dict[str, str | None] = {}
    for name in names:
        try:
            versions[name] = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def _git_identity(repo_root: Path) -> tuple[str | None, bool | None]:
    try:
        commit = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout.strip()
        status = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return None, None
    return commit or None, bool(status.strip())


def environment_manifest(
    *,
    hardware_class: str = "unspecified",
    repo_root: Path | None = None,
) -> BenchmarkEnvironment:
    hardware_class = _validate_hardware_class(hardware_class)
    root = Path(__file__).resolve().parents[2] if repo_root is None else Path(repo_root)
    commit, dirty = _git_identity(root)
    return BenchmarkEnvironment(
        schema_version=SCHEMA_VERSION,
        run_timestamp_utc=_datetime.datetime.now(_datetime.UTC).isoformat(),
        python_implementation=platform.python_implementation(),
        python_version=platform.python_version(),
        os_system=platform.system(),
        os_release=platform.release(),
        os_version=platform.version(),
        machine=platform.machine(),
        logical_cpu_count=os.cpu_count(),
        total_ram_bytes=_total_ram_bytes(),
        hardware_class=hardware_class,
        package_versions=_package_versions(),
        git_commit_sha=commit,
        git_dirty=dirty,
        command_version=COMMAND_VERSION,
    )


def _validate_nonnegative_metrics(value: object, key: str = "") -> None:
    if isinstance(value, dict):
        for child_key, child_value in value.items():
            _validate_nonnegative_metrics(child_value, str(child_key))
    elif isinstance(value, (int, float)) and not isinstance(value, bool):
        metric_key = key.lower()
        if (
            metric_key.endswith("_bytes")
            or metric_key.endswith("_ns")
            or "duration" in metric_key
            or metric_key in {"elapsed", "elapsed_ms", "elapsed_seconds"}
        ) and value < 0:
            raise ValueError(f"{key} must be non-negative")


def _json_value(value: object) -> object:
    if hasattr(value, "to_dict"):
        return value.to_dict()  # type: ignore[no-any-return]
    if hasattr(value, "__dataclass_fields__"):
        return asdict(value)  # type: ignore[arg-type]
    if isinstance(value, tuple):
        return [_json_value(item) for item in value]
    if isinstance(value, list):
        return [_json_value(item) for item in value]
    if isinstance(value, dict):
        return {str(key): _json_value(child) for key, child in value.items()}
    return value


@dataclass(frozen=True)
class BenchmarkRecord:
    environment: BenchmarkEnvironment
    scenario: BenchmarkScenario
    trace: dict[str, object]
    resources: object
    elapsed_ns: int
    audio_duration_ms: float | None = None
    event_loop: dict[str, object] | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.elapsed_ns, int) or isinstance(self.elapsed_ns, bool):
            raise ValueError("elapsed_ns must be an integer")
        if self.elapsed_ns < 0:
            raise ValueError("elapsed_ns must be non-negative")
        if self.audio_duration_ms is not None and self.audio_duration_ms < 0:
            raise ValueError("audio_duration_ms must be non-negative")
        _validate_nonnegative_metrics(_json_value(self.resources))
        _validate_nonnegative_metrics(self.trace)
        if self.event_loop is not None:
            _validate_nonnegative_metrics(self.event_loop)

    def to_dict(self) -> dict[str, object]:
        elapsed_ms = self.elapsed_ns / 1_000_000
        rtf = None
        if self.audio_duration_ms and self.audio_duration_ms > 0:
            rtf = elapsed_ms / self.audio_duration_ms
        payload: dict[str, object] = {
            "schema_version": SCHEMA_VERSION,
            "environment": self.environment.to_dict(),
            "scenario": self.scenario.to_dict(),
            "trace": _json_value(self.trace),
            "resources": _json_value(self.resources),
            "elapsed_ns": self.elapsed_ns,
            "elapsed_ms": elapsed_ms,
            "audio_duration_ms": self.audio_duration_ms,
            "rtf": rtf,
        }
        if self.event_loop is not None:
            payload["event_loop"] = _json_value(self.event_loop)
        return payload


def write_jsonl(records: list[BenchmarkRecord] | tuple[BenchmarkRecord, ...], path: Path) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
        text=True,
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as output:
            for record in records:
                output.write(json.dumps(record.to_dict(), ensure_ascii=False, sort_keys=True))
                output.write("\n")
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            os.unlink(temporary_name)
        raise
