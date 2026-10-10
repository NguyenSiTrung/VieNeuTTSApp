"""ui/subtitle_controller.py: the QML-facing SRT dub/transcript studio.

The controller drives synthesis through AppController's listener seam, so these
tests use a fake app that records submissions and a fake player that records
transport calls: render, karaoke highlight, policy invalidation and export are
all exercised without an engine or an audio device.
"""

from __future__ import annotations

import uuid
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from PySide6.QtCore import QObject, Signal

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
from vienetts_app.ui.bg_ops import SyncOrderedExecutor, run_sync
from vienetts_app.ui.subtitle_controller import SubtitleController

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
    tmp_path: Path, fake: FakeApp, player: FakePlayer, *, unit_executor: Any = None
) -> SubtitleController:
    return SubtitleController(
        fake,
        data_dir=tmp_path,
        player_factory=lambda: player,
        store_factory=lambda root: SubtitleProjectStore(Path(root) / "subtitles"),
        bg_runner=run_sync,
        unit_executor=SyncOrderedExecutor() if unit_executor is None else unit_executor,
    )


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


def test_import_refuses_a_missing_file(env, tmp_path):
    controller, _, _ = env
    assert controller.importSrt(str(tmp_path / "nope.srt")) is False
    assert "nope.srt" in controller.errorText


def test_import_refuses_a_cue_less_file(env, tmp_path):
    controller, _, _ = env
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


def test_policy_rebuild_keeps_the_cue_list_when_cues_are_unchanged(env, tmp_path):
    """Policy knobs re-plan timing, not the source cue list: no cuesChanged."""
    controller, _, _ = env
    controller.importSrt(load_srt(tmp_path))
    cues_before = controller.cues
    emitted: list[bool] = []
    controller.cuesChanged.connect(lambda: emitted.append(True))
    controller.rateCap = 1.2
    controller.rateCap = 1.35
    controller.offsetMs = 500
    assert controller.rendered is False  # the plan was rebuilt...
    assert emitted == []  # ...but the cue list QML renders did not change
    assert controller.cues == cues_before


def test_policy_round_trip_restores_the_cached_render(env, tmp_path):
    controller, fake, _ = env
    controller.importSrt(load_srt(tmp_path))
    render_all(controller, fake, tmp_path)
    duration = controller.durationMs
    adjusted = controller._project.adjusted  # noqa: SLF001
    fake.pending.clear()

    controller.rateCap = 1.2  # away: fingerprint changes
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
    assert controller.rendered is False
    assert controller._timeline is None  # noqa: SLF001
    controller.seekToCue(1)  # a stale timeline must not answer seeks
    assert player.seeks == []


def test_reimport_under_a_changed_policy_loads_no_stale_timeline(env, tmp_path):
    controller, fake, player = env
    path = load_srt(tmp_path)
    controller.importSrt(path)
    render_all(controller, fake, tmp_path)
    controller.rateCap = 1.2  # fingerprint moved away from the stored render

    assert controller.importSrt(path) is True
    assert controller.rendered is False
    assert controller._timeline is None  # noqa: SLF001
    controller.seekToCue(1)
    assert player.seeks == []


def test_transcript_mode_never_compresses(env, tmp_path):
    controller, fake, _ = env
    controller.importSrt(load_srt(tmp_path))
    controller.mode = "transcript"
    controller.mergeSentences = False
    render_all(controller, fake, tmp_path)
    assert controller.rendered is True
    project = controller._store.require(controller._project.id)  # noqa: SLF001
    assert project.stats.max_rate == 1.0


def test_render_failure_surfaces_an_error_and_clears_rendering(env, tmp_path):
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


def test_cancel_render_is_not_an_error(env, tmp_path):
    controller, fake, _ = env
    controller.importSrt(load_srt(tmp_path))
    controller.render()
    job_id = fake.pending[0][0]
    controller.cancelRender()
    assert fake.cancelled == [job_id]
    controller.on_synthesis_terminal(
        JobTerminal(job_id=job_id, owner="audiobook", state="cancelled")
    )
    assert controller.rendering is False
    assert controller.errorText == ""


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


def test_submit_exception_is_a_clean_render_failure(tmp_path):
    # If the engine seam raises inside submit, nothing may escape the slot —
    # the render fails cleanly and the partial track is aborted.

    class RaisingSubmitApp(FakeApp):
        def submit_stream_for_listener(self, text, voice, listener, *, kind="requested_chapter"):
            raise OSError("simulated submit failure")

    fake = RaisingSubmitApp()
    player = FakePlayer()
    controller = make_controller(tmp_path, fake, player)
    controller.importSrt(load_srt(tmp_path))
    controller.render()
    assert controller.rendering is False
    assert controller.errorText != ""
    assert controller.rendered is False
    assert fake.pending == []
    assert not list((tmp_path / "subtitles").glob("**/*.part.wav"))


def test_render_unit_split_failure_releases_and_resets(env, tmp_path, monkeypatch):
    controller, fake, _ = env
    controller.importSrt(load_srt(tmp_path))
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


def test_render_finish_failure_resets_state(env, tmp_path, monkeypatch):
    controller, fake, _ = env
    controller.importSrt(load_srt(tmp_path))
    controller.render()
    renderer = controller._renderer  # noqa: SLF001
    monkeypatch.setattr(
        renderer, "finish", lambda: (_ for _ in ()).throw(OSError("simulated finish failure"))
    )
    while fake.pending:
        complete_next_unit(fake, tmp_path)
    assert controller.rendering is False
    assert controller.errorText != ""
    assert controller.rendered is False


def test_finish_reload_failure_resets_state(env, tmp_path, monkeypatch):
    # The store reload after finish() sits inside the same failure boundary:
    # a raise there fails the render cleanly, and the already-finished
    # renderer must NOT be aborted — its track is live.
    controller, fake, _ = env
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
        complete_next_unit(fake, tmp_path)
    assert controller.rendering is False
    assert controller.errorText != ""
    assert aborts == []


def test_failed_terminal_releases_its_artifact(env, tmp_path):
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


def test_cancelled_terminal_releases_its_artifact(env, tmp_path):
    controller, fake, _ = env
    controller.importSrt(load_srt(tmp_path))
    controller.render()
    job_id, text, listener = fake.pending.pop(0)
    artifact, artifact_path = make_artifact(job_id, text, tmp_path)
    listener.on_synthesis_terminal(
        JobTerminal(job_id=job_id, owner="audiobook", state="cancelled", value=artifact)
    )
    assert controller.rendering is False
    assert controller.errorText == ""
    assert not artifact_path.exists()


def test_completed_terminal_with_mismatched_artifact_fails_cleanly(env, tmp_path):
    controller, fake, _ = env
    controller.importSrt(load_srt(tmp_path))
    controller.render()
    job_id, text, listener = fake.pending.pop(0)
    # The artifact belongs to a different job: it is invalid for this unit.
    artifact, artifact_path = make_artifact(uuid.uuid4().hex, text, tmp_path)
    listener.on_synthesis_terminal(
        JobTerminal(job_id=job_id, owner="audiobook", state="completed", value=artifact)
    )
    assert controller.rendering is False
    assert controller.errorText != ""
    assert not artifact_path.exists()


def test_fail_render_survives_an_abort_that_raises(env, tmp_path, monkeypatch):
    controller, fake, _ = env
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
    # Cleanup raising must not wedge the controller: state still resets.
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

    # ...and switching back re-renders too (the new track is the Qwen one).
    fake.contexts["voice-a"] = render_context()
    fake.submitted.clear()
    controller.render()
    assert len(fake.pending) == 1
    assert fake.submitted[0]["context"].profile == "vieneu"


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


def test_play_track_and_karaoke_highlight(env, tmp_path):
    controller, fake, player = env
    controller.importSrt(load_srt(tmp_path))
    render_all(controller, fake, tmp_path)
    controller.play()
    assert player.played[0].endswith("track.wav")

    player.positionChanged.emit(100)
    assert controller.activeCue == 0
    assert controller.activeCharStart >= 0

    player.positionChanged.emit(3_100)
    assert controller.activeCue == 1


def test_seek_to_cue_uses_the_measured_timeline(env, tmp_path):
    controller, fake, player = env
    controller.importSrt(load_srt(tmp_path))
    render_all(controller, fake, tmp_path)
    controller.seekToCue(1)
    assert player.seeks[-1] == 3_000
    controller.seekToCue(99)  # out of range: no-op
    assert player.seeks == [3_000]


def test_stop_playback_resets_the_highlight(env, tmp_path):
    controller, fake, player = env
    controller.importSrt(load_srt(tmp_path))
    render_all(controller, fake, tmp_path)
    controller.play()
    player.positionChanged.emit(100)
    assert controller.activeCue == 0
    controller.stopPlay()
    assert controller.activeCue == -1
    assert controller.playerState == "stopped"


def test_gap_position_clears_the_active_cue(env, tmp_path):
    controller, fake, player = env
    controller.importSrt(load_srt(tmp_path))
    render_all(controller, fake, tmp_path)
    controller.play()

    player.positionChanged.emit(100)
    assert controller.activeCue == 0
    # 800–3000 ms is rendered silence between the two cues: no highlight.
    player.positionChanged.emit(1_500)
    assert controller.activeCue == -1
    assert controller.activeCharStart == -1
    assert controller.activeCharEnd == -1


def test_active_transitions_do_not_rebuild_the_cue_model(env, tmp_path):
    controller, fake, player = env
    controller.importSrt(load_srt(tmp_path))
    render_all(controller, fake, tmp_path)
    counts = {"active": 0, "cues": 0}
    controller.activeCueChanged.connect(lambda: counts.__setitem__("active", counts["active"] + 1))
    controller.cuesChanged.connect(lambda: counts.__setitem__("cues", counts["cues"] + 1))
    model = controller.cues

    player.positionChanged.emit(100)  # cue 0 on
    player.positionChanged.emit(3_100)  # cue 1 on
    player.positionChanged.emit(1_500)  # gap: cue off

    assert counts == {"active": 3, "cues": 0}
    assert controller.cues is model  # identity stable across transitions


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
    controller.mode = "transcript"
    assert counts == {"policy": 2, "mode": 1, "merge": 1, "rate": 1, "gap": 0, "off": 0, "voice": 0}

    controller.mode = "transcript"  # no-op
    assert counts["policy"] == 2

    # Back and forth again: mergeSentences stayed True, so no second emit.
    controller.mode = "dub"
    controller.mode = "transcript"
    assert counts["merge"] == 1
    assert counts["mode"] == 3
    assert counts["policy"] == 4

    controller.maxGapMs = 500
    controller.offsetMs = 100
    controller.mergeSentences = False
    controller.voice = "voice-b"
    assert counts["policy"] == 8
    assert counts["gap"] == 1
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


def test_export_srt_writes_the_retimed_cues(env, tmp_path):
    controller, fake, _ = env
    controller.importSrt(load_srt(tmp_path))
    render_all(controller, fake, tmp_path)
    results: list[tuple[str, str]] = []
    controller.exportFinished.connect(lambda path, error: results.append((path, error)))

    target = controller.exportSrt(str(tmp_path / "out"))
    assert target.endswith(".srt")
    assert results == [(target, "")]
    assert controller.exporting is False  # run_sync completed inline
    exported = parse_cues(Path(target).read_text(encoding="utf-8"))
    project = controller._store.require(controller._project.id)  # noqa: SLF001
    assert exported == list(project.adjusted)
    assert exported[1].start_ms == 3_000


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
    assert Path(target).is_file()  # the file itself still landed

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
