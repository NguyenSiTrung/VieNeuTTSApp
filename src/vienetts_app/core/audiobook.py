"""Audiobook disk workspace (FR-A2): shelf index, chapter cache, progress.

Layout under the library root (``<data_dir>/audiobooks``)::

    library.json                     shelf index (list of book records)
    <book_id>/book.json              immutable book data (metadata + chapter
                                     texts — the library is self-contained;
                                     the source .epub may move or vanish)
    <book_id>/state.json             mutable state (chapter statuses, errors,
                                     listening progress)
    <book_id>/ch_0000.wav            per-chapter synthesized audio cache

``book_id`` is the first 16 hex chars of the source EPUB's sha256 — re-import
of the same content (even from a moved file) resumes the SAME book and keeps
its rendered audio (NFR-A1: never re-synthesize what is cached).

Every read degrades instead of crashing (corrupt index → empty shelf; corrupt
state → defaults), mirroring the voices.json posture. Only ``load_book`` on a
book the user explicitly chose raises (actionable ``AudiobookError``), so the
UI can surface and offer removal. All writes are atomic (temp file + rename)
so a crash never leaves half-written JSON or a truncated WAV that would later
be played as "ready".
"""

from __future__ import annotations

import datetime as _dt
import json
import logging
import os
import re
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from vienetts_app.core.audio import write_wav_file
from vienetts_app.core.epub import EpubBook, EpubChapter

logger = logging.getLogger(__name__)

LIBRARY_INDEX_FILENAME = "library.json"
BOOK_FILENAME = "book.json"
STATE_FILENAME = "state.json"
CHAPTER_WAV_PATTERN = "ch_{index:04d}.wav"

# Render policy cap (FR-A3): chapters longer than this are refused rather
# than truncated (same policy as importers.IMPORT_CHAR_LIMIT). 60k chars ≈
# 12–15 min of audio ≈ ~140 MB transient float32 while concatenating — the
# practical ceiling before the worker's full-audio handoff gets risky.
CHAPTER_CHAR_LIMIT = 60_000

# Persisted chapter statuses. "rendering" is controller-only transient state
# and is never written to state.json.
STATUS_PENDING = "pending"
STATUS_RENDERING = "rendering"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

_EXPORT_FORBIDDEN = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


class AudiobookError(RuntimeError):
    """Audiobook library operation failed; message is user-actionable."""


@dataclass(frozen=True)
class BookRecord:
    """One shelf entry (everything list_books needs — no chapter payloads)."""

    id: str
    title: str
    author: str
    chapter_count: int
    added_at: str
    source_path: str
    content_hash: str


@dataclass(frozen=True)
class BookProgress:
    """Where the listener left off in a book (FR-A5)."""

    current_chapter: int = 0
    position_ms: int = 0
    voice: str = ""


@dataclass(frozen=True)
class BookState:
    """Full load of one book: record + chapters + statuses + progress."""

    record: BookRecord
    chapters: list[EpubChapter]
    statuses: dict[int, str]
    errors: dict[int, str]
    progress: BookProgress


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def _read_json(path: Path) -> Any:
    """Read JSON; ``None`` on any problem (missing/corrupt) with a warning."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("ignoring unreadable JSON %s (%s)", path, exc)
        return None


def _write_json_atomic(path: Path, payload: Any) -> None:
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
    os.replace(temp, path)


def _sanitize_filename_part(title: str) -> str:
    """Chapter title → cross-platform-safe filename fragment."""
    cleaned = _EXPORT_FORBIDDEN.sub(" ", title)
    cleaned = re.sub(r"\s+", " ", cleaned).strip(" .")
    return cleaned[:80]


class AudiobookLibrary:
    """Owns the audiobook workspace under ``root_dir`` (create-on-demand)."""

    def __init__(self, root_dir: str | Path) -> None:
        self.root = Path(root_dir)

    # ── shelf ────────────────────────────────────────────────────────────────

    def list_books(self) -> list[BookRecord]:
        """Shelf records in add order; unreadable index → empty shelf."""
        index = self._read_index()
        records: list[BookRecord] = []
        for entry in index:
            record = self._record_from_index(entry) if isinstance(entry, dict) else None
            if record is None or not (self.root / record.id / BOOK_FILENAME).is_file():
                continue  # stale entry (workspace deleted by hand) — skip
            records.append(record)
        return records

    def add_book(self, book: EpubBook) -> BookRecord:
        """Register ``book`` (idempotent by content hash); returns the record.

        Re-adding an existing book only refreshes ``source_path`` (the file
        may have moved) — chapters/state/audio are untouched.
        """
        book_id = book.content_hash[:16]
        workspace = self.root / book_id
        workspace.mkdir(parents=True, exist_ok=True)
        book_path = workspace / BOOK_FILENAME
        existing = _read_json(book_path)
        if isinstance(existing, dict):
            record = self._record_from_book_json(existing, book_id) or self._new_record(
                book_id, book
            )
            if record.source_path != book.source_path:
                existing["source_path"] = book.source_path
                _write_json_atomic(book_path, existing)
                record = BookRecord(
                    id=record.id,
                    title=record.title,
                    author=record.author,
                    chapter_count=record.chapter_count,
                    added_at=record.added_at,
                    source_path=book.source_path,
                    content_hash=record.content_hash,
                )
        else:
            record = self._new_record(book_id, book)
            _write_json_atomic(
                book_path,
                {
                    "id": record.id,
                    "title": record.title,
                    "author": record.author,
                    "source_path": record.source_path,
                    "content_hash": record.content_hash,
                    "added_at": record.added_at,
                    "chapters": [
                        {"index": c.index, "title": c.title, "text": c.text} for c in book.chapters
                    ],
                },
            )
        self._upsert_index(record)
        return record

    def remove_book(self, book_id: str) -> None:
        """Delete a book's workspace + shelf entry; unknown ids are a no-op."""
        shutil.rmtree(self.root / book_id, ignore_errors=True)
        index = self._read_index()
        kept = [e for e in index if isinstance(e, dict) and e.get("id") != book_id]
        if len(kept) != len(index):
            self._write_index(kept)

    # ── load / chapter data ──────────────────────────────────────────────────

    def load_book(self, book_id: str) -> BookState:
        """Full book state; raises ``AudiobookError`` for unknown/corrupt books."""
        data = _read_json(self.root / book_id / BOOK_FILENAME)
        if data is None:
            if (self.root / book_id).is_dir():
                raise AudiobookError(
                    f"This book's data file is corrupt; remove the book from the "
                    f"shelf and import the EPUB again (workspace: {book_id})."
                )
            raise AudiobookError(f"Unknown book '{book_id}'.")
        record = self._record_from_book_json(data, book_id)
        if record is None:
            raise AudiobookError(
                f"This book's data file is corrupt; remove the book from the "
                f"shelf and import the EPUB again (workspace: {book_id})."
            )
        chapters = [
            EpubChapter(
                index=int(entry.get("index", i)),
                title=str(entry.get("title") or f"Chương {i + 1}"),
                text=str(entry.get("text") or ""),
            )
            for i, entry in enumerate(data.get("chapters") or [])
            if isinstance(entry, dict)
        ]
        if not chapters:
            raise AudiobookError(
                f"This book has no chapters on disk; remove it from the shelf "
                f"and import the EPUB again (workspace: {book_id})."
            )
        state = self._read_state(book_id)
        statuses, errors = self._reconcile_status(book_id, chapters, state)
        progress = self._progress_from_state(state)
        if progress.current_chapter >= len(chapters):
            progress = BookProgress()
        return BookState(
            record=record,
            chapters=chapters,
            statuses=statuses,
            errors=errors,
            progress=progress,
        )

    def chapter_text(self, book_id: str, index: int) -> str:
        """Chapter text for render submission (full text, no truncation)."""
        return self._chapter(self.load_book(book_id), index).text

    # ── chapter audio cache ──────────────────────────────────────────────────

    def chapter_wav_path(self, book_id: str, index: int) -> Path:
        return self.root / book_id / CHAPTER_WAV_PATTERN.format(index=index)

    def has_chapter_audio(self, book_id: str, index: int) -> bool:
        return self.chapter_wav_path(book_id, index).is_file()

    def save_chapter_audio(
        self, book_id: str, index: int, audio: np.ndarray, sample_rate: int = 48_000
    ) -> Path:
        """Atomically cache a rendered chapter and mark it ``ready``."""
        state = self.load_book(book_id)  # validates book + index range below
        self._chapter(state, index)
        target = self.chapter_wav_path(book_id, index)
        try:
            # Temp keeps the .wav suffix: soundfile infers the container
            # format from the file extension.
            temp = target.with_name(f"{target.stem}.part.wav")
            write_wav_file(audio, temp, sample_rate=sample_rate)
            os.replace(temp, target)
        except Exception as exc:
            raise AudiobookError(f"Could not save the rendered chapter: {exc}") from exc
        self._mutate_state(
            book_id,
            lambda st: (
                st.setdefault("statuses", {}).__setitem__(str(index), STATUS_READY),
                st.setdefault("errors", {}).pop(str(index), None),
            ),
        )
        return target

    def mark_chapter_failed(self, book_id: str, index: int, message: str) -> None:
        def mutate(st: dict[str, Any]) -> None:
            st.setdefault("statuses", {})[str(index)] = STATUS_FAILED
            st.setdefault("errors", {})[str(index)] = message

        self._mutate_state(book_id, mutate)

    # ── progress (FR-A5) ─────────────────────────────────────────────────────

    def set_progress(
        self, book_id: str, current_chapter: int, position_ms: int, voice: str
    ) -> None:
        state = self.load_book(book_id)
        if not 0 <= current_chapter < len(state.chapters):
            raise AudiobookError(
                f"chapter index {current_chapter} out of range (0..{len(state.chapters) - 1})"
            )
        if position_ms < 0:
            raise AudiobookError("position_ms must be >= 0")

        def mutate(st: dict[str, Any]) -> None:
            st["progress"] = {
                "current_chapter": current_chapter,
                "position_ms": int(position_ms),
                "voice": str(voice),
            }

        self._mutate_state(book_id, mutate)

    # ── export (FR-A6) ───────────────────────────────────────────────────────

    def export_chapter(self, book_id: str, index: int, dest_dir: str | Path) -> Path:
        """Copy a rendered chapter into ``dest_dir`` as ``NN - Title.wav``."""
        state = self.load_book(book_id)
        chapter = self._chapter(state, index)
        source = self.chapter_wav_path(book_id, index)
        if not source.is_file():
            raise AudiobookError(
                f"Chapter {index + 1} ('{chapter.title}') has not been rendered yet — "
                "render it first, then export."
            )
        dest = Path(dest_dir)
        name_part = _sanitize_filename_part(chapter.title) or f"chuong-{index + 1}"
        target = dest / f"{index + 1:02d} - {name_part}.wav"
        try:
            dest.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
        except OSError as exc:
            raise AudiobookError(f"Could not export the chapter: {exc}") from exc
        return target

    # ── internals ────────────────────────────────────────────────────────────

    def _new_record(self, book_id: str, book: EpubBook) -> BookRecord:
        return BookRecord(
            id=book_id,
            title=book.title,
            author=book.author,
            chapter_count=len(book.chapters),
            added_at=_utc_now_iso(),
            source_path=book.source_path,
            content_hash=book.content_hash,
        )

    def _record_from_book_json(self, data: dict[str, Any], book_id: str) -> BookRecord | None:
        title = str(data.get("title") or "").strip()
        if not title:
            return None
        return BookRecord(
            id=book_id,
            title=title,
            author=str(data.get("author") or ""),
            chapter_count=len(data.get("chapters") or []),
            added_at=str(data.get("added_at") or ""),
            source_path=str(data.get("source_path") or ""),
            content_hash=str(data.get("content_hash") or ""),
        )

    def _record_from_index(self, entry: dict[str, Any]) -> BookRecord | None:
        book_id = str(entry.get("id") or "").strip()
        title = str(entry.get("title") or "").strip()
        if not book_id or not title:
            return None
        return BookRecord(
            id=book_id,
            title=title,
            author=str(entry.get("author") or ""),
            chapter_count=int(entry.get("chapter_count") or 0),
            added_at=str(entry.get("added_at") or ""),
            source_path=str(entry.get("source_path") or ""),
            content_hash=str(entry.get("content_hash") or ""),
        )

    def _read_index(self) -> list[Any]:
        data = _read_json(self.root / LIBRARY_INDEX_FILENAME)
        if isinstance(data, dict):
            data = data.get("books")
        return data if isinstance(data, list) else []

    def _write_index(self, books: list[Any]) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        _write_json_atomic(self.root / LIBRARY_INDEX_FILENAME, {"books": books})

    def _upsert_index(self, record: BookRecord) -> None:
        index = self._read_index()
        kept = [e for e in index if not (isinstance(e, dict) and e.get("id") == record.id)]
        kept.append(
            {
                "id": record.id,
                "title": record.title,
                "author": record.author,
                "chapter_count": record.chapter_count,
                "added_at": record.added_at,
                "source_path": record.source_path,
                "content_hash": record.content_hash,
            }
        )
        self._write_index(kept)

    def _read_state(self, book_id: str) -> dict[str, Any]:
        state = _read_json(self.root / book_id / STATE_FILENAME)
        return state if isinstance(state, dict) else {}

    def _mutate_state(self, book_id: str, mutate: Callable[[dict[str, Any]], Any]) -> None:
        state = self._read_state(book_id)
        mutate(state)
        self.root.joinpath(book_id).mkdir(parents=True, exist_ok=True)
        _write_json_atomic(self.root / book_id / STATE_FILENAME, state)

    def _reconcile_status(
        self, book_id: str, chapters: list[EpubChapter], state: dict[str, Any]
    ) -> tuple[dict[int, str], dict[int, str]]:
        """Persisted statuses + errors, re-verified against the WAV cache.

        ``ready`` without its WAV file (deleted by hand) degrades to
        ``pending`` so a chapter is never believed cached when it is not.
        Unknown/stale chapter keys are dropped.
        """
        raw_statuses = state.get("statuses") or {}
        raw_errors = state.get("errors") or {}
        statuses: dict[int, str] = {}
        errors: dict[int, str] = {}
        for chapter in chapters:
            status = str(raw_statuses.get(str(chapter.index), STATUS_PENDING))
            if status not in (STATUS_PENDING, STATUS_READY, STATUS_FAILED, STATUS_RENDERING):
                status = STATUS_PENDING
            if status == STATUS_READY and not self.has_chapter_audio(book_id, chapter.index):
                status = STATUS_PENDING
            if status == STATUS_READY:
                status = STATUS_READY
            statuses[chapter.index] = status
            error = raw_errors.get(str(chapter.index))
            if status != STATUS_PENDING and isinstance(error, str):
                errors[chapter.index] = error
        return statuses, errors

    def _progress_from_state(self, state: dict[str, Any]) -> BookProgress:
        raw = state.get("progress")
        if not isinstance(raw, dict):
            return BookProgress()
        try:
            return BookProgress(
                current_chapter=max(0, int(raw.get("current_chapter", 0))),
                position_ms=max(0, int(raw.get("position_ms", 0))),
                voice=str(raw.get("voice") or ""),
            )
        except (TypeError, ValueError):
            return BookProgress()

    @staticmethod
    def _chapter(state: BookState, index: int) -> EpubChapter:
        if not 0 <= index < len(state.chapters):
            raise AudiobookError(
                f"chapter index {index} out of range (0..{len(state.chapters) - 1})"
            )
        return state.chapters[index]
