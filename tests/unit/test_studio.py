"""Studio model + basic ops (Task 1)."""

import numpy as np
import pytest

from vienetts_app.core.studio import (
    ENVELOPE_BUCKETS,
    CutOp,
    FadeOp,
    GainOp,
    GapOp,
    NormalizeOp,
    SilenceTrimOp,
    SpeedOp,
    StudioClip,
    StudioProject,
    TrimOp,
    cut_range,
    delete_clip,
    effective_controls,
    envelope_for,
    parameter_key,
    push_op,
    render_overview,
    render_project,
    set_parameter_op,
    trim_range,
)


def _tone(n=4800, freq=440.0):
    t = np.arange(n, dtype=np.float32) / 48_000
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


def _project(audio):
    return StudioProject(
        clips=(StudioClip(id="c0", label="P1", text="hello", audio=audio),), ops=()
    )


def test_concat_single_clip_round_trips():
    audio = _tone()
    out = render_project(_project(audio))
    assert np.allclose(out, audio, atol=1e-6)


def test_gain_6db_doubles_amplitude():
    out = render_project(push_op(_project(_tone()), GainOp(db=6.0)))
    assert np.allclose(out, _tone() * (10.0 ** (6.0 / 20.0)), atol=1e-5)


def test_gain_clamps_at_minus_20_plus_12():
    import pytest

    from vienetts_app.core.studio import GainOp as G

    with pytest.raises(ValueError):
        G(db=13.0)
    with pytest.raises(ValueError):
        G(db=-21.0)


def test_normalize_sets_peak():
    audio = _tone() * 0.25
    out = render_project(push_op(_project(audio), NormalizeOp(peak=0.9)))
    assert abs(float(np.max(np.abs(out))) - 0.9) < 1e-3


class TestTimelineOps:
    def test_speed_passthrough_near_one_is_bit_identical(self):
        from vienetts_app.core.studio import SpeedOp, push_op, render_project

        audio = _tone(4800)
        out = render_project(push_op(_project(audio), SpeedOp(factor=1.0)))
        assert np.array_equal(out, audio)

    def test_speed_out_of_range_rejected(self):
        import pytest

        from vienetts_app.core.studio import SpeedOp

        with pytest.raises(ValueError):
            SpeedOp(factor=2.5)
        with pytest.raises(ValueError):
            SpeedOp(factor=0.0)

    def test_gap_inserts_silence_between_clips(self):
        from vienetts_app.core.studio import (
            GapOp,
            StudioClip,
            StudioProject,
            push_op,
            render_project,
        )

        a = np.ones(100, dtype=np.float32)
        p = StudioProject(
            clips=(
                StudioClip(id="a", label="A", text="a", audio=a),
                StudioClip(id="b", label="B", text="b", audio=a),
            ),
            ops=(),
        )
        out = render_project(push_op(p, GapOp(ms=1000)))
        assert len(out) == 200 + 48_000
        assert np.allclose(out[100 : 100 + 48_000], 0.0)

    def test_move_clip_reorders_concat(self):
        from vienetts_app.core.studio import StudioClip, StudioProject, move_clip, render_project

        a = np.zeros(10, dtype=np.float32)
        b = np.ones(10, dtype=np.float32)
        p = StudioProject(
            clips=(
                StudioClip(id="a", label="A", text="a", audio=a),
                StudioClip(id="b", label="B", text="b", audio=b),
            ),
            ops=(),
        )
        out = render_project(move_clip(p, "b", 0))
        assert np.allclose(out[:10], 1.0) and np.allclose(out[10:], 0.0)

    def test_undo_pops_last_op(self):
        from vienetts_app.core.studio import GainOp, pop_op, push_op

        p = push_op(push_op(_project(_tone(100)), GainOp(db=6.0)), GainOp(db=-6.0))
        assert len(pop_op(p).ops) == 1

    def test_reset_ops_clears_all_applied_ops(self):
        from vienetts_app.core.studio import GainOp, push_op, reset_ops

        p = push_op(push_op(_project(_tone(100)), GainOp(db=6.0)), GainOp(db=-6.0))
        assert len(p.ops) == 2
        reset = reset_ops(p)
        assert len(reset.ops) == 0
        assert reset.clips == p.clips

    def test_splice_replaces_clip_with_crossfade(self):
        from vienetts_app.core.studio import (
            StudioClip,
            StudioProject,
            render_project,
            splice_clip_audio,
        )

        a = np.zeros(1000, dtype=np.float32)
        new = np.ones(1000, dtype=np.float32)
        p = StudioProject(clips=(StudioClip(id="a", label="A", text="old text", audio=a),), ops=())
        spliced = splice_clip_audio(p, "a", new, new_text="new edited text")
        out = render_project(spliced)
        assert np.allclose(out[-100:], 1.0)  # tail fully replaced past the 10 ms blend
        assert spliced.clips[0].text == "new edited text"

    def test_envelope_has_160_buckets(self):
        from vienetts_app.core.studio import project_envelope

        env = project_envelope(_project(_tone(48_000)))
        assert len(env) == 160 and all(0.0 <= v <= 1.0 for v in env)

    def test_envelope_tracks_level_ops_against_dry_peak(self):
        from vienetts_app.core.studio import GainOp, push_op, render_overview

        base = _project(_tone(48_000) * 0.25)
        _, _, env_base = render_overview(base)
        assert max(env_base) == 1.0
        _, _, env_gain = render_overview(push_op(base, GainOp(db=6.0)))
        assert all(0.0 <= v <= 1.0 for v in env_gain)
        assert env_gain != env_base  # per-render normalization hid this
        assert sum(env_gain) > sum(env_base)

    def test_envelope_clips_hot_mix_and_zeroes_silence(self):
        import numpy as np

        from vienetts_app.core.studio import GainOp, push_op, render_overview

        _, _, env_hot = render_overview(push_op(_project(_tone(4800)), GainOp(db=12.0)))
        assert all(0.0 <= v <= 1.0 for v in env_hot)
        assert max(env_hot) == 1.0
        _, _, env_silence = render_overview(_project(np.zeros(4800, dtype=np.float32)))
        assert env_silence == [0.0] * 160

    def test_undo_gain_restores_envelope_exactly(self):
        from vienetts_app.core.studio import GainOp, pop_op, push_op, render_overview

        base = _project(_tone(48_000) * 0.25)
        _, _, env_base = render_overview(base)
        _, _, env_undone = render_overview(pop_op(push_op(base, GainOp(db=6.0))))
        assert env_undone == env_base


class TestLoaders:
    def test_artifact_loader_splits_one_clip_per_paragraph(self, tmp_path):
        from vienetts_app.core.audio import write_wav_file
        from vienetts_app.core.studio import load_project_from_artifact, render_project

        audio = _tone(48_000)
        path = write_wav_file(audio, tmp_path / "art.wav")
        project = load_project_from_artifact(str(path), "first para\n\nsecond para")
        assert [c.label for c in project.clips] == ["1", "2"]
        assert [c.text for c in project.clips] == ["first para", "second para"]
        assert len(render_project(project)) == 48_000

    def test_artifact_loader_rejects_rate_mismatch(self, tmp_path):
        import pytest
        import soundfile as sf

        from vienetts_app.core.studio import load_project_from_artifact

        p = tmp_path / "odd.wav"
        sf.write(str(p), _tone(1000), 24_000)
        with pytest.raises(ValueError):
            load_project_from_artifact(str(p), "hi")

    def test_chapter_loader_skips_missing_chapters(self, tmp_path):
        from pathlib import Path

        from vienetts_app.core.audio import write_wav_file
        from vienetts_app.core.studio import load_project_from_chapters

        write_wav_file(_tone(1000), tmp_path / "ch_0000.wav")

        class FakeStore:
            def chapter_wav_path(self, book_id, index):
                return Path(tmp_path) / f"ch_{index:04d}.wav"

        project = load_project_from_chapters(FakeStore(), "b1", [0, 1], ["c0", "c1"])
        assert [c.id for c in project.clips] == ["ch0"]


class TestParameterRack:
    """set_parameter_op must set, not stack, and effective_controls must mirror the render."""

    def test_parameter_key_maps_rack_ops_and_leaves_one_shots_alone(self):
        assert parameter_key(GainOp(db=0.0)) == "gain"
        assert parameter_key(SpeedOp(factor=1.0)) == "speed"
        assert parameter_key(GapOp(ms=0)) == "gap"
        assert parameter_key(FadeOp(edge="in", ms=0)) == "fade_in"
        assert parameter_key(FadeOp(edge="out", ms=0)) == "fade_out"
        assert parameter_key(NormalizeOp(peak=1.0)) is None
        assert parameter_key(SilenceTrimOp()) is None
        assert parameter_key(TrimOp(0, 10)) is None
        assert parameter_key(CutOp(0, 10)) is None

    def test_set_parameter_op_replaces_last_same_kind_in_place(self):
        p = push_op(push_op(_project(_tone(100)), GainOp(db=6.0)), NormalizeOp(peak=0.5))
        p = push_op(p, GainOp(db=-3.0))
        out = set_parameter_op(p, GainOp(db=3.0))
        assert len(out.ops) == len(p.ops)  # replaced, not appended
        assert out.ops[2] == GainOp(db=3.0)  # same index as the op it replaced
        assert out.ops[0] == GainOp(db=6.0)  # the earlier same-key op is untouched
        assert out.ops[1] == NormalizeOp(peak=0.5)
        assert out.clips == p.clips

    def test_set_parameter_op_appends_one_shot_ops(self):
        base = _project(_tone(100))
        one_shots = (NormalizeOp(peak=0.8), SilenceTrimOp(), TrimOp(0, 50), CutOp(10, 20))
        for op in one_shots:
            out = set_parameter_op(base, op)
            assert out.ops == base.ops + (op,)

    def test_set_parameter_op_gain_does_not_compound(self):
        base = _project(_tone())
        once = set_parameter_op(base, GainOp(db=3.0))
        twice = set_parameter_op(set_parameter_op(base, GainOp(db=3.0)), GainOp(db=3.0))
        assert len(twice.ops) == 1
        # mirrors test_gain_6db_doubles_amplitude: one +3 dB Apply, not +6 dB
        single = float(np.max(np.abs(render_project(once))))
        doubled = float(np.max(np.abs(render_project(twice))))
        assert doubled == pytest.approx(single, rel=1e-6)
        assert doubled == pytest.approx(
            float(np.max(np.abs(_tone()))) * 10.0 ** (3.0 / 20.0), rel=1e-5
        )

    def test_set_parameter_op_speed_does_not_compound(self):
        base = _project(_tone(4800))
        once = set_parameter_op(base, SpeedOp(factor=1.15))
        twice = set_parameter_op(set_parameter_op(base, SpeedOp(factor=1.15)), SpeedOp(factor=1.15))
        assert len(twice.ops) == 1
        assert len(render_project(twice)) == len(render_project(once))

    def test_effective_controls_mixed_stack(self):
        ops = (
            GainOp(db=3.0),
            GainOp(db=-1.5),
            SpeedOp(factor=1.25),
            SpeedOp(factor=1.2),
            GapOp(ms=750),
            FadeOp(edge="in", ms=120),
        )
        controls = effective_controls(StudioProject(clips=_project(_tone()).clips, ops=ops))
        # dB is logarithmic, so stacked gain ops add: 3.0 + -1.5 = 1.5 dB.
        assert controls["gain"] == pytest.approx(1.5)
        # a speed factor is a ratio, so stacked speed ops multiply: 1.25 * 1.2 = 1.5x.
        assert controls["speed"] == pytest.approx(1.5)
        assert controls["gap"] == 750
        assert controls["fade"] == 120
        assert controls["fadeIn"] == 120
        assert controls["fadeOut"] == 0

    def test_effective_controls_defaults_with_no_ops(self):
        assert effective_controls(_project(_tone(100))) == {
            "gain": 0.0,
            "speed": 1.0,
            "gap": 500,
            "fade": 200,
            "fadeIn": 0,
            "fadeOut": 0,
        }

    def test_effective_controls_fade_keeps_last_and_defaults_without_one(self):
        ops = (FadeOp(edge="out", ms=80), FadeOp(edge="in", ms=150))
        controls = effective_controls(StudioProject(clips=_project(_tone(100)).clips, ops=ops))
        assert controls["fade"] == 150  # last FadeOp in the chain, whatever its edge
        assert (controls["fadeIn"], controls["fadeOut"]) == (150, 80)


def test_delete_clip_removes_one_clip_and_its_audio():
    a = np.ones(10, dtype=np.float32)
    b = np.zeros(10, dtype=np.float32)
    c = np.full(10, 2.0, dtype=np.float32)
    project = StudioProject(
        clips=(
            StudioClip(id="a", label="A", text="a", audio=a),
            StudioClip(id="b", label="B", text="b", audio=b),
            StudioClip(id="c", label="C", text="c", audio=c),
        ),
        ops=(GainOp(db=3.0),),
    )
    out = delete_clip(project, "b")
    assert [clip.id for clip in out.clips] == ["a", "c"]  # ids are never renumbered
    assert out.ops == project.ops  # ops survive a clip delete
    gain = 10.0 ** (3.0 / 20.0)
    rendered = render_project(out)
    assert len(rendered) == 20  # b's audio is gone from the mix
    assert np.allclose(rendered[:10], a * gain)
    assert np.allclose(rendered[10:], c * gain)


def test_delete_clip_rejects_unknown_id_and_last_clip():
    single = _project(_tone(100))
    with pytest.raises(ValueError):
        delete_clip(single, "nope")
    with pytest.raises(ValueError):
        delete_clip(single, "c0")  # a project with no clips cannot be rendered
    two = delete_clip(
        StudioProject(
            clips=(
                StudioClip(id="a", label="A", text="a", audio=np.ones(10, dtype=np.float32)),
                StudioClip(id="b", label="B", text="b", audio=np.zeros(10, dtype=np.float32)),
            ),
            ops=(),
        ),
        "b",
    )
    assert [clip.id for clip in two.clips] == ["a"]


class TestCutOp:
    def test_cut_op_splices_out_the_middle_range(self):
        ramp = np.arange(100, dtype=np.float32)
        out = render_project(push_op(_project(ramp), CutOp(20, 30)))
        assert len(out) == 90  # shrank by exactly b - a
        assert np.array_equal(out[:20], ramp[:20])  # left side is the original
        assert np.array_equal(out[20:], ramp[30:])  # right side is the original

    def test_cut_op_end_frame_minus_one_cuts_to_the_end(self):
        ramp = np.arange(100, dtype=np.float32)
        out = render_project(push_op(_project(ramp), CutOp(60, -1)))
        assert len(out) == 60
        assert np.array_equal(out, ramp[:60])

    def test_cut_op_empty_range_is_a_noop(self):
        ramp = np.arange(100, dtype=np.float32)
        # start == len / past the end clamps to an empty range, so the mix survives
        assert np.array_equal(render_project(push_op(_project(ramp), CutOp(100, 150))), ramp)
        assert np.array_equal(render_project(push_op(_project(ramp), CutOp(200, -1))), ramp)

    def test_cut_op_validates_its_range(self):
        with pytest.raises(ValueError):
            CutOp(-1, 10)
        with pytest.raises(ValueError):
            CutOp(10, 10)
        with pytest.raises(ValueError):
            CutOp(10, 5)

    def test_cut_op_composes_after_trim_like_any_other_op(self):
        ramp = np.arange(100, dtype=np.float32)
        # trim to [0, 60) then cut [10, 20) inside the trimmed mix
        out = render_project(cut_range(trim_range(_project(ramp), 0, 60), 10, 20))
        assert len(out) == 50
        assert np.array_equal(out[:10], ramp[:10])
        assert np.array_equal(out[10:], ramp[20:60])


def test_trim_range_and_cut_range_push_the_expected_ops():
    base = _project(_tone(100))
    assert trim_range(base, 10, 50).ops == (TrimOp(10, 50),)
    assert cut_range(base, 10, 50).ops == (CutOp(10, 50),)
    stacked = cut_range(trim_range(base, 0, 80), 10, 20)
    assert stacked.ops == (TrimOp(0, 80), CutOp(10, 20))  # appended, not replaced
    assert stacked.clips == base.clips


class TestEnvelopeFor:
    def test_envelope_for_normalises_to_its_own_peak(self):
        env = envelope_for(_tone(4800), buckets=8)
        assert len(env) == 8
        assert all(0.0 <= v <= 1.0 for v in env)
        assert max(env) == 1.0

    def test_envelope_for_uses_the_default_bucket_count(self):
        env = envelope_for(_tone(48_000))
        assert len(env) == ENVELOPE_BUCKETS == 160

    def test_envelope_for_divides_by_reference_peak_and_clamps(self):
        audio = _tone(4800)
        own_peak = float(np.max(np.abs(audio)))
        quieter = envelope_for(audio, buckets=8, reference_peak=own_peak * 2)
        assert max(quieter) == pytest.approx(0.5, rel=1e-6)
        assert all(0.0 <= v <= 1.0 for v in quieter)
        hot = envelope_for(audio, buckets=8, reference_peak=own_peak / 4)
        assert max(hot) == 1.0  # clamped, never above 1.0
        assert all(0.0 <= v <= 1.0 for v in hot)

    def test_envelope_for_empty_audio_is_all_zero(self):
        assert envelope_for(np.zeros(0, dtype=np.float32), buckets=4) == [0.0] * 4


def test_render_overview_still_normalises_against_the_dry_peak():
    base = _project(_tone(48_000) * 0.25)
    _, _, env_base = render_overview(base)
    assert max(env_base) == 1.0
    mix, _, env_gain = render_overview(push_op(base, GainOp(db=6.0)))
    assert all(0.0 <= v <= 1.0 for v in env_gain)
    assert env_gain != env_base  # per-render normalization hid this
    assert sum(env_gain) > sum(env_base)
    # render_overview delegates the bucketing and passes the dry clip peak
    dry_peak = float(np.max(np.abs(base.clips[0].audio)))
    assert env_gain == envelope_for(mix, reference_peak=dry_peak)


# ── clip provenance (Phase 5 Task 5.4) ───────────────────────────────────────


def _context(profile="vieneu", **overrides):
    """One engine identity for provenance tests (never a real model)."""
    from vienetts_app.core.synthesis_context import context_for

    kwargs = {"language": "vi" if profile == "vieneu" else "zh"}
    kwargs.update(overrides)
    return context_for(profile, **kwargs)


class TestClipProvenance:
    """A clip records the engine that produced its audio (None = unknown)."""

    def test_a_clip_without_provenance_is_unknown(self):
        assert StudioClip(id="c0", label="1", text="hi", audio=_tone()).context is None

    def test_splice_replaces_the_provenance_with_the_new_engine(self):
        from vienetts_app.core.studio import splice_clip_audio

        first = _context()
        second = _context("qwen_custom_0_6b", voice_id="Vivian")
        project = StudioProject(
            clips=(
                StudioClip(id="c0", label="1", text="a", audio=_tone(), context=first),
                StudioClip(id="c1", label="2", text="b", audio=_tone(), context=first),
            ),
            ops=(GainOp(db=3.0),),
        )

        spliced = splice_clip_audio(project, "c1", _tone(9600), new_text="b2", context=second)

        assert [c.context for c in spliced.clips] == [first, second]
        assert spliced.clips[1].text == "b2"
        assert spliced.ops == project.ops  # editing stays engine-independent

    def test_splice_without_a_context_records_an_unknown_engine(self):
        from vienetts_app.core.studio import splice_clip_audio

        project = StudioProject(
            clips=(StudioClip(id="c0", label="1", text="a", audio=_tone(), context=_context()),)
        )
        spliced = splice_clip_audio(project, "c0", _tone(9600))
        assert spliced.clips[0].context is None

    def test_artifact_loader_stamps_every_clip_with_the_take_identity(self, tmp_path):
        from vienetts_app.core.audio import write_wav_file
        from vienetts_app.core.studio import load_project_from_artifact

        context = _context()
        path = write_wav_file(_tone(48_000), tmp_path / "art.wav")
        project = load_project_from_artifact(str(path), "first para\n\nsecond para", context)
        assert [c.context for c in project.clips] == [context, context]
        # A caller that cannot say (a hand-made file) leaves them unknown.
        assert load_project_from_artifact(str(path), "one").clips[0].context is None

    def test_chapter_loader_uses_the_recorded_chapter_provenance(self, tmp_path):
        from pathlib import Path

        from vienetts_app.core.audio import write_wav_file
        from vienetts_app.core.studio import load_project_from_chapters

        write_wav_file(_tone(1000), tmp_path / "ch_0000.wav")
        write_wav_file(_tone(1000), tmp_path / "ch_0001.wav")
        context = _context()

        class FakeStore:
            def chapter_wav_path(self, book_id, index):
                return Path(tmp_path) / f"ch_{index:04d}.wav"

        project = load_project_from_chapters(
            FakeStore(), "b1", [0, 1], ["c0", "c1"], contexts={1: context}
        )
        # Chapter 1 has no recorded identity: its clip stays unknown, so a
        # re-synthesis applies the same legacy rule as the audiobook cache.
        assert [c.context for c in project.clips] == [None, context]

    def test_render_ignores_provenance(self):
        # Editing and export are engine-independent: the same audio renders
        # identically whatever produced it.
        audio = _tone()
        plain = StudioProject(clips=(StudioClip(id="c0", label="1", text="a", audio=audio),))
        stamped = StudioProject(
            clips=(StudioClip(id="c0", label="1", text="a", audio=audio, context=_context()),)
        )
        assert np.array_equal(render_project(plain), render_project(stamped))
