"""Scripted stand-in Qwen host and harness helpers (shared test support).

``FAKE_HOST_SOURCE`` is a real child process that speaks the framed protocol
with the real ``qwen_protocol`` module, so parent-side tests can exercise
spawn/handshake/stream/cancel/reap paths without torch or a checkpoint. Each
``MODE`` scripts one behaviour: ``ok``, ``silent``, ``load_error``,
``hang_synthesize``, ``slow_heartbeat``, ``heartbeat_forever``, ``slow_pcm``,
``graceful_cancel``, ``slow_cancel``, ``kill_required``, ``fail``, ``oom``,
``crash_after_pcm``, ``garbage``, ``noisy``, ``stale``, ``unknown_job``,
``wrong_handshake``.

It logs every received frame as one JSON line to ``$FAKE_HOST_LOG`` (and its own
pid on start), which is what lets tests assert on frames sent and on process
liveness. Used by ``test_qwen_engine.py`` and the worker/provider integration
tests.
"""

from __future__ import annotations

import json
import logging
import sys
import time
from pathlib import Path
from typing import Any

from vienetts_app.core.processes import process_alive
from vienetts_app.core.qwen_engine import QwenEngine

FAKE_HOST_SOURCE = r'''
"""Scripted stand-in for the Qwen model host (test fixture)."""

import json
import os
import signal
import sys
import threading
import time

from vienetts_app.core.qwen_protocol import (
    EndOfStream,
    Frame,
    SessionState,
    read_frame,
    write_frame,
)

MODE = sys.argv[1] if len(sys.argv) > 1 else "ok"
LOG = os.environ.get("FAKE_HOST_LOG", "")
OUT = sys.stdout.buffer
SESSION = SessionState("host")
WRITE_LOCK = threading.Lock()
CANCEL = threading.Event()


def log(event, **fields):
    if not LOG:
        return
    with open(LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"event": event, **fields}, sort_keys=True) + "\n")


def emit(frame):
    with WRITE_LOCK:
        SESSION.record_sent(frame)
        write_frame(OUT, frame)


def raw(frame):
    """Write a frame the session would refuse — how a stale delivery looks."""
    with WRITE_LOCK:
        write_frame(OUT, frame)


def pcm(job, seq, final, samples=12000, segment=None):
    payload = b"\x00\x00\x00\x00" * samples
    fields = {"sampleRate": 48000, "seq": seq, "final": final}
    if segment is not None:
        fields["segment"] = segment
    emit(Frame(type="pcm", job=job, payload=payload, fields=fields))


def terminal(job, status, **fields):
    emit(Frame(type="terminal", job=job, fields={"status": status, **fields}))


def synthesize_batch(frame):
    texts = list(frame.fields.get("texts", []))
    for index, _text in enumerate(texts):
        pcm(frame.job, index, True, samples=12000, segment=index)
    terminal(frame.job, "ok", frames=len(texts), audioSeconds=0.25 * len(texts))


def synthesize(frame):
    if MODE == "hang_synthesize":
        time.sleep(30)
        return
    if MODE in ("slow_heartbeat", "heartbeat_forever"):
        # Uninterruptible generate that proves liveness: fraction-less
        # progress heartbeats while it works. slow_heartbeat honors the
        # cancel only when the "generate" returns (like the real host);
        # heartbeat_forever never settles (a hung-but-responsive generate).
        deadline = time.time() + (3600.0 if MODE == "heartbeat_forever" else 1.2)
        while time.time() < deadline:
            time.sleep(0.05)
            emit(Frame(type="progress", job=frame.job, fields={"stage": "generating"}))
        if MODE == "heartbeat_forever":
            return
        if CANCEL.is_set():
            terminal(frame.job, "cancelled", frames=0)
            return
        pcm(frame.job, 0, False)
        emit(Frame(type="progress", job=frame.job, fields={"fraction": 0.5, "stage": "resampling"}))
        pcm(frame.job, 1, True)
        terminal(frame.job, "ok", frames=2, audioSeconds=0.5)
        return
    if MODE == "kill_required":
        time.sleep(30)  # never answers, and SIGTERM is ignored
        return
    if MODE == "unknown_job":
        # A misbehaving host: a frame for a job the parent never started.
        raw(
            Frame(
                type="pcm",
                job="some-other-job",
                payload=b"\x00\x00\x00\x00" * 8,
                fields={"sampleRate": 48000, "seq": 0, "final": True},
            )
        )
        return
    if MODE in ("graceful_cancel", "slow_cancel"):
        CANCEL.wait(timeout=30)
        if MODE == "slow_cancel":
            time.sleep(30)  # pretend the cancel never arrived
            return
        terminal(frame.job, "cancelled")
        return
    if MODE == "fail":
        emit(
            Frame(
                type="error",
                job=frame.job,
                fields={
                    "code": "generation_failed",
                    "message": "the model said no",
                    "fatal": False,
                },
            )
        )
        terminal(frame.job, "failed", error="the model said no")
        return
    if MODE == "oom":
        emit(
            Frame(
                type="error",
                job=frame.job,
                fields={
                    "code": "generation_failed",
                    "message": "CUDA out of memory",
                    "fatal": True,
                },
            )
        )
        terminal(frame.job, "failed", error="CUDA out of memory")
        os._exit(1)
    if MODE == "crash_after_pcm":
        pcm(frame.job, 0, False)
        sys.stderr.write("boom: the host died mid-job\n")
        sys.stderr.flush()
        os._exit(3)  # a real crash: the whole host goes away mid-job
    if MODE == "garbage":
        OUT.write(b"\x00\x01\x00\x01")
        OUT.flush()
        return
    if MODE == "slow_pcm":
        pcm(frame.job, 0, False)
        time.sleep(30)  # the caller abandons the stream while the job is still open
        return
    if MODE == "noisy":
        for index in range(40):
            sys.stderr.write("host log line %d\n" % index)
        sys.stderr.flush()
    pcm(frame.job, 0, False)
    emit(Frame(type="progress", job=frame.job, fields={"fraction": 0.5, "stage": "resampling"}))
    pcm(frame.job, 1, True)
    terminal(frame.job, "ok", frames=2, audioSeconds=0.5)
    if MODE == "stale":
        # A late delivery *after* the terminal: the parent must drop it, not
        # hand it to the next job.
        raw(
            Frame(
                type="pcm",
                job=frame.job,
                payload=b"\x00\x00\x00\x00" * 8,
                fields={"sampleRate": 48000, "seq": 2, "final": False},
            )
        )


def main():
    log("start", pid=os.getpid())
    if MODE == "kill_required":
        signal.signal(signal.SIGTERM, signal.SIG_IGN)  # force the parent to escalate
    if MODE == "wrong_handshake":
        # A host build that is not ours: it answers with the wrong first frame.
        emit(
            Frame(
                type="capabilities",
                fields={
                    "speakers": ["Ryan"],
                    "languages": ["en"],
                    "supportsClone": False,
                    "sampleRate": 48000,
                },
            )
        )
    elif MODE != "silent":
        emit(
            Frame(
                type="hello",
                fields={
                    "host": "fake-host",
                    "platform": "test",
                    "python": "3.13.0",
                    "sampleRate": 48000,
                },
            )
        )
    while True:
        try:
            frame = read_frame(sys.stdin.buffer)
        except EndOfStream:
            log("eof")
            return 0
        log("frame", type=frame.type, job=frame.job, fields=dict(frame.fields))
        SESSION.accept(frame)  # real hosts track the same transitions
        if frame.type == "load":
            if MODE == "load_error":
                emit(
                    Frame(
                        type="error",
                        fields={
                            "code": "load_failed",
                            "message": "model directory is missing",
                            "fatal": False,
                        },
                    )
                )
            else:
                emit(
                    Frame(
                        type="capabilities",
                        fields={
                            "speakers": ["Ryan"],
                            "languages": ["en"],
                            "supportsClone": False,
                            "sampleRate": 48000,
                        },
                    )
                )
        elif frame.type == "synthesize":
            threading.Thread(target=synthesize, args=(frame,), daemon=True).start()
        elif frame.type == "synthesize_batch":
            threading.Thread(target=synthesize_batch, args=(frame,), daemon=True).start()
        elif frame.type == "cancel":
            CANCEL.set()
        elif frame.type == "shutdown":
            return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def fake_host(tmp_path: Path, mode: str) -> list[str]:
    script = tmp_path / "fake_qwen_host.py"
    script.write_text(FAKE_HOST_SOURCE, encoding="utf-8")
    return [sys.executable, str(script), mode]


def engine_for(tmp_path: Path, mode: str, **overrides: Any) -> QwenEngine:
    model_dir = tmp_path / "models" / "customvoice"
    shared_dir = tmp_path / "models" / "shared"
    model_dir.mkdir(parents=True, exist_ok=True)
    shared_dir.mkdir(parents=True, exist_ok=True)
    options: dict[str, Any] = {
        "profile": "qwen_custom_0_6b",
        "model_dir": model_dir,
        "shared_dir": shared_dir,
        "command": fake_host(tmp_path, mode),
        "environment": {"FAKE_HOST_LOG": str(tmp_path / "host.log")},
        "handshake_timeout": 5.0,
        "load_timeout": 5.0,
        "frame_timeout": 5.0,
        "cancel_timeout": 0.4,
        "kill_timeout": 1.0,
        "shutdown_timeout": 1.0,
        "logger": logging.getLogger("test.qwen_engine"),
    }
    options.update(overrides)
    return QwenEngine(**options)


def host_log(tmp_path: Path) -> list[dict[str, Any]]:
    path = tmp_path / "host.log"
    if not path.exists():
        return []
    return [
        json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()
    ]


def received(tmp_path: Path, frame_type: str) -> list[dict[str, Any]]:
    return [entry for entry in host_log(tmp_path) if entry.get("type") == frame_type]


def host_pid(tmp_path: Path) -> int:
    starts = [entry for entry in host_log(tmp_path) if entry["event"] == "start"]
    assert starts, "the host never started"
    return int(starts[-1]["pid"])


def pid_alive(pid: int) -> bool:
    """Whether the fake host is still running.

    Never probe with ``os.kill(pid, 0)`` directly: on Windows that TERMINATES
    the process, and keeps answering "alive" while a handle to it is still open
    (see ``vienetts_app.core.processes``).
    """
    return process_alive(pid)


def wait_for(predicate: Any, timeout: float = 5.0, what: str = "condition") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for {what}")
