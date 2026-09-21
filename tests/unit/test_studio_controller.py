"""Controller studio wiring (Task 4)."""

from pathlib import Path

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal

from vienetts_app.core.artifacts import SynthesisArtifact
from vienetts_app.core.audio import write_wav_file
from vienetts_app.core.detector import HardwareInfo
from vienetts_app.core.engine_profiles import QWEN_CUSTOM
from vienetts_app.ui.bg_ops import run_sync
from vienetts_app.ui.controller import AppController
from vienetts_app.ui.stream_playback import StreamPlaybackController


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
        # Settable so a test can model a submission the worker refuses
        # (closing queue) — the controller must then leave nothing armed.
        self.accept = True

    def start(self):
        pass

    def submit(self, job):
        self.submitted.append(job)
        return self.accept

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


class FakeSink:
    """QAudioSink duck-type: enough for StreamPlaybackController to start.

    Keeps the streaming path off real QtMultimedia (offscreen hosts have no
    device), so ``generateStream`` never fails into an audio error banner.
    """

    def __init__(self):
        self.calls = []
        self.device = None
        self._state = "StoppedState"

    def start(self, device):
        self.calls.append("start")
        self.device = device
        self._state = "ActiveState"

    def stop(self):
        self.calls.append("stop")
        self._state = "StoppedState"

    def state(self):
        return self._state


def _make_controller(tmp_path, **app_kwargs):
    engines, workers = [], []

    def engine_factory(**kwargs):
        e = FakeEngine(**kwargs)
        engines.append(e)
        return e

    def worker_factory(engine):
        w = FakeWorker(engine)
        workers.append(w)
        return w

    sink = FakeSink()
    c = AppController(
        data_dir=tmp_path,
        engine_factory=engine_factory,
        worker_factory=worker_factory,
        catalog=lambda: [],
        saved_names=lambda voices_dir: [],
        bg_runner=run_sync,
        audio_probe=lambda: True,
        stream_playback_factory=lambda: StreamPlaybackController(sink_factory=lambda _fmt: sink),
        # Pinned machine: no nvidia-smi/torch probing from a unit test.
        hardware_probe=lambda: HardwareInfo(kind="none", torch_installed=False, cuda_version=None),
        **app_kwargs,
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


# ── engine provenance + matching-profile re-synthesis (Phase 5 Task 5.4) ─────


def _context(profile="vieneu", **overrides):
    """One engine identity for provenance tests (never a real model)."""
    from vienetts_app.core.synthesis_context import context_for

    kwargs = {"language": "vi" if profile == "vieneu" else "zh"}
    kwargs.update(overrides)
    return context_for(profile, **kwargs)


def _complete_foreground(c, tmp_path, samples=9600):
    """Finish the in-flight foreground job with a real artifact WAV."""
    from vienetts_app.core.jobs import JobTerminal

    worker = c._worker
    job = worker.submitted[-1]
    path = write_wav_file(_tone(samples), Path(job.artifact_path))
    worker.terminal.emit(
        JobTerminal(
            job_id=job.id,
            owner="text",
            state="completed",
            value=SynthesisArtifact(
                job_id=job.id,
                path=path,
                sample_rate=48_000,
                samples=samples,
                duration_ms=int(samples * 1000 / 48_000),
            ),
        )
    )
    return job


def test_clips_loaded_from_an_artifact_record_its_engine(controller_without_artifact, tmp_path):
    c = controller_without_artifact
    c.generateStream("first\n\nsecond", "voice1")
    _complete_foreground(c, tmp_path)
    context = c._current_artifact_context
    assert context is not None and context.profile == "vieneu"

    assert c.openInStudio("text", "first\n\nsecond") is True

    rows = list(c.studioClips)
    assert [row["profile"] for row in rows] == ["vieneu", "vieneu"]
    assert [row["profileLabel"] for row in rows] == ["VieNeu-TTS v3 Turbo"] * 2
    # VieNeu's unset language is "" (its SDK takes no language argument), so
    # the row reports the identity the job actually ran with.
    assert [row["language"] for row in rows] == ["", ""]
    assert [clip.context for clip in c._studio_project.clips] == [context, context]


def test_regen_refuses_a_clip_from_another_engine(controller_with_studio):
    c = controller_with_studio
    # Legacy audio (no provenance) + a Qwen profile active: the clip is
    # VieNeu's by definition, so the request is refused, never substituted.
    assert c.switchEngineProfile(QWEN_CUSTOM) is True
    before = [clip.text for clip in c._studio_project.clips]

    assert c.studioRegenClip("c0", "Vivian", "new text") is False

    assert c._worker is None  # no engine was built and no job submitted
    assert c.studioRegenClipId == ""
    assert [clip.text for clip in c._studio_project.clips] == before
    assert c._studio_regen_context is None
    assert c.studioRegenProfile == "vieneu"
    assert c.studioRegenProfileLabel == "VieNeu-TTS v3 Turbo"
    assert "VieNeu-TTS v3 Turbo" in c.errorText


def test_regen_refuses_a_qwen_clip_under_vieneu(controller_with_studio):
    from dataclasses import replace

    c = controller_with_studio
    qwen = _context(QWEN_CUSTOM, voice_id="Vivian")
    c._studio_project = replace(
        c._studio_project,
        clips=tuple(replace(clip, context=qwen) for clip in c._studio_project.clips),
    )

    assert c.studioRegenClip("c0", "voice1") is False
    assert c.studioRegenProfile == QWEN_CUSTOM
    assert c.studioRegenProfileLabel == "Qwen3-TTS CustomVoice 0.6B"
    assert c.studioRegenClipId == ""


def test_the_switch_action_moves_to_the_required_profile(controller_with_studio):
    c = controller_with_studio
    assert c.switchEngineProfile(QWEN_CUSTOM) is True
    assert c.studioRegenClip("c0", "Vivian") is False
    assert c.studioSwitchToRegenProfile() is True

    assert c.engineProfile == "vieneu"
    assert c.studioRegenProfile == ""  # the offer is consumed by the switch
    # The same clip now re-synthesizes: the engine matches its audio again.
    assert c.studioRegenClip("c0", "voice1", "new text") is True
    assert c.studioRegenClipId == "c0"


def test_the_switch_action_is_false_when_nothing_is_pending(controller_with_studio):
    c = controller_with_studio
    assert c.studioSwitchToRegenProfile() is False
    assert c.engineProfile == "vieneu"


def test_a_legacy_clip_re_synthesized_by_vieneu_gains_provenance(controller_with_studio, tmp_path):
    c = controller_with_studio
    assert c._studio_project.clips[0].context is None

    assert c.studioRegenClip("c0", "voice1", "new text") is True
    regen_context = c._studio_regen_context
    assert regen_context is not None and regen_context.profile == "vieneu"
    _complete_foreground(c, tmp_path, samples=4800)

    assert c.studioRegenClipId == ""
    clips = c._studio_project.clips
    assert clips[0].text == "new text"
    assert clips[0].context == regen_context  # provenance follows the new audio
    assert clips[1].context is None  # the untouched clip keeps its own
    assert [row["profile"] for row in c.studioClips] == ["vieneu", ""]


def test_a_regen_that_is_not_admitted_leaves_nothing_armed(controller_without_artifact, tmp_path):
    c = controller_without_artifact
    c.generateStream("warmup", "voice1")
    _complete_foreground(c, tmp_path)
    assert c.openInStudio("text", "first\n\nsecond") is True
    c._worker.accept = False  # the worker refuses the submission

    assert c.studioRegenClip("c0", "voice1", "new text") is False

    assert c.studioRegenClipId == ""
    assert c._studio_regen_context is None
    assert [clip.text for clip in c._studio_project.clips] == ["first", "second"]


def test_a_cancelled_regen_keeps_the_clip_and_disarms(controller_with_studio):
    from vienetts_app.core.jobs import JobTerminal

    c = controller_with_studio
    before = [clip.audio.copy() for clip in c._studio_project.clips]

    assert c.studioRegenClip("c0", "voice1", "new text") is True
    job = c._worker.submitted[-1]
    c._worker.terminal.emit(JobTerminal(job_id=job.id, owner="text", state="cancelled"))

    assert c.studioRegenClipId == ""
    assert c._studio_regen_context is None
    clips = c._studio_project.clips
    assert clips[0].text == "first"  # the edit was never committed
    assert all(np.array_equal(clip.audio, old) for clip, old in zip(clips, before, strict=True))


def test_editing_and_export_stay_engine_independent(controller_with_studio, tmp_path):
    c = controller_with_studio
    # Clips produced by VieNeu, a Qwen profile active: edits and export must
    # not care — only re-synthesis does.
    assert c.switchEngineProfile(QWEN_CUSTOM) is True

    assert c.studioPushGain(3.0) is True
    assert c.studioControls["gain"] == 3.0

    target = tmp_path / "mix.wav"
    assert c.studioExport(str(target)) is True
    assert target.is_file()

    # No engine was ever built: editing/export never needed one.
    assert c._worker is None
