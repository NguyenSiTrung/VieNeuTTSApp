"""Streaming playback: QAudioSink fed by a push-mode ring buffer (FR-4.1, FR-4.5 groundwork).

Owns the audio half of stream synthesis: ``feed(chunk)`` accepts the
VARIABLE-size float32 mono chunks the inference worker emits on
``chunk_ready`` (~15 360–96 000 samples @ 48 kHz, phase01 spike §4) and turns
them into bytes inside a ring buffer. A QAudioSink pulls from that buffer via
a custom pull-mode QIODevice, so variable chunk sizes are handled trivially by
appending (FR-4.1). Synthesis audio is 48 kHz mono float32 (denoise is
44.1 kHz — not handled here).

The real QtMultimedia objects are constructed lazily on the first ``start()``
via injectable factories — importing this module and constructing the
controller never loads QtMultimedia (same posture as ui/playback.py, NFR-2.1).

QML surface (aggregated by AppController; never registered directly):
    active     bool, NOTIFY activeChanged — true between start() and stop()
    errorText  str, NOTIFY errorTextChanged — sink-construction failure message
    levelReady(float) Signal — peak amplitude (0..1) of each fed ~120 ms
               window (one per feed() call for chunks that small)
    finished() Signal — play_buffer() replay drained to its end

Level metric (documented choice): ``max(|sample|)`` over the window, clamped
to 0..1; an empty or all-non-finite window yields 0.0. Chunks LARGER than
``LEVEL_WINDOW_SAMPLES`` (120 ms) are sliced into windows and emit ONE
levelReady per window (capped at ``MAX_LEVEL_EMISSIONS_PER_CHUNK``), keeping
the WaveformIndicator bar cadence at audio pace — synthesis chunks span
0.32–2 s, and one peak per feed() would read as a stalled meter. Peak
amplitude gives the Phase 2 WaveformIndicator a cheap rolling envelope
without exposing raw samples to QML.

Session lifecycle:
    start()  opens a session: any previous one is torn down first (stop +
             fresh empty buffer), then format/sink are built and the sink is
             started in pull mode against our ring-buffer device.
    feed()   legal ONLY inside a session — before start()/after stop() chunks
             are dropped entirely (no level signal), which keeps memory
             bounded if the controller is misused.
    stop()   hard stop: sink.stop() + buffered bytes dropped → immediate
             silence (what a cancel wants). Done draining naturally instead:
             the session owner simply leaves the sink running after done so
             remaining buffered audio plays out; stop() before the NEXT
             session bounds that tail.
    start() failure (sink construction) never raises: it is logged, surfaced
             through ``errorText``, the session still runs (levels flow, bytes
             are dropped without a sink), and the next start() retries.

Underrun tolerance: QAudioSink flips to Idle/Stopped when it starves mid-stream
and does not reliably resume pulling on its own. feed() therefore inspects
``state()`` AFTER appending nothing yet / BEFORE appending — when the state
name maps to StoppedState or IdleState, the sink is transparently restarted
(stop + start against the same ring buffer). A healthy ActiveState sink is
never touched, so steady streams see exactly one start per session.

Default factory seams (each lazily imports PySide6.QtMultimedia INSIDE the
function; tests pass fakes and stay QtMultimedia-free):
    sink_factory(audio_format) -> Any    default: real ``QAudioSink(format)``
    format_factory() -> Any              default: 48 kHz / mono / Float32
                                         ``QAudioFormat``

Fake-sink contract (tests; plain duck types, ZERO QtMultimedia usage):
    The controller builds the format via ``format_factory()`` then the sink via
    ``sink_factory(format)``, and afterwards only ever calls/queries the sink for:
      start(io_device)   begin pulling from our ring-buffer QIODevice
      stop()             halt playback
      state()            Qt audio STATE enum OR its member-name string
                         ("ActiveState" | "IdleState" | "StoppedState" ...)
    plus ONE OPTIONAL signal (connected via getattr when present, so a fake
    WITHOUT it works fine):
      stateChanged(name) — enum-member-name mapped; every known name is just
                           logged (unmapped names too) — never fatal.
    Everything else on QAudioSink (setVolume, suspend/resume, buffersize...)
    is deliberately untouched.
"""

from __future__ import annotations

import contextlib
import logging
import math
import time
from collections.abc import Callable
from typing import Any

import numpy as np
from PySide6.QtCore import Property, QIODevice, QObject, QTimer, Signal

from vienetts_app.core.pcm_transport import BoundedPcmTransport, TransportClosed
from vienetts_app.core.performance import PerformanceRecorder

logger = logging.getLogger(__name__)

STREAM_SAMPLE_RATE = 48_000  # infer/infer_stream synthesis rate (denoise ≠ this)
STREAM_CHANNEL_COUNT = 1

# Level granularity: chunks larger than one 120 ms window are sliced so the
# QML rolling meter advances at audio pace (~8 bars/s) instead of once per
# 0.32–2 s synthesis chunk. The cap bounds play_buffer()'s single whole-file
# feed (a 27 s buffer would otherwise emit ~225 signals in one burst).
LEVEL_WINDOW_SAMPLES = 5_760  # 120 ms @ 48 kHz mono
MAX_LEVEL_EMISSIONS_PER_CHUNK = 48

# play_buffer() drain allowance on top of the buffer's real-time duration:
# covers sink start-up latency before consumption begins.
REPLAY_DRAIN_MARGIN_MS = 300

AUDIO_PLAYBACK_UNAVAILABLE = "Hệ thống này không phát được âm thanh."

# Sink states meaning "the sink stopped consuming" mid-session (see _enum_name).
_RESTART_STATE_NAMES = frozenset({"StoppedState", "IdleState"})

# Sink restart pacing & fallback limits: prevent WASAPI COM thread thrashing
# and Access Violation crashes on Windows when audio buffer starves repeatedly.
MAX_CONSECUTIVE_AUDIO_RESTARTS = 5
MIN_RESTART_INTERVAL_MS = 60


def _default_format_factory() -> Any:
    """Production seam: real ``QAudioFormat`` @ 48 kHz / mono / Float32."""
    # Imported here (not at module top) so importing this module or building
    # the controller with injected factories never loads QtMultimedia.
    from PySide6.QtMultimedia import QAudioFormat

    fmt = QAudioFormat()
    fmt.setSampleRate(STREAM_SAMPLE_RATE)
    fmt.setChannelCount(STREAM_CHANNEL_COUNT)
    fmt.setSampleFormat(QAudioFormat.SampleFormat.Float)
    return fmt


def _make_stream_format() -> Any:
    """Public alias of the default format builder (unit-testable seam)."""
    return _default_format_factory()


def _default_sink_factory(audio_format: Any) -> Any:
    """Production seam: real ``QAudioSink`` for the given format."""
    # Same lazy-import posture as the format factory above.
    from PySide6.QtMultimedia import QAudioSink

    return QAudioSink(audio_format)


def _enum_name(value: Any) -> str:
    """Enum member name, tolerant of both real Qt enums and plain strings.

    ``str(QAudio.State.StoppedState)`` is "Audio.State.StoppedState"-shaped,
    so a ``split(".")[-1]`` fallback covers str()-received values while real
    ``.name`` attributes (and test fakes passing plain strings) pass through.
    """
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return name
    return str(value).split(".")[-1]


def _peak_level(samples: np.ndarray) -> float:
    """Peak amplitude of a chunk in 0..1 — the documented level metric.

    Non-finite samples make the max non-finite → treated as silence (0.0);
    peaks above 1.0 clamp (float overshoot happens upstream).
    """
    flat = samples.ravel()
    if flat.size == 0:
        return 0.0
    peak = float(np.max(np.abs(flat)))
    if not math.isfinite(peak):
        return 0.0
    return min(peak, 1.0)


class TransportIODevice(QIODevice):
    """QIODevice adapter that reads PCM from a bounded transport."""

    def __init__(
        self,
        transport: BoundedPcmTransport,
        parent: QObject | None = None,
        on_first_read: Callable[[], None] | None = None,
        on_read: Callable[[int], None] | None = None,
    ) -> None:
        super().__init__(parent)
        self._transport = transport
        self._on_first_read = on_first_read
        self._on_read = on_read
        self._reported_first_read = False
        self.open(QIODevice.OpenModeFlag.ReadOnly)

    def isSequential(self) -> bool:
        return True

    def bytesAvailable(self) -> int:  # noqa: N802 - Qt naming
        return self._transport.available_bytes() + super().bytesAvailable()

    def __len__(self) -> int:
        return self._transport.available_bytes()

    def clear_buffer(self) -> None:
        self._transport.close(discard=True)

    def readData(self, maxSize: int) -> bytes:  # noqa: N802 - Qt naming
        try:
            data = self._transport.take(max(0, int(maxSize)))
            if data and not self._reported_first_read:
                self._reported_first_read = True
                if self._on_first_read is not None:
                    with contextlib.suppress(Exception):
                        self._on_first_read()
            if data and self._on_read is not None:
                with contextlib.suppress(Exception):
                    self._on_read(len(data))
            return data
        except TransportClosed:
            return b""
        except Exception:
            logger.exception("TransportIODevice.readData failed")
            return b""

    def writeData(self, data: Any) -> int:  # noqa: N802 - Qt naming
        return -1


class StreamIODevice(QIODevice):
    """Push-mode ring-buffer QIODevice serving float32 bytes to QAudioSink.

    The controller pushes into ``append_bytes`` (little-endian float32 from
    feed()) which also emits readyRead; the sink PULLS through readData,
    taking up to its requested byte count off the front. Variable chunk
    sizes need no logic beyond appending (FR-4.1).

    Consumption is a read OFFSET, not a front-deletion: ``del buffer[:n]``
    memmoves the whole remainder left on EVERY pull — quadratic in buffered
    bytes and directly on the sink's latency path (a capped 5 MB replay used
    to move gigabytes of memory per play). The buffer compacts once the
    consumed prefix exceeds half of it, keeping aggregate copies amortized
    linear.
    """

    def __init__(
        self,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._buffer = bytearray()
        self._offset = 0  # consumed prefix length (buffer[:offset] is dead)
        self.open(QIODevice.OpenModeFlag.ReadOnly)

    def isSequential(self) -> bool:
        return True

    def bytesAvailable(self) -> int:  # noqa: N802 - Qt naming
        return self._available() + super().bytesAvailable()

    def __len__(self) -> int:
        return self._available()

    def _available(self) -> int:
        return len(self._buffer) - self._offset

    def _maybe_compact(self) -> None:
        if self._offset > len(self._buffer) // 2:
            del self._buffer[: self._offset]
            self._offset = 0

    # ── producer side (main thread) ─────────────────────────────────────────

    def append_bytes(self, payload: bytes | memoryview) -> None:
        if payload:
            self._maybe_compact()
            self._buffer.extend(payload)  # bytearray.extend accepts memoryview
            self.readyRead.emit()

    def clear_buffer(self) -> None:
        self._buffer.clear()
        self._offset = 0

    def take_bytes(self, max_len: int) -> bytes:
        """Take at most ``max_len`` bytes off the front (used by sinks AND tests)."""
        n = min(max_len, self._available())
        if n <= 0:
            return b""
        data = bytes(self._buffer[self._offset : self._offset + n])
        self._offset += n
        self._maybe_compact()
        return data

    # ── consumer side (called by QAudioSink's pull loop) ────────────────────

    def readData(self, maxSize: int) -> bytes:  # noqa: N802 - Qt naming
        return self.take_bytes(int(maxSize))

    def writeData(self, data: Any) -> int:  # noqa: N802 - Qt naming
        logger.debug("StreamIODevice.writeData forbidden — use feed()")
        return -1


class StreamPlaybackController(QObject):
    """Ring-buffer streaming playback (see module docstring for contracts)."""

    activeChanged = Signal()
    levelReady = Signal(float)
    errorTextChanged = Signal()
    finished = Signal()
    livePlaybackFailed = Signal()

    def __init__(
        self,
        sink_factory: Any | None = None,
        format_factory: Any | None = None,
        parent: QObject | None = None,
        performance_recorder: PerformanceRecorder | None = None,
    ) -> None:
        super().__init__(parent)
        self._sink_factory = _default_sink_factory if sink_factory is None else sink_factory
        self._format_factory = _default_format_factory if format_factory is None else format_factory
        self._io: StreamIODevice | TransportIODevice | None = None
        self._sink: Any | None = None
        self._sink_state_handler: Callable[[], None] | None = None
        self._sink_started = False
        self._discard_transport = False
        self._active = False
        self._error_text = ""
        self._performance = performance_recorder or PerformanceRecorder()
        self._trace_job_id: str | None = None
        self._consecutive_restarts = 0
        self._last_restart_monotonic = 0.0
        # play_buffer() completion: single-shot, armed per replay, disarmed by
        # stop()/start() so sessions it did not arm never see finished.
        self._drain_timer = QTimer(self)
        self._drain_timer.setSingleShot(True)
        self._drain_timer.timeout.connect(self._on_drain_timer)
        # Bulk-feed levels (play_buffer) drip at the audio's own window pace
        # instead of dumping the whole burst in one event — the meter then
        # animates with the sound instead of flashing its final shape.
        self._pending_levels: list[float] = []
        self._level_drip_timer = QTimer(self)
        self._level_drip_timer.setInterval(int(LEVEL_WINDOW_SAMPLES * 1000 / STREAM_SAMPLE_RATE))
        self._level_drip_timer.timeout.connect(self._drip_next_level)

        self._transport: BoundedPcmTransport | None = None
        self._transport_timer = QTimer(self)
        self._transport_timer.setInterval(20)
        self._transport_timer.timeout.connect(self.notify_transport_available)

    @Property(bool, notify=activeChanged)
    def active(self) -> bool:
        return self._active

    @Property(str, notify=errorTextChanged)
    def errorText(self) -> str:
        return self._error_text

    # ── slots ───────────────────────────────────────────────────────────────

    def set_performance_recorder(self, recorder: PerformanceRecorder) -> None:
        self._performance = recorder

    def begin_trace(self, job_id: str | None) -> None:
        self._trace_job_id = job_id

    def start(
        self,
        transport: BoundedPcmTransport | None = None,
        job_id: str | None = None,
    ) -> None:
        """Open playback; transport sessions wait for their prebuffer."""
        self._drain_timer.stop()
        self._transport_timer.stop()
        if self._active:
            self._shutdown_session()
            logger.debug("stream restarted mid-session")
        self._transport = transport
        self._sink_started = False
        self._discard_transport = False
        self._consecutive_restarts = 0
        self._last_restart_monotonic = 0.0
        if job_id is not None:
            self._trace_job_id = job_id
        if transport is None:
            self._io = StreamIODevice(self)
        else:
            self._io = TransportIODevice(
                transport,
                self,
                on_first_read=self._on_first_sink_pull,
                on_read=self._on_sink_read_data,
            )
            self._transport_timer.start()
        self._set_active(True)
        self._performance.mark(self._trace_job_id, "audio_session_started")
        if transport is None:
            self._ensure_sink(start_now=True)
        else:
            # Build the device before handing a transport to the worker. A
            # failed backend must fall back to artifact-only synthesis rather
            # than letting the producer block on an unread transport.
            self._ensure_sink(start_now=False)
            self.notify_transport_available()

    def notify_transport_available(self) -> None:
        """Wake the GUI-owned device after producer-side transport writes."""
        transport = self._transport
        if not self._active or transport is None:
            self._transport_timer.stop()
            return
        if self._discard_transport:
            self._discard_available_transport()
            return
        if not self._sink_started and transport.ready_for_prebuffer():
            if self._ensure_sink(start_now=True):
                self._sink_started = True
            else:
                self._enter_transport_fallback()
                return
        if self._sink_started and transport.available_bytes() and self._sink_is_stalled():
            now = time.monotonic()
            if (now - self._last_restart_monotonic) * 1000 >= MIN_RESTART_INTERVAL_MS:
                if self._consecutive_restarts >= MAX_CONSECUTIVE_AUDIO_RESTARTS:
                    logger.warning(
                        "audio sink stalled repeatedly (%d times); entering fallback",
                        self._consecutive_restarts,
                    )
                    self._enter_transport_fallback()
                    return
                self._consecutive_restarts += 1
                self._last_restart_monotonic = now
                self._performance.increment(self._trace_job_id, "audio_restarts")
                self._stop_sink_quietly()
                if not self._start_sink(self._require_io()):
                    self._enter_transport_fallback()
                    return
        if self._io is not None and transport.available_bytes():
            self._io.readyRead.emit()

    def begin_drain(self) -> None:
        """Close transport after producer completion while allowing drain."""
        if self._transport is not None:
            self._transport.close(discard=False)
            self.notify_transport_available()

    def stop(self, *, discard: bool = True) -> None:
        """Hard-stop playback and drop buffered bytes (immediate silence)."""
        self._drain_timer.stop()  # manual stop ends the replay without finished()
        self._pending_levels.clear()
        self._level_drip_timer.stop()
        if not self._active and self._io is None:
            return  # never started — idempotent no-op
        self._performance.mark(self._trace_job_id, "audio_session_stopped")
        self._shutdown_session(discard=discard)
        self._io = None
        self._set_active(False)

    def buffered_drain_ms(self) -> int:
        """Real-time duration of the audio still buffered in the sink.

        The done path keeps its UI session (live meter) flagged until this
        drains, so the meter dies with the last audible sample instead of
        with the worker's last chunk (bead rqy).
        """
        io = self._io
        if not self._active or io is None:
            return 0
        return int(len(io) * 1000 / (STREAM_SAMPLE_RATE * 4))  # mono float32

    def feed(self, chunk: Any, pace_levels: bool = False) -> None:
        """Consume one VARIABLE-size float32 mono chunk during a session.

        Emits ``levelReady(peak)`` per ~120 ms window (single whole-chunk
        emission for small chunks); appends little-endian float32 bytes to
        the ring buffer, restarting the sink first if it stalled (underrun).
        Outside a session chunks are dropped entirely (documented choice).
        ``pace_levels`` (bulk replay feeds) drips the windows at audio pace
        instead of emitting the burst at once.
        """
        if not self._active:
            return
        samples = np.asarray(chunk, dtype=np.float32).ravel()
        self._emit_levels(samples, paced=pace_levels)
        if samples.size == 0:
            return
        io = self._io
        if io is None:  # keep static analyzers honest; active ⇒ io exists
            return
        if self._sink_is_stalled():
            self._performance.increment(self._trace_job_id, "audio_restarts")
            self._stop_sink_quietly()
            self._start_sink(io)  # restart against the SAME ring buffer
        if self._sink is not None:
            # memoryview skips the tobytes() copy — bytearray.extend copies
            # once instead of twice (a capped 5 MB replay saved ~10 MB of
            # transient allocation per bulk feed).
            payload = memoryview(np.ascontiguousarray(samples, dtype="<f4")).cast("B")
            io.append_bytes(payload)

    def play_buffer(self, samples: Any) -> bool:
        """Replay one COMPLETE buffer: fresh session, single feed, drain timer.

        Returns True when a live sink session is replaying; False (session
        torn down, errorText explains) for empty buffers or sink failure —
        a replay must never leave a half-dead session behind.

        Completion: everything is fed up-front, so the sink drains exactly
        the buffer's real-time duration after start-up; a single-shot QTimer
        sized to duration + REPLAY_DRAIN_MARGIN_MS then closes the session
        and emits ``finished``. (The real QAudioSink's stateChanged is
        deliberately not connected — see _ensure_sink — so drain detection
        cannot pigyback on it.) stop()/start() disarm the timer: sessions
        they end or begin never see finished.
        """
        samples = np.asarray(samples, dtype=np.float32).ravel()
        if samples.size == 0:
            return False
        self.start()
        if self._error_text:
            self.stop()
            return False
        self.feed(samples, pace_levels=True)
        duration_ms = samples.size * 1000 // STREAM_SAMPLE_RATE
        self._drain_timer.start(duration_ms + REPLAY_DRAIN_MARGIN_MS)
        return True

    # ── internals ───────────────────────────────────────────────────────────

    def _emit_levels(self, samples: np.ndarray, paced: bool = False) -> None:
        """levelReady per ~120 ms window; whole chunk when it is that small.

        ``paced`` queues everything after the first window for the drip
        timer (bulk feeds; see _level_drip_timer).
        """
        n = int(samples.size)
        if n <= LEVEL_WINDOW_SAMPLES:
            self.levelReady.emit(_peak_level(samples))
            return
        windows = min(
            (n + LEVEL_WINDOW_SAMPLES - 1) // LEVEL_WINDOW_SAMPLES,
            MAX_LEVEL_EMISSIONS_PER_CHUNK,
        )
        peaks = [
            _peak_level(samples[i * LEVEL_WINDOW_SAMPLES : (i + 1) * LEVEL_WINDOW_SAMPLES])
            for i in range(windows)
        ]
        if not paced:
            for peak in peaks:
                self.levelReady.emit(peak)
            return
        self.levelReady.emit(peaks[0])
        self._pending_levels.extend(peaks[1:])
        if self._pending_levels:
            self._level_drip_timer.start()

    def _drip_next_level(self) -> None:
        if not self._active or not self._pending_levels:
            self._level_drip_timer.stop()
            return
        self.levelReady.emit(self._pending_levels.pop(0))
        if not self._pending_levels:
            self._level_drip_timer.stop()

    def _ensure_sink(self, *, start_now: bool) -> bool:
        """Build + wire the sink lazily; False means unavailable (error set)."""
        if self._sink is None:
            try:
                audio_format = self._format_factory()
                sink = self._sink_factory(audio_format)
            except Exception:  # noqa: BLE001 - playback must never crash synthesis
                logger.exception("audio sink construction failed")
                self._sink = None
                self._set_error(self.tr(AUDIO_PLAYBACK_UNAVAILABLE))
                return False
            self._sink = sink
            state_changed = getattr(sink, "stateChanged", None)
            if state_changed is not None and hasattr(state_changed, "connect"):
                self._sink_state_handler = lambda: self._on_sink_state_changed()
                state_changed.connect(self._sink_state_handler)
            # Device-level failures (unplugged headset, missing Linux audio
            # backend) surface here — unlike stateChanged this is wired for
            # the real sink too, otherwise the session dies silently.
            error_occurred = getattr(sink, "errorOccurred", None)
            if error_occurred is not None and hasattr(error_occurred, "connect"):
                error_occurred.connect(self._on_sink_error)
            self._set_error("")  # construction recovered from a prior failure
        if start_now:
            if isinstance(self._io, StreamIODevice):
                self._clear_buffer_quietly()
            return self._start_sink(self._require_io())
        return True

    def _require_io(self) -> StreamIODevice | TransportIODevice:
        assert self._io is not None, "feed/start require an open session"
        return self._io

    def _start_sink(self, io: StreamIODevice | TransportIODevice) -> bool:
        if self._sink is None:
            return False
        try:
            self._sink.start(io)
            self._sink_started = True
            return True
        except Exception:  # noqa: BLE001 - a dead backend must not kill the UI
            logger.exception("starting audio sink failed")
            self._sink = None
            self._set_error(self.tr(AUDIO_PLAYBACK_UNAVAILABLE))
            return False

    def _stop_sink_quietly(self) -> None:
        if self._sink is None:
            return
        try:
            self._sink.stop()
        except Exception:  # noqa: BLE001 - stopping must never raise into the UI
            logger.exception("stopping audio sink failed")

    def _sink_is_stalled(self) -> bool:
        if self._sink is None:
            return False
        try:
            name = _enum_name(self._sink.state())
        except Exception:  # noqa: BLE001 - probe failures look like a stall
            logger.exception("reading sink state failed")
            return True
        return name in _RESTART_STATE_NAMES

    def _clear_buffer_quietly(self) -> None:
        if isinstance(self._io, StreamIODevice):
            self._io.clear_buffer()

    def _shutdown_session(self, *, discard: bool = True) -> None:
        """Stop the sink and discard buffered bytes (hard stop, FR-4.2 cancel)."""
        self._stop_sink_quietly()
        if self._transport is not None:
            self._transport.close(discard=discard)
            self._transport = None
        self._sink_started = False
        self._discard_transport = False
        self._consecutive_restarts = 0
        self._clear_buffer_quietly()

    def _discard_available_transport(self) -> None:
        transport = self._transport
        if transport is None:
            return
        while transport.available_bytes():
            try:
                transport.take(transport.available_bytes())
            except TransportClosed:
                return

    def _enter_transport_fallback(self) -> None:
        """Keep the producer alive while discarding failed live playback bytes."""
        if self._transport is None or self._discard_transport:
            return
        self._discard_transport = True
        self._sink_started = False
        self._stop_sink_quietly()
        self._discard_available_transport()
        self.livePlaybackFailed.emit()

    def _on_sink_state_changed(self) -> None:
        sink = self._sink
        if sink is None:
            return
        try:
            name = _enum_name(sink.state())
        except Exception:  # noqa: BLE001 - a failed state probe cannot crash the UI
            logger.exception("reading audio sink state failed")
            return
        logger.debug("audio sink state changed: %s", name)
        if (
            name not in _RESTART_STATE_NAMES
            or self._transport is None
            or self._discard_transport
            or not self._sink_started
        ):
            return
        error = getattr(sink, "error", None)
        if not callable(error):
            return
        try:
            error_name = _enum_name(error())
        except Exception:  # noqa: BLE001 - an uninspectable sink is not fatal
            logger.exception("reading audio sink error failed")
            return
        if error_name in ("NoError", "UnderrunError"):
            return
        logger.warning("audio sink stopped with error: %s", error_name)
        self._set_error(self.tr(AUDIO_PLAYBACK_UNAVAILABLE))
        self._enter_transport_fallback()

    def _on_sink_error(self, error: Any) -> None:
        # Underrun is transient mid-stream (the feed-time restart path owns
        # it); anything else means the device/backend is gone.
        name = _enum_name(error)
        if name in ("NoError", "UnderrunError"):
            logger.debug("audio sink error (benign): %s", name)
            return
        logger.warning("audio sink error: %s", name)
        self._set_error(self.tr(AUDIO_PLAYBACK_UNAVAILABLE))
        self._enter_transport_fallback()

    def _on_first_sink_pull(self) -> None:
        self._consecutive_restarts = 0
        self._performance.mark(self._trace_job_id, "audio_first_sink_pull")

    def _on_sink_read_data(self, count: int) -> None:
        if count > 0:
            self._consecutive_restarts = 0

    def _on_drain_timer(self) -> None:
        """Replay window elapsed: close the session, then announce finished."""
        if not self._active:
            return
        self.stop()
        self.finished.emit()

    def _set_active(self, value: bool) -> None:
        if value != self._active:
            self._active = value
            self.activeChanged.emit()

    def _set_error(self, text: str) -> None:
        if text != self._error_text:
            self._error_text = text
            self.errorTextChanged.emit()
