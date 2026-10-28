"""Cross-platform process helpers: liveness probes and native CLI spawning.

``os.kill(pid, 0)`` is a POSIX idiom and must never be used as a probe: CPython
maps every signal other than ``CTRL_C_EVENT``/``CTRL_BREAK_EVENT`` onto
``TerminateProcess`` on Windows, so "is this process alive?" would kill it — and
keep answering "alive" while any handle to the exited process is still open.
Anything that needs to know whether a pid is still running goes through
:func:`process_alive`.

Every native CLI the app drives (``ffmpeg``, ``ffprobe``, ``whisper-cli``,
``audiocpp_cli``) is located with :func:`find_executable` and spawned through
:func:`run_cli` / :func:`spawn_cli`, so the rules live in one place:

- lookup is managed runtime dir → ``PATH`` → well-known POSIX dirs, because a
  Finder-launched macOS app inherits a ``PATH`` without ``/opt/homebrew/bin``;
- Windows children get ``CREATE_NO_WINDOW`` (a windowed build must not flash a
  console per spawn), and text pipes decode as UTF-8 with ``errors="replace"``
  (ffmpeg echoes raw container metadata, and a strict decode used to raise and
  discard a finished extraction);
- timeouts and ``cancel_check`` stop the child (terminate → kill) with bounded
  waits only: after a kill a grandchild can still hold the pipes open, so the
  output readers are joined with a timeout and ``communicate()`` is never used;
- every live child sits in a registry that :func:`terminate_children` drains
  at shutdown.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import IO, Any

#: Windows process exit code for "has not exited yet" (``STILL_ACTIVE``).
_STILL_ACTIVE = 259

#: ``PROCESS_QUERY_LIMITED_INFORMATION``: enough to read the exit code.
_QUERY_LIMITED_INFORMATION = 0x1000

#: ``SYNCHRONIZE``: required to wait on the process handle.
_SYNCHRONIZE = 0x00100000

#: ``WAIT_OBJECT_0``: the waited-on handle is signaled (the process exited).
_WAIT_OBJECT_0 = 0

#: ``CREATE_NO_WINDOW``: the Windows creation flag that suppresses a console.
_CREATE_NO_WINDOW = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))

#: Timeout for an ``ffprobe`` (or ``ffmpeg -i``) media probe.
FFPROBE_TIMEOUT_S = 30.0

#: Timeout for a ``--help`` / ``-version`` runnability probe of a native CLI.
PROBE_TIMEOUT_S = 15.0

#: Floor of the audio-extraction timeout; see :func:`extraction_timeout_s`.
EXTRACT_TIMEOUT_FLOOR_S = 120.0

#: POSIX directories searched after ``PATH`` (Homebrew arm64/x64, MacPorts,
#: system). A Finder-launched app's ``PATH`` has none of the first three.
WELL_KNOWN_DIRS: tuple[str, ...] = (
    "/opt/homebrew/bin",
    "/usr/local/bin",
    "/opt/local/bin",
    "/usr/bin",
)

#: How long a cancelled child gets to exit on ``terminate()`` before ``kill()``.
_CANCEL_GRACE_S = 0.3

#: Bounded reap after ``kill()``. ``wait()`` never touches the pipes, so a
#: grandchild holding them cannot block it.
_KILL_WAIT_S = 1.0

#: Reader join after a kill: a grandchild may keep the pipes open forever.
_KILLED_READER_JOIN_S = 0.5

#: Reader join after a normal exit, to collect the output tail.
_EXITED_READER_JOIN_S = 2.0

#: How often :func:`run_cli` re-checks ``cancel_check`` and the deadline.
_POLL_S = 0.05

__all__ = [
    "EXTRACT_TIMEOUT_FLOOR_S",
    "FFPROBE_TIMEOUT_S",
    "PROBE_TIMEOUT_S",
    "WELL_KNOWN_DIRS",
    "CliCancelled",
    "CliTimeout",
    "extraction_timeout_s",
    "find_executable",
    "process_alive",
    "registered_children",
    "run_cli",
    "spawn_cli",
    "terminate_children",
]


def process_alive(pid: int) -> bool:
    """Whether ``pid`` is still running.

    On POSIX a zombie still counts as alive until its parent reaps it, which is
    what makes "terminated but not yet reaped" observable. Windows has no
    zombies, so an exited process reads as gone as soon as it exits.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":  # pragma: no cover - Windows-only path
        return _windows_process_alive(pid)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _kernel32() -> object:  # pragma: no cover - Windows-only path
    """kernel32 with HANDLE-typed signatures.

    ``ctypes`` defaults ``restype`` to ``c_int``, which truncates 64-bit
    HANDLEs — a truncated handle makes ``WaitForSingleObject`` fail (and
    ``GetExitCodeProcess`` return False), silently inverting the verdict.
    A local ``WinDLL`` object keeps the typed signatures off the shared
    ``ctypes.windll.kernel32`` namespace.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.WaitForSingleObject.restype = wintypes.DWORD
    kernel32.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    kernel32.GetExitCodeProcess.restype = wintypes.BOOL
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.CloseHandle.restype = wintypes.BOOL
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


def _windows_process_alive(pid: int) -> bool:  # pragma: no cover - Windows-only path
    """Wait on the process handle: signaled means the process has exited.

    Waiting beats reading the exit code on Windows: a process that exits
    with code 259 (``STILL_ACTIVE``) is indistinguishable from a live one
    via ``GetExitCodeProcess`` and would be reported alive forever, while an
    exited handle is always signaled. ``SYNCHRONIZE`` can be denied where
    query-only rights are still granted, so the exit-code read stays as the
    fallback.
    """
    kernel32 = _kernel32()
    process = kernel32.OpenProcess(_SYNCHRONIZE | _QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return _windows_process_alive_by_exit_code(pid)
    try:
        return kernel32.WaitForSingleObject(process, 0) != _WAIT_OBJECT_0
    finally:
        kernel32.CloseHandle(process)


def _windows_process_alive_by_exit_code(
    pid: int,
) -> bool:  # pragma: no cover - Windows-only path
    """Exit-code fallback when ``SYNCHRONIZE`` access is denied.

    A process that exited with code 259 is reported alive — the known
    ``STILL_ACTIVE`` ambiguity, acceptable only because the wait path
    usually succeeds.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = _kernel32()
    process = kernel32.OpenProcess(_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return False
    try:
        exit_code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
            return False
        return exit_code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(process)


# ── Native CLI lookup ────────────────────────────────────────────────────────


def find_executable(
    name: str,
    *,
    managed_dirs: Iterable[str | os.PathLike[str] | None] = (),
    well_known_dirs: Sequence[str] | None = None,
    platform: str | None = None,
) -> Path | None:
    """Locate ``name``: managed runtime dirs, then ``PATH``, then well-known dirs.

    Managed dirs are searched in order (``None`` entries are skipped) and only
    need a regular file, matching the managed installers. On Windows ``name``
    is tried with ``.exe`` first and then as-is (shebang test stubs have no
    extension); the well-known dirs are POSIX-only and need an executable
    file. ``well_known_dirs`` and ``platform`` default to
    :data:`WELL_KNOWN_DIRS` and ``sys.platform`` and exist for tests.
    """
    windows = (sys.platform if platform is None else platform) == "win32"
    names = [name]
    if windows and not name.lower().endswith(".exe"):
        names.insert(0, f"{name}.exe")
    for directory in managed_dirs:
        if directory is None:
            continue
        for candidate_name in names:
            candidate = Path(directory) / candidate_name
            if candidate.is_file():
                return candidate
    on_path = shutil.which(name)
    if on_path:
        return Path(on_path)
    if windows:
        return None
    for directory in WELL_KNOWN_DIRS if well_known_dirs is None else well_known_dirs:
        candidate = Path(directory) / name
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def extraction_timeout_s(duration_s: float | None) -> float:
    """Audio-extraction timeout: ``max(120 s, 2 × media duration)``.

    An unknown duration (``None``/0, e.g. no ``ffprobe``) gets the floor.
    """
    return max(EXTRACT_TIMEOUT_FLOOR_S, 2.0 * max(0.0, float(duration_s or 0.0)))


# ── Spawning ─────────────────────────────────────────────────────────────────


class CliTimeout(subprocess.TimeoutExpired):
    """A native CLI outlived its timeout; it has already been killed.

    ``returncode`` is the killed child's exit status (``None`` only if it
    could not be reaped within the bounded wait).
    """

    def __init__(
        self,
        cmd: Sequence[str],
        timeout: float,
        *,
        returncode: int | None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(list(cmd), timeout, output=stdout, stderr=stderr)
        self.returncode = returncode


class CliCancelled(subprocess.SubprocessError):
    """``cancel_check`` asked to stop a native CLI; it has already been stopped."""

    def __init__(
        self,
        cmd: Sequence[str],
        *,
        returncode: int | None,
        stdout: str = "",
        stderr: str = "",
    ) -> None:
        super().__init__(f"Command {list(cmd)!r} was cancelled")
        self.cmd = list(cmd)
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


_children: set[subprocess.Popen[str]] = set()
_children_lock = threading.Lock()


def registered_children() -> tuple[subprocess.Popen[str], ...]:
    """Children spawned here that are still running; exited ones are dropped."""
    with _children_lock:
        for proc in [proc for proc in _children if proc.poll() is not None]:
            _children.discard(proc)
        return tuple(_children)


def terminate_children(timeout: float = 2.0) -> int:
    """Stop every registered child: terminate, wait up to ``timeout``, then kill.

    Meant for shutdown. Returns how many children were still running.
    """
    live = registered_children()
    for proc in live:
        with contextlib.suppress(OSError):
            proc.terminate()
    deadline = time.monotonic() + max(0.0, float(timeout))
    for proc in live:
        _wait_quietly(proc, deadline - time.monotonic())
    for proc in live:
        if proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.kill()
            _wait_quietly(proc, _KILL_WAIT_S)
    registered_children()
    return len(live)


def spawn_cli(
    cmd: Sequence[str | os.PathLike[str]],
    *,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    stdin: int | IO[Any] | None = subprocess.DEVNULL,
    stdout: int | IO[Any] | None = subprocess.PIPE,
    stderr: int | IO[Any] | None = subprocess.PIPE,
) -> subprocess.Popen[str]:
    """Start a native CLI with the shared spawn options and register it.

    The caller owns the returned process (reading its pipes, waiting on it);
    it leaves the registry once it has exited.
    """
    child_env = dict(os.environ if env is None else env)
    # Python test stubs (and any Python child) print UTF-8 into the pipe even
    # where a piped stdout would default to the Windows ANSI code page.
    child_env.setdefault("PYTHONIOENCODING", "utf-8")
    kwargs: dict[str, Any] = {
        "cwd": None if cwd is None else os.fspath(cwd),
        "env": child_env,
        "stdin": stdin,
        "stdout": stdout,
        "stderr": stderr,
        "encoding": "utf-8",
        "errors": "replace",
    }
    if os.name == "nt":
        # Windowed/frozen builds must not flash a console window per spawn.
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    proc = subprocess.Popen([os.fspath(arg) for arg in cmd], **kwargs)
    with _children_lock:
        _children.add(proc)
    return proc


def run_cli(
    cmd: Sequence[str | os.PathLike[str]],
    *,
    timeout: float | None = None,
    cancel_check: Callable[[], bool] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    on_stdout_line: Callable[[str], None] | None = None,
    on_stderr_line: Callable[[str], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a native CLI to completion and return its exit status and output.

    Both pipes are drained by reader threads (so a chatty child can never fill
    one and deadlock), and each line is also handed to ``on_stdout_line`` /
    ``on_stderr_line`` as it arrives, from the reader thread; an exception
    in a callback is swallowed so the drain keeps going.

    ``cancel_check`` is polled every 50 ms; when it returns True the child is
    terminated (killed after a short grace) and :class:`CliCancelled` is
    raised. Past ``timeout`` seconds the child is killed and
    :class:`CliTimeout` is raised. A non-zero exit is NOT an error here: the
    caller reads ``returncode``. Spawn failures raise ``OSError``.
    """
    argv = [os.fspath(arg) for arg in cmd]
    proc = spawn_cli(argv, cwd=cwd, env=env)
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    readers = [
        _start_reader(proc.stdout, stdout_lines, on_stdout_line),
        _start_reader(proc.stderr, stderr_lines, on_stderr_line),
    ]
    deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
    try:
        while True:
            if cancel_check is not None and cancel_check():
                _stop(proc, grace=_CANCEL_GRACE_S)
                _join(readers, _KILLED_READER_JOIN_S)
                raise CliCancelled(
                    argv,
                    returncode=proc.returncode,
                    stdout="".join(stdout_lines),
                    stderr="".join(stderr_lines),
                )
            wait_s = _POLL_S
            if deadline is not None:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    _stop(proc, grace=0.0)
                    _join(readers, _KILLED_READER_JOIN_S)
                    raise CliTimeout(
                        argv,
                        float(timeout or 0.0),
                        returncode=proc.returncode,
                        stdout="".join(stdout_lines),
                        stderr="".join(stderr_lines),
                    )
                wait_s = min(wait_s, remaining)
            try:
                proc.wait(timeout=wait_s)
                break
            except subprocess.TimeoutExpired:
                continue
        _join(readers, _EXITED_READER_JOIN_S)
        return subprocess.CompletedProcess(
            argv, proc.returncode, "".join(stdout_lines), "".join(stderr_lines)
        )
    except BaseException:
        # A raising cancel_check or an interrupt must not leak a running child.
        _stop(proc, grace=0.0)
        raise
    finally:
        with _children_lock:
            _children.discard(proc)


def _start_reader(
    stream: IO[str] | None,
    sink: list[str],
    on_line: Callable[[str], None] | None,
) -> threading.Thread | None:
    if stream is None:
        return None

    def pump() -> None:
        try:
            for line in stream:
                sink.append(line)
                if on_line is not None:
                    # A callback bug must not stop the drain (or the child
                    # blocks on a full pipe).
                    with contextlib.suppress(Exception):
                        on_line(line)
        except (OSError, ValueError):
            pass
        finally:
            with contextlib.suppress(Exception):
                stream.close()

    reader = threading.Thread(target=pump, name="cli-pipe-reader", daemon=True)
    reader.start()
    return reader


def _join(readers: Iterable[threading.Thread | None], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    for reader in readers:
        if reader is not None:
            reader.join(timeout=max(0.0, deadline - time.monotonic()))


def _wait_quietly(proc: subprocess.Popen[str], timeout: float) -> None:
    with contextlib.suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=max(0.0, timeout))


def _stop(proc: subprocess.Popen[str], *, grace: float) -> None:
    """Terminate ``proc`` (``grace`` > 0) and kill it if still alive; bounded."""
    if proc.poll() is not None:
        return
    if grace > 0:
        with contextlib.suppress(OSError):
            proc.terminate()
        _wait_quietly(proc, grace)
    if proc.poll() is None:
        with contextlib.suppress(OSError):
            proc.kill()
        _wait_quietly(proc, _KILL_WAIT_S)
