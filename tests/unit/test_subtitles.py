"""core/subtitles.py: SubRip cue parsing, cleaning and re-emission.

The cue model is shared by two consumers with different needs — the document
importer (spoken text, timecodes discarded) and the SRT aligner (timecodes as
data) — so these tests pin the tolerance rules both rely on: hand-edited files
parse, styling is never spoken, and char offsets match the joined text exactly.
"""

from __future__ import annotations

import pytest

from vienetts_app.core.subtitles import (
    Cue,
    SubtitleError,
    clean_cue_text,
    cue_char_spans,
    cues_text,
    format_srt,
    format_timestamp,
    parse_cues,
    parse_timestamp,
    read_cues,
)

SAMPLE = (
    "1\n"
    "00:00:00,000 --> 00:00:02,000\n"
    "Hello from the subtitle fixture.\n"
    "\n"
    "2\n"
    "00:00:02,500 --> 00:00:04,000\n"
    "Xin chào <i>thế giới</i>.\n"
    "Dòng thứ hai.\n"
    "\n"
    "3\n"
    "00:00:05,000 --> 00:00:06,500\n"
    "Third cue with <b>bold</b> and {\\an8}pos.\n"
)


# ── timestamps ───────────────────────────────────────────────────────────────


def test_parse_timestamp_forms():
    assert parse_timestamp("00:00:00,000") == 0
    assert parse_timestamp("01:02:03,004") == 3_723_004
    assert parse_timestamp("00:00:01.5") == 1_500  # 1-3 fractional digits, '.' decimal
    assert parse_timestamp("  00:00:02,250  ") == 2_250
    assert parse_timestamp("1:00:00,000") == 3_600_000  # unpadded hour
    # A full cue line is NOT a single stamp (callers use the pair regex for that).
    assert parse_timestamp("00:00:01,000 --> 00:00:02,000") is None
    assert parse_timestamp("nonsense") is None


def test_format_timestamp_round_trips_and_never_wraps_hours():
    for ms in (0, 1, 999, 1_000, 3_723_004, 359_999_999):
        assert parse_timestamp(format_timestamp(ms)) == ms
    assert format_timestamp(-5) == "00:00:00,000"
    assert format_timestamp(360_000_000) == "100:00:00,000"


# ── cue parsing ──────────────────────────────────────────────────────────────


def test_parse_cues_strips_sequence_numbers_stamps_and_styling():
    cues = parse_cues(SAMPLE)
    assert cues == [
        Cue(1, 0, 2_000, "Hello from the subtitle fixture."),
        Cue(2, 2_500, 4_000, "Xin chào thế giới. Dòng thứ hai."),
        Cue(3, 5_000, 6_500, "Third cue with bold and pos."),
    ]


def test_parse_cues_tolerates_hand_edited_files():
    # No sequence numbers, CRLF endings, a '.' decimal, a stray blank block and
    # a trailing position field on the stamp line.
    source = (
        "00:00:01.000 --> 00:00:02,000  X1:1 X2:2\r\n"
        "Câu một.\r\n"
        "\r\n"
        "\r\n"
        "00:00:03,000 --> 00:00:04,000\r\n"
        "Câu hai.\r\n"
    )
    assert parse_cues(source) == [
        Cue(1, 1_000, 2_000, "Câu một."),
        Cue(2, 3_000, 4_000, "Câu hai."),
    ]


def test_parse_cues_renumbers_skips_and_clamps():
    source = (
        "7\n00:00:00,000 --> 00:00:01,000\nGiữ lại.\n\n"
        "not a cue block at all\n\n"
        "9\n00:00:02,000 --> 00:00:03,000\n\n"  # blank body → dropped
        "11\n00:00:05,000 --> 00:00:04,000\nĐảo ngược.\n"  # end < start → clamped
    )
    assert parse_cues(source) == [
        Cue(1, 0, 1_000, "Giữ lại."),
        Cue(2, 5_000, 5_000, "Đảo ngược."),
    ]


def test_parse_cues_never_speaks_a_blank_separated_junk_block():
    # Metadata/comments stranded between cues in their own blank-separated
    # block are NOT spoken — recovery covers missing separators, not blocks
    # that were deliberately separated.
    source = (
        "1\n00:00:00,000 --> 00:00:02,000\nCâu một.\n\n"
        "NOTE: translated by hand, do not read aloud\n\n"
        "2\n00:00:02,500 --> 00:00:04,000\nCâu hai.\n"
    )
    assert parse_cues(source) == [
        Cue(1, 0, 2_000, "Câu một."),
        Cue(2, 2_500, 4_000, "Câu hai."),
    ]


def test_parse_cues_recovers_missing_blank_separators():
    # No blank lines between blocks: each "-->" line still starts a new cue
    # and the following cue's sequence number is never spoken.
    source = (
        "1\n00:00:00,000 --> 00:00:02,000\nCâu một.\n"
        "2\n00:00:02,500 --> 00:00:04,000\nCâu hai.\n"
    )
    assert parse_cues(source) == [
        Cue(1, 0, 2_000, "Câu một."),
        Cue(2, 2_500, 4_000, "Câu hai."),
    ]


def test_parse_cues_recovers_unnumbered_cues_without_blank_lines():
    source = (
        "00:00:00,000 --> 00:00:02,000\nCâu một.\n"
        "00:00:02,500 --> 00:00:04,000\nCâu hai.\n"
    )
    assert parse_cues(source) == [
        Cue(1, 0, 2_000, "Câu một."),
        Cue(2, 2_500, 4_000, "Câu hai."),
    ]


def test_parse_cues_rejects_a_malformed_arrow_line_with_its_number():
    # A line containing "-->" that does not parse as a timecode pair is a
    # corrupt file, not a cue to guess at: the error names the 1-based line.
    source = (
        "1\n00:00:00,000 --> 00:00:02,000\nCâu một.\n\n"
        "2\n00:00:02,500 --> 00:00:04\nCâu hai.\n"
    )
    with pytest.raises(SubtitleError) as excinfo:
        parse_cues(source)
    assert "line 6" in str(excinfo.value)
    assert ".srt" in str(excinfo.value)


def test_parse_cues_never_speaks_timestamps_or_sequence_lines():
    for cue in parse_cues(SAMPLE):
        assert "-->" not in cue.text
        assert not cue.text.isdigit()
        assert parse_timestamp(cue.text) is None


def test_parse_cues_empty_and_cue_less_inputs():
    assert parse_cues("") == []
    assert parse_cues("   \n\n  ") == []
    assert parse_cues("just prose\nwith no timecodes\n") == []


def test_clean_cue_text_collapses_whitespace_and_drops_overrides():
    assert clean_cue_text("a <i>b</i>\n  c   d  ") == "a b c d"
    assert clean_cue_text("{\\an8}line") == "line"
    assert clean_cue_text("<font color='#fff'>x</font>") == "x"


def test_cue_validates_its_own_fields():
    with pytest.raises(ValueError):
        Cue(0, 0, 1_000, "x")  # index must be >= 1
    with pytest.raises(ValueError):
        Cue(1, -1, 1_000, "x")
    with pytest.raises(ValueError):
        Cue(1, 2_000, 1_000, "x")
    with pytest.raises(ValueError):
        Cue(1, 0, 1_000, "   ")
    assert Cue(1, 1_000, 3_000, "x").duration_ms == 2_000


# ── joined text + offsets ────────────────────────────────────────────────────


def test_cues_text_and_char_spans_are_one_coordinate_system():
    cues = parse_cues(SAMPLE)
    text = cues_text(cues)
    spans = cue_char_spans(cues)
    assert spans == [(0, 32), (33, 65), (66, 94)]
    for cue, (start, end) in zip(cues, spans, strict=True):
        assert text[start:end] == cue.text
    # The separator between cues is exactly one newline.
    assert text[32] == "\n" and text[65] == "\n"


def test_cues_text_and_spans_empty():
    assert cues_text([]) == ""
    assert cue_char_spans([]) == []


# ── re-emission + disk ───────────────────────────────────────────────────────


def test_format_srt_round_trips_through_the_parser():
    cues = parse_cues(SAMPLE)
    assert parse_cues(format_srt(cues)) == cues
    assert format_srt([]) == "\n"


def test_read_cues_from_disk_and_bom(tmp_path):
    path = tmp_path / "a.srt"
    path.write_bytes(b"\xef\xbb\xbf" + SAMPLE.encode("utf-8"))
    assert read_cues(path) == parse_cues(SAMPLE)
    assert read_cues(str(path)) == parse_cues(SAMPLE)


def test_read_cues_refuses_undecodable_bytes(tmp_path):
    path = tmp_path / "bad.srt"
    path.write_bytes(b"\xff\xfe\x00\x00not utf-8")
    with pytest.raises(SubtitleError) as excinfo:
        read_cues(path)
    assert "UTF-8" in str(excinfo.value)
