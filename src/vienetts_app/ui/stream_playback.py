"""Streaming playback: QAudioSink pulling from the bounded PCM transport.

Owns the audio half of live preview. The inference worker writes float32
little-endian mono PCM (48 kHz) into a ``BoundedPcmTransport`` (2 s cap);
``TransportIODevice`` adapts that transport to the pull-mode ``QIODevice`` a
QAudioSink reads from, converting to PCM Int16 when the default output
rejected Float32. Levels for the QML meter come from the worker's job-chunk
peaks, not from this module.

The real QtMultimedia objects are constructed lazily on the first ``start()``
via injectable factories — importing this module and constructing the
controller never loads QtMultimedia (same posture as ui/playback.py, NFR-2.1).

QML surface (aggregated by AppController; never registered directly):
    active     bool, NOTIFY activeChanged — true between start() and stop()
    errorText  str, NOTIFY errorTextChanged — sink-construction failure message
    livePlaybackFailed() Signal — the sink died; transport bytes are now
               discarded so the producer (artifact-first synthesis) continues

Session lifecycle:
    start(transport, job_id)  opens a session: any previous one is torn down
             first, the format/sink are built, and the sink starts pulling
             once the transport holds its prebuffer.
    begin_drain()  producer finished: close the transport without discarding
             so the buffered tail plays out.
    stop()   hard stop: sink.stop() + transport closed with discard →
             immediate silence (what a cancel wants).
    Sink construction/start failures never raise: they are logged, surfaced
             through ``errorText``, and the transport enters discard mode so
             the producer never blocks on an unread transport.

File-backed feeding: the worker never waits for the sink — it publishes each
chunk after writing it to the artifact, and what does not fit in the 2 s
transport stays in the file as a backlog. The 20 ms timer below ``refill``s
the transport from that backlog, in order, so a slow or stalled sink delays
the listener and never synthesis. The drain estimate counts the backlog.

Underrun tolerance: QAudioSink flips to Idle/Stopped when it starves mid-stream
and does not reliably resume pulling on its own. ``notify_transport_available``
(driven by a 20 ms timer) restarts a stalled sink against the SAME device when
new bytes arrive, rate-limited and capped before falling back to discard mode.

Default factory seams (each lazily imports PySide6.QtMultimedia INSIDE the
function; tests pass fakes and stay QtMultimedia-free):
    sink_factory(audio_format) -> Any    default: real ``QAudioSink(format)``
    format_factory() -> Any              default: 48 kHz / mono / Float32
                                         ``QAudioFormat``, negotiated down to
                                         PCM Int16 when the default output
                                         rejects Float32 (the device converts)

Fake-sink contract (tests; plain duck types, ZERO QtMultimedia usage):
    The controller builds the format via ``format_factory()`` then the sink via
    ``sink_factory(format)``, and afterwards only ever calls/queries the sink for:
      start(io_device)   begin pulling from our transport QIODevice
      stop()             halt playback
      state()            Qt audio STATE enum OR its member-name string
                         ("ActiveState" | "IdleState" | "StoppedState" ...)
    plus OPTIONAL ``stateChanged``/``errorOccurred`` signals and ``error()``
    (connected/queried via getattr when present, so a fake WITHOUT them works).
"""

from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import Property, QIODevice, QObject, QTimer, Signal

from vienetts_app.core.pcm_transport import BoundedPcmTransport, TransportClosed
from vienetts_app.core.performance import PerformanceRecorder

logger = logging.getLogger(__name__)

STREAM_SAMPLE_RATE = 48_000  # infer/infer_stream synthesis rate (denoise ≠ this)
STREAM_CHANNEL_COUNT = 1

AUDIO_PLAYBACK_UNAVAILABLE = "Hệ thống này không phát được âm thanh."

# Sink states meaning "the sink stopped consuming" mid-session (see _enum_name).
_RESTART_STATE_NAMES = frozenset({"StoppedState", "IdleState"})

# Sink restart pacing & fallback limits: prevent WASAPI COM thread thrashing
# and Access Violation crashes on Windows when audio buffer starves repeatedly.
MAX_CONSECUTIVE_AUDIO_RESTARTS = 5
MIN_RESTART_INTERVAL_MS = 60


def _default_format_factory() -> Any:
    """Production seam: 48 kHz / mono, Float32 unless the device rejects it."""
    # Imported here (not at module top) so importing this module or building
    # the controller with injected factories never loads QtMultimedia.
    from PySide6.QtMultimedia import QAudioFormat

    fmt = QAudioFormat()
    fmt.setSampleRate(STREAM_SAMPLE_RATE)
    fmt.setChannelCount(STREAM_CHANNEL_COUNT)
    fmt.setSampleFormat(QAudioFormat.SampleFormat.Float)
    return _negotiate_sink_format(fmt)


def _int16_stream_format() -> Any:
    """48 kHz / mono PCM-Int16 fallback for Float32-rejecting outputs."""
    from PySide6.QtMultimedia import QAudioFormat

    fmt = QAudioFormat()
    fmt.setSampleRate(STREAM_SAMPLE_RATE)
    fmt.setChannelCount(STREAM_CHANNEL_COUNT)
    fmt.setSampleFormat(QAudioFormat.SampleFormat.Int16)
    return fmt


def _make_stream_format() -> Any:
    """Public alias of the default format builder (unit-testable seam)."""
    return _default_format_factory()


def _default_sink_factory(audio_format: Any) -> Any:
    """Production seam: real ``QAudioSink`` for the given format."""
    # Same lazy-import posture as the format factory above.
    from PySide6.QtMultimedia import QAudioSink

    return QAudioSink(audio_format)


def _negotiate_sink_format(preferred: Any) -> Any:
    """Keep ``preferred`` when the default output accepts it, else Int16.

    Some Windows outputs (WASAPI exclusive, BT headsets) reject Float32: the
    sink then fails at start() or reports device errors mid-session. Probing
    ``isFormatSupported`` here moves that failure to format choice, where
    the live device can convert samples to match. Any probe problem (no device,
    headless/offscreen null device, Qt without the call) keeps ``preferred``.
    """
    try:
        from PySide6.QtMultimedia import QMediaDevices

        device = QMediaDevices.defaultAudioOutput()
        if device.isNull() or device.isFormatSupported(preferred):
            return preferred
        fallback = _int16_stream_format()
        if device.isFormatSupported(fallback):
            logger.info("audio device rejects Float32; falling back to PCM Int16")
            return fallback
        return preferred
    except Exception:  # noqa: BLE001 - probe failures keep the preferred format
        logger.debug("audio format probe failed; keeping preferred format", exc_info=True)
        return preferred


def _format_is_int16(audio_format: Any) -> bool:
    """True when a negotiated format needs Int16 sample conversion."""
    try:
        return _enum_name(audio_format.sampleFormat()) == "Int16"
    except Exception:  # noqa: BLE001 - fakes without sampleFormat() are float
        return False


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


def _float32_to_int16(payload: bytes) -> bytes:
    """Little-endian float32 PCM → clipped, scaled little-endian int16 PCM.

    Non-finite samples map to silence/full scale like ``np.nan_to_num``;
    scaling truncates toward zero (0.5 → 16383), matching the old push path.
    """
    import numpy as np  # deferred: only Int16 fallback sinks pay for it

    samples = np.frombuffer(payload, dtype="<f4")
    finite = np.nan_to_num(samples, nan=0.0, posinf=1.0, neginf=-1.0)
    return (np.clip(finite, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()


class TransportIODevice(QIODevice):
    """QIODevice adapter that reads PCM from a bounded transport.

    The transport always carries little-endian float32. When the negotiated
    sink format is Int16 (``int16=True`` / ``set_int16``) reads take whole
    float32 samples — two transport bytes per output byte — and convert them,
    so a request never splits a sample; sizes reported to Qt (and the drain
    estimate) are in OUTPUT bytes.
    """

    def __init__(
        self,
        transport: BoundedPcmTransport,
        parent: QObject | None = None,
        on_first_read: Callable[[], None] | None = None,
        on_read: Callable[[int], None] | None = None,
        *,
        int16: bool = False,
    ) -> None:
        super().__init__(parent)
        self._transport = transport
        self._on_first_read = on_first_read
        self._on_read = on_read
        self._reported_first_read = False
        self._int16 = int16
        self.open(QIODevice.OpenModeFlag.ReadOnly)

    def set_int16(self, value: bool) -> None:
        self._int16 = bool(value)

    def isSequential(self) -> bool:
        return True

    def _output_bytes(self) -> int:
        available = self._transport.available_bytes()
        return (available // 4) * 2 if self._int16 else available

    def bytesAvailable(self) -> int:  # noqa: N802 - Qt naming
        return self._output_bytes() + super().bytesAvailable()

    def __len__(self) -> int:
        return self._output_bytes()

    def clear_buffer(self) -> None:
        self._transport.close(discard=True)

    def _take(self, max_size: int) -> bytes:
        if not self._int16:
            return self._transport.take(max_size)
        data = self._transport.take((max_size // 2) * 4)
        return _float32_to_int16(data) if data else b""

    def readData(self, maxSize: int) -> bytes:  # noqa: N802 - Qt naming
        try:
            data = self._take(max(0, int(maxSize)))
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


class StreamPlaybackController(QObject):
    """Transport-backed streaming playback (see module docstring for contracts)."""

    activeChanged = Signal()
    errorTextChanged = Signal()
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
        self._io: TransportIODevice | None = None
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
        # Negotiated sample width: Float32 normally, PCM Int16 when the
        # device rejected Float32 (the IO device converts; drain math uses it).
        self._sink_int16 = False
        self._bytes_per_sample = 4

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

    def start(self, transport: BoundedPcmTransport, job_id: str | None = None) -> None:
        """Open playback; the sink starts once the transport holds its prebuffer."""
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
        self._io = TransportIODevice(
            transport,
            self,
            on_first_read=self._on_first_sink_pull,
            on_read=self._on_sink_read_data,
            int16=self._sink_int16,
        )
        self._transport_timer.start()
        self._set_active(True)
        self._performance.mark(self._trace_job_id, "audio_session_started")
        # Build the device before handing a transport to the worker. A failed
        # backend must fall back to artifact-only synthesis rather than
        # letting the producer block on an unread transport.
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
        refill = getattr(transport, "refill", None)
        if callable(refill):
            with contextlib.suppress(TransportClosed):
                refill()
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
        transport = self._transport
        backlog_samples = transport.pending_bytes() // 4 if transport is not None else 0
        buffered_samples = len(io) // self._bytes_per_sample
        return int((buffered_samples + backlog_samples) * 1000 / STREAM_SAMPLE_RATE)

    # ── internals ───────────────────────────────────────────────────────────

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
            self._sink_int16 = _format_is_int16(audio_format)
            self._bytes_per_sample = 2 if self._sink_int16 else 4
            if self._io is not None:
                self._io.set_int16(self._sink_int16)
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
            return self._start_sink(self._require_io())
        return True

    def _require_io(self) -> TransportIODevice:
        assert self._io is not None, "sink start requires an open session"
        return self._io

    def _start_sink(self, io: TransportIODevice) -> bool:
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

    def _shutdown_session(self, *, discard: bool = True) -> None:
        """Stop the sink and discard buffered bytes (hard stop, FR-4.2 cancel)."""
        self._stop_sink_quietly()
        if self._transport is not None:
            self._transport.close(discard=discard)
            self._transport = None
        self._sink_started = False
        self._discard_transport = False
        self._consecutive_restarts = 0

    def _discard_available_transport(self) -> None:
        transport = self._transport
        if transport is None:
            return
        # The backlog is never read back once live playback gave up.
        transport.drop_backlog()
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
        # Underrun is transient mid-stream (notify_transport_available's
        # restart path owns it); anything else means the device/backend is gone.
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

    def _set_active(self, value: bool) -> None:
        if value != self._active:
            self._active = value
            self.activeChanged.emit()

    def _set_error(self, text: str) -> None:
        if text != self._error_text:
            self._error_text = text
            self.errorTextChanged.emit()
