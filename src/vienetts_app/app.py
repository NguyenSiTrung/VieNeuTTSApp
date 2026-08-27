"""GUI bootstrap: QGuiApplication + QML engine + shell bridge (FR-2.1/FR-2.2).

``create_app`` wires everything and loads ``Main.qml`` without entering the
event loop (the offscreen tests assert on the loaded object tree); ``run_gui``
is the real entry. Nothing here touches the TTS engine — the bridge pulls a
detector-only readout and the AppController builds its voice catalog from the
SDK asset JSON, so startup stays model-free (NFR-2.1/NFR-3.1).
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

from PySide6.QtGui import QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine

from vienetts_app.ui.bridge import ShellBridge
from vienetts_app.ui.controller import AppController

QML_DIR = Path(__file__).parent / "ui" / "qml"
MAIN_QML = QML_DIR / "Main.qml"


def create_app(
    bridge_factory: Callable[[], ShellBridge] | None = None,
    controller_factory: Callable[[], AppController] | None = None,
) -> tuple[QGuiApplication, QQmlApplicationEngine]:
    """Build the GUI (no ``exec()``); returns ``(app, engine)`` for inspection."""
    app = QGuiApplication.instance()
    if app is None:
        app = QGuiApplication(sys.argv)
    elif not isinstance(app, QGuiApplication):
        # Qt aborts deep in QQmlApplicationEngine if only a headless
        # QCoreApplication exists — fail with an actionable message instead.
        raise RuntimeError(
            "a non-GUI QCoreApplication already owns this process; QML requires a QGuiApplication"
        )
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
    engine.load(str(MAIN_QML))
    if not engine.rootObjects():
        raise RuntimeError(f"Main.qml failed to load: {MAIN_QML}")
    return app, engine


def run_gui() -> int:
    """GUI entry (FR-2.1): launch the window and run the event loop."""
    app, engine = create_app()
    controller = engine._controller  # noqa: SLF001 — anchored by create_app
    # Clean engine/worker teardown on quit (FR-3 lifecycle).
    app.aboutToQuit.connect(controller.shutdown)
    return app.exec()
