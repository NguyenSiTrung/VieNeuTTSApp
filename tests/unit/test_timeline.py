"""core/timeline.py (FR-A9): pure audio↔text alignment math.

Covers word/paragraph spans, lock-step segment offset mapping (the streaming
splitter collapses whitespace, so segment text is not a verbatim substring),
measured + estimated timelines, locate/seek helpers and JSON round-trips.
"""

from __future__ import annotations

import pytest

from vienetts_app.core.engine import split_text_for_streaming
from vienetts_app.core.timeline import (
    TIMELINE_VERSION,
    SegmentSpan,
    Timeline,
    active_word,
    build_timeline,
    estimate_timeline,
    locate_segment,
    map_segment_offsets,
    paragraph_start_ms,
    split_paragraphs,
    timeline_from_json,
    timeline_to_json,
    word_spans,
)

# "Câu một." (0..8), blank line (8..10), "Câu hai." (10..18). Word tokens:
# "Câu"(0,3) "một."(4,8) "Câu"(10,13) "hai."(14,18).
TWO_PARAS = "Câu một.\n\nCâu hai."


# ── word / paragraph spans ────────────────────────────────────────────────────


def test_word_spans_skips_whitespace_runs():
    assert word_spans(TWO_PARAS) == [(0, 3), (4, 8), (10, 13), (14, 18)]


def test_word_spans_empty_and_blank_text():
    assert word_spans("") == []
    assert word_spans("  \n\n  ") == []


def test_word_spans_keeps_diacritics_and_punctuation():
    spans = word_spans("Xin chào, thế giới!")
    assert spans == [(0, 3), (4, 9), (10, 13), (14, 19)]


def test_active_word_containing_next_and_clamped():
    spans = word_spans(TWO_PARAS)
    assert active_word(spans, 2) == (0, 3)  # inside the first "Câu"
    assert active_word(spans, 5) == (4, 8)  # inside "một."
    assert active_word(spans, 9) == (10, 13)  # the "\n\n" gap → next word
    assert active_word(spans, 0) == (0, 3)
    assert active_word(spans, 18) == (14, 18)  # past the end → last word
    assert active_word([], 3) == (-1, -1)


def test_active_word_accepts_precomputed_starts():
    # The playback-tick fast path: identical results with the caller-held
    # starts key (no per-tick rebuild over a whole chapter).
    spans = word_spans(TWO_PARAS)
    starts = [span[0] for span in spans]
    for char_index in range(0, 20):
        assert active_word(spans, char_index, starts=starts) == active_word(spans, char_index)


def test_split_paragraphs_offsets_survive_stripping():
    text = "  Câu một.  \n\n\nCâu hai.\n\n  \n\nBa."
    paragraphs = split_paragraphs(text)
    assert [p["index"] for p in paragraphs] == [0, 1, 2]
    assert [p["text"] for p in paragraphs] == ["Câu một.", "Câu hai.", "Ba."]
    first, second, third = paragraphs
    assert (first["charStart"], first["charEnd"]) == (2, 10)
    assert (second["charStart"], second["charEnd"]) == (15, 23)
    assert (third["charStart"], third["charEnd"]) == (29, 32)
    for p in paragraphs:
        assert text[p["charStart"] : p["charEnd"]] == p["text"]


def test_split_paragraphs_single_paragraph():
    assert split_paragraphs("Chỉ một đoạn.") == [
        {"index": 0, "text": "Chỉ một đoạn.", "charStart": 0, "charEnd": 13}
    ]


# ── segment offset mapping ────────────────────────────────────────────────────


def test_map_segment_offsets_separate_segments():
    assert map_segment_offsets(TWO_PARAS, ["Câu một.", "Câu hai."]) == [(0, 8), (10, 18)]


def test_map_segment_offsets_packed_segment_spans_paragraph_break():
    # The splitter joins packed units with a space where the chapter text has
    # "\n\n" — offsets must still resolve into the chapter coordinates.
    assert map_segment_offsets(TWO_PARAS, ["Câu một. Câu hai."]) == [(0, 18)]


def test_map_segment_offsets_matches_the_real_splitter():
    text = (
        "Đoạn đầu tiên. Câu thứ hai vẫn ở đây!\n\nĐoạn thứ hai; dài hơn một chút. "
        "Và một câu cuối cùng không có dấu chấm"
    )
    segments = split_text_for_streaming(text)
    offsets = map_segment_offsets(text, segments)
    # Spans are monotone, inside the text, and cover every word of the text.
    assert offsets == sorted(offsets)
    assert offsets[0][0] == 0
    assert offsets[-1][1] == len(text)
    for start, end in offsets:
        assert 0 <= start < end <= len(text)
        assert not text[start].isspace() and not text[end - 1].isspace()


def test_map_segment_offsets_hard_split_prefix_token():
    # A >cap unit is hard-split mid-token: the segment token is a PREFIX of
    # the chapter token, and lock-step consumption maps it to the WHOLE
    # chapter token; the remainder segment then runs out of tokens and gets
    # the "no highlight" placeholder. Exact spans for every normal word,
    # graceful degradation for a >512-char token.
    assert map_segment_offsets("aaaa bbbb", ["aaaa bbb", "b"]) == [(0, 9), (-1, -1)]


def test_map_segment_offsets_empty_segment_list():
    assert map_segment_offsets(TWO_PARAS, []) == []
    assert map_segment_offsets(TWO_PARAS, [""]) == [(-1, -1)]


# ── measured timeline ─────────────────────────────────────────────────────────


def test_build_timeline_cumulative_ms():
    timeline = build_timeline(TWO_PARAS, ["Câu một.", "Câu hai."], [48_000, 96_000], 48_000)
    assert timeline.approximate is False
    assert timeline.segments == (
        SegmentSpan(0, 8, 0, 1000),
        SegmentSpan(10, 18, 1000, 3000),
    )


def test_build_timeline_zero_sample_segment_keeps_contiguous_time():
    timeline = build_timeline(TWO_PARAS, ["Câu một.", "Câu hai."], [48_000, 0], 48_000)
    assert (timeline.segments[1].start_ms, timeline.segments[1].end_ms) == (1000, 1000)


def test_build_timeline_validates_inputs():
    with pytest.raises(ValueError):
        build_timeline(TWO_PARAS, ["a"], [1, 2], 48_000)  # length mismatch
    with pytest.raises(ValueError):
        build_timeline(TWO_PARAS, ["a"], [1], 0)  # bad sample rate


# ── estimated timeline ────────────────────────────────────────────────────────


def test_estimate_timeline_proportional_allocation():
    timeline = estimate_timeline(TWO_PARAS, 8000, ["Câu một.", "Câu hai."])
    assert timeline.approximate is True
    # Weights 8 vs 8 → even split; last span closes exactly at the duration.
    assert timeline.segments[0] == SegmentSpan(0, 8, 0, 4000)
    assert timeline.segments[1] == SegmentSpan(10, 18, 4000, 8000)


def test_estimate_timeline_default_segments_use_the_real_splitter():
    timeline = estimate_timeline(TWO_PARAS, 4000)
    assert len(timeline.segments) == len(split_text_for_streaming(TWO_PARAS))
    assert timeline.segments[-1].end_ms == 4000


def test_estimate_timeline_degenerate_inputs():
    assert estimate_timeline("", 1000).segments == ()
    assert estimate_timeline(TWO_PARAS, 0).segments == ()


# ── locating ──────────────────────────────────────────────────────────────────


def _two_span_timeline() -> Timeline:
    return build_timeline(TWO_PARAS, ["Câu một.", "Câu hai."], [48_000, 96_000], 48_000)


def test_locate_segment_inside_boundaries_and_clamped():
    timeline = _two_span_timeline()
    assert locate_segment(timeline, 0) == 0
    assert locate_segment(timeline, 999) == 0
    assert locate_segment(timeline, 1000) == 1
    assert locate_segment(timeline, 2999) == 1
    assert locate_segment(timeline, 999_999) == 1  # past the end → last
    assert locate_segment(timeline, -5) == 0  # before the start → first


def test_locate_segment_skips_zero_duration_spans():
    timeline = build_timeline(TWO_PARAS, ["Câu một.", "Câu hai."], [0, 48_000], 48_000)
    assert locate_segment(timeline, 0) == 1  # first span is empty → the next one
    assert locate_segment(_two_span_timeline().__class__(()), 5) == -1


def test_locate_segment_empty_timeline():
    assert locate_segment(Timeline(()), 100) == -1


def test_paragraph_start_ms_finds_first_overlapping_segment():
    timeline = _two_span_timeline()
    assert paragraph_start_ms(timeline, 0) == 0
    assert paragraph_start_ms(timeline, 10) == 1000
    assert paragraph_start_ms(timeline, 18) == -1  # nothing ends after this char


# ── JSON round-trip ───────────────────────────────────────────────────────────


def test_timeline_json_round_trip():
    timeline = _two_span_timeline()
    restored = timeline_from_json(timeline_to_json(timeline))
    assert restored == timeline


def test_timeline_json_round_trip_approximate_flag():
    timeline = estimate_timeline(TWO_PARAS, 8000)
    restored = timeline_from_json(timeline_to_json(timeline))
    assert restored is not None and restored.approximate is True


def test_timeline_from_json_rejects_bad_payloads():
    assert timeline_from_json(None) is None
    assert timeline_from_json("nope") is None
    assert timeline_from_json({"version": TIMELINE_VERSION, "segments": "x"}) is None
    assert timeline_from_json({"version": 99, "segments": []}) is None
    assert timeline_from_json({"version": TIMELINE_VERSION, "segments": [{"charStart": 0}]}) is None
