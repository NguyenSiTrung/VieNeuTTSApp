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


class TestRenderTimelineCapture:
    """FR-A9: chunk samples between progress ticks become an exact timeline."""

    def _drive_measured_render(self, harness: Harness) -> None:
        # Two fake segments: 2400+2400 samples before tick 1 (segment 0),
        # 4800 before tick 2 (segment 1); done audio = the concatenation.
        worker = harness.worker
        worker.chunk_ready.emit(np.zeros(2400, dtype=np.float32))
        worker.chunk_ready.emit(np.zeros(2400, dtype=np.float32))
        worker.progress.emit(TTSProgress(done=1, total=2, stage="synthesizing"))
        worker.chunk_ready.emit(np.zeros(4800, dtype=np.float32))
        worker.progress.emit(TTSProgress(done=2, total=2, stage="synthesizing"))
        worker.done.emit(np.zeros(9600, dtype=np.float32))

    def test_measured_timeline_saved_when_samples_match(
        self, harness: Harness, monkeypatch
    ) -> None:
        import vienetts_app.ui.audiobook_controller as ab_module
        from vienetts_app.core.timeline import SegmentSpan

        harness.open_sample()
        ab = harness.audiobook
        monkeypatch.setattr(ab_module, "split_text_for_streaming", lambda _text: ["AA.", "BB."])
        ab.renderChapter(0)
        self._drive_measured_render(harness)

        timeline = ab._library.load_chapter_timeline(ab.currentBookId, 0)
        assert timeline is not None
        assert timeline.approximate is False
        # 4800 samples @ 48 kHz = 100 ms per segment; char offsets come from
        # the lock-step token map ("AA." → "First", "BB." → "paragraph").
        assert timeline.segments == (
            SegmentSpan(0, 5, 0, 100),
            SegmentSpan(6, 15, 100, 200),
        )

    def test_sample_mismatch_falls_back_to_estimated_timeline(self, harness: Harness) -> None:
        harness.open_sample()
        ab = harness.audiobook
        ab.renderChapter(0)  # harness.render-style ticks, NO chunk_ready
        harness.worker.progress.emit(TTSProgress(done=1, total=2, stage="synthesizing"))
        harness.worker.done.emit(make_audio())  # 960 samples, none counted

        timeline = ab._library.load_chapter_timeline(ab.currentBookId, 0)
        assert timeline is not None
        assert timeline.approximate is True
        assert timeline.segments  # char-proportional spans still usable
        assert timeline.segments[-1].end_ms == 20  # 960 samples = 20 ms

    def test_render_without_chunks_still_leaves_chapter_ready(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0)
        assert harness.audiobook.chapters[0]["status"] == "ready"


class TestRenderTelemetry:
    """FR-A10: ETA for the in-flight chapter + overall render-all progress."""

    def test_eta_lifecycle(self, harness: Harness) -> None:
        ab = harness.audiobook
        harness.open_sample()
        assert ab.renderEtaMs == -1
        ab.renderChapter(0)
        assert ab.renderEtaMs == -1  # nothing measured before the first tick
        harness.worker.progress.emit(TTSProgress(done=1, total=2, stage="synthesizing"))
        assert ab.renderEtaMs >= 0
        harness.worker.done.emit(make_audio())
        assert ab.renderEtaMs == -1  # reset once the render lands

    def test_eta_completes_to_zero_on_last_segment(self, harness: Harness) -> None:
        ab = harness.audiobook
        harness.open_sample()
        ab.renderChapter(0)
        harness.worker.progress.emit(TTSProgress(done=1, total=2, stage="synthesizing"))
        harness.worker.progress.emit(TTSProgress(done=2, total=2, stage="synthesizing"))
        assert ab.renderEtaMs == 0

    def test_cancel_resets_eta(self, harness: Harness) -> None:
        ab = harness.audiobook
        harness.open_sample()
        ab.renderChapter(0)
        harness.worker.progress.emit(TTSProgress(done=1, total=2, stage="synthesizing"))
        assert ab.renderEtaMs >= 0
        ab.cancelRender()
        harness.worker.error.emit(CANCELLED_MESSAGE)
        assert ab.renderEtaMs == -1

    def test_render_all_totals_track_the_run(self, harness: Harness) -> None:
        ab = harness.audiobook
        harness.open_sample()
        assert ab.renderAllTotal == 0
        assert ab.renderAllDone == 0
        ab.renderAllPending()
        assert ab.renderAllTotal == 3  # three pending chapters in sample.epub
        for _ in range(3):
            harness.worker.progress.emit(TTSProgress(done=1, total=1, stage="synthesizing"))
            harness.worker.done.emit(make_audio())
            harness.app._set_busy(False) if harness.app.busy else None
        assert ab.renderAllDone == 3

    def test_render_all_totals_reset_on_new_run(self, harness: Harness) -> None:
        ab = harness.audiobook
        harness.open_sample()
        ab.renderAllPending()
        harness.worker.progress.emit(TTSProgress(done=1, total=1, stage="synthesizing"))
        harness.worker.done.emit(make_audio())
        assert ab.renderAllDone == 1
        ab.renderAllPending()  # only two chapters still pending
        assert ab.renderAllTotal == 2
        assert ab.renderAllDone == 0


class TestReaderSync:
    """FR-A9: paragraphs + karaoke word/paragraph spans driven by playback."""

    def _render_measured_single_segment(self, harness: Harness) -> None:
        # One real segment (both fixture sentences pack under the 512-char
        # cap): both chunks precede the single progress tick.
        worker = harness.worker
        worker.chunk_ready.emit(np.zeros(4800, dtype=np.float32))
        worker.chunk_ready.emit(np.zeros(4800, dtype=np.float32))
        worker.progress.emit(TTSProgress(done=1, total=1, stage="synthesizing"))
        worker.done.emit(np.zeros(9600, dtype=np.float32))  # 0..200 ms

    def _render_and_play(self, harness: Harness) -> None:
        harness.open_sample()
        ab = harness.audiobook
        ab.autoAdvance = False  # keep the pipeline from pre-rendering ch. 1
        ab.renderChapter(0)
        self._render_measured_single_segment(harness)
        ab.playChapter(0)
        assert harness.fake_player.calls == ["play"]

    def test_play_loads_paragraphs_and_sync(self, harness: Harness) -> None:
        ab = harness.audiobook
        self._render_and_play(harness)
        assert [p["text"] for p in ab.paragraphs] == [
            "First paragraph of chapter one.",
            "Second paragraph of chapter one.",
        ]
        assert (ab.paragraphs[0]["charStart"], ab.paragraphs[1]["charStart"]) == (0, 33)
        assert ab.syncAvailable is True

    def test_position_ticks_drive_word_and_paragraph_highlight(self, harness: Harness) -> None:
        ab = harness.audiobook
        self._render_and_play(harness)
        harness.fake_player.tick(1)  # ~0% of the 0..200 ms chapter
        assert ab.activeParagraph == 0
        assert (ab.activeCharStart, ab.activeCharEnd) == (0, 5)  # "First"
        harness.fake_player.tick(100)  # 50% → char 32 → next word "Second"
        assert ab.activeParagraph == 1
        assert (ab.activeCharStart, ab.activeCharEnd) == (33, 39)
        harness.fake_player.tick(190)  # 95% → char 60 inside the last word
        assert ab.activeParagraph == 1
        assert ab.activeCharEnd == 65  # "one." ends the chapter text

    def test_seek_to_paragraph_seeks_audio(self, harness: Harness) -> None:
        ab = harness.audiobook
        self._render_and_play(harness)
        ab.seekToParagraph(1)
        # One packed segment covers the whole chapter → paragraph 2 starts at
        # the only seekable boundary: the segment start.
        assert harness.fake_player.positions == [0]

    def test_seek_to_paragraph_ignores_bad_input(self, harness: Harness) -> None:
        ab = harness.audiobook
        self._render_and_play(harness)
        ab.seekToParagraph(99)
        ab.seekToParagraph(-1)
        assert harness.fake_player.positions == []

    def test_stop_resets_active_spans(self, harness: Harness) -> None:
        ab = harness.audiobook
        self._render_and_play(harness)
        harness.fake_player.tick(100)
        assert ab.activeParagraph == 1
        ab.stopPlay()
        assert ab.activeParagraph == -1
        assert (ab.activeCharStart, ab.activeCharEnd) == (-1, -1)

    def test_reader_open_loads_chapter_text_without_audio(self, harness: Harness) -> None:
        ab = harness.audiobook
        harness.open_sample()
        # openBook preloads the resume chapter's text so opening the reader is
        # instant; the panel itself stays closed until the user asks for it.
        assert ab.readerOpen is False
        assert len(ab.paragraphs) == 2
        ab.readerOpen = True
        assert ab.readerOpen is True
        assert [p["text"] for p in ab.paragraphs] == [
            "First paragraph of chapter one.",
            "Second paragraph of chapter one.",
        ]
        assert ab.syncAvailable is False  # no timeline, no audio yet

    def test_legacy_cache_estimates_timeline_when_duration_arrives(self, harness: Harness) -> None:
        ab = harness.audiobook
        harness.open_sample()
        # A chapter cached BEFORE timelines existed: WAV present, no JSON.
        ab._library.save_chapter_audio(ab.currentBookId, 0, make_audio())
        ab.playChapter(0)
        assert ab.syncAvailable is False
        harness.fake_player.tick(10)  # no crash, no highlight yet
        assert ab.activeParagraph == -1
        harness.fake_player.announce(20)  # player finally reports duration
        assert ab.syncAvailable is True
        harness.fake_player.tick(10)  # midpoint of the estimate → char 32,
        # inside the "\n\n" gap → the NEXT word ("Second"), paragraph 2
        assert ab.activeParagraph == 1
        assert (ab.activeCharStart, ab.activeCharEnd) == (33, 39)

    def test_select_book_clears_reader_state(self, harness: Harness) -> None:
        ab = harness.audiobook
        self._render_and_play(harness)
        harness.fake_player.tick(100)
        assert ab.paragraphs
        ab.selectBook("")
        assert ab.paragraphs == []
        assert ab.syncAvailable is False
        assert ab.activeParagraph == -1

    def test_switching_chapters_swaps_paragraphs(self, harness: Harness) -> None:
        ab = harness.audiobook
        self._render_and_play(harness)
        ab._library.save_chapter_audio(ab.currentBookId, 1, make_audio())
        ab.playChapter(1)
        assert [p["text"] for p in ab.paragraphs] == [
            "Đoạn văn tiếng Việt đầu tiên của chương hai.",
            "Đoạn văn thứ hai — có dấu gạch ngang và dấu câu!",
        ]
