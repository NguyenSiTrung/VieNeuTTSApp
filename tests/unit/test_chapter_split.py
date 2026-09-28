"""Chapter part splitting (Approach A) — pure, deterministic."""

from __future__ import annotations

import pytest

from vienetts_app.core.chapter_split import (
    MAX_CHAPTER_PARTS,
    ChapterPart,
    split_chapter_parts,
)


class TestSplitChapterParts:
    def test_empty_and_whitespace_return_no_parts(self) -> None:
        assert split_chapter_parts("") == []
        assert split_chapter_parts("   \n\t  ") == []

    def test_fitting_text_is_one_part_over_non_whitespace_body(self) -> None:
        text = "  Xin chào.  "
        parts = split_chapter_parts(text, max_chars=100)
        assert parts == [ChapterPart(2, 11)]
        assert parts[0].slice_text(text) == "Xin chào."

    def test_invalid_max_chars_raises(self) -> None:
        with pytest.raises(ValueError):
            split_chapter_parts("abc", max_chars=0)

    def test_paragraph_breaks_preferred(self) -> None:
        p1 = "Câu một. Câu hai."
        p2 = "Câu ba. Câu bốn."
        text = f"{p1}\n\n{p2}"
        parts = split_chapter_parts(text, max_chars=len(p1) + 1)
        assert len(parts) == 2
        assert parts[0].slice_text(text) == p1
        assert parts[1].slice_text(text) == p2

    def test_sentence_breaks_when_paragraphs_do_not_fit(self) -> None:
        text = "Một câu thứ nhất. Hai câu thứ hai. Ba câu thứ ba."
        # Cap forces a break at a sentence end, not mid-word.
        parts = split_chapter_parts(text, max_chars=22)
        assert len(parts) >= 2
        for part in parts:
            assert part.char_len <= 22
            slice_ = part.slice_text(text)
            assert slice_ == slice_.strip()
        joined = " ".join(p.slice_text(text) for p in parts)
        assert joined.replace("  ", " ") == text.strip()

    def test_hard_split_prefers_last_space(self) -> None:
        wordy = " ".join(["từ"] * 40)  # 119 chars, no sentence ends
        text = wordy
        parts = split_chapter_parts(text, max_chars=50)
        assert len(parts) >= 2
        for part in parts:
            assert part.char_len <= 50
        # No part starts mid-token when a space was available.
        for part in parts:
            if part.char_start > 0:
                assert text[part.char_start - 1].isspace() or text[part.char_start - 1] in ".!…"

    def test_deterministic(self) -> None:
        text = "\n\n".join(f"Đoạn {i}. A b c d e f g h i j k l m n o p." for i in range(30))
        a = split_chapter_parts(text, max_chars=80)
        b = split_chapter_parts(text, max_chars=80)
        assert a == b

    def test_max_parts_enforced(self) -> None:
        text = "x" * 500
        with pytest.raises(ValueError, match="more than"):
            split_chapter_parts(text, max_chars=10, max_parts=3)

    def test_default_limit_single_part_for_normal_chapter(self) -> None:
        text = "Ngắn. " * 100
        assert len(split_chapter_parts(text)) == 1

    def test_parts_cover_body_without_overlap(self) -> None:
        text = "\n\n".join(f"Chương phần {i}. Nội dung đủ dài một chút." for i in range(20))
        parts = split_chapter_parts(text, max_chars=60)
        assert len(parts) > 1
        assert parts[0].char_start == 0 or text[: parts[0].char_start].isspace()
        for left, right in zip(parts, parts[1:], strict=False):
            assert left.char_end <= right.char_start
            gap = text[left.char_end : right.char_start]
            assert left.char_end < right.char_start or gap.isspace()
        assert parts[-1].char_end == len(text) or text[parts[-1].char_end :].isspace()

    def test_max_chapter_parts_constant_is_bounded(self) -> None:
        assert 2 <= MAX_CHAPTER_PARTS <= 64
