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
    MAX_GAP_LIMIT_MS,
    MAX_OFFSET_MS,
    MAX_RATE_CAP,
    MIN_RATE_CAP,
    MODE_DUB,
    MODE_TRANSCRIPT,
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


def test_fit_policy_defaults_and_constructors():
    assert FitPolicy().mode == MODE_DUB
    assert FitPolicy.dub().mode == MODE_DUB
    assert FitPolicy.dub().rate_cap == 1.5
    assert FitPolicy.dub().max_gap_ms == 0  # uncapped: dub wants the real pauses
    transcript = FitPolicy.transcript()
    assert transcript.mode == MODE_TRANSCRIPT
    assert transcript.rate_cap == MIN_RATE_CAP  # transcript never stretches
    assert transcript.max_gap_ms == 1500
    assert transcript.merge_sentences is True


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "nope"},
        {"rate_cap": 0.5},
        {"rate_cap": 3.0},
        {"rate_cap": True},  # bool is not a number here
        {"rate_cap": "1.2"},
        {"max_gap_ms": -1},
        {"max_gap_ms": MAX_GAP_LIMIT_MS + 1},
        {"max_gap_ms": 1.5},
        {"offset_ms": MAX_OFFSET_MS + 1},
        {"offset_ms": -MAX_OFFSET_MS - 1},
        {"offset_ms": 1.5},
        {"merge_sentences": "yes"},
    ],
)
def test_fit_policy_rejects_invalid_fields(kwargs):
    with pytest.raises(ValueError):
        FitPolicy(**kwargs)


def test_policy_summary_is_qml_shaped():
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
    ("value", "expected"),
    [("1.2", 1.2), (5, MAX_RATE_CAP), (0.2, MIN_RATE_CAP), ("abc", 1.5), (None, 1.5)],
)
def test_clamp_rate_cap_never_raises(value, expected):
    assert clamp_rate_cap(value) == expected


def test_clamp_rate_cap_handles_non_finite():
    assert clamp_rate_cap(float("nan")) == 1.5
    assert clamp_rate_cap(float("inf")) == 1.5


# ── frame / millisecond rounding ─────────────────────────────────────────────


def test_frames_and_ms_round_trip_at_default_rate():
    for ms in (0, 1, 1000, 3_723_004):
        assert ms_for_frames(frames_for_ms(ms)) == ms
    assert frames_for_ms(1_000, 48_000) == 48_000
    assert frames_for_ms(-5) == 0
    assert ms_for_frames(-5) == 0


# ── dub mode: SRT clock is master ────────────────────────────────────────────


def test_dub_compresses_an_overlong_take_to_the_cap():
    # 3000 ms of speech in a 1500 ms window at cap 1.5 -> exactly 2000 ms.
    fit = plan_cue(cue(2, 2_000, 3_500), 3_000, FitPolicy.dub(rate_cap=1.5), prev_end_ms=1_000)
    assert fit.rate == 1.5
    assert fit.compressed is True
    assert fit.start_ms == 2_000
    assert fit.duration_ms == 2_000
    assert fit.end_ms == 4_000
    assert fit.overflow_ms == 500  # 4000 past its own subtitle end
    assert fit.pushed_ms == 0


def test_dub_never_slows_down_and_leaves_slack_as_silence():
    fit = plan_cue(cue(1, 0, 1_000), 500, FitPolicy.dub())
    assert fit.rate == 1.0
    assert fit.compressed is False
    assert fit.duration_ms == 500
    assert fit.end_ms == 500  # stops early: the rest of the window is silence
    assert fit.overflow_ms == 0


def test_dub_pushes_a_cue_when_the_previous_take_overran():
    policy = FitPolicy.dub(rate_cap=1.5)
    fits = plan_alignment(
        [cue(1, 0, 1_000), cue(2, 2_000, 3_500), cue(3, 5_000, 6_000)],
        [1_000, 3_000, 1_000],
        policy,
    )
    assert [(f.start_ms, f.end_ms) for f in fits] == [
        (0, 1_000),
        (2_000, 4_000),
        (5_000, 6_000),  # 4000 < 5000: it still lands on its own subtitle time
    ]
    # Only the overlong cue is compressed; the others are untouched.
    assert [f.rate for f in fits] == [1.0, 1.5, 1.0]
    assert [f.pushed_ms for f in fits] == [0, 0, 0]


def test_dub_pushes_successors_when_compression_is_not_enough():
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


def test_dub_never_lets_cues_overlap_even_without_srt_gaps():
    policy = FitPolicy.dub(rate_cap=1.0)  # no compression allowed
    fits = plan_alignment(
        [cue(1, 0, 1_000), cue(2, 1_000, 2_000), cue(3, 2_000, 3_000)],
        [1_500, 1_500, 1_500],
        policy,
    )
    for previous, following in zip(fits, fits[1:], strict=False):
        assert following.start_ms >= previous.end_ms


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


def test_transcript_never_compresses_and_keeps_natural_pace():
    policy = FitPolicy.transcript()
    fit = plan_cue(cue(1, 2_000, 2_500), 3_000, policy)
    assert fit.rate == 1.0
    assert fit.compressed is False
    assert fit.duration_ms == 3_000
    assert fit.overflow_ms == 2_500  # overruns its window, by design


def test_transcript_reproduces_srt_pauses_but_caps_them():
    policy = FitPolicy.transcript(max_gap_ms=1_500)
    # Previous take ends at 1000; the next subtitle starts 100 s later.
    fit = plan_cue(cue(2, 100_000, 101_000), 1_000, policy, prev_end_ms=1_000)
    assert fit.start_ms == 2_500  # 1000 + the 1500 ms cap, not 100_000
    assert fit.pushed_ms == -97_500


def test_transcript_pushes_later_cues_after_an_overrun():
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


def test_plan_alignment_rejects_length_mismatch():
    with pytest.raises(AlignmentError):
        plan_alignment([cue(1, 0, 1_000)], [1_000, 2_000], FitPolicy.dub())


def test_plan_alignment_for_clips_measures_the_audio():
    fits = plan_alignment_for_clips(
        [cue(1, 0, 2_000)], [clip(3_000)], FitPolicy.dub(rate_cap=1.5), SR
    )
    assert fits[0].natural_ms == 3_000
    assert fits[0].rate == 1.5
    assert fits[0].duration_ms == 2_000


def test_render_alignment_places_audio_and_builds_the_timeline():
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


def test_render_alignment_validates_its_inputs():
    with pytest.raises(AlignmentError):
        render_alignment([cue(1, 0, 1_000)], [clip(1_000), clip(1_000)], FitPolicy.dub(), SR)
    with pytest.raises(AlignmentError):
        render_alignment([cue(1, 0, 1_000)], [clip(1_000)], FitPolicy.dub(), 0)


def test_render_alignment_of_nothing_is_empty():
    track = render_alignment([], [], FitPolicy.dub(), SR)
    assert track.audio.size == 0
    assert track.fits == ()
    assert track.timeline.segments == ()


def test_stretch_clip_to_forces_the_exact_planned_length():
    long_fit = plan_cue(cue(1, 0, 1_000), 1_500, FitPolicy.dub())
    trimmed = stretch_clip_to(clip(1_500), long_fit, SR)
    assert trimmed.size == long_fit.duration_ms  # 1000, not the clip's 1500
    padded_fit = plan_cue(cue(1, 0, 1_000), 1_000, FitPolicy.dub())
    padded = stretch_clip_to(clip(500), padded_fit, SR)
    assert padded.size == 1_000
    np.testing.assert_array_equal(padded[:500], np.ones(500, dtype=np.float32))
    np.testing.assert_array_equal(padded[500:], np.zeros(500, dtype=np.float32))
    empty = plan_cue(cue(1, 0, 0), 0, FitPolicy.dub())
    assert stretch_clip_to(clip(10), empty, SR).size == 0


def test_stretch_clip_to_compresses_through_wsola():
    fit = plan_cue(cue(1, 0, 1_000), 2_000, FitPolicy.dub(rate_cap=2.0))
    assert fit.compressed is True
    stretched = stretch_clip_to(clip(2_000), fit, SR)
    assert stretched.size == fit.duration_ms == 1_000


def test_timeline_from_fits_rejects_mismatch():
    with pytest.raises(AlignmentError):
        timeline_from_fits([cue(1, 0, 1_000)], [], SR)


# ── stats ────────────────────────────────────────────────────────────────────


def test_alignment_stats_summarizes_the_fit():
    policy = FitPolicy.dub(rate_cap=1.5)
    fits = plan_alignment(
        [cue(1, 0, 1_000), cue(2, 2_000, 3_500), cue(3, 5_000, 6_000)],
        [1_000, 3_000, 1_000],
        policy,
    )
    stats = alignment_stats(fits)
    assert stats.cues == 3
    assert stats.compressed == 1
    assert stats.max_rate == 1.5
    assert stats.overflowed == 1
    assert stats.max_overflow_ms == 500
    assert stats.pushed == 0
    assert stats.total_ms == 6_000
    assert stats.drift_ms == 0
    assert stats.clean is False  # one cue overran its window


def test_alignment_stats_of_an_empty_plan_is_clean():
    stats = alignment_stats([])
    assert stats.cues == 0
    assert stats.clean is True
    assert stats.total_ms == 0


def test_alignment_stats_clean_when_every_cue_fits():
    fits = plan_alignment([cue(1, 0, 1_000)], [800], FitPolicy.dub())
    assert alignment_stats(fits).clean is True


# ── adjusted cues (the exported SRT) ─────────────────────────────────────────


def test_adjusted_cues_follow_the_rendered_audio():
    cues = [cue(1, 0, 1_000, "a"), cue(2, 2_000, 3_500, "b")]
    fits = plan_alignment(cues, [1_000, 3_000], FitPolicy.dub(rate_cap=1.5))
    retimed = adjusted_cues(cues, fits)
    assert [(c.start_ms, c.end_ms) for c in retimed] == [(0, 1_000), (2_000, 4_000)]
    assert [c.text for c in retimed] == ["a", "b"]
    assert [c.index for c in retimed] == [1, 2]


def test_adjusted_cues_keep_the_original_window_for_a_silent_cue():
    cues = [cue(1, 0, 1_000, "a")]
    fits = plan_alignment(cues, [0], FitPolicy.dub())
    retimed = adjusted_cues(cues, fits)
    assert (retimed[0].start_ms, retimed[0].end_ms) == (0, 1_000)


def test_adjusted_cues_rejects_mismatch():
    with pytest.raises(AlignmentError):
        adjusted_cues([cue(1, 0, 1_000)], [])


# ── speech units (merge_sentences) ───────────────────────────────────────────


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Hello.", True),
        ("Hello!", True),
        ("Really?", True),
        ("Xong…", True),
        ('He said "go."', True),
        ("no terminator", False),
        ("", False),
        ("   ", False),
    ],
)
def test_ends_sentence(text, expected):
    assert ends_sentence(text) is expected


def test_speech_units_without_merging_is_one_unit_per_cue():
    cues = [cue(1, 0, 1_000), cue(2, 1_000, 2_000)]
    assert speech_units(cues, merge=False) == [(0,), (1,)]


def test_speech_units_merge_until_a_sentence_gap_or_length_guard():
    cues = [
        cue(1, 0, 1_000, "Một hai ba"),
        cue(2, 1_000, 2_000, "bốn năm sáu"),
        cue(3, 2_000, 3_000, "bảy tám chín."),  # ends the sentence
        cue(4, 3_000, 4_000, "mười"),
        cue(5, 10_000, 11_000, "xa"),  # 6 s gap > UNIT_GAP_MS
    ]
    assert speech_units(cues, merge=True) == [(0, 1, 2), (3,), (4,)]


def test_speech_units_split_on_the_character_cap():
    cues = [cue(i + 1, i * 1_000, (i + 1) * 1_000, "abcdef") for i in range(3)]
    assert speech_units(cues, merge=True, max_chars=10) == [(0,), (1,), (2,)]


def test_speech_units_covers_every_cue_and_tolerates_empty():
    cues = [cue(i + 1, i * 1_000, (i + 1) * 1_000, "abc") for i in range(4)]
    units = speech_units(cues, merge=True)
    assert [index for unit in units for index in unit] == [0, 1, 2, 3]
    assert speech_units([], merge=True) == []


# ── splitting a merged unit's audio ──────────────────────────────────────────


def test_split_unit_audio_is_proportional_and_exact_in_total():
    cues = [cue(1, 0, 1_000, "abcdef"), cue(2, 1_000, 2_000, "abcd")]
    pieces = split_unit_audio(clip(300), [0, 1], cues, SR)
    assert [p.size for p in pieces] == [180, 120]
    assert sum(p.size for p in pieces) == 300


def test_split_unit_audio_edge_cases():
    cues = [cue(1, 0, 1_000, "abc")]
    assert split_unit_audio(clip(10), [], cues, SR) == []
    single = split_unit_audio(clip(10), [0], cues, SR)
    assert len(single) == 1 and single[0].size == 10


def test_split_unit_audio_is_proportional_for_equal_weights():
    cues = [cue(1, 0, 1_000, "ab"), cue(2, 1_000, 2_000, "cd")]
    pieces = split_unit_audio(clip(100), [0, 1], cues, SR)
    assert [p.size for p in pieces] == [50, 50]
