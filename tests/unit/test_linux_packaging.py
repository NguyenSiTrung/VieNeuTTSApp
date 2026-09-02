"""Validates the Linux desktop-entry assets shipped in the release zip.

CI stages ``packaging/linux/`` plus the hicolor icon set into
``dist/VieNeuTTS/share/linux/`` (see release.yml); these tests pin the
contract on any development platform.
"""

from __future__ import annotations

import configparser
import os
import shutil
import stat
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
PACKAGING = REPO_ROOT / "packaging" / "linux"
ICONS = REPO_ROOT / "src" / "vienetts_app" / "ui" / "assets" / "icons"


class TestDesktopEntry:
    def test_desktop_file_is_valid_entry(self) -> None:
        parser = configparser.ConfigParser(interpolation=None)  # %f field code
        parser.optionxform = str  # Desktop-entry keys are case-sensitive
        assert parser.read(PACKAGING / "vienetts-app.desktop")
        entry = parser["Desktop Entry"]
        assert entry["Type"] == "Application"
        assert entry["Name"] == "VieNeuTTS"
        assert entry["Icon"] == "vienetts-app"
        assert entry["Exec"].startswith("vienetts")
        assert entry["Terminal"] == "false"
        assert "Audio" in entry["Categories"]
        # Matches app.py's setDesktopFileName so taskbars pair the running
        # window with the launcher.
        assert entry["StartupWMClass"] == "vienetts-app"

    def test_install_script_exists_and_is_executable(self) -> None:
        script = PACKAGING / "install.sh"
        assert script.is_file()
        assert script.stat().st_mode & stat.S_IXUSR

    def test_hicolor_icon_sizes_available(self) -> None:
        sizes = {p.stem.removeprefix("icon_") for p in ICONS.glob("icon_*.png")}
        assert {"16x16", "32x32", "48x48", "128x128", "256x256", "512x512"} <= sizes


class TestInstallScript:
    def test_installs_symlink_entry_and_icons(self, tmp_path: Path) -> None:
        # Reproduce the CI staging layout (dist/VieNeuTTS/share/linux) inside
        # tmp_path, then run the real script against an isolated HOME.
        app_root = tmp_path / "VieNeuTTS"
        stage = app_root / "share" / "linux"
        stage.mkdir(parents=True)
        shutil.copy(PACKAGING / "vienetts-app.desktop", stage / "vienetts-app.desktop")
        shutil.copy(PACKAGING / "install.sh", stage / "install.sh")
        binary = app_root / "VieNeuTTS"
        binary.write_text("#!/bin/sh\n")
        binary.chmod(0o755)
        for size in ("48x48", "256x256"):
            target = stage / "icons" / "hicolor" / size / "apps" / "vienetts-app.png"
            target.parent.mkdir(parents=True)
            shutil.copy(ICONS / f"icon_{size}.png", target)

        home = tmp_path / "home"
        home.mkdir()
        env = dict(os.environ, HOME=str(home), XDG_DATA_HOME=str(home / ".local" / "share"))
        subprocess.run(["sh", str(stage / "install.sh")], env=env, check=True, capture_output=True)

        link = home / ".local" / "bin" / "vienetts"
        assert link.is_symlink()
        assert os.readlink(link) == str(binary)
        assert (home / ".local" / "share" / "applications" / "vienetts-app.desktop").is_file()
        for size in ("48x48", "256x256"):
            installed = (
                home / ".local" / "share" / "icons" / "hicolor" / size / "apps" / "vienetts-app.png"
            )
            assert installed.is_file()
            assert installed.stat().st_size > 0

    def test_fails_clearly_without_frozen_binary(self, tmp_path: Path) -> None:
        stage = tmp_path / "share" / "linux"
        stage.mkdir(parents=True)
        shutil.copy(PACKAGING / "install.sh", stage / "install.sh")
        home = tmp_path / "home"
        home.mkdir()
        env = dict(os.environ, HOME=str(home))
        result = subprocess.run(
            ["sh", str(stage / "install.sh")], env=env, capture_output=True, text=True
        )
        assert result.returncode != 0
        assert "No executable" in result.stderr
