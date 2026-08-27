"""StreamPlaybackController: QAudioSink fed by a ring buffer (FR-4.1, FR-4.5 groundwork).

All logic runs against an injected FakeSink duck-typed per the contract in
StreamPlaybackController's docstring: start(io)/stop()/state() plus an optional
stateChanged stub that emits enum member-NAME strings — unit tests never import
QtMultimedia. One smoke case constructs the real QAudioSink offscreen (with
QT_AUDIO_BACKEND=ffmpeg, following test_playback.py's pattern) and skips
gracefully when construction fails headless.
"""

from __future__ import annotations

import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QIODevice  # noqa: E402

from vienetts_app.ui.stream_playback import (  # noqa: E402
    AUDIO_PLAYBACK_UNAVAILABLE,
    STREAM_CHANNEL_COUNT,
    STREAM_SAMPLE_RATE,
    StreamPlaybackController,
    _make_stream_format,
)


def wait_until(cond, timeout: float = 3.0, interval: float = 0.01) -> bool:
    # Sink callbacks may be queued/async: pump the event loop while polling.
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

    def emit(self, *args) -> None:
        for slot in list(self._slots):
            slot(*args)


class FakeFormat:
    """Records the QAudioFormat setter surface the controller drives."""

    def __init__(self) -> None:
        self.sample_rate: int | None = None
        self.channel_count: int | None = None
        self.sample_format: object | None = None

    def setSampleRate(self, rate: int) -> None:
        self.sample_rate = rate

    def setChannelCount(self, count: int) -> None:
        self.channel_count = count

    def setSampleFormat(self, sample_format: object) -> None:
        self.sample_format = sample_format


class FakeSink:
    """QAudioSink stand-in per the documented fake-sink contract.

    Records calls; ``state()`` returns a plain enum member-name STRING, proving
    the controller maps names instead of depending on real Qt enums. Mirrors
    Qt semantics: ``start`` → ActiveState, ``stop`` → StoppedState.
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.device: object | None = None
        self._state = "StoppedState"
        self.stateChanged = SignalStub()

    def start(self, device) -> None:
        self.calls.append("start")
        self.device = device
        self.force_state("ActiveState")

    def stop(self) -> None:
        self.calls.append("stop")
        self.force_state("StoppedState")

    def state(self) -> str:
        return self._state

    def force_state(self, name: str) -> None:
        """Emulate an external transition (underrun, device loss...)."""
        if name != self._state:
            self._state = name
            self.stateChanged.emit(name)


class Harness:
    """Controller wired to a FakeSink/FakeFormat; records levels."""

    def __init__(self) -> None:
        self.created = 0
        self.fail_first_creation = False
        self.creation_failures = 0
        self.fake = FakeSink()
        self.fmt = FakeFormat()
        self.formats: list[FakeFormat] = []

        def sink_factory(fmt):
            if self.fail_first_creation:
                self.fail_first_creation = False
                self.creation_failures += 1
                msg = "no audio backend installed"
                raise RuntimeError(msg)
            self.created += 1
            self.formats.append(fmt)
            return self.fake

        self.controller = StreamPlaybackController(
            sink_factory=sink_factory,
            format_factory=lambda: self.fmt,
        )
        self.levels: list[float] = []
        self.controller.levelReady.connect(self.levels.append)


@pytest.fixture()
def qcoreapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


@pytest.fixture()
def harness(qcoreapp):
    return Harness()


class TestConstructionAndLazy:
    def test_initial_state_is_inactive_and_lazy(self, harness: Harness) -> None:
        c = harness.controller
        assert c.active is False
        assert c.errorText == ""
        assert harness.created == 0  # nothing built until start()

    def test_construction_and_fake_use_never_load_qtmultimedia(self, harness) -> None:
        loaded_before = set(sys.modules)
        c = harness.controller
        c.start()
        c.feed(np.zeros(10, dtype=np.float32))
        c.stop()
        new_modules = set(sys.modules) - loaded_before
        assert not [m for m in new_modules if m.startswith("PySide6.QtMultimedia")]

    def test_stop_before_start_is_noop(self, harness: Harness) -> None:
        harness.controller.stop()
        assert harness.controller.active is False
        assert harness.fake.calls == []

    def test_feed_before_start_drops_bytes_without_crash(self, harness: Harness) -> None:
        harness.controller.feed(np.full(7, 0.5, dtype=np.float32))
        assert harness.levels == []  # no session, no envelope
        assert harness.created == 0
        assert harness.fake.calls == []


class TestStartLifecycle:
    def test_stream_constants_are_the_synthesis_rate(self) -> None:
        assert STREAM_SAMPLE_RATE == 48_000
        assert STREAM_CHANNEL_COUNT == 1

    def test_start_builds_format_and_starts_sink(self, harness: Harness) -> None:
        c = harness.controller
        c.start()
        # The injected format factory is consulted exactly once and the SAME
        # format object reaches the sink factory (configured 48k/1/Float32 —
        # asserted against the real QAudioFormat in TestRealQtSmoke).
        assert harness.formats == [harness.fmt]
        assert harness.fake.calls == ["start"]
        assert c.active is True
        assert isinstance(harness.fake.device, QIODevice)

    def test_start_failure_surfaces_error_but_session_runs(self, harness: Harness) -> None:
        c = harness.controller
        harness.fail_first_creation = True
        c.start()  # must not raise
        assert AUDIO_PLAYBACK_UNAVAILABLE in c.errorText
        assert c.active is True  # session survives: levels flow, bytes drop
        c.feed(np.zeros(4, dtype=np.float32))
        assert harness.levels != []

    def test_failed_then_recovering_start_retries_factory(self, harness: Harness) -> None:
        c = harness.controller
        harness.fail_first_creation = True
        c.start()
        assert harness.creation_failures == 1
        c.start()  # retry this session's start: factory consulted again
        assert harness.created == 1
        assert c.errorText == ""
        assert c.active is True

    def test_restart_while_active_teardowns_previous_session(self, harness: Harness) -> None:
        c = harness.controller
        c.start()
        c.feed(np.full(4, 0.9, dtype=np.float32))
        c.start()
        # Previous tail is torn down (stop) before the fresh pull begins.
        assert harness.fake.calls == ["start", "stop", "start"]
        assert c.active is True
        # Buffered bytes of the old session do not survive into the new one.
        assert harness.fake.device is not None
        assert len(harness.fake.device) == 0  # type: ignore[arg-type]

    def test_stop_then_start_reuses_one_sink(self, harness: Harness) -> None:
        c = harness.controller
        c.start()
        c.stop()
        c.start()
        assert harness.created == 1  # same sink object, restarted
        assert harness.fake.calls == ["start", "stop", "start"]


class TestFeedBuffer:
    def test_variable_size_feeds_accumulate_in_order_little_endian(self, harness: Harness) -> None:
        c = harness.controller
        c.start()
        # Variable sizes incl. tiny (simulates worker chunk jitter); value 1.5
        # pins little-endian byte order: <f4 LE for 1.5 is 00 00 c0 3f.
        chunks = [
            np.array([1.5], dtype=np.float32),
            np.arange(977, dtype=np.float32),
            np.linspace(-0.5, 0.5, 41, dtype=np.float32),
            np.array([256.75], dtype=np.float32),
        ]
        for chunk in chunks:
            c.feed(chunk)
        device = harness.fake.device
        assert device is not None
        expected = b"".join(np.asarray(ch, dtype="<f4").tobytes() for ch in chunks)
        raw = device.readData(len(expected) + 16)  # type: ignore[union-attr]
        assert isinstance(raw, bytes)
        assert raw[:4] == b"\x00\x00\xc0?"  # 1.5 as little-endian float32
        assert raw == expected

    def test_read_data_drains_empty_after_consume(self, harness: Harness) -> None:
        c = harness.controller
        c.start()
        c.feed(np.zeros(8, dtype=np.float32))
        device = harness.fake.device
        drained = device.readData(1024)  # type: ignore[union-attr]
        assert drained == np.zeros(8, dtype=np.float32).tobytes()
        assert device.readData(1024) == b""  # type: ignore[union-attr]

    def test_write_data_forbidden(self, harness: Harness) -> None:
        c = harness.controller
        c.start()
        device = harness.fake.device
        assert device.writeData(b"\x00" * 4) == -1  # type: ignore[union-attr]


class TestLevels:
    def test_known_amplitudes_report_peak(self, harness: Harness) -> None:
        c = harness.controller
        c.start()
        c.feed(np.zeros(4, dtype=np.float32))
        assert harness.levels[-1] == pytest.approx(0.0)
        c.feed(np.array([-0.5, 0.25, 0.0], dtype=np.float32))
        assert harness.levels[-1] == pytest.approx(0.5)
        c.feed(np.array([-1.0, 0.5], dtype=np.float32))
        assert harness.levels[-1] == pytest.approx(1.0)  # magnitude counts
        c.feed(np.array([4.0, 12.0], dtype=np.float32))  # overshoot clamps
        assert harness.levels[-1] == pytest.approx(1.0)

    def test_non_finite_and_empty_chunks_are_zero(self, harness: Harness) -> None:
        c = harness.controller
        c.start()
        c.feed(np.array([], dtype=np.float32))
        assert harness.levels[-1] == pytest.approx(0.0)
        c.feed(np.array([np.nan, np.inf, -np.inf], dtype=np.float32))
        assert harness.levels[-1] == pytest.approx(0.0)

    def test_python_list_chunks_accepted(self, harness: Harness) -> None:
        c = harness.controller
        c.start()
        c.feed([0.25, -0.1])
        assert harness.levels[-1] == pytest.approx(0.25)


class TestStop:
    def test_stop_halts_sink_clears_buffer_and_deactivates(self, harness: Harness) -> None:
        c = harness.controller
        c.start()
        c.feed(np.full(64, 0.5, dtype=np.float32))
        device = harness.fake.device
        assert device is not None and len(device) > 0  # type: ignore[arg-type]
        c.stop()
        assert harness.fake.calls[-1] == "stop"
        assert c.active is False
        assert device.readData(1024) == b""  # buffered bytes gone  # type: ignore[union-attr]

    def test_stop_twice_is_idempotent(self, harness: Harness) -> None:
        c = harness.controller
        c.start()
        c.stop()
        c.stop()
        assert c.active is False

    def test_feed_after_stop_drops_chunk(self, harness: Harness) -> None:
        c = harness.controller
        c.start()
        c.stop()
        c.feed(np.ones(8, dtype=np.float32))
        device = harness.fake.device
        assert device.readData(1024) == b""  # type: ignore[union-attr]


class TestUnderrunTolerance:
    def test_feed_restarts_sink_observed_stopped_or_idle(self, harness: Harness) -> None:
        c = harness.controller
        c.start()
        c.feed(np.zeros(8, dtype=np.float32))
        assert harness.fake.calls == ["start"]  # healthy ActiveState: untouched
        harness.fake.force_state("IdleState")  # simulated underrun
        c.feed(np.ones(8, dtype=np.float32))
        assert harness.fake.calls == ["start", "stop", "start"]
        harness.fake.force_state("StoppedState")  # e.g. device loss
        c.feed(np.full(8, 0.5, dtype=np.float32))
        assert harness.fake.calls == ["start", "stop", "start", "stop", "start"]
        assert harness.fake.device is not None
        assert len(harness.fake.device) > 0  # fresh bytes still land  # type: ignore[arg-type]

    def test_unexpected_sink_states_never_crash(self, harness: Harness) -> None:
        c = harness.controller
        c.start()
        harness.fake.stateChanged.emit("SuspendedState")  # unmapped exotic state
        assert c.active is True


class TestMinimalFakeContract:
    def test_sink_without_statechanged_still_works(self, qcoreapp) -> None:
        # The contract allows fakes WITHOUT the optional stateChanged signal.
        calls: list[str] = []

        def sink_factory(_fmt):
            return SimpleNamespace(
                start=lambda dev: calls.append("start"),
                stop=lambda: calls.append("stop"),
                state=lambda: "ActiveState",
            )

        controller = StreamPlaybackController(
            sink_factory=sink_factory,
            format_factory=FakeFormat,
        )
        controller.start()
        controller.feed(np.zeros(4, dtype=np.float32))
        controller.stop()
        assert calls == ["start", "stop"]
        assert controller.active is False


class TestRealQtSmoke:
    def test_default_format_builder_produces_48k_mono_float(self) -> None:
        try:
            from PySide6.QtMultimedia import QAudioFormat

            fmt = _make_stream_format()
        except Exception as error:  # pragma: no cover - environment-dependent
            pytest.skip(f"QtMultimedia unavailable offscreen: {error}")
        assert fmt.sampleRate() == 48_000
        assert fmt.channelCount() == 1
        assert fmt.sampleFormat() == QAudioFormat.SampleFormat.Float

    def test_real_qaudiosink_offscreen_smoke(self, qcoreapp, monkeypatch) -> None:
        # Real QAudioSink under offscreen. GOTCHA (mirrors test_playback.py):
        # under pytest fd capture audio-backend probing deadlocks; forcing the
        # ffmpeg backend skips the pipewire probe. Success = start->feed->stop
        # without hanging or crashing; a headless host legitimately skips.
        monkeypatch.setenv("QT_AUDIO_BACKEND", "ffmpeg")
        controller = StreamPlaybackController()
        try:
            controller.start()
        except Exception as error:  # pragma: no cover - environment-dependent
            pytest.skip(f"real audio sink unavailable offscreen: {error}")
        if controller.errorText != "":
            pytest.skip(f"sink construction failed offscreen: {controller.errorText}")
        assert controller.active is True
        controller.feed(np.sin(np.linspace(0, np.pi, 4800)).astype(np.float32))
        wait_until(lambda: False, timeout=0.15)  # pump events; sink consumes quietly
        controller.stop()
        assert controller.active is False
