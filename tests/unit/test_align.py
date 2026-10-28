"""core/align.py: placing synthesized cue audio on the SubRip clock.

These tests pin the two behaviours the feature promises. *dub* mode locks audio
to the subtitle times (compress up to a cap, push later cues on overflow);
*transcript* mode keeps natural speech and only borrows the subtitle's pauses.
Placement is forward-only, so the same expectations hold for the whole-file
planner here and the streaming renderer that follows it.
"""

from __future__ import annotations

import numpy as np
import pytest

from vienetts_app.core.align import (
    MAX_OFFSET_MS,
    MAX_RATE_CAP,
    MIN_RATE_CAP,
    AlignedTrack,
    AlignmentError,
    FitPolicy,
    adjusted_cues,
    alignment_stats,
    clamp_rate_cap,
    ends_sentence,
    frames_for_ms,
    ms_for_frames,
    plan_alignment,
    plan_alignment_for_clips,
    plan_cue,
    policy_summary,
    render_alignment,
    speech_units,
    split_unit_audio,
    stretch_clip_to,
    timeline_from_fits,
)
from vienetts_app.core.subtitles import Cue, cues_text
from vienetts_app.core.timeline import locate_segment

# A sample rate of 1000 makes one millisecond exactly one frame, so render
# expectations read as "the cue's own milliseconds".
SR = 1_000


def cue(index: int, start_ms: int, end_ms: int, text: str = "x") -> Cue:
    return Cue(index, start_ms, end_ms, text)


def clip(ms: int, value: float = 1.0) -> np.ndarray:
    return np.full(ms, value, dtype=np.float32)


# ── policy ───────────────────────────────────────────────────────────────────


def test_fit_policy_contract() -> None:
    assert FitPolicy.dub().max_gap_ms == 0  # uncapped: dub wants the real pauses
    transcript = FitPolicy.transcript()
    assert transcript.rate_cap == MIN_RATE_CAP  # transcript never stretches
    assert transcript.merge_sentences is True

    summary = policy_summary(FitPolicy.dub(rate_cap=1.4, offset_ms=-250))
    assert summary == {
        "mode": "dub",
        "rateCap": 1.4,
        "maxGapMs": 0,
        "offsetMs": -250,
        "mergeSentences": False,
        "stretches": True,
    }
    assert policy_summary(FitPolicy.transcript())["stretches"] is False


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "nope"},
        {"offset_ms": MAX_OFFSET_MS + 1},
    ],
)
def test_fit_policy_rejects_invalid_fields(kwargs):
    with pytest.raises(ValueError):
        FitPolicy(**kwargs)


@pytest.mark.parametrize(
    ("value", "expected"),
    [("1.2", 1.2), (5, MAX_RATE_CAP)],
)
def test_clamp_rate_cap_never_raises(value, expected):
    assert clamp_rate_cap(value) == expected


def test_time_and_rate_helpers() -> None:
    assert clamp_rate_cap(float("nan")) == 1.5
    assert clamp_rate_cap(float("inf")) == 1.5

    for ms in (0, 1, 1000, 3_723_004):
        assert ms_for_frames(frames_for_ms(ms)) == ms
    assert frames_for_ms(1_000, 48_000) == 48_000
    assert frames_for_ms(-5) == 0
    assert ms_for_frames(-5) == 0


# ── dub mode: SRT clock is master ────────────────────────────────────────────


def test_dub_policy() -> None:
    # 3000 ms of speech in a 1500 ms window at cap 1.5 -> exactly 2000 ms (an
    # adjacent next cue leaves no silence to borrow).
    fit = plan_cue(
        cue(2, 2_000, 3_500),
        3_000,
        FitPolicy.dub(rate_cap=1.5),
        prev_end_ms=1_000,
        next_start_ms=3_500,
    )
    assert fit.rate == 1.5
    assert fit.compressed is True
    assert fit.start_ms == 2_000
    assert fit.duration_ms == 2_000
    assert fit.end_ms == 4_000
    assert fit.overflow_ms == 500  # 4000 past its own subtitle end
    assert fit.pushed_ms == 0

    fit = plan_cue(cue(1, 0, 1_000), 500, FitPolicy.dub())
    assert fit.rate == 1.0
    assert fit.compressed is False
    assert fit.duration_ms == 500
    assert fit.end_ms == 500  # stops early: the rest of the window is silence
    assert fit.overflow_ms == 0

    # With a 1500 ms post-cue silent gap (3500..5000), a 3000 ms take in a 1500 ms
    # window borrows up to 1200 ms from the gap at COMFORT_RATE (1.15x -> 2609 ms)
    # instead of rushing at 1.5x, while still leaving breath room before 5000 ms.
    policy = FitPolicy.dub(rate_cap=1.5)
    fits = plan_alignment(
        [cue(1, 0, 1_000), cue(2, 2_000, 3_500), cue(3, 5_000, 6_000)],
        [1_000, 3_000, 1_000],
        policy,
    )
    assert [(f.start_ms, f.end_ms) for f in fits] == [
        (0, 1_000),
        (2_000, 4_609),
        (5_000, 6_000),  # 4609 < 5000: it still lands on its own subtitle time
    ]
    assert [f.rate for f in fits] == [1.0, pytest.approx(1.15), 1.0]
    assert [f.pushed_ms for f in fits] == [0, 0, 0]
    # A 9000 ms take in a 1000 ms window cannot fit even at 2x (4500 > 1000).
    policy = FitPolicy.dub(rate_cap=2.0)
    fits = plan_alignment(
        [cue(1, 0, 1_000), cue(2, 2_000, 3_000)],
        [9_000, 500],
        policy,
    )
    first, second = fits
    assert first.rate == 2.0
    assert first.end_ms == 4_500
    assert first.overflow_ms == 3_500
    # The next cue anchors no earlier than the previous cue's end.
    assert second.start_ms == 4_500
    assert second.pushed_ms == 2_500

    policy = FitPolicy.dub(rate_cap=1.0)  # no compression allowed
    fits = plan_alignment(
        [cue(1, 0, 1_000), cue(2, 1_000, 2_000), cue(3, 2_000, 3_000)],
        [1_500, 1_500, 1_500],
        policy,
    )
    for previous, following in zip(fits, fits[1:], strict=False):
        assert following.start_ms >= previous.end_ms

    # FR-5.3 drift: a pushed cue plans against what is left of its windows, so
    # one overlong take is absorbed (up to rate_cap) instead of drifting every
    # later cue by the same 333 ms.
    adjacent = [cue(i + 1, i * 2_000, (i + 1) * 2_000) for i in range(4)]
    fits = plan_alignment(adjacent, [3_500, 2_000, 2_000, 2_000], FitPolicy.dub(rate_cap=1.5))
    assert fits[1].pushed_ms > 0 and fits[1].rate > 1.0
    assert abs(fits[-1].end_ms - fits[-1].srt_end_ms) <= 150


def test_offset_shifts_the_whole_file_and_clamps_at_zero():
    policy = FitPolicy.dub(offset_ms=-500)
    fit = plan_cue(cue(1, 0, 1_000), 1_000, policy)
    assert fit.srt_start_ms == 0
    assert fit.srt_end_ms == 500
    assert fit.start_ms == 0

    shifted = plan_cue(cue(1, 0, 1_000), 1_000, FitPolicy.dub(offset_ms=250))
    assert shifted.srt_start_ms == 250
    assert shifted.srt_end_ms == 1_250


# ── transcript mode: the voice is master ─────────────────────────────────────


def test_transcript_policy() -> None:
    policy = FitPolicy.transcript()
    fit = plan_cue(cue(1, 2_000, 2_500), 3_000, policy)
    assert fit.rate == 1.0
    assert fit.compressed is False
    assert fit.duration_ms == 3_000
    assert fit.overflow_ms == 2_500  # overruns its window, by design

    policy = FitPolicy.transcript(max_gap_ms=1_500)
    # Previous take ends at 1000; the next subtitle starts 100 s later.
    fit = plan_cue(cue(2, 100_000, 101_000), 1_000, policy, prev_end_ms=1_000)
    assert fit.start_ms == 2_500  # 1000 + the 1500 ms cap, not 100_000
    assert fit.pushed_ms == -97_500

    policy = FitPolicy.transcript(max_gap_ms=1_500)
    fits = plan_alignment(
        [cue(1, 0, 1_000), cue(2, 2_000, 2_500), cue(3, 5_000, 6_000)],
        [1_000, 5_000, 1_000],
        policy,
    )
    assert [(f.start_ms, f.end_ms) for f in fits] == [
        (0, 1_000),
        (2_000, 7_000),
        (7_000, 8_000),  # 5000 was impossible: pushed to the previous end
    ]
    assert fits[2].pushed_ms == 2_000
    # The measured timeline is what playback follows, so drift is reported, not hidden.
    assert alignment_stats(fits).drift_ms == 8_000 - 6_000


# ── plan → render ────────────────────────────────────────────────────────────


def test_plan_alignment_contract() -> None:
    with pytest.raises(AlignmentError):
        plan_alignment([cue(1, 0, 1_000)], [1_000, 2_000], FitPolicy.dub())

    fits = plan_alignment_for_clips(
        [cue(1, 0, 2_000), cue(2, 2_000, 3_000)],
        [clip(3_000), clip(1_000)],
        FitPolicy.dub(rate_cap=1.5),
        SR,
    )
    assert fits[0].natural_ms == 3_000
    assert fits[0].rate == 1.5
    assert fits[0].duration_ms == 2_000


def test_render_alignment_contract() -> None:
    cues = [cue(1, 0, 1_000, "a"), cue(2, 2_000, 3_000, "b")]
    track = render_alignment(cues, [clip(1_000), clip(1_000)], FitPolicy.dub(), SR)
    assert isinstance(track, AlignedTrack)
    assert track.sample_rate == SR
    assert track.audio.size == 3_000
    np.testing.assert_array_equal(track.audio[0:1_000], np.ones(1_000, dtype=np.float32))
    np.testing.assert_array_equal(track.audio[1_000:2_000], np.zeros(1_000, dtype=np.float32))
    np.testing.assert_array_equal(track.audio[2_000:3_000], np.ones(1_000, dtype=np.float32))
    # Cue spans index the joined cue text, so karaoke lookup works unchanged.
    assert cues_text(cues) == "a\nb"
    assert locate_segment(track.timeline, 500) == 0
    assert locate_segment(track.timeline, 2_500) == 1
    assert track.fits[0].end_ms == 1_000

    with pytest.raises(AlignmentError):
        render_alignment([cue(1, 0, 1_000)], [clip(1_000), clip(1_000)], FitPolicy.dub(), SR)
    with pytest.raises(AlignmentError):
        render_alignment([cue(1, 0, 1_000)], [clip(1_000)], FitPolicy.dub(), 0)

    track = render_alignment([], [], FitPolicy.dub(), SR)
    assert track.audio.size == 0
    assert track.fits == ()
    assert track.timeline.segments == ()


def test_stretch_clip_contract() -> None:
    long_fit = plan_cue(cue(1, 0, 1_000), 1_500, FitPolicy.dub(), next_start_ms=1_000)
    trimmed = stretch_clip_to(clip(1_500), long_fit, SR)
    assert trimmed.size == long_fit.duration_ms  # 1000, not the clip's 1500
    padded_fit = plan_cue(cue(1, 0, 1_000), 1_000, FitPolicy.dub())
    padded = stretch_clip_to(clip(500), padded_fit, SR)
    assert padded.size == 1_000
    np.testing.assert_array_equal(padded[:500], np.ones(500, dtype=np.float32))
    np.testing.assert_array_equal(padded[500:], np.zeros(500, dtype=np.float32))
    empty = plan_cue(cue(1, 0, 0), 0, FitPolicy.dub())
    assert stretch_clip_to(clip(10), empty, SR).size == 0

    fit = plan_cue(cue(1, 0, 1_000), 2_000, FitPolicy.dub(rate_cap=2.0), next_start_ms=1_000)
    assert fit.compressed is True
    stretched = stretch_clip_to(clip(2_000), fit, SR)
    assert stretched.size == fit.duration_ms == 1_000


def test_timeline_from_fits_rejects_mismatch():
    with pytest.raises(AlignmentError):
        timeline_from_fits([cue(1, 0, 1_000)], [], SR)


# ── stats ────────────────────────────────────────────────────────────────────


def test_alignment_stats_contract() -> None:
    policy = FitPolicy.dub(rate_cap=1.5)
    # Cue 2 (2000..3500) followed closely by Cue 3 (3600..6000) cannot borrow slack,
    # so 3000 ms compresses at rate_cap=1.5 to 2000 ms (ending at 4000, +500 ms overflow)
    # and pushes Cue 3 by +400 ms.
    fits = plan_alignment(
        [cue(1, 0, 1_000), cue(2, 2_000, 3_500), cue(3, 3_600, 6_000)],
        [1_000, 3_000, 1_000],
        policy,
    )
    stats = alignment_stats(fits)
    assert stats.cues == 3
    assert stats.compressed == 1
    assert stats.max_rate == 1.5
    assert stats.overflowed == 1
    assert stats.max_overflow_ms == 500
    assert stats.pushed == 1
    assert stats.total_ms == 5_000
    assert stats.drift_ms == -1_000
    assert stats.clean is False  # one cue overran its window
    stats = alignment_stats([])
    assert stats.cues == 0
    assert stats.clean is True
    assert stats.total_ms == 0

    fits = plan_alignment([cue(1, 0, 1_000)], [800], FitPolicy.dub())
    assert alignment_stats(fits).clean is True


# ── adjusted cues (the exported SRT) ─────────────────────────────────────────


def test_adjusted_cues_contract() -> None:
    cues = [cue(1, 0, 1_000, "a"), cue(2, 2_000, 3_500, "b")]
    fits = plan_alignment(cues, [1_000, 3_000], FitPolicy.dub(rate_cap=1.5))
    retimed = adjusted_cues(cues, fits)
    # The last cue borrows trailing silence at comfort rate (no next cue).
    assert [(c.start_ms, c.end_ms) for c in retimed] == [(0, 1_000), (2_000, 4_609)]
    assert [c.text for c in retimed] == ["a", "b"]
    assert [c.index for c in retimed] == [1, 2]

    cues = [cue(1, 0, 1_000, "a")]
    fits = plan_alignment(cues, [0], FitPolicy.dub())
    retimed = adjusted_cues(cues, fits)
    assert (retimed[0].start_ms, retimed[0].end_ms) == (0, 1_000)

    with pytest.raises(AlignmentError):
        adjusted_cues([cue(1, 0, 1_000)], [])

    # FR-5.3: silent cues take the offset and stay between their neighbours,
    # so the exported SRT keeps its order (they used to keep their raw times).
    cues = [cue(1, 0, 1_000), cue(2, 1_000, 2_000), cue(3, 2_500, 3_500), cue(4, 4_000, 5_000)]
    fits = plan_alignment(cues, [1_000, 0, 1_000, 0], FitPolicy.dub(offset_ms=5_000))
    retimed = adjusted_cues(cues, fits)
    assert [(c.start_ms, c.end_ms) for c in retimed] == [
        (5_000, 6_000),
        (6_000, 7_000),
        (7_500, 8_500),
        (9_000, 10_000),
    ]
    # An overrun into a silent cue's window squeezes it, never reorders it.
    fits = plan_alignment(cues, [2_500, 0, 1_000, 0], FitPolicy.dub(rate_cap=1.0, offset_ms=5_000))
    retimed = adjusted_cues(cues, fits)
    for previous, following in zip(retimed, retimed[1:], strict=False):
        assert following.start_ms >= previous.end_ms >= previous.start_ms >= 5_000


def test_two_stage_elastic_fit_speech_bounds_and_caption_hold() -> None:
    # 1. Speech-onset anchoring uses effective_speech_start_ms / effective_speech_end_ms + offset_ms
    c_speech = Cue(1, 1_000, 3_000, "Hello", speech_start_ms=1_250, speech_end_ms=2_250)
    fit_speech = plan_cue(c_speech, 900, FitPolicy.dub(offset_ms=100))
    assert fit_speech.srt_start_ms == 1_350
    assert fit_speech.srt_end_ms == 3_100
    assert fit_speech.start_ms == 1_350
    assert fit_speech.end_ms == 2_250
    assert fit_speech.rate == 1.0
    assert fit_speech.overflow_ms == 0

    # 2. AC-2: 1400 ms take in a 1000 ms window (400 ms overrun) followed by a 1000 ms silent gap
    # borrows from the gap at COMFORT_RATE (1.15x -> 1217 ms) instead of rushing at 1.40x.
    cues_gap = [Cue(1, 0, 1_000, "One"), Cue(2, 2_000, 3_000, "Two")]
    fits_gap = plan_alignment(cues_gap, [1_400, 800], FitPolicy.dub(rate_cap=1.5))
    assert fits_gap[0].rate == pytest.approx(1.15)
    assert fits_gap[0].start_ms == 0
    assert fits_gap[0].end_ms == 1_217
    assert fits_gap[0].overflow_ms == 0
    assert fits_gap[1].start_ms == 2_000
    assert fits_gap[1].pushed_ms == 0

    # 3. Same-speaker neighbor rate step clamping (|rate - prev_rate| <= 0.20) when slack permits
    spk_cues = [
        Cue(1, 0, 1_000, "Line 1", speaker="SPEAKER_01"),
        Cue(2, 1_200, 2_200, "Line 2", speaker="SPEAKER_01"),
        Cue(3, 4_500, 5_500, "Line 3", speaker="SPEAKER_01"),
    ]
    spk_fits = plan_alignment(spk_cues, [1_000, 3_080, 1_000], FitPolicy.dub(rate_cap=1.5))
    assert spk_fits[0].rate == pytest.approx(1.0)
    assert spk_fits[1].rate == pytest.approx(1.20)
    assert abs(spk_fits[1].rate - spk_fits[0].rate) <= 0.20 + 1e-6
    assert spk_fits[1].end_ms == 1_200 + int(round(3_080 / 1.20))
    assert spk_fits[2].start_ms == 4_500

    # 4. AC-6: adjusted_cues preserves readable subtitle hold without overlapping next_fit.start_ms
    hold_cues = [
        Cue(1, 0, 2_000, "Short dub"),
        Cue(2, 2_100, 3_500, "Next cue"),
    ]
    hold_fits = plan_alignment(hold_cues, [900, 1_200], FitPolicy.dub())
    retimed = adjusted_cues(hold_cues, hold_fits)
    assert hold_fits[0].end_ms == 900
    # Capped at min(srt_end_ms=2000, next_fit.start_ms - 80 = 2020) -> 2000
    assert (retimed[0].start_ms, retimed[0].end_ms) == (0, 2_000)
    # When pushed close to next cue, hold stops 80 ms before next_fit.start_ms
    pushed_cues = [
        Cue(1, 0, 2_000, "Pushed dub"),
        Cue(2, 2_200, 3_500, "Close next"),
    ]
    pushed_fits = [
        plan_cue(pushed_cues[0], 900, FitPolicy.dub(), prev_end_ms=300),
        plan_cue(pushed_cues[1], 1_000, FitPolicy.dub(), prev_end_ms=1_200),
    ]
    retimed_pushed = adjusted_cues(pushed_cues, pushed_fits)
    assert (retimed_pushed[0].start_ms, retimed_pushed[0].end_ms) == (300, 2_200 - 80)

    # 5. FR-5.3 borrow: a take that fits the cue's own caption window plays at
    # 1.0x even when the detected speech is short; the window up to srt_end is
    # never capped by the 1.2 s borrow allowance.
    short_speech = Cue(1, 1_000, 4_000, "Long line", speech_start_ms=1_000, speech_end_ms=1_300)
    fit = plan_cue(short_speech, 2_500, FitPolicy.dub(rate_cap=1.5), next_start_ms=6_000)
    assert fit.rate == 1.0
    assert (fit.start_ms, fit.end_ms, fit.overflow_ms) == (1_000, 3_500, 0)
    # ...but never into the next cue's speech when the captions overlap it.
    overlapping = plan_cue(short_speech, 2_500, FitPolicy.dub(rate_cap=1.5), next_start_ms=2_000)
    assert overlapping.rate == 1.5
    # No next cue: the archived max(srt_end, speech_end + MAX_SLACK_BORROW_MS).
    last = plan_cue(Cue(1, 0, 1_000, "Tail"), 1_400, FitPolicy.dub(rate_cap=1.5))
    assert last.rate == pytest.approx(1.15)
    assert (last.end_ms, last.overflow_ms) == (1_217, 0)


# ── speech units (merge_sentences) ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hello.", True),
        ("Hello!", True),
        ("no terminator", False),
    ],
)
def test_ends_sentence(text, expected):
    assert ends_sentence(text) is expected


def test_speech_units_contract() -> None:
    cues = [cue(1, 0, 1_000), cue(2, 1_000, 2_000)]
    assert speech_units(cues, merge=False) == [(0,), (1,)]

    cues = [
        cue(1, 0, 1_000, "Một hai ba"),
        cue(2, 1_000, 2_000, "bốn năm sáu"),
        cue(3, 2_000, 3_000, "bảy tám chín."),  # ends the sentence
        cue(4, 3_000, 4_000, "mười"),
        cue(5, 10_000, 11_000, "xa"),  # 6 s gap > UNIT_GAP_MS
    ]
    assert speech_units(cues, merge=True) == [(0, 1, 2), (3,), (4,)]

    cues = [cue(i + 1, i * 1_000, (i + 1) * 1_000, "abcdef") for i in range(3)]
    assert speech_units(cues, merge=True, max_chars=10) == [(0,), (1,), (2,)]

    cues = [cue(i + 1, i * 1_000, (i + 1) * 1_000, "abc") for i in range(4)]
    units = speech_units(cues, merge=True)
    assert [index for unit in units for index in unit] == [0, 1, 2, 3]
    assert speech_units([], merge=True) == []

    # Different speakers never merge into one speech unit even without punctuation.
    multi_spk = [
        Cue(1, 0, 1_000, "Xin chào", speaker="SPEAKER_01"),
        Cue(2, 1_100, 2_000, "bạn khỏe không", speaker="SPEAKER_01"),
        Cue(3, 2_100, 3_000, "tôi khỏe cảm ơn", speaker="SPEAKER_02"),
    ]
    assert speech_units(multi_spk, merge=True) == [(0, 1), (2,)]

    # A non-speakable cue (sound cue, ♪, punctuation, emoji) never joins a
    # speaking unit, on either side: each one is its own single-cue unit.
    sound = [
        cue(1, 0, 1_000, "Xin chào"),
        cue(2, 1_000, 2_000, "♪♪"),
        cue(3, 2_000, 3_000, "[MUSIC PLAYING]"),
        cue(4, 3_000, 4_000, "bạn khỏe"),
        cue(5, 4_000, 5_000, "không"),
        cue(6, 5_000, 6_000, "..."),
    ]
    assert speech_units(sound, merge=True) == [(0,), (1,), (2,), (3, 4), (5,)]


# ── splitting a merged unit's audio ──────────────────────────────────────────


def test_split_unit_audio_contract() -> None:
    cues = [cue(1, 0, 1_000, "abcdef"), cue(2, 1_000, 2_000, "abcd")]
    pieces = split_unit_audio(clip(300), [0, 1], cues, SR)
    assert [p.size for p in pieces] == [180, 120]
    assert sum(p.size for p in pieces) == 300

    cues = [cue(1, 0, 1_000, "abc")]
    assert split_unit_audio(clip(10), [], cues, SR) == []
    single = split_unit_audio(clip(10), [0], cues, SR)
    assert len(single) == 1 and single[0].size == 10

    cues = [cue(1, 0, 1_000, "ab"), cue(2, 1_000, 2_000, "cd")]
    pieces = split_unit_audio(clip(100), [0, 1], cues, SR)
    assert [p.size for p in pieces] == [50, 50]

    # Punctuation-aware weighting allocates extra pause weight to cues with commas/periods
    punct_cues = [
        cue(1, 0, 1_000, "abcdef,"),
        cue(2, 1_000, 2_000, "abcdefg"),
    ]
    punct_pieces = split_unit_audio(clip(1_000), [0, 1], punct_cues, SR)
    # Both cues have 7 characters, but cue 1 has a comma pause so it receives > 50% of frames
    assert punct_pieces[0].size > punct_pieces[1].size
    assert sum(p.size for p in punct_pieces) == 1_000

    # FR-5.2: each weighted split snaps to the quietest 10 ms frame within
    # ±150 ms. Flat audio has no quieter frame, so the split stays put.
    unit_cues = [cue(1, 0, 3_000, "Hello there my"), cue(2, 3_100, 5_000, "good friend.")]
    weighted = split_unit_audio(clip(1_500, 0.5), [0, 1], unit_cues, SR)[0].size
    assert weighted == 656
    paused = clip(1_500, 0.5)
    paused[weighted + 30 : weighted + 130] = 0.0  # a 100 ms pause centred 80 ms away
    head, tail = split_unit_audio(paused, [0, 1], unit_cues, SR)
    assert weighted + 30 <= head.size <= weighted + 130
    assert head.size + tail.size == 1_500
    far = clip(1_500, 0.5)
    far[weighted + 200 : weighted + 300] = 0.0  # out of reach
    assert split_unit_audio(far, [0, 1], unit_cues, SR)[0].size == weighted
    # A snap is bounded by its neighbours: the pause just past the second
    # split belongs to that split, never to the first one.
    trio = [cue(1, 0, 1_000, "a" * 10), cue(2, 1_000, 2_000, "b"), cue(3, 2_000, 3_000, "c" * 10)]
    audio = clip(1_050, 0.5)
    audio[560:600] = 0.0
    assert [p.size for p in split_unit_audio(audio, [0, 1, 2], trio, SR)] == [500, 70, 480]

    # AC-1: Pre-fit edge-silence trimming in render_alignment / plan_alignment_for_clips
    # A 1.0 s speech take with 300 ms of vocoder silence (150 ms head + 150 ms tail)
    # fits a 1.1 s cue window at 1.00x instead of being compressed to ~1.18x.
    padded_speech = np.concatenate(
        [
            np.zeros(150, dtype=np.float32),
            np.full(1_000, 0.5, dtype=np.float32),
            np.zeros(150, dtype=np.float32),
        ]
    )
    ac1_track = render_alignment(
        [cue(1, 0, 1_100, "Fit at 1x")],
        [padded_speech],
        FitPolicy.dub(rate_cap=1.5),
        SR,
    )
    assert ac1_track.fits[0].rate == 1.0
    assert ac1_track.fits[0].compressed is False
    assert ac1_track.fits[0].duration_ms == 1_030  # 1000 ms speech + 2 * 15 ms pad
