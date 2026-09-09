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
