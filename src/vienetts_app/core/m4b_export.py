"""Single-file M4B audiobook exporter (BooksReader Tier T1).

Builds a mono AAC-LC ``.m4b`` container (``-f mp4 -movflags +faststart``) with
``FFMETADATA1`` book tags and chapter markers, optional attached cover art
(``covr``), sample-accurate WAV-header chapter timing, cooperative cancellation,
pure-Python MP4 box validation, and atomic promotion.

Zero third-party dependencies beyond existing ``soundfile`` (header-only
duration probing) and VieNeuTTSApp's managed/PATH ``ffmpeg`` resolver.
"""

from __future__ import annotations

import contextlib
import logging
import os
import shutil
import struct
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path
from typing import Any

from vienetts_app.core.ai_models import cli_command
from vienetts_app.core.audio import ExportCancelled
from vienetts_app.core.audiobook import (
    REPLACE_LOCK_ATTEMPTS,
    REPLACE_LOCK_DELAY_S,
    AudiobookError,
    AudiobookLibrary,
    BookExportMetadata,
    BookState,
)
from vienetts_app.core.epub import MAX_COVER_BYTES, sniff_image_ext
from vienetts_app.core.paths import is_empty_path, normalize_local_path, sanitize_filename
from vienetts_app.core.processes import FFPROBE_TIMEOUT_S, CliCancelled, CliTimeout, run_cli
from vienetts_app.core.video_dub import (
    _MUX_PROGRESS_CAP,
    VideoDubError,
    resolve_ffmpeg,
    resolve_ffprobe,
)

logger = logging.getLogger(__name__)

_ORDERED_M4B_BITRATES: tuple[str, ...] = ("64k", "96k", "128k")
ALLOWED_M4B_BITRATES: frozenset[str] = frozenset(_ORDERED_M4B_BITRATES)

_COVER_CONVERT_TIMEOUT_S = 60.0
_MP4_HEADER_BYTES = 8
_MP4_EXTENDED_HEADER_BYTES = 16

__all__ = [
    "ALLOWED_M4B_BITRATES",
    "M4bChapter",
    "build_concat_list",
    "build_ffmetadata",
    "escape_ffmetadata_value",
    "export_m4b_audiobook",
    "measure_m4b_chapters",
    "probe_wav_duration_frames",
    "validate_mp4_container",
]


@dataclass(frozen=True)
class M4bChapter:
    """One logical audiobook chapter measured for M4B export."""

    index: int
    title: str
    start_ms: int
    end_ms: int
    audio_paths: tuple[Path, ...]


def _sf() -> Any:
    """Deferred ``soundfile`` import (keeps startup path light)."""
    import soundfile as sf

    return sf


def escape_ffmetadata_value(value: str) -> str:
    """Escape a metadata value for FFmpeg ``FFMETADATA1`` files.

    Escapes ``\\``, ``=``, ``;``, ``#``, and newlines (``\\r\\n``, ``\\r``, ``\\n``)
    while preserving Vietnamese Unicode verbatim.
    """
    text = str(value).replace("\r\n", "\n").replace("\r", "\n")
    return (
        text.replace("\\", "\\\\")
        .replace("=", "\\=")
        .replace(";", "\\;")
        .replace("#", "\\#")
        .replace("\n", "\\\n")
    )


def probe_wav_duration_frames(path: Path) -> tuple[int, int]:
    """Read ``(frames, samplerate)`` from WAV header only (no sample decode)."""
    wav_path = Path(path)
    if not wav_path.is_file():
        raise AudiobookError(f"Audio file not found: {wav_path}")
    try:
        info = _sf().info(str(wav_path))
    except Exception as exc:
        raise AudiobookError(f"Could not read WAV header for '{wav_path.name}': {exc}") from exc
    frames = int(getattr(info, "frames", 0) or 0)
    samplerate = int(getattr(info, "samplerate", 0) or 0)
    if frames <= 0 or samplerate <= 0:
        raise AudiobookError(
            f"Audio file '{wav_path.name}' has invalid duration metadata "
            f"(frames={frames}, samplerate={samplerate})."
        )
    return frames, samplerate


def measure_m4b_chapters(
    library: AudiobookLibrary,
    book_id: str,
    ready_indices: Sequence[int],
    *,
    book: BookState | None = None,
) -> tuple[list[M4bChapter], int]:
    """Measure exact cumulative chapter timestamps from WAV headers.

    Multi-part oversize chapters sum all of their ordered WAV part durations
    into a single :class:`M4bChapter` marker using exact rational arithmetic
    so cumulative timestamps never drift across parts or chapters.
    """
    indices = list(ready_indices)
    if not indices:
        return [], 0

    state = library.load_book(book_id) if book is None else book
    chapter_state = library.read_state(book_id)
    chapters_by_index = {chapter.index: chapter for chapter in state.chapters}

    measured: list[tuple[int, str, Fraction, tuple[Path, ...]]] = []
    cursor_s = Fraction(0)
    for ch_idx in indices:
        chapter = chapters_by_index.get(ch_idx)
        if chapter is None:
            raise AudiobookError(
                f"chapter index {ch_idx} out of range (0..{len(state.chapters) - 1})"
            )
        paths = library.chapter_audio_paths(book_id, chapter.index, state=chapter_state)
        existing = [p for p in paths if p.is_file()]
        if not existing:
            raise AudiobookError(
                f"Chapter {chapter.index + 1} ('{chapter.title}') has not been rendered yet — "
                "render it first, then export."
            )
        if len(existing) != len(paths):
            raise AudiobookError(
                f"Chapter {chapter.index + 1} ('{chapter.title}') is only partially rendered — "
                "render every part, then export."
            )
        chapter_dur_s = Fraction(0)
        for wav_path in paths:
            frames, sr = probe_wav_duration_frames(wav_path)
            chapter_dur_s += Fraction(frames, sr)
        cursor_s += chapter_dur_s
        measured.append((chapter.index, chapter.title, cursor_s, tuple(paths)))

    total_duration_ms = max(len(measured), int(round(cursor_s * 1000)))
    result: list[M4bChapter] = []
    prev_end_ms = 0
    count = len(measured)
    for pos, (ch_index, ch_title, end_s, audio_paths) in enumerate(measured):
        start_ms = prev_end_ms
        remaining = count - 1 - pos
        if remaining == 0:
            end_ms = total_duration_ms
        else:
            raw_end_ms = int(round(end_s * 1000))
            end_ms = max(start_ms + 1, min(raw_end_ms, total_duration_ms - remaining))
        result.append(
            M4bChapter(
                index=ch_index,
                title=ch_title,
                start_ms=start_ms,
                end_ms=end_ms,
                audio_paths=audio_paths,
            )
        )
        prev_end_ms = end_ms
    return result, total_duration_ms


def build_ffmetadata(
    meta: BookExportMetadata,
    chapters: Sequence[M4bChapter],
    *,
    narrator: str = "",
) -> str:
    """Render an FFmpeg ``FFMETADATA1`` file with book tags and chapter blocks."""
    lines: list[str] = [";FFMETADATA1"]

    title = (meta.title or "").strip()
    if title:
        escaped_title = escape_ffmetadata_value(title)
        lines.append(f"title={escaped_title}")
        lines.append(f"album={escaped_title}")

    artist = "; ".join(cleaned for item in meta.authors if (cleaned := str(item or "").strip()))
    if not artist:
        artist = (meta.author or "").strip()
    if artist:
        escaped_artist = escape_ffmetadata_value(artist)
        lines.append(f"artist={escaped_artist}")
        lines.append(f"album_artist={escaped_artist}")

    composer = (narrator or "").strip()
    if composer:
        lines.append(f"composer={escape_ffmetadata_value(composer)}")

    if meta.year is not None:
        lines.append(f"date={escape_ffmetadata_value(str(meta.year))}")

    comment = (meta.description or "").strip()
    if comment:
        lines.append(f"comment={escape_ffmetadata_value(comment)}")

    for chapter in chapters:
        lines.append("[CHAPTER]")
        lines.append("TIMEBASE=1/1000")
        lines.append(f"START={int(chapter.start_ms)}")
        lines.append(f"END={int(chapter.end_ms)}")
        lines.append(f"title={escape_ffmetadata_value(chapter.title)}")

    return "\n".join(lines) + "\n"


def build_concat_list(paths: Sequence[Path]) -> str:
    """Build an FFmpeg concat demuxer list file referencing ``paths`` in order."""
    lines: list[str] = []
    for path in paths:
        normalized = str(path).replace("\\", "/")
        escaped = normalized.replace("'", "'\\''")
        lines.append(f"file '{escaped}'\n")
    return "".join(lines)


def validate_mp4_container(path: Path) -> None:
    """Streaming ISO BMFF top-level box check: requires leading ``ftyp`` and ``moov``."""
    mp4_path = Path(path)
    if not mp4_path.is_file():
        raise AudiobookError(f"Encoded M4B output file does not exist: {mp4_path}")
    try:
        file_size = mp4_path.stat().st_size
    except OSError as exc:
        raise AudiobookError(f"Could not stat encoded M4B '{mp4_path.name}': {exc}") from exc
    if file_size < _MP4_HEADER_BYTES:
        raise AudiobookError(
            f"Encoded M4B '{mp4_path.name}' is empty or truncated ({file_size} bytes)."
        )

    pos = 0
    box_index = 0
    found_moov = False
    try:
        with mp4_path.open("rb") as fh:
            while pos < file_size:
                remaining = file_size - pos
                if remaining < _MP4_HEADER_BYTES:
                    raise AudiobookError(
                        f"Truncated MP4 box header in '{mp4_path.name}' at byte offset {pos}."
                    )
                fh.seek(pos)
                header = fh.read(_MP4_HEADER_BYTES)
                if len(header) < _MP4_HEADER_BYTES:
                    raise AudiobookError(
                        f"Truncated MP4 box header in '{mp4_path.name}' at byte offset {pos}."
                    )
                size32, box_type = struct.unpack(">I4s", header)
                if size32 == 0:
                    box_size = remaining
                elif size32 == 1:
                    if remaining < _MP4_EXTENDED_HEADER_BYTES:
                        raise AudiobookError(
                            f"Truncated 64-bit MP4 box header in '{mp4_path.name}' "
                            f"at byte offset {pos}."
                        )
                    ext_header = fh.read(8)
                    if len(ext_header) < 8:
                        raise AudiobookError(
                            f"Truncated 64-bit MP4 box header in '{mp4_path.name}' "
                            f"at byte offset {pos}."
                        )
                    box_size = struct.unpack(">Q", ext_header)[0]
                    if box_size < _MP4_EXTENDED_HEADER_BYTES:
                        raise AudiobookError(
                            f"Invalid 64-bit MP4 box size ({box_size}) in '{mp4_path.name}' "
                            f"at byte offset {pos}."
                        )
                elif size32 < _MP4_HEADER_BYTES:
                    raise AudiobookError(
                        f"Invalid MP4 box size ({size32}) in '{mp4_path.name}' "
                        f"at byte offset {pos}."
                    )
                else:
                    box_size = size32

                if box_size <= 0 or pos + box_size > file_size:
                    raise AudiobookError(
                        f"Truncated MP4 box {box_type!r} in '{mp4_path.name}' "
                        f"(offset {pos}, size {box_size}, file size {file_size})."
                    )
                if box_index == 0 and box_type != b"ftyp":
                    raise AudiobookError(
                        f"Encoded M4B '{mp4_path.name}' must start with an 'ftyp' box, "
                        f"found {box_type!r}."
                    )
                if box_type == b"moov":
                    found_moov = True

                pos += box_size
                box_index += 1
    except AudiobookError:
        raise
    except OSError as exc:
        raise AudiobookError(f"Could not validate MP4 container '{mp4_path.name}': {exc}") from exc

    if not found_moov:
        raise AudiobookError(f"Encoded M4B '{mp4_path.name}' is missing a top-level 'moov' box.")


def _check_cancelled(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise ExportCancelled("M4B export cancelled.")


def _prepare_cover_path(
    meta: BookExportMetadata,
    work_dir: Path,
    ffmpeg: Path,
    cancel_check: Callable[[], bool] | None,
) -> Path | None:
    cover_bytes = meta.cover_bytes
    if not cover_bytes or len(cover_bytes) > MAX_COVER_BYTES:
        return None
    ext = (meta.cover_ext or "").strip().lower().lstrip(".") or sniff_image_ext(cover_bytes)
    if ext == "jpeg":
        ext = "jpg"
    if ext in ("jpg", "png"):
        cover_path = work_dir / f"cover.{ext}"
        cover_path.write_bytes(cover_bytes)
        return cover_path
    if ext not in ("webp", "gif"):
        return None

    _check_cancelled(cancel_check)
    src_cover = work_dir / f"cover.{ext}"
    src_cover.write_bytes(cover_bytes)
    png_cover = work_dir / "cover.png"
    try:
        conv = run_cli(
            cli_command(
                ffmpeg,
                "-nostdin",
                "-y",
                "-i",
                str(src_cover),
                "-frames:v",
                "1",
                str(png_cover),
            ),
            timeout=_COVER_CONVERT_TIMEOUT_S,
            cancel_check=cancel_check,
        )
    except CliCancelled as exc:
        raise ExportCancelled("M4B export cancelled.") from exc
    except (CliTimeout, OSError) as exc:
        logger.warning(
            "Failed to convert %s cover to PNG for M4B export (%s); continuing without cover.",
            ext,
            exc,
        )
        return None

    _check_cancelled(cancel_check)
    if conv.returncode == 0 and png_cover.is_file() and png_cover.stat().st_size > 0:
        return png_cover

    stderr_lines = [line.strip() for line in (conv.stderr or "").splitlines() if line.strip()]
    detail = stderr_lines[-1] if stderr_lines else f"exit code {conv.returncode}"
    logger.warning(
        "Failed to convert %s cover to PNG for M4B export (%s); continuing without cover.",
        ext,
        detail,
    )
    return None


def _promote_atomic(part: Path, target: Path) -> Path:
    for attempt in range(REPLACE_LOCK_ATTEMPTS):
        try:
            os.replace(part, target)
            return target
        except PermissionError:
            if attempt == REPLACE_LOCK_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_LOCK_DELAY_S)
    return target


def _unlink_with_retry(path: Path) -> None:
    for attempt in range(REPLACE_LOCK_ATTEMPTS):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            if attempt == REPLACE_LOCK_ATTEMPTS - 1:
                return
            time.sleep(REPLACE_LOCK_DELAY_S)
        except OSError:
            return


def _verify_audio_stream_ffprobe(
    part_path: Path,
    *,
    data_dir: Path | None,
    cancel_check: Callable[[], bool] | None,
) -> None:
    ffprobe = resolve_ffprobe(data_dir)
    if ffprobe is None:
        return
    try:
        res = run_cli(
            cli_command(
                ffprobe,
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=codec_type",
                "-of",
                "csv=p=0",
                str(part_path),
            ),
            timeout=FFPROBE_TIMEOUT_S,
            cancel_check=cancel_check,
        )
    except CliCancelled as exc:
        raise ExportCancelled("M4B export cancelled.") from exc
    except (CliTimeout, OSError) as exc:
        raise AudiobookError(f"Failed to verify encoded M4B audio stream: {exc}") from exc

    _check_cancelled(cancel_check)
    if res.returncode != 0 or "audio" not in (res.stdout or "").lower():
        raise AudiobookError("Encoded M4B has no valid audio stream.")


def export_m4b_audiobook(
    library: AudiobookLibrary,
    book_id: str,
    dest_dir: str | Path,
    *,
    ready_indices: Sequence[int] | None = None,
    bitrate: str = "96k",
    narrator: str = "",
    data_dir: Path | None = None,
    book: BookState | None = None,
    progress: Callable[[float], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> Path:
    """Export rendered chapters of ``book_id`` into a single ``.m4b`` audiobook."""
    if not isinstance(bitrate, str) or bitrate not in ALLOWED_M4B_BITRATES:
        raise AudiobookError(
            f"Unsupported M4B bitrate {bitrate!r}; choose one of "
            f"{', '.join(_ORDERED_M4B_BITRATES)}."
        )
    _check_cancelled(cancel_check)

    try:
        ffmpeg = resolve_ffmpeg(data_dir)
    except VideoDubError as exc:
        raise AudiobookError(
            "ffmpeg is required to export M4B audiobooks. Install ffmpeg via "
            "Settings › Models or add it to your system PATH, or export as EPUB 3 or MP3 instead."
        ) from exc

    state = library.load_book(book_id) if book is None else book
    if ready_indices is None:
        chapter_state = library.read_state(book_id)
        selected_indices = [
            ch.index
            for ch in state.chapters
            if library.has_chapter_audio(book_id, ch.index, state=chapter_state)
        ]
    else:
        selected_indices = list(ready_indices)

    if not selected_indices:
        raise AudiobookError(
            "No rendered chapters are ready to export to M4B — render at least one chapter first."
        )

    chapters, total_duration_ms = measure_m4b_chapters(
        library, book_id, selected_indices, book=state
    )
    meta = library.resolve_book_export_metadata(book_id, book=state)
    _check_cancelled(cancel_check)

    if is_empty_path(dest_dir):
        raise AudiobookError("Destination folder for M4B export must not be empty.")
    dest = normalize_local_path(dest_dir)
    if is_empty_path(dest):
        raise AudiobookError("Destination folder for M4B export must not be empty.")
    dest.mkdir(parents=True, exist_ok=True)

    book_stem = sanitize_filename(meta.title, max_len=80, fallback=f"audiobook-{book_id}")
    target_path = dest / f"{book_stem}.m4b"
    work_dir = dest / f".{uuid.uuid4().hex}.m4bwork"
    part_path = dest / f".{uuid.uuid4().hex}.part.m4b"
    promoted = False

    try:
        work_dir.mkdir(parents=True, exist_ok=True)
        concat_path = work_dir / "concat.txt"
        metadata_path = work_dir / "ffmetadata.txt"

        all_audio_paths = [p.absolute() for ch in chapters for p in ch.audio_paths]
        concat_path.write_text(build_concat_list(all_audio_paths), encoding="utf-8")
        metadata_path.write_text(
            build_ffmetadata(meta, chapters, narrator=narrator), encoding="utf-8"
        )

        cover_path = _prepare_cover_path(meta, work_dir, ffmpeg, cancel_check)
        _check_cancelled(cancel_check)

        args: list[str] = [
            "-nostdin",
            "-y",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(concat_path),
            "-i",
            str(metadata_path),
        ]
        if cover_path is not None:
            args += ["-i", str(cover_path)]
        args += [
            "-map",
            "0:a",
            "-map_metadata",
            "1",
            "-map_chapters",
            "1",
        ]
        if cover_path is not None:
            args += [
                "-map",
                "2:v",
                "-c:v",
                "copy",
                "-disposition:v:0",
                "attached_pic",
            ]
        args += [
            "-c:a",
            "aac",
            "-b:a",
            bitrate,
            "-ac",
            "1",
            "-ar",
            "48000",
            "-f",
            "mp4",
            "-movflags",
            "+faststart",
        ]
        if progress is not None:
            args += ["-progress", "pipe:1", "-nostats"]
        args.append(str(part_path))

        reported = 0.0

        def _report(fraction: float) -> None:
            nonlocal reported
            value = min(_MUX_PROGRESS_CAP, float(fraction))
            if progress is not None and value > reported:
                reported = value
                progress(value)

        def _on_progress_line(line: str) -> None:
            key, _, value = line.strip().partition("=")
            if key in ("out_time_us", "out_time_ms"):
                if total_duration_ms > 0:
                    with contextlib.suppress(ValueError):
                        _report(int(value) / (total_duration_ms * 1000.0))
            elif key == "progress" and value == "end":
                _report(_MUX_PROGRESS_CAP)

        try:
            res = run_cli(
                cli_command(ffmpeg, *args),
                cancel_check=cancel_check,
                on_stdout_line=_on_progress_line if progress is not None else None,
            )
        except CliCancelled as exc:
            raise ExportCancelled("M4B export cancelled.") from exc
        except OSError as exc:
            raise AudiobookError(f"Failed to run ffmpeg for M4B export: {exc}") from exc

        _check_cancelled(cancel_check)
        if res.returncode != 0 or not part_path.is_file() or part_path.stat().st_size == 0:
            stderr_lines = [
                line.strip() for line in (res.stderr or "").splitlines() if line.strip()
            ]
            detail = (
                stderr_lines[-1] if stderr_lines else f"ffmpeg exited with code {res.returncode}"
            )
            raise AudiobookError(f"Failed to encode M4B audiobook: {detail}")

        validate_mp4_container(part_path)
        _check_cancelled(cancel_check)
        _verify_audio_stream_ffprobe(part_path, data_dir=data_dir, cancel_check=cancel_check)
        _check_cancelled(cancel_check)

        try:
            out_path = _promote_atomic(part_path, target_path)
        except PermissionError as exc:
            raise AudiobookError(
                f"Could not save M4B audiobook (file locked by another program): {exc}"
            ) from exc
        except OSError as exc:
            raise AudiobookError(f"Could not save M4B audiobook: {exc}") from exc
        promoted = True

        if progress is not None:
            with contextlib.suppress(Exception):
                progress(1.0)
        return out_path
    except BaseException:
        if not promoted:
            _unlink_with_retry(part_path)
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)
