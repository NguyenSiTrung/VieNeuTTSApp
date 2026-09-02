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
    engineNote        str, NOTIFY engineNoteChanged — display-only detector
                      readout; ENGINE_NOTE_PENDING ("…") until the deferred
                      hardware probe lands (resolve_engine_note[_async])
    initialWindowGeometry constant QVariantMap — saved window placement for
                      the first show; absent keys mean "use the default"
                      (Main.qml centers at 1120×740)
    saveWindowGeometry(x, y, width, height, maximized) @Slot — persist the
                      placement for the next launch (Main.qml calls on close)

Signals carry no arguments: QML re-reads the NOTIFY property, and Python
tests connect plain callables (direct calls suffice; no event loop). The one
exception is the internal ``_noteResolved(str)`` probe channel, which the
background detector thread uses to marshal its result back to the GUI thread.
"""

from __future__ import annotations

import contextlib
import logging
import threading
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import QT_TRANSLATE_NOOP, Property, QObject, Signal, Slot
from PySide6.QtGui import QGuiApplication

from vienetts_app.core.detector import detect_hardware, detected_engine_info
from vienetts_app.core.settings import load_settings, save_settings
from vienetts_app.ui.theme import (
    PREFERENCES,
    qt_system_theme,
    resolve_theme,
    save_theme,
)

logger = logging.getLogger(__name__)

# Must match Main.qml's minimumWidth/minimumHeight: smaller saved sizes are
# treated as never-placed so the shell falls back to its default geometry.
_WINDOW_MIN_WIDTH = 640
_WINDOW_MIN_HEIGHT = 420

# Shown until the deferred hardware probe lands. The production detector
# imports torch (1-3 s on GPU installs) — construction must stay probe-free
# so the first window paints immediately; app.py kicks the async resolve.
ENGINE_NOTE_PENDING = "…"

# QML nav model (FR-2.3): (id, label) pairs; ids are the only currentTab values.
# Labels are Vietnamese — the app's primary language (ids stay ASCII since they
# are also settings values). QT_TRANSLATE_NOOP scopes the labels to
# ShellBridge for lupdate; translation happens at runtime via self.tr.
TABS: tuple[tuple[str, str], ...] = (
    ("text", QT_TRANSLATE_NOOP("ShellBridge", "Văn bản")),
    ("paragraph", QT_TRANSLATE_NOOP("ShellBridge", "Đoạn văn")),
    ("audiobook", QT_TRANSLATE_NOOP("ShellBridge", "Sách nói")),
    ("cloning", QT_TRANSLATE_NOOP("ShellBridge", "Sao chép giọng")),
    ("settings", QT_TRANSLATE_NOOP("ShellBridge", "Cài đặt")),
)
TAB_IDS = frozenset(tab_id for tab_id, _ in TABS)


def _default_engine_note() -> str:
    """Production detector seam: hardware → capability-view note (no model)."""
    return detected_engine_info(detect_hardware()).note


def _restorable_geometry(
    settings: Any, screens_provider: Callable[[], Sequence[Any]] | None = None
) -> dict[str, object]:
    """Settings → the QML-consumable geometry map (absent fields = fallback).

    Placement is dropped when its top-left corner lies off every connected
    screen (monitor unplugged / resolution changed since the last run) so the
    shell re-centers instead of opening invisibly; sizes below the QML
    minimum are dropped the same way. With no GUI app instance (headless
    tests) the screen check is skipped.
    """
    if screens_provider is None:
        app = QGuiApplication.instance()
        screens = app.screens() if isinstance(app, QGuiApplication) else ()
    else:
        screens = tuple(screens_provider())
    geometry: dict[str, object] = {}
    x, y = settings.window_x, settings.window_y
    if (
        x is not None
        and y is not None
        and (not screens or any(screen.availableGeometry().contains(x, y) for screen in screens))
    ):
        geometry["x"] = x
        geometry["y"] = y
    if settings.window_width is not None and settings.window_width >= _WINDOW_MIN_WIDTH:
        geometry["width"] = settings.window_width
    if settings.window_height is not None and settings.window_height >= _WINDOW_MIN_HEIGHT:
        geometry["height"] = settings.window_height
    if settings.window_maximized:
        geometry["maximized"] = True
    return geometry


class ShellBridge(QObject):
    """Shell state exposed to QML; every dependency is injectable."""

    currentTabChanged = Signal()
    themePreferenceChanged = Signal()
    effectiveThemeChanged = Signal()
    tabsChanged = Signal()
    engineNoteChanged = Signal()
    _noteResolved = Signal(str)  # internal: worker thread → GUI thread

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
        # One settings.json read feeds both the theme preference and the
        # saved window placement (the old path paid a second read per load).
        settings = load_settings(settings_dir)
        self._theme_preference = settings.theme
        self._effective_theme = resolve_theme(self._theme_preference, system=self._system_theme())
        self._window_geometry = _restorable_geometry(settings)
        self._engine_note = ENGINE_NOTE_PENDING
        self._note_thread: threading.Thread | None = None
        self._noteResolved.connect(self._apply_engine_note)

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

    @Property("QVariantList", notify=tabsChanged)
    def tabs(self) -> list[dict[str, str]]:
        return [{"id": tab_id, "label": self.tr(label)} for tab_id, label in TABS]

    @Slot()
    def refreshTabs(self) -> None:
        """Re-emit tabs after a UI-language swap (live switch, no restart).

        The bootstrap swaps the QTranslator, calls ``engine.retranslate()``,
        then this — so the nav re-reads ``self.tr`` under the new language.
        """
        self.tabsChanged.emit()

    # -- theme (FR-2.5) -----------------------------------------------------

    @Property(str, notify=themePreferenceChanged)
    def themePreference(self) -> str:
        return self._theme_preference

    @themePreference.setter
    def themePreference(self, preference: str) -> None:
        """Persist and apply a new preference; invalid values are no-ops."""
        if preference == self._theme_preference or preference not in PREFERENCES:
            return
        try:
            save_theme(preference, self._settings_dir)
        except OSError as exc:  # disk-full/read-only: apply live, never raise into QML
            logger.warning("could not persist theme preference (%s)", exc)
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

    @Property(str, notify=engineNoteChanged)
    def engineNote(self) -> str:
        return self._engine_note

    def resolve_engine_note(self) -> None:
        """Probe hardware synchronously (tests, explicit refresh)."""
        self._apply_engine_note(self._detector())

    def resolve_engine_note_async(self) -> None:
        """Probe hardware on a daemon thread — the GUI thread never blocks
        on the detector's ``import torch`` / ``nvidia-smi`` scan."""
        if self._note_thread is not None and self._note_thread.is_alive():
            return

        def _work() -> None:
            note = self._detector()
            # A RuntimeError means the bridge was torn down mid-probe (app
            # quitting) — the result is simply dropped.
            with contextlib.suppress(RuntimeError):
                self._noteResolved.emit(note)  # auto-queued to the GUI thread

        self._note_thread = threading.Thread(
            target=_work, name="vienetts-engine-note-probe", daemon=True
        )
        self._note_thread.start()

    def _apply_engine_note(self, note: str) -> None:
        if note == self._engine_note:
            return
        self._engine_note = note
        self.engineNoteChanged.emit()

    # -- window placement (persisted through Settings) ------------------------

    @Property("QVariantMap", constant=True)
    def initialWindowGeometry(self) -> dict[str, object]:
        return dict(self._window_geometry)

    @Slot(int, int, int, int, bool)
    def saveWindowGeometry(self, x: int, y: int, width: int, height: int, maximized: bool) -> None:
        """Persist the window placement for the next launch (best effort).

        Load-modify-save so a geometry write never clobbers unrelated
        settings; failures only log — closing must never raise.
        """
        if self._settings_dir is None:
            return
        try:
            save_settings(
                replace(
                    load_settings(self._settings_dir),
                    window_x=int(x),
                    window_y=int(y),
                    window_width=int(width),
                    window_height=int(height),
                    window_maximized=bool(maximized),
                ),
                self._settings_dir,
            )
        except (OSError, ValueError) as exc:
            logger.warning("Could not persist window geometry: %s", exc)
