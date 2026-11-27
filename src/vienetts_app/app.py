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
import inspect
import logging
import signal
import sys
import time
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

from PySide6.QtCore import (
    QCoreApplication,
    QEvent,
    QLockFile,
    QObject,
    QPointF,
    Qt,
    QTimer,
    Slot,
)
from PySide6.QtGui import QGuiApplication, QIcon, QTouchEvent
from PySide6.QtQml import QQmlApplicationEngine
from PySide6.QtQuick import QQuickItem, QQuickWindow
from PySide6.QtQuickControls2 import QQuickStyle

from vienetts_app.core.detector import detect_hardware, detected_engine_info
from vienetts_app.ui.audiobook_controller import AudiobookController
from vienetts_app.ui.batch_controller import BatchFileController
from vienetts_app.ui.bridge import ShellBridge
from vienetts_app.ui.controller import AppController
from vienetts_app.ui.i18n import translator_for
from vienetts_app.ui.macos import setup_macos_app
from vienetts_app.ui.playback import PlaybackController
from vienetts_app.ui.subtitle_controller import SubtitleController
from vienetts_app.ui.windows import apply_dark_titlebars, setup_windows_app


def connect_managed_cuda_note(bridge: Any, controller: Any) -> bool:
    """Point the engine readout at the managed CUDA runtime; re-resolve on change.

    Frozen builds ship no system torch, so the default hardware probe can
    never report CUDA even with a ready managed runtime. The replacement
    detector folds the controller's managed/driver state into the same
    capability view, and managed-state landings re-resolve the note
    off-thread (never on the GUI thread). Returns False when either side
    lacks the seam (tests/fakes) — the default probe stays in place.
    """
    detector_fn = getattr(controller, "managed_cuda_for_detection", None)
    set_detector = getattr(bridge, "set_detector", None)
    re_resolve = getattr(bridge, "resolve_engine_note_async", None)
    if not callable(detector_fn) or not callable(set_detector) or not callable(re_resolve):
        return False

    def _managed_engine_note() -> str:
        ready, version = detector_fn()
        hw = detect_hardware(managed_cuda_ready=ready, managed_cuda_version=version)
        return detected_engine_info(hw).note

    set_detector(_managed_engine_note)
    for signal_name in ("cudaRuntimeStateChanged", "cudaRuntimeDriverChanged"):
        signal = getattr(controller, signal_name, None)
        if signal is not None and hasattr(signal, "connect"):
            signal.connect(re_resolve)
    return True


QML_DIR = Path(__file__).parent / "ui" / "qml"
MAIN_QML = QML_DIR / "Main.qml"
ASSETS_DIR = Path(__file__).parent / "ui" / "assets"
APP_ICON = ASSETS_DIR / "icon.png"
SINGLE_INSTANCE_LOCK_NAME = "vienetts-app.lock"

logger = logging.getLogger(__name__)


def acquire_single_instance_lock(data_dir: Path | None = None) -> QLockFile | None:
    """Take the single-instance lock; ``None`` when another instance runs.

    The lock file lives in the app data dir and ``QLockFile`` self-heals
    stale locks left by crashes (30 s staleness). The caller must keep the
    returned object alive for the whole process lifetime — ``run_gui``
    anchors it on the ``QGuiApplication``. Returns a lock even when the
    data dir is unusable (fail-open: a second window beats no window).
    """
    from vienetts_app.core.settings import default_data_dir

    base = Path(data_dir) if data_dir is not None else default_data_dir()
    try:
        base.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # Fail open: a second window beats no window when the data dir is
        # unusable. An unlocked QLockFile holds no lock and is safe to drop.
        import tempfile

        logger.debug("Single-instance lock skipped (data dir unusable: %s)", exc)
        return QLockFile(str(Path(tempfile.gettempdir()) / SINGLE_INSTANCE_LOCK_NAME))
    lock = QLockFile(str(base / SINGLE_INSTANCE_LOCK_NAME))
    lock.setStaleLockTime(30_000)
    if lock.tryLock(0):
        return lock
    return None


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


class TabPrebuild(QObject):
    """Flip ``prebuildTabs`` on the root window once its first frame is out.

    Main.qml builds only the landing tab eagerly; the other tabs are
    asynchronous Loaders that this hook activates after the first
    ``frameSwapped``, deferred one more event-loop turn so the prebuild never
    competes with the first paint. ``frameSwapped`` may come from the render
    thread; this receiver lives on the GUI thread, so the slot is queued.
    """

    def __init__(self, win: QQuickWindow) -> None:
        super().__init__(win)
        self._win: QQuickWindow | None = win
        self._cancelled = False
        self._connected = True
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(0)
        self._timer.timeout.connect(self._start)
        win.frameSwapped.connect(self._on_first_frame, Qt.ConnectionType.QueuedConnection)

    def _disconnect_frame_swapped(self, win: QQuickWindow | None) -> None:
        if not self._connected or win is None:
            return
        self._connected = False
        with contextlib.suppress(RuntimeError, TypeError, AttributeError):
            win.frameSwapped.disconnect(self._on_first_frame)

    @Slot()
    def cancel(self) -> None:
        """Stop any pending prebuild timer and detach from the window."""
        self._cancelled = True
        win, self._win = self._win, None
        self._disconnect_frame_swapped(win)
        with contextlib.suppress(RuntimeError):
            self._timer.stop()

    @Slot()
    def _on_first_frame(self) -> None:
        win = self._win
        if self._cancelled or win is None:
            return
        self._disconnect_frame_swapped(win)
        with contextlib.suppress(RuntimeError):
            self._timer.start()

    @Slot()
    def _start(self) -> None:
        win = self._win
        if self._cancelled or win is None:
            return
        with contextlib.suppress(RuntimeError):
            win.setProperty("prebuildTabs", True)


def wait_for_tabs(app: QCoreApplication, window: QObject, timeout_s: float = 20.0) -> bool:
    """Pump ``app`` until every lazy tab on ``window`` has loaded.

    Headless drivers (smoke tests, screenshot and benchmark scripts) look tab
    objects up by name; tabs incubate asynchronously after the first frame
    (see :class:`TabPrebuild`), so they wait on Main.qml's ``tabsReady`` —
    every tab Loader at ``Loader.Ready``. Returns False on timeout.
    """
    deadline = time.monotonic() + timeout_s
    while not window.property("tabsReady"):
        if time.monotonic() >= deadline:
            return False
        app.processEvents()
        time.sleep(0.005)
    return True


def create_app(
    bridge_factory: Callable[[], ShellBridge] | None = None,
    controller_factory: Callable[[], AppController] | None = None,
    playback_factory: Callable[[], PlaybackController] | None = None,
    audiobook_factory: (Callable[[AppController], AudiobookController | Any] | None) = None,
    batch_factory: (Callable[[AppController], BatchFileController | Any] | None) = None,
    subtitle_factory: (Callable[[AppController], SubtitleController | Any] | None) = None,
    startup_observer: Callable[[str], None] | None = None,
) -> tuple[QGuiApplication, QQmlApplicationEngine]:
    """Build the GUI (no ``exec()``); returns ``(app, engine)`` for inspection."""
    from vienetts_app import ensure_windowed_stdio

    ensure_windowed_stdio()
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
    # Paragraph-tab multi-file queue shares the controller's engine/worker
    # (one model load) via the listener seam; construction stays model-free
    # and connects to no app signals, so fake-app smoke scenarios are safe.
    batch = BatchFileController(controller) if batch_factory is None else batch_factory(controller)
    engine.rootContext().setContextProperty("batchController", batch)
    engine._batch = batch  # noqa: SLF001 — lifetime anchor, same as above
    # SRT subtitle studio shares the same engine/worker via the listener seam:
    # cues are synthesized unit by unit and streamed into a cue-aligned track.
    subtitle = (
        SubtitleController(controller) if subtitle_factory is None else subtitle_factory(controller)
    )
    engine.rootContext().setContextProperty("subtitleController", subtitle)
    engine._subtitle = subtitle  # noqa: SLF001 — lifetime anchor, same as above
    if hasattr(controller, "attach_subtitle_controller"):
        controller.attach_subtitle_controller(subtitle)
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
    if not MAIN_QML.is_file():
        raise RuntimeError(
            f"Main.qml not found: {MAIN_QML} — the install is incomplete; "
            "reinstall VieNeuTTS (frozen builds must bundle ui/qml/Main.qml)."
        )
    engine.load(str(MAIN_QML))
    if not engine.rootObjects():
        raise RuntimeError(
            f"Main.qml failed to load: {MAIN_QML} — check the log above for the "
            "QML error (usually a missing import or resource); reinstalling "
            "VieNeuTTS restores a corrupt ui/qml tree."
        )
    if startup_observer is not None:
        startup_observer("qml_loaded")
    root_obj = engine.rootObjects()[0]
    if isinstance(root_obj, QQuickWindow):
        focus_filter = FocusClearFilter(root_obj)
        root_obj.installEventFilter(focus_filter)
        engine._focus_clear_filter = focus_filter  # noqa: SLF001 — lifetime anchor
        root_obj._focus_clear_filter = focus_filter  # noqa: SLF001 — lifetime anchor
        root_obj._tab_prebuild = TabPrebuild(root_obj)  # noqa: SLF001 — lifetime anchor
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
        with contextlib.suppress(RuntimeError):
            wakeup.stop()
            wakeup.deleteLater()


def _schedule_startup_timer(
    app: Any,
    engine: Any,
    delay_ms: int,
    callback: Callable[[], Any],
) -> Any:
    """Create a tracked single-shot startup timer cancelled on teardown."""
    timer = QTimer(app)
    timer.setSingleShot(True)
    timer.setInterval(delay_ms)
    timer.timeout.connect(callback)
    timers: list[Any] | None = getattr(engine, "_startup_timers", None)
    if timers is None:
        timers = []
        engine._startup_timers = timers  # noqa: SLF001 — lifetime anchor for teardown
    timers.append(timer)
    timer.start()
    return timer


def _stop_engine_timers(engine: Any, *, delete: bool = False) -> None:
    """Stop tracked startup/periodic timers and pending tab prebuilds."""
    timers: list[Any] | None = getattr(engine, "_startup_timers", None)
    if timers:
        snapshot = list(timers)
        if delete:
            timers.clear()
        for timer in snapshot:
            if hasattr(timer, "stop"):
                with contextlib.suppress(Exception):
                    timer.stop()
            if delete and hasattr(timer, "deleteLater"):
                with contextlib.suppress(Exception):
                    timer.deleteLater()
    update_timer = getattr(engine, "_update_timer", None)
    if update_timer is not None:
        if hasattr(update_timer, "stop"):
            with contextlib.suppress(Exception):
                update_timer.stop()
        if delete:
            if hasattr(update_timer, "deleteLater"):
                with contextlib.suppress(Exception):
                    update_timer.deleteLater()
            with contextlib.suppress(Exception):
                engine._update_timer = None  # noqa: SLF001
    root_objects_fn = getattr(engine, "rootObjects", None)
    if callable(root_objects_fn):
        with contextlib.suppress(Exception):
            for root in root_objects_fn():
                prebuild = getattr(root, "_tab_prebuild", None)
                if prebuild is not None and hasattr(prebuild, "cancel"):
                    with contextlib.suppress(Exception):
                        prebuild.cancel()


def _shutdown_batch(batch: Any) -> None:
    """Stop any in-flight batch queue render or preview playback."""
    if batch is None:
        return
    shutdown_fn = getattr(batch, "shutdown", None)
    if callable(shutdown_fn):
        with contextlib.suppress(Exception):
            shutdown_fn()
        return
    for method_name in ("cancel", "stopPlay"):
        fn = getattr(batch, method_name, None)
        if callable(fn):
            with contextlib.suppress(Exception):
                fn()


def _shutdown_controller(controller: Any) -> None:
    """Call ``controller.shutdown(drain_pool=True)`` when supported, else zero-arg."""
    shutdown_fn = getattr(controller, "shutdown", None)
    if not callable(shutdown_fn):
        return
    accepts_drain_pool = False
    with contextlib.suppress(ValueError, TypeError):
        params = inspect.signature(shutdown_fn).parameters
        accepts_drain_pool = "drain_pool" in params or any(
            p.kind == inspect.Parameter.VAR_KEYWORD for p in params.values()
        )
    with contextlib.suppress(Exception):
        if accepts_drain_pool:
            shutdown_fn(drain_pool=True)
        else:
            shutdown_fn()


def _release_onnx_sessions() -> None:
    """Release cached ONNX Runtime audio sessions on teardown."""
    with contextlib.suppress(Exception):
        from vienetts_app.core import onnx_audio

        onnx_audio.release_onnx_sessions()


def _shutdown_components(engine: Any, *, delete_timers: bool = False) -> None:
    """Stop startup timers, stop playback/batch, shut down controllers, release ONNX."""
    _stop_engine_timers(engine, delete=delete_timers)
    playback = getattr(engine, "_playback", None)
    stop_playback = getattr(playback, "stop", None)
    if callable(stop_playback):
        with contextlib.suppress(Exception):
            stop_playback()
    _shutdown_batch(getattr(engine, "_batch", None))
    for attr in ("_subtitle", "_audiobook"):
        component = getattr(engine, attr, None)
        shutdown_fn = getattr(component, "shutdown", None)
        if callable(shutdown_fn):
            with contextlib.suppress(Exception):
                shutdown_fn()
    _shutdown_controller(getattr(engine, "_controller", None))
    _release_onnx_sessions()


def run_gui() -> int:
    """GUI entry (FR-2.1): launch the window and run the event loop."""
    lock = acquire_single_instance_lock()
    if lock is None:
        # A second window would double the ~800 MB model footprint and fight
        # over the settings file — hand the user back to the running instance.
        logger.warning("Another VieNeuTTS instance is already running; exiting.")
        from vienetts_app.crash import show_native_error_dialog

        show_native_error_dialog(
            "VieNeuTTS is already running",
            "Một phiên bản VieNeuTTS khác đang chạy (another instance is already "
            "running).\n\nVui lòng dùng cửa sổ đang mở thay vì mở thêm.",
        )
        return 1
    try:
        app, engine = create_app()
    except Exception as exc:
        logger.critical("Failed to create GUI application: %s", exc, exc_info=True)
        from vienetts_app.crash import handle_unhandled_exception

        handle_unhandled_exception(type(exc), exc, exc.__traceback__, thread_name="MainThread")
        return 1
    bridge = engine._bridge  # noqa: SLF001 — anchored by create_app
    # Dropping the lock releases it: anchor it for the process lifetime.
    app._single_instance_lock = lock  # noqa: SLF001 — lifetime anchor, see comment
    controller = engine._controller  # noqa: SLF001 — anchored by create_app

    # Clean engine/worker/timer/playback/batch/ONNX teardown on quit (FR-1.5, FR-2.3).
    def _on_about_to_quit() -> None:
        _shutdown_components(engine, delete_timers=False)

    app.aboutToQuit.connect(_on_about_to_quit)

    # "system" theme tracks the OS palette live (dark↔light without restart):
    # colorSchemeChanged fires on macOS appearance flips, Windows app-mode
    # switches, and Linux DE theme changes.
    def _on_color_scheme_changed(_scheme: Any) -> None:
        bridge.refreshSystemTheme()
        # Windows titlebar follows the OS palette, not the in-app picker —
        # repaint the frames whenever the effective theme (re)resolves.
        apply_dark_titlebars(bridge.effectiveTheme == "dark")

    app.styleHints().colorSchemeChanged.connect(_on_color_scheme_changed)
    # Engine readout follows the managed CUDA runtime (frozen builds have no
    # system torch to probe): re-resolves off-thread whenever managed or
    # driver state lands. No-op for seams without the managed API.
    connect_managed_cuda_note(bridge, controller)
    # Hardware detect runs off-thread after first paint: the production
    # detector probes package metadata and nvidia-smi (never a torch import),
    # which must never sit between app launch and the first window.
    _schedule_startup_timer(app, engine, 100, bridge.resolve_engine_note_async)
    # Torch availability prewarm: same off-thread discipline — the probe
    # behind controller.torchAvailable must never block the GUI thread.
    _schedule_startup_timer(app, engine, 110, controller.resolveTorchAvailabilityAsync)
    # Truthful model readiness (Phase 1 Task 4): filesystem-only inspect runs
    # after first paint, alongside the hardware note. create_app itself stays
    # model-free (NFR-3.1) — offscreen tests never see this.
    _schedule_startup_timer(app, engine, 120, controller.refreshModelState)
    # Engine-profile readiness (Phase 5 Task 5.1): resolves the ACTIVE
    # profile's device + its model/runtime readiness, off the GUI thread and
    # after first paint. For VieNeu this mirrors the status the line above
    # publishes (the official inspect re-hashes every model file, so it is
    # never run twice); a Qwen profile gets its own install + runtime inspected.
    _schedule_startup_timer(app, engine, 125, controller.refreshProfileState)
    # Managed CUDA readiness is likewise a filesystem-only, post-paint
    # inspection. Local diagnostics, downloads, activation, and torch imports
    # are all explicit user actions and never happen during startup.
    _schedule_startup_timer(app, engine, 130, controller.refreshCudaRuntimeState)
    # Silent update check (GitHub Releases, stdlib urllib, ~1 small GET):
    # announces nothing on failure or when current — a dot badge on the
    # Settings nav item + the Settings card surface a newer release.
    # First check 3 s after first paint, then hourly while the app stays
    # open (long studio sessions). Offscreen tests skip this (no net).
    _schedule_startup_timer(app, engine, 3000, controller.checkForUpdatesStartup)
    _update_timer = QTimer(app)
    _update_timer.setInterval(3600_000)
    _update_timer.timeout.connect(controller.checkForUpdatesStartup)
    _update_timer.start()
    engine._update_timer = _update_timer  # noqa: SLF001 — lifetime anchor
    # Background model prewarm (perf): once the shell is interactive and has
    # painted, load the model on the worker thread so the FIRST synthesis
    # click is warm instead of eating the 1.4–1.6 s cold load. create_app
    # itself stays model-free (NFR-3.1) — offscreen tests never see this.
    _schedule_startup_timer(app, engine, 500, controller.prewarm_engine)
    with _sigint_quit(app):
        try:
            return app.exec()
        finally:
            with contextlib.suppress(Exception):
                app.aboutToQuit.disconnect(_on_about_to_quit)
            _teardown_qml(app, engine)


def _teardown_qml(app: QGuiApplication, engine: QQmlApplicationEngine) -> None:
    """Destroy the QML tree while ``create_app``'s anchors are still alive.

    Qt defers the engine's deletion to interpreter shutdown, where Python
    clears module globals in arbitrary order: when ``engine`` (which anchors
    bridge/controller/playback) dies first, the surviving QML tree re-evaluates
    ~190 bindings against null context properties and spams the terminal with
    "Cannot read property 'x' of null" on every close. Deleting the tree here,
    while every anchor still holds a live object, keeps shutdown silent.
    """
    _shutdown_components(engine, delete_timers=True)
    translator = getattr(engine, "_translator", None)
    if translator is not None:
        with contextlib.suppress(Exception):
            app.removeTranslator(translator)
    retired = getattr(app, "_retired_qml_anchors", None)
    if retired is None:
        retired = []
        app._retired_qml_anchors = retired  # noqa: SLF001 — keep context QObjects alive across rebuilds
    for attr in (
        "_bridge",
        "_controller",
        "_playback",
        "_audiobook",
        "_batch",
        "_subtitle",
        "_translator",
        "_focus_clear_filter",
    ):
        obj = getattr(engine, attr, None)
        if obj is not None:
            retired.append(obj)
    for root in engine.rootObjects():
        filt = getattr(root, "_focus_clear_filter", None)
        if filt is not None:
            with contextlib.suppress(Exception):
                root.removeEventFilter(filt)
            filt._win = None  # noqa: SLF001
            root._focus_clear_filter = None  # noqa: SLF001
        prebuild = getattr(root, "_tab_prebuild", None)
        if prebuild is not None:
            if hasattr(prebuild, "cancel"):
                with contextlib.suppress(Exception):
                    prebuild.cancel()
            prebuild._win = None  # noqa: SLF001
            root._tab_prebuild = None  # noqa: SLF001
        with contextlib.suppress(Exception):
            root.setProperty("prebuildTabs", False)
        root.deleteLater()
    for _ in range(3):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
    engine.deleteLater()
    for _ in range(3):
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        app.processEvents()
