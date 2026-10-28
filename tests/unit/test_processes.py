"""Process helpers: liveness probes, native CLI lookup/spawn, and the child registry.

``os.kill(pid, 0)`` is a POSIX idiom that CPython maps onto ``TerminateProcess``
on Windows, so "is this pid alive?" would kill it there. These tests pin the
contract the release smoke and the fake-host helpers rely on: a running pid
reads as alive, a reaped one as gone, and probing never disturbs the process.

The CLI helpers are what every native spawn (ffmpeg, ffprobe, whisper-cli,
audiocpp_cli) goes through: the lookup order that finds Homebrew tools from a
Finder-launched app, the Windows/encoding spawn options, timeouts and cancel
that never leak a running child, and the registry drained at shutdown.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from vienetts_app.core import processes
from vienetts_app.core.processes import (
    CliCancelled,
    CliTimeout,
    find_executable,
    process_alive,
    registered_children,
    run_cli,
    spawn_cli,
    terminate_children,
)


def test_a_running_child_is_alive_and_probing_leaves_it_running() -> None:
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    try:
        assert process_alive(child.pid) is True
        # Read-only: on Windows `os.kill(pid, 0)` would have killed this child.
        assert child.poll() is None
    finally:
        child.kill()
        child.wait()


def test_a_reaped_child_is_gone() -> None:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    assert process_alive(child.pid) is False


def test_a_pid_that_cannot_be_probed_is_gone() -> None:
    assert process_alive(0) is False
    assert process_alive(-1) is False


def _stub(directory: Path, name: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_text("#!/usr/bin/env python3\nprint('ok')\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def test_find_executable_order_is_managed_then_path_then_well_known(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    managed = _stub(tmp_path / "managed", "tool")
    on_path = _stub(tmp_path / "path", "tool")
    brew = _stub(tmp_path / "opt" / "homebrew" / "bin", "tool")
    darwin: dict[str, Any] = {
        "well_known_dirs": [str(tmp_path / "nowhere"), str(brew.parent)],
        "platform": "darwin",
    }

    monkeypatch.setenv("PATH", str(on_path.parent))
    missing = tmp_path / "missing"
    assert find_executable("tool", managed_dirs=[missing, managed.parent], **darwin) == managed
    assert find_executable("tool", managed_dirs=[], **darwin) == on_path

    # AC-6: a Finder-launched app has no Homebrew on PATH, yet finds the tool.
    monkeypatch.setenv("PATH", "")
    assert find_executable("tool", managed_dirs=[], **darwin) == brew
    assert find_executable("absent", managed_dirs=[managed.parent], **darwin) is None

    # Well-known dirs are POSIX-only; a managed dir on Windows prefers `.exe`
    # but still accepts an extensionless (shebang test stub) name.
    windows: dict[str, Any] = {"well_known_dirs": [str(brew.parent)], "platform": "win32"}
    assert find_executable("tool", managed_dirs=[], **windows) is None
    exe = _stub(managed.parent, "tool.exe")
    assert find_executable("tool", managed_dirs=[managed.parent], **windows) == exe
    exe.unlink()
    assert find_executable("tool", managed_dirs=[managed.parent], **windows) == managed


def test_spawn_options_hide_the_console_on_windows_and_decode_utf8(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[dict[str, object]] = []

    def popen_spy(cmd: list[str], **kwargs: object) -> subprocess.Popen[str]:
        seen.append(kwargs)
        raise OSError("spy: nothing spawned")

    monkeypatch.setattr(processes.subprocess, "Popen", popen_spy)
    monkeypatch.setattr(processes.os, "name", "nt")
    with pytest.raises(OSError, match="spy"):
        run_cli(["tool", "--help"], timeout=1.0)
    with pytest.raises(OSError, match="spy"):
        spawn_cli(["tool"])
    monkeypatch.setattr(processes.os, "name", "posix")
    with pytest.raises(OSError, match="spy"):
        run_cli(["tool"])
    monkeypatch.undo()

    assert [kwargs.get("creationflags") for kwargs in seen] == [0x08000000, 0x08000000, None]
    assert all(kw["encoding"] == "utf-8" and kw["errors"] == "replace" for kw in seen)
    assert all(kw["stdin"] == subprocess.DEVNULL for kw in seen)


_PID_THEN_SLEEP = "import os, time\nprint(os.getpid(), flush=True)\ntime.sleep(30)\n"


def test_run_cli_timeout_kills_and_cancel_terminates_promptly() -> None:
    # A timeout raises the typed error with the child already killed and reaped.
    with pytest.raises(CliTimeout) as timed_out:
        run_cli([sys.executable, "-c", _PID_THEN_SLEEP], timeout=0.3)
    assert timed_out.value.returncode is not None
    if sys.platform != "win32":
        assert timed_out.value.returncode == -signal.SIGKILL

    # Cancel fires the moment the child is up; it must be gone within 0.5 s.
    cancel = threading.Event()
    pids: list[int] = []
    cancelled_at: list[float] = []

    def on_line(line: str) -> None:
        pids.append(int(line))
        cancelled_at.append(time.monotonic())
        cancel.set()

    with pytest.raises(CliCancelled) as cancelled:
        run_cli(
            [sys.executable, "-c", _PID_THEN_SLEEP],
            timeout=30.0,
            cancel_check=cancel.is_set,
            on_stdout_line=on_line,
        )
    assert time.monotonic() - cancelled_at[0] <= 0.5
    assert cancelled.value.returncode is not None
    assert process_alive(pids[0]) is False
    assert cancelled.value.stdout.strip() == str(pids[0])

    # A normal run hands back both streams, decoded as UTF-8.
    done = run_cli([sys.executable, "-c", "import sys; sys.stderr.write('Phụ đề'); print('ok')"])
    assert (done.returncode, done.stdout.strip(), done.stderr) == (0, "ok", "Phụ đề")
    assert not registered_children()


def test_terminate_children_escalates_to_kill_and_registry_empties() -> None:
    stubborn_src = (
        "import signal, sys, time\n"
        "if sys.platform != 'win32':\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "print('ready', flush=True)\n"
        "time.sleep(30)\n"
    )
    stubborn = spawn_cli([sys.executable, "-c", stubborn_src])
    quick = spawn_cli([sys.executable, "-c", "pass"])
    try:
        assert stubborn.stdout is not None
        assert stubborn.stdout.readline().strip() == "ready"  # SIGTERM is ignored now
        quick.wait(timeout=30)
        live = registered_children()
        assert stubborn in live and quick not in live

        terminate_children(timeout=1)
        assert stubborn.poll() is not None
        if sys.platform != "win32":
            assert stubborn.returncode == -signal.SIGKILL  # terminate → kill
        assert stubborn not in registered_children()
    finally:
        for proc in (stubborn, quick):
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for stream in (proc.stdout, proc.stderr):
                if stream is not None:
                    stream.close()
