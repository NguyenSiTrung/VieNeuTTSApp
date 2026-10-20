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

    def stop(self, timeout_ms: int = 5000):
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


def _make_controller(tmp_path, *, bg_runner=run_sync, **app_kwargs):
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
        bg_runner=bg_runner,
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


def test_open_in_studio(controller_without_artifact, tmp_path):
    c = controller_without_artifact
    assert c.openInStudio("text", "hello") is False  # nothing to open without an artifact

    wav = write_wav_file(_tone(), tmp_path / "art.wav")
    c._current_artifact = SynthesisArtifact(
        job_id="a" * 32,
        path=wav,
        sample_rate=48_000,
        samples=9600,
        duration_ms=200,
    )
    assert c.openInStudio("text", "first\n\nsecond") is True
    assert c.hasStudioProject is True
    clips = list(c.studioClips)
    assert len(clips) == 2
    assert clips[0]["id"] == "c0" and clips[0]["label"] == "1"
    assert clips[0]["text"] == "first" and "duration" in clips[0]
    assert clips[1]["id"] == "c1" and clips[1]["label"] == "2"
    assert clips[1]["text"] == "second" and "duration" in clips[1]


def test_studio_op_stack_controls_and_undo(controller_with_studio, tmp_path):
    """Op-stack semantics: absolute rack values, recorded undo steps, and the
    Undo button's can/name surface."""
    c = controller_with_studio
    # An empty stack: nothing to undo or reset.
    assert c.studioUndo() is False
    assert c.studioReset() is False
    assert c.studioCanUndo is False
    assert c.studioUndoName == ""
    assert c.studioOps == []

    # A slider is an absolute setting: re-applying must not compound. Two
    # +3 dB Applies used to render +6 dB while the rack kept showing "3.0 dB"
    # — the label was the last op, the audio was the sum.
    assert c.studioPushGain(3.0) is True
    assert len(c.studioOps) == 1
    assert c.studioPushGain(6.0) is True
    assert len(c.studioOps) == 1
    assert c.studioOps[0]["kind"] == "gain"
    assert c.studioOps[0]["desc"].endswith("+6.0 dB")
    assert c.studioControls["gain"] == 6.0
    assert c.studioCanUndo is True
    assert c.studioUndoName == c.studioOps[0]["name"]

    assert c.studioPushSpeed(1.15) is True
    assert c.studioPushSpeed(0.85) is True
    assert [op["kind"] for op in c.studioOps] == ["gain", "speed"]
    assert c.studioControls["speed"] == 0.85

    # Undo is a recorded step: a replaced value restores its previous setting
    # (undoing a replaced value used to remove the op altogether).
    assert c.studioUndo() is True
    assert c.studioControls["speed"] == 1.15
    assert [op["kind"] for op in c.studioOps] == ["gain", "speed"]
    assert c.studioUndo() is True
    assert [op["kind"] for op in c.studioOps] == ["gain"]
    assert c.studioControls == {
        "gain": 6.0,
        "speed": 1.0,
        "gap": 500,
        "fade": 200,
        "fadeIn": 0,
        "fadeOut": 0,
    }
    assert c.studioUndo() is True
    assert c.studioControls["gain"] == 3.0

    # A revert is a step too: undoing it restores the trimmed stack.
    assert c.studioRevertTo(-1) is True
    assert c.studioOps == []
    assert c.studioCanUndo is True
    assert c.studioUndo() is True
    assert c.studioControls["gain"] == 3.0
    while c.studioUndo():
        pass
    assert c.studioOps == []
    assert c.studioUndo() is False

    # One undo step covering several ops has no single name to report.
    assert c.studioPushGain(3.0) is True
    assert c.studioStageSpeed(1.2) is True
    assert c.studioStageNormalize() is True
    assert c.studioApplyPending() is True
    assert c.studioUndoName == ""

    assert c.studioRevertTo(-1) is True
    assert c.studioOps == []
    assert c.studioUndo() is True
    assert len(c.studioOps) == 3

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
    assert c.studioCanUndo is True
    while c.studioUndo():
        pass
    assert c.studioCanUndo is False
    assert c.studioUndoName == ""

    # Apply/Undo/Reset never start playback — Nghe thử is the only audition.
    # (Undo/Reset used to auto-play while Apply did not, so the user heard the
    # change they took back but never the one they made.)
    assert c.studioPushGain(3.0) is True
    assert c.replayActive is False
    assert c.studioPushSpeed(1.2) is True
    assert c.replayActive is False
    assert c.studioUndo() is True
    assert c.replayActive is False

    # Without a project the same empty surfaces hold.
    c2 = _make_controller(tmp_path / "case-b")
    assert c2.studioCanUndo is False
    assert c2.studioUndoName == ""


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


def test_studio_revert_and_reset_are_one_undo_step_each(controller_with_studio):
    c = controller_with_studio
    assert c.studioPushGain(3.0) is True
    assert c.studioPushSpeed(1.2) is True
    assert c.studioPushFade("in", 200) is True
    full = [op["kind"] for op in c.studioOps]
    assert full == ["gain", "speed", "fade"]

    # A chip revert truncates the stack; out-of-range reverts are refused.
    assert c.studioRevertTo(0) is True  # keep step 1 only
    assert [op["kind"] for op in c.studioOps] == ["gain"]
    assert c.studioRevertTo(-1) is True  # back to the original take
    assert c.studioOps == []
    assert c.studioRevertTo(0) is False  # nothing left to drop

    # The truncation itself is one recorded step: Undo restores the
    # pre-revert stack, then the earlier steps walk back one at a time.
    assert c.studioPushGain(3.0) is True
    assert c.studioPushSpeed(1.2) is True
    assert c.studioPushFade("in", 200) is True
    assert c.studioRevertTo(0) is True
    assert [op["kind"] for op in c.studioOps] == ["gain"]
    assert c.studioUndo() is True
    assert [op["kind"] for op in c.studioOps] == full
    assert c.studioUndo() is True
    assert [op["kind"] for op in c.studioOps] == ["gain", "speed"]

    # Reset (back to the original take) is undoable the same way.
    assert c.studioReset() is True
    assert c.studioOps == []
    assert c.studioUndo() is True
    assert [op["kind"] for op in c.studioOps] == ["gain", "speed"]
    while c.studioUndo():
        pass
    assert c.studioOps == []
    assert c.studioUndo() is False


def test_studio_delete_clip_and_range_edits(controller_with_studio):
    """Timeline clip ops: deletes refuse below one clip; range edits take ms
    (the waveform selection is in time; the core works in 48 kHz frames)."""
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

    assert c.studioDeleteClip("c0") is True
    assert [clip["id"] for clip in c.studioClips] == ["c1"]
    assert c.studioDeleteClip("c1") is False  # the last clip cannot go
    assert c.studioDeleteClip("nope") is False


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

    # A clip id that does not exist is refused.
    assert c.studioPreviewClip("non_existent") is False

    # A master-mix preview clears the clip audition.
    assert c.studioPreviewClip("c1") is True
    assert c.studioClipPlayingId == "c1"
    assert c.studioPreview() is True
    assert c.studioClipPlayingId == ""
    assert c.replayDurationMs == c.studioDurationMs


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


def test_regen_refusals_name_the_required_engine(controller_with_studio, tmp_path):
    c = controller_with_studio
    # Nothing pending: the switch action is a no-op.
    assert c.studioSwitchToRegenProfile() is False
    assert c.engineProfile == "vieneu"

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

    # The switch action moves to the required profile and consumes the offer.
    assert c.studioSwitchToRegenProfile() is True
    assert c.engineProfile == "vieneu"
    assert c.studioRegenProfile == ""
    # The same clip now re-synthesizes: the engine matches its audio again.
    assert c.studioRegenClip("c0", "voice1", "new text") is True
    assert c.studioRegenClipId == "c0"

    # A Qwen-stamped clip under VieNeu is refused, and the offer names the
    # recorded variant — an unstamped official Qwen render spells that out.
    c2 = _make_controller(tmp_path / "case-b")
    wav = write_wav_file(_tone(), tmp_path / "case-b" / "art.wav")
    c2._current_artifact = SynthesisArtifact(
        job_id="a" * 32, path=wav, sample_rate=48_000, samples=9600, duration_ms=200
    )
    assert c2.openInStudio("text", "first\n\nsecond") is True
    _with_clip_contexts(c2, _context(QWEN_CUSTOM, voice_id="Vivian"))

    assert c2.studioRegenClip("c0", "voice1") is False
    assert c2.studioRegenProfile == QWEN_CUSTOM
    assert c2.studioRegenProfileLabel == (
        "Qwen3-TTS CustomVoice 0.6B · Trọng lượng đầy đủ (PyTorch)"
    )
    assert c2.studioRegenClipId == ""


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


def test_a_regen_that_is_not_admitted_or_is_cancelled_disarms(
    controller_without_artifact, tmp_path
):
    from vienetts_app.core.jobs import JobTerminal

    c = controller_without_artifact
    c.generateStream("warmup", "voice1")
    _complete_foreground(c, tmp_path)
    assert c.openInStudio("text", "first\n\nsecond") is True
    c._worker.accept = False  # the worker refuses the submission

    assert c.studioRegenClip("c0", "voice1", "new text") is False

    assert c.studioRegenClipId == ""
    assert c._studio_regen_context is None
    assert [clip.text for clip in c._studio_project.clips] == ["first", "second"]

    # A cancelled regen keeps the clip and disarms the armed edit.
    c._worker.accept = True
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


# ── variant-level provenance (Task 5.3, track qwen_gguf_engine_20260923) ─────
#
# ``same_engine`` already compares (profile, format, quantization, engine), so
# a same-profile GGUF/official mismatch is refused — these tests pin the OFFER
# side: the banner names the recorded variant, the switch restores
# profile+format+quantization, and a selection the user lands on manually
# disarms a satisfied offer.


def _qwen_context(model_format="official", quantization="", **overrides):
    """A stamped Qwen CustomVoice context for clip provenance tests."""
    from vienetts_app.core import qwen_variants

    variant = qwen_variants.variant_for(QWEN_CUSTOM, model_format, quantization)
    overrides.setdefault("voice_id", "Vivian")
    return _context(QWEN_CUSTOM, variant=variant, **overrides)


def _with_clip_contexts(c, context):
    """Stamp every clip in the open project with ``context``."""
    from dataclasses import replace

    c._studio_project = replace(
        c._studio_project,
        clips=tuple(replace(clip, context=context) for clip in c._studio_project.clips),
    )


def test_clip_rows_expose_the_variant_identity(controller_with_studio):
    c = controller_with_studio
    _with_clip_contexts(c, _qwen_context("gguf", "Q4_K_M"))
    rows = list(c.studioClips)
    assert [row["modelFormat"] for row in rows] == ["gguf", "gguf"]
    assert [row["quantization"] for row in rows] == ["Q4_K_M", "Q4_K_M"]
    assert [row["engine"] for row in rows] == ["qwentts_cpp", "qwentts_cpp"]
    assert [row["variantLabel"] for row in rows] == ["GGUF Q4_K_M · qwentts.cpp"] * 2

    _with_clip_contexts(c, _qwen_context("official"))
    rows = list(c.studioClips)
    assert [row["modelFormat"] for row in rows] == ["official", "official"]
    assert rows[0]["variantLabel"].startswith("Trọng lượng đầy đủ")
    assert "PyTorch" in rows[0]["variantLabel"]


def test_switch_action_restores_the_recorded_variant(controller_with_studio):
    """A same-profile format mismatch names the VARIANT it needs, and the
    switch restores it — a GGUF quantization and an official-format record."""
    c = controller_with_studio
    _with_clip_contexts(c, _qwen_context("gguf", "Q8_0"))
    # The clip's format is not the active one: select the official weights so
    # the mismatch is real (the app default is GGUF Q8_0, the clip's variant).
    assert c.setQwenVariant("official", "") is True
    assert c.switchEngineProfile(QWEN_CUSTOM) is True  # official variant active

    assert c.studioRegenClip("c0", "Vivian") is False

    # Same profile — the offer must name the VARIANT it needs, not just the
    # profile the user is already on.
    assert c.studioRegenProfile == QWEN_CUSTOM
    assert "GGUF Q8_0" in c.studioRegenProfileLabel
    assert "qwentts.cpp" in c.studioRegenProfileLabel
    assert c.studioRegenClipId == ""
    assert c._worker is None

    # The switch restores exactly the recorded GGUF quantization.
    assert c.studioSwitchToRegenProfile() is True
    assert c.engineProfile == QWEN_CUSTOM
    assert c._settings.qwen_model_format == "gguf"
    assert c._settings.qwen_gguf_quantization == "Q8_0"
    assert c.studioRegenProfile == ""  # the offer is consumed by the switch

    # Official-format record: switch restores `official`, not a GGUF quant.
    _with_clip_contexts(c, _qwen_context("official"))
    assert c.setQwenVariant("gguf", "Q8_0") is True  # clip needs official

    assert c.studioRegenClip("c0", "Vivian") is False
    assert c.studioRegenProfile == QWEN_CUSTOM
    assert "GGUF" not in c.studioRegenProfileLabel

    assert c.studioSwitchToRegenProfile() is True
    assert c._settings.qwen_model_format == "official"
    assert c.studioRegenProfile == ""


def test_regen_offer_arms_on_mismatch_and_disarms_on_manual_match(
    controller_with_studio,
):
    """One state machine: mismatch arms the exact variant; landing on the
    recorded selection disarms; a still-mismatched selection keeps it armed."""
    c = controller_with_studio
    _with_clip_contexts(c, _qwen_context("gguf", "Q8_0"))
    assert c.switchEngineProfile(QWEN_CUSTOM) is True
    assert c.setQwenVariant("gguf", "Q4_K_M") is True  # wrong quant for this clip

    assert c.studioRegenClip("c0", "Vivian") is False
    assert "Q8_0" in c.studioRegenProfileLabel  # armed for the exact variant

    assert c.studioSwitchToRegenProfile() is True
    assert c._settings.qwen_gguf_quantization == "Q8_0"
    assert c._settings.qwen_model_format == "gguf"
    assert c.studioRegenProfile == ""

    # Manual match disarms the now-stale offer.
    _with_clip_contexts(c, _qwen_context("gguf", "Q8_0"))
    assert c.setQwenVariant("official", "") is True
    assert c.switchEngineProfile(QWEN_CUSTOM) is True
    assert c.studioRegenClip("c0", "Vivian") is False
    assert c.studioRegenProfile == QWEN_CUSTOM  # the offer is armed

    # The user lands on the needed selection themselves — the stale "switch
    # to …" banner must not keep offering an action that is now a no-op.
    assert c.setQwenVariant("gguf", "Q8_0") is True
    assert c.studioRegenProfile == ""
    assert c.studioRegenProfileLabel == ""
    # The retry now fails for the honest reason — the GGUF install this
    # harness does not fake — never with a profile-mismatch offer.
    assert c.studioRegenClip("c0", "Vivian") is False
    assert c.studioRegenProfile == ""
    assert "cài đặt" in c.errorText or "install" in c.errorText.lower()

    # A DIFFERENT wrong variant leaves the offer armed — it still names the
    # selection the clip actually needs.
    _with_clip_contexts(c, _qwen_context("gguf", "Q8_0"))
    assert c.setQwenVariant("official", "") is True
    assert c.switchEngineProfile(QWEN_CUSTOM) is True
    assert c.studioRegenClip("c0", "Vivian") is False
    assert c.setQwenVariant("gguf", "Q4_K_M") is True
    assert c.studioRegenProfile == QWEN_CUSTOM
    assert "Q8_0" in c.studioRegenProfileLabel


class DeferredRunner:
    """bg_runner double: queues work so a test decides when (and if) it runs."""

    def __init__(self):
        self.jobs = []

    def __call__(self, work, on_done, _parent, *, on_error=None):
        self.jobs.append((work, on_done, on_error))

    def run(self, index=0):
        work, on_done, on_error = self.jobs.pop(index)
        try:
            result = work()
        except Exception as exc:  # noqa: BLE001 - mirrors the pool bridge
            on_error(exc)
            return
        on_done(result)

    def drain(self):
        while self.jobs:
            self.run()


def _stub_chapter_book(monkeypatch, tmp_path, *, missing=False, error=None):
    """A one-chapter book whose load is counted (no real EPUB pipeline)."""
    from types import SimpleNamespace

    from vienetts_app.core import audiobook

    loads = []
    wav = write_wav_file(_tone(4800), tmp_path / "chapter0.wav")

    def load_book(self, book_id):
        loads.append(book_id)
        if error is not None:
            raise error
        chapters = [] if missing else [SimpleNamespace(index=0, text="Chương một")]
        return SimpleNamespace(chapters=chapters, contexts={})

    monkeypatch.setattr(audiobook.AudiobookLibrary, "load_book", load_book)
    monkeypatch.setattr(audiobook.AudiobookLibrary, "chapter_wav_path", lambda self, b, i: wav)
    return loads


class TestOffThreadStudioIO:
    """Chapter loads and clip-preview writes never run inside the slot."""

    def test_open_chapter_loads_the_book_off_the_gui_thread(self, qcoreapp, tmp_path, monkeypatch):
        loads = _stub_chapter_book(monkeypatch, tmp_path)
        runner = DeferredRunner()
        c = _make_controller(tmp_path, bg_runner=runner)

        assert c.openChapterInStudio("book", 0) is True
        assert loads == []  # nothing read inside the slot
        assert c.studioClips == []

        runner.run()
        assert loads == ["book"]
        assert [clip["id"] for clip in c.studioClips] == ["ch0"]

        # A newer open supersedes the in-flight chapter load.
        wav = write_wav_file(_tone(), tmp_path / "art.wav")
        c._current_artifact = SynthesisArtifact(
            job_id="a" * 32, path=wav, sample_rate=48_000, samples=9600, duration_ms=200
        )
        assert c.openChapterInStudio("book", 0) is True
        assert c.openInStudio("text", "first\n\nsecond") is True
        runner.drain()
        assert [clip["id"] for clip in c.studioClips] == ["c0", "c1"]

    def test_chapter_load_failures_surface_after_the_background_read(
        self, qcoreapp, tmp_path, monkeypatch
    ):
        from vienetts_app.core.audiobook import AudiobookError

        for i, (missing, error, message) in enumerate(
            [
                (True, None, "Không tìm thấy chương này trong sách."),
                (False, AudiobookError("sách hỏng"), "sách hỏng"),
            ]
        ):
            case = tmp_path / f"case-{i}"
            _stub_chapter_book(monkeypatch, case, missing=missing, error=error)
            runner = DeferredRunner()
            c = _make_controller(case, bg_runner=runner)

            assert c.openChapterInStudio("book", 0) is True
            assert c.errorText == ""
            runner.run()
            assert c.errorText == message
            assert c.studioClips == []

    def test_clip_preview_writes_the_wav_off_the_gui_thread(self, controller_with_studio, tmp_path):
        c = controller_with_studio
        runner = DeferredRunner()
        c._run_bg = runner
        preview = tmp_path / "studio_clip_c0.wav"

        assert c.studioPreviewClip("c0") is True
        assert not preview.exists()
        assert c.studioClipPlayingId == ""
        assert c._file_playback.played == []

        runner.run()
        assert preview.is_file()
        assert c.studioClipPlayingId == "c0"
        assert c._file_playback.played == [str(preview)]

        # A newer clip preview drops the older write.
        assert c.studioPreviewClip("c0") is True
        assert c.studioPreviewClip("c1") is True
        runner.drain()
        assert c._file_playback.played[-1] == str(tmp_path / "studio_clip_c1.wav")
        assert c.studioClipPlayingId == "c1"

        # An edit that lands while the clip preview writes drops it.
        assert c.studioPreviewClip("c0") is True
        assert c.studioDeleteClip("c1") is True
        runner.drain()
        assert len(c._file_playback.played) == 2  # nothing new played
        assert c.studioClipPlayingId == ""


def test_studio_row_models_update_in_place(controller_with_studio):
    """Row-level Studio clip/op models (perf track 6.4): pushing an op leaves
    the clip rows untouched and appends one op row — no delegate rebuilds."""
    from vienetts_app.ui.list_models import DictListModel

    c = controller_with_studio
    clips, ops = c.studioClipModel, c.studioOpModel
    assert isinstance(clips, DictListModel) and isinstance(ops, DictListModel)
    assert clips.rows() == list(c.studioClips)
    assert ops.rows() == []

    events: list[tuple] = []
    for name, model in (("clips", clips), ("ops", ops)):
        model.dataChanged.connect(
            lambda tl, _b, _r, n=name: events.append((n, "changed", tl.row()))
        )
        model.rowsInserted.connect(lambda _p, a, _b, n=name: events.append((n, "inserted", a)))
        model.rowsRemoved.connect(lambda _p, a, _b, n=name: events.append((n, "removed", a)))
        model.modelReset.connect(lambda n=name: events.append((n, "reset")))

    assert c.studioPushGain(2.5) is True
    assert events == [("ops", "inserted", 0)]
    assert ops.rows() == list(c.studioOps)
    assert c.studioOpCount == 1
    assert c.studioLastOpName == c.studioOps[0]["name"]

    events.clear()
    assert c.studioPushSpeed(1.15) is True
    assert c.studioUndo() is True
    assert events == [("ops", "inserted", 1), ("ops", "removed", 1)]
    assert c.studioClipCount == 2
    assert c.studioOpCount == 1

    events.clear()
    assert c.studioReset() is True
    assert events == [("ops", "removed", 0)]
    assert c.studioOpCount == 0
    assert c.studioLastOpName == ""


def _peak(path):
    from vienetts_app.core.audio import read_wav

    audio, _sr = read_wav(path)
    return float(np.max(np.abs(audio)))


class TestPendingEffects:
    """Task 4.3 seam: stage → preview / A-B → one Apply, one Undo."""

    def test_staging_previews_without_touching_the_op_stack(self, controller_with_studio, tmp_path):
        c = controller_with_studio
        changes = []
        c.studioPendingChanged.connect(lambda: changes.append("pending"))
        project = c._studio_project
        assert (c.studioPendingCount, c.studioPendingOps, c.studioCompareMode) == (0, [], "pending")

        assert c.studioStageGain(6.0) is True
        assert c.studioStageNormalize() is True
        assert c.studioStageFade("in", 20) is True
        assert c.studioStageGain(-3.0) is True  # replaces the gain row in place
        assert c.studioStageNormalize() is True  # staged once
        assert changes
        rows = c.studioPendingOps
        assert [r["key"] for r in rows] == ["gain", "normalize", "fade_in"]
        assert [r["kind"] for r in rows] == ["gain", "normalize", "fade"]
        assert [r["index"] for r in rows] == [0, 1, 2]
        assert "-3.0 dB" in rows[0]["desc"] and rows[0]["name"]
        assert c.studioPendingCount == 3
        assert c.studioPendingControls["gain"] == -3.0
        assert c.studioPendingControls["fadeIn"] == 20
        assert c.studioControls["gain"] == 0.0  # the applied rack is unchanged
        assert c.studioStageGain(99.0) is False  # out of range: refused, not staged
        assert c.studioPendingCount == 3

        # Nghe thử in "pending" mode renders the staged edits — and only that.
        assert c.studioPreview() is True
        assert c.replayActive is True and c.studioPlayingMode == "pending"
        assert c._file_playback.played[-1] == str(tmp_path / "studio_preview_pending.wav")
        assert _peak(tmp_path / "studio_preview_pending.wav") == pytest.approx(1.0, abs=1e-3)
        assert c.studioPendingDurationMs == pytest.approx(200, abs=2)
        assert len(c.studioPendingEnvelope) == 160
        assert c._studio_project is project and c.studioOps == []
        assert c.studioPendingCount == 3

        # Changing the staged set makes the sounding pending render stale.
        assert c.studioUnstage("normalize") is True
        assert c.replayActive is False and c.studioPlayingMode == ""
        assert c.studioPendingEnvelope == [] and c.studioPendingDurationMs == 0
        assert c.studioUnstage("normalize") is False  # nothing under that key
        assert [r["key"] for r in c.studioPendingOps] == ["gain", "fade_in"]
        assert c.studioClearPending() is True
        assert c.studioPendingCount == 0 and c.studioClearPending() is False

        # Opening a project drops whatever was staged.
        assert c.studioStageGain(6.0) is True
        assert c.studioSetCompareMode("base") is True
        assert c.openInStudio("text", "again") is True
        assert c.studioPendingCount == 0 and c.studioCompareMode == "pending"

    def test_apply_pushes_the_pending_ops_as_one_undo_step(self, controller_with_studio):
        c = controller_with_studio
        assert c.studioPushGain(3.0) is True
        assert c.studioStageGain(6.0) is True
        assert c.studioStageSpeed(1.2) is True
        assert c.studioStageGap(100) is True
        assert c.studioStageSilenceTrim() is True
        assert c.studioPendingCount == 4

        assert c.studioApplyPending() is True
        assert [op["kind"] for op in c.studioOps] == ["gain", "speed", "gap", "silence"]
        assert c.studioControls["gain"] == 6.0
        assert c.studioPendingCount == 0 and c.studioPendingOps == []
        assert c.studioApplyPending() is False  # nothing staged

        assert c.studioUndo() is True  # ONE undo reverts all four
        assert [op["kind"] for op in c.studioOps] == ["gain"]
        assert c.studioControls["gain"] == 3.0
        assert c.studioUndo() is True
        assert c.studioOps == [] and c.studioUndo() is False

        # Staging the value the stack already has leaves nothing to apply.
        assert c.studioPushGain(3.0) is True
        assert c.studioStageGain(3.0) is True
        assert c.studioPendingCount == 0

    def test_ab_toggle_switches_playback_between_base_and_pending(
        self, controller_with_studio, tmp_path
    ):
        c = controller_with_studio
        played = c._file_playback.played
        base_wav = str(tmp_path / "studio_preview.wav")
        pending_wav = str(tmp_path / "studio_preview_pending.wav")
        assert c.studioStageGain(-6.0) is True
        assert c.studioSetCompareMode("nope") is False
        assert c.studioCompareMode == "pending"

        # Toggling while nothing plays only flips the mode.
        assert c.studioSetCompareMode("base") is True
        assert c.studioCompareMode == "base" and c.replayActive is False and played == []
        assert c.studioPreview() is True
        assert (played[-1], c.studioPlayingMode) == (base_wav, "base")
        base_peak = _peak(base_wav)

        # While the master preview sounds, the toggle swaps what is playing.
        assert c.studioSetCompareMode("pending") is True
        assert (played[-1], c.studioPlayingMode) == (pending_wav, "pending")
        assert c.replayActive is True
        assert _peak(pending_wav) == pytest.approx(base_peak * 10 ** (-6 / 20), rel=1e-3)
        assert c.studioSetCompareMode("base") is True
        assert (played[-1], c.studioPlayingMode) == (base_wav, "base")
        assert c.studioSetCompareMode("base") is True and len(played) == 3  # same mode: no-op

        # A clip audition is not the master mix: the toggle leaves it alone.
        assert c.studioPreviewClip("c0") is True
        assert c.studioPlayingMode == ""
        count = len(played)
        assert c.studioSetCompareMode("pending") is True
        assert len(played) == count and c.studioClipPlayingId == "c0"

        c.stopReplay()
        assert c.studioPlayingMode == ""


def test_undo_steps_drive_the_can_undo_and_name_surfaces(controller_with_studio, tmp_path):
    """The Undo button follows what ``studioUndo`` can do, not the op count:
    a replaced value restores its previous setting, a revert or reset leaves
    the stack empty yet undoable, and a multi-op step has no single name."""
    c = controller_with_studio
    assert c.studioCanUndo is False
    assert c.studioUndoName == ""

    # Undoing a replaced value returns to the previous setting (the stack
    # used to just pop, removing the gain altogether).
    assert c.studioPushGain(3.0) is True
    assert c.studioCanUndo is True
    assert c.studioUndoName == c.studioOps[0]["name"]
    assert c.studioPushGain(6.0) is True  # replaced in place: still that op
    assert c.studioUndoName == c.studioOps[0]["name"]
    assert c.studioUndo() is True
    assert c.studioControls["gain"] == 3.0

    # A revert is a step too: the stack is empty yet still undoable.
    assert c.studioRevertTo(-1) is True
    assert c.studioOps == []
    assert c.studioCanUndo is True
    assert c.studioUndo() is True
    assert c.studioControls["gain"] == 3.0
    while c.studioUndo():
        pass
    assert c.studioOps == []

    # One step covering several staged ops has no single name to report.
    assert c.studioPushGain(3.0) is True
    assert c.studioStageSpeed(1.2) is True
    assert c.studioStageNormalize() is True
    assert c.studioApplyPending() is True
    assert c.studioUndoName == ""

    assert c.studioRevertTo(-1) is True
    assert c.studioOps == []
    assert c.studioCanUndo is True
    assert c.studioUndoName == ""
    assert c.studioUndo() is True
    assert len(c.studioOps) == 3

    assert c.studioReset() is True
    assert c.studioCanUndo is True
    while c.studioUndo():
        pass
    assert c.studioCanUndo is False
    assert c.studioUndoName == ""

    # Without a project the same empty surfaces hold.
    c2 = _make_controller(tmp_path / "case-b")
    assert c2.studioCanUndo is False
    assert c2.studioUndoName == ""


class TestSubtitleStudioController:
    """Task 3: openSubtitleInStudio, windowed navigation, per-cue speed, and save-back."""

    def _make_rendered_subtitle_store(self, tmp_path: Path, count: int = 60):
        from vienetts_app.core.align import DEFAULT_SAMPLE_RATE, FitPolicy
        from vienetts_app.core.subtitle_project import (
            SubtitleProjectStore,
            SubtitleTrackRenderer,
            build_project,
        )
        from vienetts_app.core.subtitles import Cue
        from vienetts_app.core.synthesis_context import SynthesisContext

        store = SubtitleProjectStore(tmp_path / "subtitles")
        cues = tuple(
            Cue(
                index=i + 1,
                start_ms=i * 1000 + 100,
                end_ms=i * 1000 + 600,
                text=f"Câu {i + 1}",
            )
            for i in range(count)
        )
        ctx = SynthesisContext(profile="vieneu", model_revision="r1", voice_id="Binh")
        proj = build_project(
            tmp_path / "movie.srt",
            cues,
            FitPolicy.dub(rate_cap=1.5),
            voice_key="Binh",
            context=ctx,
        )
        store.save(proj)
        with SubtitleTrackRenderer(store, proj) as renderer:
            for i in range(count):
                renderer.add_clip(i, np.full(int(0.4 * DEFAULT_SAMPLE_RATE), 0.2, dtype=np.float32))
            renderer.finish()
        return store, proj

    def test_open_window_navigate_speed_and_save_back(self, tmp_path: Path):
        from vienetts_app.ui.subtitle_controller import SubtitleController

        c = _make_controller(tmp_path)
        sub_ctrl = SubtitleController(
            c,
            data_dir=tmp_path,
            bg_runner=run_sync,
        )
        c.attach_subtitle_controller(sub_ctrl)
        store, proj = self._make_rendered_subtitle_store(tmp_path, count=60)
        sub_ctrl._project = store.require(proj.id)
        sub_ctrl._load_reader()

        assert sub_ctrl.projectId == proj.id
        assert c.studioIsSubtitle is False

        # Open cue 52 -> auto-selects window 50..60.
        assert c.openSubtitleInStudio(proj.id, 52) is True
        assert c.hasStudioProject is True
        assert c.studioIsSubtitle is True
        assert c.studioSubtitleProjectId == proj.id
        assert c.studioSubtitleTotalCues == 60
        assert c.studioSubtitleWindowStart == 50
        assert c.studioSubtitleWindowEnd == 60
        assert len(c.studioSubtitleWindows) == 2
        assert c.studioClipCount == 10
        assert c.studioClips[0]["isSrtCue"] is True
        assert c.studioClips[0]["cueNumber"] == 51

        # Switch to window 0..50.
        assert c.openSubtitleInStudio(proj.id, 0) is True
        assert c.studioSubtitleWindowStart == 0
        assert c.studioSubtitleWindowEnd == 50
        assert c.studioClipCount == 50
        assert c.studioClips[0]["cueNumber"] == 1

        # Adjust cue 0 speed directly without re-running TTS (0.5x doubles 400 ms -> 800 ms,
        # overflowing the 500 ms window by 300 ms).
        assert c.studioSetClipSpeed("cue0", 0.5) is True
        assert c.studioClips[0]["fitRate"] == pytest.approx(0.5, abs=1e-2)
        assert c.studioClips[0]["isOverflowed"] is True
        assert c.studioClips[0]["overflowMs"] == pytest.approx(300, abs=5)

        # Save back to the subtitle project and verify SubtitleController reloads.
        old_total_ms = sub_ctrl.durationMs
        assert c.studioSaveToSubtitle() is True
        assert sub_ctrl.durationMs == pytest.approx(old_total_ms, abs=10)
        saved_tl = store.load_timeline(proj.id)
        assert saved_tl is not None
        assert saved_tl.segments[0].end_ms == pytest.approx(900, abs=5)

    def test_video_dubbing_studio_speaker_voices_export_video_and_original_cue_audition(
        self, tmp_path: Path, monkeypatch
    ):
        from vienetts_app.core.align import DEFAULT_SAMPLE_RATE, FitPolicy
        from vienetts_app.core.audio import write_wav_file
        from vienetts_app.core.subtitle_project import (
            SubtitleProjectStore,
            SubtitleTrackRenderer,
            build_project,
        )
        from vienetts_app.core.subtitles import Cue
        from vienetts_app.core.synthesis_context import SynthesisContext
        from vienetts_app.ui.subtitle_controller import SubtitleController

        c = _make_controller(tmp_path)
        sub_ctrl = SubtitleController(c, data_dir=tmp_path, bg_runner=run_sync)
        c.attach_subtitle_controller(sub_ctrl)

        video_file = tmp_path / "interview.mp4"
        video_file.write_bytes(b"fake-video-bytes")

        store = SubtitleProjectStore(tmp_path / "subtitles")
        cues = (
            Cue(index=1, start_ms=100, end_ms=600, text="Câu một", speaker="SPEAKER_01"),
            Cue(index=2, start_ms=800, end_ms=1300, text="Câu hai", speaker="SPEAKER_02"),
        )
        ctx = SynthesisContext(profile="vieneu", model_revision="r1", voice_id="Binh")
        proj = build_project(
            tmp_path / "interview.srt",
            cues,
            FitPolicy.dub(rate_cap=1.5),
            voice_key="Binh",
            context=ctx,
            video_path=str(video_file),
            speaker_voices={"SPEAKER_01": "Ngoc", "SPEAKER_02": "Tuyen"},
            video_mix_mode="duck",
            video_bg_gain_db=-12.0,
            embed_subtitles=True,
        )
        store.save(proj)
        with SubtitleTrackRenderer(store, proj) as renderer:
            for i in range(len(cues)):
                renderer.add_clip(i, np.full(int(0.4 * DEFAULT_SAMPLE_RATE), 0.2, dtype=np.float32))
            renderer.finish()

        sub_ctrl._project = store.require(proj.id)
        sub_ctrl._load_reader()

        assert c.openSubtitleInStudio(proj.id, 0) is True
        assert c.studioIsSubtitle is True
        assert c.studioSubtitleHasVideo is True
        assert c.studioSubtitleVideoName == "interview.mp4"
        assert c.studioSubtitleVideoMixMode == "duck"
        assert c.studioSubtitleEmbedSubtitles is True

        # Clips expose per-speaker assigned voice in studioClips.
        clips = c.studioClips
        assert clips[0]["speaker"] == "SPEAKER_01"
        assert clips[0]["voice"] == "Ngoc"
        assert clips[1]["speaker"] == "SPEAKER_02"
        assert clips[1]["voice"] == "Tuyen"

        # Audition original video audio for cue 0 via studioPlayOriginalCue.
        extracted_calls: list[dict] = []

        def fake_extract(
            vpath,
            out_wav,
            *,
            sample_rate=16_000,
            mono=True,
            start_ms=None,
            end_ms=None,
            data_dir=None,
        ):
            extracted_calls.append(
                {
                    "video_path": str(vpath),
                    "start_ms": start_ms,
                    "end_ms": end_ms,
                    "sample_rate": sample_rate,
                }
            )
            write_wav_file(
                np.full(int(0.5 * sample_rate), 0.25, dtype=np.float32),
                Path(out_wav),
                sample_rate=sample_rate,
            )
            return Path(out_wav)

        monkeypatch.setattr("vienetts_app.core.video_dub.extract_video_audio", fake_extract)
        assert c.studioPlayOriginalCue("cue0") is True
        assert len(extracted_calls) == 1
        assert extracted_calls[0]["video_path"] == str(video_file)
        assert extracted_calls[0]["start_ms"] == 100
        assert extracted_calls[0]["end_ms"] == 600
        assert c.studioClipPlayingId == "cue0"
        assert c.replayActive is True
        c.stopReplay()

        # Edit clip 0 speed and stage a gain op in Studio; 1-click studioExportVideo
        # auto-commits staged ops, splices into track.wav, and muxes video!
        assert c.studioSetClipSpeed("cue0", 0.5) is True
        assert c.studioStageGain(3.0) is True
        assert c.studioPendingCount == 1
        mux_calls: list[dict] = []
        def fake_mux(
            vpath,
            dub_wav,
            out_path,
            *,
            srt_path=None,
            mix_mode="replace",
            bg_gain_db=-16.0,
            embed_subtitles=True,
            data_dir=None,
        ):
            out = Path(out_path)
            out.write_bytes(b"muxed-video")
            mux_calls.append(
                {
                    "video_path": str(vpath),
                    "dub_wav": str(dub_wav),
                    "out_path": str(out),
                    "srt_exists": srt_path is not None and Path(srt_path).is_file(),
                    "mix_mode": mix_mode,
                    "bg_gain_db": bg_gain_db,
                    "embed_subtitles": embed_subtitles,
                }
            )
            return out

        monkeypatch.setattr("vienetts_app.core.video_dub.mux_dubbed_video", fake_mux)
        exported: list[tuple[str, bool]] = []
        c.exportFinished.connect(lambda p, ok: exported.append((p, ok)))

        out_dir = tmp_path / "exports"
        assert c.studioExportVideo(str(out_dir)) is True
        assert len(mux_calls) == 1
        assert mux_calls[0]["video_path"] == str(video_file)
        assert mux_calls[0]["mix_mode"] == "duck"
        assert mux_calls[0]["bg_gain_db"] == pytest.approx(-12.0)
        assert mux_calls[0]["embed_subtitles"] is True
        assert mux_calls[0]["srt_exists"] is True
        assert len(exported) == 1
        assert exported[0][1] is True
        assert Path(exported[0][0]).is_file()
        assert Path(exported[0][0]).suffix == ".mp4"
        assert c.studioPendingCount == 0
        # Verify the splice was also saved back to the subtitle project timeline.
        saved_tl = store.load_timeline(proj.id)
        assert saved_tl is not None
        assert saved_tl.segments[0].end_ms == pytest.approx(900, abs=5)
