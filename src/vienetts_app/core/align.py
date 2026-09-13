"""SRT timeline alignment: place synthesized cue audio onto the subtitle clock.

Two coherent behaviours, chosen per project (:class:`FitPolicy`):

``dub``
    The SRT clock is the master. Each cue is anchored at its own start time and
    compressed with pitch-preserving WSOLA when the spoken take is longer than
    the cue window — up to ``rate_cap``. A cue that still overruns pushes the
    following cues later rather than losing audio. This is what you want when
    the result is muxed back onto the video: every cue lands on (or just after)
    its subtitle time, and the total drift stays near zero.

``transcript``
    The voice is the master. Cues are never time-compressed; each one plays at
    its natural pace and the subtitle's own pauses are reproduced between them
    (capped at ``max_gap_ms``). Consecutive cues are synthesized as one speech
    unit by default, so the model sees whole sentences instead of subtitle
    fragments and the prosody stays natural. Cue times drift later as the file
    goes on — which is fine here, because this app's synced playback highlights
    cues from the *measured* render timeline, never from the SRT.

Placement is forward-only (a cue's position depends only on its own length and
the previous cue's end), so the same code drives both the whole-file planner
used by tests and the streaming renderer that never holds a whole track in RAM.

Everything here is pure: no Qt, no disk, no engine. Stretching reuses
:func:`core.audio.time_stretch_audio` (the same WSOLA the reading-speed setting
uses), so there is exactly one time-stretch implementation in the app.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

import numpy as np

from vienetts_app.core.audio import DEFAULT_SAMPLE_RATE, time_stretch_audio
from vienetts_app.core.subtitles import Cue, cue_char_spans
from vienetts_app.core.timeline import SegmentSpan, Timeline

MODE_DUB = "dub"
MODE_TRANSCRIPT = "transcript"
MODES: tuple[str, ...] = (MODE_DUB, MODE_TRANSCRIPT)

#: WSOLA bounds, shared with the app's reading-speed range (studio.MIN_SPEED /
#: MAX_SPEED) so a rate the aligner plans is always a rate the engine can render.
MIN_RATE = 1.0  # never slow speech down: slack in a cue window becomes silence
MAX_RATE = 2.0
MIN_RATE_CAP = 1.0
MAX_RATE_CAP = 2.0
MAX_OFFSET_MS = 600_000  # ±10 min: generous for a re-cut, far from a typo
MAX_GAP_LIMIT_MS = 60_000

#: Speech-unit grouping (``merge_sentences``). Subtitle cues frequently omit
#: terminal punctuation entirely, so a sentence test alone would merge a whole
#: movie into one unit; the char and gap guards keep units sentence-sized.
UNIT_MAX_CHARS = 220
UNIT_GAP_MS = 600

_SENTENCE_TERMINATORS = ".!?…。！？"
_SENTENCE_CLOSERS = "\"'”’»)]}"


class AlignmentError(ValueError):
    """An alignment request is inconsistent (cue/clip count or rate mismatch)."""


@dataclass(frozen=True)
class FitPolicy:
    """How cue audio is placed on the SRT clock.

    ``max_gap_ms`` is the cap on silence *preserved* from a subtitle gap; ``0``
    means uncapped (dub mode wants the original pauses exactly, however long).
    ``offset_ms`` nudges the whole file — the standard fix when the subtitle
    file was authored for a different cut of the video.
    """

    mode: str = MODE_DUB
    rate_cap: float = 1.5
    max_gap_ms: int = 0
    offset_ms: int = 0
    merge_sentences: bool = False

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise ValueError(f"mode must be one of {list(MODES)}, got {self.mode!r}")
        if isinstance(self.rate_cap, bool) or not isinstance(self.rate_cap, (int, float)):
            raise ValueError(f"rate_cap must be a number, got {self.rate_cap!r}")
        if not MIN_RATE_CAP <= float(self.rate_cap) <= MAX_RATE_CAP:
            raise ValueError(
                f"rate_cap must be in [{MIN_RATE_CAP}, {MAX_RATE_CAP}], got {self.rate_cap}"
            )
        if isinstance(self.max_gap_ms, bool) or not isinstance(self.max_gap_ms, int):
            raise ValueError(f"max_gap_ms must be an integer, got {self.max_gap_ms!r}")
        if not 0 <= self.max_gap_ms <= MAX_GAP_LIMIT_MS:
            raise ValueError(
                f"max_gap_ms must be in [0, {MAX_GAP_LIMIT_MS}], got {self.max_gap_ms}"
            )
        if isinstance(self.offset_ms, bool) or not isinstance(self.offset_ms, int):
            raise ValueError(f"offset_ms must be an integer, got {self.offset_ms!r}")
        if abs(self.offset_ms) > MAX_OFFSET_MS:
            raise ValueError(f"offset_ms must be within ±{MAX_OFFSET_MS}, got {self.offset_ms}")
        if not isinstance(self.merge_sentences, bool):
            raise ValueError("merge_sentences must be a bool")

    @classmethod
    def dub(
        cls,
        *,
        rate_cap: float = 1.5,
        offset_ms: int = 0,
        max_gap_ms: int = 0,
        merge_sentences: bool = False,
    ) -> FitPolicy:
        """Hard SRT lock: compress to fit, push on overflow (see module docstring)."""
        return cls(
            mode=MODE_DUB,
            rate_cap=rate_cap,
            max_gap_ms=max_gap_ms,
            offset_ms=offset_ms,
            merge_sentences=merge_sentences,
        )

    @classmethod
    def transcript(
        cls,
        *,
        offset_ms: int = 0,
        max_gap_ms: int = 1500,
        merge_sentences: bool = True,
    ) -> FitPolicy:
        """Natural pace, subtitles only supply the pauses (see module docstring)."""
        return cls(
            mode=MODE_TRANSCRIPT,
            rate_cap=MIN_RATE_CAP,
            max_gap_ms=max_gap_ms,
            offset_ms=offset_ms,
            merge_sentences=merge_sentences,
        )


@dataclass(frozen=True)
class CueFit:
    """Where one cue's audio actually lands, and what it cost to get there.

    ``srt_start_ms``/``srt_end_ms`` are the offset-adjusted subtitle times we
    aimed at; ``start_ms``/``end_ms`` are the final placement (``start_ms``
    never overlaps the previous cue, and ``max_gap_ms`` capping can land it
    before ``srt_start_ms``).
    """

    index: int
    start_ms: int
    end_ms: int
    rate: float
    srt_start_ms: int
    srt_end_ms: int
    natural_ms: int
    overflow_ms: int

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    @property
    def pushed_ms(self) -> int:
        """Signed delta between the placed start and the adjusted SRT start.

        Positive when an overrun pushed the cue late; negative is possible too
        — ``max_gap_ms`` capping can start a cue early.
        """
        return self.start_ms - self.srt_start_ms

    @property
    def compressed(self) -> bool:
        return self.rate > MIN_RATE + 1e-3


@dataclass(frozen=True)
class AlignmentStats:
    """Render summary for the UI notice (how much fitting this file needed)."""

    cues: int
    compressed: int
    max_rate: float
    overflowed: int
    max_overflow_ms: int
    pushed: int
    total_ms: int
    drift_ms: int

    @property
    def clean(self) -> bool:
        """True when every cue landed inside its own subtitle window."""
        return self.overflowed == 0


@dataclass(frozen=True)
class AlignedTrack:
    """A rendered alignment: the mix plus the cue↔time map describing it."""

    audio: np.ndarray
    timeline: Timeline
    fits: tuple[CueFit, ...]
    sample_rate: int


# ── planning ─────────────────────────────────────────────────────────────────


def frames_for_ms(ms: int, sample_rate: int = DEFAULT_SAMPLE_RATE) -> int:
    """Sample count for ``ms`` — the single rounding rule planner and renderer share."""
    return int(round(max(0, int(ms)) * sample_rate / 1000))


def ms_for_frames(frames: int, sample_rate: int = DEFAULT_SAMPLE_RATE) -> int:
    """Milliseconds for ``frames`` — the inverse of :func:`frames_for_ms`."""
    return int(round(max(0, int(frames)) * 1000 / sample_rate))


def plan_cue(
    cue: Cue,
    natural_ms: int,
    policy: FitPolicy,
    prev_end_ms: int | None = None,
) -> CueFit:
    """Place one cue given the previous cue's end (``None`` = first cue).

    Forward-only and total: any input yields a fit, because dropping a cue
    would silently drop spoken text. ``prev_end_ms`` is what keeps cues from
    overlapping — an overlong take pushes its successors instead of covering
    them.
    """
    srt_start = max(0, int(cue.start_ms) + policy.offset_ms)
    srt_end = max(srt_start, int(cue.end_ms) + policy.offset_ms)
    window_ms = max(1, srt_end - srt_start)
    natural = max(0, int(natural_ms))

    if policy.mode == MODE_TRANSCRIPT:
        rate = MIN_RATE
    else:
        # natural/window > 1 means "speaks slower than the subtitle allows".
        rate = min(float(policy.rate_cap), max(MIN_RATE, natural / window_ms))
    duration_ms = int(round(natural / rate)) if rate > 0 else natural

    if prev_end_ms is None:
        start_ms = srt_start
    elif policy.max_gap_ms > 0:
        # Anchor at the subtitle time when the pause fits, push when the
        # previous take overran, and cap a long silent gap.
        start_ms = max(int(prev_end_ms), min(srt_start, int(prev_end_ms) + policy.max_gap_ms))
    else:
        start_ms = max(int(prev_end_ms), srt_start)

    end_ms = start_ms + duration_ms
    return CueFit(
        index=cue.index,
        start_ms=start_ms,
        end_ms=end_ms,
        rate=rate,
        srt_start_ms=srt_start,
        srt_end_ms=srt_end,
        natural_ms=natural,
        overflow_ms=max(0, end_ms - srt_end),
    )


def plan_alignment(
    cues: Sequence[Cue],
    natural_ms: Sequence[int],
    policy: FitPolicy,
) -> list[CueFit]:
    """Whole-file plan: :func:`plan_cue` folded forward over ``cues``."""
    if len(cues) != len(natural_ms):
        raise AlignmentError(
            f"cues ({len(cues)}) and natural_ms ({len(natural_ms)}) must have the same length"
        )
    fits: list[CueFit] = []
    prev_end: int | None = None
    for cue, natural in zip(cues, natural_ms, strict=True):
        fit = plan_cue(cue, natural, policy, prev_end)
        fits.append(fit)
        prev_end = fit.end_ms
    return fits


def plan_alignment_for_clips(
    cues: Sequence[Cue],
    clips: Sequence[np.ndarray],
    policy: FitPolicy,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> list[CueFit]:
    """Plan from the synthesized cue audio itself (natural length measured)."""
    return plan_alignment(cues, [ms_for_frames(len(clip), sample_rate) for clip in clips], policy)


# ── rendering ────────────────────────────────────────────────────────────────


def stretch_clip_to(clip: np.ndarray, fit: CueFit, sample_rate: int) -> np.ndarray:
    """Cue audio at the fit's rate, forced to exactly the fit's frame count.

    WSOLA returns an approximate length; the plan's ``[start_ms, end_ms)`` is
    exact. Padding/trimming to the planned count here is what makes the
    rendered timeline (built from real frames) agree with the plan, and what
    guarantees a cue can never bleed into the next one.
    """
    target = frames_for_ms(fit.duration_ms, sample_rate)
    mono = np.ascontiguousarray(np.asarray(clip, dtype=np.float32).ravel())
    if target <= 0:
        return np.zeros(0, dtype=np.float32)
    if fit.compressed:
        mono = np.ascontiguousarray(
            time_stretch_audio(mono, rate=fit.rate, sample_rate=sample_rate), dtype=np.float32
        )
    if mono.size == target:
        return mono
    if mono.size > target:
        return mono[:target]
    padded = np.zeros(target, dtype=np.float32)
    padded[: mono.size] = mono
    return padded


def timeline_from_fits(
    cues: Sequence[Cue],
    fits: Sequence[CueFit],
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> Timeline:
    """Measured :class:`Timeline` for a rendered alignment (cue-level spans).

    Char offsets index into :func:`core.subtitles.cues_text`, so the karaoke
    lookup (``locate_segment``/``active_word``) works on an SRT track exactly
    as it does on an audiobook chapter.
    """
    if len(cues) != len(fits):
        raise AlignmentError(f"cues ({len(cues)}) and fits ({len(fits)}) must have the same length")
    spans = tuple(
        SegmentSpan(char_start, char_end, fit.start_ms, fit.end_ms)
        for (char_start, char_end), fit in zip(cue_char_spans(cues), fits, strict=True)
    )
    return Timeline(spans, approximate=False)


def render_alignment(
    cues: Sequence[Cue],
    clips: Sequence[np.ndarray],
    policy: FitPolicy,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> AlignedTrack:
    """In-memory mix of ``clips`` placed on the SRT clock.

    For short files and tests. Long subtitle files must use the streaming
    renderer in :mod:`core.subtitle_project` — a two-hour track is ~1.4 GB of
    float32 and must never be materialized at once.
    """
    if len(cues) != len(clips):
        raise AlignmentError(
            f"cues ({len(cues)}) and clips ({len(clips)}) must have the same length"
        )
    if sample_rate <= 0:
        raise AlignmentError(f"sample_rate must be > 0, got {sample_rate}")
    fits = plan_alignment_for_clips(cues, clips, policy, sample_rate)
    placed = [
        stretch_clip_to(clip, fit, sample_rate) for clip, fit in zip(clips, fits, strict=True)
    ]
    total_frames = frames_for_ms(fits[-1].end_ms, sample_rate) if fits else 0
    audio = np.zeros(total_frames, dtype=np.float32)
    for fit, block in zip(fits, placed, strict=True):
        start = frames_for_ms(fit.start_ms, sample_rate)
        end = min(start + block.size, total_frames)
        if end > start:
            audio[start:end] = block[: end - start]
    return AlignedTrack(
        audio=audio,
        timeline=timeline_from_fits(cues, fits, sample_rate),
        fits=tuple(fits),
        sample_rate=sample_rate,
    )


def alignment_stats(fits: Sequence[CueFit]) -> AlignmentStats:
    """Summarize a plan for the UI ("12/430 cues compressed, max 1.4×")."""
    if not fits:
        return AlignmentStats(0, 0, MIN_RATE, 0, 0, 0, 0, 0)
    compressed = [fit for fit in fits if fit.compressed]
    overflows = [fit.overflow_ms for fit in fits if fit.overflow_ms > 0]
    return AlignmentStats(
        cues=len(fits),
        compressed=len(compressed),
        max_rate=max(fit.rate for fit in fits),
        overflowed=len(overflows),
        max_overflow_ms=max(overflows, default=0),
        pushed=sum(1 for fit in fits if fit.pushed_ms > 0),
        total_ms=fits[-1].end_ms,
        drift_ms=fits[-1].end_ms - fits[-1].srt_end_ms,
    )


def adjusted_cues(cues: Sequence[Cue], fits: Sequence[CueFit]) -> list[Cue]:
    """Cues retimed to the rendered audio — the SRT that matches the export.

    Dub mode's whole point is that this file lines up with ``track.wav``; a
    zero-length fit (a cue whose synthesis produced nothing) keeps its original
    window rather than emitting an unreadable ``00:00:00,000 --> 00:00:00,000``.
    """
    if len(cues) != len(fits):
        raise AlignmentError(f"cues ({len(cues)}) and fits ({len(fits)}) must have the same length")
    retimed: list[Cue] = []
    for cue, fit in zip(cues, fits, strict=True):
        start = fit.start_ms if fit.end_ms > fit.start_ms else cue.start_ms
        end = fit.end_ms if fit.end_ms > fit.start_ms else cue.end_ms
        retimed.append(Cue(cue.index, start, max(start, end), cue.text))
    return retimed


# ── speech units (merge_sentences) ───────────────────────────────────────────


def ends_sentence(text: str) -> bool:
    """True when ``text`` ends a sentence (terminal punctuation, closers allowed)."""
    stripped = text.rstrip().rstrip(_SENTENCE_CLOSERS).rstrip()
    return bool(stripped) and stripped[-1] in _SENTENCE_TERMINATORS


def speech_units(
    cues: Sequence[Cue],
    *,
    merge: bool,
    max_chars: int = UNIT_MAX_CHARS,
    gap_ms: int = UNIT_GAP_MS,
) -> list[tuple[int, ...]]:
    """Cue indices grouped into synthesis units, in order, covering every cue.

    ``merge=False`` yields one unit per cue (exact per-cue timing, fragmented
    prosody). ``merge=True`` joins consecutive cues into whole sentences — a
    cue whose predecessor already ended a sentence starts a new unit, as does
    one after a pause longer than ``gap_ms`` or one that would overflow
    ``max_chars``. Subtitles routinely omit final periods, so the gap and
    length guards are what keep a punctuation-free file from collapsing into a
    single unit.
    """
    if not merge:
        return [(i,) for i in range(len(cues))]
    units: list[tuple[int, ...]] = []
    current: list[int] = []
    chars = 0
    for i, cue in enumerate(cues):
        if current:
            previous = cues[current[-1]]
            gap = cue.start_ms - previous.end_ms
            if ends_sentence(previous.text) or gap > gap_ms or chars + len(cue.text) > max_chars:
                units.append(tuple(current))
                current = []
                chars = 0
        current.append(i)
        chars += len(cue.text)
    if current:
        units.append(tuple(current))
    return units


def split_unit_audio(
    audio: np.ndarray,
    indices: Sequence[int],
    cues: Sequence[Cue],
    sample_rate: int = DEFAULT_SAMPLE_RATE,
) -> list[np.ndarray]:
    """Split one merged unit's audio across its cues, proportional to text length.

    Approximate by nature (no per-word timing exists inside a merged take), but
    it is stable, exact in total length, and the per-cue fit then re-times each
    piece — so an imperfect split shifts a cue's audio slightly, never loses it.
    """
    mono = np.ascontiguousarray(np.asarray(audio, dtype=np.float32).ravel())
    if not indices:
        return []
    if len(indices) == 1:
        return [mono]
    weights = [max(1, len(cues[i].text)) for i in indices]
    total = sum(weights)
    pieces: list[np.ndarray] = []
    cursor = 0
    for position, weight in enumerate(weights):
        if position == len(weights) - 1:
            end = mono.size
        else:
            end = min(mono.size, cursor + int(round(mono.size * weight / total)))
        pieces.append(np.ascontiguousarray(mono[cursor:end], dtype=np.float32))
        cursor = end
    return pieces


def policy_summary(policy: FitPolicy) -> dict[str, Any]:
    """QML-friendly description of a policy (mode label + effective knobs)."""
    return {
        "mode": policy.mode,
        "rateCap": float(policy.rate_cap),
        "maxGapMs": int(policy.max_gap_ms),
        "offsetMs": int(policy.offset_ms),
        "mergeSentences": bool(policy.merge_sentences),
        "stretches": policy.mode == MODE_DUB,
    }


def clamp_rate_cap(value: Any) -> float:
    """Coerce a UI value into the legal ``rate_cap`` range (never raises)."""
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return 1.5
    if not math.isfinite(numeric):
        return 1.5
    return min(MAX_RATE_CAP, max(MIN_RATE_CAP, numeric))
