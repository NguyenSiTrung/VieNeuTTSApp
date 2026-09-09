"""Studio replay transport: pause keeps position, waveform seeks (regression).

Dừng used to be a full stop-and-rewind, and the waveform ignored clicks.
The transport now pauses/resumes around the kept playhead and the Studio
waveform seeks through the shared file player.
"""

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QObject

from vienetts_app.core.artifacts import SynthesisArtifact
from vienetts_app.core.audio import write_wav_file
from vienetts_app.ui.bg_ops import run_sync
from vienetts_app.ui.controller import AppController


def _tone(n=9600, freq=440.0):
    t = np.arange(n, dtype=np.float32) / 48_000
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class PausablePlayback(QObject):
    def __init__(self):
        super().__init__()
        self.played: list[str] = []
        self.stops = 0
        self.pauses = 0
        self.resumes = 0
        self.seeks: list[int] = []

    def play(self, path):
        self.played.append(str(path))
        return True

    def stop(self):
        self.stops += 1

    def pause(self):
        self.pauses += 1

    def resume(self):
        self.resumes += 1

    def seek(self, ms):
        self.seeks.append(int(ms))


class PlayOnlyPlayback(QObject):
    """Legacy fake without pause/resume/seek (degrades gracefully)."""

    def __init__(self):
        super().__init__()
        self.played: list[str] = []

    def play(self, path):
        self.played.append(str(path))
        return True

    def stop(self):
        pass


def _make_controller(tmp_path, playback):
    c = AppController(
        data_dir=tmp_path,
        engine_factory=lambda **kwargs: None,
        worker_factory=lambda engine: None,
        catalog=lambda: [],
        saved_names=lambda voices_dir: [],
        bg_runner=run_sync,
        audio_probe=lambda: True,
    )
    c.attach_file_playback(playback)
    wav = write_wav_file(_tone(), tmp_path / "art.wav")
    c._current_artifact = SynthesisArtifact(
        job_id="a" * 32,
        path=wav,
        sample_rate=48_000,
        samples=9600,
        duration_ms=200,
    )
    assert c.openInStudio("text", "hello") is True
    assert c.studioPreview() is True
    assert c.replayActive is True
    return c


def test_pause_keeps_position_and_resume_continues(qcoreapp, tmp_path):
    player = PausablePlayback()
    c = _make_controller(tmp_path, player)

    assert c.seekReplay(0.5) is True
    assert player.seeks == [100]
    assert c.replayPosition == pytest.approx(0.5)

    c.pauseReplay()
    assert c.replayPaused is True
    assert c.replayActive is True  # still lit; playhead frozen, not parked
    assert c.replayPosition == pytest.approx(0.5)
    assert player.pauses == 1

    c.resumeReplay()
    assert c.replayPaused is False
    assert c.replayActive is True
    assert player.resumes == 1
    assert c.replayPosition == pytest.approx(0.5)


def test_stop_parks_playhead_and_clears_paused(qcoreapp, tmp_path):
    player = PausablePlayback()
    c = _make_controller(tmp_path, player)

    c.seekReplay(0.5)
    c.pauseReplay()
    assert c.replayPaused is True
    c.stopReplay()
    assert c.replayActive is False
    assert c.replayPaused is False
    assert c.replayPosition == 0.0
    assert player.stops == 1


def test_seek_clamps_and_noops_while_idle(qcoreapp, tmp_path):
    player = PausablePlayback()
    c = _make_controller(tmp_path, player)

    assert c.seekReplay(2.0) is True
    assert player.seeks == [200]
    assert c.replayPosition == pytest.approx(1.0)
    assert c.seekReplay(-1.0) is True
    assert player.seeks == [200, 0]

    c.stopReplay()
    assert c.seekReplay(0.5) is False
    assert player.seeks == [200, 0]


def test_pause_noop_without_pause_capable_player(qcoreapp, tmp_path):
    c = _make_controller(tmp_path, PlayOnlyPlayback())

    c.pauseReplay()  # legacy player: must not pretend to pause
    assert c.replayPaused is False
    assert c.replayActive is True
    c.resumeReplay()
    assert c.replayActive is True


def test_pause_resume_noop_while_idle(qcoreapp, tmp_path):
    player = PausablePlayback()
    c = _make_controller(tmp_path, player)
    c.stopReplay()

    c.pauseReplay()
    c.resumeReplay()
    assert c.replayPaused is False
    assert player.pauses == 0 and player.resumes == 0


def test_end_of_media_clears_paused(qcoreapp, tmp_path):
    player = PausablePlayback()
    c = _make_controller(tmp_path, player)

    c.seekReplay(0.5)
    c.pauseReplay()
    c._on_file_replay_finished()
    assert c.replayActive is False
    assert c.replayPaused is False
    assert c.replayPosition == 0.0
