"""Chapter part splitting (Approach A) — pure, deterministic."""

from __future__ import annotations

import pytest

from vienetts_app.core.chapter_split import (
    ChapterPart,
    split_chapter_parts,
)


class TestSplitChapterParts:
    def test_trivial_inputs(self) -> None:
        assert split_chapter_parts("") == []
        assert split_chapter_parts("   \n\t  ") == []

        text = "  Xin chào.  "
        parts = split_chapter_parts(text, max_chars=100)
        assert parts == [ChapterPart(2, 11)]
        assert parts[0].slice_text(text) == "Xin chào."

        with pytest.raises(ValueError):
            split_chapter_parts("abc", max_chars=0)

    def test_break_points(self) -> None:
        p1 = "Câu một. Câu hai."
        p2 = "Câu ba. Câu bốn."
        text = f"{p1}\n\n{p2}"
        parts = split_chapter_parts(text, max_chars=len(p1) + 1)
        assert len(parts) == 2
        assert parts[0].slice_text(text) == p1
        assert parts[1].slice_text(text) == p2

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

    def test_limits(self) -> None:
        text = "x" * 500
        with pytest.raises(ValueError, match="more than"):
            split_chapter_parts(text, max_chars=10, max_parts=3)

        text = "Ngắn. " * 100
        assert len(split_chapter_parts(text)) == 1

    def test_coverage_contract(self) -> None:
        text = "\n\n".join(f"Chương phần {i}. Nội dung đủ dài một chút." for i in range(20))
        parts = split_chapter_parts(text, max_chars=60)
        assert len(parts) > 1
        assert parts[0].char_start == 0 or text[: parts[0].char_start].isspace()
        for left, right in zip(parts, parts[1:], strict=False):
            assert left.char_end <= right.char_start
            gap = text[left.char_end : right.char_start]
            assert left.char_end < right.char_start or gap.isspace()
        assert parts[-1].char_end == len(text) or text[parts[-1].char_end :].isspace()
