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
    levelReady(float) Signal — peak amplitude (0..1) of each fed chunk
    finished() Signal — play_buffer() replay drained to its end

Level metric (documented choice): ``max(|sample|)`` over the chunk, clamped to
0..1; an empty or all-non-finite chunk yields 0.0. Peak amplitude gives the
Phase 2 WaveformIndicator a cheap rolling envelope without exposing raw
samples to QML.

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

import logging
import math
from typing import Any

import numpy as np
from PySide6.QtCore import Property, QIODevice, QObject, QTimer, Signal

logger = logging.getLogger(__name__)

STREAM_SAMPLE_RATE = 48_000  # infer/infer_stream synthesis rate (denoise ≠ this)
STREAM_CHANNEL_COUNT = 1

# play_buffer() drain allowance on top of the buffer's real-time duration:
# covers sink start-up latency before consumption begins.
REPLAY_DRAIN_MARGIN_MS = 300

AUDIO_PLAYBACK_UNAVAILABLE = "Audio playback is unavailable on this system"

# Sink states meaning "the sink stopped consuming" mid-session (see _enum_name).
_RESTART_STATE_NAMES = frozenset({"StoppedState", "IdleState"})


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


class StreamIODevice(QIODevice):
    """Push-mode ring-buffer QIODevice serving float32 bytes to QAudioSink.

    The controller pushes into ``append_bytes`` (little-endian float32 from
    feed()) which also emits readyRead; the sink PULLS through readData,
    popping up to its requested byte count off the front. Variable chunk
    sizes need no logic beyond appending (FR-4.1).
    """

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._buffer = bytearray()
        self.open(QIODevice.OpenModeFlag.ReadOnly)

    def isSequential(self) -> bool:
        return True

    def bytesAvailable(self) -> int:  # noqa: N802 - Qt naming
        return len(self._buffer) + super().bytesAvailable()

    def __len__(self) -> int:
        return len(self._buffer)

    # ── producer side (main thread) ─────────────────────────────────────────

    def append_bytes(self, payload: bytes) -> None:
        if payload:
            self._buffer.extend(payload)
            self.readyRead.emit()

    def clear_buffer(self) -> None:
        self._buffer.clear()

    def take_bytes(self, max_len: int) -> bytes:
        """Pop at most ``max_len`` bytes off the front (used by sinks AND tests)."""
        data = bytes(self._buffer[:max_len])
        del self._buffer[:max_len]
        return data

    # ── consumer side (called by QAudioSink's pull loop) ────────────────────

    def readData(self, maxSize: int) -> bytes:  # noqa: N802 - Qt naming
        return self.take_bytes(int(maxSize))

    def writeData(self, data: Any) -> int:  # noqa: N802 - Qt naming
        # Push mode: only the SINK reads here; producers go through feed().
        logger.debug("StreamIODevice.writeData forbidden — use feed()")
        return -1


class StreamPlaybackController(QObject):
    """Ring-buffer streaming playback (see module docstring for contracts)."""

    activeChanged = Signal()
    levelReady = Signal(float)
    errorTextChanged = Signal()
    finished = Signal()

    def __init__(
        self,
        sink_factory: Any | None = None,
        format_factory: Any | None = None,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._sink_factory = _default_sink_factory if sink_factory is None else sink_factory
        self._format_factory = _default_format_factory if format_factory is None else format_factory
        self._io: StreamIODevice | None = None
        self._sink: Any | None = None
        self._active = False
        self._error_text = ""
        # play_buffer() completion: single-shot, armed per replay, disarmed by
        # stop()/start() so sessions it did not arm never see finished.
        self._drain_timer = QTimer(self)
        self._drain_timer.setSingleShot(True)
        self._drain_timer.timeout.connect(self._on_drain_timer)

    # ── properties ──────────────────────────────────────────────────────────

    @Property(bool, notify=activeChanged)
    def active(self) -> bool:
        return self._active

    @Property(str, notify=errorTextChanged)
    def errorText(self) -> str:
        return self._error_text

    # ── slots ───────────────────────────────────────────────────────────────

    def start(self) -> None:
        """Open a streaming session, tearing down any previous one first."""
        self._drain_timer.stop()  # a pending replay drain must not fire into this session
        if self._active:
            self._shutdown_session()
            logger.debug("stream restarted mid-session")
        self._io = StreamIODevice(self)
        self._set_active(True)
        self._ensure_sink(start_now=True)

    def stop(self) -> None:
        """Hard-stop playback and drop buffered bytes (immediate silence)."""
        self._drain_timer.stop()  # manual stop ends the replay without finished()
        if not self._active and self._io is None:
            return  # never started — idempotent no-op
        self._shutdown_session()
        self._io = None
        self._set_active(False)

    def feed(self, chunk: Any) -> None:
        """Consume one VARIABLE-size float32 mono chunk during a session.

        Emits ``levelReady(peak)``; appends little-endian float32 bytes to the
        ring buffer, restarting the sink first if it stalled (underrun).
        Outside a session chunks are dropped entirely (documented choice).
        """
        if not self._active:
            return
        samples = np.asarray(chunk, dtype=np.float32).ravel()
        self.levelReady.emit(_peak_level(samples))
        if samples.size == 0:
            return
        io = self._io
        if io is None:  # keep static analyzers honest; active ⇒ io exists
            return
        if self._sink_is_stalled():
            self._stop_sink_quietly()
            self._start_sink(io)  # restart against the SAME ring buffer
        if self._sink is not None:
            payload = np.ascontiguousarray(samples, dtype="<f4").tobytes()
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
        self.feed(samples)
        duration_ms = samples.size * 1000 // STREAM_SAMPLE_RATE
        self._drain_timer.start(duration_ms + REPLAY_DRAIN_MARGIN_MS)
        return True

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
                self._set_error(AUDIO_PLAYBACK_UNAVAILABLE)
                return False
            self._sink = sink
            state_changed = getattr(sink, "stateChanged", None)
            if (
                state_changed is not None
                and hasattr(state_changed, "connect")
                and sink.__class__.__name__ != "QAudioSink"
            ):
                state_changed.connect(self._on_sink_state_changed)
            self._set_error("")  # construction recovered from a prior failure
        if start_now:
            self._clear_buffer_quietly()
            self._start_sink(self._require_io())
        return True

    def _require_io(self) -> StreamIODevice:
        assert self._io is not None, "feed/start require an open session"
        return self._io

    def _start_sink(self, io: StreamIODevice) -> None:
        try:
            self._sink.start(io)
        except Exception:  # noqa: BLE001 - a dead backend must not kill the UI
            logger.exception("starting audio sink failed")
            self._sink = None
            self._set_error(AUDIO_PLAYBACK_UNAVAILABLE)

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
        if self._io is not None:
            self._io.clear_buffer()

    def _shutdown_session(self) -> None:
        """Stop the sink and discard buffered bytes (hard stop, FR-4.2 cancel)."""
        self._stop_sink_quietly()
        self._clear_buffer_quietly()

    def _on_sink_state_changed(self, state: Any) -> None:
        # Debug-only tap: underrun recovery happens at feed()-time against
        # state(), so unknown names are logged and otherwise ignored.
        logger.debug("audio sink state changed: %s", _enum_name(state))

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
