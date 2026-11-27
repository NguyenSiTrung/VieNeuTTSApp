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
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import IO, Any

#: Windows process exit code for "has not exited yet" (``STILL_ACTIVE``).
_STILL_ACTIVE = 259

#: ``PROCESS_TERMINATE``: required to assign a process handle to a Job Object.
_PROCESS_TERMINATE = 0x0001

#: ``PROCESS_SET_QUOTA``: required to assign a process handle to a Job Object.
_PROCESS_SET_QUOTA = 0x0100

#: ``PROCESS_QUERY_LIMITED_INFORMATION``: enough to read the exit code.
_QUERY_LIMITED_INFORMATION = 0x1000

#: ``SYNCHRONIZE``: required to wait on the process handle.
_SYNCHRONIZE = 0x00100000

#: ``WAIT_OBJECT_0``: the waited-on handle is signaled (the process exited).
_WAIT_OBJECT_0 = 0

#: ``CREATE_NO_WINDOW``: the Windows creation flag that suppresses a console.
_CREATE_NO_WINDOW = int(getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000))

#: ``JobObjectExtendedLimitInformation`` information class.
_JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS = 9

#: ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``: closing the job kills every member.
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

#: Timeout for an ``ffprobe`` (or ``ffmpeg -i``) media probe.
FFPROBE_TIMEOUT_S = 30.0

#: Timeout for a ``--help`` / ``-version`` runnability probe of a native CLI.
PROBE_TIMEOUT_S = 15.0

#: Floor of the audio-extraction timeout; see :func:`extraction_timeout_s`.
EXTRACT_TIMEOUT_FLOOR_S = 120.0

#: Default UTF-8 byte ceiling retained per pipe in :func:`run_cli` (tail kept).
DEFAULT_MAX_OUTPUT_BYTES = 4 * 1024 * 1024

#: Tighter UTF-8 tail ceiling when a live ``on_stdout_line`` / ``on_stderr_line``
#: callback is already streaming that pipe.
STREAMED_MAX_OUTPUT_BYTES = 64 * 1024

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
    "DEFAULT_MAX_OUTPUT_BYTES",
    "EXTRACT_TIMEOUT_FLOOR_S",
    "FFPROBE_TIMEOUT_S",
    "PROBE_TIMEOUT_S",
    "STREAMED_MAX_OUTPUT_BYTES",
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


def _kernel32() -> Any:  # pragma: no cover - Windows-only path
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
    kernel32.CreateJobObjectW.restype = wintypes.HANDLE
    kernel32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
    kernel32.SetInformationJobObject.restype = wintypes.BOOL
    kernel32.SetInformationJobObject.argtypes = [
        wintypes.HANDLE,
        ctypes.c_int,
        wintypes.LPVOID,
        wintypes.DWORD,
    ]
    kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
    kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel32.TerminateJobObject.restype = wintypes.BOOL
    kernel32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    return kernel32


_JOB_EXTENDED_LIMIT_INFO_CLS: type[Any] | None = None


def _new_kill_on_close_job_info() -> Any:
    """Build ``JOBOBJECT_EXTENDED_LIMIT_INFORMATION`` with kill-on-close set."""
    global _JOB_EXTENDED_LIMIT_INFO_CLS
    if _JOB_EXTENDED_LIMIT_INFO_CLS is None:
        import ctypes

        class _BasicLimitInfo(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32),
            ]

        class _IoCounters(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class _ExtendedLimitInfo(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BasicLimitInfo),
                ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        _JOB_EXTENDED_LIMIT_INFO_CLS = _ExtendedLimitInfo

    info = _JOB_EXTENDED_LIMIT_INFO_CLS()
    info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    return info


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
_child_pgids: dict[subprocess.Popen[str], int] = {}
_child_jobs: dict[subprocess.Popen[str], int] = {}
_children_lock = threading.Lock()


def _attach_windows_job(proc: subprocess.Popen[str]) -> int | None:
    """Assign ``proc`` to a kill-on-close Job Object; ``None`` if unavailable."""
    import ctypes

    try:
        kernel32 = _kernel32()
    except (AttributeError, OSError):
        return None
    job = kernel32.CreateJobObjectW(None, None)
    if not job:
        return None
    opened_handle: int | None = None
    try:
        info = _new_kill_on_close_job_info()
        if not kernel32.SetInformationJobObject(
            job,
            _JOB_OBJECT_EXTENDED_LIMIT_INFORMATION_CLASS,
            ctypes.byref(info),
            ctypes.sizeof(info),
        ):
            kernel32.CloseHandle(job)
            return None
        proc_handle = getattr(proc, "_handle", None)
        if proc_handle is None:
            opened_handle = kernel32.OpenProcess(
                _PROCESS_SET_QUOTA | _PROCESS_TERMINATE, False, proc.pid
            )
            if not opened_handle:
                kernel32.CloseHandle(job)
                return None
            target_handle = opened_handle
        else:
            target_handle = int(proc_handle)
        if not kernel32.AssignProcessToJobObject(job, target_handle):
            kernel32.CloseHandle(job)
            return None
        return int(job)
    except (AttributeError, OSError, TypeError, ValueError):
        with contextlib.suppress(Exception):
            kernel32.CloseHandle(job)
        return None
    finally:
        if opened_handle:
            with contextlib.suppress(Exception):
                kernel32.CloseHandle(opened_handle)


def _close_windows_job_handle(job: int, *, terminate: bool) -> bool:
    """Terminate (optional) and close a Windows Job Object handle."""
    try:
        kernel32 = _kernel32()
    except (AttributeError, OSError):
        return False
    terminated = False
    try:
        if terminate:
            terminated = bool(kernel32.TerminateJobObject(job, 1))
    except (AttributeError, OSError):
        terminated = False
    finally:
        with contextlib.suppress(Exception):
            kernel32.CloseHandle(job)
    return terminated


def _close_windows_job(proc: subprocess.Popen[str], *, terminate: bool) -> bool:
    with _children_lock:
        job = _child_jobs.pop(proc, None)
    if not job:
        return False
    return _close_windows_job_handle(job, terminate=terminate)


def _unregister_child(proc: subprocess.Popen[str]) -> None:
    with _children_lock:
        _children.discard(proc)
        _child_pgids.pop(proc, None)
        job = _child_jobs.pop(proc, None)
    if job:
        _close_windows_job_handle(job, terminate=False)


def _posix_pgid(proc: subprocess.Popen[str]) -> int | None:
    """Process group ID owned by ``proc`` (never our own group), or ``None``."""
    my_pgid = os.getpgrp()
    if proc.poll() is None:
        with contextlib.suppress(OSError):
            pgid = os.getpgid(proc.pid)
            if pgid > 0 and pgid != my_pgid:
                return pgid
    with _children_lock:
        recorded = _child_pgids.get(proc)
    if recorded is not None and recorded > 0 and recorded != my_pgid:
        return recorded
    return None


def _pgid_alive(pgid: int) -> bool:
    """Whether any process remains in POSIX process group ``pgid``."""
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _signal_posix_tree(
    proc: subprocess.Popen[str],
    sig: int,
    fallback: Callable[[], None],
    *,
    pgid: int | None = None,
) -> None:
    target_pgid = _posix_pgid(proc) if pgid is None else pgid
    if target_pgid is not None:
        try:
            os.killpg(target_pgid, sig)
            return
        except ProcessLookupError:
            if proc.poll() is not None:
                return
        except OSError:
            pass
    if proc.poll() is None:
        with contextlib.suppress(OSError):
            fallback()


def _terminate_tree(proc: subprocess.Popen[str], *, pgid: int | None = None) -> None:
    if os.name == "nt":
        if not _close_windows_job(proc, terminate=True) and proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.terminate()
        return
    _signal_posix_tree(proc, signal.SIGTERM, proc.terminate, pgid=pgid)


def _kill_tree(proc: subprocess.Popen[str], *, pgid: int | None = None) -> None:
    if os.name == "nt":
        if not _close_windows_job(proc, terminate=True) and proc.poll() is None:
            with contextlib.suppress(OSError):
                proc.kill()
        return
    _signal_posix_tree(proc, signal.SIGKILL, proc.kill, pgid=pgid)


def registered_children() -> tuple[subprocess.Popen[str], ...]:
    """Children spawned here that are still running; exited ones are dropped."""
    stale_jobs: list[int] = []
    with _children_lock:
        for proc in [proc for proc in _children if proc.poll() is not None]:
            _children.discard(proc)
            _child_pgids.pop(proc, None)
            job = _child_jobs.pop(proc, None)
            if job:
                stale_jobs.append(job)
        live = tuple(_children)
    for job in stale_jobs:
        _close_windows_job_handle(job, terminate=False)
    return live


def terminate_children(timeout: float = 2.0) -> int:
    """Stop every registered child tree: terminate, wait up to ``timeout``, then kill.

    Meant for shutdown. Returns how many children were still running.
    """
    live = registered_children()
    pgids = {proc: (_posix_pgid(proc) if os.name != "nt" else None) for proc in live}
    for proc in live:
        _terminate_tree(proc, pgid=pgids[proc])
    deadline = time.monotonic() + max(0.0, float(timeout))
    for proc in live:
        _wait_quietly(proc, deadline - time.monotonic())
        pgid = pgids[proc]
        if pgid is not None:
            while _pgid_alive(pgid) and time.monotonic() < deadline:
                time.sleep(min(_POLL_S, max(0.0, deadline - time.monotonic())))
    for proc in live:
        pgid = pgids[proc]
        if proc.poll() is None or (pgid is not None and _pgid_alive(pgid)):
            _kill_tree(proc, pgid=pgid)
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
    windows = os.name == "nt"
    if windows:
        # Windowed/frozen builds must not flash a console window per spawn.
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    else:
        # Own session/process group so stop/kill reaps grandchildren too.
        kwargs["start_new_session"] = True
    proc = subprocess.Popen([os.fspath(arg) for arg in cmd], **kwargs)
    job: int | None = None
    registered_job = False
    try:
        pgid: int | None = None
        if windows:
            job = _attach_windows_job(proc)
        else:
            pid = getattr(proc, "pid", None)
            if isinstance(pid, int) and pid > 0 and pid != os.getpgrp():
                pgid = pid
        with _children_lock:
            _children.add(proc)
            if job:
                _child_jobs[proc] = job
                registered_job = True
            if pgid is not None:
                _child_pgids[proc] = pgid
        return proc
    except BaseException:
        if job and not registered_job:
            _close_windows_job_handle(job, terminate=True)
        _stop(proc, grace=0.0)
        _unregister_child(proc)
        for stream in (proc.stdout, proc.stderr):
            if stream is not None:
                with contextlib.suppress(Exception):
                    stream.close()
        raise


class _TailBuffer:
    """Thread-safe UTF-8 byte-capped tail buffer for captured CLI output."""

    def __init__(self, max_bytes: int) -> None:
        self._max_bytes = max(0, int(max_bytes))
        self._chunks: deque[tuple[str, int]] = deque()
        self._total_bytes = 0
        self._lock = threading.Lock()

    def append(self, line: str) -> None:
        if self._max_bytes <= 0 or not line:
            return
        raw = line.encode("utf-8")
        size = len(raw)
        with self._lock:
            if size >= self._max_bytes:
                self._chunks.clear()
                tail = (
                    line
                    if size == self._max_bytes
                    else raw[-self._max_bytes :].decode("utf-8", errors="ignore")
                )
                tail_size = len(tail.encode("utf-8"))
                if tail:
                    self._chunks.append((tail, tail_size))
                self._total_bytes = tail_size
                return
            self._chunks.append((line, size))
            self._total_bytes += size
            while self._total_bytes > self._max_bytes and self._chunks:
                _, dropped = self._chunks.popleft()
                self._total_bytes -= dropped

    def text(self) -> str:
        with self._lock:
            return "".join(chunk for chunk, _ in self._chunks)


def run_cli(
    cmd: Sequence[str | os.PathLike[str]],
    *,
    timeout: float | None = None,
    cancel_check: Callable[[], bool] | None = None,
    cwd: str | os.PathLike[str] | None = None,
    env: Mapping[str, str] | None = None,
    on_stdout_line: Callable[[str], None] | None = None,
    on_stderr_line: Callable[[str], None] | None = None,
    max_output_bytes: int | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run a native CLI to completion and return its exit status and output.

    Both pipes are drained by reader threads (so a chatty child can never fill
    one and deadlock), and each line is also handed to ``on_stdout_line`` /
    ``on_stderr_line`` as it arrives, from the reader thread; an exception
    in a callback is swallowed so the drain keeps going. Retained output is
    tail-buffered to ``max_output_bytes`` (defaulting to
    :data:`DEFAULT_MAX_OUTPUT_BYTES`, or :data:`STREAMED_MAX_OUTPUT_BYTES` for
    a stream with a live line callback) so long noisy runs cannot OOM.

    ``cancel_check`` is polled every 50 ms; when it returns True the child tree
    is terminated (killed after a short grace) and :class:`CliCancelled` is
    raised. Past ``timeout`` seconds the child tree is killed and
    :class:`CliTimeout` is raised. A non-zero exit is NOT an error here: the
    caller reads ``returncode``. Spawn failures raise ``OSError``.
    """
    argv = [os.fspath(arg) for arg in cmd]
    timeout_s = None if timeout is None else max(0.0, float(timeout))
    explicit_cap = None if max_output_bytes is None else max(0, int(max_output_bytes))
    stdout_cap = (
        explicit_cap
        if explicit_cap is not None
        else (STREAMED_MAX_OUTPUT_BYTES if on_stdout_line is not None else DEFAULT_MAX_OUTPUT_BYTES)
    )
    stderr_cap = (
        explicit_cap
        if explicit_cap is not None
        else (STREAMED_MAX_OUTPUT_BYTES if on_stderr_line is not None else DEFAULT_MAX_OUTPUT_BYTES)
    )
    proc = spawn_cli(argv, cwd=cwd, env=env)
    stdout_reader: threading.Thread | None = None
    stderr_reader: threading.Thread | None = None
    try:
        stdout_buf = _TailBuffer(stdout_cap)
        stderr_buf = _TailBuffer(stderr_cap)
        stdout_reader = _start_reader(proc.stdout, stdout_buf, on_stdout_line)
        stderr_reader = _start_reader(proc.stderr, stderr_buf, on_stderr_line)
        readers = (stdout_reader, stderr_reader)
        deadline = None if timeout_s is None else time.monotonic() + timeout_s
        while True:
            if cancel_check is not None and cancel_check():
                _stop(proc, grace=_CANCEL_GRACE_S)
                _join(readers, _KILLED_READER_JOIN_S)
                raise CliCancelled(
                    argv,
                    returncode=proc.returncode,
                    stdout=stdout_buf.text(),
                    stderr=stderr_buf.text(),
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
                        stdout=stdout_buf.text(),
                        stderr=stderr_buf.text(),
                    )
                wait_s = min(wait_s, remaining)
            try:
                proc.wait(timeout=wait_s)
                break
            except subprocess.TimeoutExpired:
                continue
        _join(readers, _EXITED_READER_JOIN_S)
        return subprocess.CompletedProcess(
            argv, proc.returncode, stdout_buf.text(), stderr_buf.text()
        )
    except (CliCancelled, CliTimeout):
        raise
    except BaseException:
        # A raising setup step, cancel_check, or interrupt must not leak a running child.
        _stop(proc, grace=0.0)
        _join((stdout_reader, stderr_reader), _KILLED_READER_JOIN_S)
        for stream, reader in ((proc.stdout, stdout_reader), (proc.stderr, stderr_reader)):
            if stream is not None and (reader is None or not reader.is_alive()):
                with contextlib.suppress(Exception):
                    stream.close()
        raise
    finally:
        _unregister_child(proc)


def _start_reader(
    stream: IO[str] | None,
    sink: _TailBuffer,
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
    """Terminate ``proc`` and its process tree (``grace`` > 0), then kill; bounded."""
    pgid = _posix_pgid(proc) if os.name != "nt" else None
    tree_alive = proc.poll() is None or (pgid is not None and _pgid_alive(pgid))
    if not tree_alive:
        if os.name == "nt":
            _close_windows_job(proc, terminate=False)
        return
    if grace > 0:
        _terminate_tree(proc, pgid=pgid)
        deadline = time.monotonic() + grace
        _wait_quietly(proc, deadline - time.monotonic())
        if pgid is not None:
            while _pgid_alive(pgid) and time.monotonic() < deadline:
                time.sleep(min(_POLL_S, max(0.0, deadline - time.monotonic())))
    if proc.poll() is None or (pgid is not None and _pgid_alive(pgid)):
        _kill_tree(proc, pgid=pgid)
        _wait_quietly(proc, _KILL_WAIT_S)
