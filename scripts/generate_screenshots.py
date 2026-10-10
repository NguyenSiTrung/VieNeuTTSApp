"""Screenshot harness: README feature shots and the redesign review matrix.

Two modes share one script:

README mode (default) — real models, real display
-------------------------------------------------
Loads the REAL app assembly (``create_app`` — real controllers, real voice
catalog, real engine detection) on the active display, drives each tab into a
representative state — including real synthesis (models must be cached, see
README §Models) — and saves window grabs:

    docs/screenshots/text-studio.png       hero: mixed vi/en text + emotion tag,
                                           waveform overview mid-replay
    docs/screenshots/studio.png            audio studio with the hero artifact,
                                           ops applied, preview paused mid-way
    docs/screenshots/paragraph-studio.png  long-form document in the paragraph
                                           studio (realistic sample text)
    docs/screenshots/voice-cloning.png     reference clip + a cloned voice entry
    docs/screenshots/audiobook-studio.png  sample EPUB with chapter 1 rendered
                                           and transport paused mid-chapter
    docs/screenshots/settings.png          settings tab, UI switched to English

Usage (README mode):
    .venv/bin/python scripts/generate_screenshots.py [outdir]

Driving uses the same seams as the offscreen smoke suites (tab activation via
``bridge.setCurrentTab``, dialog entry points via ``QMetaObject.invokeMethod``),
so no native file dialogs open. Brief audio plays twice (~1.5 s each): the hero
grab is taken mid-replay so the waveform overview shows its accent playhead and
time labels, and the audiobook transport is paused mid-chapter for its grab.
The audiobook library is isolated to a throwaway data dir; the demo cloned
voice and imported fixture book are removed from real user data at the end.
Not part of CI: needs a display and the model cache.

Matrix mode (``--matrix``) — fakes, offscreen, CI-safe
------------------------------------------------------
Captures every destination (each id in ``vienetts_app.ui.bridge.TABS``, read
at run time) x theme {dark, light} x size {1120x740, 640x420} as
``<dest>-<theme>-<W>x<H>.png`` — the per-phase artifact of
ui_shell_redesign_20261010 (AC-8):

    .venv/bin/python scripts/generate_screenshots.py --matrix OUTDIR
    # e.g. OUTDIR = docs/screenshots/redesign/phase-1 (default: .../redesign)
    # reduced: --tabs text,settings --themes dark --sizes 640x420

No models, network, audio device or synthesis: the REAL AppController (and
audiobook/batch/subtitle controllers) runs on a throwaway temp data dir with
a stubbed "installed" model manager, the bridge gets a fixed engine note, and
the UI language is pinned to Vietnamese (the source language). Defaults to
``QT_QPA_PLATFORM=offscreen``: that platform has no GL context, so Qt Quick
falls back to its Software adaptation on its own (no ``QSG_RHI_BACKEND``
needed) and ``QQuickWindow.grabWindow()`` returns full frames. ShaderEffect
items (the cards' ``RectangularShadow``) do not draw under Software, so the
matrix shows flat cards; README mode on a real display keeps the shadows.
Each grab waits until two consecutive grabs match (theme flips animate every
card colour for ``Theme.durationBase``). Importable:
``capture_matrix(out_dir, sizes=..., themes=..., destinations=None)``.
"""

from __future__ import annotations

import argparse
import contextlib
import os
import shutil
import sys
import tempfile
import time
from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from PySide6.QtCore import Q_ARG, QMetaObject, QObject, QPointF
from PySide6.QtQuick import QQuickItem

from vienetts_app.app import create_app, wait_for_tabs
from vienetts_app.core.model_manager import ModelStatus
from vienetts_app.ui.audiobook_controller import AudiobookController
from vienetts_app.ui.controller import AppController

ROOT = Path(__file__).resolve().parents[1]
README_OUT_DIR = ROOT / "docs" / "screenshots"
MATRIX_OUT_DIR = ROOT / "docs" / "screenshots" / "redesign"
MATRIX_SIZES: tuple[tuple[int, int], ...] = ((1120, 740), (640, 420))
MATRIX_THEMES: tuple[str, ...] = ("dark", "light")
FIXTURES = ROOT / "tests" / "fixtures"
REFERENCE_CLIP = Path("/tmp/vienetts_clone_ref.wav")
CLONED_VOICE_NAME = "Giọng của tôi"
DEMO_BOOK_TITLE = "Sách thử nghiệm"
SHOT_LIBRARY_DIR = Path("/tmp/vienetts_shot_library")

HERO_TEXT = (
    "Xin chào! Đây là giọng đọc AI chạy hoàn toàn ngoại tuyến trên máy của bạn. "
    "[cười] And this part switches to English — on-device, zero cloud."
)

PARAGRAPH_TEXT = (
    "Hà Nội những ngày cuối thu, phố phường chìm trong làn sương sớm mỏng manh. "
    "Dọc bờ hồ Gươm, những hàng cây cổ thụ bắt đầu thay lá, tô điểm cho góc "
    "phố một màu vàng ấm áp hiếm có trong năm.\n\n"
    "Người ta vẫn nói mùa thu Hà Nội chỉ dài lắm là vài tuần, những ngày trời "
    "trong xanh, nắng nhẹ như tơ, hương sữa thoang thoảng nơi góc phố. Ai từng "
    "sống qua một mùa thu như thế đều khó lòng quên được cảm giác bình yên ấy.\n\n"
    "Buổi chiều, khi ánh nắng nhạt dần sau những nóc nhà cũ, tiếng xe rời rạc "
    "vảng vất trên mặt hồ. Thành phố chậm lại một nhịp, như đang thong thả "
    "kể lại câu chuyện của mình với người ở lại."
)


def pump(app: Any, seconds: float) -> None:
    """Keep the GUI thread breathing while bindings/animations settle."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.processEvents()
        time.sleep(0.02)


def wait_for(app: Any, predicate: Callable[[], bool], timeout: float, detail: str) -> bool:
    """Spin the event loop until ``predicate`` holds or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        app.processEvents()
        if predicate():
            return True
        time.sleep(0.05)
    print(f"TIMEOUT ({timeout:.0f}s) waiting for {detail}", file=sys.stderr)
    return False


def child(scope: QObject, name: str) -> QObject | None:
    return scope.findChild(QObject, name)


def has_voice(controller: Any, label: str) -> bool:
    for group in controller.voices:
        for voice in group.get("voices", []):
            if voice.get("label") == label:
                return True
    return False


class DocsModelManager:
    """Model manager stub that reports the official baseline as installed.

    This machine synthesizes through the Hugging Face cache, so the
    app-managed official install is usually absent — the real manager then
    keeps the onboarding overlay ("Đang kiểm tra mô hình...") up and it
    photobombs every grab. Docs show the post-setup experience.
    """

    def inspect(self) -> ModelStatus:
        return ModelStatus(
            state="ready",
            installed_bytes=327034699,
            required_bytes=327034699,
            progress=1.0,
            error="",
        )


def capture_readme(out_dir: Path) -> int:
    """README mode: real app, real synthesis, six feature-state grabs."""
    out_dir.mkdir(parents=True, exist_ok=True)
    if SHOT_LIBRARY_DIR.exists():
        shutil.rmtree(SHOT_LIBRARY_DIR)

    def shot_audiobook(controller: Any) -> AudiobookController:
        return AudiobookController(controller, data_dir=SHOT_LIBRARY_DIR)

    def shot_controller() -> AppController:
        return AppController(model_manager_factory=lambda _data_dir: DocsModelManager())

    app, engine = create_app(audiobook_factory=shot_audiobook, controller_factory=shot_controller)
    window = engine.rootObjects()[0]
    # Tabs incubate asynchronously after the first frame; shots need them all.
    if not wait_for_tabs(app, window):
        raise RuntimeError("tab Loaders never became ready")
    controller = engine._controller  # noqa: SLF001 — anchored by create_app
    audiobook = engine._audiobook  # noqa: SLF001 — anchored by create_app
    bridge = engine._bridge  # noqa: SLF001 — anchored by create_app

    # Deterministic geometry + branded dark theme for consistent docs shots.
    window.setProperty("width", 1120)
    window.setProperty("height", 740)
    controller.theme = "dark"
    # Shots 1–4 use the Vietnamese UI (the source language); only the settings
    # demo flips to English. The user's persisted choice is restored at the end.
    original_language = controller.language
    controller.language = "vi"
    pump(app, 0.6)

    # run_gui schedules the first model inspect 120 ms after first paint;
    # create_app alone never does, so the onboarding overlay would sit in its
    # initial "checking" state and cover every grab (the stubbed manager
    # resolves it to "ready" and the overlay clears).
    controller.refreshModelState()
    wait_for(app, lambda: controller.modelReady, 10, "model ready")

    saved: list[Path] = []

    def grab(name: str) -> None:
        path = out_dir / name
        image = window.grabWindow()
        if image.save(str(path)):
            saved.append(path)
            print(f"saved: {path}")
        else:
            print(f"FAILED: {path}", file=sys.stderr)

    def tab(object_name: str, tab_id: str) -> QObject | None:
        bridge.setCurrentTab(tab_id)
        pump(app, 0.3)
        return child(window, object_name)

    # ── Text studio (hero): batch synthesis → replay for the lit waveform ───
    text_tab = tab("textTab", "text")
    if text_tab is not None:
        editor = child(text_tab, "textEditor")
        editor.setProperty("text", HERO_TEXT)
        pump(app, 0.2)
        controller.generate(HERO_TEXT, "")
        if wait_for(
            app,
            lambda: controller.hasAudio and not controller.busy,
            240,
            "batch synthesis (first run also loads the ONNX model)",
        ):
            wait_for(app, lambda: bool(controller.waveformEnvelope), 10, "waveform envelope")
            controller.exportWav(str(REFERENCE_CLIP))
            # Mid-replay the overview shows the accent playhead + time labels
            # (idle it renders only the low-contrast dim shape by design).
            controller.replay()
            if wait_for(app, lambda: controller.replayActive, 10, "replay start"):
                pump(app, 1.4)
                grab("text-studio.png")
                controller.stopReplay()
            else:
                pump(app, 0.3)
                grab("text-studio.png")

    # ── Audio studio: hero artifact loaded, op stack applied, paused preview ─
    # Runs right after the text shot so the hero WAV is still the current
    # artifact; openInStudio wraps it as a clip project. Two ops give the Op
    # Stack card real entries, and a paused preview leaves the master
    # waveform's playhead + position labels lit for the grab.
    studio_tab = tab("studioTab", "studio")
    if studio_tab is not None and controller.openInStudio("text", HERO_TEXT):
        controller.studioPushNormalize()
        controller.studioPushFade("in", 200)
        wait_for(app, lambda: bool(controller.studioEnvelope), 30, "studio overview")
        controller.studioPreview()
        if wait_for(app, lambda: controller.replayActive, 60, "studio preview render"):
            pump(app, 1.2)
            controller.pauseReplay()
            pump(app, 0.3)
        grab("studio.png")
        controller.stopReplay()

    # ── Paragraph studio: realistic long-form document ──────────────────────
    para_tab = tab("paragraphTab", "paragraph")
    if para_tab is not None:
        child(para_tab, "paragraphEditor").setProperty("text", PARAGRAPH_TEXT)
        pump(app, 0.3)
        grab("paragraph-studio.png")

    # ── Voice cloning: consent → reference clip → cloned voice entry ────────
    clone_tab = tab("cloningTab", "cloning")
    if clone_tab is not None:
        controller.acknowledgeConsent()
        pump(app, 0.2)
        if REFERENCE_CLIP.is_file():
            QMetaObject.invokeMethod(
                clone_tab, "selectClip", Q_ARG("QVariant", str(REFERENCE_CLIP))
            )
            child(clone_tab, "voiceNameField").setProperty("text", CLONED_VOICE_NAME)
            pump(app, 0.2)
            if not has_voice(controller, CLONED_VOICE_NAME):
                controller.addVoice(CLONED_VOICE_NAME, str(REFERENCE_CLIP), False)
                wait_for(
                    app,
                    lambda: has_voice(controller, CLONED_VOICE_NAME),
                    180,
                    "voice cloning",
                )
            pump(app, 0.4)
        grab("voice-cloning.png")

    # ── Audiobook studio: fixture EPUB, chapter 1 rendered, paused mid-way ──
    tab("audiobookTab", "audiobook")
    if audiobook.openEpub(str(FIXTURES / "sample.epub")):
        pump(app, 0.4)
        book_id = next(
            (str(b.get("id")) for b in audiobook.books if b.get("title") == DEMO_BOOK_TITLE),
            None,
        )
        if book_id and audiobook.openBook(book_id):
            wait_for(app, lambda: len(audiobook.chapters) > 0, 15, "chapter list")
            audiobook.renderChapter(0)
            if wait_for(app, lambda: audiobook.renderingIndex == 0, 10, "render start"):
                wait_for(app, lambda: audiobook.renderingIndex == -1, 120, "chapter render")
            # Paused mid-chapter: the dock waveform, playhead and position
            # labels all render, without committing to a full playback.
            audiobook.playChapter(0)
            pump(app, 1.3)
            audiobook.pause()
            pump(app, 0.3)
            grab("audiobook-studio.png")
            audiobook.stopPlay()
        else:
            grab("audiobook-studio.png")

    # ── Settings: scrolled to the speech-tuning half, UI in English ────────
    # The screen caps the window below the full page height, so one grab
    # cannot show everything. Anchor the reading-speed control near the top:
    # the viewport then covers speed/pause/live preview down through the
    # Appearance (language) section — the parts the README walks through.
    settings_item = tab("settingsTab", "settings")
    window.setProperty("height", 1440)  # screen clamps to its visible frame
    controller.language = "en"
    pump(app, 1.0)
    if settings_item is not None:
        scroll = child(settings_item, "pageScrollView")
        speed = settings_item.findChild(QQuickItem, "speedSpin")
        if scroll is not None and speed is not None:
            flick = scroll.property("contentItem")
            if flick is not None:
                target_y = speed.mapToItem(flick, QPointF(0, 0)).y() - 90
                max_y = flick.property("contentHeight") - flick.property("height")
                flick.setProperty("contentY", max(0.0, min(target_y, max_y)))
                pump(app, 0.4)
    grab("settings.png")

    # ── Cleanup: no demo artifacts in the user's real settings/library ──────
    controller.language = original_language
    if has_voice(controller, CLONED_VOICE_NAME):
        controller.removeVoice(CLONED_VOICE_NAME)
    real_library = AudiobookController(controller)  # default data_dir = user's
    for book in real_library.books:
        if book.get("title") == DEMO_BOOK_TITLE:
            real_library.removeBook(str(book["id"]))
            break

    audiobook.shutdown()
    controller.shutdown()
    print(f"captured {len(saved)} screenshot(s) in {out_dir}")
    return 0 if saved else 1


# ── Matrix mode ─────────────────────────────────────────────────────────────


def matrix_file_name(destination: str, theme: str, size: tuple[int, int]) -> str:
    """``<dest>-<theme>-<W>x<H>.png`` — the stable matrix artifact name."""
    return f"{destination}-{theme}-{size[0]}x{size[1]}.png"


def _build_matrix_app(data_dir: Path) -> tuple[Any, Any]:
    """``create_app`` with every controller rooted in ``data_dir`` (no user data).

    Each factory gets the throwaway dir explicitly — ``default_data_dir()``
    (the user's real profile) is never consulted. Background work runs inline
    (``run_sync``) so the model-state refresh lands before the first grab.
    """
    from vienetts_app.ui.batch_controller import BatchFileController
    from vienetts_app.ui.bg_ops import run_sync
    from vienetts_app.ui.bridge import ShellBridge
    from vienetts_app.ui.chapter_persist import SyncPersistExecutor
    from vienetts_app.ui.subtitle_controller import SubtitleController

    return create_app(
        bridge_factory=lambda: ShellBridge(
            settings_dir=data_dir,
            detector=lambda: "ONNX · CPU",
            system_theme=lambda: "dark",
        ),
        controller_factory=lambda: AppController(
            data_dir=data_dir,
            model_manager_factory=lambda _data_dir: DocsModelManager(),
            # Normal playback posture without touching the audio stack: the
            # probe only reports; nothing plays, so no device is opened.
            audio_probe=lambda: True,
            bg_runner=run_sync,
        ),
        audiobook_factory=lambda controller: AudiobookController(
            controller,
            data_dir=data_dir / "audiobooks",
            bg_runner=run_sync,
            persist_executor=SyncPersistExecutor(),
        ),
        batch_factory=lambda controller: BatchFileController(controller, data_dir=data_dir),
        subtitle_factory=lambda controller: SubtitleController(controller, data_dir=data_dir),
    )


def _distinct_colours(image: Any, limit: int = 2) -> int:
    """Count distinct pixel values on a coarse grid (stops at ``limit``)."""
    seen: set[int] = set()
    width, height = image.width(), image.height()
    for y in range(0, height, max(1, height // 48)):
        for x in range(0, width, max(1, width // 48)):
            seen.add(image.pixel(x, y))
            if len(seen) >= limit:
                return len(seen)
    return len(seen)


def capture_matrix(
    out_dir: Path,
    sizes: Sequence[tuple[int, int]] = MATRIX_SIZES,
    themes: Sequence[str] = MATRIX_THEMES,
    destinations: Sequence[str] | None = None,
    settle_timeout_s: float = 5.0,
    stable_timeout_s: float = 3.0,
) -> list[Path]:
    """Grab every destination x theme x size with fakes; return written paths.

    ``destinations=None`` means every id in ``bridge.TABS`` (read now, so tab
    renames need no edit here). Needs a fresh process — Qt allows one
    ``QGuiApplication`` per process — and defaults ``QT_QPA_PLATFORM`` to
    ``offscreen``. Raises ``RuntimeError`` on a null, mis-sized, single-colour
    or byte-identical-to-previous grab (blank or stale frame).
    """
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QGuiApplication

    from vienetts_app.app import _teardown_qml
    from vienetts_app.core.settings import load_settings, save_settings
    from vienetts_app.ui.bridge import ENGINE_NOTE_PENDING, TABS

    tab_ids = [tab_id for tab_id, _label in TABS]
    dests = tab_ids if destinations is None else list(destinations)
    unknown = sorted(set(dests) - set(tab_ids))
    if unknown:
        raise ValueError(f"unknown destination(s) {unknown}; known: {tab_ids}")
    bad_themes = sorted(set(themes) - {"dark", "light"})
    if bad_themes:
        raise ValueError(f"unknown theme(s) {bad_themes}; use dark/light")
    if not dests or not themes or not sizes:
        raise ValueError("empty matrix: need at least one destination, theme and size")
    if QGuiApplication.instance() is None:
        os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    saved: list[Path] = []
    with tempfile.TemporaryDirectory(prefix="vienetts-shots-") as tmp:
        data_dir = Path(tmp)
        # Vietnamese is the source language: pin it so the host locale never
        # flips the matrix to English. The first theme is set up front so the
        # window paints in it from the start.
        save_settings(
            replace(load_settings(data_dir), language="vi", theme=themes[0]),
            data_dir,
        )
        app, engine = _build_matrix_app(data_dir)
        window = engine.rootObjects()[0]
        controller = engine._controller  # noqa: SLF001 — anchored by create_app
        bridge = engine._bridge  # noqa: SLF001 — anchored by create_app
        try:
            if not wait_for_tabs(app, window):
                raise RuntimeError("tab Loaders never became ready")
            # run_gui schedules this after first paint; create_app does not.
            # The stub resolves "ready" inline, clearing the setup overlay.
            controller.refreshModelState()
            if not wait_for(app, lambda: controller.modelReady, 10, "model ready"):
                raise RuntimeError("model state never reported ready")

            # The footer engine readout resolves on a worker thread (the
            # detector is the fixed stub above); run_gui kicks it post-paint.
            bridge.resolve_engine_note_async()
            wait_for(app, lambda: bridge.engineNote != ENGINE_NOTE_PENDING, 5, "engine note")

            frames = [0]
            window.frameSwapped.connect(lambda: frames.__setitem__(0, frames[0] + 1))

            def settle() -> None:
                # Two presented frames: layouts polish during the frame sync.
                # update() forces a frame even when the change was a no-op.
                for _ in range(2):
                    seen = frames[0]
                    window.update()
                    if not wait_for(
                        app, lambda seen=seen: frames[0] > seen, settle_timeout_s, "frame"
                    ):
                        raise RuntimeError("window stopped presenting frames")

            def stable_grab(name: str) -> Any:
                # Theme flips animate every card colour (Behavior on color,
                # Theme.durationBase): two frames are not enough, so grab
                # until two grabs ~60 ms apart match. A never-settling scene
                # (a spinner) is kept with a warning rather than failing.
                settle()
                image = window.grabWindow()
                deadline = time.monotonic() + stable_timeout_s
                while True:
                    pump(app, 0.06)
                    settle()
                    again = window.grabWindow()
                    if again == image:
                        return again
                    image = again
                    if time.monotonic() >= deadline:
                        print(f"WARNING: {name} never settled; kept last grab", file=sys.stderr)
                        return image

            previous = QByteArray()
            for theme in themes:
                bridge.themePreference = theme
                for width, height in sizes:
                    window.setWidth(width)
                    window.setHeight(height)
                    for dest in dests:
                        bridge.setCurrentTab(dest)
                        if not window.findChildren(QObject, dest + "Tab"):
                            raise RuntimeError(f"destination {dest!r} has no {dest}Tab item")
                        if bridge.effectiveTheme != theme:
                            raise RuntimeError(f"theme {theme!r} did not apply")
                        name = matrix_file_name(dest, theme, (width, height))
                        image = stable_grab(name)
                        if image.isNull():
                            raise RuntimeError(f"{name}: grabWindow returned a null image")
                        dpr = image.devicePixelRatio() or 1.0
                        got = (round(image.width() / dpr), round(image.height() / dpr))
                        if got != (width, height):
                            raise RuntimeError(f"{name}: grabbed {got}, want {(width, height)}")
                        if _distinct_colours(image) < 2:
                            raise RuntimeError(f"{name}: blank grab (one colour)")
                        encoded = QByteArray()
                        buffer = QBuffer(encoded)
                        buffer.open(QIODevice.OpenModeFlag.WriteOnly)
                        image.save(buffer, "PNG")
                        buffer.close()
                        if encoded == previous:
                            raise RuntimeError(f"{name}: identical to the previous grab (stale)")
                        previous = encoded
                        path = out_dir / name
                        path.write_bytes(encoded.data())
                        saved.append(path)
                        print(f"saved: {path}", flush=True)
        finally:
            for component in (engine._subtitle, engine._audiobook, controller):  # noqa: SLF001
                with contextlib.suppress(Exception):
                    component.shutdown()
            _teardown_qml(app, engine)
    return saved


def _parse_size(text: str) -> tuple[int, int]:
    try:
        width, height = (int(part) for part in text.lower().split("x"))
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"size must be WxH, got {text!r}") from exc
    return width, height


def _csv(text: str) -> list[str]:
    return [part.strip() for part in text.split(",") if part.strip()]


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="README feature shots (default) or the redesign review matrix (--matrix)."
    )
    parser.add_argument(
        "outdir",
        nargs="?",
        type=Path,
        help=f"output dir (README: {README_OUT_DIR}; matrix: {MATRIX_OUT_DIR})",
    )
    parser.add_argument(
        "--matrix",
        action="store_true",
        help="offscreen destination x theme x size grabs with fakes (no models)",
    )
    parser.add_argument(
        "--tabs", type=_csv, default=None, help="matrix: comma-separated tab ids (default: all)"
    )
    parser.add_argument(
        "--themes",
        type=_csv,
        default=list(MATRIX_THEMES),
        help="matrix: comma-separated themes (default: dark,light)",
    )
    parser.add_argument(
        "--sizes",
        type=lambda text: [_parse_size(part) for part in _csv(text)],
        default=list(MATRIX_SIZES),
        help="matrix: comma-separated WxH sizes (default: 1120x740,640x420)",
    )
    args = parser.parse_args(argv)
    if not args.matrix:
        return capture_readme(args.outdir or README_OUT_DIR)
    out_dir = args.outdir or MATRIX_OUT_DIR
    saved = capture_matrix(out_dir, sizes=args.sizes, themes=args.themes, destinations=args.tabs)
    print(f"captured {len(saved)} screenshot(s) in {out_dir}")
    return 0 if saved else 1


if __name__ == "__main__":
    raise SystemExit(main())
