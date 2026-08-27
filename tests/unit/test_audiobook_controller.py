"""AudiobookController (FR-A3/A4/A5/A6/A8): render, listen, resume, export.

Runs against the REAL AppController + real AudiobookLibrary + real
PlaybackController; fakes sit at the same seams as every other controller
suite: FakeEngine/FakeWorker under the AppController and a FakePlayer under
PlaybackController. The committed sample.epub exercises the real parser.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QObject, Signal

from vienetts_app.core.models import TTSProgress, TTSRequest
from vienetts_app.ui.audiobook_controller import AudiobookController
from vienetts_app.ui.controller import AppController
from vienetts_app.ui.playback import PlaybackController
from vienetts_app.workers.inference_worker import CANCELLED_MESSAGE

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SAMPLE_EPUB = FIXTURES / "sample.epub"


def wait_until(cond, timeout: float = 5.0, interval: float = 0.01) -> bool:
    import time

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        QCoreApplication.processEvents()
        time.sleep(interval)
    QCoreApplication.processEvents()
    return False


class FakeEngine:
    """Same contract as test_controller.FakeEngine (records calls, no model)."""

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.backend = "onnx"
        self.sample_rate = 48_000
        self.closed = False

    def infer_stream(self, text, voice=None, temperature=None):
        yield np.zeros(480, dtype=np.float32)

    def add_voice(self, *a, **k):  # pragma: no cover - unused here
        raise AssertionError

    def remove_voice(self, *a, **k):  # pragma: no cover - unused here
        raise AssertionError

    def denoise(self, *a, **k):  # pragma: no cover - unused here
        raise AssertionError

    def list_preset_voices(self):  # pragma: no cover - unused here
        return []

    def save(self, *a, **k):  # pragma: no cover - unused here
        raise AssertionError

    def close(self) -> None:
        self.closed = True


class FakeWorker(QObject):
    progress = Signal(object)
    chunk_ready = Signal(object)
    done = Signal(object)
    error = Signal(str)
    voice_op_done = Signal(object)

    def __init__(self, engine: Any) -> None:
        super().__init__()
        self.engine = engine
        self.submitted: list[Any] = []

    def start(self) -> None:
        pass

    def submit(self, item: Any) -> None:
        self.submitted.append(item)

    def cancel(self) -> None:
        pass

    def stop(self) -> None:
        pass


class SignalStub:
    def __init__(self) -> None:
        self._slots: list = []

    def connect(self, slot) -> None:
        self._slots.append(slot)

    def emit(self, *args) -> None:
        for slot in list(self._slots):
            slot(*args)


class FakePlayer:
    """QMediaPlayer duck-type incl. the optional position/duration signals."""

    def __init__(self) -> None:
        self.playbackStateChanged = SignalStub()
        self.mediaStatusChanged = SignalStub()
        self.errorOccurred = SignalStub()
        self.positionChanged = SignalStub()
        self.durationChanged = SignalStub()
        self.sources: list[str] = []
        self.calls: list[str] = []
        self.positions: list[int] = []
        self._position_ms = 0

    # -- controller-facing methods -------------------------------------------

    def setSource(self, url) -> None:
        self.sources.append(url.toLocalFile())
        self._position_ms = 0

    def play(self) -> None:
        self.calls.append("play")
        self.playbackStateChanged.emit("PlayingState")

    def stop(self) -> None:
        self.calls.append("stop")
        self.playbackStateChanged.emit("StoppedState")

    def pause(self) -> None:
        self.calls.append("pause")
        self.playbackStateChanged.emit("PausedState")

    def resume(self) -> None:
        self.calls.append("resume")
        self.playbackStateChanged.emit("PlayingState")

    def setPosition(self, ms: int) -> None:
        self.positions.append(int(ms))
        self._position_ms = int(ms)
        self.positionChanged.emit(int(ms))

    # -- test helpers ---------------------------------------------------------

    def finish(self) -> None:
        # Real QMediaPlayer stops before reporting EndOfMedia.
        self.playbackStateChanged.emit("StoppedState")
        self.mediaStatusChanged.emit("EndOfMedia")

    def tick(self, ms: int) -> None:
        self._position_ms = ms
        self.positionChanged.emit(ms)

    def announce(self, duration_ms: int) -> None:
        self.durationChanged.emit(duration_ms)


def make_audio(seconds: float = 0.02) -> np.ndarray:
    return np.zeros(int(48_000 * seconds), dtype=np.float32)


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.fake_player = FakePlayer()
        self.app = AppController(
            data_dir=tmp_path,
            engine_factory=lambda **kw: FakeEngine(**kw),
            worker_factory=lambda engine: self._new_worker(),
            catalog=lambda: [],
            saved_names=lambda _dir: [],
        )
        self.workers: list[FakeWorker] = []
        self.audiobook = AudiobookController(
            app_controller=self.app,
            data_dir=tmp_path,
            player_factory=lambda: PlaybackController(player_factory=lambda: self.fake_player),
        )

    def _new_worker(self) -> FakeWorker:
        worker = FakeWorker(FakeEngine())
        self.workers.append(worker)
        return worker

    @property
    def worker(self) -> FakeWorker:
        return self.workers[-1]

    def open_sample(self) -> None:
        ok = self.audiobook.openEpub(str(SAMPLE_EPUB))
        assert ok is True, self.audiobook.errorText

    def render(self, index: int, *, play_after: bool = False) -> None:
        if play_after:
            self.audiobook.playChapter(index)
        else:
            self.audiobook.renderChapter(index)
        assert self.worker.submitted, "render did not submit a synthesis job"
        self.worker.progress.emit(TTSProgress(done=1, total=2, stage="synthesizing"))
        self.worker.done.emit(make_audio())


@pytest.fixture()
def qcoreapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


@pytest.fixture()
def harness(qcoreapp, tmp_path: Path) -> Harness:
    return Harness(tmp_path)


class TestConstruction:
    def test_initial_state_is_empty(self, harness: Harness) -> None:
        ab = harness.audiobook
        assert ab.books == []
        assert ab.currentBookId == ""
        assert ab.chapters == []
        assert ab.currentChapterIndex == -1
        assert ab.playerState == "stopped"
        assert ab.autoAdvance is True
        assert ab.errorText == ""


class TestOpenEpub:
    def test_open_loads_book_and_chapters(self, harness: Harness) -> None:
        ab = harness.audiobook
        harness.open_sample()
        assert ab.currentBookTitle == "Sách thử nghiệm"
        assert ab.currentBookAuthor == "Tác Giả A"
        assert len(ab.chapters) == 3
        assert [c["status"] for c in ab.chapters] == ["pending"] * 3
        assert ab.currentChapterIndex == 0  # progress default
        assert [b["title"] for b in ab.books] == ["Sách thử nghiệm"]

    def test_reopen_same_file_dedupes_shelf(self, harness: Harness) -> None:
        harness.open_sample()
        harness.open_sample()
        assert len(harness.audiobook.books) == 1

    def test_missing_file_sets_error(self, harness: Harness) -> None:
        ab = harness.audiobook
        assert ab.openEpub("/no/such/book.epub") is False
        assert ab.errorText != ""
        assert ab.books == []

    def test_wrong_extension_sets_error(self, harness: Harness, tmp_path: Path) -> None:
        plain = tmp_path / "book.txt"
        plain.write_text("x", encoding="utf-8")
        assert harness.audiobook.openEpub(str(plain)) is False
        assert harness.audiobook.errorText != ""

    def test_error_clears_on_next_success(self, harness: Harness) -> None:
        harness.audiobook.openEpub("/no/such/book.epub")
        harness.open_sample()
        assert harness.audiobook.errorText == ""


class TestRender:
    def test_render_submits_stream_request_with_chapter_text(self, harness: Harness) -> None:
        harness.open_sample()
        harness.audiobook.renderChapter(1)
        request = harness.worker.submitted[-1]
        assert isinstance(request, TTSRequest)
        assert request.mode == "stream"
        assert "Đoạn văn tiếng Việt đầu tiên" in request.text

    def test_done_caches_wav_and_marks_ready(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0)
        ab = harness.audiobook
        assert ab.chapters[0]["status"] == "ready"
        wav = Path(ab.chapterWavPath(0))
        assert wav.is_file()
        assert harness.app.busy is False
        assert ab.renderingIndex == -1

    def test_render_progress_surfaces(self, harness: Harness) -> None:
        harness.open_sample()
        harness.audiobook.renderChapter(0)
        assert harness.audiobook.renderingIndex == 0
        harness.worker.progress.emit(TTSProgress(done=1, total=4, stage="synthesizing"))
        assert harness.audiobook.renderProgress == pytest.approx(0.25)

    def test_error_marks_failed_with_message(self, harness: Harness) -> None:
        harness.open_sample()
        harness.audiobook.renderChapter(0)
        harness.worker.error.emit("engine exploded")
        ab = harness.audiobook
        assert ab.chapters[0]["status"] == "failed"
        assert ab.chapters[0]["error"] == "engine exploded"
        assert ab.errorText == "engine exploded"
        assert ab.renderingIndex == -1

    def test_cancel_resets_chapter_to_pending_silently(self, harness: Harness) -> None:
        harness.open_sample()
        harness.audiobook.renderChapter(0)
        harness.worker.error.emit(CANCELLED_MESSAGE)
        ab = harness.audiobook
        assert ab.chapters[0]["status"] == "pending"
        assert ab.errorText == ""

    def test_cancel_mid_render_leaves_no_wav(self, harness: Harness) -> None:
        harness.open_sample()
        harness.audiobook.renderChapter(0)
        harness.worker.error.emit(CANCELLED_MESSAGE)
        assert not Path(harness.audiobook.chapterWavPath(0)).is_file()

    def test_oversized_chapter_fails_fast_without_submit(self, harness: Harness) -> None:
        harness.open_sample()
        ab = harness.audiobook
        ab._state.chapters[0].__dict__["text"] = "x" * 60_001  # type: ignore[index]
        ab.renderChapter(0)
        assert ab.chapters[0]["status"] == "failed"
        assert "quá dài" in ab.chapters[0]["error"]
        assert harness.workers == []  # the engine was never even built

    def test_busy_engine_defers_render_then_recovers(self, harness: Harness) -> None:
        harness.open_sample()
        # An app-tab job hogs the engine; the render must queue, not drop.
        harness.app.generate("app tab text", "")
        harness.audiobook.renderChapter(0)
        assert harness.audiobook.renderingIndex == -1
        harness.worker.done.emit(make_audio())  # app job finishes
        assert wait_until(lambda: harness.audiobook.renderingIndex == 0)
        harness.worker.done.emit(make_audio())
        assert harness.audiobook.chapters[0]["status"] == "ready"


class TestPlay:
    def test_play_ready_chapter_plays_file(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0)
        ab = harness.audiobook
        ab.playChapter(0)
        assert ab.playerState == "playing"
        assert harness.fake_player.sources[-1] == ab.chapterWavPath(0)
        assert ab.currentChapterIndex == 0

    def test_playing_pre_renders_next_chapter(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0)
        harness.audiobook.playChapter(0)
        assert harness.audiobook.renderingIndex == 1  # pipeline started
        harness.worker.done.emit(make_audio())
        assert harness.audiobook.chapters[1]["status"] == "ready"

    def test_replay_ready_chapter_never_resynthesizes(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0)
        before = len(harness.worker.submitted)
        harness.audiobook.playChapter(0)
        # pipeline renders chapter 1 — but NEVER chapter 0 again
        texts = [r.text for r in harness.worker.submitted[before:]]
        assert texts and all("chapter one" not in t for t in texts)

    def test_play_pending_chapter_renders_then_plays(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0, play_after=True)
        ab = harness.audiobook
        assert ab.playerState == "playing"
        assert harness.fake_player.sources[-1] == ab.chapterWavPath(0)

    def test_pause_and_resume_passthrough(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0)
        ab = harness.audiobook
        ab.playChapter(0)
        ab.pause()
        assert ab.playerState == "paused"
        ab.resume()
        assert ab.playerState == "playing"

    def test_seek_passthrough(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0)
        harness.audiobook.playChapter(0)
        harness.audiobook.seek(15_000)
        assert harness.fake_player.positions[-1] == 15_000

    def test_position_and_duration_surface(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0)
        harness.audiobook.playChapter(0)
        harness.fake_player.announce(120_000)
        harness.fake_player.tick(30_000)
        assert harness.audiobook.durationMs == 120_000
        assert harness.audiobook.positionMs == 30_000

    def test_prev_next_chapter_navigation(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0)
        harness.render(1)
        harness.render(2)
        ab = harness.audiobook
        ab.playChapter(1)
        ab.nextChapter()
        assert ab.currentChapterIndex == 2
        ab.prevChapter()
        assert ab.currentChapterIndex == 1

    def test_next_at_book_end_stops(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(2)
        ab = harness.audiobook
        ab.playChapter(2)
        ab.nextChapter()
        assert ab.currentChapterIndex == 2


class TestAutoAdvance:
    def test_finished_advances_to_ready_next_chapter(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0)
        harness.audiobook.playChapter(0)
        harness.worker.done.emit(make_audio())  # pipeline finished chapter 1
        harness.fake_player.finish()
        ab = harness.audiobook
        assert wait_until(lambda: ab.currentChapterIndex == 1)
        assert ab.playerState == "playing"

    def test_finished_waits_for_inflight_next_render(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0)
        ab = harness.audiobook
        ab.playChapter(0)  # pipeline: chapter 1 rendering
        harness.fake_player.finish()  # chapter 0 ended BEFORE render done
        assert ab.playerState != "playing"
        harness.worker.done.emit(make_audio())
        assert ab.playerState == "playing"
        assert ab.currentChapterIndex == 1

    def test_finished_at_book_end_stops(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(2)
        ab = harness.audiobook
        ab.playChapter(2)
        harness.fake_player.finish()
        assert ab.playerState == "stopped"
        assert ab.currentChapterIndex == 2

    def test_auto_advance_off_keeps_chapter(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0)
        harness.render(1)
        harness.audiobook.autoAdvance = False
        ab = harness.audiobook
        ab.playChapter(0)
        harness.fake_player.finish()
        assert ab.currentChapterIndex == 0


class TestResume:
    def test_progress_persisted_and_restored(self, harness: Harness, tmp_path: Path) -> None:
        harness.open_sample()
        harness.render(1)
        ab = harness.audiobook
        ab.playChapter(1)
        harness.fake_player.announce(100_000)
        harness.fake_player.tick(42_000)
        ab.pause()  # position saved on pause
        book_id = ab.currentBookId
        # Simulate app restart: fresh controllers over the same data dir.
        harness2 = Harness(tmp_path)
        assert [b["id"] for b in harness2.audiobook.books] == [book_id]
        assert harness2.audiobook.openBook(book_id) is True
        assert harness2.audiobook.currentChapterIndex == 1
        # Chapter 1 is still cached from the first session: play resumes at
        # the saved offset without any new synthesis.
        harness2.audiobook.playChapter(1)
        assert harness2.fake_player.sources[-1] == harness2.audiobook.chapterWavPath(1)
        assert harness2.fake_player.positions[-1] == 42_000  # seeked to resume point
        # Any synthesis job submitted can only be the ch2 pipeline — never
        # a re-render of the already-cached chapter 1.
        texts = [r.text for r in harness2.worker.submitted]
        assert all("chương hai" not in t for t in texts)

    def test_open_book_selects_last_book_state(self, harness: Harness) -> None:
        harness.open_sample()
        book_id = harness.audiobook.currentBookId
        harness.audiobook.selectBook("")  # shelf view: deselect
        assert harness.audiobook.currentBookId == ""
        assert harness.audiobook.openBook(book_id) is True
        assert len(harness.audiobook.chapters) == 3


class TestRenderAll:
    def test_render_all_pending_renders_every_chapter(self, harness: Harness) -> None:
        harness.open_sample()
        harness.audiobook.renderAllPending()
        for _ in range(3):
            assert harness.worker.submitted, "renderAll stalled"
            harness.worker.done.emit(make_audio())
        statuses = [c["status"] for c in harness.audiobook.chapters]
        assert statuses == ["ready"] * 3
        assert len(harness.worker.submitted) == 3  # queue drained, nothing re-run

    def test_render_all_marks_failures_and_continues(self, harness: Harness) -> None:
        harness.open_sample()
        harness.audiobook.renderAllPending()
        harness.worker.done.emit(make_audio())  # ch0 ok
        harness.worker.error.emit("boom")  # ch1 fails
        harness.worker.done.emit(make_audio())  # ch2 ok
        statuses = [c["status"] for c in harness.audiobook.chapters]
        assert statuses == ["ready", "failed", "ready"]


class TestExport:
    def test_export_chapter_writes_named_wav(self, harness: Harness, tmp_path: Path) -> None:
        harness.open_sample()
        harness.render(0)
        dest = tmp_path / "export"
        exported = harness.audiobook.exportChapter(0, str(dest))
        assert exported.endswith("01 - Chương một.wav")
        assert (dest / "01 - Chương một.wav").is_file()

    def test_export_all_ready_counts_only_ready(self, harness: Harness, tmp_path: Path) -> None:
        harness.open_sample()
        harness.render(0)
        harness.render(1)
        dest = tmp_path / "export"
        count = harness.audiobook.exportAllReady(str(dest))
        assert count == 2
        assert (dest / "01 - Chương một.wav").is_file()
        assert (dest / "02 - Chương hai.wav").is_file()


class TestLibraryManagement:
    def test_remove_book_clears_current_and_shelf(self, harness: Harness) -> None:
        harness.open_sample()
        book_id = harness.audiobook.currentBookId
        harness.audiobook.removeBook(book_id)
        ab = harness.audiobook
        assert ab.books == []
        assert ab.currentBookId == ""
        assert ab.chapters == []

    def test_removing_current_book_stops_playback(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0)
        harness.audiobook.playChapter(0)
        harness.audiobook.removeBook(harness.audiobook.currentBookId)
        assert harness.audiobook.playerState == "stopped"


class TestCoexistence:
    def test_app_tab_generate_during_play_queues_and_survives(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0)
        harness.audiobook.playChapter(0)
        # Pipeline renders ch1; a Text-tab job queues behind it and its done
        # must NOT be eaten by the audiobook listener.
        harness.app.generate("app text", "")
        harness.worker.done.emit(make_audio())  # pipeline job (listener-owned)
        harness.worker.done.emit(make_audio())  # app job (normal routing)
        assert harness.app.hasAudio is True
        assert harness.audiobook.chapters[1]["status"] == "ready"

    def test_shutdown_stops_player_and_detaches(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0)
        harness.audiobook.playChapter(0)
        harness.audiobook.shutdown()
        assert harness.audiobook.playerState == "stopped"
        harness.worker.done.emit(make_audio())  # late signal must not crash
