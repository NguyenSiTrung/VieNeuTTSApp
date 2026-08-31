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
mid-token. Offsets are therefore recovered by a lock-step word scan
(:func:`map_segment_offsets`): the splitter never reorders or rewrites words,
only the whitespace between them, so consuming chapter tokens in order against
each segment's tokens yields exact spans.

Karaoke lookup at playback time: :func:`locate_segment` finds the active
segment for a playback position; the caller interpolates a char offset inside
that segment and :func:`active_word` expands it to the containing word.
"""

from __future__ import annotations

import bisect
from dataclasses import dataclass
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

    Lock-step scan: chapter tokens (``word_spans``) are consumed in order
    against each segment's whitespace-split tokens, one token per match —
    the splitter never reorders or rewrites words, so this recovers exact
    offsets even though the segment string itself is not a substring.
    A ``(-1, -1)`` placeholder marks empty/unmappable segments (callers
    treat those as "no highlight", never as an error).
    """
    tokens = word_spans(text)
    offsets: list[tuple[int, int]] = []
    ti = 0
    for segment in segments:
        seg_tokens = segment.split()
        if not seg_tokens:
            offsets.append((-1, -1))
            continue
        first = last = -1
        for _token in seg_tokens:
            if ti >= len(tokens):
                break
            start, end = tokens[ti]
            ti += 1
            if first == -1:
                first = start
            last = end
        offsets.append((first, last) if first != -1 else (-1, -1))
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

    Zero-duration segments (a silent engine pass) never match. Positions
    before the first / past the last segment clamp to the nearest non-empty
    one — a karaoke cursor should sit on SOME word whenever audio exists.
    """
    non_empty = [
        (i, span) for i, span in enumerate(timeline.segments) if span.end_ms > span.start_ms
    ]
    if not non_empty:
        return -1
    active = -1
    for i, span in non_empty:
        if span.start_ms <= position_ms < span.end_ms:
            active = i
        elif span.start_ms > position_ms:
            break
    if active != -1:
        return active
    if position_ms < non_empty[0][1].start_ms:
        return non_empty[0][0]
    return non_empty[-1][0]


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
