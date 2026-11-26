"""Narrated EPUB 3 with Media Overlays exporter (FR-3.1..FR-3.4, BooksReader Tier T0).

Builds a self-contained, strict-XML EPUB 3.0 package with:

- Entry 0 uncompressed ``mimetype`` (``application/epub+zip``) and zero ``<!DOCTYPE``
  declarations across every XML/XHTML/SMIL document.
- Compact SMIL 3.0 Media Overlays backed by streamed libsndfile MP3 audio
  (``ZIP_STORED``, zero ``ffmpeg``, constant RAM).
- Adaptive clip normalization and two-stage coalescing (paragraph boundaries
  first, then uniform stride) enforcing BooksReader's hard ``<par>`` (4,390)
  and cumulative XML byte (781,100) caps with safe headroom (4,000 / 700,000).
- Partial-render support: every chapter in the book is included as readable
  XHTML in the spine and TOC, with Media Overlays attached only to ready chapters.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import math
import os
import re
import shutil
import time
import uuid
import zipfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape

from vienetts_app.core.audio import ExportCancelled, export_mp3_parts
from vienetts_app.core.audiobook import (
    REPLACE_LOCK_ATTEMPTS,
    REPLACE_LOCK_DELAY_S,
    AudiobookError,
    AudiobookLibrary,
    BookExportMetadata,
    BookState,
)
from vienetts_app.core.epub import MAX_COVER_BYTES, sniff_image_ext
from vienetts_app.core.id3 import mime_for_image_ext
from vienetts_app.core.paths import normalize_local_path, sanitize_filename
from vienetts_app.core.timeline import SegmentSpan, Timeline, estimate_timeline, split_paragraphs

#: Hard BooksReader parser caps for Tier T0 Media Overlays.
BOOKSREADER_MAX_PARS = 4_390
BOOKSREADER_MAX_XML_BYTES = 781_100

#: Safe operating targets leaving comfortable headroom under BooksReader's caps.
MAX_SAFE_PARS = 4_000
MAX_SAFE_XML_BYTES = 700_000

#: CSS class applied by EPUB 3 Media Overlay players to the active text span.
MEDIA_ACTIVE_CLASS = "-epub-media-overlay-active"

_EPUB_UUID_NAMESPACE = uuid.UUID("7f3b5a72-9d3e-4b78-8e4c-0e5a2c6b3a11")
_XML_HEADER = '<?xml version="1.0" encoding="utf-8"?>'
_XML_INVALID_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff\ufffe\uffff]")
_LANG_PRIMARY_RE = re.compile(r"^[A-Za-z]{2,3}$")
_LANG_SUBTAG_RE = re.compile(r"^[A-Za-z0-9]{1,8}$")
_ISBN_PREFIX_RE = re.compile(r"^(?:urn:isbn:|isbn:)", re.IGNORECASE)
_ISBN_RE = re.compile(r"^(?:[0-9]{9}[0-9X]|[0-9]{13})$")

_CONTAINER_XML = (
    f"{_XML_HEADER}"
    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    "<rootfiles>"
    '<rootfile full-path="EPUB/package.opf" media-type="application/oebps-package+xml"/>'
    "</rootfiles>"
    "</container>"
).encode()

_STYLE_CSS = (
    "body{margin:1em;line-height:1.6}"
    "h1{font-size:1.4em;margin-bottom:1em}"
    "p{margin:0 0 .8em}"
    f".{MEDIA_ACTIVE_CLASS}{{background-color:#fff3a8;color:#111;border-radius:2px}}"
).encode()

__all__ = [
    "BOOKSREADER_MAX_PARS",
    "BOOKSREADER_MAX_XML_BYTES",
    "MAX_SAFE_PARS",
    "MAX_SAFE_XML_BYTES",
    "MEDIA_ACTIVE_CLASS",
    "ChapterOverlayInput",
    "OverlayClip",
    "build_chapter_smil",
    "build_chapter_xhtml",
    "coalesce_book_clips",
    "export_epub_media_overlays",
    "format_opf_duration",
    "format_smil_clock",
    "normalize_chapter_clips",
]


@dataclass(frozen=True)
class OverlayClip:
    """Normalized text+audio span for one EPUB 3 Media Overlay ``<par>`` entry."""

    char_start: int
    char_end: int
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class ChapterOverlayInput:
    """Per-chapter inputs consumed by :func:`coalesce_book_clips`."""

    chapter_num: int
    text: str
    clips: tuple[OverlayClip, ...]
    audio_duration_ms: int


def _sf() -> Any:
    """Lazy-import soundfile so startup paths stay free of libsndfile load cost."""
    import soundfile as sf

    return sf


def _clean_xml_text(text: str) -> str:
    return _XML_INVALID_RE.sub("", text)


def _xml_text(text: str) -> str:
    return escape(_clean_xml_text(text))


def _xml_attr(value: str) -> str:
    return escape(_clean_xml_text(value), {'"': "&quot;"})


def _render_inline_xhtml(text: str) -> str:
    cleaned = _clean_xml_text(text).replace("\r\n", "\n").replace("\r", "\n")
    return "<br/>".join(escape(line) for line in cleaned.split("\n"))


def _normalize_bcp47(raw: str, *, default: str = "vi") -> str:
    cleaned = (raw or "").strip().replace("_", "-")
    if not cleaned:
        return default
    parts = cleaned.split("-")
    if not _LANG_PRIMARY_RE.fullmatch(parts[0]):
        return default
    normalized = [parts[0].lower()]
    in_extension = False
    for part in parts[1:]:
        if not _LANG_SUBTAG_RE.fullmatch(part):
            return default
        if in_extension:
            normalized.append(part.lower())
        elif len(part) == 1:
            in_extension = True
            normalized.append(part.lower())
        elif len(part) == 4 and part.isalpha():
            normalized.append(part.capitalize())
        elif len(part) == 2 and part.isalpha():
            normalized.append(part.upper())
        else:
            normalized.append(part.lower())
    return "-".join(normalized)


def _normalize_isbn(raw: str) -> str:
    stripped = _ISBN_PREFIX_RE.sub("", (raw or "").strip())
    compact = re.sub(r"[\s-]+", "", stripped).upper()
    return compact if _ISBN_RE.fullmatch(compact) else ""


def format_smil_clock(ms: int) -> str:
    """Format milliseconds as a compact, exact SMIL 3.0 Timecount clock value.

    Uses exact integer millisecond arithmetic with trailing fractional zeros
    stripped (e.g. ``0`` -> ``"0s"``, ``5000`` -> ``"5s"``, ``12350`` -> ``"12.35s"``,
    ``12354`` -> ``"12.354s"``) so monotonicity is strictly preserved and
    ``clipBegin`` / ``clipEnd`` never round upward past ``audio_duration_ms``.
    """
    clamped = max(0, int(ms))
    if clamped == 0:
        return "0s"
    secs, rem = divmod(clamped, 1000)
    if rem == 0:
        return f"{secs}s"
    frac = f"{rem:03d}".rstrip("0")
    return f"{secs}.{frac}s"


def format_opf_duration(ms: int) -> str:
    """Format milliseconds as a SMIL 3.0 full clock value ``H:MM:SS.cc``."""
    clamped = max(0, int(ms))
    if clamped == 0:
        return "0:00:00.00"
    total_cs = max(1, (clamped + 5) // 10)
    total_secs, cs = divmod(total_cs, 100)
    hours, rem_secs = divmod(total_secs, 3600)
    minutes, secs = divmod(rem_secs, 60)
    return f"{hours}:{minutes:02d}:{secs:02d}.{cs:02d}"


def _trim_span_bounds(text: str, start: int, end: int) -> tuple[int, int] | None:
    lo = max(0, start)
    hi = min(len(text), end)
    while lo < hi and (text[lo].isspace() or _XML_INVALID_RE.match(text[lo]) is not None):
        lo += 1
    while hi > lo and (text[hi - 1].isspace() or _XML_INVALID_RE.match(text[hi - 1]) is not None):
        hi -= 1
    if lo >= hi:
        return None
    if not _clean_xml_text(text[lo:hi]).strip():
        return None
    return (lo, hi)


def _sanitize_spans(
    text: str, spans: Sequence[SegmentSpan], audio_duration_ms: int
) -> list[OverlayClip]:
    clips: list[OverlayClip] = []
    prev_char_end = 0
    prev_end_ms = 0
    text_len = len(text)

    for span in spans:
        if span.char_end <= span.char_start or span.char_end <= prev_char_end:
            continue
        trimmed = _trim_span_bounds(
            text, max(prev_char_end, int(span.char_start)), min(text_len, int(span.char_end))
        )
        if trimmed is None:
            continue
        c_start, c_end = trimmed

        if span.end_ms <= span.start_ms or span.end_ms <= 0 or span.start_ms >= audio_duration_ms:
            continue
        raw_start_ms = max(0, int(span.start_ms))
        raw_end_ms = min(audio_duration_ms, int(span.end_ms))
        start_ms = max(raw_start_ms, prev_end_ms)
        if start_ms >= audio_duration_ms or raw_end_ms <= start_ms:
            continue

        clips.append(OverlayClip(c_start, c_end, start_ms, raw_end_ms))
        prev_char_end = c_end
        prev_end_ms = raw_end_ms

    return clips


def normalize_chapter_clips(
    text: str, timeline: Timeline | None, audio_duration_ms: int
) -> list[OverlayClip]:
    """Normalize chapter timeline spans into strictly valid, monotonic :class:`OverlayClip`s.

    Filters/clamps degenerate, zero-length, whitespace-only, out-of-range, and
    out-of-order spans. Falls back to :func:`estimate_timeline` when ``timeline``
    is ``None`` or yields no valid clips, and finally to a single whole-chapter
    clip whenever ``audio_duration_ms > 0`` and ``text`` has visible characters.
    """
    duration = int(audio_duration_ms)
    if duration <= 0 or not text:
        return []
    whole_bounds = _trim_span_bounds(text, 0, len(text))
    if whole_bounds is None:
        return []

    clips = _sanitize_spans(text, timeline.segments, duration) if timeline is not None else []
    if not clips:
        estimated = estimate_timeline(text, duration)
        clips = _sanitize_spans(text, estimated.segments, duration)
    if not clips:
        clips = [OverlayClip(whole_bounds[0], whole_bounds[1], 0, duration)]
    return clips


def build_chapter_xhtml(
    chapter_title: str,
    chapter_text: str,
    clips: Sequence[OverlayClip],
    *,
    language: str = "vi",
) -> tuple[bytes, list[tuple[str, OverlayClip]]]:
    """Render one chapter as DTD-free XHTML 5 with ``<span id="pN">`` overlay targets.

    Emits ``<h1>`` (untargeted) followed by ``<p>`` blocks from
    :func:`split_paragraphs` with ``<br/>`` for internal newlines. Clips that
    straddle a paragraph break assign their unique ``id`` only to the first
    non-whitespace piece inside the first overlapping ``<p>`` and render any
    continuation as plain text so every target ID is unique, non-whitespace,
    and strictly inside a ``<p>``.
    """
    lang = _normalize_bcp47(language, default="vi")
    ordered_clips = list(clips)
    if len(ordered_clips) > 1 and any(
        ordered_clips[i].char_start > ordered_clips[i + 1].char_start
        for i in range(len(ordered_clips) - 1)
    ):
        ordered_clips.sort(key=lambda c: (c.char_start, c.char_end))

    paragraphs = split_paragraphs(chapter_text)
    emitted_targets: list[tuple[str, OverlayClip]] = []
    assigned_clips: set[int] = set()
    body_parts: list[str] = [f"<h1>{_xml_text(chapter_title)}</h1>"]

    clip_idx = 0
    num_clips = len(ordered_clips)

    for para in paragraphs:
        p_start = int(para["charStart"])
        p_end = int(para["charEnd"])
        while clip_idx < num_clips and ordered_clips[clip_idx].char_end <= p_start:
            clip_idx += 1

        cursor = p_start
        j = clip_idx
        inline_parts: list[str] = []

        while j < num_clips and ordered_clips[j].char_start < p_end:
            clip = ordered_clips[j]
            seg_start = max(cursor, clip.char_start)
            seg_end = min(p_end, clip.char_end)
            if seg_start < seg_end:
                if seg_start > cursor:
                    inline_parts.append(_render_inline_xhtml(chapter_text[cursor:seg_start]))
                piece = chapter_text[seg_start:seg_end]
                rendered = _render_inline_xhtml(piece)
                if j not in assigned_clips and _clean_xml_text(piece).strip():
                    assigned_clips.add(j)
                    span_id = f"p{len(emitted_targets) + 1}"
                    emitted_targets.append((span_id, clip))
                    inline_parts.append(f'<span id="{span_id}">{rendered}</span>')
                else:
                    inline_parts.append(rendered)
                cursor = seg_end
            if clip.char_end <= p_end:
                j += 1
            else:
                break

        if cursor < p_end:
            inline_parts.append(_render_inline_xhtml(chapter_text[cursor:p_end]))
        body_parts.append(f"<p>{''.join(inline_parts)}</p>")

    esc_title = _xml_text(chapter_title)
    esc_lang = _xml_attr(lang)
    xhtml = (
        f"{_XML_HEADER}"
        f'<html xmlns="http://www.w3.org/1999/xhtml" '
        f'xmlns:epub="http://www.idpf.org/2007/ops" '
        f'xml:lang="{esc_lang}" lang="{esc_lang}">'
        f"<head>"
        f'<meta charset="utf-8"/>'
        f"<title>{esc_title}</title>"
        f'<link rel="stylesheet" type="text/css" href="style.css"/>'
        f"</head>"
        f"<body>{''.join(body_parts)}</body>"
        f"</html>"
    )
    return xhtml.encode("utf-8"), emitted_targets


def build_chapter_smil(
    chapter_num: int,
    targets: Sequence[tuple[str, OverlayClip]],
    audio_duration_ms: int,
) -> bytes:
    """Build ultra-compact, DTD-free SMIL 3.0 Media Overlay bytes for chapter ``chapter_num``."""
    k = int(chapter_num)
    if k < 1:
        raise ValueError(f"chapter_num must be >= 1, got {chapter_num}")
    duration_ms = max(0, int(audio_duration_ms))

    par_parts: list[str] = []
    if duration_ms > 0:
        prev_begin_ms = 0
        prev_end_ms = 0
        doc_href = f"c{k}.xhtml"
        audio_href = f"a{k}.mp3"
        for span_id, clip in targets:
            begin_ms = min(max(0, prev_begin_ms, int(clip.start_ms)), duration_ms - 1)
            end_ms = min(duration_ms, max(begin_ms, prev_end_ms, int(clip.end_ms)))
            prev_begin_ms = begin_ms
            prev_end_ms = end_ms
            par_parts.append(
                f'<par><text src="{doc_href}#{_xml_attr(span_id)}"/>'
                f'<audio src="{audio_href}" '
                f'clipBegin="{format_smil_clock(begin_ms)}" '
                f'clipEnd="{format_smil_clock(end_ms)}"/></par>'
            )

    smil = (
        f"{_XML_HEADER}"
        '<smil xmlns="http://www.w3.org/ns/SMIL" '
        'xmlns:epub="http://www.idpf.org/2007/ops" version="3.0">'
        f'<body><seq epub:textref="c{k}.xhtml">{"".join(par_parts)}</seq></body>'
        "</smil>"
    )
    return smil.encode("utf-8")


def _merge_clip_group(group: Sequence[OverlayClip]) -> OverlayClip:
    return OverlayClip(
        char_start=group[0].char_start,
        char_end=group[-1].char_end,
        start_ms=group[0].start_ms,
        end_ms=group[-1].end_ms,
    )


def _coalesce_chapter_to_paragraphs(text: str, clips: Sequence[OverlayClip]) -> list[OverlayClip]:
    if len(clips) <= 1:
        return list(clips)
    paragraphs = split_paragraphs(text)
    if not paragraphs:
        return list(clips)

    coalesced: list[OverlayClip] = []
    p_idx = 0
    last_p_idx = len(paragraphs) - 1
    current_group: list[OverlayClip] = []
    current_key = -1

    for clip in clips:
        while p_idx < last_p_idx and int(paragraphs[p_idx]["charEnd"]) <= clip.char_start:
            p_idx += 1
        if current_group and p_idx != current_key:
            coalesced.append(_merge_clip_group(current_group))
            current_group = [clip]
            current_key = p_idx
        else:
            if not current_group:
                current_key = p_idx
            current_group.append(clip)

    if current_group:
        coalesced.append(_merge_clip_group(current_group))
    return coalesced


def _measure_smil_budget(
    chapter_inputs: Sequence[ChapterOverlayInput],
    chapter_clips: Sequence[Sequence[OverlayClip]],
) -> tuple[int, int]:
    total_pars = 0
    total_smil_bytes = 0
    for inp, clips in zip(chapter_inputs, chapter_clips, strict=True):
        targets = [(f"p{i}", clip) for i, clip in enumerate(clips, start=1)]
        total_pars += len(targets)
        total_smil_bytes += len(build_chapter_smil(inp.chapter_num, targets, inp.audio_duration_ms))
    return total_pars, total_smil_bytes


def coalesce_book_clips(
    chapter_inputs: Sequence[ChapterOverlayInput],
    *,
    fixed_xml_bytes: int = 0,
    max_pars: int = MAX_SAFE_PARS,
    max_xml_bytes: int = MAX_SAFE_XML_BYTES,
) -> list[list[OverlayClip]]:
    """Coalesce clips across all ready chapters to stay under BooksReader's XML & ``<par>`` caps.

    1. Keep 1 ``<par>`` per clip when already within both budgets.
    2. Coalesce consecutive clips sharing a paragraph (:func:`split_paragraphs`).
    3. If still above either budget, iteratively merge consecutive clips per
       chapter with ``stride = max(2, ceil(ratio))`` while re-measuring exact
       serialized SMIL bytes until both budgets hold (or every chapter reaches 1 clip).
    """
    if not chapter_inputs:
        return []

    current: list[list[OverlayClip]] = [list(inp.clips) for inp in chapter_inputs]
    total_pars, smil_bytes = _measure_smil_budget(chapter_inputs, current)
    if total_pars <= max_pars and fixed_xml_bytes + smil_bytes <= max_xml_bytes:
        return current

    # Stage 1: paragraph-boundary coalescing
    current = [
        _coalesce_chapter_to_paragraphs(inp.text, clips)
        for inp, clips in zip(chapter_inputs, current, strict=True)
    ]
    total_pars, smil_bytes = _measure_smil_budget(chapter_inputs, current)
    if total_pars <= max_pars and fixed_xml_bytes + smil_bytes <= max_xml_bytes:
        return current

    # Stage 2: iterative uniform-stride coalescing
    smil_overhead = sum(
        len(build_chapter_smil(inp.chapter_num, (), inp.audio_duration_ms))
        for inp in chapter_inputs
    )
    while total_pars > max_pars or fixed_xml_bytes + smil_bytes > max_xml_bytes:
        if all(len(clips) <= 1 for clips in current):
            break

        par_ratio = (total_pars / max(1, max_pars)) if total_pars > max_pars else 1.0
        if fixed_xml_bytes + smil_bytes > max_xml_bytes:
            par_payload = max(0, smil_bytes - smil_overhead)
            avail_payload = max_xml_bytes - fixed_xml_bytes - smil_overhead
            byte_ratio = (
                (par_payload / avail_payload) if (avail_payload > 0 and par_payload > 0) else 2.0
            )
        else:
            byte_ratio = 1.0

        stride = max(2, math.ceil(max(par_ratio, byte_ratio)))
        current = [
            [_merge_clip_group(clips[i : i + stride]) for i in range(0, len(clips), stride)]
            if len(clips) > 1
            else clips
            for clips in current
        ]
        total_pars, smil_bytes = _measure_smil_budget(chapter_inputs, current)

    return current


def _build_package_opf(
    *,
    state: BookState,
    meta: BookExportMetadata,
    language: str,
    ready_durations_ms: dict[int, int],
    cover_ext: str,
    cover_mime: str,
) -> bytes:
    isbn = _normalize_isbn(meta.isbn)
    if isbn:
        identifier = f"urn:isbn:{isbn}"
    else:
        seed = state.record.content_hash or state.record.id or meta.title
        identifier = f"urn:uuid:{uuid.uuid5(_EPUB_UUID_NAMESPACE, seed)}"

    title = (meta.title or state.record.title or "Audiobook").strip()
    authors = [a.strip() for a in meta.authors if a and a.strip()]
    if not authors and meta.author.strip():
        authors = [part.strip() for part in meta.author.split(";") if part.strip()]

    modified_utc = _dt.datetime.now(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    total_duration_ms = sum(ready_durations_ms.values())

    meta_parts: list[str] = [
        f'<dc:identifier id="uid">{_xml_text(identifier)}</dc:identifier>',
        f"<dc:title>{_xml_text(title)}</dc:title>",
    ]
    for author in authors:
        meta_parts.append(f"<dc:creator>{_xml_text(author)}</dc:creator>")
    meta_parts.append(f"<dc:language>{_xml_text(language)}</dc:language>")
    if meta.description and meta.description.strip():
        meta_parts.append(f"<dc:description>{_xml_text(meta.description.strip())}</dc:description>")
    if meta.year is not None and 1000 <= meta.year <= 2999:
        meta_parts.append(f"<dc:date>{meta.year}</dc:date>")
    meta_parts.append(f'<meta property="dcterms:modified">{modified_utc}</meta>')
    for k in sorted(ready_durations_ms):
        meta_parts.append(
            f'<meta property="media:duration" refines="#s{k}">'
            f"{format_opf_duration(ready_durations_ms[k])}</meta>"
        )
    meta_parts.append(
        f'<meta property="media:duration">{format_opf_duration(total_duration_ms)}</meta>'
    )
    meta_parts.append(f'<meta property="media:active-class">{MEDIA_ACTIVE_CLASS}</meta>')
    if cover_ext and cover_mime:
        meta_parts.append('<meta name="cover" content="cover"/>')

    manifest_parts: list[str] = [
        '<item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" properties="nav"/>',
        '<item id="css" href="style.css" media-type="text/css"/>',
    ]
    if cover_ext and cover_mime:
        manifest_parts.append(
            f'<item id="cover" href="cover.{_xml_attr(cover_ext)}" '
            f'media-type="{_xml_attr(cover_mime)}" properties="cover-image"/>'
        )

    spine_parts: list[str] = []
    for k in range(1, len(state.chapters) + 1):
        if k in ready_durations_ms:
            manifest_parts.append(
                f'<item id="c{k}" href="c{k}.xhtml" media-type="application/xhtml+xml" '
                f'media-overlay="s{k}"/>'
            )
        else:
            manifest_parts.append(
                f'<item id="c{k}" href="c{k}.xhtml" media-type="application/xhtml+xml"/>'
            )
        spine_parts.append(f'<itemref idref="c{k}"/>')

    for k in sorted(ready_durations_ms):
        manifest_parts.append(
            f'<item id="s{k}" href="s{k}.smil" media-type="application/smil+xml"/>'
        )
        manifest_parts.append(f'<item id="a{k}" href="a{k}.mp3" media-type="audio/mpeg"/>')

    opf = (
        f"{_XML_HEADER}"
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid" '
        'prefix="media: http://www.idpf.org/epub/vocab/overlays/#">'
        f'<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">{"".join(meta_parts)}</metadata>'
        f"<manifest>{''.join(manifest_parts)}</manifest>"
        f"<spine>{''.join(spine_parts)}</spine>"
        "</package>"
    )
    return opf.encode("utf-8")


def _build_nav_xhtml(title: str, chapters: Sequence[Any], *, language: str) -> bytes:
    esc_title = _xml_text(title)
    esc_lang = _xml_attr(language)
    items = "".join(
        f'<li><a href="c{k}.xhtml">{_xml_text(ch.title)}</a></li>'
        for k, ch in enumerate(chapters, start=1)
    )
    nav = (
        f"{_XML_HEADER}"
        f'<html xmlns="http://www.w3.org/1999/xhtml" '
        f'xmlns:epub="http://www.idpf.org/2007/ops" '
        f'xml:lang="{esc_lang}" lang="{esc_lang}">'
        f"<head>"
        f'<meta charset="utf-8"/>'
        f"<title>{esc_title}</title>"
        f'<link rel="stylesheet" type="text/css" href="style.css"/>'
        f"</head>"
        f'<body><nav epub:type="toc" id="toc"><h1>{esc_title}</h1><ol>{items}</ol></nav></body>'
        f"</html>"
    )
    return nav.encode("utf-8")


def _write_stored_bytes(zf: zipfile.ZipFile, arcname: str, data: bytes) -> None:
    info = zipfile.ZipInfo(arcname)
    info.compress_type = zipfile.ZIP_STORED
    zf.writestr(info, data, compress_type=zipfile.ZIP_STORED)


def _replace_with_lock_retry(src: Path, dst: Path) -> None:
    for attempt in range(REPLACE_LOCK_ATTEMPTS):
        try:
            os.replace(src, dst)
            return
        except PermissionError:
            if attempt == REPLACE_LOCK_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_LOCK_DELAY_S)


def export_epub_media_overlays(
    library: AudiobookLibrary,
    book_id: str,
    dest_dir: str | Path,
    *,
    ready_indices: Sequence[int] | None = None,
    book: BookState | None = None,
    progress: Callable[[int, int], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> Path:
    """Export a book as a narrated EPUB 3 archive with synchronized SMIL Media Overlays."""

    def _check_cancelled() -> None:
        if cancel_check is not None and cancel_check():
            raise ExportCancelled("Audio export cancelled.")

    _check_cancelled()

    state = library.load_book(book_id) if book is None else book
    chapter_state = library.read_state(book_id)

    requested: set[int] | None = None
    if ready_indices is not None:
        requested = {int(idx) for idx in ready_indices}

    ready_chapters: list[tuple[int, Any]] = []
    for k, ch in enumerate(state.chapters, start=1):
        if requested is not None and ch.index not in requested:
            continue
        if library.has_chapter_audio(book_id, ch.index, state=chapter_state):
            ready_chapters.append((k, ch))

    if not ready_chapters:
        raise AudiobookError("No rendered chapters are ready to export as EPUB 3.")

    meta = library.resolve_book_export_metadata(book_id, book=state)
    language = _normalize_bcp47(meta.language, default="vi")
    title = (meta.title or state.record.title or "Audiobook").strip()

    cover_bytes: bytes | None = None
    cover_ext = ""
    cover_mime = ""
    if meta.cover_bytes and len(meta.cover_bytes) <= MAX_COVER_BYTES:
        detected_ext = sniff_image_ext(meta.cover_bytes) or (
            meta.cover_ext or ""
        ).strip().lower().lstrip(".")
        detected_mime = mime_for_image_ext(detected_ext)
        if detected_ext in ("jpg", "png", "gif", "webp") and detected_mime:
            cover_bytes = meta.cover_bytes
            cover_ext = detected_ext
            cover_mime = detected_mime

    dest = normalize_local_path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    book_stem = sanitize_filename(title, max_len=80, fallback=f"audiobook-{book_id}")
    target_path = dest / f"{book_stem}.epub"

    token = uuid.uuid4().hex
    work_dir = dest / f".{book_stem}.{token}.epubwork"
    part_epub = dest / f".{book_stem}.{token}.part.epub"
    total_steps = len(ready_chapters) + 1

    sf = _sf()
    try:
        work_dir.mkdir(parents=True, exist_ok=True)

        mp3_paths: dict[int, Path] = {}
        ready_durations_ms: dict[int, int] = {}
        overlay_inputs: list[ChapterOverlayInput] = []

        for done, (k, ch) in enumerate(ready_chapters, start=1):
            _check_cancelled()
            sources = library.chapter_audio_paths(book_id, ch.index, state=chapter_state)
            mp3_path = work_dir / f"a{k}.mp3"
            try:
                export_mp3_parts(sources, mp3_path, id3_bytes=None, cancel_check=cancel_check)
            except ExportCancelled:
                raise
            except Exception as exc:
                raise AudiobookError(
                    f"Could not encode audio for chapter {k} ('{ch.title}'): {exc}"
                ) from exc

            try:
                info = sf.info(str(mp3_path))
                if info.samplerate <= 0 or info.frames <= 0:
                    raise AudiobookError(f"Encoded audio for chapter {k} is empty or unreadable.")
                duration_ms = max(1, int(round(info.frames * 1000 / info.samplerate)))
            except AudiobookError:
                raise
            except Exception as exc:
                raise AudiobookError(
                    f"Could not inspect encoded MP3 for chapter {k}: {exc}"
                ) from exc

            timeline = library.load_chapter_timeline(book_id, ch.index)
            clips = normalize_chapter_clips(ch.text, timeline, duration_ms)

            mp3_paths[k] = mp3_path
            ready_durations_ms[k] = duration_ms
            overlay_inputs.append(
                ChapterOverlayInput(
                    chapter_num=k,
                    text=ch.text,
                    clips=tuple(clips),
                    audio_duration_ms=duration_ms,
                )
            )
            if progress is not None:
                progress(done, total_steps)

        _check_cancelled()
        opf_bytes = _build_package_opf(
            state=state,
            meta=meta,
            language=language,
            ready_durations_ms=ready_durations_ms,
            cover_ext=cover_ext,
            cover_mime=cover_mime,
        )
        fixed_xml_bytes = len(_CONTAINER_XML) + len(opf_bytes)
        coalesced_lists = coalesce_book_clips(
            overlay_inputs,
            fixed_xml_bytes=fixed_xml_bytes,
            max_pars=MAX_SAFE_PARS,
            max_xml_bytes=MAX_SAFE_XML_BYTES,
        )
        coalesced_by_num = {
            inp.chapter_num: clips
            for inp, clips in zip(overlay_inputs, coalesced_lists, strict=True)
        }

        nav_bytes = _build_nav_xhtml(title, state.chapters, language=language)

        with zipfile.ZipFile(part_epub, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
            # Entry 0 MUST be uncompressed mimetype
            _write_stored_bytes(zf, "mimetype", b"application/epub+zip")
            zf.writestr("META-INF/container.xml", _CONTAINER_XML)
            zf.writestr("EPUB/package.opf", opf_bytes)
            zf.writestr("EPUB/nav.xhtml", nav_bytes)
            zf.writestr("EPUB/style.css", _STYLE_CSS)
            if cover_bytes is not None and cover_ext:
                _write_stored_bytes(zf, f"EPUB/cover.{cover_ext}", cover_bytes)

            smil_by_num: dict[int, bytes] = {}
            for k, ch in enumerate(state.chapters, start=1):
                _check_cancelled()
                clips_for_ch = coalesced_by_num.get(k, ())
                xhtml_bytes, emitted_targets = build_chapter_xhtml(
                    ch.title, ch.text, clips_for_ch, language=language
                )
                zf.writestr(f"EPUB/c{k}.xhtml", xhtml_bytes)
                if k in ready_durations_ms:
                    smil_by_num[k] = build_chapter_smil(k, emitted_targets, ready_durations_ms[k])

            for k in sorted(ready_durations_ms):
                _check_cancelled()
                zf.writestr(f"EPUB/s{k}.smil", smil_by_num[k])
                zf.write(mp3_paths[k], f"EPUB/a{k}.mp3", compress_type=zipfile.ZIP_STORED)

        _check_cancelled()
        try:
            _replace_with_lock_retry(part_epub, target_path)
        except PermissionError as exc:
            raise AudiobookError(
                f"Could not write EPUB 3 file (locked by another program): {exc}"
            ) from exc
    except (ExportCancelled, AudiobookError):
        with contextlib.suppress(OSError):
            part_epub.unlink(missing_ok=True)
        raise
    except Exception as exc:
        with contextlib.suppress(OSError):
            part_epub.unlink(missing_ok=True)
        raise AudiobookError(f"Could not export EPUB 3 audiobook: {exc}") from exc
    except BaseException:
        with contextlib.suppress(OSError):
            part_epub.unlink(missing_ok=True)
        raise
    finally:
        shutil.rmtree(work_dir, ignore_errors=True)

    if progress is not None:
        progress(total_steps, total_steps)
    return target_path
