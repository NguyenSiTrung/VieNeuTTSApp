"""SubRip (.srt) cue model: parse, clean, re-emit (pure, no Qt, no disk policy).

The app has two consumers of a subtitle file:

1. ``core.importers._read_srt`` — imports subtitles as plain spoken text,
   deliberately discarding the timecodes.
2. ``core.align`` — synthesizes audio that is *placed on the SRT timeline*, so
   it needs the timecodes as data.

Both go through :func:`parse_cues` here, so "what a cue's text is" (sequence
numbers dropped, ``-->`` lines dropped, ``<...>``/``{...}`` styling stripped,
multi-line bodies joined with a space, whitespace collapsed) has exactly one
definition. Timestamps are milliseconds since the start of the subtitle file.

Parsing is structural and tolerant where real files need it — every line
containing ``-->`` opens a cue boundary, so missing blank separators and
missing or inconsistent sequence numbers recover, and ``.``/``,`` decimals,
1-3 fraction digits, optional position fields, one-dash arrows and inverted
end/start pairs (clamped) all parse. A line that contains ``-->`` but does
not match the timestamp pattern is corrupt, not ambiguous: :func:`parse_cues`
raises :class:`SubtitleError` naming the 1-based line rather than guessing a
timing or silently speaking that line's body. Whether zero usable cues is an
error stays with the caller (the importer refuses it with a user-actionable
message; the aligner does the same).
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

#: SRT styling that must not be spoken: ``<i>``/``<font …>`` tags and
#: ``{\an8}``-style override blocks.
_TAG_RE = re.compile(r"<[^>]*>")
_OVERRIDE_RE = re.compile(r"\{[^}]*\}")
_WS_RE = re.compile(r"\s+")

#: One ``HH:MM:SS,mmm`` stamp (1-3 fractional digits, ``,`` or ``.`` decimal).
_STAMP = r"(?P<h{n}>\d{{1,3}}):(?P<m{n}>\d{{1,2}}):(?P<s{n}>\d{{1,2}})[,.](?P<f{n}>\d{{1,3}})"

_STAMP_RE = re.compile("^" + _STAMP.format(n="") + r"\s*$")

#: ``HH:MM:SS,mmm --> HH:MM:SS,mmm``, tolerant of the optional trailing
#: position fields some tools append (``X1:… Y1:… X2:… Y2:…``) and of a
#: one-dash arrow.
_TIMESTAMP_RE = re.compile(_STAMP.format(n="1") + r"\s*--?>\s*" + _STAMP.format(n="2"))

_MS_PER_HOUR = 3_600_000
_MS_PER_MINUTE = 60_000
_MS_PER_SECOND = 1_000


class SubtitleError(RuntimeError):
    """A subtitle file exists but cannot be used (unreadable or cue-less).

    Messages are user-facing and actionable; the original exception (if any)
    is chained as ``__cause__``.
    """


@dataclass(frozen=True)
class Cue:
    """One subtitle cue: a ``[start_ms, end_ms)`` window and its spoken text.

    ``index`` is 1-based and renumbered over the *kept* cues (a file whose
    blocks are unnumbered or numbered inconsistently still yields 1..n), so it
    is a stable identifier for the UI and for re-emitted SRT alike.
    """

    index: int
    start_ms: int
    end_ms: int
    text: str

    def __post_init__(self) -> None:
        if self.index < 1:
            raise ValueError(f"cue index must be >= 1, got {self.index}")
        if self.start_ms < 0:
            raise ValueError(f"cue start_ms must be >= 0, got {self.start_ms}")
        if self.end_ms < self.start_ms:
            raise ValueError(f"cue end_ms ({self.end_ms}) must be >= start_ms ({self.start_ms})")
        if not self.text.strip():
            raise ValueError("cue text must be non-blank")

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms


def _fraction_to_ms(digits: str) -> int:
    """``"5"``/``"50"``/``"500"`` → 500 ms (right-padded, SRT is deciseconds)."""
    return int(digits.ljust(3, "0")[:3])


def _match_ms(match: re.Match[str], suffix: str) -> int:
    """Milliseconds of one ``_STAMP`` group set (``suffix`` = ``""``/``"1"``/``"2"``)."""
    return (
        int(match[f"h{suffix}"]) * _MS_PER_HOUR
        + int(match[f"m{suffix}"]) * _MS_PER_MINUTE
        + int(match[f"s{suffix}"]) * _MS_PER_SECOND
        + _fraction_to_ms(match[f"f{suffix}"])
    )


def parse_timestamp(text: str) -> int | None:
    """Milliseconds for one ``HH:MM:SS,mmm`` stamp; ``None`` when unparsable."""
    match = _STAMP_RE.match(text.strip())
    if match is None:
        return None
    return _match_ms(match, "")


def format_timestamp(ms: int) -> str:
    """``ms`` → ``HH:MM:SS,mmm`` (hours never wrap: a 100-hour file still round-trips)."""
    ms = max(0, int(ms))
    hours, rest = divmod(ms, _MS_PER_HOUR)
    minutes, rest = divmod(rest, _MS_PER_MINUTE)
    seconds, millis = divmod(rest, _MS_PER_SECOND)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d},{millis:03d}"


def clean_cue_text(body: str) -> str:
    """Cue body → spoken text: styling dropped, whitespace collapsed, stripped."""
    text = _TAG_RE.sub("", body)
    text = _OVERRIDE_RE.sub("", text)
    return _WS_RE.sub(" ", text).strip()


def parse_cues(source: str) -> list[Cue]:
    """Every usable cue in ``source``, in file order, renumbered from 1.

    Line-oriented: each line containing ``-->`` opens a cue whose body runs
    to the first blank line in its slice, or to the next timestamp line/EOF
    when none separates them — so missing blank separators recover while
    blank-separated junk blocks are never spoken. Only in the no-blank case
    is a trailing integer line dropped as the next cue's sequence number. A
    ``-->`` line that does not parse as a timestamp pair raises
    :class:`SubtitleError` with its 1-based line number — timing is never
    guessed and a corrupt line's body is never spoken. Timestamp and
    sequence lines never appear in cue text; cues whose body cleans to
    nothing are skipped, and a source with no ``-->`` at all returns ``[]``.
    """
    text = source.replace("\r\n", "\n").replace("\r", "\n")
    lines = text.split("\n")
    entries: list[tuple[int, re.Match[str]]] = []
    for line_number, line in enumerate(lines, 1):
        if "-->" not in line:
            continue
        match = _TIMESTAMP_RE.search(line)
        if match is None:
            raise SubtitleError(
                f"Invalid subtitle timecode on line {line_number}. "
                "Re-save the file as SubRip (.srt) and try again."
            )
        entries.append((line_number - 1, match))
    cues: list[Cue] = []
    for position, (stamp_index, match) in enumerate(entries):
        body_end = entries[position + 1][0] if position + 1 < len(entries) else len(lines)
        region = lines[stamp_index + 1 : body_end]
        # The body ends at the first blank line in the slice: anything after
        # it is a separate non-cue block (notes, metadata, stray prose) and
        # must not be spoken. With no blank separator the body runs to the
        # next stamp and its tail integer is that cue's sequence number.
        blank_at = next((i for i, line in enumerate(region) if not line.strip()), len(region))
        body = [line.strip() for line in region[:blank_at]]
        if blank_at == len(region) and body and body[-1].isdigit():
            body.pop()
        body_text = clean_cue_text(" ".join(body))
        if not body_text:
            continue
        start_ms = _match_ms(match, "1")
        end_ms = _match_ms(match, "2")
        # Hand-edited files do contain end < start; a zero-length window keeps
        # the cue (and its audio) instead of silently dropping spoken text.
        cues.append(Cue(len(cues) + 1, start_ms, max(start_ms, end_ms), body_text))
    return cues


def read_cues(path: str | Path) -> list[Cue]:
    """Cues from an ``.srt`` file on disk (UTF-8, BOM tolerated).

    Raises:
        SubtitleError: the file cannot be decoded as UTF-8, or a line
            containing ``-->`` is not a valid timestamp pair. A readable file
            that yields no cue returns ``[]`` — callers decide whether that is
            an error (it usually is, and they have the filename to say so).
    """
    path = Path(path)
    try:
        raw = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as err:
        raise SubtitleError(
            f"Could not decode '{path.name}' as UTF-8 text. "
            "Re-save the file with UTF-8 encoding and try again."
        ) from err
    return parse_cues(raw)


def cues_text(cues: Iterable[Cue]) -> str:
    """The spoken text of ``cues`` joined by newlines.

    This is the string every cue's char offsets index into (see
    :func:`cue_char_spans`), so the timeline, the karaoke lookup and the
    paragraph list all agree on one coordinate system.
    """
    return "\n".join(cue.text for cue in cues)


def cue_char_spans(cues: Iterable[Cue]) -> list[tuple[int, int]]:
    """``[char_start, char_end)`` of each cue inside :func:`cues_text`.

    Exact by construction (the join adds one newline between cue texts and the
    cue texts themselves are whitespace-collapsed), so no lock-step word scan
    is needed the way chapter segments need one.
    """
    spans: list[tuple[int, int]] = []
    cursor = 0
    for cue in cues:
        start = cursor
        cursor = start + len(cue.text)
        spans.append((start, cursor))
        cursor += 1  # the "\n" separator
    return spans


def format_srt(cues: Iterable[Cue]) -> str:
    """Cues → SubRip text (trailing newline; indices renumbered from 1)."""
    blocks = [
        f"{i}\n{format_timestamp(cue.start_ms)} --> {format_timestamp(cue.end_ms)}\n{cue.text}"
        for i, cue in enumerate(cues, start=1)
    ]
    return "\n\n".join(blocks) + "\n"
