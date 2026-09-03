"""AudiobookController (FR-A3/A4/A5/A6/A8): render, listen, resume, export.

Runs against the REAL AppController + real AudiobookLibrary + real
PlaybackController; fakes sit at the same seams as every other controller
suite: FakeEngine/FakeWorker under the AppController and a FakePlayer under
PlaybackController. The committed sample.epub exercises the real parser.
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PySide6.QtCore import QCoreApplication, QObject, Signal

from vienetts_app.core.artifacts import SynthesisArtifact
from vienetts_app.core.models import TTSRequest
from vienetts_app.ui.audiobook_controller import AudiobookController
from vienetts_app.ui.bg_ops import run_sync
from vienetts_app.ui.chapter_persist import SyncPersistExecutor
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
    """Tagged signal surface of InferenceWorker; records submissions."""

    progress = Signal(object)  # JobProgress
    chunk_ready = Signal(object)  # JobChunk
    terminal = Signal(object)  # JobTerminal

    def __init__(self, engine: Any) -> None:
        super().__init__()
        self.engine = engine
        self.submitted: list[Any] = []
        self.cancel_job_ids: list[str] = []
        self.cancelled_owners: list[str] = []
        self.stopped = False

    def start(self) -> None:
        pass

    def submit(self, item: Any) -> bool:
        self.submitted.append(item)
        return True

    def cancel_job(self, job_id: str) -> bool:
        self.cancel_job_ids.append(job_id)
        return True

    def cancel_owner(self, owner: str) -> int:
        self.cancelled_owners.append(owner)
        return 0

    def cancel(self) -> None:
        pass

    def stop(self) -> None:
        self.stopped = True

    # -- tagged-emit conveniences (the worker tags by submitted job) --------
    def progress_last(self, done: int, total: int, stage: str = "synthesizing") -> None:
        from vienetts_app.core.jobs import JobProgress

        job = self.submitted[-1]
        self.progress.emit(JobProgress(job.id, done=done, total=total, stage=stage))

    def chunk_last(self, samples: Any) -> None:
        from vienetts_app.core.jobs import JobChunk

        job = self.submitted[-1]
        values = np.asarray(samples, dtype=np.float32)
        self.chunk_ready.emit(
            JobChunk(job.id, sample_count=int(values.size), peak=float(np.max(np.abs(values))))
        )

    def complete_last(self, value: Any, owner: str = "audiobook") -> None:
        from vienetts_app.core.jobs import JobTerminal

        job = self.submitted[-1]
        if isinstance(value, np.ndarray):
            assert job.artifact_path is not None
            value = make_artifact(job.artifact_path, job_id=job.id, audio=value)
        self.terminal.emit(
            JobTerminal(job_id=job.id, owner=owner, state="completed", value=value)  # type: ignore[arg-type]
        )

    def fail_last(self, message: str, owner: str = "audiobook") -> None:
        from vienetts_app.core.jobs import JobTerminal

        job = self.submitted[-1]
        if message == CANCELLED_MESSAGE:
            self.terminal.emit(
                JobTerminal(job_id=job.id, owner=owner, state="cancelled")  # type: ignore[arg-type]
            )
        else:
            self.terminal.emit(
                JobTerminal(job_id=job.id, owner=owner, state="failed", error=message)  # type: ignore[arg-type]
            )


class SignalStub:
    def __init__(self) -> None:
        self._slots: list = []

    def connect(self, slot) -> None:
        self._slots.append(slot)

    def disconnect(self, slot) -> None:
        if slot in self._slots:
            self._slots.remove(slot)

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


def make_artifact(
    path: Path,
    *,
    job_id: str,
    samples: int | None = None,
    audio: np.ndarray | None = None,
    sample_rate: int = 48_000,
) -> SynthesisArtifact:
    """Write a small deterministic WAV fixture and describe it as a terminal artifact."""
    import soundfile as sf

    payload = (
        np.zeros(int(samples), dtype=np.float32)
        if audio is None
        else np.ascontiguousarray(audio, dtype=np.float32)
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(path, payload, sample_rate, subtype="FLOAT", format="WAV")
    return SynthesisArtifact(
        job_id=job_id,
        path=path,
        sample_rate=sample_rate,
        samples=int(payload.size),
        duration_ms=int(payload.size * 1000 / sample_rate),
    )


class Harness:
    def __init__(self, tmp_path: Path, *, persist_executor: Any = None) -> None:
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
            # Deterministic rendering assertions: chapter persistence runs
            # inline instead of on the background pool.
            persist_executor=SyncPersistExecutor()
            if persist_executor is None
            else persist_executor,
            bg_runner=run_sync,
        )

    def _new_worker(self) -> FakeWorker:
        worker = FakeWorker(FakeEngine())
        self.workers.append(worker)
        return worker

    @property
    def worker(self) -> FakeWorker:
        return self.workers[-1]

    @property
    def audiobook_lib(self):
        return self.audiobook._library

    def open_sample(self) -> None:
        ok = self.audiobook.openEpub(str(SAMPLE_EPUB))
        assert ok is True, self.audiobook.errorText

    def render(self, index: int, *, play_after: bool = False) -> None:
        if play_after:
            self.audiobook.playChapter(index)
        else:
            self.audiobook.renderChapter(index)
        assert self.worker.submitted, "render did not submit a synthesis job"
        self.worker.progress_last(1, 2, "synthesizing")
        self.worker.complete_last(make_audio())


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

    def test_render_progress_readable_before_first_render(self, harness: Harness) -> None:
        # Regression: QML binds renderProgress as soon as a book opens, but
        # _render_progress used to be created only in _start_render — every
        # pre-render read raised AttributeError into the QML console.
        assert harness.audiobook.renderProgress == 0.0

    def test_progress_persist_failure_never_raises(self, harness: Harness, monkeypatch) -> None:
        # Regression: _save_progress caught only AudiobookError; an OSError
        # from the disk layer raised straight through the position-tick slot
        # (fires every ~2 s during playback).
        harness.open_sample()

        def boom(*_args, **_kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(harness.audiobook_lib, "set_progress", boom)
        harness.audiobook._save_progress(force=True)  # must not raise
        assert harness.audiobook.errorText == ""  # best-effort: no error banner


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
        job = harness.worker.submitted[-1]
        request = job.request
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

    def test_matching_artifact_promotes_to_chapter_cache_then_releases_source(
        self, harness: Harness
    ) -> None:
        harness.open_sample()
        harness.audiobook.renderChapter(0)
        job = harness.worker.submitted[-1]
        assert job.artifact_path is not None
        artifact = make_artifact(job.artifact_path, job_id=job.id, samples=48_000)

        harness.worker.complete_last(artifact)

        target = harness.audiobook_lib.chapter_wav_path(harness.audiobook.currentBookId, 0)
        assert target.is_file()
        assert not target.with_name("ch_0000.part.wav").exists()
        assert not artifact.path.exists()

    def test_pending_promotion_blocks_duplicate_render_submission(
        self, tmp_path: Path, qcoreapp
    ) -> None:
        class DelayedPersistExecutor(SyncPersistExecutor):
            def submit_artifact(self, *args: Any) -> None:
                self.pending = args

        persist = DelayedPersistExecutor()
        harness = Harness(tmp_path, persist_executor=persist)
        harness.open_sample()
        harness.audiobook.renderChapter(0)
        job = harness.worker.submitted[-1]
        assert job.artifact_path is not None
        harness.worker.complete_last(make_artifact(job.artifact_path, job_id=job.id, samples=480))
        assert persist.pending

        harness.audiobook.renderChapter(0)

        assert len(harness.worker.submitted) == 1

    def test_invalid_completed_artifact_fails_without_chapter_output(
        self, harness: Harness
    ) -> None:
        harness.open_sample()
        harness.audiobook.renderChapter(0)
        job = harness.worker.submitted[-1]
        assert job.artifact_path is not None
        job.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        job.artifact_path.write_bytes(b"not a WAV")
        artifact = SynthesisArtifact(
            job_id=job.id,
            path=job.artifact_path,
            sample_rate=48_000,
            samples=480,
            duration_ms=10,
        )

        harness.worker.complete_last(artifact)

        target = harness.audiobook_lib.chapter_wav_path(harness.audiobook.currentBookId, 0)
        assert harness.audiobook.chapters[0]["status"] == "failed"
        assert not target.exists()
        assert not target.with_name("ch_0000.part.wav").exists()
        assert not artifact.path.exists()

    def test_ready_state_failure_removes_final_and_allows_render_retry(
        self, harness: Harness, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        harness.open_sample()
        harness.audiobook.renderChapter(0)
        first_job = harness.worker.submitted[-1]
        assert first_job.artifact_path is not None
        source = make_artifact(first_job.artifact_path, job_id=first_job.id, samples=480)
        original_mark_ready = harness.audiobook_lib.mark_chapter_ready
        calls = 0

        def fail_once(book_id: str, index: int) -> None:
            nonlocal calls
            calls += 1
            if calls == 1:
                from vienetts_app.core.audiobook import AudiobookError

                raise AudiobookError("state write failed")
            original_mark_ready(book_id, index)

        monkeypatch.setattr(harness.audiobook_lib, "mark_chapter_ready", fail_once)
        harness.worker.complete_last(source)

        target = harness.audiobook_lib.chapter_wav_path(harness.audiobook.currentBookId, 0)
        assert harness.audiobook.chapters[0]["status"] == "failed"
        assert source.path.exists()
        assert not target.exists()
        assert not target.with_name("ch_0000.part.wav").exists()

        harness.audiobook.renderChapter(0)
        retry_job = harness.worker.submitted[-1]
        assert retry_job.id != first_job.id
        assert retry_job.artifact_path is not None
        harness.worker.complete_last(
            make_artifact(retry_job.artifact_path, job_id=retry_job.id, samples=480)
        )

        assert harness.audiobook.chapters[0]["status"] == "ready"
        assert target.is_file()

    def test_completed_artifact_with_wrong_job_id_fails_without_chapter_output(
        self, harness: Harness
    ) -> None:
        harness.open_sample()
        harness.audiobook.renderChapter(0)
        job = harness.worker.submitted[-1]
        assert job.artifact_path is not None
        artifact = make_artifact(job.artifact_path, job_id="b" * 32, samples=480)

        harness.worker.complete_last(artifact)

        target = harness.audiobook_lib.chapter_wav_path(harness.audiobook.currentBookId, 0)
        assert harness.audiobook.chapters[0]["status"] == "failed"
        assert not target.exists()
        assert not target.with_name("ch_0000.part.wav").exists()

    def test_copy_failure_preserves_artifact_and_removes_chapter_part(
        self, harness: Harness, monkeypatch
    ) -> None:
        import vienetts_app.ui.chapter_persist as persist_module

        harness.open_sample()
        harness.audiobook.renderChapter(0)
        job = harness.worker.submitted[-1]
        assert job.artifact_path is not None
        artifact = make_artifact(job.artifact_path, job_id=job.id, samples=480)
        monkeypatch.setattr(
            persist_module.shutil,
            "copyfile",
            lambda *_args, **_kwargs: (_ for _ in ()).throw(OSError("disk full")),
        )

        harness.worker.complete_last(artifact)

        target = harness.audiobook_lib.chapter_wav_path(harness.audiobook.currentBookId, 0)
        assert harness.audiobook.chapters[0]["status"] == "failed"
        assert artifact.path.is_file()
        assert not target.exists()
        assert not target.with_name("ch_0000.part.wav").exists()

    def test_cancelled_terminal_leaves_no_chapter_final_or_part(self, harness: Harness) -> None:
        harness.open_sample()
        harness.audiobook.renderChapter(0)

        harness.worker.fail_last(CANCELLED_MESSAGE)

        target = harness.audiobook_lib.chapter_wav_path(harness.audiobook.currentBookId, 0)
        assert not target.exists()
        assert not target.with_name("ch_0000.part.wav").exists()

    def test_render_progress_surfaces(self, harness: Harness) -> None:
        harness.open_sample()
        harness.audiobook.renderChapter(0)
        assert harness.audiobook.renderingIndex == 0
        harness.worker.progress_last(1, 4, "synthesizing")
        assert harness.audiobook.renderProgress == pytest.approx(0.25)

    def test_chapters_reads_hit_the_model_cache(self, harness: Harness) -> None:
        # QML re-reads `chapters` on every chaptersChanged; each read used to
        # rebuild the model and stat every chapter WAV on the GUI thread.
        harness.open_sample()
        ab = harness.audiobook
        lib = harness.audiobook_lib
        real = lib.has_chapter_audio
        stats: list[int] = []

        def counting(book_id: str, index: int) -> bool:
            stats.append(index)
            return real(book_id, index)

        lib.has_chapter_audio = counting  # type: ignore[method-assign]
        first = ab.chapters
        again = ab.chapters
        assert first is again
        assert len(stats) == 3  # one stat per chapter, once — not per read
        ab.renderChapter(0)
        assert ab.chapters is not first  # invalidated by the state change

    def test_error_marks_failed_with_message(self, harness: Harness) -> None:
        harness.open_sample()
        harness.audiobook.renderChapter(0)
        harness.worker.fail_last("engine exploded")
        ab = harness.audiobook
        assert ab.chapters[0]["status"] == "failed"
        assert ab.chapters[0]["error"] == "engine exploded"
        assert ab.errorText == "engine exploded"
        assert ab.renderingIndex == -1

    @staticmethod
    def _distinct_epub_copy(tmp_path: Path) -> Path:
        # Same content + one extra member → different sha256 → different id.
        import zipfile

        target = tmp_path / "second.epub"
        with (
            zipfile.ZipFile(SAMPLE_EPUB) as zin,
            zipfile.ZipFile(target, "w") as zout,
        ):
            for item in zin.infolist():
                zout.writestr(item, zin.read(item.filename))
            zout.writestr("distinct.txt", "different book id")
        return target

    def test_book_switch_mid_render_drops_done_result(self, harness: Harness, tmp_path) -> None:
        # Regression: on_synthesis_done wrote through the (already swapped)
        # self._state — chapter audio of book A landed in book B and B's
        # state.json got a ready/failed status for a chapter it never rendered.
        harness.open_sample()
        book_a = harness.audiobook.currentBookId
        harness.audiobook.renderChapter(0)
        assert harness.audiobook.renderingIndex == 0

        assert harness.audiobook.openEpub(str(self._distinct_epub_copy(tmp_path))) is True
        book_b = harness.audiobook.currentBookId
        assert book_b != book_a

        # Straggler terminal signal of book A's (cancelled) render.
        harness.worker.progress_last(1, 2, "synthesizing")
        harness.worker.complete_last(make_audio())

        ab = harness.audiobook
        assert not harness.audiobook_lib.has_chapter_audio(book_b, 0)
        assert [c["status"] for c in ab.chapters] == ["pending"] * 3
        assert ab.errorText == ""
        # Book A's persisted state never saw the aborted render either.
        reloaded = harness.audiobook_lib.load_book(book_a)
        assert reloaded.statuses.get(0, "pending") != "ready"

    def test_book_switch_drops_and_releases_old_managed_artifact(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        harness.open_sample()
        harness.audiobook.renderChapter(0)
        job = harness.worker.submitted[-1]
        assert job.artifact_path is not None
        artifact = make_artifact(job.artifact_path, job_id=job.id, samples=480)

        assert harness.audiobook.openEpub(str(self._distinct_epub_copy(tmp_path))) is True
        new_book = harness.audiobook.currentBookId
        harness.worker.complete_last(artifact)

        assert not artifact.path.exists()
        assert not harness.audiobook_lib.has_chapter_audio(new_book, 0)

    def test_stale_managed_artifact_is_released_without_writing_cache(
        self, harness: Harness
    ) -> None:
        from vienetts_app.core.jobs import JobTerminal

        harness.open_sample()
        job_id = "a" * 32
        source = harness.app._artifact_store.allocate(job_id)
        artifact = make_artifact(source, job_id=job_id, samples=480)

        harness.audiobook.on_synthesis_terminal(
            JobTerminal(job_id=job_id, owner="audiobook", state="completed", value=artifact)
        )

        assert not artifact.path.exists()
        assert not harness.audiobook_lib.has_chapter_audio(harness.audiobook.currentBookId, 0)

    def test_book_switch_mid_render_drops_error_result(self, harness: Harness, tmp_path) -> None:
        harness.open_sample()
        harness.audiobook.renderChapter(0)
        assert harness.audiobook.openEpub(str(self._distinct_epub_copy(tmp_path))) is True

        harness.worker.fail_last("engine exploded")

        ab = harness.audiobook
        assert [c["status"] for c in ab.chapters] == ["pending"] * 3
        assert ab.chapters[0]["error"] == ""
        assert ab.errorText == ""

    def test_remove_book_mid_render_never_resurrects_it(self, harness: Harness) -> None:
        # The terminal done would mkdir+write through save_chapter_audio,
        # re-creating the removed book's directory with a chapter WAV.
        harness.open_sample()
        book_a = harness.audiobook.currentBookId
        harness.audiobook.renderChapter(0)
        book_dir = harness.audiobook_lib.root / book_a

        harness.audiobook.removeBook(book_a)
        assert harness.audiobook.currentBookId == ""

        harness.worker.complete_last(make_audio())

        assert harness.audiobook.currentBookId == ""
        assert not book_dir.exists()

    def test_cancel_resets_chapter_to_pending_silently(self, harness: Harness) -> None:
        harness.open_sample()
        harness.audiobook.renderChapter(0)
        harness.worker.fail_last(CANCELLED_MESSAGE)
        ab = harness.audiobook
        assert ab.chapters[0]["status"] == "pending"
        assert ab.errorText == ""

    def test_cancel_mid_render_leaves_no_wav(self, harness: Harness) -> None:
        harness.open_sample()
        harness.audiobook.renderChapter(0)
        harness.worker.fail_last(CANCELLED_MESSAGE)
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
        harness.worker.complete_last(make_audio())  # app job finishes
        assert wait_until(lambda: harness.audiobook.renderingIndex == 0)
        harness.worker.complete_last(make_audio())
        assert harness.audiobook.chapters[0]["status"] == "ready"


class TestPlay:
    def test_play_ready_chapter_plays_file(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0)
        ab = harness.audiobook
        ab.playChapter(0)
        assert ab.playerState == "playing"
        # Path-wrapped: the fake player records QUrl-normalized (forward-slash)
        # paths while chapterWavPath is a native str — equal on every OS only
        # through pathlib.
        assert Path(harness.fake_player.sources[-1]) == Path(ab.chapterWavPath(0))
        assert ab.currentChapterIndex == 0

    def test_playing_pre_renders_next_chapter(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0)
        harness.audiobook.playChapter(0)
        assert harness.audiobook.renderingIndex == 1  # pipeline started
        harness.worker.complete_last(make_audio())
        assert harness.audiobook.chapters[1]["status"] == "ready"

    def test_replay_ready_chapter_never_resynthesizes(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0)
        before = len(harness.worker.submitted)
        harness.audiobook.playChapter(0)
        # pipeline renders chapter 1 — but NEVER chapter 0 again
        texts = [r.request.text for r in harness.worker.submitted[before:]]
        assert texts and all("chapter one" not in t for t in texts)

    def test_play_pending_chapter_renders_then_plays(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0, play_after=True)
        ab = harness.audiobook
        assert ab.playerState == "playing"
        assert Path(harness.fake_player.sources[-1]) == Path(ab.chapterWavPath(0))

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
        harness.worker.complete_last(make_audio())  # pipeline finished chapter 1
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
        harness.worker.complete_last(make_audio())
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
        assert Path(harness2.fake_player.sources[-1]) == Path(harness2.audiobook.chapterWavPath(1))
        assert harness2.fake_player.positions[-1] == 42_000  # seeked to resume point
        # Any synthesis job submitted can only be the ch2 pipeline — never
        # a re-render of the already-cached chapter 1.
        texts = [r.request.text for r in harness2.worker.submitted]
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
            harness.worker.complete_last(make_audio())
        statuses = [c["status"] for c in harness.audiobook.chapters]
        assert statuses == ["ready"] * 3
        assert len(harness.worker.submitted) == 3  # queue drained, nothing re-run

    def test_render_all_marks_failures_and_continues(self, harness: Harness) -> None:
        harness.open_sample()
        harness.audiobook.renderAllPending()
        harness.worker.complete_last(make_audio())  # ch0 ok
        harness.worker.fail_last("boom")  # ch1 fails
        harness.worker.complete_last(make_audio())  # ch2 ok
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
        pipeline_job = harness.worker.submitted[-2]
        app_job = harness.worker.submitted[-1]
        assert pipeline_job.artifact_path is not None
        assert app_job.artifact_path is not None
        harness.worker.terminal.emit(
            _completed(
                pipeline_job.id,
                make_artifact(pipeline_job.artifact_path, job_id=pipeline_job.id, samples=480),
            )
        )
        harness.worker.terminal.emit(
            _completed(
                app_job.id,
                make_artifact(app_job.artifact_path, job_id=app_job.id, samples=480),
                owner="text",
            )
        )
        assert harness.app.hasAudio is True
        assert harness.audiobook.chapters[1]["status"] == "ready"

    def test_shutdown_stops_player_and_detaches(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0)
        harness.audiobook.playChapter(0)
        harness.audiobook.shutdown()
        assert harness.audiobook.playerState == "stopped"
        harness.worker.complete_last(make_audio())  # late signal must not crash


class TestRenderTimelineCapture:
    """FR-A9: chunk samples between progress ticks become an exact timeline."""

    def _drive_measured_render(self, harness: Harness) -> None:
        # Two fake segments: 2400+2400 samples before tick 1 (segment 0),
        # 4800 before tick 2 (segment 1); done audio = the concatenation.
        worker = harness.worker
        worker.chunk_last(np.zeros(2400, dtype=np.float32))
        worker.chunk_last(np.zeros(2400, dtype=np.float32))
        worker.progress_last(1, 2, "synthesizing")
        worker.chunk_last(np.zeros(4800, dtype=np.float32))
        worker.progress_last(2, 2, "synthesizing")
        worker.complete_last(np.zeros(9600, dtype=np.float32))

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
        harness.worker.progress_last(1, 2, "synthesizing")
        harness.worker.complete_last(make_audio())  # 960 samples, none counted

        timeline = ab._library.load_chapter_timeline(ab.currentBookId, 0)
        assert timeline is not None
        assert timeline.approximate is True
        assert timeline.segments  # char-proportional spans still usable
        assert timeline.segments[-1].end_ms == 20  # 960 samples = 20 ms

    def test_render_without_chunks_still_leaves_chapter_ready(self, harness: Harness) -> None:
        harness.open_sample()
        harness.render(0)
        assert harness.audiobook.chapters[0]["status"] == "ready"

    def test_metadata_only_chunks_build_exact_timeline_without_pcm_decode(
        self, harness: Harness, monkeypatch
    ) -> None:
        import types

        import vienetts_app.ui.audiobook_controller as ab_module
        import vienetts_app.ui.chapter_persist as persist_module
        from vienetts_app.core.jobs import JobChunk

        harness.open_sample()
        ab = harness.audiobook
        monkeypatch.setattr(ab_module, "split_text_for_streaming", lambda _text: ["AA.", "BB."])
        ab.renderChapter(0)
        job = harness.worker.submitted[-1]
        assert job.artifact_path is not None
        artifact = make_artifact(job.artifact_path, job_id=job.id, samples=400)
        assert not hasattr(ab_module, "np")
        monkeypatch.setattr(
            persist_module,
            "read_wav",
            lambda *_args, **_kwargs: pytest.fail("full WAV decode forbidden"),
            raising=False,
        )

        for _ in range(200):
            ab.on_synthesis_chunk(JobChunk(job.id, sample_count=1, peak=0.0))
        ab.on_synthesis_progress(types.SimpleNamespace(job_id=job.id, done=1, total=2))
        for _ in range(200):
            ab.on_synthesis_chunk(JobChunk(job.id, sample_count=1, peak=0.0))
        ab.on_synthesis_progress(types.SimpleNamespace(job_id=job.id, done=2, total=2))
        harness.worker.complete_last(artifact)

        timeline = harness.audiobook_lib.load_chapter_timeline(ab.currentBookId, 0)
        assert timeline is not None and timeline.approximate is False
        assert timeline.segments[-1].end_ms == 8


class TestRenderTelemetry:
    """FR-A10: ETA for the in-flight chapter + overall render-all progress."""

    def test_eta_lifecycle(self, harness: Harness) -> None:
        ab = harness.audiobook
        harness.open_sample()
        assert ab.renderEtaMs == -1
        ab.renderChapter(0)
        assert ab.renderEtaMs == -1  # nothing measured before the first tick
        harness.worker.progress_last(1, 2, "synthesizing")
        assert ab.renderEtaMs >= 0
        harness.worker.complete_last(make_audio())
        assert ab.renderEtaMs == -1  # reset once the render lands

    def test_eta_completes_to_zero_on_last_segment(self, harness: Harness) -> None:
        ab = harness.audiobook
        harness.open_sample()
        ab.renderChapter(0)
        harness.worker.progress_last(1, 2, "synthesizing")
        harness.worker.progress_last(2, 2, "synthesizing")
        assert ab.renderEtaMs == 0

    def test_cancel_resets_eta(self, harness: Harness) -> None:
        ab = harness.audiobook
        harness.open_sample()
        ab.renderChapter(0)
        harness.worker.progress_last(1, 2, "synthesizing")
        assert ab.renderEtaMs >= 0
        ab.cancelRender()
        harness.worker.fail_last(CANCELLED_MESSAGE)
        assert ab.renderEtaMs == -1

    def test_render_all_totals_track_the_run(self, harness: Harness) -> None:
        ab = harness.audiobook
        harness.open_sample()
        assert ab.renderAllTotal == 0
        assert ab.renderAllDone == 0
        ab.renderAllPending()
        assert ab.renderAllTotal == 3  # three pending chapters in sample.epub
        for _ in range(3):
            harness.worker.progress_last(1, 1, "synthesizing")
            harness.worker.complete_last(make_audio())
            harness.app._set_busy(False) if harness.app.busy else None
        assert ab.renderAllDone == 3

    def test_render_all_totals_reset_on_new_run(self, harness: Harness) -> None:
        ab = harness.audiobook
        harness.open_sample()
        ab.renderAllPending()
        harness.worker.progress_last(1, 1, "synthesizing")
        harness.worker.complete_last(make_audio())
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
        worker.chunk_last(np.zeros(4800, dtype=np.float32))
        worker.chunk_last(np.zeros(4800, dtype=np.float32))
        worker.progress_last(1, 1, "synthesizing")
        worker.complete_last(np.zeros(9600, dtype=np.float32))  # 0..200 ms

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


class TestCopyChapter:
    """One-tap transcript export: the reader's own rows, joined, to the clipboard."""

    def test_copy_without_a_loaded_chapter_is_rejected(self, harness: Harness) -> None:
        assert harness.audiobook.copyChapter() is False

    def test_copy_puts_the_whole_chapter_on_the_clipboard(
        self, harness: Harness, monkeypatch
    ) -> None:
        import vienetts_app.ui.audiobook_controller as ab_mod

        # QGuiApplication.clipboard() needs a GUI app instance; the unit
        # suite runs under QCoreApplication. Swap the module-level symbol
        # so the slot's real call path (clipboard().setText) is exercised.
        class FakeClipboard:
            def __init__(self) -> None:
                self.texts: list[str] = []

            def setText(self, text: str) -> None:
                self.texts.append(text)

        class FakeGuiApplication:
            _clipboard = FakeClipboard()

            @staticmethod
            def clipboard() -> FakeClipboard:
                return FakeGuiApplication._clipboard

        monkeypatch.setattr(ab_mod, "QGuiApplication", FakeGuiApplication)
        harness.open_sample()  # openBook preloads the chapter's paragraphs

        assert harness.audiobook.copyChapter() is True
        assert FakeGuiApplication._clipboard.texts == [
            "First paragraph of chapter one.\n\nSecond paragraph of chapter one."
        ]


class TestChapterEnvelope:
    """PlaybackWaveform feed: overview sidecar + chapterEnvelope property."""

    @staticmethod
    def speechlike_audio() -> np.ndarray:
        # Loud half then silent half: envelope descends 1.0 → 0.0.
        return np.concatenate(
            [
                np.full(2_400, 0.5, dtype=np.float32),
                np.zeros(2_400, dtype=np.float32),
            ]
        )

    def test_initial_envelope_empty(self, harness) -> None:
        assert harness.audiobook.chapterEnvelope == []

    def test_render_persists_normalized_envelope_sidecar(self, harness) -> None:
        harness.open_sample()
        harness.audiobook.renderChapter(0)
        assert harness.worker.submitted
        harness.worker.complete_last(self.speechlike_audio())
        book_id = harness.audiobook.currentBookId
        buckets = harness.audiobook_lib.load_chapter_envelope(book_id, 0)
        assert buckets is not None and len(buckets) > 0
        assert max(buckets) == pytest.approx(1.0)
        assert buckets[0] == pytest.approx(1.0)
        assert buckets[-1] == pytest.approx(0.0)

    def test_play_chapter_exposes_saved_envelope(self, harness) -> None:
        harness.open_sample()
        harness.audiobook.playChapter(0)
        assert harness.worker.submitted
        harness.worker.complete_last(self.speechlike_audio())  # render lands → plays
        buckets = harness.audiobook.chapterEnvelope
        assert len(buckets) > 0
        assert buckets[0] == pytest.approx(1.0)

    def test_legacy_cache_computes_envelope_from_wav_once(self, harness) -> None:
        # A chapter cached BEFORE sidecars existed: playing it computes the
        # envelope from the WAV (never blocking play start in production —
        # the pool thread does the decode) and persists the sidecar so the
        # cost is paid once.
        harness.open_sample()
        book_id = harness.audiobook.currentBookId
        harness.audiobook_lib.save_chapter_audio(book_id, 0, self.speechlike_audio())
        assert harness.audiobook_lib.load_chapter_envelope(book_id, 0) is None
        harness.audiobook.playChapter(0)
        assert wait_until(lambda: len(harness.audiobook.chapterEnvelope) > 0)
        saved = harness.audiobook_lib.load_chapter_envelope(book_id, 0)
        assert saved is not None
        assert saved == harness.audiobook.chapterEnvelope

    def test_switching_chapters_swaps_envelope(self, harness) -> None:
        harness.open_sample()
        harness.audiobook.playChapter(0)
        assert harness.worker.submitted
        harness.worker.complete_last(self.speechlike_audio())
        assert len(harness.audiobook.chapterEnvelope) > 0
        # Chapter 1 from cache (saved by a direct library write = legacy path)
        harness.audiobook_lib.save_chapter_audio(
            harness.audiobook.currentBookId, 1, self.speechlike_audio()
        )
        harness.audiobook.playChapter(1)
        assert wait_until(lambda: len(harness.audiobook.chapterEnvelope) > 0)

    def test_unreadable_chapter_leaves_envelope_empty(self, harness) -> None:
        harness.open_sample()
        book_id = harness.audiobook.currentBookId
        harness.audiobook_lib.save_chapter_audio(book_id, 0, self.speechlike_audio())
        harness.audiobook_lib.chapter_wav_path(book_id, 0).write_bytes(b"not a wav")
        harness.audiobook.playChapter(0)
        assert wait_until(lambda: not harness.audiobook_lib.envelope_path(book_id, 0).exists())
        assert harness.audiobook.chapterEnvelope == []


class TestAsyncChapterPersist:
    """Production persist path: background pool + queued status callback."""

    def test_done_persists_via_thread_pool_without_blocking_the_gui_thread(
        self, harness: Harness, qcoreapp, tmp_path: Path
    ) -> None:
        # Everything else in this file runs SyncPersistExecutor for
        # determinism; this pins the real wiring — the WAV write happens on
        # the pool thread and the ready flip arrives through a queued signal
        # (pumped here, as the live event loop would).
        async_ab = AudiobookController(
            app_controller=harness.app,
            data_dir=tmp_path,
            player_factory=lambda: PlaybackController(player_factory=lambda: harness.fake_player),
            bg_runner=run_sync,
        )
        try:
            harness.open_sample()
            book_id = harness.audiobook.currentBookId
            # Same book, independent controller: mirror the open book.
            assert async_ab.openBook(book_id) is True
            async_ab.renderChapter(0)
            assert async_ab.renderingIndex == 0
            harness.worker.complete_last(make_audio())
            assert async_ab.renderingIndex == -1  # done handled immediately…

            def landed() -> bool:
                qcoreapp.processEvents()
                return async_ab.chapters[0]["status"] == "ready"

            assert wait_until(landed, timeout=5.0)
            assert harness.audiobook_lib.has_chapter_audio(book_id, 0)
            # Envelope + timeline sidecars landed with the WAV.
            assert harness.audiobook_lib.load_chapter_envelope(book_id, 0) is not None
            assert harness.audiobook_lib.load_chapter_timeline(book_id, 0) is not None
        finally:
            async_ab.shutdown()  # flush() must drain the pool cleanly

    def test_shutdown_flushes_pending_chapter_writes(
        self, harness: Harness, qcoreapp, tmp_path: Path
    ) -> None:
        async_ab = AudiobookController(
            app_controller=harness.app,
            data_dir=tmp_path,
            player_factory=lambda: PlaybackController(player_factory=lambda: harness.fake_player),
        )
        harness.open_sample()
        book_id = harness.audiobook.currentBookId
        assert async_ab.openBook(book_id) is True
        async_ab.renderChapter(0)
        harness.worker.complete_last(make_audio())
        async_ab.shutdown()  # no event pumping: flush() alone must persist
        assert harness.audiobook_lib.has_chapter_audio(book_id, 0)

    def test_removal_while_paused_after_validation_cannot_resurrect_book(
        self, tmp_path: Path, qcoreapp, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import vienetts_app.ui.chapter_persist as persist_module
        from vienetts_app.ui.chapter_persist import PersistExecutor, _ChapterPersistJob

        validated = threading.Event()
        resume = threading.Event()
        real_validate = persist_module.validate_wav_artifact

        class PausedPersistExecutor(PersistExecutor):
            def __init__(self) -> None:
                super().__init__()
                self.thread: threading.Thread | None = None

            def submit_artifact(self, *args: Any) -> None:
                job = _ChapterPersistJob(*args, self.signals)
                self.thread = threading.Thread(target=job.run)
                self.thread.start()

            def submit_legacy_envelope(self, *args: Any) -> None:
                return None

            def flush(self, timeout_ms: int = 5000) -> None:
                if self.thread is not None:
                    self.thread.join(timeout_ms / 1000)

        persist = PausedPersistExecutor()

        def pause_after_source_validation(path: Path) -> tuple[int, int]:
            result = real_validate(path)
            if not validated.is_set():
                validated.set()
                assert resume.wait(2)
            return result

        monkeypatch.setattr(persist_module, "validate_wav_artifact", pause_after_source_validation)
        harness = Harness(tmp_path, persist_executor=persist)
        harness.open_sample()
        book_id = harness.audiobook.currentBookId
        book_dir = harness.audiobook_lib.root / book_id
        harness.audiobook.renderChapter(0)
        job = harness.worker.submitted[-1]
        assert job.artifact_path is not None
        source = make_artifact(job.artifact_path, job_id=job.id, samples=480)
        harness.worker.complete_last(source)
        assert validated.wait(2)

        harness.audiobook.removeBook(book_id)
        resume.set()
        persist.flush()
        assert wait_until(lambda: not source.path.exists())

        assert not book_dir.exists()
        assert not source.path.exists()


def _completed(job_id: str, artifact: SynthesisArtifact, owner: str = "audiobook"):
    from vienetts_app.core.jobs import JobTerminal

    return JobTerminal(job_id=job_id, owner=owner, state="completed", value=artifact)


def _failed(job_id: str, message: str):
    from vienetts_app.core.jobs import JobTerminal

    return JobTerminal(job_id=job_id, owner="audiobook", state="failed", error=message)


class TestRenderJobIdentity:
    """Phase 2 Task 3 RED: audiobook owns its render by job ID."""

    @staticmethod
    def _distinct_epub_copy(tmp_path: Path) -> Path:
        import zipfile

        target = tmp_path / "second-identity.epub"
        with (
            zipfile.ZipFile(SAMPLE_EPUB) as zin,
            zipfile.ZipFile(target, "w") as zout,
        ):
            for item in zin.infolist():
                zout.writestr(item, zin.read(item.filename))
            zout.writestr("distinct.txt", "different book id")
        return target

    def test_audiobook_ignores_foreign_terminal_after_book_switch(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        harness.open_sample()
        harness.audiobook.renderChapter(0)
        first_job = harness.worker.submitted[-1]

        assert harness.audiobook.openEpub(str(self._distinct_epub_copy(tmp_path))) is True
        book_b = harness.audiobook.currentBookId

        assert first_job.artifact_path is not None
        artifact = make_artifact(first_job.artifact_path, job_id=first_job.id, samples=480)
        harness.worker.terminal.emit(_completed(first_job.id, artifact))

        assert not harness.audiobook_lib.has_chapter_audio(book_b, 0)
        assert not artifact.path.exists()

    def test_cancel_render_targets_only_its_job(self, harness: Harness) -> None:
        harness.open_sample()
        harness.audiobook.renderChapter(0)
        job = harness.worker.submitted[-1]

        harness.audiobook.cancelRender()

        assert harness.worker.cancel_job_ids == [job.id]

    def test_stale_failed_terminal_leaves_current_book_untouched(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        harness.open_sample()
        harness.audiobook.renderChapter(0)
        first_job = harness.worker.submitted[-1]

        assert harness.audiobook.openEpub(str(self._distinct_epub_copy(tmp_path))) is True

        harness.worker.terminal.emit(_failed(first_job.id, "engine exploded"))

        ab = harness.audiobook
        assert [c["status"] for c in ab.chapters] == ["pending"] * 3
        assert ab.errorText == ""
