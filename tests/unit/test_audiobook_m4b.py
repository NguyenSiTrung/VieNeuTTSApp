"""Unit and integration tests for core/m4b_export.py (BooksReader Tier T1 M4B)."""

from __future__ import annotations

import json
import logging
import os
import struct
import subprocess
import threading
import time
import zlib
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from vienetts_app.core import m4b_export
from vienetts_app.core.audio import ExportCancelled, write_wav_file
from vienetts_app.core.audiobook import (
    AudiobookError,
    AudiobookLibrary,
    BookExportMetadata,
)
from vienetts_app.core.chapter_split import ChapterPart
from vienetts_app.core.epub import EpubBook, EpubChapter
from vienetts_app.core.m4b_export import (
    ALLOWED_M4B_BITRATES,
    M4bChapter,
    build_concat_list,
    build_ffmetadata,
    escape_ffmetadata_value,
    export_m4b_audiobook,
    measure_m4b_chapters,
    probe_wav_duration_frames,
    validate_mp4_container,
)
from vienetts_app.core.processes import process_alive
from vienetts_app.core.video_dub import VideoDubError, resolve_ffmpeg, resolve_ffprobe

SAMPLE_RATE = 48_000


def _make_png_bytes(width: int = 8, height: int = 8) -> bytes:
    """Create a minimal valid RGB PNG in pure Python (no Pillow dependency)."""

    def _chunk(chunk_type: bytes, data: bytes) -> bytes:
        crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
        return struct.pack(">I", len(data)) + chunk_type + data + struct.pack(">I", crc)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw_rows = b"".join(b"\x00" + b"\x40\x80\xc0" * width for _ in range(height))
    idat = zlib.compress(raw_rows)
    return (
        b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IDAT", idat) + _chunk(b"IEND", b"")
    )


def _make_gif_bytes() -> bytes:
    """Minimal valid 1x1 GIF89a image (35 bytes)."""
    return (
        b"GIF89a\x01\x00\x01\x00\x80\x00\x00\xff\x00\x00\x00\x00\x00"
        b"!\xf9\x04\x01\x00\x00\x00\x00,\x00\x00\x00\x00\x01\x00\x01\x00"
        b"\x00\x02\x02D\x01\x00;"
    )


def _make_jpeg_bytes() -> bytes:
    """Minimal JPEG-sniffed bytes for stub tests."""
    return b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


def _make_webp_bytes() -> bytes:
    """Minimal WebP-sniffed bytes for stub tests."""
    payload = b"VP8 " + struct.pack("<I", 4) + b"\x00\x00\x00\x00"
    return b"RIFF" + struct.pack("<I", 4 + len(payload)) + b"WEBP" + payload


def _make_tone(frames: int, sample_rate: int = SAMPLE_RATE, freq: float = 440.0) -> np.ndarray:
    t = np.arange(frames, dtype=np.float32) / float(sample_rate)
    return (np.sin(2.0 * np.pi * freq * t) * 0.25).astype(np.float32)


def _minimal_mp4_bytes() -> bytes:
    """Minimal structurally valid MP4 with top-level ``ftyp`` and ``moov`` boxes."""
    ftyp = struct.pack(">I4s4sI", 16, b"ftyp", b"M4B ", 0)
    moov = struct.pack(">I4s", 8, b"moov")
    return ftyp + moov


def _make_book(
    *,
    title: str = "Sách nói thử nghiệm",
    authors: tuple[str, ...] = ("Nguyễn Văn A",),
    chapters: list[EpubChapter] | None = None,
    year: int | None = 2024,
    description: str = "Mô tả sách nói.",
    cover_bytes: bytes | None = None,
    cover_ext: str = "",
    content_hash: str = "a" * 64,
) -> EpubBook:
    ch_list = chapters or [
        EpubChapter(index=0, title="Chương 1: Mở đầu", text="Nội dung chương một."),
        EpubChapter(index=1, title="Chương 2: Tiếp theo", text="Nội dung chương hai."),
    ]
    return EpubBook(
        title=title,
        author="; ".join(authors),
        authors=authors,
        language="vi",
        year=year,
        description=description,
        cover_bytes=cover_bytes,
        cover_ext=cover_ext,
        chapters=ch_list,
        source_path="/books/sample.epub",
        content_hash=content_hash,
    )


def _save_multipart_chapter(
    library: AudiobookLibrary,
    book_id: str,
    chapter_index: int,
    part_frames: list[int],
    sample_rate: int = SAMPLE_RATE,
) -> list[Path]:
    parts = [
        ChapterPart(char_start=i * 100, char_end=(i + 1) * 100) for i in range(len(part_frames))
    ]
    library.save_segment_plan(book_id, chapter_index, parts)
    saved: list[Path] = []
    for seg_idx, n_frames in enumerate(part_frames):
        tmp = library.root / book_id / f".tmp_s{seg_idx:02d}.wav"
        write_wav_file(_make_tone(n_frames, sample_rate), tmp, sample_rate=sample_rate)
        promoted = library.promote_chapter_part(book_id, chapter_index, tmp, segment=seg_idx)
        saved.append(promoted)
    return saved


def _write_stub_ffmpeg(
    path: Path,
    capture_dir: Path,
    *,
    cover_fail: bool = False,
    main_fail: bool = False,
    emit_progress: list[str] | None = None,
    sleep_s: float = 0.0,
    pid_file: Path | None = None,
    output_bytes: bytes | None = None,
) -> Path:
    """Create a cross-platform Python shebang stub ffmpeg executable."""
    capture_dir.mkdir(parents=True, exist_ok=True)
    mp4_hex = (_minimal_mp4_bytes() if output_bytes is None else output_bytes).hex()
    png_hex = _make_png_bytes(2, 2).hex()
    script = (
        "#!/usr/bin/env python3\n"
        "import json, os, shutil, sys, time\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        f"capture = Path({str(capture_dir)!r})\n"
        "call_idx = len(os.listdir(capture))\n"
        "call_dir = capture / str(call_idx)\n"
        "call_dir.mkdir(parents=True, exist_ok=True)\n"
        "(call_dir / 'argv.json').write_text(json.dumps(args), encoding='utf-8')\n"
        "for i, tok in enumerate(args):\n"
        "    if tok == '-i' and i + 1 < len(args):\n"
        "        src = Path(args[i + 1])\n"
        "        if src.is_file() and src.name in ('concat.txt', 'ffmetadata.txt'):\n"
        "            shutil.copy(src, call_dir / src.name)\n"
        f"pid_file = {str(pid_file)!r} if {pid_file is not None!r} else None\n"
        "if pid_file:\n"
        "    Path(pid_file).write_text(str(os.getpid()), encoding='utf-8')\n"
        "if '-frames:v' in args:\n"
        f"    if {cover_fail!r}:\n"
        "        sys.stderr.write('unsupported cover format\\n')\n"
        "        sys.exit(1)\n"
        f"    Path(args[-1]).write_bytes(bytes.fromhex({png_hex!r}))\n"
        "    sys.exit(0)\n"
        f"if {sleep_s!r} > 0:\n"
        f"    time.sleep({sleep_s!r})\n"
        f"for line in {list(emit_progress or [])!r}:\n"
        "    print(line, flush=True)\n"
        f"if {main_fail!r}:\n"
        "    sys.stderr.write('simulated encoder crash\\n')\n"
        "    sys.exit(1)\n"
        f"Path(args[-1]).write_bytes(bytes.fromhex({mp4_hex!r}))\n"
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def _write_stub_ffprobe(path: Path, *, stdout: str = "audio\n", returncode: int = 0) -> Path:
    script = (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"sys.stdout.write({stdout!r})\n"
        "sys.stdout.flush()\n"
        f"sys.exit({returncode!r})\n"
    )
    path.write_text(script, encoding="utf-8")
    path.chmod(0o755)
    return path


def _read_recorded_calls(capture_dir: Path) -> list[list[str]]:
    calls: list[list[str]] = []
    idx = 0
    while (capture_dir / str(idx) / "argv.json").is_file():
        calls.append(json.loads((capture_dir / str(idx) / "argv.json").read_text(encoding="utf-8")))
        idx += 1
    return calls


@pytest.fixture()
def library(tmp_path: Path) -> AudiobookLibrary:
    return AudiobookLibrary(tmp_path / "audiobooks")


# ── Task 4.1: FFMETADATA1 escaping, WAV header timing, concat list ───────────


class TestFfmetadataAndConcat:
    def test_escape_ffmetadata_value_special_chars_and_vietnamese(self) -> None:
        assert escape_ffmetadata_value("") == ""
        assert escape_ffmetadata_value("Chương 1: Tiếng Việt đẹp") == "Chương 1: Tiếng Việt đẹp"
        assert (
            escape_ffmetadata_value("a\\b=c;d#e\nf\r\ng\rh") == "a\\\\b\\=c\\;d\\#e\\\nf\\\ng\\\nh"
        )
        # Backslash immediately before newline must escape both independently.
        assert escape_ffmetadata_value("end\\\nnext") == "end\\\\\\\nnext"

    def test_probe_wav_duration_frames_header_only_no_decode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import soundfile as sf

        wav = tmp_path / "sample.wav"
        write_wav_file(_make_tone(24_000, SAMPLE_RATE), wav, sample_rate=SAMPLE_RATE)

        def _forbid_read(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("soundfile.read must not be called")

        monkeypatch.setattr(sf, "read", _forbid_read)
        assert probe_wav_duration_frames(wav) == (24_000, SAMPLE_RATE)

        with pytest.raises(AudiobookError):
            probe_wav_duration_frames(tmp_path / "missing.wav")

        bad_wav = tmp_path / "corrupt.wav"
        bad_wav.write_bytes(b"not a wav file")
        with pytest.raises(AudiobookError):
            probe_wav_duration_frames(bad_wav)

        class _ZeroInfo:
            frames = 0
            samplerate = 48_000

        monkeypatch.setattr(sf, "info", lambda _p: _ZeroInfo())
        with pytest.raises(AudiobookError):
            probe_wav_duration_frames(wav)

    def test_measure_m4b_chapters_standard_and_multipart_exact_cumulative(
        self, library: AudiobookLibrary, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import soundfile as sf

        book = _make_book(
            chapters=[
                EpubChapter(index=0, title="Chương 1", text="Một."),
                EpubChapter(index=1, title="Chương 2 (chưa tạo)", text="Hai."),
                EpubChapter(index=2, title="Chương 3 (nhiều phần)", text="Ba."),
                EpubChapter(index=3, title="Chương 4", text="Bốn."),
            ]
        )
        record = library.add_book(book)
        # ch 0: 16,000 frames @ 48 kHz = 333.333... ms
        library.save_chapter_audio(record.id, 0, _make_tone(16_000))
        # ch 2: 3 parts of 16,000 frames @ 48 kHz = exactly 1,000.0 ms combined
        part_paths = _save_multipart_chapter(library, record.id, 2, [16_000, 16_000, 16_000])
        # ch 3: 24,000 frames @ 48 kHz = 500.0 ms
        library.save_chapter_audio(record.id, 3, _make_tone(24_000))

        def _forbid_read(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("measure_m4b_chapters must not decode WAV samples")

        monkeypatch.setattr(sf, "read", _forbid_read)

        chapters, total_ms = measure_m4b_chapters(library, record.id, [0, 2, 3])
        assert total_ms == 1833
        assert len(chapters) == 3

        assert chapters[0].index == 0
        assert chapters[0].title == "Chương 1"
        assert (chapters[0].start_ms, chapters[0].end_ms) == (0, 333)
        assert len(chapters[0].audio_paths) == 1

        # Multi-part oversize chapter merges into ONE M4bChapter with all 3 WAVs in order.
        assert chapters[1].index == 2
        assert chapters[1].title == "Chương 3 (nhiều phần)"
        assert (chapters[1].start_ms, chapters[1].end_ms) == (333, 1333)
        assert chapters[1].audio_paths == tuple(part_paths)

        assert chapters[2].index == 3
        assert (chapters[2].start_ms, chapters[2].end_ms) == (1333, 1833)

        for prev, curr in zip(chapters, chapters[1:], strict=False):
            assert prev.start_ms < prev.end_ms <= curr.start_ms < total_ms
        assert chapters[-1].start_ms < chapters[-1].end_ms == total_ms

    def test_measure_m4b_chapters_tiny_sub_ms_remains_strictly_monotonic(
        self, library: AudiobookLibrary
    ) -> None:
        record = library.add_book(_make_book())
        # 10 frames @ 48 kHz ≈ 0.21 ms each
        library.save_chapter_audio(record.id, 0, _make_tone(10))
        library.save_chapter_audio(record.id, 1, _make_tone(10))

        chapters, total_ms = measure_m4b_chapters(library, record.id, [0, 1])
        assert total_ms >= 2
        assert len(chapters) == 2
        assert 0 <= chapters[0].start_ms < chapters[0].end_ms
        assert chapters[0].end_ms <= chapters[1].start_ms < chapters[1].end_ms == total_ms

    def test_measure_m4b_chapters_rejects_missing_or_partial_chapter(
        self, library: AudiobookLibrary
    ) -> None:
        record = library.add_book(_make_book())
        with pytest.raises(AudiobookError):
            measure_m4b_chapters(library, record.id, [0])

        # Partially rendered multi-part chapter
        library.save_segment_plan(
            record.id,
            1,
            [ChapterPart(0, 50), ChapterPart(50, 100)],
        )
        tmp = library.root / record.id / ".s0.wav"
        write_wav_file(_make_tone(4_800), tmp, sample_rate=SAMPLE_RATE)
        library.promote_chapter_part(record.id, 1, tmp, segment=0)
        with pytest.raises(AudiobookError):
            measure_m4b_chapters(library, record.id, [1])

    def test_build_ffmetadata_tags_escaping_and_empty_omission(self) -> None:
        meta = BookExportMetadata(
            title="Sách = Hay; #1",
            author="Fallback",
            authors=("Nguyễn Du", "Trần Bình"),
            year=1820,
            description="Dòng 1 \\ tốt\nDòng 2",
        )
        chapters = [
            M4bChapter(
                index=0,
                title="Chương 1: A = B; C #1",
                start_ms=0,
                end_ms=1500,
                audio_paths=(Path("/a.wav"),),
            ),
            M4bChapter(
                index=1,
                title="Chương 2: Kết thúc",
                start_ms=1500,
                end_ms=3200,
                audio_paths=(Path("/b.wav"),),
            ),
        ]
        rendered = build_ffmetadata(meta, chapters, narrator="  Giọng đọc Việt  ")
        lines = rendered.splitlines()
        assert lines[0] == ";FFMETADATA1"
        assert "title=Sách \\= Hay\\; \\#1" in lines
        assert "album=Sách \\= Hay\\; \\#1" in lines
        assert "artist=Nguyễn Du\\; Trần Bình" in lines
        assert "album_artist=Nguyễn Du\\; Trần Bình" in lines
        assert "composer=Giọng đọc Việt" in lines
        assert "date=1820" in lines
        assert "comment=Dòng 1 \\\\ tốt\\\nDòng 2" in rendered
        assert (
            "[CHAPTER]\nTIMEBASE=1/1000\nSTART=0\nEND=1500\ntitle=Chương 1: A \\= B\\; C \\#1\n"
        ) in rendered
        assert (
            "[CHAPTER]\nTIMEBASE=1/1000\nSTART=1500\nEND=3200\ntitle=Chương 2: Kết thúc\n"
        ) in rendered

        # Blank optional fields must be omitted cleanly.
        minimal = build_ffmetadata(BookExportMetadata(title="Chỉ có tiêu đề"), chapters[:1])
        assert "title=Chỉ có tiêu đề\n" in minimal
        assert "album=Chỉ có tiêu đề\n" in minimal
        for omitted in ("artist=", "album_artist=", "composer=", "date=", "comment="):
            assert omitted not in minimal

        # Legacy author string fallback when authors tuple is empty.
        legacy = build_ffmetadata(
            BookExportMetadata(title="Cũ", author="Tác giả cũ"),
            chapters[:1],
        )
        assert "artist=Tác giả cũ\n" in legacy
        assert "album_artist=Tác giả cũ\n" in legacy

    def test_build_concat_list_escapes_quotes_backslashes_spaces_and_unicode(self) -> None:
        paths = [
            Path("/tmp/Sách nói/Chương 1.wav"),
            Path("C:\\Users\\Nguyễn O'Brien\\Sách hay\\ch_0001.wav"),
        ]
        result = build_concat_list(paths)
        assert result == (
            "file '/tmp/Sách nói/Chương 1.wav'\n"
            "file 'C:/Users/Nguyễn O'\\''Brien/Sách hay/ch_0001.wav'\n"
        )


# ── Task 4.2: MP4 container validation ───────────────────────────────────────


class TestValidateMp4Container:
    def test_accepts_valid_ftyp_moov_including_64bit_and_eof_boxes(self, tmp_path: Path) -> None:
        basic = tmp_path / "basic.m4b"
        basic.write_bytes(_minimal_mp4_bytes())
        validate_mp4_container(basic)

        # 64-bit largesize box (size32 == 1) + moov + to-EOF box (size32 == 0).
        ftyp = struct.pack(">I4s4sI", 16, b"ftyp", b"M4B ", 0)
        mdat_payload = b"\x00" * 24
        mdat_64 = struct.pack(">I4sQ", 1, b"mdat", 16 + len(mdat_payload)) + mdat_payload
        moov = struct.pack(">I4s", 12, b"moov") + b"\x00" * 4
        trailing_free = struct.pack(">I4s", 0, b"free") + b"\x00" * 10
        extended = tmp_path / "extended.m4b"
        extended.write_bytes(ftyp + mdat_64 + moov + trailing_free)
        validate_mp4_container(extended)

    def test_rejects_missing_empty_corrupt_truncated_and_invalid_sizes(
        self, tmp_path: Path
    ) -> None:
        with pytest.raises(AudiobookError):
            validate_mp4_container(tmp_path / "does_not_exist.m4b")

        empty = tmp_path / "empty.m4b"
        empty.write_bytes(b"")
        with pytest.raises(AudiobookError):
            validate_mp4_container(empty)

        # First box is not ftyp.
        no_ftyp = tmp_path / "no_ftyp.m4b"
        no_ftyp.write_bytes(struct.pack(">I4s", 8, b"moov"))
        with pytest.raises(AudiobookError, match="ftyp"):
            validate_mp4_container(no_ftyp)

        # Missing moov box.
        no_moov = tmp_path / "no_moov.m4b"
        no_moov.write_bytes(
            struct.pack(">I4s4sI", 16, b"ftyp", b"M4B ", 0)
            + struct.pack(">I4s", 12, b"mdat")
            + b"abcd"
        )
        with pytest.raises(AudiobookError, match="moov"):
            validate_mp4_container(no_moov)

        # Truncated box extending past EOF.
        truncated = tmp_path / "truncated.m4b"
        truncated.write_bytes(
            struct.pack(">I4s4sI", 16, b"ftyp", b"M4B ", 0) + struct.pack(">I4s", 200, b"moov")
        )
        with pytest.raises(AudiobookError):
            validate_mp4_container(truncated)

        # Invalid box size < 8 (size == 4).
        bad_size = tmp_path / "bad_size.m4b"
        bad_size.write_bytes(
            struct.pack(">I4s4sI", 16, b"ftyp", b"M4B ", 0) + struct.pack(">I4s", 4, b"moov")
        )
        with pytest.raises(AudiobookError):
            validate_mp4_container(bad_size)

        # Invalid 64-bit box with largesize < 16.
        bad_64 = tmp_path / "bad_64.m4b"
        bad_64.write_bytes(
            struct.pack(">I4s4sI", 16, b"ftyp", b"M4B ", 0)
            + struct.pack(">I4sQ", 1, b"mdat", 8)
            + struct.pack(">I4s", 8, b"moov")
        )
        with pytest.raises(AudiobookError):
            validate_mp4_container(bad_64)

        # Truncated 64-bit extended header (< 16 bytes remaining).
        short_64 = tmp_path / "short_64.m4b"
        short_64.write_bytes(
            struct.pack(">I4s4sI", 16, b"ftyp", b"M4B ", 0)
            + struct.pack(">I4s", 1, b"mdat")
            + b"\x00\x00"
        )
        with pytest.raises(AudiobookError):
            validate_mp4_container(short_64)


# ── Task 4.2: Stub ffmpeg M4B export tests ───────────────────────────────────


class TestExportM4bStub:
    def test_allowed_bitrates_and_validation(
        self, library: AudiobookLibrary, tmp_path: Path
    ) -> None:
        assert isinstance(ALLOWED_M4B_BITRATES, frozenset)
        assert set(ALLOWED_M4B_BITRATES) == {"64k", "96k", "128k"}
        record = library.add_book(_make_book())
        library.save_chapter_audio(record.id, 0, _make_tone(4_800))

        with pytest.raises(AudiobookError, match="bitrate"):
            export_m4b_audiobook(library, record.id, tmp_path / "out", bitrate="320k")

    def test_missing_ffmpeg_raises_actionable_error(
        self, library: AudiobookLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record = library.add_book(_make_book())
        library.save_chapter_audio(record.id, 0, _make_tone(4_800))

        def _missing(_data_dir: Path | None = None) -> Path:
            raise VideoDubError("ffmpeg not found")

        monkeypatch.setattr(m4b_export, "resolve_ffmpeg", _missing)
        with pytest.raises(AudiobookError) as exc_info:
            export_m4b_audiobook(library, record.id, tmp_path / "out")

        msg = str(exc_info.value)
        assert "ffmpeg" in msg.lower()
        assert "epub 3" in msg.lower()
        assert "mp3" in msg.lower()

    def test_zero_ready_chapters_raises_actionable_error(
        self, library: AudiobookLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record = library.add_book(_make_book())
        stub = _write_stub_ffmpeg(tmp_path / "stub_ffmpeg", tmp_path / "cap")
        monkeypatch.setattr(m4b_export, "resolve_ffmpeg", lambda _d=None: stub)
        monkeypatch.setattr(m4b_export, "resolve_ffprobe", lambda _d=None: None)

        with pytest.raises(AudiobookError):
            export_m4b_audiobook(library, record.id, tmp_path / "out")

    @pytest.mark.parametrize(
        ("cover_ext", "cover_bytes", "bitrate"),
        [
            ("jpg", _make_jpeg_bytes(), "64k"),
            ("png", _make_png_bytes(), "128k"),
        ],
        ids=["jpg-64k", "png-128k"],
    )
    def test_direct_cover_and_ffmpeg_argv(
        self,
        library: AudiobookLibrary,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        cover_ext: str,
        cover_bytes: bytes,
        bitrate: str,
    ) -> None:
        book = _make_book(
            title="Truyện Kiều",
            authors=("Nguyễn Du",),
            cover_bytes=cover_bytes,
            cover_ext=cover_ext,
        )
        record = library.add_book(book)
        library.save_chapter_audio(record.id, 0, _make_tone(24_000))
        library.save_chapter_audio(record.id, 1, _make_tone(48_000))

        capture = tmp_path / "cap"
        stub = _write_stub_ffmpeg(tmp_path / "stub_ffmpeg", capture)
        monkeypatch.setattr(m4b_export, "resolve_ffmpeg", lambda _d=None: stub)
        monkeypatch.setattr(m4b_export, "resolve_ffprobe", lambda _d=None: None)

        out_dir = tmp_path / "out"
        out_path = export_m4b_audiobook(
            library,
            record.id,
            out_dir,
            bitrate=bitrate,
            narrator="Giọng đọc Miền Bắc",
        )
        assert out_path == out_dir / "Truyện Kiều.m4b"
        assert out_path.is_file()
        # No temp files or work dirs left behind.
        assert [p.name for p in out_dir.iterdir()] == ["Truyện Kiều.m4b"]

        calls = _read_recorded_calls(capture)
        assert len(calls) == 1
        argv = calls[0]
        pairs = set(zip(argv, argv[1:], strict=False))
        assert ("-f", "concat") in pairs
        assert ("-safe", "0") in pairs
        assert ("-c:a", "aac") in pairs
        assert ("-b:a", bitrate) in pairs
        assert ("-ac", "1") in pairs
        assert ("-ar", "48000") in pairs
        assert ("-map", "0:a") in pairs
        assert ("-map_metadata", "1") in pairs
        assert ("-map_chapters", "1") in pairs
        assert ("-f", "mp4") in pairs
        assert ("-movflags", "+faststart") in pairs
        assert ("-map", "2:v") in pairs
        assert ("-c:v", "copy") in pairs
        assert ("-disposition:v:0", "attached_pic") in pairs

        ffmeta = (capture / "0" / "ffmetadata.txt").read_text(encoding="utf-8")
        assert "composer=Giọng đọc Miền Bắc\n" in ffmeta

    @pytest.mark.parametrize(
        ("cover_ext", "cover_bytes"),
        [
            ("webp", _make_webp_bytes()),
            ("gif", _make_gif_bytes()),
        ],
        ids=["webp", "gif"],
    )
    def test_webp_and_gif_cover_conversion_and_graceful_fallback(
        self,
        library: AudiobookLibrary,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        caplog: pytest.LogCaptureFixture,
        cover_ext: str,
        cover_bytes: bytes,
    ) -> None:
        book = _make_book(
            title=f"Sách bìa {cover_ext}",
            cover_bytes=cover_bytes,
            cover_ext=cover_ext,
            content_hash=("b" if cover_ext == "webp" else "c") * 64,
        )
        record = library.add_book(book)
        library.save_chapter_audio(record.id, 0, _make_tone(24_000))

        # 1) Conversion succeeds: 2 ffmpeg calls, second attaches cover.png.
        cap_ok = tmp_path / f"cap_ok_{cover_ext}"
        stub_ok = _write_stub_ffmpeg(tmp_path / f"ffmpeg_ok_{cover_ext}", cap_ok, cover_fail=False)
        monkeypatch.setattr(m4b_export, "resolve_ffmpeg", lambda _d=None: stub_ok)
        monkeypatch.setattr(m4b_export, "resolve_ffprobe", lambda _d=None: None)

        out_ok = export_m4b_audiobook(library, record.id, tmp_path / f"out_ok_{cover_ext}")
        assert out_ok.is_file()
        calls_ok = _read_recorded_calls(cap_ok)
        assert len(calls_ok) == 2
        assert "-frames:v" in calls_ok[0] and calls_ok[0][-1].endswith("cover.png")
        assert "2:v" in calls_ok[1]
        assert any(arg.endswith("cover.png") for arg in calls_ok[1])

        # 2) Conversion fails: logs warning and succeeds cleanly WITHOUT cover art.
        cap_fail = tmp_path / f"cap_fail_{cover_ext}"
        stub_fail = _write_stub_ffmpeg(
            tmp_path / f"ffmpeg_fail_{cover_ext}", cap_fail, cover_fail=True
        )
        monkeypatch.setattr(m4b_export, "resolve_ffmpeg", lambda _d=None: stub_fail)

        with caplog.at_level(logging.WARNING, logger="vienetts_app.core.m4b_export"):
            out_fail = export_m4b_audiobook(library, record.id, tmp_path / f"out_fail_{cover_ext}")
        assert out_fail.is_file()
        assert any("cover" in rec.message.lower() for rec in caplog.records)
        calls_fail = _read_recorded_calls(cap_fail)
        assert len(calls_fail) == 2
        assert "2:v" not in calls_fail[1]
        assert "attached_pic" not in calls_fail[1]

    def test_progress_relay_monotonic_capped_at_95_until_promote(
        self, library: AudiobookLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record = library.add_book(_make_book())
        # 2 chapters of 1.0s each -> total_duration_ms = 2000
        library.save_chapter_audio(record.id, 0, _make_tone(SAMPLE_RATE))
        library.save_chapter_audio(record.id, 1, _make_tone(SAMPLE_RATE))

        stub = _write_stub_ffmpeg(
            tmp_path / "stub_ffmpeg",
            tmp_path / "cap",
            emit_progress=[
                "out_time_us=N/A",
                "out_time_ms=N/A",
                "out_time_us=500000",  # 0.25
                "out_time_ms=500000",  # duplicate -> ignored
                "out_time_us=400000",  # regression -> ignored
                "out_time_ms=1500000",  # 0.75
                "out_time_us=2500000",  # >1.0 -> capped at 0.95
                "progress=end",  # stays capped at 0.95
            ],
        )
        monkeypatch.setattr(m4b_export, "resolve_ffmpeg", lambda _d=None: stub)
        monkeypatch.setattr(m4b_export, "resolve_ffprobe", lambda _d=None: None)

        updates: list[float] = []
        out_path = export_m4b_audiobook(
            library, record.id, tmp_path / "out", progress=updates.append
        )
        assert out_path.is_file()
        assert updates == [pytest.approx(0.25), pytest.approx(0.75), pytest.approx(0.95), 1.0]

    def test_cancel_kills_stub_and_leaves_no_temp_or_output_files(
        self, library: AudiobookLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record = library.add_book(_make_book())
        library.save_chapter_audio(record.id, 0, _make_tone(24_000))

        pid_file = tmp_path / "stub.pid"
        stub = _write_stub_ffmpeg(
            tmp_path / "stub_ffmpeg",
            tmp_path / "cap",
            sleep_s=30.0,
            pid_file=pid_file,
        )
        monkeypatch.setattr(m4b_export, "resolve_ffmpeg", lambda _d=None: stub)
        monkeypatch.setattr(m4b_export, "resolve_ffprobe", lambda _d=None: None)

        cancelled = threading.Event()

        def _trigger_cancel() -> None:
            deadline = time.monotonic() + 5.0
            while not pid_file.is_file() and time.monotonic() < deadline:
                time.sleep(0.02)
            cancelled.set()

        trigger = threading.Thread(target=_trigger_cancel, daemon=True)
        trigger.start()

        out_dir = tmp_path / "out"
        started = time.monotonic()
        with pytest.raises(ExportCancelled):
            export_m4b_audiobook(library, record.id, out_dir, cancel_check=cancelled.is_set)
        elapsed = time.monotonic() - started
        trigger.join(timeout=2.0)

        assert elapsed < 5.0
        assert pid_file.is_file()
        child_pid = int(pid_file.read_text(encoding="utf-8").strip())
        assert not process_alive(child_pid)
        assert list(out_dir.iterdir()) == []

    def test_ffprobe_audio_stream_verification(
        self, library: AudiobookLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record = library.add_book(_make_book())
        library.save_chapter_audio(record.id, 0, _make_tone(24_000))
        stub_ff = _write_stub_ffmpeg(tmp_path / "stub_ffmpeg", tmp_path / "cap")
        monkeypatch.setattr(m4b_export, "resolve_ffmpeg", lambda _d=None: stub_ff)

        # Valid audio stream reported by ffprobe -> succeeds.
        probe_ok = _write_stub_ffprobe(tmp_path / "ffprobe_ok", stdout="audio\n", returncode=0)
        monkeypatch.setattr(m4b_export, "resolve_ffprobe", lambda _d=None: probe_ok)
        out_ok = export_m4b_audiobook(library, record.id, tmp_path / "out_ok")
        assert out_ok.is_file()

        # Empty ffprobe output -> raises AudiobookError and cleans up.
        probe_empty = _write_stub_ffprobe(tmp_path / "ffprobe_empty", stdout="", returncode=0)
        monkeypatch.setattr(m4b_export, "resolve_ffprobe", lambda _d=None: probe_empty)
        out_bad_dir = tmp_path / "out_bad"
        with pytest.raises(AudiobookError, match="audio stream"):
            export_m4b_audiobook(library, record.id, out_bad_dir)
        assert list(out_bad_dir.iterdir()) == []

    def test_ffmpeg_failure_surfaces_stderr_and_cleans_up(
        self, library: AudiobookLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record = library.add_book(_make_book())
        library.save_chapter_audio(record.id, 0, _make_tone(24_000))
        stub_ff = _write_stub_ffmpeg(tmp_path / "stub_ffmpeg", tmp_path / "cap", main_fail=True)
        monkeypatch.setattr(m4b_export, "resolve_ffmpeg", lambda _d=None: stub_ff)
        monkeypatch.setattr(m4b_export, "resolve_ffprobe", lambda _d=None: None)

        out_dir = tmp_path / "out"
        with pytest.raises(AudiobookError, match="simulated encoder crash"):
            export_m4b_audiobook(library, record.id, out_dir)
        assert list(out_dir.iterdir()) == []

    def test_atomic_promote_retries_windows_lock_and_surfaces_persistent_lock(
        self, library: AudiobookLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record = library.add_book(_make_book())
        library.save_chapter_audio(record.id, 0, _make_tone(24_000))
        stub_ff = _write_stub_ffmpeg(tmp_path / "stub_ffmpeg", tmp_path / "cap")
        monkeypatch.setattr(m4b_export, "resolve_ffmpeg", lambda _d=None: stub_ff)
        monkeypatch.setattr(m4b_export, "resolve_ffprobe", lambda _d=None: None)
        monkeypatch.setattr(m4b_export, "REPLACE_LOCK_DELAY_S", 0.0)

        real_replace = os.replace
        state = {"failures": 0}

        def _flaky_replace(src: Any, dst: Any) -> None:
            if state["failures"] < 2:
                state["failures"] += 1
                raise PermissionError(32, "locked")
            real_replace(src, dst)

        monkeypatch.setattr(m4b_export.os, "replace", _flaky_replace)

        out_path = export_m4b_audiobook(library, record.id, tmp_path / "out_retry")
        assert out_path.is_file()
        assert state["failures"] == 2

        locked_calls = {"n": 0}

        def _always_locked(_src: Any, _dst: Any) -> None:
            locked_calls["n"] += 1
            raise PermissionError(32, "locked")

        monkeypatch.setattr(m4b_export.os, "replace", _always_locked)
        out_locked = tmp_path / "out_locked"
        with pytest.raises(AudiobookError, match="locked"):
            export_m4b_audiobook(library, record.id, out_locked)
        assert locked_calls["n"] == m4b_export.REPLACE_LOCK_ATTEMPTS
        assert list(out_locked.iterdir()) == []

    def test_early_cancel_raises_before_invoking_ffmpeg(
        self, library: AudiobookLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        record = library.add_book(_make_book())
        library.save_chapter_audio(record.id, 0, _make_tone(24_000))

        def _should_not_resolve(_d: Path | None = None) -> Path:
            raise AssertionError("resolve_ffmpeg must not be called when already cancelled")

        monkeypatch.setattr(m4b_export, "resolve_ffmpeg", _should_not_resolve)
        with pytest.raises(ExportCancelled):
            export_m4b_audiobook(library, record.id, tmp_path / "out", cancel_check=lambda: True)

    def test_cjk_title_uses_short_uuid_temp_and_rejects_empty_dest_dir(
        self, library: AudiobookLibrary, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        cjk_title = "漢" * 80
        book = _make_book(title=cjk_title, content_hash="9" * 64)
        record = library.add_book(book)
        library.save_chapter_audio(record.id, 0, _make_tone(24_000))

        capture = tmp_path / "cap"
        stub = _write_stub_ffmpeg(tmp_path / "stub_ffmpeg", capture)
        monkeypatch.setattr(m4b_export, "resolve_ffmpeg", lambda _d=None: stub)
        monkeypatch.setattr(m4b_export, "resolve_ffprobe", lambda _d=None: None)

        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        for bad_dest in ("", "   ", ".", Path(""), Path("."), '""', "file://"):
            with pytest.raises(AudiobookError, match="(?i)destination|empty"):
                export_m4b_audiobook(library, record.id, bad_dest)
        assert list(cwd.iterdir()) == []

        out_dir = tmp_path / "cjk_m4b_out"
        out_path = export_m4b_audiobook(library, record.id, out_dir)
        assert out_path.is_file()
        calls = _read_recorded_calls(capture)
        assert len(calls) == 1
        part_name = Path(calls[0][-1]).name
        assert part_name.endswith(".part.m4b")
        assert len(part_name.encode("utf-8")) < 64
        assert "漢" not in part_name


# ── Real-ffmpeg integration test ─────────────────────────────────────────────


def _has_real_ffmpeg() -> bool:
    try:
        resolve_ffmpeg()
    except VideoDubError:
        return False
    return True


@pytest.mark.skipif(not _has_real_ffmpeg(), reason="ffmpeg not installed")
def test_real_ffmpeg_m4b_export_multipart_vietnamese_and_cover(
    library: AudiobookLibrary, tmp_path: Path
) -> None:
    book = _make_book(
        title="Dế Mèn Phiêu Lưu Ký",
        authors=("Tô Hoài", "Minh Họa"),
        year=1941,
        description="Tác phẩm kinh điển = tuyệt vời; #1",
        cover_bytes=_make_png_bytes(16, 16),
        cover_ext="png",
        chapters=[
            EpubChapter(index=0, title="Chương 1: Bài học đầu đời = A; #1", text="Nội dung 1."),
            EpubChapter(index=1, title="Chương 2: Trường đoạn nhiều phần", text="Nội dung 2."),
            EpubChapter(index=2, title="Chương 3: Chưa tổng hợp", text="Nội dung 3."),
        ],
        content_hash="d" * 64,
    )
    record = library.add_book(book)
    # Chapter 0: 0.4 s single WAV
    library.save_chapter_audio(record.id, 0, _make_tone(int(SAMPLE_RATE * 0.4), freq=330.0))
    # Chapter 1: 2-part oversize chapter (0.3 s + 0.3 s = 0.6 s total)
    _save_multipart_chapter(
        library,
        record.id,
        1,
        [int(SAMPLE_RATE * 0.3), int(SAMPLE_RATE * 0.3)],
    )
    # Chapter 2 intentionally left pending (partial book export).

    progress_values: list[float] = []
    out_dir = tmp_path / "m4b_out"
    out_path = export_m4b_audiobook(
        library,
        record.id,
        out_dir,
        bitrate="96k",
        narrator="Giọng đọc Bình",
        progress=progress_values.append,
    )

    assert out_path == out_dir / "Dế Mèn Phiêu Lưu Ký.m4b"
    assert out_path.is_file()
    assert [p.name for p in out_dir.iterdir()] == ["Dế Mèn Phiêu Lưu Ký.m4b"]

    # Binary container checks: starts with ftyp, contains moov.
    header = out_path.read_bytes()[:12]
    assert header[4:8] == b"ftyp"
    validate_mp4_container(out_path)

    # Progress invariants.
    assert progress_values
    assert progress_values[-1] == 1.0
    assert all(0.0 < v <= 0.95 for v in progress_values[:-1])
    assert progress_values == sorted(progress_values)

    ffprobe = resolve_ffprobe()
    if ffprobe is None:
        pytest.skip("ffprobe not installed")

    probe = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_format",
            "-show_streams",
            "-show_chapters",
            "-of",
            "json",
            str(out_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    info = json.loads(probe.stdout)

    audio_streams = [s for s in info["streams"] if s.get("codec_type") == "audio"]
    assert len(audio_streams) == 1
    assert audio_streams[0]["codec_name"] == "aac"
    assert int(audio_streams[0]["channels"]) == 1
    assert int(audio_streams[0]["sample_rate"]) == 48_000

    cover_streams = [
        s
        for s in info["streams"]
        if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") == 1
    ]
    assert len(cover_streams) == 1

    chapters = info["chapters"]
    assert len(chapters) == 2
    assert chapters[0]["tags"]["title"] == "Chương 1: Bài học đầu đời = A; #1"
    assert chapters[1]["tags"]["title"] == "Chương 2: Trường đoạn nhiều phần"
    assert float(chapters[0]["start_time"]) == pytest.approx(0.0, abs=0.05)
    assert float(chapters[0]["end_time"]) == pytest.approx(0.4, abs=0.05)
    assert float(chapters[1]["start_time"]) == pytest.approx(0.4, abs=0.05)
    assert float(chapters[1]["end_time"]) == pytest.approx(1.0, abs=0.08)

    tags = info["format"]["tags"]
    assert tags["title"] == "Dế Mèn Phiêu Lưu Ký"
    assert tags["album"] == "Dế Mèn Phiêu Lưu Ký"
    assert tags["artist"] == "Tô Hoài; Minh Họa"
    assert tags["album_artist"] == "Tô Hoài; Minh Họa"
    assert tags["composer"] == "Giọng đọc Bình"
    assert tags["date"] == "1941"
    assert tags["comment"] == "Tác phẩm kinh điển = tuyệt vời; #1"


@pytest.mark.skipif(not _has_real_ffmpeg(), reason="ffmpeg not installed")
def test_real_ffmpeg_m4b_converts_gif_cover_to_attached_pic(
    library: AudiobookLibrary, tmp_path: Path
) -> None:
    book = _make_book(
        title="Sách bìa GIF",
        cover_bytes=_make_gif_bytes(),
        cover_ext="gif",
        content_hash="e" * 64,
    )
    record = library.add_book(book)
    library.save_chapter_audio(record.id, 0, _make_tone(int(SAMPLE_RATE * 0.25)))

    out_path = export_m4b_audiobook(library, record.id, tmp_path / "gif_out")
    validate_mp4_container(out_path)

    ffprobe = resolve_ffprobe()
    if ffprobe is None:
        pytest.skip("ffprobe not installed")
    probe = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_streams",
            "-of",
            "json",
            str(out_path),
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=True,
    )
    info = json.loads(probe.stdout)
    cover_streams = [
        s
        for s in info["streams"]
        if s.get("codec_type") == "video" and s.get("disposition", {}).get("attached_pic") == 1
    ]
    assert len(cover_streams) == 1
    assert cover_streams[0]["codec_name"] == "png"
