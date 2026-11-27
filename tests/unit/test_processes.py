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

import contextlib
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
    assert [kwargs.get("start_new_session") for kwargs in seen] == [None, None, True]
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


def test_run_cli_bounds_captured_output_and_keeps_tail() -> None:
    # Explicit byte cap keeps only the trailing lines of both streams.
    noisy_src = (
        "import sys\n"
        "for i in range(200):\n"
        "    print(f'out-{i:04d}-xxxxxxxxxxxx')\n"
        "    print(f'err-{i:04d}-yyyyyyyyyyyy', file=sys.stderr)\n"
    )
    res = run_cli([sys.executable, "-c", noisy_src], max_output_bytes=256)
    assert res.returncode == 0
    assert len(res.stdout.encode("utf-8")) <= 256
    assert len(res.stderr.encode("utf-8")) <= 256
    assert "out-0199-xxxxxxxxxxxx" in res.stdout
    assert "out-0000-xxxxxxxxxxxx" not in res.stdout
    assert "err-0199-yyyyyyyyyyyy" in res.stderr
    assert "err-0000-yyyyyyyyyyyy" not in res.stderr

    # Streamed mode uses the tighter streamed cap while still delivering every line to the callback.
    stream_cap = processes.STREAMED_MAX_OUTPUT_BYTES
    assert stream_cap < processes.DEFAULT_MAX_OUTPUT_BYTES
    line_count = (stream_cap // 32) + 200
    streamed_src = (
        f"for i in range({line_count}):\n    print(f'progress-{{i:06d}}-abcdefghijklmno')\n"
    )
    seen_lines: list[str] = []
    streamed = run_cli(
        [sys.executable, "-c", streamed_src],
        on_stdout_line=seen_lines.append,
    )
    assert streamed.returncode == 0
    assert len(seen_lines) == line_count
    assert len(streamed.stdout.encode("utf-8")) <= stream_cap
    assert f"progress-{line_count - 1:06d}-abcdefghijklmno" in streamed.stdout
    assert "progress-000000-abcdefghijklmno" not in streamed.stdout

    # Oversized single UTF-8 line is truncated cleanly to valid UTF-8 within the cap.
    utf8_src = "print('Phụ đề ' * 40 + 'END')"
    utf8_res = run_cli([sys.executable, "-c", utf8_src], max_output_bytes=64)
    assert utf8_res.returncode == 0
    assert len(utf8_res.stdout.encode("utf-8")) <= 64
    assert utf8_res.stdout.strip().endswith("END")

    # Zero cap retains nothing while still draining and firing callbacks.
    zero_seen: list[str] = []
    zero_res = run_cli(
        [sys.executable, "-c", "print('hello'); print('world')"],
        on_stdout_line=zero_seen.append,
        max_output_bytes=0,
    )
    assert zero_res.returncode == 0
    assert [line.strip() for line in zero_seen] == ["hello", "world"]
    assert zero_res.stdout == ""
    assert zero_res.stderr == ""


def test_run_cli_cleans_up_child_when_post_spawn_setup_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    spawned: list[subprocess.Popen[str]] = []
    real_spawn = processes.spawn_cli
    real_start_reader = processes._start_reader
    calls = 0

    def spy_spawn(*args: Any, **kwargs: Any) -> subprocess.Popen[str]:
        proc = real_spawn(*args, **kwargs)
        spawned.append(proc)
        return proc

    def failing_second_reader(*args: Any, **kwargs: Any) -> threading.Thread | None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise RuntimeError("boom starting stderr reader")
        return real_start_reader(*args, **kwargs)

    monkeypatch.setattr(processes, "spawn_cli", spy_spawn)
    monkeypatch.setattr(processes, "_start_reader", failing_second_reader)

    try:
        with pytest.raises(RuntimeError, match="boom starting stderr reader"):
            run_cli([sys.executable, "-c", _PID_THEN_SLEEP], timeout=30.0)

        assert len(spawned) == 1
        proc = spawned[0]
        assert proc.poll() is not None
        assert process_alive(proc.pid) is False
        assert proc not in registered_children()
        if proc.stderr is not None:
            assert proc.stderr.closed is True
    finally:
        for proc in spawned:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
            for stream in (proc.stdout, proc.stderr):
                if stream is not None and not stream.closed:
                    stream.close()


def _wait_until_gone(pid: int, timeout: float = 1.5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not process_alive(pid):
            return True
        time.sleep(0.02)
    return not process_alive(pid)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group tree test")
def test_posix_process_tree_termination_kills_grandchildren() -> None:
    import os

    # Helper script: spawns a grandchild (which inherits stdout/stderr!) and prints both PIDs.
    def _tree_script(*, stubborn_grandchild: bool = False) -> str:
        gc_code = (
            "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
            if stubborn_grandchild
            else "import time; time.sleep(30)"
        )
        return (
            "import os, subprocess, sys, time\n"
            f"gc = subprocess.Popen([sys.executable, '-c', {gc_code!r}])\n"
            "print(f'{os.getpid()} {gc.pid}', flush=True)\n"
            "time.sleep(30)\n"
        )

    # 1. Timeout kills both child and grandchild (grandchild cannot keep pipes open or survive).
    pids: list[tuple[int, int]] = []
    try:
        with pytest.raises(CliTimeout):
            run_cli(
                [sys.executable, "-c", _tree_script()],
                timeout=0.5,
                on_stdout_line=lambda line: pids.append(tuple(int(p) for p in line.split())),  # type: ignore[arg-type]
            )
        assert len(pids) == 1
        child_pid, gc_pid = pids[0]
        assert _wait_until_gone(child_pid)
        assert _wait_until_gone(gc_pid)
    finally:
        for _, gc_pid in pids:
            if process_alive(gc_pid):
                with contextlib.suppress(OSError):
                    os.kill(gc_pid, signal.SIGKILL)

    # 2. Cancel kills even a grandchild that ignores SIGTERM after the child exits on SIGTERM.
    cancel = threading.Event()
    cancel_pids: list[tuple[int, int]] = []

    def on_cancel_line(line: str) -> None:
        parts = line.split()
        if len(parts) == 2:
            cancel_pids.append((int(parts[0]), int(parts[1])))
            cancel.set()

    try:
        with pytest.raises(CliCancelled):
            run_cli(
                [sys.executable, "-c", _tree_script(stubborn_grandchild=True)],
                timeout=30.0,
                cancel_check=cancel.is_set,
                on_stdout_line=on_cancel_line,
            )
        assert len(cancel_pids) == 1
        child_pid, gc_pid = cancel_pids[0]
        assert _wait_until_gone(child_pid)
        assert _wait_until_gone(gc_pid)
    finally:
        for _, gc_pid in cancel_pids:
            if process_alive(gc_pid):
                with contextlib.suppress(OSError):
                    os.kill(gc_pid, signal.SIGKILL)

    # 3. terminate_children() also terminates grandchildren of spawn_cli() children.
    proc = spawn_cli([sys.executable, "-c", _tree_script()])
    gc_pid_3 = 0
    try:
        assert proc.stdout is not None
        line = proc.stdout.readline().strip()
        child_pid_3, gc_pid_3 = (int(p) for p in line.split())
        assert process_alive(child_pid_3) and process_alive(gc_pid_3)
        terminate_children(timeout=0.5)
        assert _wait_until_gone(child_pid_3)
        assert _wait_until_gone(gc_pid_3)
        assert proc not in registered_children()
    finally:
        if gc_pid_3 and process_alive(gc_pid_3):
            with contextlib.suppress(OSError):
                os.kill(gc_pid_3, signal.SIGKILL)
        if proc.poll() is None:
            proc.kill()
            proc.wait()
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                stream.close()


def test_windows_job_object_attaches_terminates_and_falls_back_gracefully(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes
    from ctypes import wintypes

    # 1. Verify _kernel32() binds explicit wintypes argtypes/restype on a local WinDLL.
    win_dll_calls: list[tuple[str, bool]] = []

    class _FakeFn:
        def __init__(self) -> None:
            self.restype: Any = None
            self.argtypes: Any = None

    class _FakeWinDLL:
        def __init__(self, name: str, *, use_last_error: bool = False) -> None:
            win_dll_calls.append((name, use_last_error))
            self._fns: dict[str, _FakeFn] = {}

        def __getattr__(self, item: str) -> _FakeFn:
            return self._fns.setdefault(item, _FakeFn())

    monkeypatch.setattr(ctypes, "WinDLL", _FakeWinDLL, raising=False)
    dll = processes._kernel32()
    assert win_dll_calls == [("kernel32", True)]
    assert dll.CreateJobObjectW.restype == wintypes.HANDLE
    assert dll.CreateJobObjectW.argtypes == [wintypes.LPVOID, wintypes.LPCWSTR]
    assert dll.SetInformationJobObject.restype == wintypes.BOOL
    assert dll.SetInformationJobObject.argtypes == [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    assert dll.AssignProcessToJobObject.restype == wintypes.BOOL
    assert dll.AssignProcessToJobObject.argtypes == [wintypes.HANDLE, wintypes.HANDLE]
    assert dll.TerminateJobObject.restype == wintypes.BOOL
    assert dll.TerminateJobObject.argtypes == [wintypes.HANDLE, wintypes.UINT]

    # 2. Simulate Windows spawn + Job Object kill-on-close setup + TerminateJobObject cleanup.
    class _FakeKernel32:
        def __init__(self, *, allow_assign: bool = True) -> None:
            self.allow_assign = allow_assign
            self.created: list[int] = []
            self.info_calls: list[tuple[int, int, int, int]] = []
            self.assigned: list[tuple[int, int]] = []
            self.terminated_jobs: list[tuple[int, int]] = []
            self.closed: list[int] = []

        def CreateJobObjectW(self, attrs: Any, name: Any) -> int:
            handle = 0x1000 + len(self.created) + 1
            self.created.append(handle)
            return handle

        def SetInformationJobObject(
            self, job: int, info_class: int, info_ptr: Any, size: int
        ) -> int:
            flags = int(info_ptr._obj.BasicLimitInformation.LimitFlags)
            self.info_calls.append((job, info_class, flags, size))
            return 1

        def AssignProcessToJobObject(self, job: int, proc_handle: int) -> int:
            self.assigned.append((job, proc_handle))
            return 1 if self.allow_assign else 0

        def TerminateJobObject(self, job: int, exit_code: int) -> int:
            self.terminated_jobs.append((job, exit_code))
            return 1

        def CloseHandle(self, handle: int) -> int:
            self.closed.append(handle)
            return 1

    class _FakePopen:
        def __init__(self, cmd: list[str], **kwargs: Any) -> None:
            self.cmd = cmd
            self.kwargs = kwargs
            self.pid = 4242
            self._handle = 0x9999
            self.stdout = None
            self.stderr = None
            self.returncode: int | None = None
            self.terminate_calls = 0
            self.kill_calls = 0

        def poll(self) -> int | None:
            return self.returncode

        def wait(self, timeout: float | None = None) -> int:
            if self.returncode is None:
                raise subprocess.TimeoutExpired(self.cmd, timeout or 0.0)
            return self.returncode

        def terminate(self) -> None:
            self.terminate_calls += 1
            self.returncode = 1

        def kill(self) -> None:
            self.kill_calls += 1
            self.returncode = 1

    fake_k32 = _FakeKernel32(allow_assign=True)
    spawned_procs: list[_FakePopen] = []

    def make_fake_popen(cmd: list[str], **kwargs: Any) -> _FakePopen:
        p = _FakePopen(cmd, **kwargs)
        spawned_procs.append(p)
        return p

    monkeypatch.setattr(processes.os, "name", "nt")
    monkeypatch.setattr(processes, "_kernel32", lambda: fake_k32)
    monkeypatch.setattr(processes.subprocess, "Popen", make_fake_popen)

    proc = spawn_cli(["tool"])
    assert proc.kwargs.get("creationflags") == 0x08000000
    assert fake_k32.created == [0x1001]
    expected_struct_size = 144 if ctypes.sizeof(ctypes.c_size_t) == 8 else 112
    assert fake_k32.info_calls == [(0x1001, 9, 0x2000, expected_struct_size)]
    assert fake_k32.assigned == [(0x1001, 0x9999)]

    # Simulate TerminateJobObject killing the child process.
    orig_term_job = fake_k32.TerminateJobObject

    def term_job_and_exit(job: int, code: int) -> int:
        proc.returncode = code
        return orig_term_job(job, code)

    fake_k32.TerminateJobObject = term_job_and_exit  # type: ignore[method-assign]
    terminate_children(timeout=0.1)
    assert fake_k32.terminated_jobs == [(0x1001, 1)]
    assert fake_k32.closed == [0x1001]
    assert proc not in registered_children()

    # 3. Normal exit in run_cli closes the Job Object handle cleanly without leaking it.
    normal_k32 = _FakeKernel32(allow_assign=True)
    monkeypatch.setattr(processes, "_kernel32", lambda: normal_k32)

    def make_exited_popen(cmd: list[str], **kwargs: Any) -> _FakePopen:
        p = _FakePopen(cmd, **kwargs)
        p.returncode = 0
        return p

    monkeypatch.setattr(processes.subprocess, "Popen", make_exited_popen)
    done = run_cli(["tool"])
    assert done.returncode == 0
    assert normal_k32.created == [0x1001]
    assert normal_k32.terminated_jobs == []
    assert normal_k32.closed == [0x1001]

    # 4. Graceful fallback when AssignProcessToJobObject is denied (e.g. older Windows nested job).
    monkeypatch.setattr(processes.subprocess, "Popen", make_fake_popen)
    denied_k32 = _FakeKernel32(allow_assign=False)
    monkeypatch.setattr(processes, "_kernel32", lambda: denied_k32)
    fallback_proc = spawn_cli(["tool"])
    assert denied_k32.created == [0x1001]
    assert denied_k32.closed == [0x1001]  # Job handle closed immediately on assign failure.
    terminate_children(timeout=0.1)
    assert fallback_proc.terminate_calls == 1
    assert denied_k32.terminated_jobs == []
    assert fallback_proc not in registered_children()
