"""Screenshot-matrix smoke (ui_shell_redesign_20261010 Task 1.6, AC-8).

Runs ``scripts/generate_screenshots.py --matrix`` in a SUBPROCESS (one
QGuiApplication per process) under ``QT_QPA_PLATFORM=offscreen`` and asserts
it writes ``<screen>-<theme>-<W>x<H>.png`` for every screen — each
``bridge.TABS`` destination, with Tạo giọng đọc's modes and Giọng đọc's views
as separate screens — with the right PNG dimensions. The harness itself rejects null, mis-sized,
single-colour and stale grabs, so a zero exit also means real renders.

Reduced matrix for CI cost: every screen x both themes (exercises the live
theme flip) at 640x420 (exercises the resize from the 1120x740 default). Split
into one item per theme so the two halves run independently (xdist workers), not
as one long serial subprocess.
"""

import os
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from vienetts_app.ui.bridge import TABS

# Every screen the matrix covers: the five destinations, with Tạo giọng đọc
# split into its four modes and Giọng đọc into its two views (FR-3.1), plus
# Sách nói with the fixture book open (master–detail + player, FR-4.1),
# Studio with a two-clip project open (FR-4.3) and Cài đặt's Engine section
# with its "Nâng cao" rows open (FR-4.2).
SCREENS = (
    "create-compose",
    "create-document",
    "create-files",
    "subtitles",
    "audiobook",
    "audiobook-book",
    "voices-library",
    "voices-clone",
    "studio",
    "studio-project",
    "settings",
    "settings-engine",
)

pytestmark = [pytest.mark.smoke, pytest.mark.slow]

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "generate_screenshots.py"
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"


def png_size(path: Path) -> tuple[int, int]:
    """(width, height) from the PNG IHDR chunk — no Qt in the test process."""
    header = path.read_bytes()[:24]
    assert header[:8] == PNG_SIGNATURE, f"{path.name} is not a PNG"
    return struct.unpack(">II", header[16:24])


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_matrix_writes_every_destination_theme_and_size(tmp_path, theme) -> None:
    size = (640, 420)
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--matrix",
            str(tmp_path),
            "--themes",
            theme,
            "--sizes",
            f"{size[0]}x{size[1]}",
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr

    assert {screen.split("-")[0] for screen in SCREENS} == {tab_id for tab_id, _ in TABS}
    # This item covers exactly its one theme; the other theme is its own item,
    # so the two together still cover every screen x theme.
    expected = {f"{screen}-{theme}-{size[0]}x{size[1]}.png" for screen in SCREENS}
    assert {p.name for p in tmp_path.iterdir()} == expected
    for name in expected:
        assert png_size(tmp_path / name) == size, name
    saved = {Path(ln.removeprefix("saved: ")).name for ln in proc.stdout.splitlines()}
    assert expected <= saved, proc.stdout


SUBTITLE_SIZES = ((640, 420), (760, 600), (1120, 740), (1440, 900))


@pytest.mark.parametrize("theme", ["dark", "light"])
def test_subtitles_matrix_populated_at_four_sizes(tmp_path, theme) -> None:
    """Task 5.2: the populated studio (real cues, six rows) at every review size."""
    env = {**os.environ, "QT_QPA_PLATFORM": "offscreen"}
    proc = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--matrix",
            str(tmp_path),
            "--tabs",
            "subtitles",
            "--themes",
            theme,
            "--sizes",
            ",".join(f"{w}x{h}" for w, h in SUBTITLE_SIZES),
        ],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    expected = {f"subtitles-{theme}-{width}x{height}.png" for width, height in SUBTITLE_SIZES}
    assert {p.name for p in tmp_path.iterdir()} == expected
    for name, size in zip(
        (f"subtitles-{theme}-{w}x{h}.png" for w, h in SUBTITLE_SIZES),
        SUBTITLE_SIZES,
        strict=True,
    ):
        assert png_size(tmp_path / name) == size, name
    # A populated page reflows: no two sizes may share the exact same frame.
    assert len({(tmp_path / name).read_bytes() for name in expected}) == len(expected)
