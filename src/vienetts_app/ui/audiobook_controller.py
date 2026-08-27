"""AudiobookController: QML-facing audiobook studio (FR-A3..A6, FR-A8).

Registered by app.py as the QML context property ``audiobook``. Owns the
AudiobookLibrary workspace, chapter rendering (through the shared worker via
AppController's synthesis-listener seam — one engine, one worker, no second
model), and chapter listening via its own PlaybackController (file playback:
pause/resume/seek + finished() auto-advance — the streaming sink cannot do
any of those).

Architecture (performance-first, spec §Research):
- **render-then-play**: a chapter is synthesized once into its WAV cache;
  every replay, resume, and export reads the file. Renders use the worker's
  segmented stream mode, so RSS stays on the bounded plateau.
- **pipelined pre-render**: starting playback of chapter N submits a render
  of N+1, so by the time N finishes, N+1 is (usually) already on disk and
  auto-advance is gapless.
- **resume**: progress (book/chapter/position/voice) persists on play, seek,
  pause and throttled position ticks; reopening seeks back.
- **serialized with the rest of the app**: exactly one render is in flight;
  intents (play/render) queued while the engine is busy dispatch on
  busyChanged, and app-tab jobs submitted behind a render route normally
  once the listener detaches.

QML surface (context property ``audiobook``):
    books            QVariantList [{id,title,author,chapterCount}]
    currentBookId    str ("" = no book open)   bookTitle / bookAuthor  str
    chapters         QVariantList [{index,title,chars,status,error,current}]
    currentChapterIndex int (-1 = none)        playerState  "stopped"|"playing"|"paused"
    positionMs / durationMs int                renderProgress float 0..1
    renderingIndex   int (-1 = idle)           autoAdvance bool (rw)
    renderVoice      str (rw; "" → app defaultVoice)
    errorText        str
    openEpub(path)->bool   openBook(id)->bool  selectBook(id)  removeBook(id)
    playChapter(i) pause() resume() stopPlay() seek(ms) prevChapter() nextChapter()
    renderChapter(i) renderAllPending() cancelRender()
    exportChapter(i, dir)->str  exportAllReady(dir)->int  chapterWavPath(i)->str
    shutdown()

Status strings: "pending" | "rendering" | "ready" | "failed" (+
per-chapter ``error``). Controller error copy is Vietnamese (UI language);
engine/library passthrough messages surface verbatim.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import Property, QObject, Signal, Slot

from vienetts_app.core.audiobook import (
    CHAPTER_CHAR_LIMIT,
    STATUS_PENDING,
    STATUS_READY,
    AudiobookError,
    AudiobookLibrary,
    BookState,
)
from vienetts_app.core.epub import import_epub
from vienetts_app.ui.playback import PlaybackController
from vienetts_app.workers.inference_worker import CANCELLED_MESSAGE

logger = logging.getLogger(__name__)

SAMPLE_RATE = 48_000

OVERSIZE_CHAPTER_MESSAGE = (
    "Chương {title} quá dài ({chars:,} ký tự, giới hạn {limit:,}). "
    "Hãy dùng bản EPUB có chương ngắn hơn."
)

# How often the listening position is persisted while playing (ms → s).
POSITION_SAVE_INTERVAL_SECONDS = 2.0


def _default_library_factory(data_dir: Path) -> AudiobookLibrary:
    return AudiobookLibrary(Path(data_dir) / "audiobooks")


def _default_player_factory() -> PlaybackController:
    return PlaybackController()


class AudiobookController(QObject):
    """Audiobook state machine exposed to QML; dependencies injectable."""

    booksChanged = Signal()
    currentBookIdChanged = Signal()
    currentBookTitleChanged = Signal()
    currentBookAuthorChanged = Signal()
    chaptersChanged = Signal()
    currentChapterChanged = Signal()
    playerStateChanged = Signal()
    positionMsChanged = Signal()
    durationMsChanged = Signal()
    renderProgressChanged = Signal()
    renderingIndexChanged = Signal()
    autoAdvanceChanged = Signal()
    renderVoiceChanged = Signal()
    errorTextChanged = Signal()

    def __init__(
        self,
        app_controller: Any,
        data_dir: Path | None = None,
        player_factory: Callable[[], PlaybackController] | None = None,
        library_factory: Callable[[Path], AudiobookLibrary] | None = None,
    ) -> None:
        super().__init__()
        from vienetts_app.core.settings import default_data_dir

        self._app = app_controller
        self._data_dir = default_data_dir() if data_dir is None else Path(data_dir)
        factory = _default_library_factory if library_factory is None else library_factory
        self._library = factory(self._data_dir)
        player_factory = _default_player_factory if player_factory is None else player_factory
        self._player = player_factory()

        self._state: BookState | None = None
        self._statuses: dict[int, str] = {}
        self._chapter_errors: dict[int, str] = {}

        self._current_chapter = -1
        self._player_state = "stopped"
        self._position_ms = 0
        self._duration_ms = 0
        self._rendering_index = -1
        self._render_progress = 0.0
        self._render_all = False
        self._play_after_render = -1  # chapter to auto-play once its render lands
        self._queued: tuple[str, int] | None = None  # ("render"|"play", index)
        self._auto_advance = True
        self._render_voice = ""
        self._error_text = ""

        # Resume bookkeeping (FR-A5): chapter/position restored on open.
        self._resume_chapter = -1
        self._resume_position_ms = 0
        self._last_position_save = 0.0

        self._wire_player()
        self._app.busyChanged.connect(self._on_app_busy_changed)
        self._refresh_books()

    # ── player wiring ────────────────────────────────────────────────────────

    def _wire_player(self) -> None:
        self._player.stateChanged.connect(self._on_player_state_changed)
        self._player.finished.connect(self._on_player_finished)
        self._player.positionChanged.connect(self._on_player_position)
        self._player.durationChanged.connect(self._on_player_duration)
        self._player.errorTextChanged.connect(self._on_player_error)

    def _on_player_state_changed(self) -> None:
        state = str(self._player.state)
        if state != self._player_state:
            self._player_state = state
            self.playerStateChanged.emit()
        if state in ("paused", "stopped"):
            self._save_progress(force=True)

    def _on_player_position(self, ms: int) -> None:
        if ms != self._position_ms:
            self._position_ms = ms
            self.positionMsChanged.emit()
        now = time.monotonic()
        if now - self._last_position_save >= POSITION_SAVE_INTERVAL_SECONDS:
            self._save_progress(force=True)

    def _on_player_duration(self, ms: int) -> None:
        if ms != self._duration_ms:
            self._duration_ms = ms
            self.durationMsChanged.emit()

    def _on_player_error(self) -> None:
        message = str(self._player.errorText or "")
        if message:
            self._set_error(message)

    def _on_player_finished(self) -> None:
        self._save_progress(force=True)
        if not self._auto_advance or self._state is None:
            return
        nxt = self._current_chapter + 1
        if nxt < len(self._state.chapters):
            self.playChapter(nxt)

    # ── properties ───────────────────────────────────────────────────────────

    @Property("QVariantList", notify=booksChanged)
    def books(self) -> list[dict[str, Any]]:
        return self._books

    @Property(str, notify=currentBookIdChanged)
    def currentBookId(self) -> str:
        return self._state.record.id if self._state is not None else ""

    @Property(str, notify=currentBookTitleChanged)
    def currentBookTitle(self) -> str:
        return self._state.record.title if self._state is not None else ""

    @Property(str, notify=currentBookAuthorChanged)
    def currentBookAuthor(self) -> str:
        return self._state.record.author if self._state is not None else ""

    @Property("QVariantList", notify=chaptersChanged)
    def chapters(self) -> list[dict[str, Any]]:
        return self._chapters_model()

    @Property(int, notify=currentChapterChanged)
    def currentChapterIndex(self) -> int:
        return self._current_chapter

    @Property(str, notify=playerStateChanged)
    def playerState(self) -> str:
        return self._player_state

    @Property(int, notify=positionMsChanged)
    def positionMs(self) -> int:
        return self._position_ms

    @Property(int, notify=durationMsChanged)
    def durationMs(self) -> int:
        return self._duration_ms

    @Property(float, notify=renderProgressChanged)
    def renderProgress(self) -> float:
        return self._render_progress

    @Property(int, notify=renderingIndexChanged)
    def renderingIndex(self) -> int:
        return self._rendering_index

    @Property(bool, notify=autoAdvanceChanged)
    def autoAdvance(self) -> bool:
        return self._auto_advance

    @autoAdvance.setter
    def autoAdvance(self, value: bool) -> None:
        value = bool(value)
        if value != self._auto_advance:
            self._auto_advance = value
            self.autoAdvanceChanged.emit()

    @Property(str, notify=renderVoiceChanged)
    def renderVoice(self) -> str:
        return self._render_voice

    @renderVoice.setter
    def renderVoice(self, value: str) -> None:
        value = str(value or "")
        if value != self._render_voice:
            self._render_voice = value
            self.renderVoiceChanged.emit()
            self._save_progress(force=True)

    @Property(str, notify=errorTextChanged)
    def errorText(self) -> str:
        return self._error_text

    def _set_error(self, message: str) -> None:
        if message != self._error_text:
            self._error_text = message
            self.errorTextChanged.emit()

    def _chapters_model(self) -> list[dict[str, Any]]:
        if self._state is None:
            return []
        return [
            {
                "index": chapter.index,
                "title": chapter.title,
                "chars": len(chapter.text),
                "status": self._statuses.get(chapter.index, STATUS_PENDING),
                "error": self._chapter_errors.get(chapter.index, ""),
                "current": chapter.index == self._current_chapter,
                "ready": self._library.has_chapter_audio(self._state.record.id, chapter.index),
            }
            for chapter in self._state.chapters
        ]

    def _emit_chapters(self) -> None:
        self.chaptersChanged.emit()

    def _refresh_books(self) -> None:
        self._books = [
            {
                "id": record.id,
                "title": record.title,
                "author": record.author,
                "chapterCount": record.chapter_count,
            }
            for record in self._library.list_books()
        ]
        self.booksChanged.emit()

    # ── library / book lifecycle ─────────────────────────────────────────────

    @Slot(str, result=bool)
    def openEpub(self, path: str) -> bool:  # type: ignore[override]
        """Import an EPUB and open it; errors surface via ``errorText``."""
        try:
            book = import_epub(path)
            record = self._library.add_book(book)
        except FileNotFoundError:
            self._set_error(f"Không tìm thấy tệp: {path}")
            return False
        except Exception as exc:  # noqa: BLE001 - import must never crash the UI
            self._set_error(str(exc))
            return False
        self._set_error("")
        self._refresh_books()
        return self.openBook(record.id)

    @Slot(str, result=bool)
    def openBook(self, book_id: str) -> bool:  # type: ignore[override]
        """Open a shelf book, restoring its chapters and progress (FR-A5)."""
        try:
            state = self._library.load_book(book_id)
        except AudiobookError as exc:
            self._set_error(str(exc))
            return False
        self._stop_playback()
        self._state = state
        self._statuses = dict(state.statuses)
        self._chapter_errors = dict(state.errors)
        self._current_chapter = state.progress.current_chapter
        if not 0 <= self._current_chapter < len(state.chapters):
            self._current_chapter = 0
        self._resume_chapter = self._current_chapter
        self._resume_position_ms = state.progress.position_ms
        if state.progress.voice:
            self._render_voice = state.progress.voice
        self._position_ms = 0
        self._duration_ms = 0
        self.currentBookIdChanged.emit()
        self.currentBookTitleChanged.emit()
        self.currentBookAuthorChanged.emit()
        self.currentChapterChanged.emit()
        self.positionMsChanged.emit()
        self.durationMsChanged.emit()
        self._emit_chapters()
        self._set_error("")
        return True

    @Slot(str)
    def selectBook(self, book_id: str) -> None:
        """Shelf selection: open a book, or clear the view for ""."""
        if not book_id:
            self._stop_playback()
            self._state = None
            self._statuses = {}
            self._chapter_errors = {}
            self._current_chapter = -1
            self.currentBookIdChanged.emit()
            self.currentBookTitleChanged.emit()
            self.currentBookAuthorChanged.emit()
            self.currentChapterChanged.emit()
            self._emit_chapters()
            return
        self.openBook(book_id)

    @Slot(str)
    def removeBook(self, book_id: str) -> None:
        if self._state is not None and self._state.record.id == book_id:
            self._stop_playback()
            self._state = None
            self._statuses = {}
            self._chapter_errors = {}
            self._current_chapter = -1
            self.currentBookIdChanged.emit()
            self.currentBookTitleChanged.emit()
            self.currentBookAuthorChanged.emit()
            self.currentChapterChanged.emit()
            self._emit_chapters()
        self._library.remove_book(book_id)
        self._refresh_books()

    # ── listening ────────────────────────────────────────────────────────────

    @Slot(int)
    def playChapter(self, index: int) -> None:
        """Play chapter ``index`` — from cache, or render-then-play (FR-A4)."""
        if self._state is None or not 0 <= index < len(self._state.chapters):
            return
        if index == self._rendering_index:
            self._queued = ("play", index)  # plays as soon as the render lands
            return
        if self._library.has_chapter_audio(self._state.record.id, index):
            self._play_file(index)
            return
        if self._app.busy:
            self._queued = ("play", index)
            return
        self._start_render(index, play_when_done=True)

    @Slot()
    def pause(self) -> None:
        self._player.pause()

    @Slot()
    def resume(self) -> None:
        self._player.resume()

    @Slot()
    def stopPlay(self) -> None:
        self._stop_playback()

    @Slot(int)
    def seek(self, ms: int) -> None:
        self._player.seek(int(ms))

    @Slot()
    def prevChapter(self) -> None:
        if self._current_chapter > 0:
            self.playChapter(self._current_chapter - 1)

    @Slot()
    def nextChapter(self) -> None:
        if self._state is not None and self._current_chapter < len(self._state.chapters) - 1:
            self.playChapter(self._current_chapter + 1)

    def _play_file(self, index: int) -> None:
        assert self._state is not None
        wav = self._library.chapter_wav_path(self._state.record.id, index)
        self._current_chapter = index
        self.currentChapterChanged.emit()
        self._emit_chapters()
        self._player.play(str(wav))
        if self._resume_chapter == index and self._resume_position_ms > 0:
            self._player.seek(self._resume_position_ms)
        self._resume_chapter = -1
        self._resume_position_ms = 0
        self._save_progress(force=True)
        self._kick()  # pipelined pre-render of the next chapter

    def _stop_playback(self) -> None:
        try:
            self._player.stop()
        except Exception:  # noqa: BLE001 - stopping must never raise
            logger.exception("stopping audiobook playback failed")
        self._queued = None
        self._render_all = False
        self._play_after_render = -1

    # ── rendering ────────────────────────────────────────────────────────────

    @Slot(int)
    def renderChapter(self, index: int) -> None:
        """Render one chapter to its WAV cache (no playback)."""
        if self._state is None or not 0 <= index < len(self._state.chapters):
            return
        if index == self._rendering_index:
            return
        if self._library.has_chapter_audio(self._state.record.id, index):
            return  # cached renders are never repeated (NFR-A1)
        if self._app.busy:
            self._queued = ("render", index)
            return
        self._start_render(index, play_when_done=False)

    @Slot()
    def renderAllPending(self) -> None:
        if self._state is None:
            return
        self._render_all = True
        self._kick()

    @Slot()
    def cancelRender(self) -> None:
        self._queued = None
        self._render_all = False
        if self._rendering_index != -1 or self._app.busy:
            self._app.cancel()

    @Slot(int, result=str)
    def chapterWavPath(self, index: int) -> str:  # type: ignore[override]
        if self._state is None:
            return ""
        return str(self._library.chapter_wav_path(self._state.record.id, index))

    def _start_render(self, index: int, *, play_when_done: bool) -> None:
        assert self._state is not None
        text = self._state.chapters[index].text
        if len(text) > CHAPTER_CHAR_LIMIT:
            message = OVERSIZE_CHAPTER_MESSAGE.format(
                title=self._state.chapters[index].title,
                chars=len(text),
                limit=CHAPTER_CHAR_LIMIT,
            )
            self._statuses[index] = "failed"
            self._chapter_errors[index] = message
            self._library.mark_chapter_failed(self._state.record.id, index, message)
            self._set_error(message)
            self._emit_chapters()
            self._kick()
            return
        voice = self._render_voice or self._app.defaultVoice
        self._app.attach_synthesis_listener(self)
        ok = self._app.submit_stream_for_listener(text, voice)
        if not ok:
            self._app.detach_synthesis_listener()
            self._queued = ("play" if play_when_done else "render", index)
            return
        self._rendering_index = index
        self._render_progress = 0.0
        self.renderingIndexChanged.emit()
        self.renderProgressChanged.emit()
        self._statuses[index] = "rendering"
        self._chapter_errors.pop(index, None)
        if play_when_done:
            self._play_after_render = index
        self._emit_chapters()

    # ── synthesis-listener contract (called by AppController, FR-A8) ────────

    def on_synthesis_progress(self, payload: Any) -> None:
        total = getattr(payload, "total", 0)
        done = getattr(payload, "done", 0)
        fraction = (done / total) if total > 0 else 0.0
        if fraction != self._render_progress:
            self._render_progress = fraction
            self.renderProgressChanged.emit()

    def on_synthesis_done(self, audio: Any) -> None:
        self._app.detach_synthesis_listener()
        index, self._rendering_index = self._rendering_index, -1
        self._render_progress = 1.0 if index >= 0 else 0.0
        self.renderingIndexChanged.emit()
        self.renderProgressChanged.emit()
        if index < 0 or self._state is None:
            return
        try:
            self._library.save_chapter_audio(
                self._state.record.id, index, np.asarray(audio), SAMPLE_RATE
            )
        except AudiobookError as exc:
            self._statuses[index] = "failed"
            self._chapter_errors[index] = str(exc)
            self._set_error(str(exc))
            self._emit_chapters()
            self._kick()
            return
        self._statuses[index] = STATUS_READY
        self._chapter_errors.pop(index, None)
        self._emit_chapters()
        if self._play_after_render == index:
            self._play_after_render = -1
            self._play_file(index)
            return
        self._kick()

    def on_synthesis_error(self, message: str) -> None:
        self._app.detach_synthesis_listener()
        index, self._rendering_index = self._rendering_index, -1
        self._render_progress = 0.0
        self.renderingIndexChanged.emit()
        self.renderProgressChanged.emit()
        cancelled = message == CANCELLED_MESSAGE
        if index < 0 or self._state is None:
            return
        if cancelled:
            # Silent reset (documented policy: cancel is not an error).
            self._statuses[index] = STATUS_PENDING
            self._chapter_errors.pop(index, None)
            self._queued = None
            self._render_all = False
            self._play_after_render = -1
            self._emit_chapters()
            return
        self._statuses[index] = "failed"
        self._chapter_errors[index] = message
        try:
            self._library.mark_chapter_failed(self._state.record.id, index, message)
        except AudiobookError:  # noqa: BLE001 - status text already in memory
            logger.exception("persisting chapter failure failed")
        self._set_error(message)
        self._emit_chapters()
        self._kick()

    # ── engine-idle dispatch (queued intents, pipelines, render-all) ────────

    def _on_app_busy_changed(self) -> None:
        if not self._app.busy:
            self._kick()

    def _kick(self) -> None:
        """Run the next deferred action now that the engine is idle."""
        if self._state is None or self._rendering_index != -1 or self._app.busy:
            return
        if self._queued is not None:
            kind, index = self._queued
            self._queued = None
            if kind == "play":
                self.playChapter(index)
            else:
                self.renderChapter(index)
            return
        if self._player_state == "playing" and self._auto_advance:
            nxt = self._current_chapter + 1
            if 0 <= nxt < len(self._state.chapters) and not self._library.has_chapter_audio(
                self._state.record.id, nxt
            ):
                self.renderChapter(nxt)  # pipelined pre-render
                return
        if self._render_all:
            for chapter in self._state.chapters:
                if self._statuses.get(chapter.index, STATUS_PENDING) == STATUS_PENDING and (
                    not self._library.has_chapter_audio(self._state.record.id, chapter.index)
                ):
                    self.renderChapter(chapter.index)
                    return
            self._render_all = False

    # ── progress persistence (FR-A5) ─────────────────────────────────────────

    def _save_progress(self, *, force: bool) -> None:
        if self._state is None:
            return
        if not force and self._player_state == "stopped":
            return
        self._last_position_save = time.monotonic()
        position = self._position_ms if self._current_chapter >= 0 else 0
        try:
            self._library.set_progress(
                self._state.record.id,
                current_chapter=max(0, self._current_chapter),
                position_ms=int(position),
                voice=self._render_voice,
            )
        except AudiobookError:  # noqa: BLE001 - progress persistence is best-effort
            logger.exception("saving audiobook progress failed")

    # ── export (FR-A6) ───────────────────────────────────────────────────────

    @Slot(int, str, result=str)
    def exportChapter(self, index: int, dest_dir: str) -> str:  # type: ignore[override]
        if self._state is None:
            self._set_error("Chưa mở sách nào.")
            return ""
        try:
            return str(self._library.export_chapter(self._state.record.id, index, dest_dir))
        except AudiobookError as exc:
            self._set_error(str(exc))
            return ""

    @Slot(str, result=int)
    def exportAllReady(self, dest_dir: str) -> int:  # type: ignore[override]
        if self._state is None:
            self._set_error("Chưa mở sách nào.")
            return 0
        exported = 0
        for chapter in self._state.chapters:
            if self._library.has_chapter_audio(self._state.record.id, chapter.index):
                try:
                    self._library.export_chapter(self._state.record.id, chapter.index, dest_dir)
                    exported += 1
                except AudiobookError as exc:
                    self._set_error(str(exc))
                    return exported
        return exported

    # ── lifecycle ────────────────────────────────────────────────────────────

    @Slot()
    def shutdown(self) -> None:
        self._save_progress(force=True)
        self._stop_playback()
        self._app.detach_synthesis_listener()
        self._rendering_index = -1
        self.renderingIndexChanged.emit()
