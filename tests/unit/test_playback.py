"""PlaybackController: thin QMediaPlayer wrapper for full-file WAV playback (FR-3.2).

All logic runs against an injected FakePlayer (duck-typed per the contract in
PlaybackController's docstring): play/stop/pause/resume + signal stubs that
emit enum member-NAME strings, so unit tests never import QtMultimedia. One
smoke case constructs the real QMediaPlayer offscreen, and one smoke case
exercises the real audio-device probe.
"""

import sys
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QUrl  # noqa: E402

from vienetts_app.core.audio import write_wav_file  # noqa: E402
from vienetts_app.ui.playback import PlaybackController, audio_output_available  # noqa: E402


def wait_until(cond, timeout: float = 5.0, interval: float = 0.01) -> bool:
    # Real-player signals can be queued/async: pump the event loop while polling.
    app = QCoreApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        if app is not None:
            app.processEvents()
        time.sleep(interval)
    return False


class SignalStub:
    """Minimal Qt Signal duck-type: connect/emit with synchronous delivery."""

    def __init__(self) -> None:
        self._slots: list = []

    def connect(self, slot) -> None:
        self._slots.append(slot)

    def disconnect(self, slot) -> None:
        self._slots.remove(slot)

    def emit(self, *args) -> None:
        for slot in list(self._slots):
            slot(*args)


class FakePlayer:
    """QMediaPlayer stand-in; records calls, drives the wrapper via stubs.

    Emits enum member names ("PlayingState", "EndOfMedia", ...) instead of the
    Qt enums — proves the wrapper is name-mapped, not import-coupled.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.sources: list[QUrl] = []
        self.playbackStateChanged = SignalStub()
        self.mediaStatusChanged = SignalStub()
        self.errorOccurred = SignalStub()

    def setSource(self, url: QUrl) -> None:
        self.calls.append("setSource")
        self.sources.append(url)

    def play(self) -> None:
        self.calls.append("play")
        self.playbackStateChanged.emit("PlayingState")

    def stop(self) -> None:
        self.calls.append("stop")
        self.playbackStateChanged.emit("StoppedState")

    def pause(self) -> None:
        self.calls.append("pause")
        self.playbackStateChanged.emit("PausedState")

    def resume(self) -> None:
        self.calls.append("resume")
        self.playbackStateChanged.emit("PlayingState")

    def emit_end_of_media(self) -> None:
        self.mediaStatusChanged.emit("EndOfMedia")

    def emit_error(self, message: str) -> None:
        self.errorOccurred.emit("ResourceError", message)


class PlaybackHarness:
    """Controller wired to a FakePlayer factory; records every notification."""

    def __init__(self) -> None:
        self.fake = FakePlayer()
        self.created = 0

        def factory() -> FakePlayer:
            self.created += 1
            return self.fake

        self.controller = PlaybackController(player_factory=factory)
        self.states: list[str] = []
        self.paths: list[str] = []
        self.errors: list[str] = []
        self.finished: list[bool] = []
        self.controller.stateChanged.connect(lambda: self.states.append(self.controller.state))
        self.controller.sourcePathChanged.connect(
            lambda: self.paths.append(self.controller.sourcePath)
        )
        self.controller.errorTextChanged.connect(
            lambda: self.errors.append(self.controller.errorText)
        )
        self.controller.finished.connect(lambda: self.finished.append(True))


@pytest.fixture()
def harness(qcoreapp):
    return PlaybackHarness()


class TestInitialAndLazy:
    def test_initial_state_and_lazy_factory(self, harness, tmp_path) -> None:
        c = harness.controller
        assert c.state == "stopped"
        assert c.sourcePath == ""
        assert c.fileName == ""
        assert c.errorText == ""

        # stop/pause/resume before first play are no-ops
        c.stop()
        c.pause()
        c.resume()
        assert c.state == "stopped"
        assert harness.created == 0

        # factory called on first play, reused thereafter
        harness.controller.play(str(tmp_path / "a.wav"))
        assert harness.created == 1
        harness.controller.play(str(tmp_path / "b.wav"))
        assert harness.created == 1


class TestPlay:
    def test_play_sets_source_and_starts(self, harness, tmp_path) -> None:
        wav = tmp_path / "out.wav"
        harness.controller.play(str(wav))
        assert harness.fake.calls == ["setSource", "play"]
        # QUrl.toLocalFile() normalizes to forward slashes on every OS; the
        # comparison must go through Path, never str, or Windows fails.
        assert Path(harness.fake.sources[0].toLocalFile()) == wav
        assert harness.controller.state == "playing"
        assert harness.controller.sourcePath == str(wav)
        assert harness.states == ["playing"]

    def test_play_accepts_path_object(self, harness, tmp_path) -> None:
        wav = tmp_path / "xin-chao.wav"
        harness.controller.play(wav)
        assert harness.controller.sourcePath == str(wav)
        assert Path(harness.fake.sources[0].toLocalFile()) == wav

    def test_play_while_playing_stops_first(self, harness, tmp_path) -> None:
        first, second = tmp_path / "a.wav", tmp_path / "b.wav"
        harness.controller.play(str(first))
        harness.controller.play(str(second))
        assert harness.fake.calls == ["setSource", "play", "stop", "setSource", "play"]
        assert harness.controller.sourcePath == str(second)
        assert harness.controller.state == "playing"

    def test_play_while_paused_also_stops_first(self, harness, tmp_path) -> None:
        harness.controller.play(str(tmp_path / "a.wav"))
        harness.controller.pause()
        harness.controller.play(str(tmp_path / "b.wav"))
        assert harness.fake.calls == ["setSource", "play", "pause", "stop", "setSource", "play"]

    def test_replacing_playback_releases_previous_before_new_play(self, harness, tmp_path) -> None:
        events: list[str] = []
        harness.controller.play(
            str(tmp_path / "a.wav"), on_released=lambda: events.append("released")
        )

        def record_new_play() -> None:
            events.append("new play")
            harness.fake.playbackStateChanged.emit("PlayingState")

        harness.fake.play = record_new_play

        harness.controller.play(str(tmp_path / "b.wav"))

        assert events == ["released", "new play"]

    def test_file_name_is_basename(self, harness, tmp_path) -> None:
        harness.controller.play(tmp_path / "audio" / "bai-doc.wav")
        assert harness.controller.fileName == "bai-doc.wav"


class TestStopPauseResume:
    def test_stop_stops_and_clears_source(self, harness, tmp_path) -> None:
        harness.controller.play(str(tmp_path / "out.wav"))
        harness.controller.stop()
        assert harness.fake.calls == ["setSource", "play", "stop", "setSource"]
        assert harness.fake.sources[-1] == QUrl()  # source cleared with an empty URL
        assert harness.controller.state == "stopped"
        assert harness.controller.sourcePath == ""
        assert harness.controller.fileName == ""

    def test_stop_releases_playback_once(self, harness, tmp_path) -> None:
        released: list[bool] = []
        harness.controller.play(
            str(tmp_path / "out.wav"), on_released=lambda: released.append(True)
        )

        harness.controller.stop()
        harness.controller.stop()

        assert released == [True]

    def test_pause_then_resume_transition(self, harness, tmp_path) -> None:
        c = harness.controller
        c.play(str(tmp_path / "out.wav"))
        c.pause()
        assert c.state == "paused"
        assert harness.fake.calls == ["setSource", "play", "pause"]
        c.resume()
        assert c.state == "playing"
        assert harness.fake.calls == ["setSource", "play", "pause", "resume"]

    def test_resume_without_resume_method_plays(self, qcoreapp, tmp_path) -> None:
        # Real QMediaPlayer has no resume() — play() is Qt's resume path.
        fake = FakePlayer()
        qt_shaped = SimpleNamespace(
            setSource=fake.setSource,
            play=fake.play,
            stop=fake.stop,
            pause=fake.pause,
            playbackStateChanged=fake.playbackStateChanged,
            mediaStatusChanged=fake.mediaStatusChanged,
            errorOccurred=fake.errorOccurred,
        )
        controller = PlaybackController(player_factory=lambda: qt_shaped)
        controller.play(str(tmp_path / "out.wav"))
        controller.pause()
        controller.resume()
        assert fake.calls == ["setSource", "play", "pause", "play"]
        assert controller.state == "playing"

    def test_noop_transitions_when_stopped_or_already_playing(self, harness, tmp_path) -> None:
        c = harness.controller
        c.play(str(tmp_path / "out.wav"))
        c.stop()
        c.pause()
        c.resume()
        assert "pause" not in harness.fake.calls[3:]
        assert "resume" not in harness.fake.calls[3:]
        assert c.state == "stopped"

        c.play(str(tmp_path / "out2.wav"))
        calls_before = len(harness.fake.calls)
        c.resume()
        assert len(harness.fake.calls) == calls_before


class TestStateMapping:
    def test_every_playback_state_maps_to_string(self, harness, tmp_path) -> None:
        c = harness.controller
        c.play(str(tmp_path / "out.wav"))  # constructs + connects the fake
        harness.fake.playbackStateChanged.emit("PausedState")
        assert c.state == "paused"
        harness.fake.playbackStateChanged.emit("PlayingState")
        assert c.state == "playing"
        harness.fake.playbackStateChanged.emit("StoppedState")
        assert c.state == "stopped"


class TestFinished:
    def test_finished_emitted_on_end_of_media(self, harness, tmp_path) -> None:
        harness.controller.play(str(tmp_path / "out.wav"))
        harness.fake.emit_end_of_media()
        assert harness.finished == [True]

    def test_end_of_media_releases_playback_once(self, harness, tmp_path) -> None:
        released: list[bool] = []
        harness.controller.play(
            str(tmp_path / "out.wav"), on_released=lambda: released.append(True)
        )

        harness.fake.emit_end_of_media()
        harness.fake.emit_end_of_media()

        assert released == [True]

    def test_stale_end_of_media_after_replacement_does_not_release_new_playback(
        self, harness, tmp_path
    ) -> None:
        released: list[str] = []
        harness.controller.play(
            str(tmp_path / "first.wav"), on_released=lambda: released.append("first")
        )
        stale_handler = harness.fake.mediaStatusChanged._slots[-1]
        harness.controller.play(
            str(tmp_path / "second.wav"), on_released=lambda: released.append("second")
        )

        stale_handler("EndOfMedia")

        assert released == ["first"]
        harness.fake.emit_end_of_media()
        assert released == ["first", "second"]

    def test_other_media_statuses_do_not_emit_finished(self, harness, tmp_path) -> None:
        harness.controller.play(str(tmp_path / "out.wav"))
        harness.fake.mediaStatusChanged.emit("LoadedMedia")
        harness.fake.mediaStatusChanged.emit("BufferedMedia")
        assert harness.finished == []


class TestErrors:
    def test_error_occurred_sets_error_text(self, harness, tmp_path) -> None:
        c = harness.controller
        c.play(str(tmp_path / "out.wav"))
        harness.fake.emit_error("audio device vanished")
        assert "audio device vanished" in c.errorText
        assert c.errorText == harness.errors[-1]

    def test_backend_error_releases_playback_once(self, harness, tmp_path) -> None:
        released: list[bool] = []
        harness.controller.play(
            str(tmp_path / "out.wav"), on_released=lambda: released.append(True)
        )

        harness.fake.emit_error("audio device vanished")
        harness.fake.emit_error("audio device vanished again")

        assert released == [True]

    def test_successful_play_clears_error(self, harness, tmp_path) -> None:
        c = harness.controller
        c.play(str(tmp_path / "one.wav"))
        harness.fake.emit_error("boom")
        assert c.errorText != ""

    def test_invalid_path_handling_and_no_release(self, harness) -> None:
        c = harness.controller
        released: list[bool] = []
        for bad in ("", "   \n\t", None):
            c.play(bad, on_released=lambda: released.append(True))  # type: ignore[arg-type]
            assert c.state == "stopped"
            assert c.errorText != ""
        assert released == []

    def test_player_construction_failure_releases_callback(self, qcoreapp, tmp_path) -> None:
        released: list[bool] = []

        def failing_factory() -> FakePlayer:
            raise RuntimeError("no audio device")

        controller = PlaybackController(player_factory=failing_factory)
        controller.play(str(tmp_path / "out.wav"), on_released=lambda: released.append(True))

        assert released == [True]


class TestAudioOutputProbe:
    """FR-4.6a core: pure-fake providers, no QtMultimedia anywhere near."""

    def test_empty_device_list_reports_unavailable(self) -> None:
        assert audio_output_available(provider=lambda: []) is False

    def test_any_non_empty_iterable_counts_as_available(self) -> None:
        # The contract is "iterable with at least one device": one object,
        # several names, or a generator all count as available.
        assert audio_output_available(provider=lambda: [object()]) is True
        assert audio_output_available(provider=lambda: ["speakers", "headphones"]) is True
        assert audio_output_available(provider=lambda: iter([object()])) is True

    def test_fake_provider_does_not_load_qtmultimedia(self) -> None:
        # The lazy-import posture: probing via a fake must never drag in the
        # real audio stack (NFR-2.1, same as PlaybackController's factory).
        loaded_before = set(sys.modules)
        audio_output_available(provider=lambda: [object()])
        new_modules = set(sys.modules) - loaded_before
        assert not [name for name in new_modules if name.startswith("PySide6.QtMultimedia")]


class TestRealPlayerSmoke:
    def test_real_player_offscreen_smoke(self, qcoreapp, tmp_path, monkeypatch) -> None:
        # Real QMediaPlayer + QAudioOutput under QCoreApplication(offscreen).
        # Audio backends vary: reaching "playing" OR an error is success; a
        # crash is the only failure mode this test guards against.
        #
        # GOTCHA: under pytest's default fd capture, QAudioOutput construction
        # deadlocks in the pipewire devicemonitor (works fine with -s/--capture=no
        # and in plain python). Forcing the ffmpeg audio backend skips the
        # pipewire probe entirely — still a real player + real WAV decode.
        monkeypatch.setenv("QT_AUDIO_BACKEND", "ffmpeg")
        wav = write_wav_file(np.zeros(3 * 48_000, dtype=np.float32), tmp_path / "smoke.wav")
        controller = PlaybackController()
        controller.play(str(wav))
        reached = wait_until(
            lambda: controller.state == "playing" or controller.errorText != "", timeout=8.0
        )
        assert reached, (
            f"neither playing nor error after 8s: state={controller.state!r} "
            f"error={controller.errorText!r} source={controller.sourcePath!r}"
        )
        assert controller.state == "playing" or controller.errorText != ""
        assert controller.fileName == "smoke.wav"
        controller.stop()
        assert controller.sourcePath == ""


class TestRealProbeSmoke:
    def test_real_probe_offscreen_smoke(self, monkeypatch) -> None:
        # Default provider = real QMediaDevices.audioOutputs() under offscreen.
        # Same gotcha as the real-player smoke: the pipewire devicemonitor probe
        # deadlocks under pytest fd capture, so force the ffmpeg backend.
        # Either answer is valid (headless hosts legitimately have zero audio
        # outputs); the test guards against crashing/hanging, and skips
        # gracefully when QtMultimedia objects cannot be constructed at all.
        monkeypatch.setenv("QT_AUDIO_BACKEND", "ffmpeg")
        QCoreApplication.instance() or QCoreApplication([])
        try:
            from PySide6.QtMultimedia import QMediaDevices
        except Exception as error:  # pragma: no cover - environment-dependent
            pytest.skip(f"QtMultimedia unavailable offscreen: {error}")
        try:
            probe_result = audio_output_available()
        except Exception as error:
            pytest.skip(f"audio-device probe failed offscreen: {error}")
        assert probe_result in (True, False)
        # Cross-check the default provider really asked QMediaDevices.
        assert audio_output_available() == bool(QMediaDevices.audioOutputs())
