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


def _windows_process_alive(pid: int) -> bool:  # pragma: no cover - Windows-only path
    """Read the exit code: the only non-destructive liveness probe on Windows."""
    import ctypes

    process = ctypes.windll.kernel32.OpenProcess(_QUERY_LIMITED_INFORMATION, False, pid)
    if not process:
        return False
    try:
        exit_code = ctypes.c_ulong()
        if not ctypes.windll.kernel32.GetExitCodeProcess(process, ctypes.byref(exit_code)):
            return False
        return exit_code.value == _STILL_ACTIVE
    finally:
        ctypes.windll.kernel32.CloseHandle(process)
