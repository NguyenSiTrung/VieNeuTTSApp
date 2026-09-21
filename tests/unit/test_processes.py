"""Process liveness: the shared replacement for ``os.kill(pid, 0)``.

``os.kill(pid, 0)`` is a POSIX idiom that CPython maps onto ``TerminateProcess``
on Windows, so "is this pid alive?" would kill it there. These tests pin the
contract the release smoke and the fake-host helpers rely on: a running pid
reads as alive, a reaped one as gone, and probing never disturbs the process.
"""

from __future__ import annotations

import subprocess
import sys

from vienetts_app.core.processes import process_alive


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
