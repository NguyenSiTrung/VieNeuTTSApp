"""ui/subtitle_controller.py: the QML-facing SRT dub/transcript studio.

The controller drives synthesis through AppController's listener seam, so these
tests use a fake app that records submissions and a fake player that records
transport calls: render, karaoke highlight, policy invalidation and export are
all exercised without an engine or an audio device.
"""

from __future__ import annotations

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
        "vienetts_app.ui.subtitle_controller.transcribe_media_to_cues",
        lambda path, cfg, **kw: [
            Cue(1, 0, 1500, "Xin chào bạn."),
            Cue(2, 1700, 3200, "Chào buổi sáng."),
        ],
    )
    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.diarize_cues",
        lambda path, cues, cfg=None, **kw: [
            Cue(1, 0, 1500, "Xin chào bạn.", speaker="SPEAKER_01"),
            Cue(2, 1700, 3200, "Chào buổi sáng.", speaker="SPEAKER_02"),
        ],
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


def test_transcribe_keeps_asr_result_when_diarization_crashes(env, tmp_path, monkeypatch):
    from vienetts_app.core.subtitles import Cue

    controller, _, _ = env
    video = tmp_path / "talk.mp4"
    video.write_bytes(b"fake_mp4")
    assert controller.importVideo(str(video)) is True
    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.transcribe_media_to_cues",
        lambda path, cfg, **kw: [Cue(1, 0, 1500, "Xin chào."), Cue(2, 1700, 3200, "Tạm biệt.")],
    )

    def _explode(path, cues, cfg=None, **kw):
        raise RuntimeError("model exploded")

    monkeypatch.setattr("vienetts_app.ui.subtitle_controller.diarize_cues", _explode)
    assert controller.transcribeVideo(True) is True
    assert controller.loaded is True
    assert controller.cueCount == 2
    assert "model exploded" in controller.errorText


def test_transcribe_surfaces_acoustic_fallback_warning(env, tmp_path, monkeypatch):
    from vienetts_app.core.subtitles import Cue

    controller, _, _ = env
    video = tmp_path / "talk2.mp4"
    video.write_bytes(b"fake_mp4")
    assert controller.importVideo(str(video)) is True
    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.transcribe_media_to_cues",
        lambda path, cfg, **kw: [Cue(1, 0, 1500, "Xin chào.")],
    )

    def _fallback(path, cues, cfg=None, notes=None, **kw):
        assert notes is not None
        notes.append("Nhãn người nói được tạo bằng phương pháp dự phòng (âm học).")
        return [Cue(1, 0, 1500, "Xin chào.", speaker="SPEAKER_01")]

    monkeypatch.setattr("vienetts_app.ui.subtitle_controller.diarize_cues", _fallback)
    assert controller.transcribeVideo(True) is True
    assert "dự phòng" in controller.errorText
    assert controller.noticeLevel == "warning"


def test_rediarization_remaps_labels_to_existing_speaker_voices(env, tmp_path, monkeypatch):
    from vienetts_app.core.subtitles import Cue

    controller, _, _ = env
    video = tmp_path / "interview.mp4"
    video.write_bytes(b"fake_mp4")
    assert controller.importVideo(str(video)) is True
    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.transcribe_media_to_cues",
        lambda path, cfg, **kw: [Cue(1, 0, 1500, "Một."), Cue(2, 1700, 3200, "Hai.")],
    )
    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.diarize_cues",
        lambda path, cues, cfg=None, **kw: [
            Cue(1, 0, 1500, "Một.", speaker="SPEAKER_01"),
            Cue(2, 1700, 3200, "Hai.", speaker="SPEAKER_02"),
        ],
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


def test_asr_config_reads_device_and_threads_settings(env):
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

    def _fake_transcribe(path, cfg, *, work_dir=None, notes=None, **kw):
        assert work_dir is not None
        seen_work_dirs.append(Path(work_dir))
        (Path(work_dir) / "source_16k.wav").write_bytes(b"cached_wav")
        if notes is not None:
            notes.append("ASR dự phòng đã kích hoạt.")
        return [Cue(1, 0, 1500, f"Phụ đề từ {Path(path).parent.name}")]

    monkeypatch.setattr(
        "vienetts_app.ui.subtitle_controller.transcribe_media_to_cues",
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


def test_glossary_survives_reimport_and_edits_during_render(env, tmp_path):
    controller, fake, _ = env
    path = load_srt(tmp_path)
    assert controller.importSrt(path)
    controller.setLlmGlossary("saved")
    assert controller.importSrt(path)
    assert controller.llmGlossary == "saved"
    controller.render()
    assert controller.rendering
    controller.setLlmGlossary("edited while rendering")
    while fake.pending:
        complete_next_unit(fake, tmp_path)
    assert controller.rendered
    assert controller.llmGlossary == "edited while rendering"
    assert controller._store.require(controller.projectId).llm_glossary == controller.llmGlossary  # noqa: SLF001


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

    def _transcribe(path, cfg, **kw):
        calls.append(str(path))
        return [Cue(1, 0, 1500, "Bản gốc từ ASR")]

    monkeypatch.setattr("vienetts_app.ui.subtitle_controller.transcribe_media_to_cues", _transcribe)
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


def test_project_lifecycle_lists_opens_and_removes(tmp_path):
    controller, fake, _ = _rendered_controller(tmp_path)
    first_id = controller.projectId
    second = tmp_path / "other.srt"
    second.write_text("1\n00:00:01,000 --> 00:00:02,000\nNội dung khác\n", encoding="utf-8")
    assert controller.importSrt(str(second)) is True
    assert controller.projectId != first_id
    assert controller.projectCount == 2
    rows = controller.projects
    ids = {row["id"] for row in rows}
    assert ids == {first_id, controller.projectId}
    assert {row["title"] for row in rows} == {"movie", "other"}
    assert all(row["modifiedAt"] for row in rows)

    assert controller.openProject(first_id) is True
    assert controller.projectId == first_id
    assert controller.title == "movie"
    assert controller.rendered is True
    assert controller.cueCount == 2
    assert controller.mode == "dub"

    assert controller.removeProject(first_id) is True
    assert controller.loaded is False
    assert controller.projectCount == 1
    assert [row["id"] for row in controller.projects] == [controller.projects[0]["id"]]


def test_removing_the_active_project_returns_to_the_empty_state(tmp_path):
    controller, _, _ = _rendered_controller(tmp_path)
    pid = controller.projectId
    assert controller.removeProject(pid) is True
    assert controller.loaded is False
    assert controller.cueCount == 0
    assert controller.projectCount == 0


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


def test_cancel_export_unlatches_and_ignores_the_stale_completion(tmp_path):
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
    events: list[tuple[str, str]] = []
    controller.exportFinished.connect(lambda path, error: events.append((path, error)))

    target = controller.exportTrack(str(tmp_path / "out"))
    assert target != ""
    assert controller.exporting is True
    assert len(runner.calls) == 1

    controller.cancelExport()
    assert controller.exporting is False
    assert events == [("", "Đã hủy xuất.")]

    # The worker finishing later must not emit a second result or re-latch.
    work, done, _on_error = runner.calls[0]
    done(work())
    assert events == [("", "Đã hủy xuất.")]
    assert controller.exporting is False

    # Cancelling while idle is a no-op.
    controller.cancelExport()
    assert events == [("", "Đã hủy xuất.")]


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

    def transcribe(path, cfg, *, progress_cb, **kw):
        progress_cb(0.5, "Đang nhận diện…")
        return [Cue(1, 0, 1500, "Xin chào")]

    def diarize(path, cues, cfg=None, *, progress_cb, **kw):
        progress_cb(0.5)
        return [replace(cue, speaker="SPEAKER_01") for cue in cues]

    monkeypatch.setattr("vienetts_app.ui.subtitle_controller.transcribe_media_to_cues", transcribe)
    monkeypatch.setattr("vienetts_app.ui.subtitle_controller.diarize_cues", diarize)

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

    def extract(vpath, cues, spk, out, **kw):
        spoken = [cue for cue in cues if cue.speaker == spk]
        extracted.append((spoken[0].start_ms, spoken[0].end_ms))
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

    # A newer playback request supersedes a pending audition …
    video.write_bytes(b"fake_mp4, edited again")
    assert controller.playSpeakerSample("SPEAKER_01") is True
    controller.stopPlay()
    finish(runner.calls[-1])
    assert len(player.played) == 4
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


def test_video_overrun_is_probed_once_per_render(tmp_path, monkeypatch):
    calls: list[int] = []

    def _fake_overrun(*args, **kwargs):
        calls.append(1)
        return 4200

    monkeypatch.setattr("vienetts_app.ui.subtitle_controller.dub_overrun_ms", _fake_overrun)
    controller, _, _ = _rendered_controller(tmp_path)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"fake_mp4")
    controller._project = replace(controller._project, video_path=str(video))  # noqa: SLF001
    controller.videoChanged.emit()

    assert controller.videoOverrunMs == 4200
    assert controller.videoOverrunMs == 4200
    assert calls == [1]

    # The two events that can change the answer each drop the cache.
    controller.renderedChanged.emit()
    assert controller.videoOverrunMs == 4200
    controller.videoChanged.emit()
    assert controller.videoOverrunMs == 4200
    assert calls == [1, 1, 1]


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


def test_video_speech_bounds_refinement_and_uniform_unit_render(env, tmp_path: Path) -> None:
    from vienetts_app.core.audio import write_wav_file

    controller, fake, _ = env
    video = tmp_path / "speech_lesson.mp4"
    video.write_bytes(b"fake_video")
    srt = tmp_path / "speech_lesson.srt"
    srt.write_text(
        "1\n00:00:00,000 --> 00:00:01,500\nMệnh đề đầu tiên,\n\n"
        "2\n00:00:01,600 --> 00:00:03,000\nmệnh đề thứ hai.\n",
        encoding="utf-8",
    )

    # Populate cached 16 kHz WAV with active speech only in 200..1100 ms and 1700..2700 ms
    cache_dir = controller._media_cache_dir(video)  # noqa: SLF001
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
    write_wav_file(wav_16k, cache_dir / "source_16k.wav", sample_rate=sr16)

    assert controller.importVideo(str(video)) is True
    assert controller.loaded is True
    proj_cues = controller._project.cues  # noqa: SLF001
    assert proj_cues[0].speech_start_ms is not None and 150 <= proj_cues[0].speech_start_ms <= 220
    assert proj_cues[0].speech_end_ms is not None and 1080 <= proj_cues[0].speech_end_ms <= 1150
    assert proj_cues[1].speech_start_ms is not None and 1650 <= proj_cues[1].speech_start_ms <= 1720
    assert proj_cues[1].speech_end_ms is not None and 2680 <= proj_cues[1].speech_end_ms <= 2750

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
