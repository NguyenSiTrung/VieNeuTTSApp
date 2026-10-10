"""Screenshot-matrix smoke (ui_shell_redesign_20261010 Task 1.6, AC-8).

Runs ``scripts/generate_screenshots.py --matrix`` in a SUBPROCESS (one
QGuiApplication per process) under ``QT_QPA_PLATFORM=offscreen`` and asserts
it writes ``<dest>-<theme>-<W>x<H>.png`` for every ``bridge.TABS`` id with
the right PNG dimensions. The harness itself rejects null, mis-sized,
single-colour and stale grabs, so a zero exit also means real renders.

Reduced matrix for CI cost: every destination x both themes (exercises the
live theme flip) at 640x420 (exercises the resize from the 1120x740 default).
The full 24-shot matrix takes ~11 s on a loaded 4-core host; this one ~6 s.
"""

import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from vienetts_app.ui.bridge import TABS

pytestmark = [pytest.mark.smoke, pytest.mark.slow]

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "generate_screenshots.py"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_size(path: Path) -> tuple[int, int]:
    """(width, height) from the PNG IHDR chunk — no Qt in the test process."""
    header = path.read_bytes()[:24]
    assert header[:8] == PNG_SIGNATURE, f"{path.name} is not a PNG"
    return struct.unpack(">II", header[16:24])


def test_matrix_writes_every_destination_theme_and_size(tmp_path) -> None:
    themes, size = ("dark", "light"), (640, 420)
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    proc = subprocess.run(
        [sys.executable, str(SCRIPT), "--matrix", str(tmp_path), "--sizes", "640x420"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    expected = {
        f"{tab_id}-{theme}-{size[0]}x{size[1]}.png" for theme in themes for tab_id, _ in TABS
    }
    assert {p.name for p in tmp_path.iterdir()} == expected
    for name in expected:
        assert png_size(tmp_path / name) == size, name
    saved = {Path(ln.removeprefix("saved: ")).name for ln in proc.stdout.splitlines()}
    assert expected <= saved, proc.stdout
