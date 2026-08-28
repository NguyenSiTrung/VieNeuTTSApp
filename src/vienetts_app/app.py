"""GUI bootstrap: QGuiApplication + QML engine + shell bridge (FR-2.1/FR-2.2).

``create_app`` wires everything and loads ``Main.qml`` without entering the
event loop (the offscreen tests assert on the loaded object tree); ``run_gui``
is the real entry. Nothing here touches the TTS engine — the bridge pulls a
detector-only readout, the AppController builds its voice catalog from the
SDK asset JSON, and the PlaybackController constructs no QtMultimedia objects
until the first play — so startup stays model-free (NFR-2.1/NFR-3.1).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtGui import QGuiApplication, QIcon
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuickControls2 import QQuickStyle

from vienetts_app.ui.audiobook_controller import AudiobookController
from vienetts_app.ui.bridge import ShellBridge
from vienetts_app.ui.controller import AppController
from vienetts_app.ui.i18n import translator_for
from vienetts_app.ui.macos import setup_macos_app
from vienetts_app.ui.playback import PlaybackController

QML_DIR = Path(__file__).parent / "ui" / "qml"
MAIN_QML = QML_DIR / "Main.qml"
ASSETS_DIR = Path(__file__).parent / "ui" / "assets"
APP_ICON = ASSETS_DIR / "icon.png"


def _install_translator(
    app: QGuiApplication, engine: QQmlApplicationEngine, language: str
) -> None:
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


def create_app(
    bridge_factory: Callable[[], ShellBridge] | None = None,
    controller_factory: Callable[[], AppController] | None = None,
    playback_factory: Callable[[], PlaybackController] | None = None,
    audiobook_factory: (Callable[[AppController], AudiobookController | Any] | None) = None,
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
    return app, engine


def run_gui() -> int:
    """GUI entry (FR-2.1): launch the window and run the event loop."""
    app, engine = create_app()
    controller = engine._controller  # noqa: SLF001 — anchored by create_app
    audiobook = engine._audiobook  # noqa: SLF001 — anchored by create_app
    # Clean engine/worker teardown on quit (FR-3 lifecycle).
    app.aboutToQuit.connect(audiobook.shutdown)
    app.aboutToQuit.connect(controller.shutdown)
    return app.exec()
