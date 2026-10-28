"""ui/subtitle_controller.py: the QML-facing SRT dub/transcript studio.

The controller drives synthesis through AppController's listener seam, so these
tests use a fake app that records submissions and a fake player that records
transport calls: render, karaoke highlight, policy invalidation and export are
all exercised without an engine or an audio device.
"""

from __future__ import annotations

import contextlib
import json
import re
import threading
import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PySide6.QtCore import Property, QCoreApplication, QObject, Signal

from vienetts_app.core.artifacts import SynthesisArtifact
from vienetts_app.core.audio import DEFAULT_SAMPLE_RATE, write_wav_file
from vienetts_app.core.jobs import JobTerminal
from vienetts_app.core.subtitle_project import (
    SubtitleProjectError,
    SubtitleProjectStore,
    SubtitleTrackRenderer,
    render_fingerprint,
)
from vienetts_app.core.subtitles import parse_cues
from vienetts_app.core.transcribe_pipeline import (
    ACOUSTIC_FALLBACK_NOTE,
    DIAR_CANCELLED_NOTE,
    DIAR_FAILED_NOTE,
    TranscriptionResult,
)
from vienetts_app.ui.bg_ops import SyncOrderedExecutor, run_sync
from vienetts_app.ui.subtitle_controller import API_ONLY_PROPERTIES, SubtitleController

SRT = "1\n00:00:00,000 --> 00:00:02,000\nXin chào\n\n2\n00:00:03,000 --> 00:00:05,000\nTạm biệt\n"

# Adjacent cues (no gap) so the sentence-merge guard lets them join.
SRT_TIGHT = (
    "1\n00:00:00,000 --> 00:00:02,000\nXin chào\n\n2\n00:00:02,000 --> 00:00:04,000\nTạm biệt\n"
)


class FakePlayer(QObject):
    """Records transport calls; emits the signals the controller listens to."""

    stateChanged = Signal()
    finished = Signal()
    positionChanged = Signal(int)
    durationChanged = Signal(int)
    errorTextChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self.state = "stopped"
        self.errorText = ""
        self.played: list[str] = []
        self.seeks: list[int] = []

    def play(self, path: Any, on_released: Any = None) -> bool:
        self.played.append(str(path))
        self.state = "playing"
        self.stateChanged.emit()
        return True

    def pause(self) -> None:
        self.state = "paused"
        self.stateChanged.emit()

    def resume(self) -> None:
        self.state = "playing"
        self.stateChanged.emit()

    def stop(self) -> None:
        self.state = "stopped"
        self.stateChanged.emit()

    def seek(self, ms: int) -> None:
        self.seeks.append(int(ms))


class FakeApp:
    """Minimal AppController surface the controller reads via getattr."""

    def __init__(self) -> None:
        self.defaultVoice = "voice-a"
        self.exportFormat = "wav"
        self.pending: list[tuple[str, str, Any]] = []
        self.cancelled: list[str] = []

    def submit_stream_for_listener(self, text, voice, listener, *, kind="requested_chapter"):
        job_id = uuid.uuid4().hex
        self.pending.append((job_id, text, listener))
        return job_id

    def cancel_job(self, job_id):
        self.cancelled.append(job_id)
        return True


def clip_ms(text: str) -> int:
    return max(200, len(text) * 100)


def render_context(profile: str = "vieneu", **overrides: Any) -> Any:
    """One engine identity for provenance tests (never a real model)."""
    from vienetts_app.core.synthesis_context import context_for

    kwargs: dict[str, Any] = {"language": "vi" if profile == "vieneu" else "zh"}
    kwargs.update(overrides)
    return context_for(profile, **kwargs)


class IdentifiedApp(FakeApp):
    """FakeApp with the engine-identity gate (Phase 5 Task 5.3).

    ``contexts`` maps a voice to the identity the active engine would use for
    it; a voice with no entry is REFUSED the way the capability table refuses
    an unsupported combination (reported on ``errorText`` only when the caller
    asked for a report).
    """

    def __init__(self) -> None:
        super().__init__()
        self.contexts: dict[str, Any] = {}
        self.errorText = ""
        self.submitted: list[dict[str, Any]] = []

    def submission_context_for(self, voice, *, report=True):
        context = self.contexts.get(str(voice or ""))
        if context is None:
            if report:
                self.errorText = f"the active engine cannot serve {voice!r}"
            return None
        return context

    def submit_stream_for_listener(
        self, text, voice, listener, *, kind="requested_chapter", context=None
    ):
        self.submitted.append({"text": text, "voice": voice, "context": context})
        return super().submit_stream_for_listener(text, voice, listener, kind=kind)


def make_controller(
    tmp_path: Path,
    fake: FakeApp,
    player: FakePlayer,
    *,
    unit_executor: Any = None,
    bg_runner: Any = run_sync,
) -> SubtitleController:
    return SubtitleController(
        fake,
        data_dir=tmp_path,
        player_factory=lambda: player,
        store_factory=lambda root: SubtitleProjectStore(Path(root) / "subtitles"),
        bg_runner=bg_runner,
        unit_executor=SyncOrderedExecutor() if unit_executor is None else unit_executor,
    )


def transcribed(cues: Any, *notes: str, labelled: Any = None) -> Any:
    """A ``run_transcription`` stand-in: ``labelled`` when speakers are asked for."""

    def run(media: Any, asr_cfg: Any, diar_cfg: Any = None, **kw: Any) -> TranscriptionResult:
        Path(kw["work_dir"]).mkdir(parents=True, exist_ok=True)  # as the 16 kHz prep does
        out = labelled if diar_cfg is not None and labelled is not None else cues
        return TranscriptionResult(list(out), list(notes))

    return run


def make_artifact(job_id: str, text: str, tmp_path: Path) -> tuple[SynthesisArtifact, Path]:
    """A real artifact WAV under the data dir (releasable by the controller)."""
    ms = clip_ms(text)
    frames = int(round(ms * DEFAULT_SAMPLE_RATE / 1000))
    path = tmp_path / "artifacts" / "interactive" / f"{job_id}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    write_wav_file(np.full(frames, 0.4, dtype=np.float32), path, DEFAULT_SAMPLE_RATE)
    return (
        SynthesisArtifact(
            job_id=job_id,
            path=path,
            sample_rate=DEFAULT_SAMPLE_RATE,
            samples=frames,
            duration_ms=ms,
        ),
        path,
    )


def complete_next_unit(fake: FakeApp, tmp_path: Path) -> None:
    """Deliver a completed terminal for the oldest pending unit."""
    job_id, text, listener = fake.pending.pop(0)
    artifact, _ = make_artifact(job_id, text, tmp_path)
    listener.on_synthesis_terminal(
        JobTerminal(job_id=job_id, owner="audiobook", state="completed", value=artifact)
    )


def render_all(controller: SubtitleController, fake: FakeApp, tmp_path: Path) -> None:
    controller.render()
    while fake.pending:
        complete_next_unit(fake, tmp_path)


@pytest.fixture()
def env(tmp_path: Path) -> tuple[SubtitleController, FakeApp, FakePlayer]:
    fake = FakeApp()
    player = FakePlayer()
    controller = make_controller(tmp_path, fake, player)
    return controller, fake, player


def load_srt(tmp_path: Path, text: str = SRT) -> str:
    path = tmp_path / "movie.srt"
    path.write_text(text, encoding="utf-8")
    return str(path)


# ── import ───────────────────────────────────────────────────────────────────


def test_import_srt_builds_the_workspace(env, tmp_path):
    controller, _, _ = env
    assert controller.importSrt(load_srt(tmp_path)) is True
    assert controller.loaded is True
    assert controller.title == "movie"
    assert controller.cueCount == 2
    assert controller.errorText == ""
    rows = controller.cues
    assert rows[0]["startLabel"] == "00:00:00,000"
    assert rows[1]["startLabel"] == "00:00:03,000"
    assert rows[0]["text"] == "Xin chào"
    # Highlight lives on activeCue, not on the row payload (a stable list).
    assert "active" not in rows[0]
    assert controller.activeCue == -1

    # A legacy encoding decodes like the Audiobook import and says so on the
    # warning channel — for an imported SRT and a video's companion alike.
    legacy = tmp_path / "legacy.srt"
    legacy.write_bytes(SRT.encode("utf-16-le"))
    assert controller.importSrt(str(legacy)) is True
    assert controller.cues[1]["text"] == "Tạm biệt"
    assert controller.noticeLevel == "warning" and "UTF-16" in controller.errorText
    controller.clearNotice()
    legacy.write_bytes("1\n00:00:00,000 --> 00:00:02,000\nXin chào\n".encode("cp1258"))
    video = tmp_path / "legacy.mp4"
    video.write_bytes(b"fake_mp4")
    assert controller.importVideo(str(video)) is True
    assert controller.hasVideo is True and controller.cues[0]["text"] == "Xin chào"
    assert controller.noticeLevel == "warning" and "1258" in controller.errorText


def test_import_rejections(env, tmp_path):
    controller, _, _ = env
    assert controller.importSrt(str(tmp_path / "nope.srt")) is False
    assert "nope.srt" in controller.errorText

    controller._set_error("")  # noqa: SLF001
    path = tmp_path / "bad.srt"
    path.write_text("just prose\nno timecodes\n", encoding="utf-8")
    assert controller.importSrt(str(path)) is False
    assert controller.errorText != ""


# ── render ───────────────────────────────────────────────────────────────────


def test_render_streams_units_and_exposes_the_result(env, tmp_path):
    controller, fake, _ = env
    controller.importSrt(load_srt(tmp_path))
    assert controller.rendered is False

    render_all(controller, fake, tmp_path)

    assert controller.rendering is False
    assert controller.rendered is True
    assert controller.renderProgress == 1.0
    assert controller.durationMs == 3_800  # cue2 800 ms placed at 3000 ms
    assert controller.statsSummary.startswith("2 phụ đề")
    # The unit artifacts are cleaned up after being placed.
    assert list((tmp_path / "artifacts" / "interactive").glob("*.wav")) == []


def test_render_uses_merged_units_when_enabled(env, tmp_path):
    controller, fake, _ = env
    controller.importSrt(load_srt(tmp_path, SRT_TIGHT))
    controller.mergeSentences = True
    controller.render()
    # Two punctuation-free cues merge into one synthesis unit.
    assert len(fake.pending) == 1
    assert fake.pending[0][1] == "Xin chào Tạm biệt"
    while fake.pending:
        complete_next_unit(fake, tmp_path)
    assert controller.rendered is True

    # Transcript mode never compresses the speech rate.
    controller.mode = "transcript"
    controller.mergeSentences = False
    fake.pending.clear()
    controller.render()
    while fake.pending:
        complete_next_unit(fake, tmp_path)
    assert controller.rendered is True
    project = controller._store.require(controller._project.id)  # noqa: SLF001
    assert project.stats.max_rate == 1.0


def test_render_and_reimport_reuse_a_cached_track(env, tmp_path):
    """A second render and a same-file reimport both adopt the stored track
    instead of submitting a fresh synthesis or clobbering the render."""
    controller, fake, _ = env
    path = load_srt(tmp_path)
    controller.importSrt(path)
    render_all(controller, fake, tmp_path)
    duration = controller.durationMs
    stats = controller.statsSummary
    adjusted = controller._project.adjusted  # noqa: SLF001
    fake.pending.clear()

    controller.render()
    assert fake.pending == []  # cached: nothing submitted
    assert controller.rendered is True

    # Re-importing the same file must adopt the stored render, not save a
    # fresh (unrendered) project over it.
    assert controller.importSrt(path) is True
    assert fake.pending == []  # no new synthesis
    assert controller.rendered is True
    assert controller.durationMs == duration
    assert controller.statsSummary == stats
    assert controller._project.adjusted == adjusted  # noqa: SLF001
    assert controller._timeline is not None  # noqa: SLF001

    target = controller.exportSrt(str(tmp_path / "out"))
    assert parse_cues(Path(target).read_text(encoding="utf-8")) == list(adjusted)


def test_policy_rebuild_refreshes_the_cue_rows_in_place(env, tmp_path):
    """M5 / FR-1.9: a knob re-plans timing, so each row's badges follow the new
    plan. The rows are diffed in place — never removed or re-inserted."""
    controller, fake, _ = env
    long_text = "Một câu thoại khá dài cần nén mạnh hơn mới vừa khung"  # 52 chars
    controller.importSrt(load_srt(tmp_path, SRT.replace("Xin chào", long_text)))
    render_all(controller, fake, tmp_path)
    model = controller.cueModel
    row_churn: list[Any] = []
    for signal in (model.modelReset, model.rowsRemoved, model.rowsInserted):
        signal.connect(lambda *args: row_churn.append(args))
    row = controller.cues[0]
    assert row["fitRate"] > 1.0 and row["overflowMs"] > 0
    assert row["overBudget"] is False  # 52 chars fit the default 1.5× budget

    controller.rateCap = 1.2
    controller.confirmRenderInvalidation()
    row = controller.cues[0]
    assert (row["fitRate"], row["overflowMs"], row["overBudget"]) == (1.0, 0, True)
    assert model.get(0)["overBudget"] is True
    controller.mode = "transcript"  # no live render left: applies at once
    assert controller.cues[0]["overBudget"] is False  # transcript mode has no budget
    assert model.get(0)["overBudget"] is False
    assert row_churn == []


def test_policy_round_trip_restores_the_cached_render(env, tmp_path):
    controller, fake, _ = env
    controller.importSrt(load_srt(tmp_path))
    render_all(controller, fake, tmp_path)
    duration = controller.durationMs
    adjusted = controller._project.adjusted  # noqa: SLF001
    fake.pending.clear()

    controller.rateCap = 1.2  # away: staged behind the destroy confirmation
    assert controller.rendered is True  # nothing lost until the user agrees
    assert controller.invalidationPending is True
    assert controller.invalidationReason == "rate_cap"
    assert "lồng tiếng" in controller.invalidationMessage
    controller.confirmRenderInvalidation()
    assert controller.rendered is False
    controller.rateCap = 1.5  # back: the cached render is adopted again
    assert controller.rendered is True
    assert controller.durationMs == duration
    assert controller._project.adjusted == adjusted  # noqa: SLF001
    assert controller._timeline is not None  # noqa: SLF001
    assert fake.pending == []


def test_policy_change_clears_the_stale_timeline(env, tmp_path):
    # The project id is content-derived, so an uncached fingerprint still maps
    # to a directory holding the PREVIOUS render's timeline — it must not be
    # loaded while the current project is unrendered.
    controller, fake, player = env
    controller.importSrt(load_srt(tmp_path))
    render_all(controller, fake, tmp_path)
    assert controller._timeline is not None  # noqa: SLF001

    controller.rateCap = 1.2  # fingerprint moves away from the stored render
    assert controller.rendered is True  # staged: the render is still live
    controller.confirmRenderInvalidation()
    assert controller.rendered is False
    assert controller._timeline is None  # noqa: SLF001
    controller.seekToCue(1)  # a stale timeline must not answer seeks
    assert player.seeks == []

    # A re-import under the changed policy likewise adopts no stale timeline.
    assert controller.importSrt(load_srt(tmp_path)) is True
    assert controller.rendered is False
    assert controller._timeline is None  # noqa: SLF001
    controller.seekToCue(1)
    assert player.seeks == []


def test_terminal_outcomes(env, tmp_path):
    controller, fake, _ = env
    controller.importSrt(load_srt(tmp_path))
    controller.render()
    job_id, _text, listener = fake.pending.pop(0)
    listener.on_synthesis_terminal(
        JobTerminal(job_id=job_id, owner="audiobook", state="failed", error="boom")
    )
    assert controller.rendering is False
    assert controller.errorText == "boom"
    assert controller.rendered is False

    # A cancelled render is not an error.
    controller._set_error("")  # noqa: SLF001
    controller.render()
    job_id = fake.pending[0][0]
    controller.cancelRender()
    assert fake.cancelled == [job_id]
    controller.on_synthesis_terminal(
        JobTerminal(job_id=job_id, owner="audiobook", state="cancelled")
    )
    assert controller.rendering is False
    assert controller.errorText == ""

    # M1: cancelling after the job already completed (its terminal still in
    # flight) ends the render now; the late result submits nothing more.
    fake.pending.clear()
    controller.render()
    job_id, text, listener = fake.pending.pop(0)
    controller.cancelRender()
    assert controller.rendering is False
    artifact, wav = make_artifact(job_id, text, tmp_path)
    listener.on_synthesis_terminal(
        JobTerminal(job_id=job_id, owner="audiobook", state="completed", value=artifact)
    )
    assert fake.pending == [] and controller.rendering is False
    assert controller.rendered is False and controller.errorText == ""
    assert not wav.exists()  # the late artifact is released, never placed


def test_render_open_failure_is_a_clean_error(env, tmp_path, monkeypatch):
    controller, fake, _ = env
    controller.importSrt(load_srt(tmp_path))

    class BrokenRenderer:
        def __init__(self, *_args, **_kwargs):
            pass

        def open(self):
            raise OSError("simulated open failure")

    monkeypatch.setattr("vienetts_app.ui.subtitle_controller.SubtitleTrackRenderer", BrokenRenderer)
    controller.render()
    # A raw OSError must never escape the slot; the controller lands clean.
    assert controller.rendering is False
    assert controller.errorText != ""
    assert controller.rendered is False
    assert fake.pending == []

    # If the engine seam raises inside submit, nothing may escape the slot —
    # the render fails cleanly and the partial track is aborted.

    class RaisingSubmitApp(FakeApp):
        def submit_stream_for_listener(self, text, voice, listener, *, kind="requested_chapter"):
            raise OSError("simulated submit failure")

    raising = RaisingSubmitApp()
    controller = make_controller(tmp_path / "submit-raise", raising, FakePlayer())
    controller.importSrt(load_srt(tmp_path))
    controller.render()
    assert controller.rendering is False
    assert controller.errorText != ""
    assert controller.rendered is False
    assert raising.pending == []
    assert not list((tmp_path / "submit-raise" / "subtitles").glob("**/*.part.wav"))


def test_render_stage_failures_reset_state(env, tmp_path, monkeypatch):
    # A split failure mid-render releases the bad unit's artifact and resets.
    import vienetts_app.ui.subtitle_controller as subtitle_module

    controller, fake, _ = env
    controller.importSrt(load_srt(tmp_path))
    real_split = subtitle_module.split_unit_audio
    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.split_unit_audio",
        lambda *_a, **_k: (_ for _ in ()).throw(IndexError("simulated split failure")),
    )
    controller.render()
    job_id, text, listener = fake.pending.pop(0)
    artifact, artifact_path = make_artifact(job_id, text, tmp_path)
    listener.on_synthesis_terminal(
        JobTerminal(job_id=job_id, owner="audiobook", state="completed", value=artifact)
    )
    assert controller.rendering is False
    assert controller.errorText != ""
    assert not artifact_path.exists()  # the bad unit's artifact is released
    assert not list((tmp_path / "subtitles").glob("**/*.part.wav"))
    monkeypatch.setattr("vienetts_app.ui.subtitle_controller.split_unit_audio", real_split)

    # A renderer.finish() raise fails the render cleanly.
    case = tmp_path / "case-b"
    fake = FakeApp()
    controller = make_controller(case, fake, FakePlayer())
    controller.importSrt(load_srt(tmp_path))
    controller.render()
    monkeypatch.setattr(
        controller._renderer,  # noqa: SLF001
        "finish",
        lambda: (_ for _ in ()).throw(OSError("simulated finish failure")),
    )
    while fake.pending:
        complete_next_unit(fake, tmp_path)
    assert controller.rendering is False
    assert controller.errorText != ""
    assert controller.rendered is False

    # The store reload after finish() sits inside the same failure boundary:
    # a raise there fails the render cleanly, and the already-finished
    # renderer must NOT be aborted — its track is live.
    case = tmp_path / "case-c"
    fake = FakeApp()
    controller = make_controller(case, fake, FakePlayer())
    controller.importSrt(load_srt(tmp_path))
    controller.render()
    aborts: list[SubtitleTrackRenderer] = []
    original_abort = SubtitleTrackRenderer.abort

    def spy_abort(self):
        aborts.append(self)
        original_abort(self)

    monkeypatch.setattr(SubtitleTrackRenderer, "abort", spy_abort)
    monkeypatch.setattr(
        controller._store,  # noqa: SLF001
        "require",
        lambda *_a, **_k: (_ for _ in ()).throw(OSError("simulated reload failure")),
    )
    while fake.pending:
        complete_next_unit(fake, case)
    assert controller.rendering is False
    assert controller.errorText != ""
    assert aborts == []


def test_terminal_artifact_release_and_cleanup_edges(env, tmp_path, monkeypatch):
    controller, fake, _ = env
    controller.importSrt(load_srt(tmp_path))
    controller.render()
    job_id, text, listener = fake.pending.pop(0)
    artifact, artifact_path = make_artifact(job_id, text, tmp_path)
    listener.on_synthesis_terminal(
        JobTerminal(job_id=job_id, owner="audiobook", state="failed", value=artifact, error="boom")
    )
    assert controller.rendering is False
    assert controller.errorText == "boom"
    assert not artifact_path.exists()

    # A cancelled terminal releases its artifact without surfacing an error.
    case = tmp_path / "case-b"
    fake = FakeApp()
    controller = make_controller(case, fake, FakePlayer())
    controller.importSrt(load_srt(tmp_path))
    controller.render()
    job_id, text, listener = fake.pending.pop(0)
    artifact, artifact_path = make_artifact(job_id, text, case)
    listener.on_synthesis_terminal(
        JobTerminal(job_id=job_id, owner="audiobook", state="cancelled", value=artifact)
    )
    assert controller.rendering is False
    assert controller.errorText == ""
    assert not artifact_path.exists()

    # A completed terminal carrying another job's artifact fails cleanly.
    case = tmp_path / "case-c"
    fake = FakeApp()
    controller = make_controller(case, fake, FakePlayer())
    controller.importSrt(load_srt(tmp_path))
    controller.render()
    job_id, text, listener = fake.pending.pop(0)
    # The artifact belongs to a different job: it is invalid for this unit.
    artifact, artifact_path = make_artifact(uuid.uuid4().hex, text, case)
    listener.on_synthesis_terminal(
        JobTerminal(job_id=job_id, owner="audiobook", state="completed", value=artifact)
    )
    assert controller.rendering is False
    assert controller.errorText != ""
    assert not artifact_path.exists()

    # Cleanup raising must not wedge the controller: state still resets.
    case = tmp_path / "case-d"
    fake = FakeApp()
    controller = make_controller(case, fake, FakePlayer())
    controller.importSrt(load_srt(tmp_path))
    controller.render()
    renderer = controller._renderer  # noqa: SLF001
    monkeypatch.setattr(
        renderer, "abort", lambda: (_ for _ in ()).throw(OSError("simulated abort failure"))
    )
    job_id, _text, listener = fake.pending.pop(0)
    listener.on_synthesis_terminal(
        JobTerminal(job_id=job_id, owner="audiobook", state="failed", error="kaboom")
    )
    assert controller.rendering is False
    assert controller.errorText == "kaboom"


# ── engine identity (Phase 5 Task 5.3) ───────────────────────────────────────


def identified_env(tmp_path: Path) -> tuple[SubtitleController, IdentifiedApp, FakePlayer]:
    """A controller whose app resolves one identity per voice (VieNeu by default)."""
    fake = IdentifiedApp()
    fake.contexts["voice-a"] = render_context()
    player = FakePlayer()
    return make_controller(tmp_path, fake, player), fake, player


def test_render_carries_the_engine_identity_it_was_submitted_with(tmp_path):
    controller, fake, _ = identified_env(tmp_path)
    controller.importSrt(load_srt(tmp_path))
    render_all(controller, fake, tmp_path)

    assert fake.submitted  # one submission per speech unit
    contexts = [entry["context"] for entry in fake.submitted]
    assert all(context is not None for context in contexts)
    assert {context.profile for context in contexts} == {"vieneu"}
    context = contexts[0]
    assert controller._project.context == context  # noqa: SLF001
    assert controller._store.require(controller._project.id).context == context  # noqa: SLF001
    # The identity the track was rendered with is reusable as-is.
    fake.pending.clear()
    fake.submitted.clear()
    controller.render()
    assert fake.submitted == []
    assert controller.rendered is True


def test_an_engine_switch_re_renders_instead_of_adopting_the_track(tmp_path):
    controller, fake, _ = identified_env(tmp_path)
    controller.importSrt(load_srt(tmp_path))
    render_all(controller, fake, tmp_path)
    fake.pending.clear()
    fake.submitted.clear()

    # The same cues under another engine: the cached track is not adopted.
    fake.contexts["voice-a"] = render_context("qwen_custom_0_6b", voice_id="Vivian")
    controller.render()
    assert len(fake.pending) == 1  # re-synthesized, not reused
    assert fake.submitted[0]["context"].profile == "qwen_custom_0_6b"
    # The previous engine's measured alignment is not presented as current.
    assert controller.rendered is False
    assert controller.durationMs == 0
    assert controller._timeline is None  # noqa: SLF001
    while fake.pending:
        complete_next_unit(fake, tmp_path)
    assert controller.rendered is True
    assert controller._project.context.profile == "qwen_custom_0_6b"  # noqa: SLF001

    # ...and switching back re-renders too (the new track is the Qwen one),
    # from the VieNeu clips the unit cache still holds (FR-5.1).
    fake.contexts["voice-a"] = render_context()
    fake.submitted.clear()
    controller.render()
    assert fake.submitted == [] and fake.pending == []
    assert controller.rendered is True and controller.rendering is False
    assert controller._project.context.profile == "vieneu"  # noqa: SLF001


def numbered_srt(count: int) -> str:
    """``count`` cues of 2 s, 3 s apart, reading "Câu số N."."""

    def stamp(ms: int) -> str:
        minutes, ms = divmod(ms, 60_000)
        return f"00:{minutes:02d}:{ms // 1000:02d},{ms % 1000:03d}"

    return "".join(
        f"{n + 1}\n{stamp(n * 3000)} --> {stamp(n * 3000 + 2000)}\nCâu số {n + 1}.\n\n"
        for n in range(count)
    )


def test_a_rerender_synthesizes_only_units_missing_from_the_clip_cache(tmp_path, monkeypatch):
    # FR-5.1: each synthesized unit's raw clip is cached under its inputs, so
    # a re-render synthesizes only what changed and re-places the rest.
    controller, fake, _ = identified_env(tmp_path)
    controller.importSrt(load_srt(tmp_path, numbered_srt(30)))
    render_all(controller, fake, tmp_path)
    assert len(fake.submitted) == 30 and controller.rendered is True
    clips = sorted((project_dir(controller) / "units").glob("*.wav"))
    assert len(clips) == 30
    # The engine's artifacts are released; a cached clip never is.
    assert not list((tmp_path / "artifacts").rglob("*.wav"))
    controller._release_artifact(  # noqa: SLF001
        SynthesisArtifact("cached", clips[0], DEFAULT_SAMPLE_RATE, 1, 1)
    )
    assert clips[0].is_file()

    def stage(knob: str, value: Any) -> None:
        setattr(controller, knob, value)
        controller.confirmRenderInvalidation()

    def rerender() -> list[str]:
        fake.submitted.clear()
        render_all(controller, fake, tmp_path)
        assert controller.rendered is True and controller.rendering is False
        return [entry["text"] for entry in fake.submitted]

    # Editing 1 of 30 cues re-synthesizes exactly that unit.
    assert controller.updateCue(4, "Câu đã sửa.", "") is True
    controller.confirmRenderInvalidation()
    assert rerender() == ["Câu đã sửa."]
    # Offset and rate cap only move placement: nothing is synthesized, and
    # the engine identity is hashed once per voice, not once per unit.
    from vienetts_app.core.synthesis_context import SynthesisContext

    hashed: list[int] = []
    fingerprint = SynthesisContext.fingerprint
    monkeypatch.setattr(
        SynthesisContext, "fingerprint", lambda ctx: hashed.append(1) or fingerprint(ctx)
    )
    duration = controller.durationMs
    stage("offsetMs", 120)
    assert rerender() == []
    assert len(hashed) == 1
    assert controller.durationMs == duration + 120
    stage("rateCap", 1.2)
    assert rerender() == []
    assert controller._project.policy.rate_cap == 1.2  # noqa: SLF001

    # A voice re-enrolled under the same name (its voices.json entry changed)
    # re-synthesizes its units; another voice's entry changing does not.
    voices = tmp_path / "voices" / "voices.json"
    voices.parent.mkdir(parents=True, exist_ok=True)
    voices.write_text(json.dumps({"presets": {"voice-a": {"codes": [1, 2]}}}), encoding="utf-8")
    stage("offsetMs", 0)
    assert len(rerender()) == 30
    entries = {"voice-a": {"codes": [1, 2]}, "voice-z": {"codes": [3]}}
    voices.write_text(json.dumps({"presets": entries}), encoding="utf-8")
    stage("offsetMs", 60)
    assert rerender() == []

    # A unit failing at index 5 fails the render; the retry resumes there.
    vieneu_clips = set((project_dir(controller) / "units").glob("*.wav"))
    fake.contexts["voice-a"] = render_context("qwen_custom_0_6b", voice_id="Vivian")
    fake.submitted.clear()
    controller.render()
    for _ in range(5):
        complete_next_unit(fake, tmp_path)
    job_id, _text, listener = fake.pending.pop(0)
    listener.on_synthesis_terminal(
        JobTerminal(job_id=job_id, owner="audiobook", state="failed", error="boom")
    )
    assert controller.rendering is False and controller.errorText == "boom"
    assert rerender() == [f"Câu số {n}." for n in range(6, 31)]

    # A cached clip found unreadable at placement fails the render with a
    # clear message, and the retry synthesizes just that unit again.
    qwen_clips = sorted(set((project_dir(controller) / "units").glob("*.wav")) - vieneu_clips)
    assert len(qwen_clips) == 30
    qwen_clips[7].write_bytes(b"not a wav")
    stage("offsetMs", 0)
    controller.render()
    assert controller.rendering is False and "âm thanh đã lưu" in controller.errorText
    assert len(rerender()) == 1


def test_a_refused_combination_reports_and_submits_nothing(tmp_path):
    controller, fake, _ = identified_env(tmp_path)
    controller.importSrt(load_srt(tmp_path))
    fake.contexts.clear()  # the active engine cannot serve this voice

    controller.render()

    assert fake.pending == []
    assert controller.rendering is False
    assert controller.errorText == fake.errorText != ""
    assert controller.rendered is False


def test_import_with_a_refused_combination_neither_adopts_nor_overwrites(tmp_path):
    controller, fake, _ = identified_env(tmp_path)
    path = load_srt(tmp_path)
    controller.importSrt(path)
    render_all(controller, fake, tmp_path)
    stored = controller._project  # noqa: SLF001
    duration = controller.durationMs
    fake.pending.clear()

    fake.contexts.clear()
    assert controller.importSrt(path) is True
    # The stored render is another engine's: it is neither adopted nor
    # overwritten by an unrendered project.
    assert controller.rendered is False
    assert controller._project.fingerprint != stored.fingerprint  # noqa: SLF001
    saved = controller._store.require(stored.id)  # noqa: SLF001
    assert saved.rendered is True
    assert saved.context == stored.context

    # The render is back once the identity is available again.
    fake.contexts["voice-a"] = render_context()
    assert controller.importSrt(path) is True
    assert controller.rendered is True
    assert controller.durationMs == duration
    assert fake.pending == []


def test_a_pre_provenance_render_survives_the_upgrade(tmp_path):
    controller, fake, _ = identified_env(tmp_path)
    path = load_srt(tmp_path)
    controller.importSrt(path)
    render_all(controller, fake, tmp_path)
    project = controller._project  # noqa: SLF001
    # Rewrite the workspace the way the pre-multi-engine app left it: the
    # pre-context fingerprint, and no identity at all.
    controller._store.save(  # noqa: SLF001
        replace(
            project,
            context=None,
            fingerprint=render_fingerprint(
                project.cues, project.policy, project.sample_rate, project.voice_key
            ),
        )
    )
    fake.pending.clear()
    fake.submitted.clear()

    # VieNeu — the engine the pre-multi-engine app used — adopts it.
    assert controller.importSrt(path) is True
    assert controller.rendered is True
    assert controller.durationMs == project.total_ms
    controller.render()
    assert fake.pending == []  # still cached: nothing re-synthesized
    assert controller.rendered is True


# ── playback + karaoke ───────────────────────────────────────────────────────


def test_play_renders_first_when_needed(env, tmp_path):
    controller, fake, player = env
    controller.importSrt(load_srt(tmp_path))
    controller.play()
    assert controller.rendering is True
    while fake.pending:
        complete_next_unit(fake, tmp_path)
    assert player.played and player.played[0].endswith("track.wav")


def test_playback_seeks_and_karaoke_highlight(env, tmp_path):
    controller, fake, player = env
    controller.importSrt(load_srt(tmp_path))
    render_all(controller, fake, tmp_path)

    # The cue model must stay identity-stable across active-cue transitions.
    counts = {"active": 0, "cues": 0}
    controller.activeCueChanged.connect(lambda: counts.__setitem__("active", counts["active"] + 1))
    controller.cuesChanged.connect(lambda: counts.__setitem__("cues", counts["cues"] + 1))
    model = controller.cues

    controller.play()
    assert player.played[0].endswith("track.wav")

    # Karaoke highlight follows the measured timeline.
    player.positionChanged.emit(100)
    assert controller.activeCue == 0
    assert controller.activeCharStart >= 0

    player.positionChanged.emit(3_100)
    assert controller.activeCue == 1

    # 800–3000 ms is rendered silence between the two cues: no highlight.
    player.positionChanged.emit(1_500)
    assert controller.activeCue == -1
    assert controller.activeCharStart == -1
    assert controller.activeCharEnd == -1

    assert counts == {"active": 3, "cues": 0}
    assert controller.cues is model  # identity stable across transitions

    # seekToCue uses the measured timeline; out of range is a no-op.
    controller.seekToCue(1)
    assert player.seeks[-1] == 3_000
    controller.seekToCue(99)
    assert player.seeks == [3_000]

    # stop resets the highlight and the transport state.
    player.positionChanged.emit(100)
    assert controller.activeCue == 0
    controller.stopPlay()
    assert controller.activeCue == -1
    assert controller.playerState == "stopped"


# ── policy signals ───────────────────────────────────────────────────────────


def test_policy_knobs_emit_policy_changed_exactly_once(env, tmp_path):
    controller, _, _ = env
    counts = {"policy": 0, "mode": 0, "merge": 0, "rate": 0, "gap": 0, "off": 0, "voice": 0}
    controller.policyChanged.connect(lambda: counts.__setitem__("policy", counts["policy"] + 1))
    controller.modeChanged.connect(lambda: counts.__setitem__("mode", counts["mode"] + 1))
    controller.mergeSentencesChanged.connect(
        lambda: counts.__setitem__("merge", counts["merge"] + 1)
    )
    controller.rateCapChanged.connect(lambda: counts.__setitem__("rate", counts["rate"] + 1))
    controller.maxGapMsChanged.connect(lambda: counts.__setitem__("gap", counts["gap"] + 1))
    controller.offsetMsChanged.connect(lambda: counts.__setitem__("off", counts["off"] + 1))
    controller.voiceChanged.connect(lambda: counts.__setitem__("voice", counts["voice"] + 1))

    # No-op writes emit nothing at all.
    controller.rateCap = 1.5
    controller.mode = "dub"
    controller.voice = ""
    assert counts == {"policy": 0, "mode": 0, "merge": 0, "rate": 0, "gap": 0, "off": 0, "voice": 0}

    controller.rateCap = 1.2
    assert counts == {"policy": 1, "mode": 0, "merge": 0, "rate": 1, "gap": 0, "off": 0, "voice": 0}

    # Switching to transcript forces mergeSentences on — once, because it was off.
    # The effective gap moves 0 → 1500 (transcript's real default), so the
    # field is notified as well (FR-1.5).
    controller.mode = "transcript"
    assert counts == {"policy": 2, "mode": 1, "merge": 1, "rate": 1, "gap": 1, "off": 0, "voice": 0}

    controller.mode = "transcript"  # no-op
    assert counts["policy"] == 2

    # Back and forth again: mergeSentences stayed True, so no second emit.
    controller.mode = "dub"
    controller.mode = "transcript"
    assert counts["merge"] == 1
    assert counts["mode"] == 3
    assert counts["policy"] == 4
    assert counts["gap"] == 3  # each mode switch changes the effective gap

    controller.maxGapMs = 500
    controller.offsetMs = 100
    controller.mergeSentences = False
    controller.voice = "voice-b"
    assert counts["policy"] == 8
    assert counts["gap"] == 4
    assert counts["off"] == 1
    assert counts["merge"] == 2
    assert counts["voice"] == 1


# ── export ───────────────────────────────────────────────────────────────────


def test_export_track_emits_finished(env, tmp_path):
    controller, fake, _ = env
    controller.importSrt(load_srt(tmp_path))
    render_all(controller, fake, tmp_path)
    results: list[tuple[str, str]] = []
    controller.exportFinished.connect(lambda path, error: results.append((path, error)))

    target = controller.exportTrack(str(tmp_path / "out"))
    assert target.endswith(".wav")
    assert results and results[0][1] == ""
    assert Path(results[0][0]).is_file()
    assert controller.exporting is False

    # The SRT export writes the retimed cues.
    target = controller.exportSrt(str(tmp_path / "out"))
    assert target.endswith(".srt")
    assert results[-1] == (target, "")
    assert controller.exporting is False  # run_sync completed inline
    exported = parse_cues(Path(target).read_text(encoding="utf-8"))
    project = controller._store.require(controller._project.id)  # noqa: SLF001
    assert exported == list(project.adjusted)
    assert exported[1].start_ms == 3_000

    # L4: a render saved before adjusted cues existed exports its cues — the
    # source canExportSrt and exportVideo use — never an empty file.
    legacy = replace(controller._project, adjusted=())  # noqa: SLF001
    controller._project = legacy  # noqa: SLF001
    target = controller.exportSrt(str(tmp_path / "out"))
    assert parse_cues(Path(target).read_text(encoding="utf-8")) == list(legacy.cues)


def test_export_srt_failure_reports_and_resets(env, tmp_path, monkeypatch):
    controller, fake, _ = env
    controller.importSrt(load_srt(tmp_path))
    render_all(controller, fake, tmp_path)
    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.export_srt_file",
        lambda *_a, **_k: (_ for _ in ()).throw(SubtitleProjectError("simulated export failure")),
    )
    results: list[tuple[str, str]] = []
    controller.exportFinished.connect(lambda path, error: results.append((path, error)))

    target = controller.exportSrt(str(tmp_path / "out"))
    assert target.endswith(".srt")  # the planned target is still returned
    assert results and results[0][0] == ""
    assert "simulated export failure" in results[0][1]
    assert controller.exporting is False
    assert controller.errorText != ""


def test_exports_share_the_busy_gate(env, tmp_path):
    # A deferred bg runner keeps `exporting` latched so the second export —
    # of either kind — must be refused while the first is in flight.
    captured: list[tuple] = []

    def defer(work, done, parent, on_error=None):  # noqa: ARG001
        captured.append((work, done, on_error))

    fake = FakeApp()
    player = FakePlayer()
    controller = SubtitleController(
        fake,
        data_dir=tmp_path,
        player_factory=lambda: player,
        store_factory=lambda root: SubtitleProjectStore(Path(root) / "subtitles"),
        bg_runner=defer,
        unit_executor=SyncOrderedExecutor(),
    )
    controller.importSrt(load_srt(tmp_path))
    render_all(controller, fake, tmp_path)

    target = controller.exportSrt(str(tmp_path / "out"))
    assert target.endswith(".srt")
    assert controller.exporting is True
    assert controller.exportTrack(str(tmp_path / "out")) == ""  # shares _exporting
    assert controller.exportSrt(str(tmp_path / "out")) == ""

    work, done, _on_error = captured.pop()
    done(work())
    assert controller.exporting is False
    assert Path(target).is_file()


def test_export_runner_rejection_resets_exporting(env, tmp_path):
    # A synchronous thread-pool rejection (pool shut down, queue closed) must
    # fail the export like any other failure — exporting unlatched, error
    # surfaced, exportFinished emitted once, "" returned.
    def boom(*_a, **_k):
        raise OSError("pool is shut down")

    fake = FakeApp()
    player = FakePlayer()
    controller = SubtitleController(
        fake,
        data_dir=tmp_path,
        player_factory=lambda: player,
        store_factory=lambda root: SubtitleProjectStore(Path(root) / "subtitles"),
        bg_runner=boom,
        unit_executor=SyncOrderedExecutor(),
    )
    controller.importSrt(load_srt(tmp_path))
    render_all(controller, fake, tmp_path)
    results: list[tuple[str, str]] = []
    controller.exportFinished.connect(lambda path, error: results.append((path, error)))

    assert controller.exportTrack(str(tmp_path / "out")) == ""
    assert controller.exporting is False
    assert controller.errorText != ""
    assert results == [("", "pool is shut down")]

    controller._set_error("")  # noqa: SLF001
    assert controller.exportSrt(str(tmp_path / "out")) == ""
    assert controller.exporting is False
    assert controller.errorText != ""
    assert results == [("", "pool is shut down"), ("", "pool is shut down")]


def make_deferred(tmp_path: Path) -> tuple[SubtitleController, FakeApp, list]:
    """A controller whose bg_runner captures (work, done, on_error) instead of running."""
    captured: list[tuple] = []

    def defer(work, done, parent, on_error=None):  # noqa: ARG001
        captured.append((work, done, on_error))

    fake = FakeApp()
    controller = SubtitleController(
        fake,
        data_dir=tmp_path,
        player_factory=lambda: FakePlayer(),
        store_factory=lambda root: SubtitleProjectStore(Path(root) / "subtitles"),
        bg_runner=defer,
        unit_executor=SyncOrderedExecutor(),
    )
    return controller, fake, captured


def test_clear_discards_an_in_flight_export_result(tmp_path):
    # clear() during an in-flight export must not let the stale callback emit
    # exportFinished — but the latch stays held until that callback lands, so
    # a new export can never race the old one's target.
    controller, fake, captured = make_deferred(tmp_path)
    controller.importSrt(load_srt(tmp_path))
    render_all(controller, fake, tmp_path)
    results: list[tuple[str, str]] = []
    controller.exportFinished.connect(lambda path, error: results.append((path, error)))

    target = controller.exportTrack(str(tmp_path / "out"))
    assert controller.exporting is True
    controller.clear()
    assert controller.loaded is False
    assert controller.exporting is True  # latched until the stale callback lands

    work, done, _on_error = captured.pop()
    done(work())  # the old export completes after the clear — discarded
    assert controller.exporting is False
    assert results == []
    # FR-1.7: the export generation moved on, which cancels the export — so
    # its file does not land either.
    assert not Path(target).exists()

    # A fresh import reuses the on-disk render; a new export runs cleanly.
    controller.importSrt(load_srt(tmp_path))
    assert controller.rendered is True
    target2 = controller.exportSrt(str(tmp_path / "out"))
    assert target2.endswith(".srt")
    assert controller.exporting is True
    work2, done2, _ = captured.pop()
    done2(work2())
    assert controller.exporting is False
    assert results == [(target2, "")]


def test_clear_discards_a_stale_export_failure(tmp_path):
    # A stale export failure must not surface an error or emit exportFinished.
    controller, fake, captured = make_deferred(tmp_path)
    controller.importSrt(load_srt(tmp_path))
    render_all(controller, fake, tmp_path)
    results: list[tuple[str, str]] = []
    controller.exportFinished.connect(lambda path, error: results.append((path, error)))

    controller.exportTrack(str(tmp_path / "out"))
    controller.clear()
    _work, _done, on_error = captured.pop()
    on_error(OSError("late failure"))
    assert controller.exporting is False
    assert controller.errorText == ""
    assert results == []


def test_export_refuses_before_a_render(env, tmp_path):
    controller, _, _ = env
    controller.importSrt(load_srt(tmp_path))
    results: list[tuple[str, str]] = []
    controller.exportFinished.connect(lambda path, error: results.append((path, error)))
    assert controller.exportTrack(str(tmp_path / "out")) == ""
    assert controller.errorText != ""
    # The retimed SRT does not exist until a render lands — refuse, never
    # silently fall back to the source times.
    controller._set_error("")  # noqa: SLF001
    assert controller.exportSrt(str(tmp_path / "out")) == ""
    assert controller.errorText != ""
    assert controller.exporting is False
    assert results == []


# ── lifecycle ────────────────────────────────────────────────────────────────


def test_clear_resets_everything(env, tmp_path):
    controller, fake, player = env
    controller.importSrt(load_srt(tmp_path))
    render_all(controller, fake, tmp_path)
    controller.play()
    controller.clear()
    assert controller.loaded is False
    assert controller.cueCount == 0
    assert controller.rendered is False
    assert controller.durationMs == 0
    assert controller.playerState == "stopped"


# ── ordered dub-unit worker (perf_hardening FR-2.4) ───────────────────────────


class DeferredExecutor:
    """unit_executor double: FIFO queue the test drains explicitly."""

    def __init__(self) -> None:
        self.queue: list[tuple[Any, Any, Any]] = []

    def submit(self, work, on_done, on_error) -> None:
        self.queue.append((work, on_done, on_error))

    def run_next(self) -> None:
        work, on_done, on_error = self.queue.pop(0)
        try:
            result = work()
        except Exception as exc:  # noqa: BLE001 - mirrors OrderedExecutor
            on_error(exc)
            return
        on_done(result)

    def drain(self) -> None:
        while self.queue:
            self.run_next()

    def flush(self, timeout_ms: int = 5000) -> None:
        self.drain()


def deferred_env(tmp_path: Path) -> tuple[SubtitleController, FakeApp, DeferredExecutor]:
    fake = FakeApp()
    executor = DeferredExecutor()
    controller = make_controller(tmp_path, fake, FakePlayer(), unit_executor=executor)
    srt = "".join(
        f"{n + 1}\n00:00:{n * 3:02d},000 --> 00:00:{n * 3 + 2:02d},000\nCâu số {n + 1}\n\n"
        for n in range(4)
    )
    assert controller.importSrt(load_srt(tmp_path, srt)) is True
    return controller, fake, executor


def project_dir(controller: SubtitleController) -> Path:
    return controller._store.project_dir(controller._project.id)


class TestOrderedUnitWorker:
    def test_units_are_placed_on_the_executor_in_submission_order(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        controller, fake, executor = deferred_env(tmp_path)
        placed: list[int] = []
        real_add = SubtitleTrackRenderer.add_clip

        def spy(self, cue_index, clip):
            placed.append(cue_index)
            return real_add(self, cue_index, clip)

        monkeypatch.setattr(SubtitleTrackRenderer, "add_clip", spy)
        controller.render()
        units = len(controller._units)
        assert units >= 2

        complete_next_unit(fake, tmp_path)
        assert placed == [], "placement must not run on the GUI thread"
        assert len(executor.queue) == 1
        # Synthesis of the next unit overlaps the placement of this one.
        assert len(fake.pending) == 1

        while fake.pending:
            complete_next_unit(fake, tmp_path)
        assert placed == []
        executor.drain()
        assert placed == list(range(len(controller._project.cues)))
        assert controller.rendered is True
        assert controller.rendering is False

        # A unit with nothing to speak (a ♪ cue is always its own unit) is
        # placed as silence, in order on the same worker, and never submitted:
        # synthesized alone, its empty clip failed the whole render.
        silent: list[int] = []
        real_silent = SubtitleTrackRenderer.add_silent_cue

        def spy_silent(self, cue_index):
            silent.append(cue_index)
            return real_silent(self, cue_index)

        monkeypatch.setattr(SubtitleTrackRenderer, "add_silent_cue", spy_silent)
        music = (
            "1\n00:00:00,000 --> 00:00:02,000\nXin chào\n\n"
            "2\n00:00:02,000 --> 00:00:03,000\n♪\n\n"
            "3\n00:00:03,000 --> 00:00:05,000\nTạm biệt\n"
        )
        assert controller.importSrt(load_srt(tmp_path, music)) is True
        progress: list[float] = []
        controller.renderProgressChanged.connect(lambda: progress.append(controller.renderProgress))
        fake.pending.clear()
        controller.render()
        complete_next_unit(fake, tmp_path)
        assert [text for _id, text, _l in fake.pending] == ["Tạm biệt"]
        complete_next_unit(fake, tmp_path)
        assert silent == []  # queued behind the first unit's placement
        executor.drain()
        assert silent == [1]
        assert controller.rendered is True
        assert progress == sorted(progress) and progress[-1] == 1.0

        # A unit the engine returns empty (0 samples) is placed as silence
        # too, its artifact released, and the render completes.
        silent.clear()
        assert controller.importSrt(load_srt(tmp_path, SRT)) is True
        controller.render()
        job_id, text, listener = fake.pending.pop(0)
        artifact, artifact_path = make_artifact(job_id, text, tmp_path)
        listener.on_synthesis_terminal(
            JobTerminal(
                job_id=job_id,
                owner="audiobook",
                state="completed",
                value=replace(artifact, samples=0, duration_ms=0),
            )
        )
        complete_next_unit(fake, tmp_path)
        executor.drain()
        assert silent == [0]
        assert not artifact_path.exists()
        assert controller.rendered is True and controller.errorText == ""

        # An SRT with nothing to speak at all is refused up front.
        only_sound = (
            "1\n00:00:01,000 --> 00:00:02,000\n♪\n\n"
            "2\n00:00:03,000 --> 00:00:04,000\n[Tiếng nhạc]\n"
        )
        assert controller.importSrt(load_srt(tmp_path, only_sound)) is True
        controller.render()
        assert fake.pending == [] and executor.queue == []
        assert controller.rendering is False and controller.rendered is False
        assert "lời thoại" in controller.errorText

    def test_finish_writes_the_track_off_thread(self, tmp_path: Path) -> None:
        controller, fake, executor = deferred_env(tmp_path)
        controller.render()
        while fake.pending:
            complete_next_unit(fake, tmp_path)
        track = controller._store.wav_path(controller._project.id)
        while len(executor.queue) > 1:
            executor.run_next()
        assert not track.exists()
        assert controller.rendering is True
        executor.run_next()  # the finish job: promote + timeline + project save
        assert track.is_file()
        assert controller.rendering is False
        assert controller.rendered is True
        assert controller.renderProgress == 1.0

    def test_cancel_drops_queued_units(self, tmp_path: Path, monkeypatch) -> None:
        controller, fake, executor = deferred_env(tmp_path)
        placed: list[int] = []
        real_add = SubtitleTrackRenderer.add_clip
        monkeypatch.setattr(
            SubtitleTrackRenderer,
            "add_clip",
            lambda self, i, clip: placed.append(i) or real_add(self, i, clip),
        )
        controller.render()
        complete_next_unit(fake, tmp_path)
        complete_next_unit(fake, tmp_path)
        assert len(executor.queue) == 2

        controller.cancelRender()
        in_flight = fake.cancelled[-1]
        job_id, _text, listener = fake.pending.pop(0)
        assert job_id == in_flight
        listener.on_synthesis_terminal(
            JobTerminal(job_id=job_id, owner="audiobook", state="cancelled", value=None)
        )
        assert controller.rendering is False
        executor.drain()
        assert placed == []
        assert list(project_dir(controller).glob("*.part.wav")) == []
        assert controller.rendered is False

    def test_a_failed_placement_cancels_the_overlapping_synthesis(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        controller, fake, executor = deferred_env(tmp_path)
        monkeypatch.setattr(
            "vienetts_app.ui.subtitle_controller.read_wav",
            lambda path: (_ for _ in ()).throw(OSError("unreadable")),
        )
        controller.render()
        complete_next_unit(fake, tmp_path)
        next_job = fake.pending[0][0]
        executor.run_next()
        assert controller.rendering is False
        assert "unreadable" in controller.errorText
        assert next_job in fake.cancelled

    def test_units_synthesize_from_the_render_start_snapshot(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # H1: an AI delivery landing mid-render (forced past the busy gate)
        # used to mix new text onto old timings and, with fewer cues, raise
        # IndexError and leave `rendering` stuck.
        from vienetts_app.core.subtitles import Cue

        controller, fake, captured = make_deferred(tmp_path)
        srt = "".join(
            f"{n + 1}\n00:00:{n * 3:02d},000 --> 00:00:{n * 3 + 2:02d},000\nCâu số {n + 1}\n\n"
            for n in range(4)
        )
        assert controller.importSrt(load_srt(tmp_path, srt)) is True
        monkeypatch.setattr(
            "vienetts_app.ui.subtitle_controller.refine_cues_with_llm",
            lambda cues, cfg, **kw: [Cue(1, 0, 2000, "Bản AI 1"), Cue(2, 3000, 5000, "Bản AI 2")],
        )
        submitted: list[str] = []
        real_submit = fake.submit_stream_for_listener
        monkeypatch.setattr(
            fake,
            "submit_stream_for_listener",
            lambda text, voice, listener, **kw: (
                submitted.append(text) or real_submit(text, voice, listener, **kw)
            ),
        )

        assert controller.refineWithLlm("refine_asr", "vi") is True
        work, done, _on_error = captured.pop()
        controller._set_ai_state(False, "", 0.0)  # noqa: SLF001 - force past the gate
        controller.render()
        assert controller.rendering is True
        done(work())  # the AI result lands while unit 1 synthesizes
        assert controller.cueCount == 2
        while fake.pending:
            complete_next_unit(fake, tmp_path)

        assert submitted == [f"Câu số {n}" for n in range(1, 5)]
        assert controller.rendering is False
        assert controller.errorText == ""

    def test_stale_render_generations_are_ignored(self, tmp_path: Path) -> None:
        controller, fake, executor = deferred_env(tmp_path)
        controller.render()
        complete_next_unit(fake, tmp_path)  # placement for render A queued
        controller.cancelRender()
        job_id, _text, listener = fake.pending.pop(0)
        listener.on_synthesis_terminal(
            JobTerminal(job_id=job_id, owner="audiobook", state="cancelled", value=None)
        )

        controller.render()  # render B starts before A's queue drained
        progress_before = controller.renderProgress
        executor.run_next()  # A's stale placement (dropped)
        executor.run_next()  # A's abort
        assert controller.rendering is True
        assert controller.renderProgress == progress_before
        while fake.pending:
            complete_next_unit(fake, tmp_path)
        executor.drain()
        assert controller.rendered is True
        assert controller.errorText == ""


# ── row-level cue model (perf track 6.4) ─────────────────────────────────────


def test_the_cue_model_mirrors_cues_and_diffs_a_reimport_by_row(env, tmp_path):
    from vienetts_app.ui.list_models import DictListModel

    controller, _, _ = env
    model = controller.cueModel
    assert isinstance(model, DictListModel)
    controller.importSrt(load_srt(tmp_path))
    assert model.rows() == controller.cues
    events: list[tuple] = []
    model.dataChanged.connect(lambda tl, br, _r: events.append(("changed", tl.row(), br.row())))
    model.rowsInserted.connect(lambda *_a: events.append(("inserted",)))
    model.rowsRemoved.connect(lambda *_a: events.append(("removed",)))
    model.modelReset.connect(lambda: events.append(("reset",)))

    # Same cue numbering, one cue's text edited on disk: one row changes.
    controller.importSrt(load_srt(tmp_path, SRT.replace("Tạm biệt", "Hẹn gặp lại")))
    assert events == [("changed", 1, 1)]
    assert model.get(1)["text"] == "Hẹn gặp lại"

    events.clear()
    controller.clear()
    assert events == [("removed",)]
    assert model.property("count") == 0


# ── video dubbing, ASR, diarization, multi-speaker & LLM refinement ──────────


def test_video_import_auto_pairs_companion_srt_and_exports_video(env, tmp_path, monkeypatch):
    controller, fake, _ = env
    video = tmp_path / "demo.mp4"
    video.write_bytes(b"fake_mp4")
    srt = tmp_path / "demo.srt"
    srt.write_text(SRT, encoding="utf-8")

    # Auto-discovers demo.srt when importing demo.mp4
    assert controller.importVideo(str(video)) is True
    assert controller.loaded is True
    assert controller.hasVideo is True
    assert controller.videoPath == str(video)
    assert controller.videoName == "demo.mp4"

    controller.videoMixMode = "duck"
    controller.videoBgGainDb = -14.0
    controller.embedSubtitles = False
    assert controller.subtitleEmbedMode == "soft"
    assert controller.softSubtitlesSupported is True
    controller.subtitleEmbedMode = "bogus"
    assert controller.subtitleEmbedMode == "soft"
    controller.subtitleEmbedMode = "burn"
    assert controller.videoMixMode == "duck"
    assert controller.videoBgGainDb == pytest.approx(-14.0)
    assert controller.embedSubtitles is False
    assert controller.subtitleEmbedMode == "burn"

    # Render track
    controller.render()
    while fake.pending:
        complete_next_unit(fake, tmp_path)
    assert controller.rendered is True

    mux_calls: list[dict] = []

    def _fake_mux(vpath, apath, outpath, **kwargs):
        out = Path(outpath)
        out.write_bytes(b"muxed_video")
        mux_calls.append({"video": str(vpath), "audio": str(apath), "out": str(out), **kwargs})
        return out

    monkeypatch.setattr("vienetts_app.ui.subtitle_controller.mux_dubbed_video", _fake_mux)
    out_dir = tmp_path / "exports"
    out_path = controller.exportVideo(str(out_dir))
    assert out_path.endswith(".mp4")
    assert Path(out_path).is_file()
    assert len(mux_calls) == 1
    assert mux_calls[0]["mix_mode"] == "duck"
    assert mux_calls[0]["bg_gain_db"] == pytest.approx(-14.0)
    assert mux_calls[0]["embed_subtitles"] is False
    assert mux_calls[0]["subtitle_mode"] == "burn"
    # Export now reports progress and tags the soft track's language.
    assert mux_calls[0]["progress"] is not None
    assert "subtitle_language" in mux_calls[0]
    assert controller.exportProgress == pytest.approx(0.0)
    assert "duck_spans" in mux_calls[0]
    assert mux_calls[0]["duck_spans"]


def test_reimporting_the_same_srt_keeps_saved_embed_choices(env, tmp_path):
    """Re-importing a file must not reset burn-in / embed-off to the defaults."""
    controller, fake, _ = env
    srt = tmp_path / "lesson.srt"
    srt.write_text(SRT, encoding="utf-8")

    assert controller.importSrt(str(srt)) is True
    controller.embedSubtitles = False
    controller.subtitleEmbedMode = "burn"
    controller.videoMixMode = "duck"
    assert controller.embedSubtitles is False
    assert controller.subtitleEmbedMode == "burn"

    # A fresh controller (app restart) re-importing the same file previously
    # reset these to True/"soft" because the new project was built from the
    # in-memory defaults rather than the stored one.
    fresh = type(controller)(
        controller._app,
        data_dir=controller._data_dir,
        player_factory=lambda: controller._player,
    )
    assert fresh.importSrt(str(srt)) is True
    assert fresh.embedSubtitles is False
    assert fresh.subtitleEmbedMode == "burn"
    assert fresh.videoMixMode == "duck"


def test_video_without_srt_transcribes_diarizes_refines_and_routes_speaker_voices(
    env, tmp_path, monkeypatch
):
    from vienetts_app.core.subtitles import Cue

    controller, fake, _ = env
    video = tmp_path / "raw_interview.mp4"
    video.write_bytes(b"fake_mp4")

    # Importing video with no companion SRT attaches videoPath ready for ASR
    assert controller.importVideo(str(video)) is True
    assert controller.hasVideo is True
    assert controller.loaded is False

    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.run_transcription",
        transcribed(
            [Cue(1, 0, 1500, "Xin chào bạn."), Cue(2, 1700, 3200, "Chào buổi sáng.")],
            labelled=[
                Cue(1, 0, 1500, "Xin chào bạn.", speaker="SPEAKER_01"),
                Cue(2, 1700, 3200, "Chào buổi sáng.", speaker="SPEAKER_02"),
            ],
        ),
    )

    assert controller.transcribeVideo(True) is True
    assert controller.loaded is True
    assert controller.hasSpeakers is True
    assert [s["id"] for s in controller.speakers] == ["SPEAKER_01", "SPEAKER_02"]

    # Assign distinct voices per speaker
    assert controller.setSpeakerVoice("SPEAKER_01", "Binh") is True
    assert controller.setSpeakerVoice("SPEAKER_02", "Tuyen") is True

    # Render routes each cue to its speaker's voice
    submitted_voices: list[str] = []
    orig_submit = fake.submit_stream_for_listener

    def _spy_submit(text, voice, listener, **kwargs):
        submitted_voices.append(str(voice or ""))
        return orig_submit(text, voice, listener, **kwargs)

    monkeypatch.setattr(fake, "submit_stream_for_listener", _spy_submit)
    controller.render()
    while fake.pending:
        complete_next_unit(fake, tmp_path)
    assert controller.rendered is True
    assert submitted_voices == ["Binh", "Tuyen"]

    # LLM refinement updates cues in place while preserving the project workspace ID
    # and persisting the updated managed .srt so re-importing the video reloads it.
    orig_project_id = controller.projectId
    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.refine_cues_with_llm",
        lambda cues, cfg, **kw: [
            Cue(1, 0, 1500, "Xin chào bạn thân mến!", speaker="SPEAKER_01"),
            Cue(2, 1700, 3200, "Chào buổi sáng tốt lành!", speaker="SPEAKER_02"),
        ],
    )
    assert controller.refineWithLlm("refine_asr", "vi") is True
    assert controller.invalidationPending is True
    controller.confirmRenderInvalidation()
    assert controller.projectId == orig_project_id
    assert controller.cues[0]["text"] == "Xin chào bạn thân mến!"
    assert controller.cues[1]["text"] == "Chào buổi sáng tốt lành!"
    assert Path(controller.sourcePath).is_file()
    assert "fitRate" in controller.cues[0]
    assert "overflowMs" in controller.cues[0]
    assert "overBudget" in controller.cues[0]

    # Inline cue editing via updateCue updates text and speaker in place.
    # No live render remains (the LLM step above already confirmed its loss),
    # so cue edits apply immediately and need no second dialog.
    assert controller.updateCue(0, "Xin chào chỉnh sửa trực tiếp!", "SPEAKER_02") is True
    assert controller.invalidationPending is False
    assert controller.cues[0]["text"] == "Xin chào chỉnh sửa trực tiếp!"
    assert controller.cues[0]["speaker"] == "SPEAKER_02"
    assert controller.updateCue(0, "Xin chào bạn thân mến!", "SPEAKER_01") is True

    # playSpeakerSample extracts and plays a speaker reference clip from the video
    import numpy as np

    from vienetts_app.core.audio import write_wav_file

    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.extract_speaker_reference_wav",
        lambda vpath, cues, spk, out, **kw: (
            write_wav_file(np.zeros(48_000, dtype=np.float32), Path(out), sample_rate=48_000),
            "Xin chào bạn thân mến!",
        ),
    )
    assert controller.playSpeakerSample("SPEAKER_01") is True

    # Invalid per-speaker voice is rejected upfront by render()
    assert controller.setSpeakerVoice("SPEAKER_01", "InvalidVoice") is True
    from vienetts_app.core.synthesis_context import SynthesisContext

    valid_ctx = SynthesisContext(profile="vieneu", model_revision="rev1", voice_id="Binh")
    fake.submission_context_for = lambda voice="", report=True: (
        None if voice == "InvalidVoice" else valid_ctx
    )
    controller.render()
    assert controller.rendering is False
    assert controller.errorText != ""

    # cloneSpeakerFromVideo rejects profiles that do not support cloning or lack consent
    fake.supportsCloning = False
    assert controller.cloneSpeakerFromVideo("SPEAKER_01", "Clone1") is False
    assert "nhân bản" in controller.errorText.lower()


# ── diarization hardening (device plumbing, fallback warnings, label remap) ──


def test_diarization_config_reads_device_and_threads_settings(env):
    controller, fake, _ = env
    fake.diarizationBackend = "nemotron_gguf"
    fake.diarizationModel = "nemotron-3-diarization-bf16"
    fake.diarizationMaxSpeakers = 4
    fake.diarizationDevice = "metal"
    fake.diarizationThreads = 6
    cfg = controller._diarization_config()  # noqa: SLF001
    assert cfg.backend == "nemotron_gguf"
    assert cfg.model == "nemotron-3-diarization-bf16"
    assert cfg.max_speakers == 4
    assert cfg.device == "metal"
    assert cfg.threads == 6


def test_transcription_runs_the_pipeline_and_surfaces_its_notes(qcoreapp, tmp_path, monkeypatch):
    # FR-4.1/4.2 wiring: ASR, speaker turns and labels come from ONE
    # run_transcription call (shared 16 kHz prep, turn splitting). Its notes
    # — a cloud run, a diarization failure kept as a note on the ASR cues —
    # surface as one warning, after the app's backend note.
    from vienetts_app.core.subtitles import Cue

    runner = _DeferredRunner()
    fake = FakeApp()
    fake.asrBackendNote = "faster-whisper không có — đang dùng chế độ Tự động."
    controller = make_controller(tmp_path, fake, FakePlayer(), bg_runner=runner)
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"fake_mp4")
    assert controller.importVideo(str(video)) is True
    calls: list[dict[str, Any]] = []
    notes = ["Đã dùng Whisper API (đám mây)", "Không phân tách được người nói: model exploded"]

    def run(media, asr_cfg, diar_cfg=None, **kw):
        calls.append({"media": media, "asr": asr_cfg, "diar": diar_cfg, **kw})
        return TranscriptionResult(
            [Cue(1, 0, 1500, "Xin chào."), Cue(2, 1700, 3200, "Hết.")], notes
        )

    monkeypatch.setattr("vienetts_app.ui.subtitle_controller.run_transcription", run)
    released: list[str] = []
    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.release_idle_asr_models",
        lambda: released.append("idle"),
    )
    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.release_asr_models",
        lambda: released.append("all"),
    )

    def start(with_diarization: bool) -> tuple[Any, Any]:
        assert controller.transcribeVideo(with_diarization) is True
        work, done, _ = runner.calls[-1]
        return work, done

    unused = _media_entry(tmp_path / "subtitles" / "_media_cache", "unused", 10, 45)
    in_use = controller._media_cache_dir(video)  # noqa: SLF001
    _media_entry(in_use.parent, in_use.name, 10, 45)
    work, done = start(True)
    done(work())
    # FR-6.3: each transcription is followed by a sweep on a worker, which
    # spares the active project's entry however old it is.
    sweep, sweep_done, _ = runner.calls[-1]
    assert unused.is_dir()
    sweep_done(_on_thread(sweep))
    assert not unused.exists() and in_use.is_dir()
    call = calls[-1]
    assert (call["media"], call["asr"], call["diar"]) == (
        video,
        controller._asr_config(),  # noqa: SLF001
        controller._diarization_config(),  # noqa: SLF001
    )
    assert call["work_dir"] == controller._media_cache_dir(video)  # noqa: SLF001
    assert (call["data_dir"], call["parallel"]) == (tmp_path, False)
    assert controller.loaded is True and controller.cueCount == 2
    assert controller.errorText == "\n".join([fake.asrBackendNote, *notes])
    assert controller.noticeLevel == "warning"

    # A cancel, or a newer op's generation, reaches the running pipeline.
    fake.asrBackendNote = ""
    work_a, done_a = start(False)
    result_a = work_a()
    check_a = calls[-1]["cancel_check"]
    assert calls[-1]["diar"] is None and check_a() is False
    controller.cancelAiTask()
    assert check_a() is True
    work_b, done_b = start(False)  # resets the cancel flag: the generation still differs
    result_b = work_b()
    assert check_a() is True and calls[-1]["cancel_check"]() is False
    done_a(result_a)
    assert controller.aiBusy is True  # dropped: op B owns the state
    done_b(result_b)
    assert controller.aiBusy is False and controller.errorText == "\n".join(notes)

    # FR-3.7: an idle Whisper model is released by a periodic tick on a
    # worker (never the GUI thread), and every model at shutdown.
    timer = controller._asr_idle_timer  # noqa: SLF001
    assert timer.isActive() is True
    queued = len(runner.calls)
    timer.timeout.emit()
    assert released == [] and len(runner.calls) == queued + 1
    tick, tick_done, _ = runner.calls[-1]
    tick_done(tick())
    assert released == ["idle"]
    controller.shutdown()
    assert timer.isActive() is False and released == ["idle", "all"]


def test_pipeline_notes_are_shown_in_the_ui_language(qcoreapp, tmp_path, monkeypatch):
    # The pipeline's diarization notes are Vietnamese literals; the known ones
    # reach the UI through self.tr (the error text kept), others unchanged.
    from vienetts_app.core.subtitles import Cue

    runner = _DeferredRunner()
    controller = make_controller(tmp_path, FakeApp(), FakePlayer(), bg_runner=runner)
    monkeypatch.setattr(controller, "tr", lambda text: f"[en] {text}")
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"fake_mp4")
    assert controller.importVideo(str(video)) is True
    notes = [
        DIAR_FAILED_NOTE.format(error="model exploded"),
        DIAR_CANCELLED_NOTE,
        ACOUSTIC_FALLBACK_NOTE,
        "Đã dùng Whisper API (đám mây)",
    ]
    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.run_transcription",
        transcribed([Cue(1, 0, 1500, "Xin chào.")], *notes),
    )
    assert controller.transcribeVideo(True) is True
    work, done, _ = runner.calls[-1]
    done(work())
    assert controller.errorText.splitlines() == [
        "[en] " + DIAR_FAILED_NOTE.format(error="model exploded"),
        "[en] " + DIAR_CANCELLED_NOTE,
        "[en] " + ACOUSTIC_FALLBACK_NOTE,
        "Đã dùng Whisper API (đám mây)",
    ]


def test_rediarization_remaps_labels_to_existing_speaker_voices(env, tmp_path, monkeypatch):
    from vienetts_app.core.subtitles import Cue

    controller, _, _ = env
    video = tmp_path / "interview.mp4"
    video.write_bytes(b"fake_mp4")
    assert controller.importVideo(str(video)) is True
    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.run_transcription",
        transcribed(
            [Cue(1, 0, 1500, "Một."), Cue(2, 1700, 3200, "Hai.")],
            labelled=[
                Cue(1, 0, 1500, "Một.", speaker="SPEAKER_01"),
                Cue(2, 1700, 3200, "Hai.", speaker="SPEAKER_02"),
            ],
        ),
    )
    assert controller.transcribeVideo(True) is True
    assert controller.setSpeakerVoice("SPEAKER_01", "Binh") is True
    assert controller.setSpeakerVoice("SPEAKER_02", "Tuyen") is True

    # A re-run that swaps labels must not move the cloned voices.
    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.diarize_cues",
        lambda path, cues, cfg=None, **kw: [
            Cue(1, 0, 1500, "Một.", speaker="SPEAKER_02"),
            Cue(2, 1700, 3200, "Hai.", speaker="SPEAKER_01"),
        ],
    )
    assert controller.runDiarization() is True
    assert [c["speaker"] for c in controller.cues] == ["SPEAKER_01", "SPEAKER_02"]
    by_id = {s["id"]: s["voice"] for s in controller.speakers}
    assert by_id == {"SPEAKER_01": "Binh", "SPEAKER_02": "Tuyen"}


def test_export_srt_can_include_speaker_labels(env, tmp_path):
    controller, fake, _ = env
    assert controller.importSrt(load_srt(tmp_path)) is True
    controller._update_project_cues(  # noqa: SLF001
        [
            replace(controller._project.cues[0], speaker="SPEAKER_01"),  # noqa: SLF001
            replace(controller._project.cues[1], speaker="SPEAKER_02"),  # noqa: SLF001
        ]
    )
    render_all(controller, fake, tmp_path)
    out_dir = tmp_path / "srt_out"
    assert controller.exportSrt(str(out_dir)) != ""
    target = next(out_dir.glob("*.srt"))
    assert "[SPEAKER_01]" not in target.read_text(encoding="utf-8")

    controller._app.subtitleExportSpeakerLabels = True
    assert controller.exportSrt(str(out_dir)) != ""
    exported = sorted(out_dir.glob("*.srt"), key=lambda p: p.stat().st_mtime)
    assert "[SPEAKER_01]" in exported[-1].read_text(encoding="utf-8")


def test_asr_config_reads_device_and_threads_settings(env, monkeypatch):
    controller, fake, _ = env
    fake.asrBackend = "whisper_cpp"
    fake.asrModel = "small"
    fake.asrLanguage = "vi"
    fake.asrDevice = "cuda"
    fake.asrThreads = 10
    cfg = controller._asr_config()  # noqa: SLF001
    assert cfg.backend == "whisper_cpp"
    assert cfg.model == "small"
    assert cfg.language == "vi"
    assert cfg.device == "cuda"
    assert cfg.threads == 10
    assert cfg.cloud_fallback is False  # FR-3.4: opt-in only
    # Automatic threads resolve to the host default (FR-3.7); the cloud
    # fallback follows the user's opt-in.
    monkeypatch.setattr("vienetts_app.ui.subtitle_controller._auto_asr_threads", lambda: 3)
    fake.asrThreads = 0
    fake.asrCloudFallback = True
    cfg = controller._asr_config()  # noqa: SLF001
    assert (cfg.threads, cfg.cloud_fallback) == (3, True)
    fake.llmBaseUrl = "http://llm/v1"
    fake.llmApiKey = "llm-secret"
    fake.asrApiBaseUrl = "http://asr/v1"
    fake.asrApiKey = ""
    fake.asrApiModel = "whisper-custom"
    cfg = controller._asr_config()  # noqa: SLF001
    assert (cfg.api_base_url, cfg.api_key, cfg.api_model) == ("http://asr/v1", "", "whisper-custom")
    fake.asrApiBaseUrl = ""
    fake.asrBackend = "openai_api"
    cfg = controller._asr_config()  # noqa: SLF001
    assert (cfg.api_base_url, cfg.api_key) == ("http://llm/v1", "llm-secret")
    fake.asrBackend = "auto"
    cfg = controller._asr_config()  # noqa: SLF001
    assert (cfg.api_base_url, cfg.api_key) == ("", "")
    fake.asrBackend = "openai_api"
    fake.llmBaseUrl = ""
    cfg = controller._asr_config()  # noqa: SLF001
    assert (cfg.api_base_url, cfg.api_key) == ("", "")
    fake.llmBaseUrl = "http://llm/v1"
    fake.llmModel = "default"
    fake.llmModelTranslate = "translation"
    fake.llmConcurrency = 3
    fake.llmTimeoutS = 180
    fake.llmBatchSize = 40
    cfg = controller._llm_config("translate_dub")  # noqa: SLF001
    assert (cfg.model, cfg.concurrency, cfg.timeout_s, cfg.batch_size) == (
        "translation",
        3,
        180,
        40,
    )
    assert controller._llm_config("refine_asr").model == "default"  # noqa: SLF001


def test_managed_srt_disambiguates_same_stem_videos_and_persists_media_cache_and_asr_notes(
    env, tmp_path, monkeypatch
):
    from vienetts_app.core.subtitles import Cue

    controller, _, _ = env
    dir_a = tmp_path / "dir_a"
    dir_b = tmp_path / "dir_b"
    dir_a.mkdir()
    dir_b.mkdir()
    video_a = dir_a / "clip.mp4"
    video_b = dir_b / "clip.mp4"
    video_a.write_bytes(b"video_a")
    video_b.write_bytes(b"video_b")

    seen_work_dirs: list[Path] = []

    def _fake_transcribe(path, cfg, diar_cfg=None, *, work_dir=None, **kw):
        assert work_dir is not None
        seen_work_dirs.append(Path(work_dir))
        Path(work_dir).mkdir(parents=True, exist_ok=True)  # the pipeline creates it
        (Path(work_dir) / "source_16k.wav").write_bytes(b"cached_wav")
        return TranscriptionResult(
            [Cue(1, 0, 1500, f"Phụ đề từ {Path(path).parent.name}")],
            ["ASR dự phòng đã kích hoạt."],
        )

    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.run_transcription",
        _fake_transcribe,
    )

    assert controller.importVideo(str(video_a)) is True
    assert controller.transcribeVideo(False) is True
    assert controller.loaded is True
    assert controller.title == "clip"
    assert controller.cues[0]["text"] == "Phụ đề từ dir_a"
    assert "ASR dự phòng" in controller.errorText
    srt_a = Path(controller.sourcePath)
    assert srt_a.is_file()
    # Persistent media cache dir survives after transcribeVideo finishes
    assert len(seen_work_dirs) == 1
    assert (seen_work_dirs[0] / "source_16k.wav").is_file()

    # Importing dir_b/clip.mp4 (same stem, different directory) must NOT auto-load dir_a's SRT
    controller.clear()
    assert controller.importVideo(str(video_b)) is True
    assert controller.loaded is False

    assert controller.transcribeVideo(False) is True
    assert controller.loaded is True
    assert controller.title == "clip"
    assert controller.cues[0]["text"] == "Phụ đề từ dir_b"
    srt_b = Path(controller.sourcePath)
    assert srt_b.is_file()
    assert srt_a != srt_b


# ── data safety: destroy-confirmation, staleness, lifecycle (FR-1.1…1.6) ────


def _rendered_controller(tmp_path: Path) -> tuple[SubtitleController, FakeApp, FakePlayer]:
    fake = FakeApp()
    player = FakePlayer()
    controller = make_controller(tmp_path, fake, player)
    controller.importSrt(load_srt(tmp_path))
    render_all(controller, fake, tmp_path)
    assert controller.rendered is True
    fake.pending.clear()
    return controller, fake, player


_POLICY_KNOBS = [
    ("mode", "transcript", "mode"),
    ("rateCap", 1.2, "rate_cap"),
    ("maxGapMs", 800, "max_gap_ms"),
    ("offsetMs", 250, "offset_ms"),
    ("mergeSentences", True, "merge_sentences"),
    ("voice", "voice-b", "voice"),
]


@pytest.mark.parametrize(("knob", "value", "reason"), _POLICY_KNOBS)
def test_policy_knob_stages_a_named_confirmation(tmp_path, knob, value, reason):
    controller, _, _ = _rendered_controller(tmp_path)
    events: list[tuple[str, str]] = []
    controller.renderInvalidationRequested.connect(lambda r, m: events.append((r, m)))
    duration = controller.durationMs
    stats = controller.statsSummary

    setattr(controller, knob, value)
    assert controller.rendered is True  # nothing is lost before the decision
    assert controller.invalidationPending is True
    assert controller.invalidationReason == reason
    assert events == [(reason, controller.invalidationMessage)]
    assert "lồng tiếng" in controller.invalidationMessage
    assert "căn chỉnh" in controller.invalidationMessage

    # Reject: the control snaps back to its unchanged value.
    controller.rejectRenderInvalidation()
    assert controller.invalidationPending is False
    assert controller.rendered is True
    assert controller.durationMs == duration
    assert controller.renderStale is False

    # Confirm: the change applies and the render is stale, not silently gone.
    setattr(controller, knob, value)
    controller.confirmRenderInvalidation()
    assert controller.rendered is False
    assert controller.renderStale is True
    assert controller.statsSummary == stats
    assert controller.canExportTrack is False
    assert controller.canExportSrt is False
    assert controller.exportBlockedReason != ""
    assert controller.exportTrack(str(tmp_path / "out")) == ""


def test_a_second_knob_replaces_the_staged_change(tmp_path):
    controller, _, _ = _rendered_controller(tmp_path)
    controller.rateCap = 1.2
    assert controller.invalidationReason == "rate_cap"
    controller.offsetMs = 300
    assert controller.invalidationReason == "offset_ms"
    controller.rejectRenderInvalidation()
    # Rejecting the later change leaves both knobs untouched.
    assert controller.rateCap == 1.5
    assert controller.offsetMs == 0
    assert controller.rendered is True


def test_a_knob_change_while_rendering_waits_for_the_render_to_end(env, tmp_path):
    # FR-1.1: a knob moved mid-render is staged, never applied under the
    # running render (the track that lands would not match the knobs).
    controller, fake, _ = env
    controller.importSrt(load_srt(tmp_path))
    controller.render()
    assert controller.rendering is True
    assert controller._can_apply_now() is False  # noqa: SLF001
    controller.rateCap = 1.2
    assert controller.invalidationReason == "rate_cap"
    assert controller.rateCap == 1.5
    controller.rejectRenderInvalidation()
    assert controller.rateCap == 1.5

    # Confirmed mid-render: deferred until the render finishes.
    controller.rateCap = 1.2
    controller.confirmRenderInvalidation()
    assert controller.invalidationPending is False
    assert controller.rateCap == 1.5
    while fake.pending:
        complete_next_unit(fake, tmp_path)
    assert controller.rendering is False
    assert controller.rateCap == 1.2
    assert controller._project.policy.rate_cap == 1.2  # noqa: SLF001
    assert controller.rendered is False and controller.renderStale is True

    # A cancelled render releases its deferred change too.
    controller.render()
    controller.offsetMs = 250
    controller.confirmRenderInvalidation()
    assert controller.offsetMs == 0
    controller.cancelRender()
    job_id, _text, listener = fake.pending.pop(0)
    listener.on_synthesis_terminal(
        JobTerminal(job_id=job_id, owner="audiobook", state="cancelled", value=None)
    )
    assert controller.rendering is False
    assert controller.offsetMs == 250
    assert controller._project.policy.offset_ms == 250  # noqa: SLF001


def test_rerender_clears_staleness_and_restores_exports(tmp_path):
    controller, fake, _ = _rendered_controller(tmp_path)
    controller.rateCap = 1.2
    controller.confirmRenderInvalidation()
    assert controller.renderStale is True
    controller.render()
    while fake.pending:
        complete_next_unit(fake, tmp_path)
    assert controller.rendered is True
    assert controller.renderStale is False
    assert controller.renderStaleReason == ""
    assert controller.canExportTrack is True
    assert controller.canExportSrt is True
    assert controller.exportBlockedReason == ""


def test_cue_edit_requires_confirmation_only_while_rendered(tmp_path):
    controller, _, _ = _rendered_controller(tmp_path)
    original = controller.cues[0]["text"]
    stats = controller.statsSummary
    assert controller.updateCue(0, "Sửa lời thoại", "") is True
    assert controller.rendered is True
    assert controller.invalidationReason == "cue_edit"
    assert controller.cues[0]["text"] == original
    controller.rejectRenderInvalidation()
    assert controller.cues[0]["text"] == original

    assert controller.updateCue(0, "Sửa lời thoại", "") is True
    controller.confirmRenderInvalidation()
    assert controller.cues[0]["text"] == "Sửa lời thoại"
    assert controller.renderStale is True
    assert controller.statsSummary == stats

    # A further edit has no live render to protect: it applies immediately.
    assert controller.updateCue(0, "Sửa lần nữa", "") is True
    assert controller.invalidationPending is False
    assert controller.cues[0]["text"] == "Sửa lần nữa"


def test_a_speaker_voice_change_on_a_live_render_is_staged(env, tmp_path):
    # FR-1.6 / L1: setSpeakerVoice used to drop a live render with no prompt
    # and no stale reason; it now goes through the same staged confirmation.
    controller, fake, _ = env
    controller.importSrt(load_srt(tmp_path))
    assert controller.updateCue(0, "Xin chào", "SPEAKER_01") is True  # unrendered: at once
    render_all(controller, fake, tmp_path)
    assert controller.rendered is True
    reasons: list[str] = []
    controller.renderInvalidationRequested.connect(lambda reason, _m: reasons.append(reason))

    # No cue speaks as SPEAKER_09: the render does not depend on its voice.
    assert controller.setSpeakerVoice("SPEAKER_09", "voice-c") is True
    assert controller.invalidationPending is False and controller.rendered is True

    assert controller.setSpeakerVoice("SPEAKER_01", "voice-b") is True
    assert controller.invalidationReason == "speaker_voice" and reasons == ["speaker_voice"]
    assert "lồng tiếng" in controller.invalidationMessage
    assert controller.rendered is True and controller.speakers[0]["voice"] == "voice-a"
    controller.rejectRenderInvalidation()
    assert controller.rendered is True and controller.speakers[0]["voice"] == "voice-a"

    assert controller.setSpeakerVoice("SPEAKER_01", "voice-b") is True
    controller.confirmRenderInvalidation()
    assert controller.speakers[0]["voice"] == "voice-b"
    assert controller.rendered is False and controller.renderStale is True
    assert controller._stale_reason == "speaker_voice"  # noqa: SLF001
    assert "giọng người nói" in controller.renderStaleReason
    # The render made before the voice was mapped is not re-adopted: the
    # context-less (legacy) comparison covers the used speakers' voices.
    controller.render()
    assert [text for _id, text, _l in fake.pending] == ["Xin chào"]


def test_diarization_and_llm_refinement_are_gated(tmp_path, monkeypatch):
    from vienetts_app.core.subtitles import Cue

    controller, _, _ = _rendered_controller(tmp_path)
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"fake_mp4")
    controller._project = replace(controller._project, video_path=str(video))  # noqa: SLF001
    controller._pending_video_path = str(video)  # noqa: SLF001

    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.diarize_cues",
        lambda path, cues, cfg=None, **kw: [replace(cue, speaker="SPEAKER_01") for cue in cues],
    )
    assert controller.runDiarization() is True
    assert controller.invalidationReason == "diarization"
    assert controller.hasSpeakers is False  # not started before confirmation
    controller.rejectRenderInvalidation()
    assert controller.hasSpeakers is False

    assert controller.runDiarization() is True
    controller.confirmRenderInvalidation()
    assert controller.hasSpeakers is True
    assert [s["id"] for s in controller.speakers] == ["SPEAKER_01"]

    # The confirmed diarization invalidated the render, so the LLM step below
    # applies immediately — the gate protects a LIVE render, not a stale one.
    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.refine_cues_with_llm",
        lambda cues, cfg, **kw: [Cue(1, 0, 2000, "Đã làm sạch", speaker="SPEAKER_01")],
    )
    assert controller.refineWithLlm("refine_asr", "vi") is True
    assert controller.cues[0]["text"] == "Đã làm sạch"


def test_a_render_commit_keeps_what_changed_meanwhile(env, tmp_path, monkeypatch):
    # FR-1.6: renderer.finish() commits the render's start snapshot. It now
    # lands merged onto the latest stored project, so nothing saved during
    # the render is overwritten.
    controller, fake, _ = env
    store = controller._store  # noqa: SLF001
    path = load_srt(tmp_path)
    assert controller.importSrt(path)
    controller.setLlmGlossary("saved")
    assert controller.importSrt(path)
    assert controller.llmGlossary == "saved"
    pid = controller.projectId

    # Settings changed mid-render land, and the render stays live.
    controller.render()
    assert controller.rendering
    controller.setLlmGlossary("edited while rendering")
    controller.videoMixMode = "replace"
    controller.embedSubtitles = False
    while fake.pending:
        complete_next_unit(fake, tmp_path)
    assert controller.rendered
    stored = store.require(pid)
    assert stored.rendered
    assert controller.llmGlossary == stored.llm_glossary == "edited while rendering"
    assert controller.videoMixMode == stored.video_mix_mode == "replace"
    assert controller.embedSubtitles is stored.embed_subtitles is False

    # A cue edit saved while finish() commits survives it, and outdates the
    # track that commit promotes.
    real_finish = SubtitleTrackRenderer.finish

    def finish_after_an_edit(renderer: SubtitleTrackRenderer) -> Any:
        latest = store.require(pid)
        edited = (replace(latest.cues[0], text="Sửa trong lúc ghi"), *latest.cues[1:])
        store.save(replace(latest, cues=edited))
        return real_finish(renderer)

    monkeypatch.setattr(SubtitleTrackRenderer, "finish", finish_after_an_edit)
    controller.rateCap = 1.2
    controller.confirmRenderInvalidation()
    render_all(controller, fake, tmp_path)
    assert controller.rendering is False
    stored = store.require(pid)
    assert stored.cues[0].text == controller.cues[0]["text"] == "Sửa trong lúc ghi"
    assert stored.rendered is False and stored.video_mix_mode == "replace"
    assert controller.rendered is False and controller.renderStale is True
    assert controller._stale_reason == "cue_edit"  # noqa: SLF001
    assert controller.statsSummary != ""  # the outdated render's stats stay visible


def test_field_setters_and_reimport_keep_the_stored_render(tmp_path):
    # FR-1.6 / L1: field setters (and a re-import) used to save the in-memory
    # project over project.json. With a knob moved away that project is
    # unrendered, so the render was lost and moving the knob back
    # re-synthesized everything. They now change only their field on disk.
    controller, fake, _ = _rendered_controller(tmp_path)
    store = controller._store  # noqa: SLF001
    pid = controller.projectId
    rendered = store.require(pid)

    def assert_render_kept() -> None:
        stored = store.require(pid)
        assert stored.rendered
        assert (stored.stats, stored.adjusted, stored.fingerprint, stored.render_id) == (
            rendered.stats,
            rendered.adjusted,
            rendered.fingerprint,
            rendered.render_id,
        )

    controller.rateCap = 1.2
    controller.confirmRenderInvalidation()
    assert controller.rendered is False
    controller.videoMixMode = "replace"
    controller.videoBgGainDb = -10.0
    controller.embedSubtitles = False
    controller.subtitleEmbedMode = "burn"
    controller.setLlmGlossary("Thuật ngữ")
    assert_render_kept()
    stored = store.require(pid)
    assert (
        stored.video_mix_mode,
        stored.video_bg_gain_db,
        stored.embed_subtitles,
        stored.subtitle_embed_mode,
        stored.llm_glossary,
    ) == ("replace", -10.0, False, "burn", "Thuật ngữ")

    controller.rateCap = 1.5  # back: the render is adopted, nothing synthesized
    assert controller.rendered is True
    controller.render()
    assert fake.pending == [] and controller.rendering is False
    assert controller.videoMixMode == "replace" and controller.llmGlossary == "Thuật ngữ"

    # Re-importing the same file under other knobs keeps it as well.
    controller.rateCap = 1.2
    controller.confirmRenderInvalidation()
    assert controller.importSrt(load_srt(tmp_path)) is True
    assert controller.rendered is False
    assert_render_kept()
    controller.rateCap = 1.5
    assert controller.rendered is True
    controller.render()
    assert fake.pending == []


def test_llm_refinement_on_a_live_render_is_gated(tmp_path, monkeypatch):
    from vienetts_app.core.subtitles import Cue

    controller, _, _ = _rendered_controller(tmp_path)
    signal_count = []
    controller.llmGlossaryChanged.connect(lambda: signal_count.append(1))
    controller.setLlmGlossary("term: name")
    assert controller.llmGlossary == "term: name"
    assert controller.invalidationPending is False and controller.rendered is True
    assert controller._store.require(controller.projectId).llm_glossary == "term: name"  # noqa: SLF001
    assert signal_count

    def refine(cues, cfg, **kw):
        assert kw["glossary"] == "term: name"
        kw["notes"].append("partial result")
        return [Cue(1, 0, 2000, "Đã làm sạch")]

    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.refine_cues_with_llm",
        refine,
    )
    assert controller.refineWithLlm("refine_asr", "vi") is True
    assert controller.invalidationReason == "llm"
    assert controller.cues[0]["text"] == "Xin chào"
    controller.confirmRenderInvalidation()
    assert controller.cues[0]["text"] == "Đã làm sạch"
    assert controller.llmGlossary == "term: name"
    assert "partial result" in controller.hintText


def test_re_asr_is_gated_when_cue_edits_would_be_lost(tmp_path, monkeypatch):
    from vienetts_app.core.subtitles import Cue

    fake = FakeApp()
    player = FakePlayer()
    controller = make_controller(tmp_path, fake, player)
    video = tmp_path / "interview.mp4"
    video.write_bytes(b"fake_mp4")
    calls: list[str] = []

    def _transcribe(path, cfg, diar_cfg=None, **kw):
        calls.append(str(path))
        return TranscriptionResult([Cue(1, 0, 1500, "Bản gốc từ ASR")], [])

    monkeypatch.setattr("vienetts_app.ui.subtitle_controller.run_transcription", _transcribe)
    assert controller.importVideo(str(video)) is True
    assert controller.transcribeVideo(False) is True
    assert calls and controller.loaded is True

    assert controller.updateCue(0, "Người dùng đã sửa", "") is True
    assert controller.cues[0]["text"] == "Người dùng đã sửa"
    before = len(calls)
    assert controller.transcribeVideo(False) is True
    assert controller.invalidationReason == "asr"
    assert len(calls) == before  # confirmation first
    controller.rejectRenderInvalidation()
    assert controller.cues[0]["text"] == "Người dùng đã sửa"
    assert controller.transcribeVideo(False) is True
    controller.confirmRenderInvalidation()
    assert len(calls) == before + 1


def test_project_lifecycle_lists_opens_and_removes(tmp_path, monkeypatch):
    from vienetts_app.core.subtitles import Cue

    controller, fake, _ = _rendered_controller(tmp_path)
    # FR-1.9 / L2: the picker list refreshes once per list-affecting change.
    changes: list[int] = []
    controller.projectsChanged.connect(lambda: changes.append(1))

    def changed_once() -> bool:
        count = len(changes)
        changes.clear()
        return count == 1

    first_id = controller.projectId
    second = tmp_path / "other.srt"
    second.write_text("1\n00:00:01,000 --> 00:00:02,000\nNội dung khác\n", encoding="utf-8")
    assert controller.importSrt(str(second)) is True
    assert changed_once()  # import
    render_all(controller, fake, tmp_path)
    assert controller.rendered is True and changed_once()  # render complete
    assert controller.projectId != first_id
    # FR-6.1: the rows are served from a cache keyed by each project.json's
    # stat — every read after the scan parses nothing.
    parsed: list[str] = []
    store_load = controller._store.load  # noqa: SLF001
    monkeypatch.setattr(
        controller._store,  # noqa: SLF001
        "load",
        lambda pid: parsed.append(pid) or store_load(pid),
    )
    assert controller.projectCount == 2
    rows = controller.projects
    ids = {row["id"] for row in rows}
    assert ids == {first_id, controller.projectId}
    assert {row["title"] for row in rows} == {"movie", "other"}
    assert all(row["modifiedAt"] for row in rows)
    assert controller.projects == rows and controller.projectCount == 2
    assert sorted(parsed) == sorted(ids)
    assert changes == []  # a scan answered inline is not announced again

    assert controller.openProject(first_id) is True
    assert controller.projectId == first_id
    assert controller.title == "movie"
    assert controller.rendered is True
    assert controller.cueCount == 2
    assert controller.mode == "dub"
    assert changed_once()

    # FR-5.7: a commit cut off between project.json and the track promote
    # leaves another render's track.wav — the project reopens unrendered.
    project_json = controller._store.project_path(first_id)  # noqa: SLF001
    payload = json.loads(project_json.read_text(encoding="utf-8"))
    payload["renderId"] = "f" * 32
    project_json.write_text(json.dumps(payload), encoding="utf-8")
    assert controller.openProject(first_id) is True
    assert controller.rendered is False and controller.durationMs == 0
    assert controller.canExportTrack is False
    # The picker row agrees with openProject, and only the rewritten
    # project.json is parsed again.
    parsed.clear()
    row = next(row for row in controller.projects if row["id"] == first_id)
    assert (row["rendered"], row["durationMs"], row["active"]) == (False, 0, True)
    assert parsed == [first_id]
    changes.clear()

    assert controller.removeProject(first_id) is True
    assert changed_once()  # remove (it clears the active project too)
    assert controller.loaded is False
    assert controller.projectCount == 1
    assert [row["id"] for row in controller.projects] == [controller.projects[0]["id"]]

    assert controller.openProject(controller.projects[0]["id"]) is True
    changes.clear()
    controller.clear()
    assert changed_once()  # clear
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"fake_mp4")
    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.run_transcription",
        transcribed([Cue(1, 0, 1500, "Từ ASR")]),
    )
    assert controller.importVideo(str(video)) is True
    assert controller.transcribeVideo(False) is True
    assert controller.loaded is True and changed_once()  # ASR import


def test_removing_the_active_project_returns_to_the_empty_state(tmp_path):
    controller, _, _ = _rendered_controller(tmp_path)
    pid = controller.projectId
    assert controller.removeProject(pid) is True
    assert controller.loaded is False
    assert controller.cueCount == 0
    assert controller.projectCount == 0


OTHER_SRT = "1\n00:00:01,000 --> 00:00:02,000\nNội dung khác\n"


def test_switching_projects_cancels_ai_and_drops_its_late_results(tmp_path, monkeypatch):
    # FR-1.4 / M2: an import cancels the running AI op, and whatever it
    # delivers afterwards is dropped (generation + captured project_id).
    from vienetts_app.core.subtitles import Cue

    runner = _DeferredRunner()
    controller = make_controller(tmp_path, FakeApp(), FakePlayer(), bg_runner=runner)
    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.run_transcription",
        transcribed([Cue(1, 0, 1500, "Từ ASR")]),
    )
    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.diarize_cues",
        lambda path, cues, cfg=None, **kw: [replace(c, speaker="SPEAKER_09") for c in cues],
    )
    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.refine_cues_with_llm",
        lambda cues, cfg, **kw: [replace(c, text="Từ LLM") for c in cues],
    )

    def finish_last() -> None:
        work, done, _ = runner.calls[-1]
        done(work())

    video = tmp_path / "talk.mp4"
    video.write_bytes(b"fake_mp4")
    other = tmp_path / "other.srt"
    other.write_text(OTHER_SRT, encoding="utf-8")

    # Import during ASR: the ASR is cancelled and its result never replaces
    # the imported project.
    assert controller.importVideo(str(video)) is True
    assert controller.transcribeVideo(False) is True and controller.aiBusy is True
    assert controller.importSrt(load_srt(tmp_path)) is True
    assert controller.aiBusy is False
    movie_id = controller.projectId
    finish_last()
    assert controller.projectId == movie_id and controller.cues[0]["text"] == "Xin chào"

    # Diarization of A, then B imported: A's result lands nowhere.
    assert controller.attachVideo(str(video)) is True
    assert controller.runDiarization() is True and controller.aiBusy is True
    assert controller.importSrt(str(other)) is True and controller.aiBusy is False
    finish_last()
    assert controller.hasSpeakers is False
    assert controller._store.require(movie_id).cues[0].speaker == ""  # noqa: SLF001

    # LLM on B, then A reopened: B's result lands nowhere either.
    other_id = controller.projectId
    assert controller.refineWithLlm("refine_asr", "vi") is True and controller.aiBusy is True
    assert controller.openProject(movie_id) is True and controller.aiBusy is False
    finish_last()
    assert controller.cues[0]["text"] == "Xin chào"
    assert controller._store.require(other_id).cues[0].text == "Nội dung khác"  # noqa: SLF001


def test_an_enrollment_lands_in_the_project_that_started_it(tmp_path, monkeypatch):
    # FR-1.4: `on_enrolled` completes a clone asynchronously. It persists the
    # voice into the enrolling project — not whichever is open by then — and
    # is never refused by the busy owners (it finishes an op, starts none).
    runner = _DeferredRunner()
    fake = FakeApp()
    enrollments: list[Any] = []
    fake.addVoice = lambda name, path, denoise, transcript, *, on_enrolled=None: enrollments.append(
        on_enrolled
    )
    controller = make_controller(tmp_path, fake, FakePlayer(), bg_runner=runner)
    checks: list[Any] = []

    def extract(vpath, cues, spk, out, *, cancel_check, **kw):
        checks.append(cancel_check)
        write_wav_file(np.zeros(4_800, dtype=np.float32), Path(out), sample_rate=48_000)
        return Path(out), "Xin chào"

    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.extract_speaker_reference_wav", extract
    )
    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.refine_cues_with_llm",
        lambda cues, cfg, **kw: list(cues),
    )
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"fake_mp4")
    other = tmp_path / "other.srt"
    other.write_text(OTHER_SRT, encoding="utf-8")
    assert controller.importSrt(load_srt(tmp_path)) is True
    assert controller.attachVideo(str(video)) is True
    assert controller.updateCue(0, "Xin chào", "SPEAKER_01") is True
    movie_id = controller.projectId

    def clone() -> Any:
        assert controller.cloneSpeakerFromVideo("SPEAKER_01", "Giọng A") is True
        work, done, _ = runner.calls[-1]
        done(work())
        return enrollments[-1]

    # Enrolled after B was opened (and while B runs an AI op).
    on_enrolled = clone()
    assert checks[-1]() is False  # the clone's extraction stops with its AI op …
    assert controller.importSrt(str(other)) is True
    assert checks[-1]() is True  # … which the project switch cancelled
    assert controller.refineWithLlm("refine_asr", "vi") is True and controller.aiBusy is True
    on_enrolled("clone-1")
    assert controller._store.require(movie_id).speaker_voices == {"SPEAKER_01": "clone-1"}  # noqa: SLF001
    assert controller._project.speaker_voices == {}  # noqa: SLF001
    controller.cancelAiTask()

    # Enrolled while its own project runs an AI op: applied, not refused.
    assert controller.openProject(movie_id) is True
    on_enrolled = clone()
    assert controller.refineWithLlm("refine_asr", "vi") is True and controller.aiBusy is True
    on_enrolled("clone-2")
    assert controller.speakers[0]["voice"] == "clone-2"
    assert controller._store.require(movie_id).speaker_voices == {"SPEAKER_01": "clone-2"}  # noqa: SLF001
    controller.cancelAiTask()

    # Enrolled mid-render (FR-1.6): the render's commit used to overwrite it.
    # It survives, and the track that lands — made with clone-2 — is stale.
    on_enrolled = clone()
    controller.render()
    assert controller.rendering is True
    on_enrolled("clone-3")
    assert controller.speakers[0]["voice"] == "clone-3"
    while fake.pending:
        complete_next_unit(fake, tmp_path)
    assert controller.rendering is False
    assert controller.speakers[0]["voice"] == "clone-3"
    assert controller._store.require(movie_id).speaker_voices == {"SPEAKER_01": "clone-3"}  # noqa: SLF001
    assert controller.rendered is False and controller.renderStale is True
    assert controller._stale_reason == "speaker_voice"  # noqa: SLF001


def test_remove_project_stops_owners_first_and_prunes_unreferenced_media(tmp_path, monkeypatch):
    # FR-1.4 / M7: removing the active project cancels its render, export or
    # AI op, clears, releases the video, and only then deletes the folder —
    # and it drops the managed SRT / media cache nothing else references.
    from vienetts_app.core.subtitles import Cue

    runner = _DeferredRunner()
    fake = FakeApp()
    controller = make_controller(tmp_path, fake, FakePlayer(), bg_runner=runner)
    store = controller._store  # noqa: SLF001
    events: list[str] = []
    controller.renderingChanged.connect(
        lambda: None if controller.rendering else events.append("cancel")
    )
    controller.exportingChanged.connect(
        lambda: None if controller.exporting else events.append("cancel")
    )
    controller.aiBusyChanged.connect(lambda: None if controller.aiBusy else events.append("cancel"))
    controller.loadedChanged.connect(lambda: None if controller.loaded else events.append("clear"))
    controller.videoReleaseRequested.connect(lambda: events.append("release"))
    real_remove = store.remove

    def remove(project_id: str) -> None:
        events.append("remove")
        real_remove(project_id)

    monkeypatch.setattr(store, "remove", remove)

    def start(owner: str, n: int) -> None:
        srt = tmp_path / f"take{n}.srt"
        srt.write_text(SRT, encoding="utf-8")
        assert controller.importSrt(str(srt)) is True
        if owner != "render":
            render_all(controller, fake, tmp_path)
        if owner == "render":
            controller.render()
        elif owner == "export":
            assert controller.exportTrack(str(tmp_path / "out")) != ""
        else:
            monkeypatch.setattr(
                "vienetts_app.ui.subtitle_controller.refine_cues_with_llm",
                lambda cues, cfg, **kw: list(cues),
            )
            assert controller.refineWithLlm("refine_asr", "vi") is True
            controller.confirmRenderInvalidation()
        assert controller.busyOwner == owner

    for n, owner in enumerate(("render", "export", "ai")):
        start(owner, n)
        pid = controller.projectId
        events.clear()
        assert controller.removeProject(pid) is True
        assert events == ["cancel", "clear", "release", "remove"], owner
        assert controller.busyOwner == "" and not store.project_dir(pid).exists()
        fake.pending.clear()

    # Media: an ASR project (managed SRT) and a companion-SRT project share
    # one video, so its cache outlives the first removal but not the second.
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"fake_mp4")
    (tmp_path / "talk.srt").write_text(OTHER_SRT, encoding="utf-8")
    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.run_transcription",
        transcribed([Cue(1, 0, 1500, "Từ ASR")]),
    )
    assert controller.importVideo(str(video)) is True
    companion_id = controller.projectId
    assert controller.transcribeVideo(False) is True
    work, done, _ = runner.calls[-1]
    done(work())
    managed_srt = Path(controller.sourcePath)
    cache = controller._media_cache_dir(video)  # noqa: SLF001
    assert managed_srt.parent == store.root and cache.is_dir()
    assert controller.removeProject(controller.projectId) is True
    assert not managed_srt.exists() and cache.is_dir()  # the companion project keeps it
    assert controller.removeProject(companion_id) is True
    assert not cache.exists()
    assert (tmp_path / "talk.srt").is_file() and video.is_file()  # user files untouched


def test_shutdown_cancels_ai_and_export_and_reaps_cli_children(tmp_path, monkeypatch):
    # FR-1.5 / M4: quitting stops the AI op and voids the export, then
    # terminates every registered native child (whisper-cli, ffmpeg, …).
    import subprocess
    import sys

    from vienetts_app.core import processes

    runner = _DeferredRunner()
    controller = make_controller(tmp_path, FakeApp(), FakePlayer(), bg_runner=runner)
    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.refine_cues_with_llm",
        lambda cues, cfg, **kw: [replace(c, text="Sau khi thoát") for c in cues],
    )
    assert controller.importSrt(load_srt(tmp_path)) is True
    assert controller.refineWithLlm("refine_asr", "vi") is True and controller.aiBusy is True
    export_generation = controller._export_generation  # noqa: SLF001
    child = processes.spawn_cli([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        controller.shutdown()
        assert child.poll() is not None  # terminated (or killed) and reaped
        assert child not in processes.registered_children()
    finally:
        if child.poll() is None:
            child.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            child.wait(timeout=5)
    assert controller.aiBusy is False
    assert controller._export_generation == export_generation + 1  # noqa: SLF001
    work, done, _ = runner.calls[-1]
    done(work())  # the cancelled op's late result is dropped
    assert controller.cues[0]["text"] == "Xin chào"


def test_transcript_mode_uses_its_real_gap_cap(tmp_path):
    controller, _, _ = _rendered_controller(tmp_path)
    # Dub mode preserves the source pauses exactly (0 == uncapped).
    assert controller.maxGapMs == 0
    assert controller._fit_policy().max_gap_ms == 0  # noqa: SLF001

    # The mode change is staged (a live render would be lost); the effective
    # gap follows only once the change is applied.
    controller.mode = "transcript"
    assert controller.maxGapMs == 0
    controller.confirmRenderInvalidation()
    assert controller.mode == "transcript"
    assert controller.maxGapMs == 1500
    assert controller._fit_policy().max_gap_ms == 1500  # noqa: SLF001

    controller.maxGapMs = 0  # an explicit uncapped choice survives
    assert controller._fit_policy().max_gap_ms == 0  # noqa: SLF001
    controller.mode = "dub"
    assert controller.maxGapMs == 0  # explicit, not the mode default
    assert controller._fit_policy().max_gap_ms == 0  # noqa: SLF001


def test_new_projects_keep_the_original_audio_by_default(tmp_path):
    controller = make_controller(tmp_path, FakeApp(), FakePlayer())
    controller.importSrt(load_srt(tmp_path))
    assert controller.videoMixMode == "duck"
    assert controller._store.require(controller.projectId).video_mix_mode == "duck"  # noqa: SLF001
    controller.videoMixMode = "replace"
    assert controller.videoMixMode == "replace"
    assert controller._store.require(controller.projectId).video_mix_mode == "replace"  # noqa: SLF001


class _DeferredRunner:
    def __init__(self) -> None:
        self.calls: list[tuple[Any, Any, Any]] = []

    def __call__(self, work, done, owner, on_error=None):
        self.calls.append((work, done, on_error))


def _on_thread(work: Any) -> Any:
    """Run a queued ``work`` on a real worker thread (for thread-recording spies)."""
    box: list[Any] = []
    thread = threading.Thread(target=lambda: box.append(work()))
    thread.start()
    thread.join()
    return box[0]


def test_cancel_export_unlatches_and_ignores_the_stale_completion(tmp_path, monkeypatch):
    from vienetts_app.core.video_dub import VideoDubCancelled

    fake = FakeApp()
    player = FakePlayer()
    runner = _DeferredRunner()
    controller = SubtitleController(
        fake,
        data_dir=tmp_path,
        player_factory=lambda: player,
        store_factory=lambda root: SubtitleProjectStore(Path(root) / "subtitles"),
        bg_runner=runner,
        unit_executor=SyncOrderedExecutor(),
    )
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"fake_mp4")
    controller.importSrt(load_srt(tmp_path))
    assert controller.attachVideo(str(video)) is True
    render_all(controller, fake, tmp_path)
    events: list[tuple[str, str]] = []
    controller.exportFinished.connect(lambda path, error: events.append((path, error)))
    out = str(tmp_path / "out")
    cancelled = ("", "Đã hủy xuất.")

    target = controller.exportTrack(out)
    assert target != ""
    assert controller.exporting is True
    assert len(runner.calls) == 1

    controller.cancelExport()
    assert controller.exporting is False
    assert events == [cancelled]

    # The worker finishing later must not emit a second result or re-latch,
    # and the cancelled export leaves no file (M3) — nor does an SRT export.
    work, done, _on_error = runner.calls[0]
    done(work())
    assert events == [cancelled]
    assert controller.exporting is False
    assert not Path(target).exists()
    target = controller.exportSrt(out)
    controller.cancelExport()
    work, done, _on_error = runner.calls[-1]
    done(work())
    assert not Path(target).exists()
    assert events == [cancelled, cancelled]

    # Cancelling while idle is a no-op.
    controller.cancelExport()
    assert events == [cancelled, cancelled]

    # M3: video export 1 is cancelled while its ffmpeg runs and export 2
    # starts. Export 1 still stops (the cancel check is per generation, not a
    # shared flag export 2 resets), and the two never share a temp SRT or an
    # output name.
    started, release = threading.Event(), threading.Event()
    muxes: list[dict[str, Any]] = []

    def fake_mux(vpath, apath, outpath, *, srt_path=None, cancel_check=None, **kw):
        if not muxes:
            started.set()
            release.wait(5)
        muxes.append({"srt": Path(srt_path), "srt_alive": Path(srt_path).is_file()})
        muxes[-1]["cancelled"] = cancel_check()
        if muxes[-1]["cancelled"]:
            raise VideoDubCancelled("Đã hủy xuất video.")
        Path(outpath).write_bytes(b"muxed")
        return Path(outpath)

    monkeypatch.setattr("vienetts_app.ui.subtitle_controller.mux_dubbed_video", fake_mux)
    first = controller.exportVideo(out)
    work1, _done1, failed1 = runner.calls[-1]
    outcome: list[BaseException] = []

    def run_first() -> None:
        try:
            work1()
        except BaseException as exc:  # noqa: BLE001 - handed to the controller below
            outcome.append(exc)

    worker = threading.Thread(target=run_first)
    worker.start()
    assert started.wait(5)
    controller.cancelExport()
    second = controller.exportVideo(out)
    release.set()
    worker.join(5)
    work2, done2, _failed2 = runner.calls[-1]
    done2(work2())
    failed1(outcome[0])
    assert [m["cancelled"] for m in muxes] == [True, False]
    assert muxes[0]["srt"] != muxes[1]["srt"]
    assert all(m["srt_alive"] for m in muxes) and not any(m["srt"].exists() for m in muxes)
    assert first != second
    assert not Path(first).exists() and Path(second).read_bytes() == b"muxed"
    assert events[-2:] == [cancelled, (second, "")]


# ── derived state model (FR-2.2) ────────────────────────────────────────────


def test_derived_state_covers_the_whole_lifecycle(tmp_path):
    fake = FakeApp()
    player = FakePlayer()
    controller = make_controller(tmp_path, fake, player)
    states: list[str] = []
    owners: list[str] = []
    controller.stateChanged.connect(lambda: states.append(controller.state))
    controller.busyOwnerChanged.connect(lambda: owners.append(controller.busyOwner))

    assert controller.state == "empty"
    assert controller.busyOwner == ""

    video = tmp_path / "movie.mp4"
    video.write_bytes(b"fake_mp4")
    assert controller.importVideo(str(video)) is True
    assert controller.state == "source_only"

    assert controller.importSrt(load_srt(tmp_path)) is True
    assert controller.state == "cues_ready"

    controller.render()
    assert controller.state == "rendering"
    assert controller.busyOwner == "render"
    while fake.pending:
        complete_next_unit(fake, tmp_path)
    assert controller.state == "rendered"
    assert controller.busyOwner == ""

    controller._set_ai_state(True, "Đang chạy AI…", 0.2)  # noqa: SLF001
    assert controller.state == "ai_busy"
    assert controller.busyOwner == "ai"
    controller._set_ai_state(False, "", 0.0)  # noqa: SLF001
    assert controller.state == "rendered"

    controller._set_error("lỗi thử nghiệm")  # noqa: SLF001
    assert controller.state == "error"
    controller._set_error("")  # noqa: SLF001
    assert controller.state == "rendered"

    controller.rateCap = 1.2
    controller.confirmRenderInvalidation()
    assert controller.state == "stale"

    # busyOwner reports one owner (or none) at every transition.
    assert set(owners) <= {"", "ai", "render", "export"}
    assert "rendering" in states and "rendered" in states


def test_a_cache_hit_render_never_enters_the_rendering_state(tmp_path):
    controller, _, _ = _rendered_controller(tmp_path)
    states: list[str] = []
    controller.stateChanged.connect(lambda: states.append(controller.state))
    controller.render()  # adopts the cached track
    assert controller.rendered is True
    assert "rendering" not in states


def test_exporting_is_the_single_busy_owner(tmp_path):
    fake = FakeApp()
    player = FakePlayer()
    runner = _DeferredRunner()
    controller = SubtitleController(
        fake,
        data_dir=tmp_path,
        player_factory=lambda: player,
        store_factory=lambda root: SubtitleProjectStore(Path(root) / "subtitles"),
        bg_runner=runner,
        unit_executor=SyncOrderedExecutor(),
    )
    controller.importSrt(load_srt(tmp_path))
    render_all(controller, fake, tmp_path)
    controller.exportTrack(str(tmp_path / "out"))
    assert controller.state == "exporting"
    assert controller.busyOwner == "export"
    controller.cancelExport()
    assert controller.state == "rendered"
    assert controller.busyOwner == ""


# ── FR-3.6 / FR-3.7: hint channel and notice severity ────────────────────────


def test_seek_before_render_sets_a_hint(env, tmp_path):
    controller, _, player = env
    controller.importSrt(load_srt(tmp_path))
    controller.seekToCue(0)
    assert "âm thanh" in controller.hintText
    assert player.seeks == []


def test_play_before_render_announces_the_render(env, tmp_path):
    controller, _, _ = env
    controller.importSrt(load_srt(tmp_path))
    controller.play()
    assert "đang tạo" in controller.hintText
    assert controller.rendering is True


def test_busy_owners_refuse_each_other_with_a_reason(tmp_path):
    # FR-1.1 / AC-1: `ai`, `render` and `export` exclude one another. Every
    # refused entry slot starts nothing and explains itself through the hint
    # channel, mirrored by `busyReason` (the dock's disabledReason).
    fake, player, runner = FakeApp(), FakePlayer(), _DeferredRunner()
    controller = SubtitleController(
        fake,
        data_dir=tmp_path,
        player_factory=lambda: player,
        store_factory=lambda root: SubtitleProjectStore(Path(root) / "subtitles"),
        bg_runner=runner,
        unit_executor=SyncOrderedExecutor(),
    )
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"fake_mp4")
    assert controller.importSrt(load_srt(tmp_path)) is True
    assert controller.importVideo(str(video)) is True
    assert controller.hasVideo is True
    out = str(tmp_path / "out")

    def ai_and_cue_slots_refuse() -> None:
        assert controller.transcribeVideo(False) is False
        assert controller.runDiarization() is False
        assert controller.refineWithLlm("refine_asr", "vi") is False
        assert controller.cloneSpeakerFromVideo("SPEAKER_01") is False
        assert controller.updateCue(0, "Sửa khi đang bận", "") is False
        assert controller.setSpeakerVoice("SPEAKER_01", "voice-b") is False
        assert controller.cues[0]["text"] == "Xin chào"
        assert controller.invalidationPending is False

    def refused(owner_reason: str) -> None:
        assert controller.busyReason == owner_reason != ""
        assert controller.hintText == owner_reason

    # ── an AI op in flight: render / play-to-render / exports start nothing ──
    assert controller.refineWithLlm("refine_asr", "vi") is True
    assert controller.aiBusy is True and len(runner.calls) == 1
    ai_reason = controller.busyReason
    controller.render()
    assert controller.rendering is False and fake.pending == []
    refused(ai_reason)
    controller.play()
    assert controller.rendering is False and fake.pending == [] and player.played == []
    ai_and_cue_slots_refuse()
    assert len(runner.calls) == 1  # no second AI op queued
    controller.rateCap = 1.2  # knobs refuse too (snap back)
    assert controller.rateCap == 1.5
    controller.voice = "voice-b"  # ...but not the picker's one-way voice write
    assert controller.voice == "voice-b"
    refused(ai_reason)
    controller.cancelAiTask()
    assert controller.busyReason == "" and controller.hintText == ""  # moot once idle

    # ── rendering: AI ops, cue edits and exports refuse ──
    controller.render()
    assert controller.rendering is True
    render_reason = controller.busyReason
    ai_and_cue_slots_refuse()
    refused(render_reason)
    assert len(runner.calls) == 1
    while fake.pending:
        complete_next_unit(fake, tmp_path)
    assert controller.rendered is True and controller.hintText == ""

    # ── exporting: AI ops, cue edits, knobs and render refuse ──
    assert controller.exportTrack(out) != ""
    assert controller.exporting is True
    export_reason = controller.busyReason
    ai_and_cue_slots_refuse()
    refused(export_reason)
    controller.rateCap = 1.2
    assert controller.rateCap == 1.5 and controller.invalidationPending is False
    assert controller.exportSrt(out) == ""
    assert len(runner.calls) == 2  # only the export itself
    controller.cancelExport()

    # ── AI busy on a rendered project: every export refuses ──
    assert controller.refineWithLlm("refine_asr", "vi") is True
    controller.confirmRenderInvalidation()
    assert controller.aiBusy is True
    assert controller.exportTrack(out) == ""
    assert controller.exportSrt(out) == ""
    assert controller.exportVideo(out) == ""
    assert controller.exporting is False
    refused(ai_reason)
    assert len({ai_reason, render_reason, export_reason}) == 3


def test_ai_progress_reaches_the_gui_thread_only_for_the_live_op(qcoreapp, tmp_path, monkeypatch):
    # FR-1.2 / H2: AI workers report through a generation-checked relay, so
    # `_set_ai_state` only ever runs on the GUI thread and a report from a
    # cancelled op never switches `aiBusy` back on.
    from vienetts_app.core.subtitles import Cue

    runner = _DeferredRunner()
    controller = make_controller(tmp_path, FakeApp(), FakePlayer(), bg_runner=runner)
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"fake_mp4")
    assert controller.importVideo(str(video)) is True
    calls: list[tuple[bool, bool, float]] = []
    set_ai_state = controller._set_ai_state  # noqa: SLF001

    def spy(busy: bool, stage_text: str = "", progress: float = 0.0) -> None:
        on_gui = threading.current_thread() is threading.main_thread()
        calls.append((on_gui, busy, round(progress, 3)))
        set_ai_state(busy, stage_text, progress)

    monkeypatch.setattr(controller, "_set_ai_state", spy)

    def transcribe(path, cfg, diar_cfg=None, *, progress, **kw):
        progress(0.35, "Đang nhận diện…")  # the pipeline's combined bar
        progress(0.85, "Đang phân tách người nói…")
        return TranscriptionResult([Cue(1, 0, 1500, "Xin chào", speaker="SPEAKER_01")], [])

    monkeypatch.setattr("vienetts_app.ui.subtitle_controller.run_transcription", transcribe)

    def start_worker(work: Any) -> tuple[threading.Thread, list[Any]]:
        out: list[Any] = []
        thread = threading.Thread(target=lambda: out.append(work()))
        thread.start()
        return thread, out

    # ── ASR then diarization progress: queued off the worker, painted on the GUI ──
    assert controller.transcribeVideo(True) is True
    work, done, _ = runner.calls[-1]
    thread, out = start_worker(work)
    thread.join(10)
    assert out and all(on_gui for on_gui, _, _ in calls)  # nothing ran on the worker
    painted = len(calls)
    QCoreApplication.sendPostedEvents()
    assert calls[painted:] == [(True, True, 0.35), (True, True, 0.85)]
    assert controller.aiProgress == pytest.approx(0.85)
    done(out[0])
    assert controller.aiBusy is False and controller.hasSpeakers is True

    # ── LLM batches reported around cancelAiTask() are dropped ──
    reported, resume = threading.Event(), threading.Event()

    def refine(cues, cfg, *, progress_cb, **kw):
        progress_cb(0.2)  # queued before the cancel, delivered after it
        reported.set()
        assert resume.wait(5)
        progress_cb(0.9)  # reported after the cancel
        return [replace(cue, text="Đã hủy") for cue in cues]

    monkeypatch.setattr("vienetts_app.ui.subtitle_controller.refine_cues_with_llm", refine)
    assert controller.refineWithLlm("refine_asr", "vi") is True
    work, done, _ = runner.calls[-1]
    thread, out = start_worker(work)
    assert reported.wait(5)
    controller.cancelAiTask()
    resume.set()
    thread.join(10)
    QCoreApplication.sendPostedEvents()
    assert controller.aiBusy is False and controller.aiProgress == 0.0
    assert all(on_gui for on_gui, _, _ in calls)
    done(out[0])  # the cancelled op's result is dropped too
    assert controller.aiBusy is False and controller.cues[0]["text"] == "Xin chào"


def test_speaker_sample_has_its_own_generation_cache_key_and_playback_guard(tmp_path, monkeypatch):
    # FR-1.3 / L6: an audition never touches the AI generation, re-extracts its
    # cached clip only when the chosen bounds or the video change, and plays
    # through the same guard as the dub track.
    from vienetts_app.core.subtitles import Cue

    runner = _DeferredRunner()
    player = FakePlayer()
    controller = make_controller(tmp_path, FakeApp(), player, bg_runner=runner)
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"fake_mp4")
    assert controller.importSrt(load_srt(tmp_path)) is True
    assert controller.importVideo(str(video)) is True
    assert controller.updateCue(0, "Xin chào", "SPEAKER_01") is True
    assert controller.updateCue(1, "Tạm biệt", "SPEAKER_01") is True
    extracted: list[tuple[int, int]] = []
    targets: list[Path] = []
    live: list[bool] = []

    def extract(vpath, cues, spk, out, *, work_dir, data_dir, cancel_check, **kw):
        # FR-4.4 wiring: a video is scored on its cached 16 kHz audio, and the
        # extraction stops once its audition is superseded.
        assert (work_dir, data_dir) == (controller._media_cache_dir(video), tmp_path)  # noqa: SLF001
        live.append(not cancel_check())
        spoken = [cue for cue in cues if cue.speaker == spk]
        extracted.append((spoken[0].start_ms, spoken[0].end_ms))
        targets.append(Path(out))
        write_wav_file(np.zeros(4_800, dtype=np.float32), Path(out), sample_rate=48_000)
        return Path(out), spoken[0].text

    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.extract_speaker_reference_wav", extract
    )
    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.refine_cues_with_llm",
        lambda cues, cfg, **kw: [replace(cue, text=cue.text + "!") for cue in cues],
    )

    def finish(call: tuple[Any, Any, Any]) -> None:
        work, done, _ = call
        done(work())

    # An AI op started during the extraction completes normally, and the
    # audition still plays: neither invalidates the other.
    ai_generation = controller._ai_generation  # noqa: SLF001
    assert controller.playSpeakerSample("SPEAKER_01") is True
    assert controller._ai_generation == ai_generation  # noqa: SLF001
    assert controller.refineWithLlm("refine_asr", "vi") is True
    sample_op, ai_op = runner.calls[-2:]
    finish(ai_op)
    assert controller.aiBusy is False and controller.cues[0]["text"] == "Xin chào!"
    finish(sample_op)
    assert extracted == [(0, 2000)] and len(player.played) == 1
    ref = player.played[0]

    # Reused while the chosen bounds and the video stay put (a text-only edit
    # keeps them) …
    assert controller.playSpeakerSample("SPEAKER_01") is True
    assert player.played == [ref, ref] and extracted == [(0, 2000)]
    # … re-extracted once the chosen clip moves, or the video changes.
    assert controller.updateCue(0, "Xin chào!", "") is True
    assert controller.playSpeakerSample("SPEAKER_01") is True
    finish(runner.calls[-1])
    video.write_bytes(b"fake_mp4, re-encoded")
    assert controller.playSpeakerSample("SPEAKER_01") is True
    finish(runner.calls[-1])
    assert extracted == [(0, 2000), (3000, 5000), (3000, 5000)]
    assert player.played == [ref] * 4
    # The audio-scored choice weighs every cue, so another speaker's cue
    # moving re-keys the clip even though the cue-only bounds stay put.
    cues = controller._project.cues  # noqa: SLF001
    keys = {
        controller._speaker_ref_key(  # noqa: SLF001
            str(video),
            (*cues, Cue(3, start, start + 900, "Ừ.", speaker="SPEAKER_02")),
            "SPEAKER_01",
        )
        for start in (6000, 6400)
    }
    assert len(keys) == 2
    # FR-6.3: handing the cache dir over creates nothing (the core fills it).
    assert not controller._media_cache_dir(video).exists()  # noqa: SLF001
    # Each extraction writes its own temp file, renamed over the clip only
    # when complete: two in flight never write one file.
    assert len(set(targets)) == 3 and Path(ref) not in targets
    assert not any(target.exists() for target in targets)

    # A newer playback request supersedes a pending audition …
    video.write_bytes(b"fake_mp4, edited again")
    assert controller.playSpeakerSample("SPEAKER_01") is True
    controller.stopPlay()
    finish(runner.calls[-1])
    assert len(player.played) == 4
    assert live == [True, True, True, False]  # … and its extraction is told to stop
    # … and a player that raises is contained by the guarded playback path.

    def broken_play(path: Any, on_released: Any = None) -> bool:
        raise RuntimeError("no audio device")

    monkeypatch.setattr(player, "play", broken_play)
    assert controller.playSpeakerSample("SPEAKER_01") is False  # the cached clip
    assert controller.errorText == "Hệ thống này không phát được âm thanh."


def test_notice_level_separates_errors_from_info(env, tmp_path):
    controller, _, _ = env
    assert controller.noticeLevel == "error"
    controller.importSrt(str(tmp_path / "missing.srt"))
    assert "Không tìm thấy" in controller.errorText
    assert controller.noticeLevel == "error"
    controller.clearNotice()
    assert controller.errorText == ""
    assert controller.noticeLevel == "info"
    assert controller.importSrt(load_srt(tmp_path)) is True
    assert controller.errorText == ""
    assert controller.noticeLevel == "info"


def test_video_overrun_is_probed_off_thread_once_per_track_and_video(tmp_path, monkeypatch):
    # FR-6.1: the property answers at once; ffprobe runs on a worker, keyed
    # by the track's and the video's stat (not by the mix/gain/embed knobs).
    calls: list[bool] = []

    def _fake_overrun(*args, **kwargs):
        calls.append(threading.current_thread() is threading.main_thread())
        return 4200

    monkeypatch.setattr("vienetts_app.ui.subtitle_controller.dub_overrun_ms", _fake_overrun)
    runner = _DeferredRunner()
    fake = FakeApp()
    controller = make_controller(tmp_path, fake, FakePlayer(), bg_runner=runner)
    controller.importSrt(load_srt(tmp_path))
    render_all(controller, fake, tmp_path)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake_mp4")
    assert controller.attachVideo(str(video)) is True and controller.rendered is True
    changed: list[int] = []
    controller.overrunChanged.connect(lambda: changed.append(1))

    queued = len(runner.calls)
    assert controller.videoOverrunMs == 0
    assert controller.videoOverrunMs == 0  # one probe in flight, not one per read
    assert calls == [] and len(runner.calls) == queued + 1
    work, done, _ = runner.calls[-1]
    done(_on_thread(work))
    assert calls == [False] and changed == [1]
    assert controller.videoOverrunMs == 4200

    controller.videoMixMode = "replace"
    controller.videoBgGainDb = -10.0
    controller.embedSubtitles = False
    controller.renderedChanged.emit()
    assert controller.videoOverrunMs == 4200
    assert len(runner.calls) == queued + 1 and changed == [1]

    # A changed video (or track) is probed again.
    video.write_bytes(b"fake_mp4, re-encoded longer")
    controller.videoChanged.emit()
    assert changed == [1, 1]
    assert controller.videoOverrunMs == 0 and len(runner.calls) == queued + 2


def _media_entry(root: Path, name: str, size: int, age_days: float) -> Path:
    """A media-cache entry last used ``age_days`` ago (its sidecar's mtime)."""
    import os
    import time

    entry = root / name
    entry.mkdir(parents=True)
    (entry / "source_16k.wav").write_bytes(b"\0" * size)
    sidecar = entry / "source_16k.wav.json"
    sidecar.write_text("{}", encoding="utf-8")
    stamp = time.time() - age_days * 86_400
    os.utime(sidecar, (stamp, stamp))
    return entry


def test_media_cache_eviction_keeps_the_active_entry_and_drops_the_oldest(tmp_path):
    # FR-6.3: entries unused for 30 days go, then the least recently used
    # (sidecar mtime) until the cache fits its cap — never the active entry.
    from vienetts_app.ui.subtitle_controller import evict_media_cache

    root = tmp_path / "subtitles" / "_media_cache"
    active = _media_entry(root, "active", 400, 90)
    _media_entry(root, "stale", 10, 31)
    _media_entry(root, "old", 300, 3)
    _media_entry(root, "mid", 300, 2)
    _media_entry(root, "new", 300, 1)
    removed = evict_media_cache(root, keep={active}, cap_bytes=1_100)
    assert sorted(path.name for path in removed) == ["old", "stale"]
    assert sorted(path.name for path in root.iterdir()) == ["active", "mid", "new"]

    # The sweep runs on a worker at startup, under the 30-day/5 GB defaults.
    _media_entry(root, "unused", 10, 45)
    runner = _DeferredRunner()
    make_controller(tmp_path, FakeApp(), FakePlayer(), bg_runner=runner)
    assert len(runner.calls) == 1 and (root / "unused").is_dir()
    work, done, _ = runner.calls[0]
    done(_on_thread(work))
    assert sorted(path.name for path in root.iterdir()) == ["mid", "new"]


QML_COMPONENTS = Path(__file__).resolve().parents[2] / "src" / "vienetts_app" / "ui" / "qml"


def test_every_property_is_consumed_or_api_only() -> None:
    """Task 4.8: no controller property is dead surface.

    Every ``@Property`` is either read from QML as
    ``subtitleController.<name>`` or listed in ``API_ONLY_PROPERTIES`` with a
    documented reason. The list must stay exact: a stale entry that QML does
    consume fails the second half, so deleting a binding forces the list to be
    revisited.
    """
    properties = {
        name for name, value in SubtitleController.__dict__.items() if isinstance(value, Property)
    }
    assert properties, "introspection found no @Property descriptors"

    qml = "\n".join(path.read_text(encoding="utf-8") for path in QML_COMPONENTS.rglob("*.qml"))

    def referenced(name: str) -> bool:
        return bool(re.search(rf"subtitleController\s*\.\s*{re.escape(name)}\b", qml))

    unbound = {name for name in properties if not referenced(name)}
    assert unbound == set(API_ONLY_PROPERTIES), {
        "undocumented": sorted(unbound - API_ONLY_PROPERTIES),
        "stale_api_only": sorted(API_ONLY_PROPERTIES - unbound),
        "unknown": sorted(API_ONLY_PROPERTIES - properties),
    }


def test_video_speech_bounds_are_refined_off_thread_by_generation_then_render_uniformly(
    tmp_path: Path, monkeypatch
) -> None:
    from vienetts_app.core.audio import write_wav_file
    from vienetts_app.ui import subtitle_controller as module

    runner = _DeferredRunner()
    fake = FakeApp()
    controller = make_controller(tmp_path, fake, FakePlayer(), bg_runner=runner)
    video = tmp_path / "speech_lesson.mp4"
    video.write_bytes(b"fake_video")
    srt_text = (
        "1\n00:00:00,000 --> 00:00:01,500\nMệnh đề đầu tiên,\n\n"
        "2\n00:00:01,600 --> 00:00:03,000\nmệnh đề thứ hai.\n"
    )
    (tmp_path / "speech_lesson.srt").write_text(srt_text, encoding="utf-8")

    # FR-6.3: a media-cache lookup creates nothing (the core fills it).
    cache_dir = controller._media_cache_dir(video)  # noqa: SLF001
    assert not cache_dir.exists()
    # Cached 16 kHz WAV with active speech only in 200..1100 ms and 1700..2700 ms
    sr16 = 16_000
    wav_16k = np.zeros(sr16 * 3, dtype=np.float32)
    t1 = np.linspace(0.0, 0.9, int(sr16 * 0.9), endpoint=False, dtype=np.float32)
    wav_16k[int(sr16 * 0.2) : int(sr16 * 1.1)] = (0.3 * np.sin(2.0 * np.pi * 220.0 * t1)).astype(
        np.float32
    )
    t2 = np.linspace(0.0, 1.0, int(sr16 * 1.0), endpoint=False, dtype=np.float32)
    wav_16k[int(sr16 * 1.7) : int(sr16 * 2.7)] = (0.3 * np.sin(2.0 * np.pi * 260.0 * t2)).astype(
        np.float32
    )
    cache_dir.mkdir(parents=True)
    write_wav_file(wav_16k, cache_dir / "source_16k.wav", sample_rate=sr16)
    real_refine = module.refine_cues_speech_bounds
    seen: list[tuple[bool, Any]] = []

    def spy(cues, wav, **kw):
        seen.append((threading.current_thread() is threading.main_thread(), kw.get("data_dir")))
        return real_refine(cues, wav, **kw)

    monkeypatch.setattr(module, "refine_cues_speech_bounds", spy)

    def unrefined() -> bool:
        return all(cue.speech_start_ms is None for cue in controller._project.cues)  # noqa: SLF001

    # FR-6.1: the import lands at once; the WAV is read on a worker, with
    # Silero when installed (data_dir), and applied by generation.
    assert controller.importVideo(str(video)) is True
    assert controller.loaded is True and unrefined() and seen == []
    stale_work, stale_done, _ = runner.calls[-1]
    assert controller.importVideo(str(video)) is True  # a newer import supersedes it
    work, done, _ = runner.calls[-1]
    stale_done(_on_thread(stale_work))
    assert unrefined()
    done(_on_thread(work))
    assert seen == [(False, tmp_path), (False, tmp_path)]
    proj_cues = controller._project.cues  # noqa: SLF001
    assert proj_cues[0].speech_start_ms is not None and 150 <= proj_cues[0].speech_start_ms <= 220
    assert proj_cues[0].speech_end_ms is not None and 1080 <= proj_cues[0].speech_end_ms <= 1150
    assert proj_cues[1].speech_start_ms is not None and 1650 <= proj_cues[1].speech_start_ms <= 1720
    assert proj_cues[1].speech_end_ms is not None and 2680 <= proj_cues[1].speech_end_ms <= 2750
    assert controller._store.require(controller.projectId).cues == proj_cues  # noqa: SLF001

    # Attaching a video to a loaded project refines it the same way.
    controller.clear()
    plain = tmp_path / "notes" / "plain.srt"
    plain.parent.mkdir()
    plain.write_text(srt_text, encoding="utf-8")
    other = tmp_path / "clips" / "lesson_b.mp4"
    other.parent.mkdir()
    other.write_bytes(b"fake_video_b")
    other_cache = controller._media_cache_dir(other)  # noqa: SLF001
    other_cache.mkdir(parents=True)
    write_wav_file(wav_16k, other_cache / "source_16k.wav", sample_rate=sr16)
    assert controller.importSrt(str(plain)) is True and unrefined()
    queued = len(runner.calls)
    assert controller.attachVideo(str(other)) is True
    assert controller.videoPath == str(other) and unrefined()
    assert len(runner.calls) == queued + 1
    work, done, _ = runner.calls[-1]
    done(_on_thread(work))
    assert controller._project.cues == proj_cues  # noqa: SLF001
    assert seen[-1] == (False, tmp_path)

    # Enable mergeSentences in dub mode and render a merged 2-cue unit; both cues get the same rate
    controller.mergeSentences = True
    controller.render()
    assert len(fake.pending) == 1
    job_id, _, listener = fake.pending.pop(0)
    # Synthesize 3200 ms of speech for the ~2500 ms unit window -> compressed at uniform rate
    unit_wav = tmp_path / "merged_unit.wav"
    unit_samples = np.full(int(48_000 * 3.2), 0.25, dtype=np.float32)
    write_wav_file(unit_samples, unit_wav, sample_rate=48_000)
    art = SynthesisArtifact(
        job_id=job_id,
        path=unit_wav,
        sample_rate=48_000,
        samples=unit_samples.size,
        duration_ms=3_200,
    )
    listener.on_synthesis_terminal(
        JobTerminal(job_id=job_id, owner="audiobook", state="completed", value=art)
    )
    assert controller.rendered is True
    rates = controller._project.cue_rates  # noqa: SLF001
    assert len(rates) == 2
    assert rates[0] > 1.0
    assert rates[0] == pytest.approx(rates[1])

    # Re-importing the SRT keeps the stored render on disk until the refined
    # cues land, which then adopt it instead of re-synthesizing.
    assert controller.importSrt(str(plain)) is True
    assert controller.rendered is False and unrefined()
    assert controller._store.require(controller.projectId).rendered is True  # noqa: SLF001
    work, done, _ = runner.calls[-1]
    done(_on_thread(work))
    assert controller.rendered is True and controller._project.cue_rates == rates  # noqa: SLF001
    assert fake.pending == []


def test_cue_lookup_bisects_to_the_linear_scan_answer(env, tmp_path: Path) -> None:
    # FR-6.2: cueIndexAt and the video sync answer like the first-match scan
    # they replace — unsorted, overlapping and zero-length cues included.
    controller, _, _ = env
    srt = (
        "1\n00:00:03,000 --> 00:00:05,000\nCâu B\n\n"
        "2\n00:00:00,000 --> 00:00:04,000\nCâu A\n\n"
        "3\n00:00:01,000 --> 00:00:01,000\nCâu rỗng\n\n"
        "4\n00:00:01,500 --> 00:00:02,000\nCâu C\n\n"
        "5\n00:00:07,000 --> 00:00:08,000\nCâu D\n"
    )
    assert controller.importSrt(load_srt(tmp_path, srt)) is True
    cues = controller._project.cues  # noqa: SLF001

    def oracle(ms: int) -> int:
        return next((i for i, cue in enumerate(cues) if cue.start_ms <= ms < cue.end_ms), -1)

    bounds = {t for cue in cues for t in (cue.start_ms, cue.end_ms)}
    edges = sorted({-1, 0, 10_000} | {t + d for t in bounds for d in (-1, 0, 1)})
    expected = [oracle(ms) for ms in edges]
    assert [controller.cueIndexAt(ms) for ms in edges] == expected
    synced = []
    for ms in edges:
        controller.syncActiveCueFromVideo(ms)
        synced.append(controller.activeCue)
    assert synced == [oracle(max(0, ms)) for ms in edges]
    # The index is rebuilt with the cues.
    assert controller.cueIndexAt(7_500) == 4
    assert controller.importSrt(load_srt(tmp_path)) is True
    assert (controller.cueIndexAt(7_500), controller.cueIndexAt(3_400)) == (-1, 1)


def test_video_preview_url_release_unrendered_seek_and_audio_exclusivity(
    env, tmp_path: Path, monkeypatch
) -> None:
    from PySide6.QtCore import QUrl

    controller, fake, player = env
    releases: list[str] = []
    video_seeks: list[int] = []
    controller.videoReleaseRequested.connect(lambda: releases.append("released"))
    controller.videoSeekRequested.connect(video_seeks.append)

    # 1. No video initially -> videoUrl is empty; unrendered seekToCue refuses with hintText
    assert controller.importSrt(load_srt(tmp_path)) is True
    assert controller.hasVideo is False
    assert controller.videoUrl == ""
    controller.seekToCue(1)
    assert "Chưa có bản âm thanh" in controller.hintText
    assert controller.activeCue == -1
    assert player.seeks == []
    assert video_seeks == []

    # 2. Attach video -> videoUrl exposes QUrl.fromLocalFile(videoPath)
    video = tmp_path / "preview_clip.mp4"
    video.write_bytes(b"fake_mp4")
    assert controller.attachVideo(str(video)) is True
    assert controller.hasVideo is True
    assert controller.videoUrl == QUrl.fromLocalFile(str(video)).toString()

    # 3. Unrendered seekToCue with video attached seeks video preview without touching dub player
    controller.seekToCue(1)
    assert controller.hintText == ""
    assert controller.activeCue == 1
    assert controller.positionMs == 3_000
    assert player.seeks == []
    assert video_seeks == [3_000]
    controller.seekToCue(99)  # out of range is a no-op
    assert controller.activeCue == 1
    assert video_seeks == [3_000]

    # 4. syncActiveCueFromVideo highlights cue spanning ms (or -1 in silence) when dub is idle
    controller.syncActiveCueFromVideo(500)
    assert controller.activeCue == 0
    assert controller.positionMs == 500
    controller.syncActiveCueFromVideo(2_500)  # silence between 0..2000 and 3000..5000
    assert controller.activeCue == -1
    assert controller.positionMs == 2_500
    controller.syncActiveCueFromVideo(3_400)
    assert controller.activeCue == 1

    # 5. Rendered seekToCue seeks both dub player (measured timeline) and video preview (SRT start)
    render_all(controller, fake, tmp_path)
    assert controller.rendered is True
    video_seeks.clear()
    player.seeks.clear()
    controller.seekToCue(0)
    assert player.seeks == [0]
    assert video_seeks == [0]

    # 6. Audio exclusivity: notifyVideoPreviewPlaying stops active dub playback;
    #    while dub is playing, syncActiveCueFromVideo does not clobber dub karaoke state.
    controller.play()
    assert controller.playerState == "playing"
    player.positionChanged.emit(100)
    assert controller.activeCue == 0
    controller.syncActiveCueFromVideo(3_500)
    assert controller.activeCue == 0  # dub playback owns activeCue while playing
    controller.notifyVideoPreviewPlaying()
    assert controller.playerState == "stopped"
    assert player.state == "stopped"

    # 7. videoReleaseRequested fires before disk operations: exportVideo, clearVideo,
    #    openProject, removeProject, and clear.
    mux_saw_release: list[bool] = []

    def _fake_mux(*_args, **_kwargs):
        mux_saw_release.append(bool(releases))
        return tmp_path / "out.mp4"

    monkeypatch.setattr("vienetts_app.ui.subtitle_controller.mux_dubbed_video", _fake_mux)
    releases.clear()
    controller.exportVideo(str(tmp_path / "exports"))
    assert mux_saw_release == [True]
    assert len(releases) == 1

    releases.clear()
    controller.clearVideo()
    assert len(releases) == 1
    assert controller.hasVideo is False
    assert controller.videoUrl == ""

    # Re-attach video so project has video on disk, then test openProject, removeProject, clear
    assert controller.attachVideo(str(video)) is True
    proj_id = controller.projectId
    releases.clear()
    assert controller.openProject(proj_id) is True
    assert len(releases) == 1

    releases.clear()
    controller.clear()
    assert len(releases) == 1
    assert controller.videoUrl == ""

    releases.clear()
    assert controller.removeProject(proj_id) is True
    assert len(releases) >= 1
