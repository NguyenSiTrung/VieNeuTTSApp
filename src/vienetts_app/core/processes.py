"""Cross-platform process liveness probes.

``os.kill(pid, 0)`` is a POSIX idiom and must never be used as a probe: CPython
maps every signal other than ``CTRL_C_EVENT``/``CTRL_BREAK_EVENT`` onto
``TerminateProcess`` on Windows, so "is this process alive?" would kill it — and
keep answering "alive" while any handle to the exited process is still open.
Anything that needs to know whether a pid is still running goes through
:func:`process_alive`.
"""

from __future__ import annotations

import os
import sys

#: Windows process exit code for "has not exited yet" (``STILL_ACTIVE``).
_STILL_ACTIVE = 259

#: ``PROCESS_QUERY_LIMITED_INFORMATION``: enough to read the exit code.
_QUERY_LIMITED_INFORMATION = 0x1000

#: ``SYNCHRONIZE``: required to wait on the process handle.
_SYNCHRONIZE = 0x00100000

#: ``WAIT_OBJECT_0``: the waited-on handle is signaled (the process exited).
_WAIT_OBJECT_0 = 0

__all__ = ["process_alive"]


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
