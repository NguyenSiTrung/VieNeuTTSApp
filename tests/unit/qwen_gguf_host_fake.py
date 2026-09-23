"""Scripted stand-in for the Qwen GGUF host and harness helpers (test support).

The child is the official fake's protocol script with two GGUF contract
injections: the ``start`` log records the child's working directory (the
engine must spawn it inside the runtime pack for ggml backend discovery),
and a ``load`` frame whose ``format`` is not ``"gguf"`` is answered with an
error — a parent that sends official fields has a routing bug, not a load
failure. ``supportsClone`` answers from the loaded profile so Base/CustomVoice
capabilities stay distinguishable.

``engine_for`` builds a :class:`QwenGgufEngine` wired to the fake command;
the runtime pack directory is created because the engine spawns the child
with ``cwd`` inside it.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path
from typing import Any

from tests.unit import qwen_host_fake

from vienetts_app.core.qwen_gguf_engine import QwenGgufEngine

FAKE_GGUF_HOST_SOURCE = (
    qwen_host_fake.FAKE_HOST_SOURCE.replace(
        'log("start", pid=os.getpid())',
        'log("start", pid=os.getpid(), cwd=os.getcwd())',
    )
    .replace(
        """def emit(frame):
    with WRITE_LOCK:""",
        """def emit(frame):
    log("emit", type=frame.type, job=frame.job, fields=dict(frame.fields))
    with WRITE_LOCK:""",
    )
    .replace(
        '''def raw(frame):
    """Write a frame the session would refuse — how a stale delivery looks."""
    with WRITE_LOCK:''',
        '''def raw(frame):
    """Write a frame the session would refuse — how a stale delivery looks."""
    log("emit", type=frame.type, job=frame.job, fields=dict(frame.fields))
    with WRITE_LOCK:''',
    )
    .replace(
        """        if frame.type == "load":
            if MODE == "load_error":""",
        """        if frame.type == "load":
            if frame.fields.get("format") != "gguf":
                emit(
                    Frame(
                        type="error",
                        fields={
                            "code": "load_failed",
                            "message": "a non-gguf load reached the gguf host",
                            "fatal": False,
                        },
                    )
                )
            elif MODE == "load_error":""",
    )
    .replace(
        """                            "supportsClone": False,""",
        """                            "supportsClone": frame.fields.get("profile") == "base",""",
    )
)

host_log = qwen_host_fake.host_log
received = qwen_host_fake.received
host_pid = qwen_host_fake.host_pid
pid_alive = qwen_host_fake.pid_alive
wait_for = qwen_host_fake.wait_for


def fake_host(tmp_path: Path, mode: str) -> list[str]:
    script = tmp_path / "fake_qwen_gguf_host.py"
    script.write_text(FAKE_GGUF_HOST_SOURCE, encoding="utf-8")
    return [sys.executable, str(script), mode]


def gguf_paths(tmp_path: Path) -> tuple[Path, Path, Path]:
    """A runtime pack dir + talker/codec pair; only the pack must exist."""
    pack = tmp_path / "pack"
    pack.mkdir(parents=True, exist_ok=True)
    models = tmp_path / "models"
    models.mkdir(parents=True, exist_ok=True)
    talker = models / "talker.gguf"
    codec = models / "codec.gguf"
    talker.write_bytes(b"GGUF")
    codec.write_bytes(b"GGUF")
    return pack, talker, codec


def engine_for(tmp_path: Path, mode: str, **overrides: Any) -> QwenGgufEngine:
    pack, talker, codec = gguf_paths(tmp_path)
    options: dict[str, Any] = {
        "profile": "qwen_custom_0_6b",
        "runtime_dir": pack,
        "talker_path": talker,
        "codec_path": codec,
        "quantization": "Q8_0",
        "device": "cpu",
        "command": fake_host(tmp_path, mode),
        "environment": {"FAKE_HOST_LOG": str(tmp_path / "host.log")},
        "handshake_timeout": 5.0,
        "load_timeout": 5.0,
        "frame_timeout": 5.0,
        "cancel_timeout": 0.4,
        "kill_timeout": 1.0,
        "shutdown_timeout": 1.0,
        "logger": logging.getLogger("test.qwen_gguf_engine"),
    }
    options.update(overrides)
    return QwenGgufEngine(**options)


def host_cwd(tmp_path: Path) -> str:
    """The working directory the spawned fake reported — the pack dir."""
    starts = [entry for entry in host_log(tmp_path) if entry["event"] == "start"]
    assert starts, "the host never started"
    return str(starts[-1].get("cwd", ""))


def emitted(tmp_path: Path, frame_type: str) -> list[dict[str, Any]]:
    """Frames the fake SENT to the parent (the ``emit`` log entries)."""
    return [
        entry
        for entry in host_log(tmp_path)
        if entry.get("event") == "emit" and entry.get("type") == frame_type
    ]


def terminals_for(tmp_path: Path, job: str) -> list[str]:
    """Terminal statuses the fake emitted for ``job``, in order."""
    return [
        str(entry["fields"].get("status", ""))
        for entry in emitted(tmp_path, "terminal")
        if entry.get("job") == job
    ]


def pcm_after_terminal(tmp_path: Path, job: str) -> list[dict[str, Any]]:
    """PCM frames the fake emitted for ``job`` after its terminal — must be empty."""
    entries = [
        entry
        for entry in host_log(tmp_path)
        if entry.get("event") == "emit"
        and entry.get("job") == job
        and entry.get("type") in ("pcm", "terminal")
    ]
    seen_terminal = False
    late: list[dict[str, Any]] = []
    for entry in entries:
        if entry.get("type") == "terminal":
            seen_terminal = True
        elif seen_terminal:
            late.append(entry)
    return late
