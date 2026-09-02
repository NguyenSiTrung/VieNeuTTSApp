"""GUI bootstrap: QGuiApplication + QML engine + shell bridge (FR-2.1/FR-2.2).

``create_app`` wires everything and loads ``Main.qml`` without entering the
event loop (the offscreen tests assert on the loaded object tree); ``run_gui``
is the real entry. Nothing here touches the TTS engine — the bridge pulls a
detector-only readout, the AppController builds its voice catalog from the
SDK asset JSON, and the PlaybackController constructs no QtMultimedia objects
until the first play — so startup stays model-free (NFR-2.1/NFR-3.1).
"""

from __future__ import annotations

import contextlib
import signal
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from PySide6.QtCore import QEvent, QObject, QPointF, QTimer
from PySide6.QtGui import QGuiApplication, QIcon, QTouchEvent
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtQuickControls2 import QQuickStyle

from vienetts_app.ui.audiobook_controller import AudiobookController
from vienetts_app.ui.bridge import ShellBridge
from vienetts_app.ui.controller import AppController
from vienetts_app.ui.i18n import translator_for
from vienetts_app.ui.macos import setup_macos_app
from vienetts_app.ui.playback import PlaybackController
from vienetts_app.ui.windows import apply_dark_titlebars, setup_windows_app

QML_DIR = Path(__file__).parent / "ui" / "qml"
MAIN_QML = QML_DIR / "Main.qml"
ASSETS_DIR = Path(__file__).parent / "ui" / "assets"
APP_ICON = ASSETS_DIR / "icon.png"


def _install_translator(app: QGuiApplication, engine: QQmlApplicationEngine, language: str) -> None:
    """Swap the UI-language translator (``None`` = Vietnamese source, no catalog)."""
    previous = getattr(engine, "_translator", None)
    if previous is not None:
        app.removeTranslator(previous)
    translator = translator_for(language)
    if translator is not None:
        app.installTranslator(translator)
    # QGuiApplication owns installed translators; the engine handle marks
    # which language the live UI is showing (and keeps the last one
    # referenced for teardown/tests).
    engine._translator = translator  # noqa: SLF001 — lifetime anchor, see comment


def is_click_inside_active_control(active_item: QQuickItem, win_pos: QPointF) -> bool:
    """Return True if ``win_pos`` (in window coordinates) falls within the
    active input item or its immediate control container (e.g. SpinBox).
    """
    if active_item is None:
        return False
    local_pos = active_item.mapFromItem(None, win_pos)
    if active_item.contains(local_pos):
        return True

    # Check immediate container (SpinBox, direct editor ScrollView, etc.)
    p = active_item.parentItem()
    depth = 0
    while p is not None and depth < 3:
        class_name = p.metaObject().className()
        is_spinbox = "SpinBox" in class_name
        is_editor_scroll = "ScrollView" in class_name and "PageShell" not in (
            p.parentItem().metaObject().className() if p.parentItem() else ""
        )
        if (is_spinbox or is_editor_scroll) and p.contains(p.mapFromItem(None, win_pos)):
            return True
        p = p.parentItem()
        depth += 1
    return False


def clear_item_focus(item: QQuickItem) -> None:
    """Clear focus on ``item`` and any parent focus scopes (e.g. SpinBox)."""
    item.setProperty("focus", False)
    p = item.parentItem()
    while p is not None:
        if p.property("focus"):
            p.setProperty("focus", False)
        p = p.parentItem()


class FocusClearFilter(QObject):
    """Event filter on QQuickWindow that clears active focus from editable text
    inputs when clicking or tapping anywhere outside the active input control.
    """

    def __init__(self, win: QQuickWindow) -> None:
        super().__init__(win)
        self._win = win

    def eventFilter(self, watched: QObject, event: QEvent) -> bool:
        event_type = event.type()
        if event_type == QEvent.Type.MouseButtonPress:
            pos = event.position()
            self._handle_press(watched, pos)
        elif event_type == QEvent.Type.TouchBegin:
            if isinstance(event, QTouchEvent) and event.points():
                pos = event.points()[0].position()
                self._handle_press(watched, pos)
        return super().eventFilter(watched, event)

    def _handle_press(self, watched: QObject, win_pos: QPointF) -> None:
        win: QQuickWindow | None = (
            watched if isinstance(watched, QQuickWindow) else getattr(self, "_win", None)
        )
        if win is None:
            parent = self.parent()
            if isinstance(parent, QQuickWindow):
                win = parent
        if win is None:
            return
        active_item = win.activeFocusItem()
        if active_item is None:
            return
        has_text_cursor = (
            active_item.property("cursorPosition") is not None
            and active_item.property("text") is not None
        )
        if has_text_cursor and not is_click_inside_active_control(active_item, win_pos):
            clear_item_focus(active_item)


def create_app(
    bridge_factory: Callable[[], ShellBridge] | None = None,
    controller_factory: Callable[[], AppController] | None = None,
    playback_factory: Callable[[], PlaybackController] | None = None,
    audiobook_factory: (Callable[[AppController], AudiobookController | Any] | None) = None,
    startup_observer: Callable[[str], None] | None = None,
) -> tuple[QGuiApplication, QQmlApplicationEngine]:
    """Build the GUI (no ``exec()``); returns ``(app, engine)`` for inspection."""
    app = QGuiApplication.instance()
    if app is None:
        QQuickStyle.setStyle("Basic")
        app = QGuiApplication(sys.argv)
    elif not isinstance(app, QGuiApplication):
        # Qt aborts deep in QQmlApplicationEngine if only a headless
        # QCoreApplication exists — fail with an actionable message instead.
        raise RuntimeError(
            "a non-GUI QCoreApplication already owns this process; QML requires a QGuiApplication"
        )
    else:
        QQuickStyle.setStyle("Basic")

    app.setApplicationName("VieNeuTTS")
    app.setApplicationDisplayName("VieNeuTTS")
    app.setOrganizationName("VieNeuTTS")
    app.setOrganizationDomain("vienetts.ai")
    app.setDesktopFileName("vienetts-app")
    if APP_ICON.is_file():
        app.setWindowIcon(QIcon(str(APP_ICON)))
    setup_macos_app(app_name="VieNeuTTS", icon_path=APP_ICON if APP_ICON.is_file() else None)
    setup_windows_app()

    engine = QQmlApplicationEngine()
    # qmldir/`import "."` resolution needs the QML dir on the import path.
    engine.addImportPath(str(QML_DIR))

    bridge = ShellBridge() if bridge_factory is None else bridge_factory()
    engine.rootContext().setContextProperty("bridge", bridge)
    # setContextProperty does NOT take ownership: keep a Python reference on
    # the engine or the bridge is garbage-collected and QML sees `null`.
    engine._bridge = bridge  # noqa: SLF001 — lifetime anchor, see comment
    controller = AppController() if controller_factory is None else controller_factory()
    engine.rootContext().setContextProperty("controller", controller)
    engine._controller = controller  # noqa: SLF001 — lifetime anchor, see comment
    # PlaybackController construction is lazy (no QtMultimedia objects until
    # the first play), so registering it here keeps startup audio-stack-free.
    playback = PlaybackController() if playback_factory is None else playback_factory()
    engine.rootContext().setContextProperty("playback", playback)
    engine._playback = playback  # noqa: SLF001 — lifetime anchor, see comment
    # Large-audio Phát replays through the same player from a self-cleaning
    # temp WAV; the seam also wires EndOfMedia → replay cleanup.
    if hasattr(controller, "attach_file_playback"):
        controller.attach_file_playback(playback)
    # Audiobook studio shares the controller's engine/worker (one model load)
    # and builds its own file player lazily — construction stays model-free.
    audiobook = (
        AudiobookController(controller)
        if audiobook_factory is None
        else audiobook_factory(controller)
    )
    engine.rootContext().setContextProperty("audiobook", audiobook)
    engine._audiobook = audiobook  # noqa: SLF001 — lifetime anchor, see comment
    # UI language: the translator must be installed BEFORE engine.load() so
    # qsTr/self.tr resolve in the startup language the controller pinned at
    # construction. Vietnamese is the source language — no catalog, no
    # translator.
    _install_translator(app, engine, controller.appliedLanguage)

    # Live language switch (no restart): swap translators, re-evaluate every
    # qsTr binding (retranslate), and re-emit the Python-side nav labels.
    def _apply_language_live() -> None:
        _install_translator(app, engine, controller.language)
        engine.retranslate()
        bridge.refreshTabs()

    controller.languageChanged.connect(_apply_language_live)
    engine.load(str(MAIN_QML))
    if not engine.rootObjects():
        raise RuntimeError(f"Main.qml failed to load: {MAIN_QML}")
    if startup_observer is not None:
        startup_observer("qml_loaded")
    root_obj = engine.rootObjects()[0]
    if isinstance(root_obj, QQuickWindow):
        focus_filter = FocusClearFilter(root_obj)
        root_obj.installEventFilter(focus_filter)
        engine._focus_clear_filter = focus_filter  # noqa: SLF001 — lifetime anchor
        root_obj._focus_clear_filter = focus_filter  # noqa: SLF001 — lifetime anchor
    return app, engine


@contextlib.contextmanager
def _sigint_quit(app: QGuiApplication) -> Iterator[None]:
    """Ctrl+C from a terminal must quit cleanly while ``exec()`` blocks.

    Qt's C++ event loop never returns control to the interpreter, so SIGINT
    stays pending until Python runs bytecode again; a short no-op QTimer
    wakes Python just often enough to service the handler, and the handler
    quits via the normal path (aboutToQuit still tears engines and workers
    down — no KeyboardInterrupt traceback on the user's terminal).
    """
    previous_handler = signal.getsignal(signal.SIGINT)

    def _on_sigint(_signum: int, _frame: Any) -> None:
        app.quit()

    signal.signal(signal.SIGINT, _on_sigint)
    wakeup = QTimer(app)
    wakeup.timeout.connect(lambda: None)
    wakeup.start(200)
    try:
        yield
    finally:
        signal.signal(signal.SIGINT, previous_handler)
        wakeup.stop()


def run_gui() -> int:
    """GUI entry (FR-2.1): launch the window and run the event loop."""
    app, engine = create_app()
    bridge = engine._bridge  # noqa: SLF001 — anchored by create_app
    controller = engine._controller  # noqa: SLF001 — anchored by create_app
    audiobook = engine._audiobook  # noqa: SLF001 — anchored by create_app
    # Clean engine/worker teardown on quit (FR-3 lifecycle).
    app.aboutToQuit.connect(audiobook.shutdown)
    app.aboutToQuit.connect(controller.shutdown)

    # "system" theme tracks the OS palette live (dark↔light without restart):
    # colorSchemeChanged fires on macOS appearance flips, Windows app-mode
    # switches, and Linux DE theme changes.
    def _on_color_scheme_changed(_scheme: Any) -> None:
        bridge.refreshSystemTheme()
        # Windows titlebar follows the OS palette, not the in-app picker —
        # repaint the frames whenever the effective theme (re)resolves.
        apply_dark_titlebars(bridge.effectiveTheme == "dark")

    app.styleHints().colorSchemeChanged.connect(_on_color_scheme_changed)
    apply_dark_titlebars(bridge.effectiveTheme == "dark")
    # Hardware detect runs off-thread after first paint: the production
    # detector imports torch (1–3 s on GPU installs), which must never sit
    # between app launch and the first window.
    QTimer.singleShot(100, bridge.resolve_engine_note_async)
    # Background model prewarm (perf): once the shell is interactive and has
    # painted, load the model on the worker thread so the FIRST synthesis
    # click is warm instead of eating the 1.4–1.6 s cold load. create_app
    # itself stays model-free (NFR-3.1) — offscreen tests never see this.
    QTimer.singleShot(500, controller.prewarm_engine)
    with _sigint_quit(app):
        return app.exec()
