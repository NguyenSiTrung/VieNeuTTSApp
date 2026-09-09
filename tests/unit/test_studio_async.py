"""Studio Apply must not block the GUI thread on long mixes (regression).

Every op push used to call ``render_project`` twice (mix + envelope),
synchronously inside the QML slot — seconds of frozen UI on long audio.
The overview now renders once, off the GUI thread, behind ``studioBusy``.
"""

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject, Signal

from vienetts_app.core.artifacts import SynthesisArtifact
from vienetts_app.core.audio import read_wav, write_wav_file
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


class DeferredBg:
    """Test runner: queue jobs; the test drives completion explicitly."""

    def __init__(self):
        self.jobs: list[tuple] = []

    def __call__(self, work, on_done, parent, *, on_error=None):
        self.jobs.append((work, on_done, on_error))

    def run_all(self):
        while self.jobs:
            self.run_one(0)

    def run_one(self, index=0):
        work, on_done, on_error = self.jobs.pop(index)
        try:
            result = work()
        except Exception as exc:  # noqa: BLE001 - mirror bg_ops delivery
            if on_error is not None:
                on_error(exc)
                return
            raise
        on_done(result)


def _make_controller(tmp_path, bg):
    c = AppController(
        data_dir=tmp_path,
        engine_factory=lambda **kwargs: FakeEngine(**kwargs),
        worker_factory=lambda engine: FakeWorker(engine),
        catalog=lambda: [],
        saved_names=lambda voices_dir: [],
        bg_runner=bg,
        audio_probe=lambda: True,
    )
    c.attach_file_playback(FakePlayback())
    wav = write_wav_file(_tone(), tmp_path / "art.wav")
    c._current_artifact = SynthesisArtifact(
        job_id="a" * 32,
        path=wav,
        sample_rate=48_000,
        samples=9600,
        duration_ms=200,
    )
    return c


def _open_settled(qcoreapp, tmp_path, bg):
    c = _make_controller(tmp_path, bg)
    assert c.openInStudio("text", "first\n\nsecond") is True
    assert c.studioBusy is True  # overview render queued, GUI never blocked
    bg.run_all()
    assert c.studioBusy is False
    assert c.studioDurationMs > 0
    return c


def test_render_overview_matches_render_project():
    from vienetts_app.core.studio import (
        GainOp,
        StudioClip,
        StudioProject,
        push_op,
        render_overview,
        render_project,
    )

    project = push_op(
        StudioProject(
            clips=(StudioClip(id="c0", label="1", text="hi", audio=_tone()),),
            ops=(),
        ),
        GainOp(db=3.0),
    )
    mix, duration_ms, envelope = render_overview(project)
    assert np.array_equal(mix, render_project(project))
    assert duration_ms == int(len(mix) * 1000 / 48_000)
    assert len(envelope) == 160
    assert all(0.0 <= v <= 1.0 for v in envelope)


def test_push_publishes_ops_instantly_and_settles_off_thread(qcoreapp, tmp_path):
    bg = DeferredBg()
    c = _open_settled(qcoreapp, tmp_path, bg)

    assert c.studioPushGain(3.0) is True
    # Slot returned without rendering: ops visible, overview pending.
    assert len(c.studioOps) == 1
    assert c.studioBusy is True
    assert len(bg.jobs) == 1

    bg.run_all()
    assert c.studioBusy is False
    assert c.studioDurationMs > 0
    assert len(c.studioEnvelope) == 160


def test_stale_overview_result_dropped(qcoreapp, tmp_path):
    from vienetts_app.core.studio import render_overview

    bg = DeferredBg()
    c = _open_settled(qcoreapp, tmp_path, bg)

    assert c.studioPushGain(3.0) is True
    assert c.studioBusyKind == "gain"
    assert c.studioPushSpeed(0.5) is True
    assert c.studioBusyKind == "speed"
    assert len(bg.jobs) == 2
    # Newest render lands first, then the stale one.
    bg.run_one(1)
    # The stale gain render is still in flight, but the data is already final.
    assert c.studioBusy is True
    _, expected_ms, expected_env = render_overview(c._studio_project)
    assert c.studioDurationMs == expected_ms
    bg.run_one(0)
    assert c.studioBusy is False
    assert c.studioBusyKind == ""
    assert c.studioDurationMs == expected_ms
    assert list(c.studioEnvelope) == pytest.approx(expected_env)


def test_only_triggering_button_kind_spins(qcoreapp, tmp_path):
    bg = DeferredBg()
    c = _open_settled(qcoreapp, tmp_path, bg)
    assert c.studioBusyKind == ""

    assert c.studioPushGain(3.0) is True
    assert c.studioBusy is True
    assert c.studioBusyKind == "gain"
    bg.run_all()
    assert c.studioBusy is False
    assert c.studioBusyKind == ""


def test_preview_plays_after_off_thread_render(qcoreapp, tmp_path):
    bg = DeferredBg()
    c = _open_settled(qcoreapp, tmp_path, bg)

    assert c.studioPreview() is True
    assert c.studioBusy is True
    assert c.studioBusyKind == "preview"
    assert c.replayActive is False  # nothing to play until the render lands
    bg.run_all()
    assert c.studioBusy is False
    assert c.studioBusyKind == ""
    assert c.replayActive is True
    preview = tmp_path / "studio_preview.wav"
    assert preview.is_file()
    audio, sr = read_wav(preview)
    assert sr == 48_000 and len(audio) > 0


def test_preview_and_export_refused_while_render_in_flight(qcoreapp, tmp_path):
    bg = DeferredBg()
    c = _make_controller(tmp_path, bg)
    assert c.openInStudio("text", "first\n\nsecond") is True
    assert c.studioBusy is True  # open's overview still queued

    assert c.studioPreview() is False
    assert c.studioExport(str(tmp_path / "out.wav")) is False
    assert len(bg.jobs) == 1  # no extra renders queued behind it
    bg.run_all()
    assert c.studioBusy is False


def test_export_renders_and_writes_off_thread(qcoreapp, tmp_path):
    bg = DeferredBg()
    c = _open_settled(qcoreapp, tmp_path, bg)
    target = tmp_path / "studio_out.wav"

    assert c.studioExport(str(target)) is True
    assert c.exporting is True
    assert not target.is_file()  # render + copy happen in the queued job
    bg.run_all()
    assert c.exporting is False
    assert target.is_file()
    audio, sr = read_wav(target)
    assert sr == 48_000 and len(audio) > 0
