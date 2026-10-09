"""Startup benchmark process isolation (Phase 1 Task 5, TDD RED)."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from scripts.benchmarks import run_startup


def test_parent_launches_a_fresh_child_per_iteration(monkeypatch, tmp_path) -> None:
    commands: list[list[str]] = []
    real_run = run_startup.subprocess.run

    def fake_run(command, **_kwargs):
        if "--child-output" not in list(command):
            return real_run(command, **_kwargs)
        commands.append(list(command))
        Path(command[command.index("--child-output") + 1]).write_text(
            '{"frame_signal_supported": false, "events": []}\n', encoding="utf-8"
        )
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(run_startup.subprocess, "run", fake_run)
    assert (
        run_startup.run(
            run_startup._parser().parse_args(
                ["--iterations", "3", "--output", str(tmp_path / "out.jsonl")]
            )
        )
        == 0
    )
    assert len(commands) == 3
    assert all("--child-output" in command for command in commands)


TAB_VISIT_PROBE = """
import json, sys, tempfile, time
from pathlib import Path

from PySide6.QtCore import QObject

from vienetts_app.app import create_app
from vienetts_app.ui.controller import AppController

started = time.perf_counter()
app, engine = create_app(controller_factory=lambda: AppController(
    data_dir=Path(tempfile.mkdtemp()), engine_factory=lambda **_k: None,
    worker_factory=lambda _e: None, catalog=lambda: [], saved_names=lambda _v: []))
window = engine.rootObjects()[0]
bridge = engine.rootContext().contextProperty("bridge")
frames = []
window.frameSwapped.connect(lambda: frames.append(time.perf_counter()))

def pump_until(cond, timeout=20.0):
    worst, deadline = 0.0, time.perf_counter() + timeout
    while time.perf_counter() < deadline:
        tick = time.perf_counter()
        app.processEvents()
        worst = max(worst, time.perf_counter() - tick)
        if cond():
            return worst * 1e3
        time.sleep(0.002)
    raise SystemExit("timed out")

pump_until(lambda: bool(frames))
out = {"first_frame_ms": (frames[0] - started) * 1e3}
# Visit Settings right after the first frame, while the prebuild may still
# be incubating: neither the tab switch nor any event-loop turn may block.
tick = time.perf_counter()
bridge.setCurrentTab("settings")
out["settings_switch_ms"] = (time.perf_counter() - tick) * 1e3
out["settings_worst_slice_ms"] = pump_until(
    lambda: bool(window.findChildren(QObject, "settingsTab")))
print("RESULT:" + json.dumps(out))
"""


@pytest.mark.benchmark
def test_first_settings_visit_never_blocks_the_gui_for_100_ms() -> None:
    import json
    import os
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "-c", TAB_VISIT_PROBE],
        capture_output=True,
        text=True,
        env={**os.environ, "QT_QPA_PLATFORM": "offscreen"},
        check=False,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stderr
    (line,) = (ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:"))
    result = json.loads(line.removeprefix("RESULT:"))
    # Before 6.1 the synchronous Loader blocked the switch for ~200-260 ms.
    assert result["settings_switch_ms"] < 100, result
    assert result["settings_worst_slice_ms"] < 100, result
    # No startup regression: create_app -> first frame stayed ~0.43-0.48 s
    # offscreen on the reference box before lazy tabs; keep generous headroom.
    assert result["first_frame_ms"] < 1000, result
