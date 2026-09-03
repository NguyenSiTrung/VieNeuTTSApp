"""Startup benchmark process isolation (Phase 1 Task 5, TDD RED)."""

from pathlib import Path
from types import SimpleNamespace

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
