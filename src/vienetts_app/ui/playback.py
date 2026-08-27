"""Full-playback wrapper over QMediaPlayer + QAudioOutput (FR-3.2, §Audio).

A thin QObject exposed to QML for playing back exported WAV files in one
piece (streaming via QAudioSink is Phase 4 — out of scope here). The real
QtMultimedia objects are created lazily on the first ``play`` so importing
this module and constructing the controller never touches the audio stack
(same lazy-construction posture as the engine seam, NFR-2.1).

QML surface (context property ``playback``):
    play(path) @Slot(str)      play a local file (str/Path); blank/None is a
                               no-op that raises errorTextChanged
    stop() @Slot()             stop and clear the source
    pause() @Slot() / resume() best-effort; no-ops when stopped
    state        str, NOTIFY stateChanged — "stopped"|"playing"|"paused"
    sourcePath   str, NOTIFY sourcePathChanged — the file being played
    fileName     str, NOTIFY sourcePathChanged — basename for display
    finished()   Signal — underlying media reached EndOfMedia
    errorText    str, NOTIFY errorTextChanged — last player error, cleared on
                 the next successful play

Fake-player contract (for tests; duck-typed, no QtMultimedia import):
    the controller only ever calls/queries a player for
      setSource(QUrl) / play() / stop() / pause() / resume()
    (``resume`` is optional: QMediaPlayer itself has none — ``play()`` is the
    Qt resume convention, so the wrapper falls back to ``play()``) and only
    connects three signals
      playbackStateChanged(name) — "StoppedState"|"PlayingState"|"PausedState"
                                   (enum member name; str(enum) also accepted)
      mediaStatusChanged(name)   — "EndOfMedia" ends playback (finished())
      errorOccurred(name, text)  — text is stringified into errorText
    Anything else on QMediaPlayer is deliberately untouched.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from PySide6.QtCore import Property, QObject, QUrl, Signal, Slot

logger = logging.getLogger(__name__)

STATE_STOPPED = "stopped"
STATE_PLAYING = "playing"
STATE_PAUSED = "paused"

BLANK_PATH_MESSAGE = "No audio file to play"


def _default_player_factory() -> Any:
    """Production seam: real QMediaPlayer + attached QAudioOutput."""
    # Imported here (not at module top) so merely importing this module and
    # constructing the controller never loads QtMultimedia/backend plugins.
    from PySide6.QtMultimedia import QAudioOutput, QMediaPlayer

    player = QMediaPlayer()
    player.setAudioOutput(QAudioOutput(player))  # child: output dies with player
    return player


def _enum_name(value: Any) -> str:
    """Enum member name, tolerant of both real Qt enums and plain strings.

    ``str(QMediaPlayer.PlayingState)`` is "PlaybackState.PlayingState", so a
    ``split(".")[-1]`` fallback covers str()-received enum values while real
    ``.name`` attributes (and test fakes passing plain strings) pass through.
    """
    name = getattr(value, "name", None)
    if isinstance(name, str) and name:
        return name
    return str(value).split(".")[-1]


class PlaybackController(QObject):
    """Thin QMediaPlayer wrapper: play/stop/pause/resume + state/source/error.

    ``player_factory`` injects the player (tests pass a fake per the contract
    in the module docstring); ``None`` uses the real lazy QtMultimedia setup.
    """

    stateChanged = Signal()
    sourcePathChanged = Signal()
    errorTextChanged = Signal()
    finished = Signal()

    _PLAYBACK_STATE_NAMES = {
        "StoppedState": STATE_STOPPED,
        "PlayingState": STATE_PLAYING,
        "PausedState": STATE_PAUSED,
    }

    def __init__(self, player_factory: Any | None = None, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._player_factory = _default_player_factory if player_factory is None else player_factory
        self._player: Any | None = None  # built lazily on first play
        self._state = STATE_STOPPED
        self._source_path = ""
        self._error_text = ""

    # ── properties ──────────────────────────────────────────────────────────

    @Property(str, notify=stateChanged)
    def state(self) -> str:
        return self._state

    @Property(str, notify=sourcePathChanged)
    def sourcePath(self) -> str:
        return self._source_path

    @Property(str, notify=sourcePathChanged)
    def fileName(self) -> str:
        return Path(self._source_path).name if self._source_path else ""

    @Property(str, notify=errorTextChanged)
    def errorText(self) -> str:
        return self._error_text

    # ── slots ───────────────────────────────────────────────────────────────

    @Slot(str)
    def play(self, path: str | Path | None) -> None:
        """Play a local file end-to-end, replacing anything already playing.

        Blank/None paths are a no-op that notifies ``errorTextChanged``.
        """
        text = "" if path is None else str(path).strip()
        if not text:
            # Do not even construct the player for a no-op.
            self._set_error(BLANK_PATH_MESSAGE)
            return
        player = self._ensure_player()
        if player is None:
            return
        if self._state != STATE_STOPPED:
            player.stop()  # stop() updates state via playbackStateChanged
        player.setSource(QUrl.fromLocalFile(text))
        self._set_source(text)
        self._set_error("")
        player.play()

    @Slot()
    def stop(self) -> None:
        """Stop playback and clear the source."""
        player = self._player
        if player is None:
            return
        player.stop()  # normally emits StoppedState → _on_playback_state_changed
        player.setSource(QUrl())
        self._set_source("")
        if self._state != STATE_STOPPED:  # players that stay silent on stop()
            self._state = STATE_STOPPED
            self.stateChanged.emit()

    @Slot()
    def pause(self) -> None:
        """Best-effort pause; no-op when stopped."""
        if self._state != STATE_PLAYING:
            return
        self._player.pause()

    @Slot()
    def resume(self) -> None:
        """Best-effort resume; no-op unless paused.

        QMediaPlayer has no ``resume()`` — ``play()`` resumes from pause — so
        the wrapper prefers a ``resume()`` method when the player has one
        (test fakes) and falls back to ``play()`` (real QMediaPlayer).
        """
        if self._state != STATE_PAUSED:
            return
        player = self._player
        if hasattr(player, "resume"):
            player.resume()
        else:
            player.play()

    # ── internals ───────────────────────────────────────────────────────────

    def _ensure_player(self) -> Any | None:
        """Construct + wire the player on first use; None if construction fails."""
        if self._player is not None:
            return self._player
        try:
            player = self._player_factory()
        except Exception:  # noqa: BLE001 - playback must never crash the app
            logger.exception("audio player construction failed")
            self._set_error("Audio playback is unavailable on this system")
            return None
        self._player = player
        player.playbackStateChanged.connect(self._on_playback_state_changed)
        player.mediaStatusChanged.connect(self._on_media_status_changed)
        player.errorOccurred.connect(self._on_error_occurred)
        return player

    def _on_playback_state_changed(self, playback_state: Any) -> None:
        name = _enum_name(playback_state)
        state = self._PLAYBACK_STATE_NAMES.get(name)
        if state is None:
            logger.debug("unmapped playback state %r ignored", name)
            return
        if state != self._state:
            self._state = state
            self.stateChanged.emit()

    def _on_media_status_changed(self, status: Any) -> None:
        if _enum_name(status) == "EndOfMedia":
            self.finished.emit()

    def _on_error_occurred(self, error: Any, error_text: Any) -> None:
        message = f"{_enum_name(error)}: {error_text}" if error_text else _enum_name(error)
        self._set_error(message)

    def _set_source(self, path: str) -> None:
        if path != self._source_path:
            self._source_path = path
            self.sourcePathChanged.emit()

    def _set_error(self, text: str) -> None:
        if text != self._error_text:
            self._error_text = text
            self.errorTextChanged.emit()
