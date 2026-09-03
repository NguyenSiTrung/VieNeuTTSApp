"""Screenshot harness: capture the real UI at feature states for the README.

Loads the REAL app assembly (``create_app`` — real controllers, real voice
catalog, real engine detection) on the active display, drives each tab into a
representative state — including real synthesis (models must be cached, see
README §Models) — and saves window grabs:

    docs/screenshots/text-studio.png       hero: mixed vi/en text + emotion tag,
                                           waveform overview mid-replay
    docs/screenshots/paragraph-studio.png  long-form document in the paragraph
                                           studio (realistic sample text)
    docs/screenshots/voice-cloning.png     reference clip + a cloned voice entry
    docs/screenshots/audiobook-studio.png  sample EPUB with chapter 1 rendered
                                           and transport paused mid-chapter
    docs/screenshots/settings.png          settings tab, UI switched to English

Usage:
    .venv/bin/python scripts/generate_screenshots.py [outdir]

Driving uses the same seams as the offscreen smoke suites (tab activation via
``bridge.setCurrentTab``, dialog entry points via ``QMetaObject.invokeMethod``),
so no native file dialogs open. Brief audio plays twice (~1.5 s each): the hero
grab is taken mid-replay so the waveform overview shows its accent playhead and
time labels, and the audiobook transport is paused mid-chapter for its grab.
The audiobook library is isolated to a throwaway data dir; the demo cloned
voice and imported fixture book are removed from real user data at the end.
Not part of CI: needs a display and the model cache.
"""

from __future__ import annotations

import shutil
import sys
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from PySide6.QtCore import Q_ARG, QMetaObject, QObject

from vienetts_app.app import create_app
from vienetts_app.ui.audiobook_controller import AudiobookController

ROOT = Path(__file__).resolve().parents[1]
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


def main() -> int:
    out_dir = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "docs" / "screenshots"
    out_dir.mkdir(parents=True, exist_ok=True)
    if SHOT_LIBRARY_DIR.exists():
        shutil.rmtree(SHOT_LIBRARY_DIR)

    def shot_audiobook(controller: Any) -> AudiobookController:
        return AudiobookController(controller, data_dir=SHOT_LIBRARY_DIR)

    app, engine = create_app(audiobook_factory=shot_audiobook)
    window = engine.rootObjects()[0]
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

    # ── Settings: taller window so Synthesis & Audio + Appearance fit ──────
    tab("settingsTab", "settings")
    window.setProperty("height", 1440)
    controller.language = "en"
    pump(app, 1.0)
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


if __name__ == "__main__":
    raise SystemExit(main())
