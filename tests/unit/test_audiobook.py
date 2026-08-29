"""Audiobook library (FR-A2): disk workspace, cache, progress, export."""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pytest

from vienetts_app.core.audio import read_wav
from vienetts_app.core.audiobook import (
    CHAPTER_CHAR_LIMIT,
    AudiobookError,
    AudiobookLibrary,
)
from vienetts_app.core.epub import EpubBook, EpubChapter

SAMPLE_RATE = 48_000


def make_book(path: str = "/books/sample.epub", chapters: int = 2) -> EpubBook:
    return EpubBook(
        title="Sách thử nghiệm",
        author="Tác Giả A",
        chapters=[
            EpubChapter(index=i, title=f"Chương {i + 1}", text=f"Nội dung chương {i + 1}.")
            for i in range(chapters)
        ],
        source_path=path,
        content_hash="0" * 64,  # stable 64-hex; tests override per-book
    )


def make_audio(seconds: float = 0.05) -> np.ndarray:
    return (np.sin(np.linspace(0.0, 440.0 * 6.28, int(SAMPLE_RATE * seconds))) * 0.3).astype(
        np.float32
    )


@pytest.fixture()
def library(tmp_path: Path) -> AudiobookLibrary:
    return AudiobookLibrary(tmp_path / "audiobooks")


class TestAddAndList:
    def test_add_creates_workspace_and_index_entry(self, library: AudiobookLibrary) -> None:
        record = library.add_book(make_book())
        workspace = library.root / record.id
        assert (workspace / "book.json").is_file()
        books = library.list_books()
        assert [b.id for b in books] == [record.id]
        assert books[0].title == "Sách thử nghiệm"
        assert books[0].author == "Tác Giả A"
        assert books[0].chapter_count == 2

    def test_record_fields(self, library: AudiobookLibrary) -> None:
        record = library.add_book(make_book())
        assert record.id == "0" * 16  # first 16 hex of the content hash
        assert record.source_path == "/books/sample.epub"
        assert record.content_hash == "0" * 64
        assert record.added_at  # ISO-ish timestamp recorded

    def test_same_content_reuses_id_and_updates_source_path(
        self, library: AudiobookLibrary
    ) -> None:
        first = library.add_book(make_book(path="/a/book.epub"))
        second = library.add_book(make_book(path="/moved/book.epub"))
        assert second.id == first.id
        assert len(library.list_books()) == 1
        assert library.list_books()[0].source_path == "/moved/book.epub"

    def test_different_books_coexist(self, library: AudiobookLibrary) -> None:
        library.add_book(make_book())
        other = make_book()
        object.__setattr__(other, "content_hash", "f" * 64)  # type: ignore[misc]
        library.add_book(other)
        assert len(library.list_books()) == 2

    def test_list_books_empty_when_root_missing(self, tmp_path: Path) -> None:
        library = AudiobookLibrary(tmp_path / "nothing")
        assert library.list_books() == []

    def test_list_books_skips_workspace_without_book_json(self, library: AudiobookLibrary) -> None:
        library.add_book(make_book())
        (library.root / "deadbeef00000000").mkdir(parents=True, exist_ok=True)
        assert len(library.list_books()) == 1


class TestLoadBook:
    def test_load_returns_chapters_with_texts(self, library: AudiobookLibrary) -> None:
        record = library.add_book(make_book(chapters=3))
        state = library.load_book(record.id)
        assert [c.title for c in state.chapters] == ["Chương 1", "Chương 2", "Chương 3"]
        assert state.chapters[2].text == "Nội dung chương 3."

    def test_all_chapters_pending_initially(self, library: AudiobookLibrary) -> None:
        record = library.add_book(make_book())
        assert library.load_book(record.id).statuses == {0: "pending", 1: "pending"}

    def test_load_unknown_book_raises(self, library: AudiobookLibrary) -> None:
        with pytest.raises(AudiobookError, match="Unknown book"):
            library.load_book("n0tsuchb00k12345")

    def test_corrupt_book_json_raises_actionable(self, library: AudiobookLibrary) -> None:
        record = library.add_book(make_book())
        (library.root / record.id / "book.json").write_text("{broken", encoding="utf-8")
        with pytest.raises(AudiobookError, match="corrupt"):
            library.load_book(record.id)

    def test_ready_status_reconciled_when_wav_missing(self, library: AudiobookLibrary) -> None:
        record = library.add_book(make_book())
        library.save_chapter_audio(record.id, 0, make_audio())
        assert library.load_book(record.id).statuses[0] == "ready"
        library.chapter_wav_path(record.id, 0).unlink()
        assert library.load_book(record.id).statuses[0] == "pending"


class TestChapterAudio:
    def test_save_then_read_round_trip(self, library: AudiobookLibrary) -> None:
        record = library.add_book(make_book())
        audio = make_audio()
        path = library.save_chapter_audio(record.id, 0, audio)
        assert path == library.chapter_wav_path(record.id, 0)
        loaded, rate = read_wav(path)
        assert rate == SAMPLE_RATE
        assert loaded.shape == audio.shape
        assert library.has_chapter_audio(record.id, 0)
        assert not library.has_chapter_audio(record.id, 1)

    def test_save_marks_ready_and_clears_error(self, library: AudiobookLibrary) -> None:
        record = library.add_book(make_book())
        library.mark_chapter_failed(record.id, 0, "boom")
        library.save_chapter_audio(record.id, 0, make_audio())
        state = library.load_book(record.id)
        assert state.statuses[0] == "ready"
        assert state.errors == {}

    def test_failed_status_records_message(self, library: AudiobookLibrary) -> None:
        record = library.add_book(make_book())
        library.mark_chapter_failed(record.id, 1, "engine exploded")
        state = library.load_book(record.id)
        assert state.statuses[1] == "failed"
        assert state.errors[1] == "engine exploded"

    def test_save_unknown_book_raises(self, library: AudiobookLibrary) -> None:
        with pytest.raises(AudiobookError):
            library.save_chapter_audio("n0tsuchb00k12345", 0, make_audio())

    def test_save_invalid_index_raises(self, library: AudiobookLibrary) -> None:
        record = library.add_book(make_book(chapters=1))
        with pytest.raises(AudiobookError, match="index"):
            library.save_chapter_audio(record.id, 5, make_audio())


class TestProgress:
    def test_progress_round_trip(self, library: AudiobookLibrary) -> None:
        record = library.add_book(make_book())
        assert library.load_book(record.id).progress.current_chapter == 0
        library.set_progress(record.id, current_chapter=1, position_ms=12_345, voice="Adam")
        progress = library.load_book(record.id).progress
        assert (progress.current_chapter, progress.position_ms, progress.voice) == (
            1,
            12_345,
            "Adam",
        )

    def test_progress_survives_state_rewrites(self, library: AudiobookLibrary) -> None:
        record = library.add_book(make_book())
        library.set_progress(record.id, current_chapter=1, position_ms=99, voice="Eva")
        library.save_chapter_audio(record.id, 0, make_audio())  # unrelated churn
        assert library.load_book(record.id).progress.position_ms == 99

    def test_progress_rejects_bad_chapter_index(self, library: AudiobookLibrary) -> None:
        record = library.add_book(make_book(chapters=2))
        with pytest.raises(AudiobookError, match="index"):
            library.set_progress(record.id, current_chapter=9, position_ms=0, voice="")

    def test_set_progress_after_load_skips_full_book_reload(
        self, library: AudiobookLibrary, monkeypatch
    ) -> None:
        # Regression: set_progress called load_book (a full book.json parse —
        # every chapter text) on EVERY call just to validate the chapter
        # index; the playback position tick fires it every ~2 s on the GUI
        # thread. After one load the cached chapter count must suffice.
        record = library.add_book(make_book(chapters=3))
        library.load_book(record.id)  # prime the cache

        def no_reload(_book_id):
            raise AssertionError("set_progress re-parsed book.json (cache miss)")

        monkeypatch.setattr(library, "load_book", no_reload)
        library.set_progress(record.id, current_chapter=2, position_ms=500, voice="Adam")
        monkeypatch.undo()
        assert library.load_book(record.id).progress.current_chapter == 2

    def test_set_progress_unknown_book_still_validates_via_load(
        self, library: AudiobookLibrary
    ) -> None:
        with pytest.raises(AudiobookError, match="Unknown book"):
            library.set_progress("no-such-book", current_chapter=0, position_ms=0, voice="")


class TestRemove:
    def test_remove_deletes_workspace_and_index_entry(self, library: AudiobookLibrary) -> None:
        record = library.add_book(make_book())
        library.remove_book(record.id)
        assert not (library.root / record.id).exists()
        assert library.list_books() == []

    def test_remove_unknown_is_noop(self, library: AudiobookLibrary) -> None:
        library.remove_book("n0tsuchb00k12345")  # must not raise


class TestExport:
    def test_export_names_file_with_index_and_title(
        self, library: AudiobookLibrary, tmp_path: Path
    ) -> None:
        record = library.add_book(make_book())
        library.save_chapter_audio(record.id, 0, make_audio())
        dest = tmp_path / "out"
        exported = library.export_chapter(record.id, 0, dest)
        assert exported == dest / "01 - Chương 1.wav"
        assert exported.is_file()
        _, rate = read_wav(exported)
        assert rate == SAMPLE_RATE

    def test_export_sanitizes_unsafe_titles(
        self, library: AudiobookLibrary, tmp_path: Path
    ) -> None:
        book = make_book()
        object.__setattr__(
            book,
            "chapters",
            [EpubChapter(0, 'Chương "1" / Hai: <đường>', "nội dung.")],
        )
        record = library.add_book(book)
        library.save_chapter_audio(record.id, 0, make_audio())
        exported = library.export_chapter(record.id, 0, tmp_path / "out")
        name = exported.name
        assert "/" not in name
        assert exported.is_file()

    def test_export_without_audio_raises(self, library: AudiobookLibrary, tmp_path: Path) -> None:
        record = library.add_book(make_book())
        with pytest.raises(AudiobookError, match="not been rendered"):
            library.export_chapter(record.id, 0, tmp_path / "out")


class TestChapterCharLimit:
    def test_limit_is_bounded_and_documented(self) -> None:
        assert 1_000 <= CHAPTER_CHAR_LIMIT <= 200_000

    def test_chapter_text_returns_full_text(self, library: AudiobookLibrary) -> None:
        record = library.add_book(make_book())
        assert library.chapter_text(record.id, 1) == "Nội dung chương 2."


class TestChapterTimeline:
    """FR-A9: ch_XXXX.timeline.json next to the WAV (measured render timing)."""

    def _saved_book_with_audio(self, library: AudiobookLibrary) -> str:
        record = library.add_book(make_book())
        library.save_chapter_audio(record.id, 0, make_audio())
        return record.id

    def test_timeline_round_trip(self, library: AudiobookLibrary) -> None:
        from vienetts_app.core.timeline import SegmentSpan, Timeline

        book_id = self._saved_book_with_audio(library)
        timeline = Timeline(
            (SegmentSpan(0, 8, 0, 1000), SegmentSpan(10, 18, 1000, 3000)),
            approximate=False,
        )
        path = library.save_chapter_timeline(book_id, 0, timeline)
        assert path == library.timeline_path(book_id, 0)
        assert path.name == "ch_0000.timeline.json"
        assert library.load_chapter_timeline(book_id, 0) == timeline

    def test_round_trip_preserves_approximate_flag(self, library: AudiobookLibrary) -> None:
        from vienetts_app.core.timeline import estimate_timeline

        book_id = self._saved_book_with_audio(library)
        timeline = estimate_timeline("Câu một. Câu hai.", 8_000)
        library.save_chapter_timeline(book_id, 0, timeline)
        assert library.load_chapter_timeline(book_id, 0) == timeline

    def test_load_missing_timeline_returns_none(self, library: AudiobookLibrary) -> None:
        book_id = self._saved_book_with_audio(library)
        assert library.load_chapter_timeline(book_id, 0) is None

    def test_load_corrupt_timeline_returns_none(self, library: AudiobookLibrary) -> None:
        book_id = self._saved_book_with_audio(library)
        library.timeline_path(book_id, 0).write_text("{not json", encoding="utf-8")
        assert library.load_chapter_timeline(book_id, 0) is None

    def test_load_timeline_without_audio_returns_none(self, library: AudiobookLibrary) -> None:
        from vienetts_app.core.timeline import SegmentSpan, Timeline

        record = library.add_book(make_book())
        library.save_chapter_timeline(
            record.id,
            0,
            Timeline((SegmentSpan(0, 8, 0, 1000),)),
        )
        # A timeline without its WAV is useless (the WAV may have been deleted
        # by hand) — degrade to None so the reader falls back to an estimate
        # once the re-render's duration is known.
        assert library.load_chapter_timeline(record.id, 0) is None

    def test_save_unknown_book_raises(self, library: AudiobookLibrary) -> None:
        from vienetts_app.core.timeline import SegmentSpan, Timeline

        with pytest.raises(AudiobookError, match="Unknown book"):
            library.save_chapter_timeline("unknown", 0, Timeline((SegmentSpan(0, 1, 0, 1),)))

    def test_save_invalid_index_raises(self, library: AudiobookLibrary) -> None:
        from vienetts_app.core.timeline import SegmentSpan, Timeline

        book_id = self._saved_book_with_audio(library)
        with pytest.raises(AudiobookError, match="out of range"):
            library.save_chapter_timeline(book_id, 9, Timeline((SegmentSpan(0, 1, 0, 1),)))


class TestChapterEnvelope:
    """Waveform overview sidecar: ch_XXXX.waveform.json next to the WAV."""

    def _saved_book_with_audio(self, library: AudiobookLibrary) -> str:
        record = library.add_book(make_book())
        library.save_chapter_audio(record.id, 0, make_audio())
        return record.id

    def test_envelope_round_trip(self, library: AudiobookLibrary) -> None:
        book_id = self._saved_book_with_audio(library)
        buckets = [0.25, 0.5, 1.0, 0.75]
        path = library.save_chapter_envelope(book_id, 0, buckets)
        assert path == library.envelope_path(book_id, 0)
        assert path.name == "ch_0000.waveform.json"
        assert library.load_chapter_envelope(book_id, 0) == buckets

    def test_load_missing_envelope_returns_none(self, library: AudiobookLibrary) -> None:
        book_id = self._saved_book_with_audio(library)
        assert library.load_chapter_envelope(book_id, 0) is None

    def test_load_corrupt_envelope_returns_none(self, library: AudiobookLibrary) -> None:
        book_id = self._saved_book_with_audio(library)
        library.envelope_path(book_id, 0).write_text("{not json", encoding="utf-8")
        assert library.load_chapter_envelope(book_id, 0) is None

    def test_load_envelope_without_audio_returns_none(self, library: AudiobookLibrary) -> None:
        book_id = self._saved_book_with_audio(library)
        library.save_chapter_envelope(book_id, 0, [0.5, 1.0])
        library.chapter_wav_path(book_id, 0).unlink()  # audio gone: useless sidecar
        assert library.load_chapter_envelope(book_id, 0) is None

    def test_save_unknown_book_raises(self, library: AudiobookLibrary) -> None:
        with pytest.raises(AudiobookError):
            library.save_chapter_envelope("nope", 0, [1.0])

    def test_save_invalid_index_raises(self, library: AudiobookLibrary) -> None:
        book_id = self._saved_book_with_audio(library)
        with pytest.raises(AudiobookError):
            library.save_chapter_envelope(book_id, 9, [1.0])
