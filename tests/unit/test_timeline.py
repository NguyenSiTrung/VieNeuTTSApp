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


def test_word_spans_and_positions() -> None:
    assert word_spans(TWO_PARAS) == [(0, 3), (4, 8), (10, 13), (14, 18)]
    # Empty and blank text produce no spans.
    assert word_spans("") == []
    assert word_spans("  \n\n  ") == []
    # Diacritics and punctuation are preserved.
    assert word_spans("Xin chào, thế giới!") == [(0, 3), (4, 9), (10, 13), (14, 19)]

    spans = word_spans(TWO_PARAS)
    assert active_word(spans, 2) == (0, 3)  # inside the first "Câu"
    assert active_word(spans, 5) == (4, 8)  # inside "một."
    assert active_word(spans, 9) == (10, 13)  # the "\n\n" gap → next word
    assert active_word(spans, 0) == (0, 3)
    assert active_word(spans, 18) == (14, 18)  # past the end → last word
    assert active_word([], 3) == (-1, -1)

    # The playback-tick fast path: identical results with the caller-held
    # starts key (no per-tick rebuild over a whole chapter).
    spans = word_spans(TWO_PARAS)
    starts = [span[0] for span in spans]
    for char_index in range(0, 20):
        assert active_word(spans, char_index, starts=starts) == active_word(spans, char_index)


def test_split_paragraphs_offsets_and_single_paragraph():
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

    assert split_paragraphs("Chỉ một đoạn.") == [
        {"index": 0, "text": "Chỉ một đoạn.", "charStart": 0, "charEnd": 13}
    ]


# ── segment offset mapping ────────────────────────────────────────────────────


def test_segment_offset_mapping() -> None:
    assert map_segment_offsets(TWO_PARAS, ["Câu một.", "Câu hai."]) == [(0, 8), (10, 18)]
    assert map_segment_offsets(TWO_PARAS, []) == []
    assert map_segment_offsets(TWO_PARAS, [""]) == [(-1, -1)]

    # The splitter joins packed units with a space where the chapter text has
    # "\n\n" — offsets must still resolve into the chapter coordinates.
    assert map_segment_offsets(TWO_PARAS, ["Câu một. Câu hai."]) == [(0, 18)]

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

    # A >cap unit is hard-split mid-token: character lock-step scanning aligns
    # both the prefix and the remainder to their exact character spans without
    # dropping or drifting subsequent words.
    assert map_segment_offsets("aaaa bbbb", ["aaaa bbb", "b"]) == [(0, 8), (8, 9)]
    assert map_segment_offsets("aaaa bbbb cccc", ["aaaa bbb", "b", "cccc"]) == [
        (0, 8),
        (8, 9),
        (10, 14),
    ]


def test_map_segment_offsets_hard_split_long_paragraph_no_drift() -> None:
    long_token = "a" * 600 + "b" * 600
    text = f"Đầu đoạn. {long_token} Cuối đoạn thật sự."
    segments = split_text_for_streaming(text, max_chars=512)
    assert len(segments) >= 3
    offsets = map_segment_offsets(text, segments)

    assert len(offsets) == len(segments)
    assert all(start >= 0 and end > start for start, end in offsets)
    assert offsets == sorted(offsets)
    assert offsets[0][0] == 0
    assert offsets[-1][1] == len(text)
    for seg, (start, end) in zip(segments, offsets, strict=True):
        assert "".join(text[start:end].split()) == "".join(seg.split())
        assert not text[start].isspace() and not text[end - 1].isspace()


def test_map_segment_offsets_spaceless_cjk_and_whitespace_variants() -> None:
    from vienetts_app.core.text_segmentation import split_text_for_profile

    # Spaceless Chinese paragraph split at CJK full stops.
    zh_text = "第一句话很长。第二句话也不短！第三句话在这里？第四句话结束。"
    zh_segments = split_text_for_profile(zh_text, language="zh", max_chars=14)
    assert len(zh_segments) >= 3
    zh_offsets = map_segment_offsets(zh_text, zh_segments)
    assert zh_offsets == sorted(zh_offsets)
    assert zh_offsets[0][0] == 0
    assert zh_offsets[-1][1] == len(zh_text)
    for seg, (start, end) in zip(zh_segments, zh_offsets, strict=True):
        assert zh_text[start:end] == seg

    # Spaceless Japanese paragraph hard-split mid-sentence without punctuation.
    ja_text = "あいうえおかきくけこ" * 60
    ja_segments = split_text_for_profile(ja_text, language="ja", max_chars=256)
    assert len(ja_segments) == 3
    ja_offsets = map_segment_offsets(ja_text, ja_segments)
    assert ja_offsets == [(0, 256), (256, 512), (512, 600)]
    for seg, (start, end) in zip(ja_segments, ja_offsets, strict=True):
        assert ja_text[start:end] == seg

    # Normalized whitespace variants (tabs, CRLF, multiple spaces, fullwidth space).
    ws_text = "  Xin\t\tchào\r\n\r\nthế\u3000giới!  "
    assert map_segment_offsets(ws_text, ["Xin chào", "thế giới!"]) == [(2, 11), (15, 24)]
    assert map_segment_offsets(ws_text, ["Xin chào thế giới!"]) == [(2, 24)]

    # Unmappable segment returns (-1, -1) without poisoning subsequent segments.
    assert map_segment_offsets("alpha beta gamma", ["alpha", "MISSING", "gamma"]) == [
        (0, 5),
        (-1, -1),
        (11, 16),
    ]
    assert map_segment_offsets("", ["anything"]) == [(-1, -1)]
    assert map_segment_offsets("alpha", ["   \n\t "]) == [(-1, -1)]


# ── measured timeline ─────────────────────────────────────────────────────────


def test_timeline_build_and_estimate() -> None:
    timeline = build_timeline(TWO_PARAS, ["Câu một.", "Câu hai."], [48_000, 96_000], 48_000)
    assert timeline.approximate is False
    assert timeline.segments == (
        SegmentSpan(0, 8, 0, 1000),
        SegmentSpan(10, 18, 1000, 3000),
    )

    # A zero-sample segment keeps the timeline contiguous.
    timeline = build_timeline(TWO_PARAS, ["Câu một.", "Câu hai."], [48_000, 0], 48_000)
    assert (timeline.segments[1].start_ms, timeline.segments[1].end_ms) == (1000, 1000)

    with pytest.raises(ValueError):
        build_timeline(TWO_PARAS, ["a"], [1, 2], 48_000)  # length mismatch
    with pytest.raises(ValueError):
        build_timeline(TWO_PARAS, ["a"], [1], 0)  # bad sample rate

    timeline = estimate_timeline(TWO_PARAS, 8000, ["Câu một.", "Câu hai."])
    assert timeline.approximate is True
    # Weights 8 vs 8 → even split; last span closes exactly at the duration.
    assert timeline.segments[0] == SegmentSpan(0, 8, 0, 4000)
    assert timeline.segments[1] == SegmentSpan(10, 18, 4000, 8000)

    assert estimate_timeline("", 1000).segments == ()
    assert estimate_timeline(TWO_PARAS, 0).segments == ()

    timeline = estimate_timeline(TWO_PARAS, 4000)
    assert len(timeline.segments) == len(split_text_for_streaming(TWO_PARAS))
    assert timeline.segments[-1].end_ms == 4000


# ── locating ──────────────────────────────────────────────────────────────────


def _two_span_timeline() -> Timeline:
    return build_timeline(TWO_PARAS, ["Câu một.", "Câu hai."], [48_000, 96_000], 48_000)


def test_timeline_lookup_and_json() -> None:
    timeline = _two_span_timeline()
    assert locate_segment(timeline, 0) == 0
    assert locate_segment(timeline, 999) == 0
    assert locate_segment(timeline, 1000) == 1
    assert locate_segment(timeline, 2999) == 1
    assert locate_segment(timeline, 999_999) == 1  # past the end → last
    assert locate_segment(timeline, -5) == 0  # before the start → first

    timeline = build_timeline(TWO_PARAS, ["Câu một.", "Câu hai."], [0, 48_000], 48_000)
    assert locate_segment(timeline, 0) == 1  # first span is empty → the next one
    assert locate_segment(Timeline(()), 100) == -1

    timeline = _two_span_timeline()
    assert paragraph_start_ms(timeline, 0) == 0
    assert paragraph_start_ms(timeline, 10) == 1000
    assert paragraph_start_ms(timeline, 18) == -1  # nothing ends after this char

    timeline = _two_span_timeline()
    restored = timeline_from_json(timeline_to_json(timeline))
    assert restored == timeline

    estimated = estimate_timeline(TWO_PARAS, 8000)
    restored = timeline_from_json(timeline_to_json(estimated))
    assert restored is not None and restored.approximate is True

    assert timeline_from_json(None) is None
    assert timeline_from_json("nope") is None
    assert timeline_from_json({"version": TIMELINE_VERSION, "segments": "x"}) is None
    assert timeline_from_json({"version": 99, "segments": []}) is None
    assert timeline_from_json({"version": TIMELINE_VERSION, "segments": [{"charStart": 0}]}) is None


def test_locate_segment_bisect_edge_cases_and_scaling(monkeypatch: pytest.MonkeyPatch) -> None:
    import bisect

    # All-empty / zero-duration segments yield -1.
    all_zero = Timeline((SegmentSpan(0, 3, 0, 0), SegmentSpan(4, 7, 100, 100)))
    assert locate_segment(all_zero, 0) == -1
    assert locate_segment(all_zero, 100) == -1

    # Interspersed leading, middle, and trailing zero-duration segments.
    interspersed = Timeline(
        (
            SegmentSpan(0, 1, 0, 0),
            SegmentSpan(1, 2, 0, 1000),
            SegmentSpan(2, 3, 1000, 1000),
            SegmentSpan(3, 4, 1000, 2000),
            SegmentSpan(4, 5, 2000, 2000),
        )
    )
    assert locate_segment(interspersed, -10) == 1
    assert locate_segment(interspersed, 0) == 1
    assert locate_segment(interspersed, 999) == 1
    assert locate_segment(interspersed, 1000) == 3
    assert locate_segment(interspersed, 1999) == 3
    assert locate_segment(interspersed, 2000) == 3
    assert locate_segment(interspersed, 9999) == 3

    # Silence gap between cues clamps to last non-empty (matches existing contract).
    gapped = Timeline((SegmentSpan(0, 1, 100, 1000), SegmentSpan(2, 3, 2000, 3000)))
    assert locate_segment(gapped, 50) == 0
    assert locate_segment(gapped, 500) == 0
    assert locate_segment(gapped, 1500) == 1
    assert locate_segment(gapped, 2500) == 1
    assert locate_segment(gapped, 5000) == 1

    # Nested overlapping spans still find the containing earlier span when past the inner one.
    nested = Timeline((SegmentSpan(0, 5, 0, 2000), SegmentSpan(5, 8, 500, 1000)))
    assert locate_segment(nested, 250) == 0
    assert locate_segment(nested, 750) == 1
    assert locate_segment(nested, 1500) == 0
    assert locate_segment(nested, 2500) == 1

    # Large timeline uses bisect.bisect_right (O(log N), no per-tick list allocation).
    spans = tuple(SegmentSpan(i * 2, i * 2 + 1, i * 100, (i + 1) * 100) for i in range(10_000))
    big = Timeline(spans)
    bisect_calls = 0
    real_bisect_right = bisect.bisect_right

    def spy_bisect_right(*args, **kwargs):
        nonlocal bisect_calls
        bisect_calls += 1
        return real_bisect_right(*args, **kwargs)

    monkeypatch.setattr(bisect, "bisect_right", spy_bisect_right)
    assert locate_segment(big, 0) == 0
    assert locate_segment(big, 543_250) == 5432
    assert locate_segment(big, 999_999) == 9999
    assert locate_segment(big, 2_000_000) == 9999
    assert bisect_calls == 4
