"""Ahead-of-time QML for the frozen bundle (perf track 6.5).

Qt's QML loader looks for a compiled unit at ``<source>c`` (``Main.qmlc``,
``helpers.jsc``) next to a local QML/JS file before it consults its per-user
disk cache. The unit carries no source timestamp, only the Qt version and the
QML compile hash, so one built by the same PySide6 that is bundled stays valid
wherever the app is installed. Shipping one per file means the first launch
after install loads bytecode instead of compiling the whole UI (cold first
frame ~0.6 s → ~0.3 s on the dev box).

Units are generated into a staging directory (the PyInstaller workpath),
never into ``src/``: a stale unit beside an edited ``.qml`` in a dev checkout
would be loaded instead of the edit.
"""

from __future__ import annotations

import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SOURCE_SUFFIXES = (".qml", ".js")


def qmlcachegen_path() -> Path:
    """The ``qmlcachegen`` shipped with PySide6 (same lookup as pyside6-qmlcachegen)."""
    import PySide6

    pyside_dir = Path(PySide6.__file__).resolve().parent
    if sys.platform == "win32":
        return pyside_dir / "qmlcachegen.exe"
    return pyside_dir / "Qt" / "libexec" / "qmlcachegen"


def compile_qml_tree(source_dir: Path, out_dir: Path, *, dest: str) -> list[tuple[str, str]]:
    """Compile every QML/JS file under ``source_dir`` into ``out_dir``.

    Returns PyInstaller ``datas`` entries placing each unit beside its source
    under ``dest``. Any file that fails to compile fails the build.
    """
    source_dir = Path(source_dir)
    out_dir = Path(out_dir)
    tool = os.fspath(qmlcachegen_path())
    sources = sorted(p for p in source_dir.rglob("*") if p.suffix in SOURCE_SUFFIXES)

    def compile_one(source: Path) -> tuple[str, str]:
        rel = source.relative_to(source_dir)
        unit = out_dir / rel.parent / (rel.name + "c")
        unit.parent.mkdir(parents=True, exist_ok=True)
        proc = subprocess.run(
            [tool, "--only-bytecode", "-o", os.fspath(unit), os.fspath(source)],
            capture_output=True,
            text=True,
        )
        if proc.returncode != 0 or not unit.is_file():
            raise RuntimeError(f"qmlcachegen failed for {rel}: {proc.stderr.strip()}")
        # qmlcachegen's compile statistics are build noise, never bundled.
        unit.with_name(unit.name + ".aotstats").unlink(missing_ok=True)
        return os.fspath(unit), os.fspath(Path(dest) / rel.parent)

    with ThreadPoolExecutor(max_workers=os.cpu_count() or 4) as pool:
        return list(pool.map(compile_one, sources))
