"""Controller studio wiring (Task 4)."""

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal

from vienetts_app.core.artifacts import SynthesisArtifact
from vienetts_app.core.audio import write_wav_file
from vienetts_app.ui.bg_ops import run_sync
from vienetts_app.ui.controller import AppController


def _tone(n=9600, freq=440.0):
    t = np.arange(n, dtype=np.float32) / 48_000
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class FakeEngine:
    def __init__(self, **kwargs):
        self.sample_rate = 48_000
        self.closed = False

    def close(self):
        self.closed = True


class FakeWorker(QObject):
    progress = Signal(object)
    chunk_ready = Signal(object)
    terminal = Signal(object)

    def __init__(self, engine):
        super().__init__()
        self.engine = engine
        self.submitted = []

    def start(self):
        pass

    def submit(self, job):
        self.submitted.append(job)
        return True

    def cancel_job(self, job_id):
        return True

    def cancel_owner(self, owner):
        return 0

    def stop(self):
        pass


class FakePlayback(QObject):
    def __init__(self):
        super().__init__()
        self.played = []

    def play(self, path):
        self.played.append(str(path))
        return True

    def stop(self):
        pass


def _make_controller(tmp_path):
    engines, workers = [], []

    def engine_factory(**kwargs):
        e = FakeEngine(**kwargs)
        engines.append(e)
        return e

    def worker_factory(engine):
        w = FakeWorker(engine)
        workers.append(w)
        return w

    c = AppController(
        data_dir=tmp_path,
        engine_factory=engine_factory,
        worker_factory=worker_factory,
        catalog=lambda: [],
        saved_names=lambda voices_dir: [],
        bg_runner=run_sync,
        audio_probe=lambda: True,
    )
    c.attach_file_playback(FakePlayback())
    return c


@pytest.fixture()
def controller_without_artifact(qcoreapp, tmp_path):
    return _make_controller(tmp_path)


@pytest.fixture()
def controller_with_artifact(qcoreapp, tmp_path):
    c = _make_controller(tmp_path)
    wav = write_wav_file(_tone(), tmp_path / "art.wav")
    c._current_artifact = SynthesisArtifact(
        job_id="a" * 32,
        path=wav,
        sample_rate=48_000,
        samples=9600,
        duration_ms=200,
    )
    return c


@pytest.fixture()
def controller_with_studio(controller_with_artifact):
    assert controller_with_artifact.openInStudio("text", "first\n\nsecond") is True
    return controller_with_artifact


def test_open_in_studio_needs_artifact(controller_without_artifact):
    assert controller_without_artifact.openInStudio("text", "hello") is False


def test_open_in_studio_loads_two_clips(controller_with_artifact):
    c = controller_with_artifact
    assert c.openInStudio("text", "first\n\nsecond") is True
    assert c.hasStudioProject is True
    clips = list(c.studioClips)
    assert len(clips) == 2
    assert clips[0]["id"] == "c0" and clips[0]["label"] == "1"
    assert clips[0]["text"] == "first" and "duration" in clips[0]
    assert clips[1]["id"] == "c1" and clips[1]["label"] == "2"
    assert clips[1]["text"] == "second" and "duration" in clips[1]


def test_studio_undo_empty_is_false(controller_with_studio):
    assert controller_with_studio.studioUndo() is False


def test_studio_reset(controller_with_studio):
    c = controller_with_studio
    assert c.studioPushGain(3.0) is True
    assert c.studioPushSpeed(1.2) is True
    assert c.studioReset() is True
    # Second reset when already empty returns False
    assert c.studioReset() is False


def test_studio_ops_property(controller_with_studio):
    c = controller_with_studio
    assert c.studioOps == []
    assert c.studioPushGain(2.5) is True
    assert len(c.studioOps) == 1
    assert c.studioOps[0]["kind"] == "gain"
    assert "+2.5 dB" in c.studioOps[0]["desc"]
    assert c.studioControls["gain"] == 2.5

    assert c.studioPushSpeed(1.15) is True
    assert len(c.studioOps) == 2
    assert c.studioOps[1]["kind"] == "speed"
    assert c.studioControls["speed"] == 1.15

    assert c.studioUndo() is True
    assert len(c.studioOps) == 1
    assert c.studioControls == {
        "gain": 2.5,
        "speed": 1.0,
        "gap": 500,
        "fade": 200,
        "fadeIn": 0,
        "fadeOut": 0,
    }

    assert c.studioReset() is True
    assert c.studioOps == []
    assert c.studioControls == {
        "gain": 0.0,
        "speed": 1.0,
        "gap": 500,
        "fade": 200,
        "fadeIn": 0,
        "fadeOut": 0,
    }


def test_rack_apply_sets_instead_of_stacking(controller_with_studio):
    """A slider is an absolute setting: re-applying must not compound.

    Two +3 dB Applies used to render +6 dB while the rack kept showing
    "3.0 dB" — the label was the last op, the audio was the sum. The op count
    is the contract: one op per rack parameter, replaced in place.
    """
    c = controller_with_studio
    assert c.studioPushGain(3.0) is True
    assert len(c.studioOps) == 1
    assert c.studioPushGain(6.0) is True
    assert len(c.studioOps) == 1
    assert c.studioOps[0]["desc"].endswith("+6.0 dB")
    assert c.studioControls["gain"] == 6.0

    assert c.studioPushSpeed(1.15) is True
    assert c.studioPushSpeed(0.85) is True
    assert [op["kind"] for op in c.studioOps] == ["gain", "speed"]
    assert c.studioControls["speed"] == 0.85


def test_rack_controls_report_the_rendered_mix(controller_with_studio, tmp_path):
    """The displayed rack value equals what render_project produces."""
    from vienetts_app.core.studio import GainOp, StudioProject, render_project, set_parameter_op

    c = controller_with_studio
    project = c._studio_project
    project = set_parameter_op(project, GainOp(db=6.0))
    c._studio_project = project
    dry = float(np.max(np.abs(render_project(StudioProject(clips=project.clips, ops=())))))
    rendered = float(np.max(np.abs(render_project(project))))
    assert 20 * np.log10(rendered / dry) == pytest.approx(c.studioControls["gain"], abs=0.05)


def test_studio_edits_never_start_playback(controller_with_studio):
    """Apply, Undo and Reset are all silent; Nghe thử is the only audition.

    Undo/Reset used to auto-play while Apply did not, so the user heard the
    change they took back but never the one they made.
    """
    c = controller_with_studio
    assert c.studioPushGain(3.0) is True
    assert c.replayActive is False
    assert c.studioPushSpeed(1.2) is True
    assert c.replayActive is False
    assert c.studioUndo() is True
    assert c.replayActive is False
    assert c.studioReset() is False or c.replayActive is False  # reset of an empty stack is a no-op


def test_studio_revert_to_truncates_the_stack(controller_with_studio):
    c = controller_with_studio
    assert c.studioPushGain(3.0) is True
    assert c.studioPushSpeed(1.2) is True
    assert c.studioPushFade("in", 200) is True
    assert len(c.studioOps) == 3

    assert c.studioRevertTo(0) is True  # keep step 1 only
    assert [op["kind"] for op in c.studioOps] == ["gain"]
    assert c.studioRevertTo(-1) is True  # back to the original take
    assert c.studioOps == []
    assert c.studioRevertTo(0) is False  # nothing left to drop


def test_studio_delete_clip(controller_with_studio):
    c = controller_with_studio
    assert c.studioDeleteClip("c0") is True
    assert [clip["id"] for clip in c.studioClips] == ["c1"]
    assert c.studioDeleteClip("c1") is False  # the last clip cannot go
    assert c.studioDeleteClip("nope") is False


def test_studio_range_edits_use_milliseconds(controller_with_studio):
    """The waveform selection is in time; core works in 48 kHz frames."""
    c = controller_with_studio
    assert c.studioPushTrimRange(50, 150) is True
    assert c.studioOps[0]["kind"] == "trim"
    assert "0.05s" in c.studioOps[0]["desc"]
    assert c.studioDurationMs == pytest.approx(100, abs=2)

    assert c.studioPushCutRange(0, 50) is True
    assert c.studioOps[1]["kind"] == "cut"
    assert c.studioDurationMs == pytest.approx(50, abs=2)

    assert c.studioPushCutRange(10, -1) is True  # end_ms < 0 = to the end
    assert c.studioDurationMs == pytest.approx(10, abs=2)
    assert "cuối" in c.studioOps[2]["desc"]

    assert c.studioPushTrimRange(20, 10) is False  # inverted range refused


def test_studio_clip_audition_publishes_clip_audio(controller_with_studio):
    """A clip audition must describe itself: id + its own length and envelope.

    The dock used to sweep the whole-mix waveform under a clip-length timecode
    while a single segment played.
    """
    c = controller_with_studio
    assert c.studioClipPlayingId == ""
    assert c.studioClipEnvelope == []

    first_clip_ms = round(c.studioClips[0]["duration"] * 1000)
    assert c.studioPreviewClip("c0") is True
    assert c.studioClipPlayingId == "c0"
    assert c.studioClipDurationMs == pytest.approx(first_clip_ms, abs=1)
    assert len(c.studioClipEnvelope) == 160
    assert c.replayDurationMs == c.studioClipDurationMs

    c.stopReplay()
    assert c.studioClipPlayingId == ""
    assert c.studioClipEnvelope == []
    assert c.studioClipDurationMs == 0


def test_studio_master_preview_clears_the_clip_audition(controller_with_studio):
    c = controller_with_studio
    assert c.studioPreviewClip("c1") is True
    assert c.studioClipPlayingId == "c1"
    assert c.studioPreview() is True
    assert c.studioClipPlayingId == ""
    assert c.replayDurationMs == c.studioDurationMs


def test_studio_preview_clip(controller_with_studio):
    c = controller_with_studio
    assert c.studioPreviewClip("c0") is True
    assert c.studioPreviewClip("non_existent") is False


def test_studio_regen_clip_with_custom_text(controller_with_studio, tmp_path):
    c = controller_with_studio
    from vienetts_app.core.artifacts import SynthesisArtifact
    from vienetts_app.core.audio import write_wav_file

    assert c.studioRegenClip("c0", "voice1", "edited text for segment") is True
    assert c.studioRegenClipId == "c0"

    # Simulate completion of regen synthesis
    fake_wav = tmp_path / "regen_result.wav"
    write_wav_file(_tone(4800), fake_wav)
    artifact = SynthesisArtifact(
        path=fake_wav, job_id="job_regen", sample_rate=48000, samples=4800, duration_ms=100
    )
    c._maybe_splice_regen(artifact)

    assert c.studioRegenClipId == ""


def test_studio_regen_invalidates_old_preview_transport(controller_with_studio, tmp_path):
    c = controller_with_studio
    assert c.studioPreview() is True
    assert c.replayActive is True
    assert c.replayDurationMs == 200

    c._studio_regen_clip_id = "c0"
    c._studio_regen_clip_text = "replacement"
    fake_wav = tmp_path / "regen_short.wav"
    write_wav_file(_tone(2400), fake_wav)
    artifact = SynthesisArtifact(
        path=fake_wav, job_id="job_regen", sample_rate=48000, samples=2400, duration_ms=50
    )
    c._maybe_splice_regen(artifact)

    assert c.replayActive is False
    assert c.replayDurationMs == 0
