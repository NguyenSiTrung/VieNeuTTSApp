"""Chapter audio↔text alignment (FR-A9): pure functions, no Qt, no deps.

A :class:`Timeline` maps each render segment (see
``core.engine.split_text_for_streaming``) to an exact ``[start_ms, end_ms]``
window of the chapter WAV plus the segment's ``[char_start, char_end)``
offsets into the chapter text. Measured timelines come from the render
pipeline (audio samples counted per segment); estimates allocate the WAV
duration proportionally to segment length (``approximate=True`` — legacy
chapters cached before timelines existed).

Segment text is NOT a verbatim substring of the chapter text — the splitter
collapses whitespace, so ``"\\n\\n"`` between paragraphs becomes ``" "`` when
two units pack into one segment, and a unit longer than the cap is hard-split
mid-token. Offsets are therefore recovered by a whitespace-insensitive
character lock-step scan (:func:`map_segment_offsets`): the splitter never
reorders or rewrites non-whitespace characters, only the whitespace between
them, so consuming chapter characters in order against each segment yields
exact spans even for hard-splits and spaceless CJK paragraphs.

Karaoke lookup at playback time: :func:`locate_segment` finds the active
segment for a playback position; the caller interpolates a char offset inside
that segment and :func:`active_word` expands it to the containing word.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass, field
from typing import Any

from vienetts_app.core.engine import split_text_for_streaming

TIMELINE_VERSION = 1


@dataclass(frozen=True)
class SegmentSpan:
    """One render segment: time window in the WAV + char window in the text."""

    char_start: int
    char_end: int
    start_ms: int
    end_ms: int


@dataclass(frozen=True)
class Timeline:
    """Ordered segment spans covering a chapter; ``approximate`` marks
    char-proportional estimates (measured timelines stay exact)."""

    segments: tuple[SegmentSpan, ...]
    approximate: bool = False
    _non_empty_indices: tuple[int, ...] = field(init=False, repr=False, compare=False)
    _non_empty_starts: tuple[int, ...] = field(init=False, repr=False, compare=False)
    _ends_monotone: bool = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        indices: list[int] = []
        starts: list[int] = []
        ends_monotone = True
        prev_end: int | None = None
        for i, span in enumerate(self.segments):
            if span.end_ms > span.start_ms:
                indices.append(i)
                starts.append(span.start_ms)
                if prev_end is not None and prev_end > span.end_ms:
                    ends_monotone = False
                prev_end = span.end_ms
        object.__setattr__(self, "_non_empty_indices", tuple(indices))
        object.__setattr__(self, "_non_empty_starts", tuple(starts))
        object.__setattr__(self, "_ends_monotone", ends_monotone)


# ── word / paragraph spans ────────────────────────────────────────────────────


def word_spans(text: str) -> list[tuple[int, int]]:
    """``[start, end)`` offsets of every whitespace-delimited token in ``text``."""
    spans: list[tuple[int, int]] = []
    i, n = 0, len(text)
    while i < n:
        if text[i].isspace():
            i += 1
            continue
        j = i
        while j < n and not text[j].isspace():
            j += 1
        spans.append((i, j))
        i = j
    return spans


def active_word(
    spans: list[tuple[int, int]],
    char_index: int,
    *,
    starts: list[int] | None = None,
) -> tuple[int, int]:
    """Span of the word at/after ``char_index``; clamps to the first/last word.

    Returns ``(-1, -1)`` for an empty span list. A ``char_index`` inside a
    whitespace gap resolves to the NEXT word (karaoke highlight leads the
    audio, never lags behind it). ``starts`` may pass the precomputed
    ``[span[0] for span in spans]`` — per-tick callers (playback position
    updates) must not rebuild it over a whole chapter each call.
    """
    if not spans:
        return (-1, -1)
    if starts is None:
        starts = [span[0] for span in spans]
    position = bisect.bisect_right(starts, char_index) - 1
    if position >= 0 and char_index < spans[position][1]:
        return spans[position]  # containing word
    nxt = position + 1
    return spans[nxt] if nxt < len(spans) else spans[-1]


def split_paragraphs(text: str) -> list[dict[str, Any]]:
    """Paragraph blocks (``"\\n\\n"``-separated, per core.epub) with char offsets.

    Blank blocks are dropped; ``index`` is renumbered over the KEPT blocks so
    QML delegates and the controller agree on paragraph ids.
    """
    paragraphs: list[dict[str, Any]] = []
    pos = 0
    for chunk in text.split("\n\n"):
        stripped = chunk.strip()
        if stripped:
            start = pos + (len(chunk) - len(chunk.lstrip()))
            paragraphs.append(
                {
                    "index": len(paragraphs),
                    "text": stripped,
                    "charStart": start,
                    "charEnd": start + len(stripped),
                }
            )
        pos += len(chunk) + 2  # the consumed "\n\n" separator
    return paragraphs


# ── segment offset mapping ────────────────────────────────────────────────────


def map_segment_offsets(text: str, segments: list[str]) -> list[tuple[int, int]]:
    """``[char_start, char_end)`` of each segment inside ``text``.

    Whitespace-insensitive character lock-step scan: non-whitespace characters
    in ``text`` are consumed in order against each segment's non-whitespace
    characters, recovering exact spans across normalized whitespace
    (``"\\n\\n"`` collapsed to ``" "``), mid-token hard-splits (>512 chars),
    and spaceless CJK paragraphs. A ``(-1, -1)`` placeholder marks empty or
    genuinely unmappable segments (callers treat those as "no highlight",
    never as an error).
    """
    n = len(text)
    ti = 0
    offsets: list[tuple[int, int]] = []
    compact_text: str | None = None
    compact_pos: list[int] | None = None

    for segment in segments:
        scan = ti
        first = -1
        last = -1
        has_non_ws = False
        matched = True
        for ch in segment:
            if ch.isspace():
                continue
            has_non_ws = True
            while scan < n and text[scan].isspace():
                scan += 1
            if scan < n and text[scan] == ch:
                if first == -1:
                    first = scan
                last = scan + 1
                scan += 1
            else:
                matched = False
                break

        if not has_non_ws:
            offsets.append((-1, -1))
            continue

        if matched:
            offsets.append((first, last))
            ti = scan
            continue

        # Fallback resync: search forward in the non-whitespace projection of text.
        if compact_text is None or compact_pos is None:
            compact_chars: list[str] = []
            compact_pos = []
            for idx, ch in enumerate(text):
                if not ch.isspace():
                    compact_chars.append(ch)
                    compact_pos.append(idx)
            compact_text = "".join(compact_chars)

        compact_seg = "".join(segment.split())
        start_idx = bisect.bisect_left(compact_pos, ti)
        found = compact_text.find(compact_seg, start_idx)
        if found == -1:
            offsets.append((-1, -1))
            continue

        start_char = compact_pos[found]
        end_char = compact_pos[found + len(compact_seg) - 1] + 1
        offsets.append((start_char, end_char))
        ti = end_char

    return offsets


# ── timeline construction ─────────────────────────────────────────────────────


def build_timeline(
    text: str, segments: list[str], segment_samples: list[int], sample_rate: int
) -> Timeline:
    """Exact timeline from per-segment audio sample counts (48 kHz renders).

    ``segment_samples[i]`` is the audio the engine produced for
    ``segments[i]``; the chapter WAV is their concatenation, so cumulative
    sample counts give exact ms boundaries. Raises ``ValueError`` on a
    segments/samples length mismatch or a non-positive sample rate.
    """
    if sample_rate <= 0:
        raise ValueError(f"sample_rate must be > 0, got {sample_rate}")
    if len(segments) != len(segment_samples):
        raise ValueError(
            f"segments ({len(segments)}) and segment_samples ({len(segment_samples)}) "
            "must have the same length"
        )
    offsets = map_segment_offsets(text, segments)
    spans: list[SegmentSpan] = []
    cursor_samples = 0
    for (char_start, char_end), samples in zip(offsets, segment_samples, strict=True):
        start_ms = round(cursor_samples * 1000 / sample_rate)
        cursor_samples += max(0, int(samples))
        end_ms = round(cursor_samples * 1000 / sample_rate)
        spans.append(SegmentSpan(char_start, char_end, start_ms, end_ms))
    return Timeline(tuple(spans), approximate=False)


def estimate_timeline(text: str, duration_ms: int, segments: list[str] | None = None) -> Timeline:
    """Char-proportional timeline for a known total duration (approximate).

    Used for chapters cached before timelines existed: same segmentation,
    duration split by segment character weight. Degenerate inputs (no
    segments, non-positive duration) yield an empty timeline.
    """
    if segments is None:
        segments = split_text_for_streaming(text)
    duration_ms = max(0, int(duration_ms))
    if not segments or duration_ms <= 0:
        return Timeline((), approximate=True)
    offsets = map_segment_offsets(text, segments)
    weights = [max(1, len(segment)) for segment in segments]
    total_weight = sum(weights)
    spans: list[SegmentSpan] = []
    cursor_ms = 0
    for (char_start, char_end), weight in zip(offsets, weights, strict=True):
        start_ms = cursor_ms
        cursor_ms = min(duration_ms, start_ms + round(duration_ms * weight / total_weight))
        spans.append(SegmentSpan(char_start, char_end, start_ms, cursor_ms))
    if spans:  # hand the rounding residue to the last span
        last = spans[-1]
        spans[-1] = SegmentSpan(last.char_start, last.char_end, last.start_ms, duration_ms)
    return Timeline(tuple(spans), approximate=True)


# ── lookup helpers ────────────────────────────────────────────────────────────


def locate_segment(timeline: Timeline, position_ms: int) -> int:
    """Index of the segment playing at ``position_ms``; -1 when none exists.

    O(log N) via ``bisect`` over pre-indexed non-empty segment starts (zero
    per-tick list allocation). Zero-duration segments (a silent engine pass)
    never match. Positions before the first / past the last segment clamp to
    the nearest non-empty one — a karaoke cursor should sit on SOME word
    whenever audio exists.
    """
    starts = timeline._non_empty_starts
    if not starts:
        return -1
    indices = timeline._non_empty_indices
    pos = bisect.bisect_right(starts, position_ms) - 1
    if pos < 0:
        return indices[0]
    segments = timeline.segments
    cand = indices[pos]
    if position_ms < segments[cand].end_ms:
        return cand
    if not timeline._ends_monotone:
        for j in range(pos - 1, -1, -1):
            earlier = indices[j]
            if position_ms < segments[earlier].end_ms:
                return earlier
    return indices[-1]


def paragraph_start_ms(timeline: Timeline, paragraph_char_start: int) -> int:
    """Playback position where a paragraph starts; -1 when nothing overlaps.

    The first non-empty segment whose char window ends after the paragraph's
    first character — segments may straddle paragraph breaks (packed units),
    in which case the paragraph starts mid-segment and we accept the
    segment's own start (the closest seekable boundary the timeline knows).
    """
    for span in timeline.segments:
        if span.end_ms > span.start_ms and span.char_end > paragraph_char_start:
            return span.start_ms
    return -1


# ── persistence payload ───────────────────────────────────────────────────────


def timeline_to_json(timeline: Timeline) -> dict[str, Any]:
    """JSON payload for ``ch_XXXX.timeline.json`` (text itself is NOT copied —
    char offsets index into the chapter text stored in book.json)."""
    return {
        "version": TIMELINE_VERSION,
        "approximate": bool(timeline.approximate),
        "segments": [
            {
                "charStart": span.char_start,
                "charEnd": span.char_end,
                "startMs": span.start_ms,
                "endMs": span.end_ms,
            }
            for span in timeline.segments
        ],
    }


def timeline_from_json(data: Any) -> Timeline | None:
    """Inverse of :func:`timeline_to_json`; ``None`` on any malformed payload
    (callers degrade to an estimate rather than crash — every read of
    user-state files in this project fails soft)."""
    if not isinstance(data, dict):
        return None
    try:
        if int(data.get("version") or 0) != TIMELINE_VERSION:
            return None
        raw_segments = data.get("segments")
        if not isinstance(raw_segments, list):
            return None
        spans = [
            SegmentSpan(
                int(entry["charStart"]),
                int(entry["charEnd"]),
                int(entry["startMs"]),
                int(entry["endMs"]),
            )
            for entry in raw_segments
            if isinstance(entry, dict)
        ]
        if len(spans) != len(raw_segments):
            return None
    except (KeyError, TypeError, ValueError):
        return None
    return Timeline(tuple(spans), approximate=bool(data.get("approximate")))
