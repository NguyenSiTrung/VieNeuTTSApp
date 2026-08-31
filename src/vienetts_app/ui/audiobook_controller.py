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
    positionMs / durationMs int                chapterEnvelope QVariantList[float 0..1]
                                               (overview of the playing chapter;
                                               empty until one is loaded)
    renderProgress float 0..1
    renderingIndex   int (-1 = idle)           autoAdvance bool (rw)
    renderVoice      str (rw; "" → app defaultVoice)
    renderEtaMs      int (-1 unknown)          renderAllTotal / renderAllDone int
    readerOpen bool (rw)                       paragraphs QVariantList
    activeParagraph int (-1 = none)            activeCharStart / activeCharEnd int
    syncAvailable bool
    errorText        str
    openEpub(path)->bool   openBook(id)->bool  selectBook(id)  removeBook(id)
    playChapter(i) pause() resume() stopPlay() seek(ms) seekToParagraph(i)
    prevChapter() nextChapter()
    renderChapter(i) renderAllPending() cancelRender()
    exportChapter(i, dir)->str  exportAllReady(dir)->int  chapterWavPath(i)->str
    shutdown()

Status strings: "pending" | "rendering" | "ready" | "failed" (+
per-chapter ``error``). Controller error copy is Vietnamese (UI language);
engine/library passthrough messages surface verbatim.
"""

from __future__ import annotations

import bisect
import logging
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np
from PySide6.QtCore import QT_TRANSLATE_NOOP, Property, QObject, QTimer, Signal, Slot

from vienetts_app.core.audio import compute_waveform_envelope, read_wav
from vienetts_app.core.audiobook import (
    CHAPTER_CHAR_LIMIT,
    STATUS_PENDING,
    STATUS_READY,
    STATUS_RENDERING,
    AudiobookError,
    AudiobookLibrary,
    BookState,
)
from vienetts_app.core.engine import split_text_for_streaming
from vienetts_app.core.epub import import_epub
from vienetts_app.core.timeline import (
    Timeline,
    active_word,
    build_timeline,
    estimate_timeline,
    locate_segment,
    paragraph_start_ms,
    split_paragraphs,
    word_spans,
)
from vienetts_app.ui.playback import PlaybackController
from vienetts_app.workers.inference_worker import CANCELLED_MESSAGE

logger = logging.getLogger(__name__)

SAMPLE_RATE = 48_000

OVERSIZE_CHAPTER_MESSAGE = QT_TRANSLATE_NOOP(
    "AudiobookController", "Chương {title} quá dài ({chars:,} ký tự, giới hạn {limit:,}). "
) + QT_TRANSLATE_NOOP("AudiobookController", "Hãy dùng bản EPUB có chương ngắn hơn.")

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
    chapterEnvelopeChanged = Signal()
    renderProgressChanged = Signal()
    renderingIndexChanged = Signal()
    autoAdvanceChanged = Signal()
    renderVoiceChanged = Signal()
    errorTextChanged = Signal()
    renderEtaMsChanged = Signal()
    renderAllTotalChanged = Signal()
    renderAllDoneChanged = Signal()
    readerOpenChanged = Signal()
    paragraphsChanged = Signal()
    activeParagraphChanged = Signal()
    activeSpanChanged = Signal()
    syncAvailableChanged = Signal()

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
        # Built model for the `chapters` property (None = stale, rebuild on
        # next read; _emit_chapters invalidates).
        self._chapters_cache: list[dict[str, Any]] | None = None

        self._current_chapter = -1
        self._player_state = "stopped"
        self._position_ms = 0
        self._duration_ms = 0
        # PlaybackWaveform overview of the chapter being played (empty until a
        # chapter with a saved — or computable — envelope starts playing).
        self._chapter_envelope: list[float] = []
        self._rendering_index = -1
        # Book whose chapter _rendering_index refers to. Listener results are
        # validated against it: a shelf switch mid-render swaps self._state,
        # and an unguarded done/error would write chapter audio, statuses and
        # sidecars into the WRONG book's library entry.
        self._render_book_id = ""
        self._render_progress = 0.0
        self._render_all = False
        self._play_after_render = -1  # chapter to auto-play once its render lands
        self._queued: tuple[str, int] | None = None  # ("render"|"play", index)
        self._auto_advance = True
        self._render_voice = ""
        self._error_text = ""

        # Timeline capture (FR-A9): the worker emits a chapter's chunks per
        # segment in order, then progress(done=k) closes segment k−1 — so
        # samples counted between ticks ARE that segment's audio.
        self._render_segments: list[str] = []
        self._segment_samples: list[int] = []
        self._pending_samples = 0
        self._segments_closed = 0
        self._render_text = ""
        self._render_started_at: float | None = None

        # Render telemetry (FR-A10).
        self._render_eta_ms = -1
        self._render_all_total = 0
        self._render_all_done = 0

        # Reader/sync state (FR-A9): _reader_chapter is the index whose text /
        # timeline are currently materialized (-1 = nothing loaded).
        self._reader_open = False
        self._reader_chapter = -1
        self._paragraphs: list[dict[str, Any]] = []
        self._paragraph_starts: list[int] = []
        self._words: list[tuple[int, int]] = []
        self._word_starts: list[int] = []
        self._timeline: Timeline | None = None
        self._timeline_estimated = False
        self._active_paragraph = -1
        self._active_char_start = -1
        self._active_char_end = -1

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
        self._update_active_span()

    def _on_player_duration(self, ms: int) -> None:
        if ms != self._duration_ms:
            self._duration_ms = ms
            self.durationMsChanged.emit()
        # Legacy chapters (cached before timelines existed) get a char-
        # proportional estimate once the player reveals the WAV length.
        if (
            self._timeline is None
            and not self._timeline_estimated
            and self._state is not None
            and 0 <= self._current_chapter < len(self._state.chapters)
            and ms > 0
            and self._library.has_chapter_audio(self._state.record.id, self._current_chapter)
        ):
            text = self._state.chapters[self._current_chapter].text
            self._timeline = estimate_timeline(text, ms)
            self._timeline_estimated = True
            self.syncAvailableChanged.emit()
            self._update_active_span()

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

    @Property("QVariantList", notify=chapterEnvelopeChanged)
    def chapterEnvelope(self) -> list[float]:
        """Peak-normalized 0..1 overview buckets of the playing chapter."""
        return self._chapter_envelope

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

    @Property(int, notify=renderEtaMsChanged)
    def renderEtaMs(self) -> int:
        """Estimated ms left for the in-flight chapter render; -1 unknown."""
        return self._render_eta_ms

    @Property(int, notify=renderAllTotalChanged)
    def renderAllTotal(self) -> int:
        """Chapters a render-all run set out to synthesize; 0 = no run."""
        return self._render_all_total

    @Property(int, notify=renderAllDoneChanged)
    def renderAllDone(self) -> int:
        """Chapters that landed ready so far in the current render-all run."""
        return self._render_all_done

    @Property(bool, notify=readerOpenChanged)
    def readerOpen(self) -> bool:
        """Whether the chapter reader panel is shown alongside the player."""
        return self._reader_open

    @readerOpen.setter
    def readerOpen(self, value: bool) -> None:
        value = bool(value)
        if value != self._reader_open:
            self._reader_open = value
            self.readerOpenChanged.emit()
        if value and self._current_chapter >= 0:
            self._ensure_reader_loaded(self._current_chapter)

    @Property("QVariantList", notify=paragraphsChanged)
    def paragraphs(self) -> list[dict[str, Any]]:
        return self._paragraphs

    @Property(int, notify=activeParagraphChanged)
    def activeParagraph(self) -> int:
        return self._active_paragraph

    @Property(int, notify=activeSpanChanged)
    def activeCharStart(self) -> int:
        """Chapter-text offset where the spoken word starts; -1 when idle."""
        return self._active_char_start

    @Property(int, notify=activeSpanChanged)
    def activeCharEnd(self) -> int:
        return self._active_char_end

    @Property(bool, notify=syncAvailableChanged)
    def syncAvailable(self) -> bool:
        """True while playback can be matched to the text (FR-A9)."""
        return self._timeline is not None and len(self._timeline.segments) > 0

    def _set_render_eta(self, value: int) -> None:
        value = int(value)
        if value != self._render_eta_ms:
            self._render_eta_ms = value
            self.renderEtaMsChanged.emit()

    def _set_render_all(self, *, total: int | None = None, done: int | None = None) -> None:
        if total is not None and int(total) != self._render_all_total:
            self._render_all_total = int(total)
            self.renderAllTotalChanged.emit()
        if done is not None and int(done) != self._render_all_done:
            self._render_all_done = int(done)
            self.renderAllDoneChanged.emit()

    def _set_error(self, message: str) -> None:
        if message != self._error_text:
            self._error_text = message
            self.errorTextChanged.emit()

    def _chapters_model(self) -> list[dict[str, Any]]:
        # Cached build: the QML binding re-reads `chapters` on every
        # chaptersChanged (each render-state transition), and rebuilding the
        # model stats every chapter WAV per read scales with book size on the
        # GUI thread. _emit_chapters() rebuilds; reads are pure cache hits.
        if self._chapters_cache is not None:
            return self._chapters_cache
        if self._state is None:
            self._chapters_cache = []
            return self._chapters_cache
        self._chapters_cache = [
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
        return self._chapters_cache

    def _emit_chapters(self) -> None:
        self._chapters_cache = None
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
            self._set_error(self.tr("Không tìm thấy tệp: {}").format(path))
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
        self._cancel_render_for_book_switch()
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
        self._ensure_reader_loaded(self._current_chapter)  # reader shows the resume chapter
        self._emit_chapters()
        self._set_error("")
        return True

    @Slot(str)
    def selectBook(self, book_id: str) -> None:
        """Shelf selection: open a book, or clear the view for ""."""
        if not book_id:
            self._cancel_render_for_book_switch()
            self._stop_playback()
            self._state = None
            self._statuses = {}
            self._chapter_errors = {}
            self._current_chapter = -1
            self._clear_reader()
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
            self._cancel_render_for_book_switch()
            self._stop_playback()
            self._state = None
            self._statuses = {}
            self._chapter_errors = {}
            self._current_chapter = -1
            self._clear_reader()
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

    @Slot(int)
    def seekToParagraph(self, index: int) -> None:
        """Jump playback to where paragraph ``index`` starts (FR-A9).

        No-op without a timeline or a valid index — the reader stays a view,
        never an error surface.
        """
        if self._timeline is None or not 0 <= index < len(self._paragraphs):
            return
        ms = paragraph_start_ms(self._timeline, int(self._paragraphs[index]["charStart"]))
        if ms >= 0:
            self.seek(ms)

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
        self._load_chapter_envelope(index)
        self._ensure_reader_loaded(index, force=True)
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
        self._reset_active_span()  # the karaoke cursor never outlives playback

    # ── reader / karaoke sync (FR-A9) ────────────────────────────────────────

    def _ensure_reader_loaded(self, index: int, *, force: bool = False) -> None:
        """Materialize paragraph/word/timeline state for ``index`` (idempotent).

        ``force`` re-reads the timeline for an already-loaded chapter — a
        render may have just written it (chapter text is immutable, so
        paragraphs/words never need rebuilding).
        """
        if self._state is None or not 0 <= index < len(self._state.chapters):
            return
        if self._reader_chapter == index and not force:
            return
        if self._reader_chapter == index:
            self._timeline = self._library.load_chapter_timeline(self._state.record.id, index)
            self._timeline_estimated = False
            self._reset_active_span()
            self.syncAvailableChanged.emit()
            return
        text = self._state.chapters[index].text
        self._paragraphs = split_paragraphs(text)
        self._words = word_spans(text)
        # Precomputed bisect keys: _update_active_span runs on every playback
        # tick, and rebuilding these over a 60k-char chapter each tick is
        # thousands of wasted allocations (text is immutable, so once is enough).
        self._paragraph_starts = [p["charStart"] for p in self._paragraphs]
        self._word_starts = [span[0] for span in self._words]
        self._timeline = self._library.load_chapter_timeline(self._state.record.id, index)
        self._timeline_estimated = False  # an estimate may still be built later
        self._reader_chapter = index
        self._reset_active_span()
        self.paragraphsChanged.emit()
        self.syncAvailableChanged.emit()

    def _clear_reader(self) -> None:
        if self._reader_chapter == -1 and not self._paragraphs and self._timeline is None:
            return
        self._paragraphs = []
        self._paragraph_starts = []
        self._words = []
        self._word_starts = []
        self._timeline = None
        self._timeline_estimated = False
        self._reader_chapter = -1
        self._reset_active_span()
        self.paragraphsChanged.emit()
        self.syncAvailableChanged.emit()

    def _reset_active_span(self) -> None:
        if self._active_paragraph != -1:
            self._active_paragraph = -1
            self.activeParagraphChanged.emit()
        if self._active_char_start != -1 or self._active_char_end != -1:
            self._active_char_start = -1
            self._active_char_end = -1
            self.activeSpanChanged.emit()

    def _update_active_span(self) -> None:
        """Map the playback position onto the spoken word + paragraph."""
        if self._timeline is None or self._current_chapter < 0:
            self._reset_active_span()
            return
        span_index = locate_segment(self._timeline, self._position_ms)
        if span_index < 0:
            self._reset_active_span()
            return
        segment = self._timeline.segments[span_index]
        if segment.char_start < 0 or segment.end_ms <= segment.start_ms:
            self._reset_active_span()  # unmapped or silent segment
            return
        fraction = (self._position_ms - segment.start_ms) / (segment.end_ms - segment.start_ms)
        fraction = min(1.0, max(0.0, fraction))
        char_index = round(segment.char_start + fraction * (segment.char_end - segment.char_start))
        word_start, word_end = active_word(self._words, char_index, starts=self._word_starts)
        if word_start < 0:
            self._reset_active_span()
            return
        paragraph_index = self._paragraph_for_char(word_start)
        span_changed = (word_start, word_end) != (self._active_char_start, self._active_char_end)
        if paragraph_index != self._active_paragraph:
            self._active_paragraph = paragraph_index
            self.activeParagraphChanged.emit()
        if span_changed:
            self._active_char_start = word_start
            self._active_char_end = word_end
            self.activeSpanChanged.emit()

    def _paragraph_for_char(self, char_index: int) -> int:
        position = bisect.bisect_right(self._paragraph_starts, char_index) - 1
        return max(0, position)

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
        # The run covers every chapter not yet cached — including one already
        # rendering (a re-count mid-run must not strand the in-flight chapter
        # outside the new totals).
        pending = sum(
            1
            for chapter in self._state.chapters
            if self._statuses.get(chapter.index, STATUS_PENDING)
            in (STATUS_PENDING, STATUS_RENDERING)
            and not self._library.has_chapter_audio(self._state.record.id, chapter.index)
        )
        self._set_render_all(total=pending, done=0)
        self._render_all = True
        self._kick()

    @Slot()
    def cancelRender(self) -> None:
        self._queued = None
        self._render_all = False
        self._set_render_all(total=0, done=0)
        if self._rendering_index != -1 or self._app.busy:
            self._app.cancel()

    def _cancel_render_for_book_switch(self) -> None:
        """Cancel OUR in-flight render before the shelf switches books.

        Narrower than ``cancelRender``: a text-tab synthesis the user is
        running must survive a book switch. Terminal signals racing the
        ``self._state`` swap are dropped by ``_render_target_gone``.
        """
        if self._rendering_index == -1:
            return
        self._queued = None
        self._render_all = False
        self._set_render_all(total=0, done=0)
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
            message = self.tr(OVERSIZE_CHAPTER_MESSAGE).format(
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
        self._render_book_id = self._state.record.id
        self._reset_render_capture()
        self._render_segments = split_text_for_streaming(text)
        self._render_text = text
        self._render_started_at = time.monotonic()
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

    def _render_target_gone(self) -> bool:
        """True when the rendered book is no longer the open book.

        Book switches cancel the render and clear queued intents, but the
        worker's terminal signal can still land after the swap — counting or
        persisting it through the (already replaced) ``self._state`` would
        corrupt the newly opened book. Straggler signals are dropped instead;
        the chapter re-renders on demand when its book is opened again.
        """
        return self._state is None or self._state.record.id != self._render_book_id

    def on_synthesis_progress(self, payload: Any) -> None:
        if self._render_target_gone():
            return
        total = getattr(payload, "total", 0)
        done = getattr(payload, "done", 0)
        fraction = (done / total) if total > 0 else 0.0
        if fraction != self._render_progress:
            self._render_progress = fraction
            self.renderProgressChanged.emit()
        # Timeline capture: a progress tick closes every segment it passed.
        while self._segments_closed < done and self._segments_closed < len(self._render_segments):
            self._segment_samples.append(self._pending_samples)
            self._pending_samples = 0
            self._segments_closed += 1
        # ETA (FR-A10): mean per-segment time projected onto what remains.
        if (
            done >= 1
            and total > done
            and self._render_started_at is not None
            and len(self._segment_samples) > 0
        ):
            elapsed = time.monotonic() - self._render_started_at
            per_segment = elapsed / len(self._segment_samples)
            self._set_render_eta(int(per_segment * (total - done) * 1000))

    def on_synthesis_chunk(self, chunk: Any) -> None:
        """Count streamed samples; the next progress tick claims them (FR-A9)."""
        if self._render_target_gone():
            return
        self._pending_samples += int(np.asarray(chunk).size)

    def on_synthesis_done(self, audio: Any) -> None:
        self._app.detach_synthesis_listener()
        index, self._rendering_index = self._rendering_index, -1
        book_id, self._render_book_id = self._render_book_id, ""
        self._render_progress = 1.0 if index >= 0 else 0.0
        self.renderingIndexChanged.emit()
        self.renderProgressChanged.emit()
        if index < 0 or self._state is None or self._state.record.id != book_id:
            # No render in flight — or the shelf switched/removed the book
            # mid-render: the audio belongs to a book that is no longer open
            # and must never be written into the current one.
            self._reset_render_capture()
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
            self._reset_render_capture()
            self._kick()
            return
        self._statuses[index] = STATUS_READY
        self._chapter_errors.pop(index, None)
        self._emit_chapters()
        self._save_chapter_envelope(index, np.asarray(audio))
        self._save_render_timeline(index, int(np.asarray(audio).size))
        self._reset_render_capture()
        if self._render_all:
            self._set_render_all(done=self._render_all_done + 1)
        if self._play_after_render == index:
            self._play_after_render = -1
            self._play_file(index)
            return
        self._kick()

    def _save_render_timeline(self, index: int, audio_samples: int) -> None:
        """Persist the chapter's audio↔text alignment (FR-A9).

        Measured when the counted samples cover exactly the emitted audio;
        otherwise a char-proportional estimate flagged ``approximate`` (the
        reader still works, just less precisely). Failures degrade silently —
        playback never depended on this file.
        """
        assert self._state is not None
        timeline: Timeline | None = None
        if self._render_segments and audio_samples > 0:
            if sum(self._segment_samples) == audio_samples:
                timeline = build_timeline(
                    self._render_text, self._render_segments, self._segment_samples, SAMPLE_RATE
                )
            else:
                timeline = estimate_timeline(
                    self._render_text,
                    round(audio_samples * 1000 / SAMPLE_RATE),
                    self._render_segments,
                )
        if timeline is None:
            return
        try:
            self._library.save_chapter_timeline(self._state.record.id, index, timeline)
        except AudiobookError:  # noqa: BLE001 - sync degrades, playback must not
            logger.exception("saving chapter timeline failed")

    def _save_chapter_envelope(self, index: int, audio: np.ndarray) -> None:
        """Persist the rendered chapter's waveform overview (PlaybackWaveform).

        Same degrade-silently posture as the timeline: a sidecar failure must
        never fail a render that already cached its audio.
        """
        assert self._state is not None
        try:
            buckets = compute_waveform_envelope(audio)
            if buckets:
                self._library.save_chapter_envelope(self._state.record.id, index, buckets)
        except Exception:  # noqa: BLE001 - overview is cosmetic, never fatal
            logger.exception("saving chapter waveform envelope failed")

    def _load_chapter_envelope(self, index: int) -> None:
        """Expose the chapter's overview: sidecar now, else compute-and-save.

        The sidecar (written at render time) reads in microseconds; chapters
        cached before sidecars existed fall back to decoding the WAV once —
        deferred one event-loop cycle so playback starts first — and persist
        the result so the cost is never paid twice.
        """
        assert self._state is not None
        book_id = self._state.record.id
        saved = self._library.load_chapter_envelope(book_id, index)
        if saved is not None:
            self._set_chapter_envelope(saved)
            return
        self._set_chapter_envelope([])
        QTimer.singleShot(0, lambda: self._compute_envelope_from_wav(book_id, index))

    def _compute_envelope_from_wav(self, book_id: str, index: int) -> None:
        if self._state is None or self._state.record.id != book_id:
            return  # the user switched books while the decode was queued
        if self._current_chapter != index:
            return  # ...or moved on to another chapter
        try:
            samples, _sr = read_wav(self._library.chapter_wav_path(book_id, index))
            buckets = compute_waveform_envelope(samples)
        except Exception:  # noqa: BLE001 - unreadable audio: flat overview
            logger.exception("computing chapter waveform envelope failed")
            return
        self._set_chapter_envelope(buckets)
        try:
            if buckets:
                self._library.save_chapter_envelope(book_id, index, buckets)
        except AudiobookError:  # noqa: BLE001 - persistence is best-effort
            logger.exception("saving computed chapter waveform failed")

    def _set_chapter_envelope(self, buckets: list[float]) -> None:
        if buckets != self._chapter_envelope:
            self._chapter_envelope = buckets
            self.chapterEnvelopeChanged.emit()

    def _reset_render_capture(self) -> None:
        self._render_segments = []
        self._segment_samples = []
        self._pending_samples = 0
        self._segments_closed = 0
        self._render_text = ""
        self._render_started_at = None
        self._set_render_eta(-1)

    def on_synthesis_error(self, message: str) -> None:
        self._app.detach_synthesis_listener()
        index, self._rendering_index = self._rendering_index, -1
        book_id, self._render_book_id = self._render_book_id, ""
        self._render_progress = 0.0
        self.renderingIndexChanged.emit()
        self.renderProgressChanged.emit()
        cancelled = message == CANCELLED_MESSAGE
        self._reset_render_capture()
        if index < 0 or self._state is None or self._state.record.id != book_id:
            # Render of a book that is no longer open (switch/remove raced the
            # terminal signal): statuses of the CURRENT book must stay intact.
            return
        if cancelled:
            # Silent reset (documented policy: cancel is not an error).
            self._statuses[index] = STATUS_PENDING
            self._chapter_errors.pop(index, None)
            self._queued = None
            self._render_all = False
            self._play_after_render = -1
            self._set_render_all(total=0, done=0)
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
        except (AudiobookError, OSError):  # noqa: BLE001 - progress persistence is best-effort
            logger.exception("saving audiobook progress failed")

    # ── export (FR-A6) ───────────────────────────────────────────────────────

    @Slot(int, str, result=str)
    def exportChapter(self, index: int, dest_dir: str) -> str:  # type: ignore[override]
        if self._state is None:
            self._set_error(self.tr("Chưa mở sách nào."))
            return ""
        try:
            return str(self._library.export_chapter(self._state.record.id, index, dest_dir))
        except AudiobookError as exc:
            self._set_error(str(exc))
            return ""

    @Slot(str, result=int)
    def exportAllReady(self, dest_dir: str) -> int:  # type: ignore[override]
        if self._state is None:
            self._set_error(self.tr("Chưa mở sách nào."))
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
