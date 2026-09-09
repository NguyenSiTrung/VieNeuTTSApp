"""Studio model + basic ops (Task 1)."""

import numpy as np

from vienetts_app.core.studio import (
    GainOp,
    NormalizeOp,
    StudioClip,
    StudioProject,
    push_op,
    render_project,
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
        p = StudioProject(clips=(StudioClip(id="a", label="A", text="a", audio=a),), ops=())
        out = render_project(splice_clip_audio(p, "a", new))
        assert np.allclose(out[-100:], 1.0)  # tail fully replaced past the 10 ms blend

    def test_envelope_has_160_buckets(self):
        from vienetts_app.core.studio import project_envelope

        env = project_envelope(_project(_tone(48_000)))
        assert len(env) == 160 and all(0.0 <= v <= 1.0 for v in env)


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
