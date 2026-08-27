"""Theme preference persistence + effective-theme resolution (FR-2.4/FR-2.5).

The user picks a *preference* ("system"|"light"|"dark"), persisted in
Settings; the UI runs on an *effective* theme ("dark"|"light"). "system"
follows the live Qt palette; any invalid value degrades to dark so the app
never crashes over a theme string.
"""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication

from vienetts_app.core.settings import load_settings, save_settings

PREFERENCES = frozenset(("system", "light", "dark"))


def resolve_theme(preference: str, system: str = "dark") -> str:
    """Map a stored preference to the effective theme "dark"|"light" (FR-2.4).

    Explicit "dark"/"light" pass through; "system" follows ``system`` (the
    value reported by :func:`qt_system_theme`); any invalid preference — or
    an unknown ``system`` value — falls back to "dark".
    """
    if preference in ("dark", "light"):
        return preference
    if preference == "system":
        return "light" if system == "light" else "dark"
    return "dark"


def qt_system_theme() -> str:
    """Return "dark"|"light" from the live Qt palette.

    Reads ``QGuiApplication.styleHints().colorScheme()``: Dark→"dark",
    Light→"light", Unknown→"dark". With no GUI app instance (headless tests)
    or on any Qt-level failure the safe default is "dark"; never raises.
    """
    app = QGuiApplication.instance()
    if app is None:
        return "dark"
    try:
        scheme = app.styleHints().colorScheme()
    except Exception:  # non-GUI QCoreApplication, torn-down app, ...
        return "dark"
    return "light" if scheme == Qt.ColorScheme.Light else "dark"


def load_theme(data_dir: Path | None = None) -> str:
    """Return the stored theme preference ("system"|"light"|"dark").

    Missing file → the Settings default ("system") via load_settings.
    """
    return load_settings(data_dir).theme


def save_theme(preference: str, data_dir: Path | None = None) -> None:
    """Persist ``preference``, preserving every other settings field.

    Load-modify-save through core/settings.py so a theme switch never
    clobbers unrelated settings. Rejects anything outside
    {"system", "light", "dark"} with ``ValueError``.
    """
    if preference not in PREFERENCES:
        raise ValueError(f"theme must be one of {sorted(PREFERENCES)}, got {preference!r}")
    save_settings(replace(load_settings(data_dir), theme=preference), data_dir)
