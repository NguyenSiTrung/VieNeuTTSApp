"""Packaging sanity: the app package imports from the installed (editable) distribution."""

import re
from pathlib import Path

from vienetts_app.__main__ import main

PROJECT_ROOT = Path(__file__).resolve().parents[2]


def test_package_exposes_version() -> None:
    import vienetts_app

    assert isinstance(vienetts_app.__version__, str)
    assert vienetts_app.__version__.count(".") == 2


def test_release_version_matches_metadata_and_cli_fallback(monkeypatch, capsys) -> None:
    """The source-checkout --version fallback must match release metadata."""
    import vienetts_app
    from vienetts_app import _version

    pyproject_version = re.search(
        r'^version = "([^"]+)"$',
        (PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"),
        flags=re.MULTILINE,
    )
    lock_version = re.search(
        r'\[\[package\]\]\nname = "vienetts-app"\nversion = "([^"]+)"',
        (PROJECT_ROOT / "uv.lock").read_text(encoding="utf-8"),
    )

    assert pyproject_version is not None
    assert lock_version is not None
    expected_version = "0.1.12"
    assert pyproject_version.group(1) == expected_version
    assert lock_version.group(1) == expected_version
    assert vienetts_app.__version__ == expected_version

    monkeypatch.setattr(_version, "BUILD_VERSION", "")
    assert main(["--version"]) == 0
    assert capsys.readouterr().out.strip() == vienetts_app.__version__
