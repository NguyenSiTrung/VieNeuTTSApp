"""Narrated EPUB 3 with Media Overlays export (FR-3.1..FR-3.4, BooksReader Tier T0)."""

from __future__ import annotations

import re
import zipfile
from pathlib import Path
from typing import Any
from xml.etree import ElementTree as ET

import numpy as np
import pytest
import soundfile as sf

from vienetts_app.core.audio import ExportCancelled, write_wav_file
from vienetts_app.core.audiobook import AudiobookError, AudiobookLibrary
from vienetts_app.core.chapter_split import ChapterPart
from vienetts_app.core.epub import EpubBook, EpubChapter, import_epub
from vienetts_app.core.epub_export import (
    BOOKSREADER_MAX_PARS,
    BOOKSREADER_MAX_XML_BYTES,
    MAX_SAFE_PARS,
    MAX_SAFE_XML_BYTES,
    MEDIA_ACTIVE_CLASS,
    ChapterOverlayInput,
    OverlayClip,
    build_chapter_smil,
    build_chapter_xhtml,
    coalesce_book_clips,
    export_epub_media_overlays,
    format_opf_duration,
    format_smil_clock,
    normalize_chapter_clips,
)
from vienetts_app.core.timeline import SegmentSpan, Timeline

SAMPLE_RATE = 48_000

_OCF_NS = "urn:oasis:names:tc:opendocument:xmlns:container"
_OPF_NS = "http://www.idpf.org/2007/opf"
_DC_NS = "http://purl.org/dc/elements/1.1/"
_XHTML_NS = "http://www.w3.org/1999/xhtml"
_EPUB_NS = "http://www.idpf.org/2007/ops"
_SMIL_NS = "http://www.w3.org/ns/SMIL"

_MINIMAL_JPEG = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xd9"


def _make_audio(seconds: float = 0.25, freq: float = 440.0) -> np.ndarray:
    count = max(1, int(round(SAMPLE_RATE * seconds)))
    t = np.linspace(0.0, seconds, count, endpoint=False, dtype=np.float32)
    return (0.25 * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def _parse_smil_clock_ms(raw: str) -> float:
    text = raw.strip()
    if text.endswith("ms"):
        return float(text[:-2])
    if text.endswith("s"):
        return float(text[:-1]) * 1000.0
    if text.endswith("min"):
        return float(text[:-3]) * 60_000.0
    if text.endswith("h"):
        return float(text[:-1]) * 3_600_000.0
    parts = text.split(":")
    if len(parts) == 3:
        return (int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])) * 1000.0
    if len(parts) == 2:
        return (int(parts[0]) * 60 + float(parts[1])) * 1000.0
    return float(text) * 1000.0


def _collect_body_id_offsets(
    body_el: ET.Element, doc_href: str
) -> tuple[dict[str, tuple[int, str]], int]:
    """Walk XHTML ``<body>`` in document order and return ``(id -> (offset, text), chars)``."""
    id_offsets: dict[str, tuple[int, str]] = {}
    seen_ids: set[str] = set()
    cursor = 0

    def _walk(el: ET.Element) -> None:
        nonlocal cursor
        el_id = el.get("id")
        start_offset = cursor
        if el_id is not None:
            assert el_id not in seen_ids, f"duplicate id {el_id} in {doc_href}"
            seen_ids.add(el_id)
            assert el.tag != f"{{{_XHTML_NS}}}h1", "h1 must not be SMIL-targeted"
        if el.text:
            cursor += len(el.text)
        for child in el:
            _walk(child)
            if child.tail:
                cursor += len(child.tail)
        if el_id is not None:
            id_offsets[el_id] = (start_offset, "".join(el.itertext()))

    _walk(body_el)
    return id_offsets, cursor


def _verify_epub_archive(epub_path: Path, tmp_extract: Path) -> dict[str, Any]:
    """Strict BooksReader Tier T0 + OCF + Media Overlays invariant verifier."""
    assert epub_path.is_file()
    tmp_extract.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(epub_path, "r") as zf:
        infos = zf.infolist()
        assert infos, "EPUB archive must not be empty"

        # 1. Entry 0 MUST be uncompressed mimetype with exact bytes
        first = infos[0]
        assert first.filename == "mimetype"
        assert first.compress_type == zipfile.ZIP_STORED
        assert zf.read("mimetype") == b"application/epub+zip"

        names = set(zf.namelist())
        assert "META-INF/container.xml" in names

        # 2. Zero <!DOCTYPE anywhere in XML/XHTML/SMIL entries + valid XML parse
        xml_entries: dict[str, ET.Element] = {}
        raw_entries: dict[str, bytes] = {}
        for info in infos:
            if info.filename.endswith((".xml", ".opf", ".xhtml", ".smil")):
                raw = zf.read(info.filename)
                raw_entries[info.filename] = raw
                assert b"<!doctype" not in raw.lower(), f"DOCTYPE found in {info.filename}"
                xml_entries[info.filename] = ET.fromstring(raw)

        # 3. container.xml -> EPUB/package.opf
        container = xml_entries["META-INF/container.xml"]
        rootfile = container.find(f".//{{{_OCF_NS}}}rootfile")
        assert rootfile is not None
        assert rootfile.get("full-path") == "EPUB/package.opf"
        assert rootfile.get("media-type") == "application/oebps-package+xml"

        # 4. OPF structure & metadata
        opf = xml_entries["EPUB/package.opf"]
        assert opf.tag == f"{{{_OPF_NS}}}package"
        assert opf.get("version") == "3.0"
        assert opf.get("unique-identifier") == "uid"
        assert "http://www.idpf.org/epub/vocab/overlays/#" in (opf.get("prefix") or "")

        manifest_el = opf.find(f"{{{_OPF_NS}}}manifest")
        spine_el = opf.find(f"{{{_OPF_NS}}}spine")
        metadata_el = opf.find(f"{{{_OPF_NS}}}metadata")
        assert manifest_el is not None and spine_el is not None and metadata_el is not None

        manifest_by_id: dict[str, ET.Element] = {}
        for item in manifest_el.findall(f"{{{_OPF_NS}}}item"):
            item_id = item.get("id") or ""
            assert item_id and item_id not in manifest_by_id
            manifest_by_id[item_id] = item
            href = item.get("href") or ""
            assert f"EPUB/{href}" in names

        # nav must exist and be excluded from spine
        assert "nav" in manifest_by_id
        assert "nav" in (manifest_by_id["nav"].get("properties") or "").split()
        spine_ids = [ref.get("idref") or "" for ref in spine_el.findall(f"{{{_OPF_NS}}}itemref")]
        assert spine_ids
        assert "nav" not in spine_ids

        # Metadata checks
        uid_el = metadata_el.find(f"{{{_DC_NS}}}identifier")
        assert uid_el is not None and uid_el.get("id") == "uid" and (uid_el.text or "").strip()
        modified_els = [
            el
            for el in metadata_el.findall(f"{{{_OPF_NS}}}meta")
            if el.get("property") == "dcterms:modified"
        ]
        assert len(modified_els) == 1
        assert re.fullmatch(
            r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", (modified_els[0].text or "").strip()
        )
        active_class_els = [
            el
            for el in metadata_el.findall(f"{{{_OPF_NS}}}meta")
            if el.get("property") == "media:active-class"
        ]
        assert len(active_class_els) == 1
        assert active_class_els[0].text == MEDIA_ACTIVE_CLASS

        # Cover ZIP_STORED check if present
        if "cover" in manifest_by_id:
            cover_href = manifest_by_id["cover"].get("href") or ""
            cover_info = zf.getinfo(f"EPUB/{cover_href}")
            assert cover_info.compress_type == zipfile.ZIP_STORED

        # 5. Walk spine & verify SMIL referential integrity, MP3 validity, and global monotonicity
        total_pars = 0
        smil_xml_bytes = 0
        global_char_offset = 0
        global_audio_ms = 0.0
        global_anchors: list[tuple[int, float]] = []
        overlaid_spine_ids: list[str] = []
        audio_durations_ms: dict[str, int] = {}

        for spine_id in spine_ids:
            doc_item = manifest_by_id[spine_id]
            doc_href = doc_item.get("href") or ""
            xhtml_root = xml_entries[f"EPUB/{doc_href}"]
            body_el = xhtml_root.find(f"{{{_XHTML_NS}}}body")
            assert body_el is not None

            # Walk body text nodes to compute exact DOM character offsets per id
            id_offsets, local_cursor = _collect_body_id_offsets(body_el, doc_href)

            overlay_id = doc_item.get("media-overlay")
            if overlay_id:
                overlaid_spine_ids.append(spine_id)
                smil_item = manifest_by_id[overlay_id]
                assert smil_item.get("media-type") == "application/smil+xml"
                smil_href = smil_item.get("href") or ""
                smil_zip_name = f"EPUB/{smil_href}"
                smil_xml_bytes += len(raw_entries[smil_zip_name])
                smil_root = xml_entries[smil_zip_name]
                assert smil_root.tag == f"{{{_SMIL_NS}}}smil"
                assert smil_root.get("version") == "3.0"

                seq_el = smil_root.find(f".//{{{_SMIL_NS}}}seq")
                assert seq_el is not None
                assert seq_el.get(f"{{{_EPUB_NS}}}textref") == doc_href

                pars = seq_el.findall(f"{{{_SMIL_NS}}}par")
                assert pars, f"expected at least one <par> in {smil_zip_name}"
                total_pars += len(pars)

                prev_local_char = -1
                prev_begin = -1.0
                prev_end = -1.0
                chapter_dur_ms = 0

                for par in pars:
                    text_el = par.find(f"{{{_SMIL_NS}}}text")
                    audio_el = par.find(f"{{{_SMIL_NS}}}audio")
                    assert text_el is not None and audio_el is not None

                    src = text_el.get("src") or ""
                    file_part, sep, frag_id = src.partition("#")
                    assert sep == "#" and file_part == doc_href
                    assert frag_id in id_offsets, f"dangling SMIL target {src}"
                    char_off, elem_text = id_offsets[frag_id]
                    assert elem_text.strip(), f"SMIL target {src} has whitespace-only text"
                    assert char_off > prev_local_char
                    prev_local_char = char_off

                    audio_src = audio_el.get("src") or ""
                    audio_zip_name = f"EPUB/{audio_src}"
                    assert audio_zip_name in names
                    audio_info = zf.getinfo(audio_zip_name)
                    assert audio_info.compress_type == zipfile.ZIP_STORED

                    if audio_src not in audio_durations_ms:
                        extracted = tmp_extract / audio_src
                        extracted.write_bytes(zf.read(audio_zip_name))
                        sf_info = sf.info(str(extracted))
                        assert sf_info.format == "MP3", f"expected MP3, got {sf_info.format}"
                        audio_durations_ms[audio_src] = max(
                            1, int(round(sf_info.frames * 1000 / sf_info.samplerate))
                        )
                    chapter_dur_ms = audio_durations_ms[audio_src]

                    clip_begin = _parse_smil_clock_ms(audio_el.get("clipBegin") or "")
                    clip_end = _parse_smil_clock_ms(audio_el.get("clipEnd") or "")
                    assert 0.0 <= clip_begin < chapter_dur_ms
                    assert clip_begin <= clip_end <= chapter_dur_ms
                    assert clip_begin >= prev_begin
                    assert clip_end >= prev_end
                    prev_begin = clip_begin
                    prev_end = clip_end

                    global_anchors.append(
                        (global_char_offset + char_off, global_audio_ms + clip_begin)
                    )

                global_audio_ms += chapter_dur_ms

            global_char_offset += local_cursor

        # Global monotonic check on both axes
        for prev_anchor, curr_anchor in zip(global_anchors, global_anchors[1:], strict=False):
            assert curr_anchor[0] > prev_anchor[0], "global charOffset must strictly increase"
            assert curr_anchor[1] >= prev_anchor[1], "global audioTimeMs must be non-decreasing"

        total_xml_bytes = (
            len(raw_entries["META-INF/container.xml"])
            + len(raw_entries["EPUB/package.opf"])
            + smil_xml_bytes
        )
        assert total_pars <= MAX_SAFE_PARS <= BOOKSREADER_MAX_PARS
        assert total_xml_bytes <= MAX_SAFE_XML_BYTES <= BOOKSREADER_MAX_XML_BYTES

        return {
            "opf": opf,
            "metadata": metadata_el,
            "manifest": manifest_by_id,
            "spine_ids": spine_ids,
            "overlaid_spine_ids": overlaid_spine_ids,
            "total_pars": total_pars,
            "total_xml_bytes": total_xml_bytes,
            "global_anchors": global_anchors,
            "audio_durations_ms": audio_durations_ms,
            "xml_entries": xml_entries,
            "raw_entries": raw_entries,
        }


class TestConstantsAndClockFormatting:
    def test_booksreader_budget_constants(self) -> None:
        assert BOOKSREADER_MAX_PARS == 4_390
        assert BOOKSREADER_MAX_XML_BYTES == 781_100
        assert MAX_SAFE_PARS == 4_000
        assert MAX_SAFE_XML_BYTES == 700_000
        assert MEDIA_ACTIVE_CLASS == "-epub-media-overlay-active"

    def test_format_smil_clock_compact_and_exact(self) -> None:
        assert format_smil_clock(0) == "0s"
        assert format_smil_clock(5_000) == "5s"
        assert format_smil_clock(12_300) == "12.3s"
        assert format_smil_clock(12_350) == "12.35s"
        assert format_smil_clock(12_354) == "12.354s"
        assert format_smil_clock(5) == "0.005s"
        # Must never round clipBegin up to >= duration_ms
        assert _parse_smil_clock_ms(format_smil_clock(999)) < 1_000.0
        # Monotonic across tight sub-10ms spacing
        samples = [0, 1, 2, 5, 9, 10, 11, 999, 1_000, 1_001, 12_354, 12_355]
        parsed = [_parse_smil_clock_ms(format_smil_clock(ms)) for ms in samples]
        assert parsed == sorted(parsed)
        assert len(set(parsed)) == len(samples)

    def test_format_opf_duration_valid_smil_full_clock(self) -> None:
        assert format_opf_duration(0) == "0:00:00.00"
        assert format_opf_duration(83_450) == "0:01:23.45"
        assert format_opf_duration(3_661_050) == "1:01:01.05"
        assert format_opf_duration(59_999) == "0:01:00.00"
        assert format_opf_duration(1) == "0:00:00.01"


class TestNormalizeChapterClips:
    def test_filters_and_clamps_degenerate_and_out_of_order_spans(self) -> None:
        text = "Xin chào thế giới.\n\nĐây là đoạn thứ hai.\n\nVà đoạn cuối cùng."
        timeline = Timeline(
            segments=(
                SegmentSpan(-1, -1, 0, 100),  # unmapped
                SegmentSpan(0, 0, 0, 100),  # zero-length text
                SegmentSpan(18, 20, 0, 100),  # whitespace-only "\n\n"
                SegmentSpan(0, 18, -50, 1_000),  # negative start clamped to 0
                SegmentSpan(10, 15, 1_000, 1_200),  # out-of-order char offset (already covered)
                SegmentSpan(16, 40, 800, 2_000),  # overlapping char & time -> clamped forward
                SegmentSpan(40, 42, 2_000, 2_000),  # zero-duration audio
                SegmentSpan(42, 999, 2_000, 3_500),  # end past len(text) and duration
                SegmentSpan(50, 58, 3_000, 3_600),  # starts at/past audio_duration_ms
            ),
            approximate=False,
        )
        duration_ms = 3_000
        clips = normalize_chapter_clips(text, timeline, duration_ms)
        assert len(clips) == 3

        prev_char_end = 0
        prev_start_ms = 0
        prev_end_ms = 0
        for clip in clips:
            assert 0 <= clip.char_start < clip.char_end <= len(text)
            assert text[clip.char_start : clip.char_end].strip() != ""
            assert clip.char_start >= prev_char_end
            assert 0 <= clip.start_ms < duration_ms
            assert clip.start_ms <= clip.end_ms <= duration_ms
            assert clip.start_ms >= prev_start_ms
            assert clip.end_ms >= prev_end_ms
            prev_char_end = clip.char_end
            prev_start_ms = clip.start_ms
            prev_end_ms = clip.end_ms

        assert clips[0] == OverlayClip(0, 18, 0, 1_000)
        assert clips[1].char_start >= 18
        assert clips[1].start_ms == 1_000
        assert clips[1].end_ms == 2_000
        assert clips[2].char_end == len(text)
        assert clips[2].end_ms == duration_ms

    def test_falls_back_to_estimate_and_single_whole_clip(self) -> None:
        text = "  Đoạn một có dấu tiếng Việt.\n\nĐoạn hai tiếp nối.  "
        # None timeline -> estimate_timeline fallback
        est_clips = normalize_chapter_clips(text, None, 4_000)
        assert est_clips
        assert all(0 <= c.start_ms < 4_000 and c.start_ms <= c.end_ms <= 4_000 for c in est_clips)

        # All-degenerate timeline -> falls back to estimate_timeline
        broken = Timeline(segments=(SegmentSpan(-1, -1, 0, 500),), approximate=False)
        recovered = normalize_chapter_clips(text, broken, 4_000)
        assert recovered == est_clips

        # Extreme 1ms duration with multi-sentence text still emits at least one valid clip
        tiny = normalize_chapter_clips(text, None, 1)
        assert len(tiny) >= 1
        assert tiny[0].start_ms == 0
        assert tiny[-1].end_ms == 1
        assert text[tiny[0].char_start : tiny[0].char_end].strip() != ""

        # Empty text or non-positive duration -> []
        assert normalize_chapter_clips("   \n\n  ", None, 1_000) == []
        assert normalize_chapter_clips("Hello", None, 0) == []


class TestXhtmlAndSmilBuilders:
    def test_paragraph_rendering_br_escaping_and_straddling_clip(self) -> None:
        title = "Chương 1: Khởi đầu & <Thử thách>"
        text = "Dòng 1\nDòng 2 kết thúc.\n\nĐoạn hai bắt đầu ở đây.\n\nĐoạn ba độc lập."
        p1_end = text.index("\n\n")
        p2_start = p1_end + 2
        p2_mid = p2_start + len("Đoạn hai")
        p3_start = text.rindex("\n\n") + 2

        clips = [
            OverlayClip(0, 6, 0, 500),  # "Dòng 1"
            OverlayClip(7, p2_mid, 500, 1_800),  # straddles P0 -> P1!
            OverlayClip(p2_mid + 1, text.rindex("\n\n"), 1_800, 2_500),  # rest of P1
            OverlayClip(p3_start, len(text), 2_500, 3_500),  # P2
        ]

        xhtml_bytes, targets = build_chapter_xhtml(title, text, clips, language="vi")
        assert b"<!doctype" not in xhtml_bytes.lower()
        root = ET.fromstring(xhtml_bytes)
        assert root.tag == f"{{{_XHTML_NS}}}html"
        assert root.get("lang") == "vi"

        body = root.find(f"{{{_XHTML_NS}}}body")
        assert body is not None
        h1 = body.find(f"{{{_XHTML_NS}}}h1")
        assert h1 is not None and h1.text == title and h1.get("id") is None

        paragraphs = body.findall(f"{{{_XHTML_NS}}}p")
        assert len(paragraphs) == 3

        # Internal \n became <br/>
        assert paragraphs[0].find(f".//{{{_XHTML_NS}}}br") is not None

        # All 4 clips got unique IDs p1..p4 strictly inside <p>, never duplicating id across <p>
        assert [span_id for span_id, _ in targets] == ["p1", "p2", "p3", "p4"]
        all_ids = [el.get("id") for el in body.iter() if el.get("id") is not None]
        assert all_ids == ["p1", "p2", "p3", "p4"]

        # Full paragraph text preserved verbatim (including straddling continuation in P1)
        assert "".join(paragraphs[0].itertext()) == "Dòng 1Dòng 2 kết thúc."
        assert "".join(paragraphs[1].itertext()) == "Đoạn hai bắt đầu ở đây."
        assert "".join(paragraphs[2].itertext()) == "Đoạn ba độc lập."

        # Build SMIL from emitted targets
        smil_bytes = build_chapter_smil(1, targets, audio_duration_ms=3_500)
        assert b"<!doctype" not in smil_bytes.lower()
        smil_root = ET.fromstring(smil_bytes)
        assert smil_root.tag == f"{{{_SMIL_NS}}}smil"
        pars = smil_root.findall(f".//{{{_SMIL_NS}}}par")
        assert len(pars) == 4
        assert [p.find(f"{{{_SMIL_NS}}}text").get("src") for p in pars] == [  # type: ignore[union-attr]
            "c1.xhtml#p1",
            "c1.xhtml#p2",
            "c1.xhtml#p3",
            "c1.xhtml#p4",
        ]

    def test_strips_illegal_xml_control_chars_and_preserves_unclipped_text(self) -> None:
        text = "Trước \x00 clip.\n\nGiữa không có clip.\n\nSau clip."
        clips = [OverlayClip(0, text.index("\n\n"), 0, 1_000)]
        xhtml_bytes, targets = build_chapter_xhtml("Tiêu đề\x07", text, clips)
        root = ET.fromstring(xhtml_bytes)
        body = root.find(f"{{{_XHTML_NS}}}body")
        assert body is not None
        ps = body.findall(f"{{{_XHTML_NS}}}p")
        assert len(ps) == 3
        assert "".join(ps[1].itertext()) == "Giữa không có clip."
        assert "".join(ps[2].itertext()) == "Sau clip."
        assert len(targets) == 1

    def test_smil_builder_clamps_out_of_range_and_non_monotonic_clips(self) -> None:
        targets = [
            ("p1", OverlayClip(0, 5, -100, 600)),
            ("p2", OverlayClip(6, 10, 400, 500)),  # starts & ends behind p1
            ("p3", OverlayClip(11, 15, 1_500, 2_000)),  # past duration_ms=1000
        ]
        smil = build_chapter_smil(2, targets, audio_duration_ms=1_000)
        root = ET.fromstring(smil)
        pars = root.findall(f".//{{{_SMIL_NS}}}par")
        assert len(pars) == 3
        prev_b = -1.0
        prev_e = -1.0
        for par in pars:
            audio = par.find(f"{{{_SMIL_NS}}}audio")
            assert audio is not None
            b = _parse_smil_clock_ms(audio.get("clipBegin") or "")
            e = _parse_smil_clock_ms(audio.get("clipEnd") or "")
            assert 0.0 <= b < 1_000.0
            assert b <= e <= 1_000.0
            assert b >= prev_b and e >= prev_e
            prev_b, prev_e = b, e


class TestCoalesceBookClips:
    def test_under_budget_keeps_one_par_per_clip(self) -> None:
        text = "Câu một. Câu hai.\n\nCâu ba."
        clips = (
            OverlayClip(0, 8, 0, 1_000),
            OverlayClip(9, 17, 1_000, 2_000),
            OverlayClip(19, 26, 2_000, 3_000),
        )
        result = coalesce_book_clips(
            [ChapterOverlayInput(chapter_num=1, text=text, clips=clips, audio_duration_ms=3_000)],
            fixed_xml_bytes=2_000,
        )
        assert result == [list(clips)]

    def test_coalesces_12000_segments_within_booksreader_caps(self) -> None:
        # 20 chapters x 600 paragraphs x 1 segment/paragraph = 12,000 segments
        # (forces BOTH paragraph pass AND uniform stride pass!)
        inputs: list[ChapterOverlayInput] = []
        for ch_num in range(1, 21):
            paras: list[str] = []
            clips: list[OverlayClip] = []
            cursor = 0
            for p_idx in range(600):
                sentence = f"Đoạn {p_idx + 1} của chương {ch_num}."
                if p_idx > 0:
                    cursor += 2  # "\n\n"
                start = cursor
                end = start + len(sentence)
                paras.append(sentence)
                clips.append(OverlayClip(start, end, p_idx * 500, (p_idx + 1) * 500))
                cursor = end
            inputs.append(
                ChapterOverlayInput(
                    chapter_num=ch_num,
                    text="\n\n".join(paras),
                    clips=tuple(clips),
                    audio_duration_ms=600 * 500,
                )
            )

        fixed_bytes = 4_000
        coalesced = coalesce_book_clips(inputs, fixed_xml_bytes=fixed_bytes)

        total_pars = sum(len(c) for c in coalesced)
        assert 0 < total_pars <= MAX_SAFE_PARS <= BOOKSREADER_MAX_PARS

        smil_bytes = 0
        for inp, ch_clips in zip(inputs, coalesced, strict=True):
            assert ch_clips[0].char_start == inp.clips[0].char_start
            assert ch_clips[-1].char_end == inp.clips[-1].char_end
            assert ch_clips[0].start_ms == inp.clips[0].start_ms
            assert ch_clips[-1].end_ms == inp.clips[-1].end_ms
            targets = [(f"p{i}", clip) for i, clip in enumerate(ch_clips, start=1)]
            smil_bytes += len(build_chapter_smil(inp.chapter_num, targets, inp.audio_duration_ms))

        assert fixed_bytes + smil_bytes <= MAX_SAFE_XML_BYTES <= BOOKSREADER_MAX_XML_BYTES

    def test_paragraph_coalescing_resolves_multi_segment_paragraphs_before_stride(self) -> None:
        # 10 chapters x 200 paragraphs x 3 segments/paragraph = 6,000 segments (> 4,000)
        # Paragraph coalescing alone brings it to 2,000 <= 4,000 without needing stride!
        inputs: list[ChapterOverlayInput] = []
        for ch_num in range(1, 11):
            paras: list[str] = []
            clips: list[OverlayClip] = []
            cursor = 0
            t_ms = 0
            for p_idx in range(200):
                if p_idx > 0:
                    cursor += 2
                s1 = f"A{p_idx}."
                s2 = f"B{p_idx}."
                s3 = f"C{p_idx}."
                para = f"{s1} {s2} {s3}"
                paras.append(para)
                c1_start = cursor
                c1_end = c1_start + len(s1)
                c2_start = c1_end + 1
                c2_end = c2_start + len(s2)
                c3_start = c2_end + 1
                c3_end = c3_start + len(s3)
                clips.append(OverlayClip(c1_start, c1_end, t_ms, t_ms + 300))
                clips.append(OverlayClip(c2_start, c2_end, t_ms + 300, t_ms + 600))
                clips.append(OverlayClip(c3_start, c3_end, t_ms + 600, t_ms + 900))
                t_ms += 900
                cursor += len(para)
            inputs.append(
                ChapterOverlayInput(
                    chapter_num=ch_num,
                    text="\n\n".join(paras),
                    clips=tuple(clips),
                    audio_duration_ms=t_ms,
                )
            )

        coalesced = coalesce_book_clips(inputs, fixed_xml_bytes=2_000)
        assert [len(c) for c in coalesced] == [200] * 10

    def test_xml_byte_budget_triggers_coalescing_even_when_pars_below_cap(self) -> None:
        # 500 pars (< 4,000), ~50 KB of SMIL, but fixed_xml_bytes=680,000 -> total > 700,000
        paras = [f"P{i}." for i in range(500)]
        clips: list[OverlayClip] = []
        cursor = 0
        for i, p in enumerate(paras):
            if i > 0:
                cursor += 2
            clips.append(OverlayClip(cursor, cursor + len(p), i * 100, (i + 1) * 100))
            cursor += len(p)
        inp = ChapterOverlayInput(
            chapter_num=1, text="\n\n".join(paras), clips=tuple(clips), audio_duration_ms=50_000
        )
        fixed_bytes = 680_000
        coalesced = coalesce_book_clips([inp], fixed_xml_bytes=fixed_bytes)
        targets = [(f"p{i}", clip) for i, clip in enumerate(coalesced[0], start=1)]
        smil_len = len(build_chapter_smil(1, targets, inp.audio_duration_ms))
        assert len(coalesced[0]) < 500
        assert fixed_bytes + smil_len <= MAX_SAFE_XML_BYTES <= BOOKSREADER_MAX_XML_BYTES


class TestExportEpubMediaOverlays:
    def test_full_multichapter_export_metadata_cover_and_fallback_timeline(
        self, tmp_path: Path
    ) -> None:
        lib = AudiobookLibrary(tmp_path / "lib")
        ch1_text = "Xin chào Việt Nam.\nDòng xuống dòng.\n\nĐoạn thứ hai của chương một."
        ch2_text = "Chương hai không có tệp timeline.\n\nVẫn phải ước lượng chuẩn xác."
        book = EpubBook(
            title="Truyện Kiều & Thi Ca",
            author="Nguyễn Du; Tác Giả Phụ",
            authors=("Nguyễn Du", "Tác Giả Phụ"),
            language="vi",
            isbn="9780306406157",
            year=1820,
            description="Tác phẩm kinh điển tiếng Việt.",
            cover_bytes=_MINIMAL_JPEG,
            cover_ext="jpg",
            chapters=[
                EpubChapter(index=0, title="Chương 1: Mở đầu", text=ch1_text),
                EpubChapter(index=1, title="Chương 2: Tiếp theo", text=ch2_text),
            ],
            source_path="/books/kieu.epub",
            content_hash="a" * 64,
        )
        record = lib.add_book(book)

        # Chapter 0: WAV + explicit timeline; Chapter 1: WAV only (estimate fallback)
        lib.save_chapter_audio(record.id, 0, _make_audio(0.4))
        p0_end = ch1_text.index("\n\n")
        lib.save_chapter_timeline(
            record.id,
            0,
            Timeline(
                segments=(
                    SegmentSpan(0, p0_end, 0, 200),
                    SegmentSpan(p0_end + 2, len(ch1_text), 200, 400),
                ),
                approximate=False,
            ),
        )
        lib.save_chapter_audio(record.id, 1, _make_audio(0.3))

        dest_dir = tmp_path / "export"
        progress_events: list[tuple[int, int]] = []
        out = export_epub_media_overlays(
            lib,
            record.id,
            dest_dir,
            progress=lambda done, total: progress_events.append((done, total)),
        )

        assert out == dest_dir / "Truyện Kiều & Thi Ca.epub"
        assert progress_events == [(1, 3), (2, 3), (3, 3)]
        # No leftover temp files or work dirs
        assert list(dest_dir.iterdir()) == [out]

        report = _verify_epub_archive(out, tmp_path / "extract")
        assert report["spine_ids"] == ["c1", "c2"]
        assert report["overlaid_spine_ids"] == ["c1", "c2"]

        metadata = report["metadata"]
        assert metadata.find(f"{{{_DC_NS}}}identifier").text == "urn:isbn:9780306406157"
        assert metadata.find(f"{{{_DC_NS}}}title").text == "Truyện Kiều & Thi Ca"
        creators = [el.text for el in metadata.findall(f"{{{_DC_NS}}}creator")]
        assert creators == ["Nguyễn Du", "Tác Giả Phụ"]
        assert metadata.find(f"{{{_DC_NS}}}language").text == "vi"
        assert metadata.find(f"{{{_DC_NS}}}description").text == "Tác phẩm kinh điển tiếng Việt."
        assert metadata.find(f"{{{_DC_NS}}}date").text == "1820"

        # Per-overlay and total media:duration metadata
        dur_metas = [
            el
            for el in metadata.findall(f"{{{_OPF_NS}}}meta")
            if el.get("property") == "media:duration"
        ]
        refined_refs = {el.get("refines") for el in dur_metas if el.get("refines")}
        assert refined_refs == {"#s1", "#s2"}
        book_totals = [el for el in dur_metas if not el.get("refines")]
        assert len(book_totals) == 1 and book_totals[0].text

        # Cover manifest + EPUB 2 compat meta + stylesheet active class
        with zipfile.ZipFile(out, "r") as zf:
            css_text = zf.read("EPUB/style.css").decode("utf-8")
            assert f".{MEDIA_ACTIVE_CLASS}" in css_text
        cover_item = report["manifest"]["cover"]
        assert cover_item.get("href") == "cover.jpg"
        assert cover_item.get("media-type") == "image/jpeg"
        assert "cover-image" in (cover_item.get("properties") or "").split()
        epub2_cover = [
            el
            for el in metadata.findall(f"{{{_OPF_NS}}}meta")
            if el.get("name") == "cover" and el.get("content") == "cover"
        ]
        assert len(epub2_cover) == 1

        # Round-trip readable by our own import_epub()
        reimported = import_epub(out)
        assert reimported.title == "Truyện Kiều & Thi Ca"
        assert reimported.authors == ("Nguyễn Du", "Tác Giả Phụ")
        assert reimported.isbn == "9780306406157"
        assert len(reimported.chapters) == 2

    def test_deterministic_uuid_and_default_vi_language_when_missing(self, tmp_path: Path) -> None:
        lib = AudiobookLibrary(tmp_path / "lib")
        book = EpubBook(
            title="Sách Không ISBN",
            author="",
            chapters=[EpubChapter(index=0, title="Chương 1", text="Nội dung duy nhất.")],
            source_path="/books/no_isbn.epub",
            content_hash="b" * 64,
        )
        record = lib.add_book(book)
        lib.save_chapter_audio(record.id, 0, _make_audio(0.2))

        out1 = export_epub_media_overlays(lib, record.id, tmp_path / "out1")
        out2 = export_epub_media_overlays(lib, record.id, tmp_path / "out2")

        r1 = _verify_epub_archive(out1, tmp_path / "ex1")
        r2 = _verify_epub_archive(out2, tmp_path / "ex2")

        uid1 = r1["metadata"].find(f"{{{_DC_NS}}}identifier").text
        uid2 = r2["metadata"].find(f"{{{_DC_NS}}}identifier").text
        assert uid1.startswith("urn:uuid:")
        assert uid1 == uid2
        assert r1["metadata"].find(f"{{{_DC_NS}}}language").text == "vi"

    def test_oversize_multipart_chapter_merges_audio_and_timelines(self, tmp_path: Path) -> None:
        lib = AudiobookLibrary(tmp_path / "lib")
        part1_text = "Phần một của chương dài."
        part2_text = "Phần hai của chương dài."
        full_text = f"{part1_text}\n\n{part2_text}"
        book = EpubBook(
            title="Sách Chương Dài",
            author="Tác Giả",
            chapters=[EpubChapter(index=0, title="Chương Dài", text=full_text)],
            source_path="/books/long.epub",
            content_hash="c" * 64,
        )
        record = lib.add_book(book)
        parts = [
            ChapterPart(char_start=0, char_end=len(part1_text)),
            ChapterPart(char_start=len(part1_text) + 2, char_end=len(full_text)),
        ]
        lib.save_segment_plan(record.id, 0, parts)

        for seg_idx, part_text in enumerate((part1_text, part2_text)):
            tmp_wav = tmp_path / f"seg{seg_idx}.wav"
            write_wav_file(_make_audio(0.35, freq=330.0 + seg_idx * 110.0), tmp_wav, SAMPLE_RATE)
            lib.promote_chapter_part(record.id, 0, tmp_wav, segment=seg_idx)
            lib.save_chapter_timeline(
                record.id,
                0,
                Timeline(segments=(SegmentSpan(0, len(part_text), 0, 350),), approximate=False),
                segment=seg_idx,
            )

        out = export_epub_media_overlays(lib, record.id, tmp_path / "out")
        report = _verify_epub_archive(out, tmp_path / "ex")
        assert report["overlaid_spine_ids"] == ["c1"]
        # Combined duration ~700ms (2 x 350ms)
        assert report["audio_durations_ms"]["a1.mp3"] >= 650
        assert len(report["global_anchors"]) == 2
        assert report["global_anchors"][1][1] >= 340.0

    def test_partial_render_and_zero_ready_error(self, tmp_path: Path) -> None:
        lib = AudiobookLibrary(tmp_path / "lib")
        book = EpubBook(
            title="Sách 5 Chương",
            author="Tác Giả",
            chapters=[
                EpubChapter(index=i, title=f"Chương {i + 1}", text=f"Nội dung chương {i + 1}.")
                for i in range(5)
            ],
            source_path="/books/five.epub",
            content_hash="d" * 64,
        )
        record = lib.add_book(book)

        # 0 ready chapters -> actionable AudiobookError
        with pytest.raises(AudiobookError, match="No rendered chapters"):
            export_epub_media_overlays(lib, record.id, tmp_path / "empty_out")

        # Render 2 of 5 chapters (indices 1 and 4 -> c2 and c5)
        lib.save_chapter_audio(record.id, 1, _make_audio(0.25))
        lib.save_chapter_audio(record.id, 4, _make_audio(0.25))

        out = export_epub_media_overlays(lib, record.id, tmp_path / "partial_out")
        report = _verify_epub_archive(out, tmp_path / "ex_partial")
        assert report["spine_ids"] == ["c1", "c2", "c3", "c4", "c5"]
        assert report["overlaid_spine_ids"] == ["c2", "c5"]

        # TOC lists all 5 chapters
        nav = report["xml_entries"]["EPUB/nav.xhtml"]
        toc_hrefs = [el.get("href") for el in nav.findall(f".//{{{_XHTML_NS}}}a")]
        assert toc_hrefs == ["c1.xhtml", "c2.xhtml", "c3.xhtml", "c4.xhtml", "c5.xhtml"]

        # Non-ready chapters have NO media-overlay attribute
        for cid in ("c1", "c3", "c4"):
            assert report["manifest"][cid].get("media-overlay") is None

        # Explicit ready_indices subset
        out_sub = export_epub_media_overlays(
            lib, record.id, tmp_path / "subset_out", ready_indices=[4, 99, 0, 4]
        )
        report_sub = _verify_epub_archive(out_sub, tmp_path / "ex_sub")
        assert report_sub["overlaid_spine_ids"] == ["c5"]

    def test_full_epub_budget_stress_10000_plus_segments(self, tmp_path: Path) -> None:
        lib = AudiobookLibrary(tmp_path / "lib")
        chapters: list[EpubChapter] = []
        timelines: list[Timeline] = []
        # 5 chapters x 2,100 segments = 10,500 segments total!
        for ch_idx in range(5):
            paras: list[str] = []
            spans: list[SegmentSpan] = []
            cursor = 0
            for p_idx in range(2_100):
                if p_idx > 0:
                    cursor += 2
                s = f"C{ch_idx}P{p_idx}."
                paras.append(s)
                spans.append(SegmentSpan(cursor, cursor + len(s), p_idx, p_idx + 1))
                cursor += len(s)
            chapters.append(
                EpubChapter(index=ch_idx, title=f"Chương {ch_idx + 1}", text="\n\n".join(paras))
            )
            timelines.append(Timeline(segments=tuple(spans), approximate=False))

        book = EpubBook(
            title="Bộ Sách Rất Dài",
            author="Nhiều Chương",
            chapters=chapters,
            source_path="/books/huge.epub",
            content_hash="e" * 64,
        )
        record = lib.add_book(book)
        wav = _make_audio(2.2)  # 2,200 ms > 2,100 ms timeline span
        for ch_idx, tl in enumerate(timelines):
            lib.save_chapter_audio(record.id, ch_idx, wav)
            lib.save_chapter_timeline(record.id, ch_idx, tl)

        out = export_epub_media_overlays(lib, record.id, tmp_path / "stress_out")
        report = _verify_epub_archive(out, tmp_path / "stress_ex")
        assert report["total_pars"] <= MAX_SAFE_PARS <= BOOKSREADER_MAX_PARS
        assert report["total_xml_bytes"] <= MAX_SAFE_XML_BYTES <= BOOKSREADER_MAX_XML_BYTES
        assert len(report["overlaid_spine_ids"]) == 5

    def test_cancellation_cleans_up_temp_files(self, tmp_path: Path) -> None:
        lib = AudiobookLibrary(tmp_path / "lib")
        book = EpubBook(
            title="Sách Bị Hủy",
            author="Tác Giả",
            chapters=[
                EpubChapter(index=0, title="Chương 1", text="Nội dung 1."),
                EpubChapter(index=1, title="Chương 2", text="Nội dung 2."),
            ],
            source_path="/books/cancel.epub",
            content_hash="f" * 64,
        )
        record = lib.add_book(book)
        lib.save_chapter_audio(record.id, 0, _make_audio(0.2))
        lib.save_chapter_audio(record.id, 1, _make_audio(0.2))

        dest_dir = tmp_path / "cancel_out"
        cancelled = {"flag": False}

        def on_progress(done: int, _total: int) -> None:
            if done >= 1:
                cancelled["flag"] = True

        with pytest.raises(ExportCancelled):
            export_epub_media_overlays(
                lib,
                record.id,
                dest_dir,
                progress=on_progress,
                cancel_check=lambda: cancelled["flag"],
            )

        assert not dest_dir.exists() or list(dest_dir.iterdir()) == []

    @pytest.mark.parametrize("bad_dest", ["", "   ", ".", Path(""), Path("."), '""', "file://"])
    def test_rejects_empty_dest_dir(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_dest: str | Path
    ) -> None:
        cwd = tmp_path / "cwd"
        cwd.mkdir()
        monkeypatch.chdir(cwd)
        lib = AudiobookLibrary(tmp_path / "lib")
        book = EpubBook(
            title="Sách Kiểm Tra Đường Dẫn",
            author="Tác Giả",
            chapters=[EpubChapter(index=0, title="Chương 1", text="Nội dung 1.")],
            source_path="/books/path.epub",
            content_hash="1" * 64,
        )
        record = lib.add_book(book)
        lib.save_chapter_audio(record.id, 0, _make_audio(0.2))

        with pytest.raises(AudiobookError, match="(?i)destination|empty"):
            export_epub_media_overlays(lib, record.id, bad_dest)
        assert list(cwd.iterdir()) == []

    def test_cjk_title_uses_short_uuid_temp_paths(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from vienetts_app.core import epub_export

        lib = AudiobookLibrary(tmp_path / "lib")
        cjk_title = "漢" * 80
        book = EpubBook(
            title=cjk_title,
            author="作者",
            chapters=[EpubChapter(index=0, title="第一章", text="正文内容。")],
            source_path="/books/cjk.epub",
            content_hash="2" * 64,
        )
        record = lib.add_book(book)
        lib.save_chapter_audio(record.id, 0, _make_audio(0.2))

        observed_part_names: list[str] = []
        observed_work_dirs: set[str] = set()
        orig_replace = epub_export._replace_with_lock_retry
        orig_mkdir = Path.mkdir

        def _spy_replace(src: Path, dst: Path) -> None:
            observed_part_names.append(src.name)
            orig_replace(src, dst)

        def _spy_mkdir(self: Path, *args: Any, **kwargs: Any) -> None:
            if self.name.endswith(".epubwork"):
                observed_work_dirs.add(self.name)
            orig_mkdir(self, *args, **kwargs)

        monkeypatch.setattr(epub_export, "_replace_with_lock_retry", _spy_replace)
        monkeypatch.setattr(Path, "mkdir", _spy_mkdir)

        dest_dir = tmp_path / "cjk_out"
        out = export_epub_media_overlays(lib, record.id, dest_dir)
        assert out.is_file()
        assert len(observed_part_names) == 1
        assert observed_part_names[0].endswith(".part.epub")
        assert len(observed_part_names[0].encode("utf-8")) < 64
        assert "漢" not in observed_part_names[0]
        assert len(observed_work_dirs) == 1
        work_dir_name = next(iter(observed_work_dirs))
        assert work_dir_name.endswith(".epubwork")
        assert len(work_dir_name.encode("utf-8")) < 64
        assert "漢" not in work_dir_name
