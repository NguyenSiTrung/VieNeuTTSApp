"""SubRip (.srt) / WebVTT cue model: parse, clean, re-emit (pure, no Qt, no disk policy).

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
1-3 fraction digits, hour-less WebVTT ``MM:SS.mmm`` stamps, optional
position/cue-setting fields, one-dash arrows and inverted end/start pairs
(clamped) all parse. A line that contains ``-->`` but does not match the
timestamp pattern is corrupt, not ambiguous: :func:`parse_cues` raises
:class:`SubtitleError` naming the 1-based line rather than guessing a timing
or silently speaking that line's body. Whether zero usable cues is an error
stays with the caller (the importer refuses it with a user-actionable message;
the aligner does the same).

Speaker labels vs sound cues. A leading ``[label]`` is a *speaker* when it is
a diarization id (``SPEAKER_01``, ``Speaker 2``) or name-shaped: ASCII, at most
32 characters, every word capitalised (``Narrator``, ``JOHN``, ``Mary Jane``),
not an ALL-CAPS phrase of two or more words (``MUSIC PLAYING``) and free of
sound words (``LAUGHS``, ``MUSIC`` …). ``SPEAKER_00: …`` also labels a speaker.
Every other ``[…]`` group, and every ``(…)`` group, holding a letter and at
most 64 characters is a *sound cue* (``[door slams]``, ``(laughs)``). Sound
cues before speech are dropped from the text (``[door slams] Get out!`` →
``Get out!``); a cue made only of sound cues is kept as a caption but is not
speakable (:func:`is_speakable`), except that bare ASR filler tokens
(:data:`NON_SPEECH_LABELS` in brackets, e.g. ``[BLANK_AUDIO]``) are dropped.

:func:`read_cues` decodes BOM → UTF-8 → UTF-16 without BOM (NUL-byte
pattern) → Windows-1258 → Windows-1252; a guessed encoding adds a note.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

#: SRT/VTT styling that must not be spoken: ``<i>``/``<font …>``/``<v Name>``
#: tags and VTT ``<00:01.000>`` karaoke stamps. A real tag starts with a tag
#: name or ``/`` right after ``<`` (the HTML rule), so ``x < y and y > z``
#: is dialogue, not markup. ``{\an8}``-style override blocks go too.
_TAG_RE = re.compile(r"<(?:/?[A-Za-z][^<>]*|\d[\d:.,]*)>")
_OVERRIDE_RE = re.compile(r"\{[^}]*\}")
_WS_RE = re.compile(r"\s+")
#: ``SPEAKER_00: …`` speaker prefix (the bracketed form is shape-checked).
_PLAIN_SPEAKER_RE = re.compile(r"^(?P<spk>SPEAKER_\d+):\s+")
#: One leading ``[…]``/``(…)`` group (speaker label or sound cue), with the
#: optional ``:`` and spacing that follow it.
_LEADING_GROUP_RE = re.compile(r"^(?:\[(?P<b>[^\[\]]{1,64})\]|\((?P<p>[^()]{1,64})\)):?\s*")
#: Any bracketed/parenthesised group, wherever it sits in a cue.
_GROUP_RE = re.compile(r"\[(?P<b>[^\[\]]{1,64})\]|\((?P<p>[^()]{1,64})\)")
_SPEAKER_ID_RE = re.compile(r"^speaker[ _-]?(?:\d{1,3}|[a-z])$", re.IGNORECASE)
_NAME_LABEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_ -]{0,31}$")
_LABEL_WORD_RE = re.compile(r"[A-Za-z0-9]+")
NON_SPEECH_LABELS: frozenset[str] = frozenset(
    {
        "MUSIC",
        "BLANK_AUDIO",
        "APPLAUSE",
        "LAUGHTER",
        "SILENCE",
        "NOISE",
        "INAUDIBLE",
        "SOUND",
        "SFX",
        "CHEERING",
        "COUGH",
        "SIGH",
    }
)
#: Words that make a capitalised bracket label a sound cue, not a name
#: (``[Laughs]``, ``[THUNDER]``). Common SDH vocabulary; names that are also
#: everyday words (``Rain``, ``Bang``) are deliberately left out.
_SOUND_WORDS: frozenset[str] = frozenset(
    {
        *NON_SPEECH_LABELS,
        *("AUDIO", "BLANK", "MUSICAL", "SONG", "SINGING", "HUMMING", "WHISTLING", "PLAYING"),
        *("LAUGH", "LAUGHS", "LAUGHING", "CHUCKLES", "CHUCKLING", "GIGGLES", "GIGGLING", "SIGHS"),
        *("SIGHING", "GASP", "GASPS", "GASPING", "SCREAM", "SCREAMS", "SCREAMING", "SHOUTS"),
        *("SHOUTING", "YELLS", "YELLING", "CRIES", "CRYING", "SOBS", "SOBBING", "COUGHS"),
        *("COUGHING", "SNIFFS", "SNIFFLES", "GROANS", "GRUNTS", "PANTING", "WHISPERS"),
        *("WHISPERING", "MUMBLING", "MUTTERING", "INDISTINCT", "CHATTER", "CLAPPING", "CHEERS"),
        *("STATIC", "EXPLOSION", "GUNSHOT", "GUNSHOTS", "GUNFIRE", "THUNDER", "FOOTSTEPS", "KNOCK"),
        *("KNOCKS", "KNOCKING", "RINGS", "RINGING", "BEEPS", "BEEPING", "BUZZING", "BARKS"),
        *("BARKING", "SLAMS", "CREAKS", "CRASH", "SIREN", "DOOR", "PHONE"),
    }
)
#: One ``HH:MM:SS,mmm`` stamp (1-3 fractional digits, ``,`` or ``.`` decimal).
#: Hours are optional, so WebVTT's ``MM:SS.mmm`` short form parses as well.
_STAMP = r"(?:(?P<h{n}>\d{{1,3}}):)?(?P<m{n}>\d{{1,2}}):(?P<s{n}>\d{{1,2}})[,.](?P<f{n}>\d{{1,3}})"

_STAMP_RE = re.compile("^" + _STAMP.format(n="") + r"\s*$")

#: ``HH:MM:SS,mmm --> HH:MM:SS,mmm``, tolerant of the optional trailing
#: position fields some tools append (``X1:… Y1:… X2:… Y2:…``) and of a
#: one-dash arrow.
_TIMESTAMP_RE = re.compile(_STAMP.format(n="1") + r"\s*--?>\s*" + _STAMP.format(n="2"))

_MS_PER_HOUR = 3_600_000
_MS_PER_MINUTE = 60_000
_MS_PER_SECOND = 1_000

#: Byte-order marks, longest first (the UTF-32-LE mark starts with UTF-16-LE's).
_BOMS: tuple[tuple[bytes, str], ...] = (
    (b"\xef\xbb\xbf", "utf-8"),
    (b"\xff\xfe\x00\x00", "utf-32-le"),
    (b"\x00\x00\xfe\xff", "utf-32-be"),
    (b"\xff\xfe", "utf-16-le"),
    (b"\xfe\xff", "utf-16-be"),
)
#: Legacy code pages tried after UTF-8/UTF-16: Vietnamese first, then Western.
_LEGACY_CODECS: tuple[tuple[str, str], ...] = (
    ("cp1258", "Windows-1258"),
    ("cp1252", "Windows-1252"),
)


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
    speaker: str = ""
    speech_start_ms: int | None = None
    speech_end_ms: int | None = None

    def __post_init__(self) -> None:
        if self.index < 1:
            raise ValueError(f"cue index must be >= 1, got {self.index}")
        if self.start_ms < 0:
            raise ValueError(f"cue start_ms must be >= 0, got {self.start_ms}")
        if self.end_ms < self.start_ms:
            raise ValueError(f"cue end_ms ({self.end_ms}) must be >= start_ms ({self.start_ms})")
        if not self.text.strip():
            raise ValueError("cue text must be non-blank")
        if not isinstance(self.speaker, str):
            raise ValueError("cue speaker must be a string")
        if self.speech_start_ms is not None:
            if isinstance(self.speech_start_ms, bool) or not isinstance(self.speech_start_ms, int):
                raise ValueError("cue speech_start_ms must be an int or None")
            if not (self.start_ms <= self.speech_start_ms <= self.end_ms):
                raise ValueError(
                    f"cue speech_start_ms ({self.speech_start_ms}) must be within "
                    f"[{self.start_ms}, {self.end_ms}]"
                )
        if self.speech_end_ms is not None:
            if isinstance(self.speech_end_ms, bool) or not isinstance(self.speech_end_ms, int):
                raise ValueError("cue speech_end_ms must be an int or None")
            if not (self.start_ms <= self.speech_end_ms <= self.end_ms):
                raise ValueError(
                    f"cue speech_end_ms ({self.speech_end_ms}) must be within "
                    f"[{self.start_ms}, {self.end_ms}]"
                )
        if self.effective_speech_start_ms > self.effective_speech_end_ms:
            raise ValueError(
                f"cue effective_speech_end_ms ({self.effective_speech_end_ms}) must be >= "
                f"effective_speech_start_ms ({self.effective_speech_start_ms})"
            )

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def effective_speech_start_ms(self) -> int:
        return self.start_ms if self.speech_start_ms is None else self.speech_start_ms

    @property
    def effective_speech_end_ms(self) -> int:
        return self.end_ms if self.speech_end_ms is None else self.speech_end_ms

    @property
    def speech_duration_ms(self) -> int:
        return self.effective_speech_end_ms - self.effective_speech_start_ms

    @property
    def speakable(self) -> bool:
        """False for a sound-cue/♪/punctuation/emoji-only caption (see :func:`is_speakable`)."""
        return is_speakable(self.text)


def _fraction_to_ms(digits: str) -> int:
    """``"5"``/``"50"``/``"500"`` → 500 ms (right-padded, SRT is deciseconds)."""
    return int(digits.ljust(3, "0")[:3])


def _match_ms(match: re.Match[str], suffix: str) -> int:
    """Milliseconds of one ``_STAMP`` group set (``suffix`` = ``""``/``"1"``/``"2"``)."""
    return (
        int(match[f"h{suffix}"] or 0) * _MS_PER_HOUR
        + int(match[f"m{suffix}"]) * _MS_PER_MINUTE
        + int(match[f"s{suffix}"]) * _MS_PER_SECOND
        + _fraction_to_ms(match[f"f{suffix}"])
    )


def parse_timestamp(text: str) -> int | None:
    """Milliseconds for one ``[HH:]MM:SS,mmm`` stamp; ``None`` when unparsable."""
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


def _is_non_speech_label(label: str) -> bool:
    """``[MUSIC]``/``[Blank audio]``-style ASR filler token (see :data:`NON_SPEECH_LABELS`)."""
    return label.strip().upper().replace(" ", "_").replace("-", "_") in NON_SPEECH_LABELS


def _is_sound_cue(label: str) -> bool:
    """A ``[…]``/``(…)`` group that describes a sound rather than holding a number."""
    return any(ch.isalpha() for ch in label)


def _is_speaker_label(label: str) -> bool:
    """True when a bracketed ``label`` names a speaker (rules in the module docstring)."""
    label = label.strip()
    if _SPEAKER_ID_RE.match(label):
        return True
    if not _NAME_LABEL_RE.match(label) or _is_non_speech_label(label):
        return False
    words = _LABEL_WORD_RE.findall(label)
    if any(word[0].islower() or word.upper() in _SOUND_WORDS for word in words):
        return False
    alpha = [word for word in words if word.isalpha()]
    return not (len(alpha) >= 2 and all(word.isupper() for word in alpha))


def _split_cue_body(text: str) -> tuple[str, str] | None:
    """Cleaned cue body → ``(speaker, text)``; ``None`` when blank or ASR filler only.

    Peels one leading speaker label, then the sound cues in front of the
    speech. A body that is nothing but sound cues keeps them as its caption
    text (non-speakable) unless every one is a bracketed filler token.
    """
    speaker = ""
    plain = _PLAIN_SPEAKER_RE.match(text)
    if plain is not None:
        speaker, text = plain["spk"], text[plain.end() :]
    else:
        group = _LEADING_GROUP_RE.match(text)
        if group is not None and group["b"] is not None and _is_speaker_label(group["b"]):
            rest = text[group.end() :]
            if rest:
                speaker, text = group["b"].strip(), rest
    rest = text
    fillers_only = True
    while (group := _LEADING_GROUP_RE.match(rest)) is not None:
        label = group["b"] if group["b"] is not None else group["p"]
        if not _is_sound_cue(label):
            break
        fillers_only = fillers_only and group["b"] is not None and _is_non_speech_label(label)
        rest = rest[group.end() :]
    if rest:
        return speaker, rest
    return None if fillers_only else (speaker, text)


def is_speakable(text: str) -> bool:
    """Whether ``text`` has anything to synthesize.

    False when nothing is left but sound cues (``(laughs)``, ``[MUSIC
    PLAYING]``), ``♪``, punctuation, emoji or styling — such a cue stays in
    transcript/caption output but is skipped (rendered as silence) by
    synthesis. Pure function of the text, so an edited cue re-evaluates.
    """
    spoken = _GROUP_RE.sub(
        lambda m: " " if _is_sound_cue(m["b"] or m["p"]) else m.group(), clean_cue_text(text)
    )
    return any(ch.isalnum() for ch in spoken)


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
    nothing (or to bracketed ASR filler such as ``[BLANK_AUDIO]``) are
    skipped, and a source with no ``-->`` at all returns ``[]``. Speaker
    labels and leading sound cues are peeled off the text as described in
    the module docstring; a sound-cue-only cue is kept, non-speakable.
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
        split = _split_cue_body(clean_cue_text(" ".join(body)))
        if split is None:
            continue
        speaker, body_text = split
        start_ms = _match_ms(match, "1")
        end_ms = _match_ms(match, "2")
        # Hand-edited files do contain end < start; a zero-length window keeps
        # the cue (and its audio) instead of silently dropping spoken text.
        cues.append(Cue(len(cues) + 1, start_ms, max(start_ms, end_ms), body_text, speaker=speaker))
    return cues


def _utf16_without_bom(raw: bytes) -> str | None:
    """``"utf-16-le"``/``"utf-16-be"`` when ``raw``'s NUL bytes fall on one parity.

    Subtitle text is timestamp- and Latin-heavy, so in UTF-16 most code units
    have a zero high byte: odd offsets for LE, even ones for BE. NULs spread
    over both parities (UTF-32, binary data) match neither.
    """
    pairs = len(raw) // 2
    if pairs == 0:
        return None
    even_nul = raw[0 : pairs * 2 : 2].count(0)
    odd_nul = raw[1 : pairs * 2 : 2].count(0)
    if odd_nul >= pairs * 0.2 and even_nul * 4 <= odd_nul:
        return "utf-16-le"
    if even_nul >= pairs * 0.2 and odd_nul * 4 <= even_nul:
        return "utf-16-be"
    return None


def decode_subtitle_bytes(raw: bytes, *, name: str = "", notes: list[str] | None = None) -> str:
    """Subtitle file bytes → text: BOM → UTF-8 → UTF-16 (no BOM) → cp1258 → cp1252.

    A BOM is authoritative. Without one, NUL bytes on one byte parity mean
    BOM-less UTF-16 (ASCII-range UTF-16 is also valid UTF-8, so this check
    must precede UTF-8); other NULs are padding/garbage and are dropped.
    Then UTF-8, then the Vietnamese and the Western Windows code pages.
    Legacy text is NFC-composed (cp1258 spells tones as combining marks).
    Every guessed encoding appends a user-facing note to ``notes`` (when
    given) so a mis-guess is visible, not silent.

    Raises:
        SubtitleError: a BOM-declared body does not decode, or no code page
            decodes the bytes.
    """
    label = name or "subtitle"
    undecodable = (
        f"Could not decode '{label}' as text (tried UTF-8, UTF-16, Windows-1258 "
        "and Windows-1252). Re-save the file with UTF-8 encoding and try again."
    )

    def guessed(text: str, encoding: str) -> str:
        if notes is not None:
            notes.append(
                f"Tệp '{label}' không phải UTF-8 — đã đọc theo bảng mã {encoding}. "
                "Hãy kiểm tra dấu tiếng Việt và lưu lại tệp dưới dạng UTF-8."
            )
        return text

    for bom, codec in _BOMS:
        if raw.startswith(bom):
            try:
                return raw[len(bom) :].decode(codec)
            except UnicodeDecodeError as err:
                raise SubtitleError(undecodable) from err
    if b"\x00" in raw:
        codec = _utf16_without_bom(raw)
        if codec is not None:
            try:
                return guessed(raw.decode(codec), codec.upper())
            except UnicodeDecodeError:
                pass
        # Stray NULs (zero padding, a crashed writer) are never subtitle text.
        raw = raw.replace(b"\x00", b"")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        pass
    for codec, display in _LEGACY_CODECS:
        try:
            text = raw.decode(codec)
        except UnicodeDecodeError:
            continue
        return guessed(unicodedata.normalize("NFC", text), display)
    raise SubtitleError(undecodable)


def read_cues(path: str | Path, *, notes: list[str] | None = None) -> list[Cue]:
    """Cues from an ``.srt``/``.vtt`` file on disk (see :func:`decode_subtitle_bytes`).

    ``notes`` (optional) receives a user-facing note when the encoding had
    to be guessed (no BOM and not UTF-8).

    Raises:
        SubtitleError: the file cannot be decoded, or a line containing
            ``-->`` is not a valid timestamp pair. A readable file that
            yields no cue returns ``[]`` — callers decide whether that is an
            error (it usually is, and they have the filename to say so).
    """
    path = Path(path)
    return parse_cues(decode_subtitle_bytes(path.read_bytes(), name=path.name, notes=notes))


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


def format_srt(cues: Iterable[Cue], *, include_speakers: bool = False) -> str:
    """Cues → SubRip text (trailing newline; indices renumbered from 1)."""
    blocks = []
    for i, cue in enumerate(cues, start=1):
        body = f"[{cue.speaker}] {cue.text}" if include_speakers and cue.speaker else cue.text
        blocks.append(
            f"{i}\n{format_timestamp(cue.start_ms)} --> {format_timestamp(cue.end_ms)}\n{body}"
        )
    return "\n\n".join(blocks) + "\n"
