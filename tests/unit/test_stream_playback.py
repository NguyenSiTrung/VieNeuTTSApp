"""StreamPlaybackController: QAudioSink pulling from the bounded PCM transport.

All logic runs against an injected FakeSink duck-typed per the contract in
StreamPlaybackController's docstring: start(io)/stop()/state() plus an optional
stateChanged stub that emits enum member-NAME strings — unit tests never import
QtMultimedia. One smoke case constructs the real QAudioSink offscreen (with
QT_AUDIO_BACKEND=ffmpeg, following test_playback.py's pattern) and skips
gracefully when construction fails headless.
"""

from __future__ import annotations

import os
import sys
import time
from types import SimpleNamespace

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QIODevice  # noqa: E402

from vienetts_app.core.pcm_transport import PREBUFFER_BYTES, BoundedPcmTransport  # noqa: E402
from vienetts_app.core.performance import PerformanceRecorder  # noqa: E402
from vienetts_app.ui.stream_playback import (  # noqa: E402
    AUDIO_PLAYBACK_UNAVAILABLE,
    STREAM_CHANNEL_COUNT,
    STREAM_SAMPLE_RATE,
    StreamPlaybackController,
    TransportIODevice,
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


class StateSignalStub(SignalStub):
    """Match Qt's accepted zero-argument stateChanged Python slot shape."""

    def emit(self, *args) -> None:  # noqa: ARG002
        for slot in list(self._slots):
            slot()


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
        self.stateChanged = StateSignalStub()
        self.errorOccurred = SignalStub()

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


class QAudioSink:
    """Real-QAudioSink-shaped fake: no ``errorOccurred`` signal."""

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.device: object | None = None
        self._state = "StoppedState"
        self._error = "NoError"
        self.stateChanged = StateSignalStub()

    def start(self, device) -> None:
        self.calls.append("start")
        self.device = device
        self._state = "ActiveState"
        self.stateChanged.emit(self._state)

    def stop(self) -> None:
        self.calls.append("stop")
        self._state = "StoppedState"
        self.stateChanged.emit(self._state)

    def state(self) -> str:
        return self._state

    def error(self) -> str:
        return self._error

    def fail(self, error: str) -> None:
        self._error = error
        self._state = "StoppedState"
        self.stateChanged.emit(self._state)


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
        self.transport: BoundedPcmTransport | None = None

    def open(self, job_id: str = "a" * 32) -> BoundedPcmTransport:
        """Start a transport session and push past the prebuffer (sink starts)."""
        self.transport = BoundedPcmTransport()
        self.controller.start(self.transport, job_id)
        self.transport.put(memoryview(bytes(PREBUFFER_BYTES)))
        self.controller.notify_transport_available()
        return self.transport

    def push(self, samples) -> None:
        assert self.transport is not None
        payload = np.ascontiguousarray(samples, dtype="<f4").tobytes()
        self.transport.put(memoryview(payload))
        self.controller.notify_transport_available()


@pytest.fixture()
def harness(qcoreapp):
    return Harness()


class TestConstructionAndLazy:
    def test_pre_start_state_guards_are_noops(self, harness: Harness) -> None:
        c = harness.controller
        assert c.active is False
        assert c.errorText == ""
        assert harness.created == 0  # nothing built until start()
        # stop() before any session is a no-op.
        c.stop()
        assert c.active is False
        assert harness.fake.calls == []
        assert c.buffered_drain_ms() == 0

    def test_construction_and_fake_use_never_load_qtmultimedia(self, harness) -> None:
        loaded_before = set(sys.modules)
        harness.open()
        harness.push(np.zeros(10, dtype=np.float32))
        harness.controller.stop()
        new_modules = set(sys.modules) - loaded_before
        assert not [m for m in new_modules if m.startswith("PySide6.QtMultimedia")]

    def test_buffered_drain_ms_tracks_buffered_bytes(self, harness: Harness) -> None:
        c = harness.controller
        transport = harness.open()
        transport.take(PREBUFFER_BYTES)
        harness.push(np.zeros(24_000, dtype=np.float32))  # 0.5 s of float32 mono
        assert c.buffered_drain_ms() == 500
        harness.fake.device.readData(96_000 // 4)  # quarter of the buffer drained
        assert c.buffered_drain_ms() == 375
        c.stop()
        assert c.buffered_drain_ms() == 0  # stopped session reports nothing


class TestFileBackedFeeder:
    """The 20 ms feeder tops the transport up from the producer's artifact."""

    @staticmethod
    def _backlogged(harness: Harness, seconds: float) -> tuple[BoundedPcmTransport, bytes]:
        transport = harness.open()
        transport.take(PREBUFFER_BYTES)
        pcm = np.arange(int(48_000 * seconds), dtype=np.float32).astype("<f4").tobytes()
        transport.attach_source(lambda start, end: pcm[start:end])
        transport.publish(memoryview(pcm))
        return transport, pcm

    def test_the_timer_tick_refills_from_the_source_in_order(self, harness: Harness) -> None:
        transport, pcm = self._backlogged(harness, 3.0)  # 3 s > the 2 s cap
        assert transport.pending_bytes() > 0
        played = bytearray()
        for _ in range(400):
            harness.controller.notify_transport_available()
            chunk = harness.fake.device.readData(96_000)
            if not chunk and not transport.pending_bytes():
                break
            played.extend(chunk)
        assert bytes(played) == pcm
        assert transport.max_available_bytes <= transport._capacity

    def test_drain_estimate_counts_the_backlog(self, harness: Harness) -> None:
        transport, _pcm = self._backlogged(harness, 3.0)
        # 2 s in the transport + 1 s still in the file.
        assert harness.controller.buffered_drain_ms() == 3_000

    def test_begin_drain_keeps_feeding_the_backlog(self, harness: Harness) -> None:
        transport, pcm = self._backlogged(harness, 2.5)
        harness.controller.begin_drain()
        played = bytearray()
        for _ in range(400):
            harness.controller.notify_transport_available()
            chunk = harness.fake.device.readData(96_000)
            if not chunk:
                break
            played.extend(chunk)
        assert bytes(played) == pcm

    def test_a_failed_sink_drops_the_backlog_instead_of_reading_it(self, harness: Harness) -> None:
        transport, _pcm = self._backlogged(harness, 3.0)
        harness.controller._on_sink_error("FatalError")
        assert transport.pending_bytes() == 0
        assert transport.available_bytes() == 0


class TestStartLifecycle:
    def test_stream_constants_are_the_synthesis_rate(self) -> None:
        assert STREAM_SAMPLE_RATE == 48_000
        assert STREAM_CHANNEL_COUNT == 1

    def test_start_builds_format_and_starts_sink(self, harness: Harness) -> None:
        c = harness.controller
        harness.open()
        # The injected format factory is consulted exactly once and the SAME
        # format object reaches the sink factory (configured 48k/1/Float32 —
        # asserted against the real QAudioFormat in TestRealQtSmoke).
        assert harness.formats == [harness.fmt]
        assert harness.fake.calls == ["start"]
        assert c.active is True
        assert isinstance(harness.fake.device, QIODevice)

    def test_transport_starts_at_prebuffer_and_consumption_unblocks_producer(
        self, harness: Harness
    ) -> None:
        transport = BoundedPcmTransport()
        harness.controller.start(transport, "a" * 32)
        assert harness.fake.calls == []

        transport.put(memoryview(bytes(PREBUFFER_BYTES)))
        harness.controller.notify_transport_available()
        assert harness.fake.calls == ["start"]

        assert harness.fake.device.readData(PREBUFFER_BYTES) == bytes(PREBUFFER_BYTES)
        transport.put(memoryview(b"next"))
        assert transport.available_bytes() == 4

    def test_transport_records_first_append_and_sink_pull(self, qcoreapp) -> None:
        recorder = PerformanceRecorder(enabled=True)
        sink = FakeSink()
        controller = StreamPlaybackController(
            sink_factory=lambda _fmt: sink,
            format_factory=FakeFormat,
            performance_recorder=recorder,
        )
        job_id = "b" * 32
        recorder.begin(job_id, {"mode": "stream"})
        controller.start(BoundedPcmTransport(), job_id)
        transport = controller._transport
        assert transport is not None

        # The worker marks a successful transport put; this isolated device
        # test performs that producer boundary directly.
        transport.put(memoryview(bytes(PREBUFFER_BYTES)))
        recorder.mark(job_id, "audio_first_buffer_append")
        controller.notify_transport_available()
        assert sink.device.readData(PREBUFFER_BYTES) == bytes(PREBUFFER_BYTES)
        controller.stop()

        (trace,) = recorder.snapshot(job_id)
        names = [event["name"] for event in trace["events"]]
        assert "audio_session_started" in names
        assert names.index("audio_first_buffer_append") < names.index("audio_first_sink_pull")
        assert "audio_session_stopped" in names
        assert "audio_buffer_bytes" not in trace["maxima"]

    def test_transport_sink_start_failure_falls_back_without_closing_transport(
        self, harness: Harness
    ) -> None:
        transport = BoundedPcmTransport()
        harness.fake.start = lambda _device: (_ for _ in ()).throw(RuntimeError("device gone"))
        harness.controller.start(transport, "c" * 32)
        transport.put(memoryview(bytes(PREBUFFER_BYTES)))
        harness.controller.notify_transport_available()

        transport.put(memoryview(bytes(PREBUFFER_BYTES)))
        harness.controller.notify_transport_available()
        assert transport.available_bytes() == 0

    def test_transport_underrun_restarts_same_io_when_new_bytes_arrive(self, qcoreapp) -> None:
        recorder = PerformanceRecorder(enabled=True)
        sink = FakeSink()
        controller = StreamPlaybackController(
            sink_factory=lambda _fmt: sink,
            format_factory=FakeFormat,
            performance_recorder=recorder,
        )
        recorder.begin("d" * 32, {"mode": "stream"})
        transport = BoundedPcmTransport()
        controller.start(transport, "d" * 32)
        transport.put(memoryview(bytes(PREBUFFER_BYTES)))
        controller.notify_transport_available()
        io = sink.device
        assert io is not None
        assert io.readData(PREBUFFER_BYTES) == bytes(PREBUFFER_BYTES)
        sink.force_state("IdleState")
        transport.put(memoryview(b"next"))
        controller.notify_transport_available()

        assert sink.calls == ["start", "stop", "start"]
        assert sink.device is io
        assert io.readData(4) == b"next"
        controller.stop()
        (trace,) = recorder.snapshot("d" * 32)
        assert trace["counters"]["audio_restarts"] == 1

    def test_real_shaped_sink_fatal_state_enters_transport_fallback(self, qcoreapp) -> None:
        sink = QAudioSink()
        controller = StreamPlaybackController(
            sink_factory=lambda _fmt: sink,
            format_factory=FakeFormat,
        )
        transport = BoundedPcmTransport()
        failures: list[bool] = []
        controller.livePlaybackFailed.connect(lambda: failures.append(True))
        controller.start(transport, "e" * 32)
        transport.put(memoryview(bytes(PREBUFFER_BYTES)))
        controller.notify_transport_available()

        sink.fail("FatalError")
        transport.put(memoryview(b"next"))
        controller.notify_transport_available()

        assert failures == [True]
        assert transport.available_bytes() == 0

    def test_transport_consecutive_stalls_enter_fallback(self, harness: Harness) -> None:
        transport = BoundedPcmTransport()
        failures: list[bool] = []
        harness.controller.livePlaybackFailed.connect(lambda: failures.append(True))
        harness.controller.start(transport, "f" * 32)
        transport.put(memoryview(bytes(PREBUFFER_BYTES)))
        harness.controller.notify_transport_available()
        assert harness.fake.calls == ["start"]

        for _ in range(6):
            harness.fake.force_state("IdleState")
            transport.put(memoryview(b"chunk"))
            harness.controller._last_restart_monotonic = 0.0
            harness.controller.notify_transport_available()

        assert failures == [True]
        assert harness.controller._discard_transport is True

    def test_start_failure_surfaces_error_then_retry_recovers(self, harness: Harness) -> None:
        c = harness.controller
        harness.fail_first_creation = True
        c.start(BoundedPcmTransport(), "g" * 32)  # must not raise
        assert AUDIO_PLAYBACK_UNAVAILABLE in c.errorText
        # Retrying a session consults the factory again.
        harness.open()
        assert harness.creation_failures == 1
        assert harness.created == 1
        assert c.errorText == ""
        assert c.active is True

    def test_restart_and_stop_start_teardown_reuses_one_sink(self, harness: Harness) -> None:
        c = harness.controller
        first = harness.open()
        harness.open()
        # Previous tail is torn down (stop) before the fresh pull begins, and
        # the old transport's bytes are discarded.
        assert harness.fake.calls == ["start", "stop", "start"]
        assert c.active is True
        assert first.available_bytes() == 0
        c.stop()
        harness.open()
        assert harness.created == 1  # same sink object, restarted
        assert harness.fake.calls == ["start", "stop", "start", "stop", "start"]

    def test_unexpected_sink_states_never_crash(self, harness: Harness) -> None:
        harness.open()
        harness.fake.stateChanged.emit("SuspendedState")  # unmapped exotic state
        assert harness.controller.active is True


class TestTransportDevice:
    def test_float32_bytes_pass_through_in_order_little_endian(self, harness: Harness) -> None:
        transport = harness.open()
        transport.take(PREBUFFER_BYTES)
        # Variable sizes incl. tiny (simulates worker chunk jitter); value 1.5
        # pins little-endian byte order: <f4 LE for 1.5 is 00 00 c0 3f.
        chunks = [
            np.array([1.5], dtype=np.float32),
            np.arange(977, dtype=np.float32),
            np.linspace(-0.5, 0.5, 41, dtype=np.float32),
            np.array([256.75], dtype=np.float32),
        ]
        for chunk in chunks:
            harness.push(chunk)
        device = harness.fake.device
        expected = b"".join(np.asarray(ch, dtype="<f4").tobytes() for ch in chunks)
        raw = device.readData(len(expected) + 16)
        assert isinstance(raw, bytes)
        assert raw[:4] == b"\x00\x00\xc0?"  # 1.5 as little-endian float32
        assert raw == expected

    def test_io_device_contract_drain_write_and_availability(self, qcoreapp) -> None:
        transport = BoundedPcmTransport()
        device = TransportIODevice(transport)
        transport.put(memoryview(bytes(32)))
        assert device.readData(1024) == bytes(32)
        assert device.readData(1024) == b""  # drained empty
        assert device.writeData(b"\x00" * 4) == -1  # read-only playback device
        assert device.bytesAvailable() == 0
        assert device.atEnd() is True
        transport.put(memoryview(bytes(64)))
        assert device.bytesAvailable() == 64
        assert len(device.read(32)) == 32
        assert device.bytesAvailable() == 32
        transport.close(discard=True)
        assert device.readData(16) == b""  # closed transport reads empty


class FakeInt16Format(FakeFormat):
    """Negotiated Int16 format: sampleFormat() reports Int16, not Float."""

    def sampleFormat(self) -> str:
        return "Int16"


class TestInt16Fallback:
    """Float32-rejecting outputs (WASAPI/BT): the live device converts to Int16."""

    RAMP = np.array([1.0, -1.0, 0.5, 0.0, 1.5, -2.0, np.nan, 0.25], dtype=np.float32)
    EXPECTED = np.array([32767, -32767, 16383, 0, 32767, -32767, 0, 8191], dtype="<i2")

    def test_device_converts_scaled_clipped_and_never_splits_a_sample(self, qcoreapp) -> None:
        transport = BoundedPcmTransport()
        device = TransportIODevice(transport, int16=True)
        transport.put(memoryview(self.RAMP.astype("<f4").tobytes()))
        assert device.bytesAvailable() == self.RAMP.size * 2
        assert len(device) == self.RAMP.size * 2
        # Odd request sizes round down to whole int16 samples (never half one).
        first = device.readData(5)
        assert first == self.EXPECTED[:2].tobytes()
        rest = device.readData(1024)
        assert first + rest == self.EXPECTED.tobytes()
        assert device.readData(1) == b""

    def test_negotiated_int16_session_converts_live_pcm_and_sizes_drain(self, qcoreapp) -> None:
        sink = FakeSink()
        controller = StreamPlaybackController(
            sink_factory=lambda _fmt: sink,
            format_factory=FakeInt16Format,
        )
        transport = BoundedPcmTransport()
        controller.start(transport, "h" * 32)
        transport.put(memoryview(bytes(PREBUFFER_BYTES)))
        controller.notify_transport_available()
        assert sink.device.readData(PREBUFFER_BYTES // 2) == bytes(PREBUFFER_BYTES // 2)
        transport.put(memoryview(self.RAMP.astype("<f4").tobytes()))
        assert sink.device.readData(1024) == self.EXPECTED.tobytes()
        # Drain math follows the negotiated width: 4800 int16 samples @48k = 100 ms.
        transport.put(memoryview(np.zeros(4800, dtype="<f4").tobytes()))
        assert controller.buffered_drain_ms() == 100
        controller.stop()

    def test_float32_session_bytes_are_unchanged(self, harness: Harness) -> None:
        transport = harness.open()
        transport.take(PREBUFFER_BYTES)
        harness.push(self.RAMP)
        assert harness.fake.device.readData(1024) == self.RAMP.astype("<f4").tobytes()


class TestStop:
    def test_stop_postconditions_and_idempotence(self, harness: Harness) -> None:
        c = harness.controller
        transport = harness.open()
        harness.push(np.full(64, 0.5, dtype=np.float32))
        device = harness.fake.device
        assert len(device) > 0
        c.stop()
        assert harness.fake.calls[-1] == "stop"
        assert c.active is False
        assert transport.available_bytes() == 0  # buffered bytes gone
        assert device.readData(1024) == b""
        c.stop()
        assert c.active is False  # second stop is idempotent


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
        transport = BoundedPcmTransport()
        controller.start(transport, "i" * 32)
        transport.put(memoryview(bytes(PREBUFFER_BYTES)))
        controller.notify_transport_available()
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

    @pytest.mark.skipif(
        os.environ.get("CI") == "true",
        reason="the drained-buffer assert needs a real audio output device; CI "
        "runners construct the sink fine but nothing drains it (bytesAvailable "
        "stays non-zero). Runs fully on any host with a sound device.",
    )
    def test_real_qaudiosink_offscreen_smoke(self, qcoreapp, monkeypatch) -> None:
        # Real QAudioSink under offscreen. GOTCHA (mirrors test_playback.py):
        # under pytest fd capture audio-backend probing deadlocks; forcing the
        # ffmpeg backend skips the pipewire probe. Success = start->push->stop
        # without hanging or crashing; a headless host legitimately skips.
        monkeypatch.setenv("QT_AUDIO_BACKEND", "ffmpeg")
        controller = StreamPlaybackController()
        transport = BoundedPcmTransport()
        try:
            controller.start(transport, "j" * 32)
        except Exception as error:  # pragma: no cover - environment-dependent
            pytest.skip(f"real audio sink unavailable offscreen: {error}")
        if controller.errorText != "":
            pytest.skip(f"sink construction failed offscreen: {controller.errorText}")
        assert controller.active is True
        samples = np.sin(np.linspace(0, np.pi, 9600)).astype("<f4")
        transport.put(memoryview(samples.tobytes()))
        controller.begin_drain()
        assert wait_until(lambda: transport.available_bytes() == 0)  # sink consumed it
        controller.stop()
        assert controller.active is False


class TestSinkErrorSignal:
    """QAudioSink.errorOccurred wiring: device failures must not be silent."""

    def test_sink_error_occurred_benign_quiet_fatal_and_io_banners(self, harness: Harness) -> None:
        c = harness.controller
        harness.open()
        harness.fake.errorOccurred.emit("NoError")
        harness.fake.errorOccurred.emit("UnderrunError")
        assert c.errorText == ""
        harness.fake.errorOccurred.emit("FatalError")
        assert AUDIO_PLAYBACK_UNAVAILABLE in c.errorText
        assert c.active is True  # the session (artifact-first synthesis) keeps running
        harness.fake.errorOccurred.emit("IOError")
        assert AUDIO_PLAYBACK_UNAVAILABLE in c.errorText

    def test_fatal_error_discards_transport_bytes_without_closing_producer(
        self, harness: Harness
    ) -> None:
        transport = BoundedPcmTransport()
        harness.controller.start(transport, "b" * 32)
        transport.put(memoryview(bytes(PREBUFFER_BYTES)))
        harness.controller.notify_transport_available()
        harness.fake.errorOccurred.emit("FatalError")

        transport.put(memoryview(bytes(PREBUFFER_BYTES)))
        harness.controller.notify_transport_available()
        assert transport.available_bytes() == 0
