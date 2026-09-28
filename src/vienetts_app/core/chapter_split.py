"""Oversize-chapter parts (Approach A): pure, deterministic text splitting.

A chapter whose text exceeds :data:`~vienetts_app.core.audiobook.CHAPTER_CHAR_LIMIT`
cannot be synthesized into one WAV without a large full-audio handoff (the
reason the limit exists). Instead the text is cut into **parts** at paragraph
and sentence boundaries. Each part becomes its own cached sub-audio under the
same logical chapter (``ch_0000_s00.wav``, ``ch_0000_s01.wav``, …); the player
chains them so listening stays continuous.

Spans are half-open ``[char_start, char_end)`` offsets into the **original**
chapter text so the sync reader can keep chapter-global char coordinates.
Whitespace between parts is never spoken and is left as a gap in the span
cover (harmless for karaoke lookup).

``split_chapter_parts`` is deterministic: the same text always yields the same
parts, which is what makes re-renders and timeline merges stable. A chapter
that already fits returns exactly one part covering its non-whitespace body —
callers keep the legacy single-WAV path in that case.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

#: Hard safety ceiling on parts per chapter (``limit * MAX`` chars max).
#: A pathological spine item cannot spawn thousands of render jobs.
MAX_CHAPTER_PARTS = 24

# Paragraph runs: one or more blank lines (tolerating spaces/tabs on the line).
_PARAGRAPH_RE = re.compile(r"\n[ \t]*\n[\n \t]*")
# Sentence ends: terminator + optional closing quotes/brackets, before space/EOL.
_SENTENCE_RE = re.compile(r'[.!?…]["\'”’)\]]*(?=\s|$)')


@dataclass(frozen=True)
class ChapterPart:
    """One renderable slice of a chapter (offsets into the original text)."""

    char_start: int
    char_end: int

    def slice_text(self, text: str) -> str:
        return text[self.char_start : self.char_end]

    @property
    def char_len(self) -> int:
        return self.char_end - self.char_start


def _break_positions(text: str, lo: int, hi: int) -> list[int]:
    """Exclusive end offsets where a part may stop (plus ``hi``)."""
    positions: set[int] = {hi}
    for match in _PARAGRAPH_RE.finditer(text, lo, hi):
        if lo < match.end() < hi:
            positions.add(match.end())
    for match in _SENTENCE_RE.finditer(text, lo, hi):
        end = match.end()
        if lo < end < hi:
            positions.add(end)
    return sorted(positions)


def split_chapter_parts(
    text: str, max_chars: int = 60_000, *, max_parts: int = MAX_CHAPTER_PARTS
) -> list[ChapterPart]:
    """Cut ``text`` into parts of ≤ ``max_chars`` at natural boundaries.

    - Empty / whitespace-only text → ``[]``.
    - Text that fits in ``max_chars`` → one part over its non-whitespace body.
    - Oversize text → greedy parts packed at paragraph, then sentence, breaks.
      A single unit longer than the cap is hard-split at the cap, preferring
      the last space so words stay whole where possible.

    Raises ``ValueError`` when ``max_chars < 1`` or the text would need more
    than ``max_parts`` pieces (caller surfaces an actionable refusal instead of
    a multi-hour render).
    """
    if max_chars < 1:
        raise ValueError(f"max_chars must be >= 1, got {max_chars}")
    n = len(text or "")
    first = 0
    while first < n and text[first].isspace():
        first += 1
    last = n
    while last > first and text[last - 1].isspace():
        last -= 1
    body_len = last - first
    if body_len <= 0:
        return []
    if body_len <= max_chars:
        return [ChapterPart(first, last)]

    breaks = _break_positions(text, first, last)
    parts: list[ChapterPart] = []
    start = first
    while start < last:
        end = start
        for candidate in breaks:
            if candidate <= start:
                continue
            if candidate - start <= max_chars:
                end = candidate
            else:
                break
        if end <= start:
            window = text[start : start + max_chars]
            space = window.rfind(" ")
            cut = space if space > 0 else max_chars
            end = start + cut
            if end <= start:
                end = start + max_chars
        # Skip leading whitespace of the next piece (never spoken).
        nxt = end
        while nxt < last and text[nxt].isspace():
            nxt += 1
        parts.append(ChapterPart(start, end))
        start = nxt if nxt > end else end
        if len(parts) > max_parts:
            raise ValueError(
                f"chapter would need more than {max_parts} parts ({n} chars at {max_chars} each)"
            )
    return parts
