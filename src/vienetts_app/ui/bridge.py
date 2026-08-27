"""Shell bridge: QML-exposed shell state (FR-2.2/FR-2.5/FR-2.7).

A plain QObject (no QThread, no engine wiring) registered by app.py as the
QML context property ``bridge``. It holds the navigation tab, the theme
preference/effective pair (persisted through ui/theme.py), and the
model-free engine readout from the detector capability view (NFR-2.1:
construction never instantiates TTSEngine or loads a model — the detector
seam is injectable so tests never pay for detection either).

QML surface (context property ``bridge``):
    currentTab        str, NOTIFY currentTabChanged — "text"|"paragraph"|
                      "cloning"|"settings"; writes to anything else are no-ops
    setCurrentTab(id) @Slot(str) — plain slot for nav buttons
    tabs              constant QVariantList [{"id": ..., "label": ...}, ...]
                      (built from the module-level TABS (id, label) pairs)
    themePreference   str, NOTIFY themePreferenceChanged — "system"|"light"|
                      "dark"; a write persists via ui/theme.save_theme and
                      re-resolves effectiveTheme
    effectiveTheme    str, NOTIFY effectiveThemeChanged — "dark"|"light",
                      read-only (no WRITE); refresh via refreshSystemTheme()
    refreshSystemTheme() @Slot() — re-resolve after the OS palette changes
    engineNote        str, constant — display-only detector readout

Signals carry no arguments: QML re-reads the NOTIFY property, and Python
tests connect plain callables (direct calls suffice; no event loop).
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

from PySide6.QtCore import Property, QObject, Signal, Slot

from vienetts_app.core.detector import detect_hardware, detected_engine_info
from vienetts_app.ui.theme import (
    PREFERENCES,
    load_theme,
    qt_system_theme,
    resolve_theme,
    save_theme,
)

# QML nav model (FR-2.3): (id, label) pairs; ids are the only currentTab values.
TABS: tuple[tuple[str, str], ...] = (
    ("text", "Text"),
    ("paragraph", "Paragraph"),
    ("cloning", "Cloning"),
    ("settings", "Settings"),
)
TAB_IDS = frozenset(tab_id for tab_id, _ in TABS)


def _default_engine_note() -> str:
    """Production detector seam: hardware → capability-view note (no model)."""
    return detected_engine_info(detect_hardware()).note


class ShellBridge(QObject):
    """Shell state exposed to QML; every dependency is injectable."""

    currentTabChanged = Signal()
    themePreferenceChanged = Signal()
    effectiveThemeChanged = Signal()

    def __init__(
        self,
        settings_dir: Path | None = None,
        detector: Callable[[], str] | None = None,
        system_theme: Callable[[], str] | None = None,
    ) -> None:
        super().__init__()
        self._settings_dir = settings_dir
        self._detector = _default_engine_note if detector is None else detector
        self._system_theme = qt_system_theme if system_theme is None else system_theme
        self._current_tab = "text"
        self._theme_preference = load_theme(settings_dir)
        self._effective_theme = resolve_theme(self._theme_preference, system=self._system_theme())
        self._engine_note = self._detector()

    # -- currentTab (FR-2.2) -------------------------------------------------

    @Property(str, notify=currentTabChanged)
    def currentTab(self) -> str:
        return self._current_tab

    @currentTab.setter
    def currentTab(self, tab: str) -> None:
        self.setCurrentTab(tab)

    @Slot(str)
    def setCurrentTab(self, tab: str) -> None:
        """Switch the active tab; unknown ids are a silent no-op."""
        if tab == self._current_tab or tab not in TAB_IDS:
            return
        self._current_tab = tab
        self.currentTabChanged.emit()

    # -- tabs list (FR-2.3) --------------------------------------------------

    @Property("QVariantList", constant=True)
    def tabs(self) -> list[dict[str, str]]:
        return [{"id": tab_id, "label": label} for tab_id, label in TABS]

    # -- theme (FR-2.5) -----------------------------------------------------

    @Property(str, notify=themePreferenceChanged)
    def themePreference(self) -> str:
        return self._theme_preference

    @themePreference.setter
    def themePreference(self, preference: str) -> None:
        """Persist and apply a new preference; invalid values are no-ops."""
        if preference == self._theme_preference or preference not in PREFERENCES:
            return
        save_theme(preference, self._settings_dir)
        self._theme_preference = preference
        self.themePreferenceChanged.emit()
        self._reapply_theme()

    @Property(str, notify=effectiveThemeChanged)
    def effectiveTheme(self) -> str:
        return self._effective_theme

    @Slot()
    def refreshSystemTheme(self) -> None:
        """Re-resolve the effective theme from the live system theme."""
        self._reapply_theme()

    def _reapply_theme(self) -> None:
        effective = resolve_theme(self._theme_preference, system=self._system_theme())
        if effective != self._effective_theme:
            self._effective_theme = effective
            self.effectiveThemeChanged.emit()

    # -- engine readout (FR-2.7) ---------------------------------------------

    @Property(str, constant=True)
    def engineNote(self) -> str:
        return self._engine_note
