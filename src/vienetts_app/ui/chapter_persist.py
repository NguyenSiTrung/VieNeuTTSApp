"""Chapter persistence jobs: WAV/envelope/timeline writes off the GUI thread.

A finished chapter render used to write its WAV (up to ~350 MB at the 60k-char
cap), compute the waveform envelope, and build the transcript timeline on the
GUI thread — freezing the shell after EVERY chapter during render-all and
undermining gapless auto-advance. Jobs here are pure: library + data in, a
result signal out. The controller validates book identity in the callback
before touching any state, mirroring the straggler-signal guards used for
worker signals.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np
from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from vienetts_app.core.audio import (
    DEFAULT_SAMPLE_RATE,
    compute_waveform_envelope,
    compute_waveform_envelope_from_wav,
)
from vienetts_app.core.audiobook import AudiobookError, AudiobookLibrary
from vienetts_app.core.timeline import Timeline, build_timeline, estimate_timeline

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RenderSnapshot:
    """Frozen copy of the controller's timeline capture for one finished render.

    Taken BEFORE the capture resets: the timeline build runs off-thread while
    the next render's progress ticks are already streaming in.
    """

    text: str = ""
    segments: tuple[str, ...] = ()
    segment_samples: tuple[int, ...] = ()


class ChapterPersistSignals(QObject):
    """Result channel shared by all jobs (owned by the executor).

    Emitted from the persist thread; receivers live on the GUI thread, so
    Qt auto-queues the delivery.
    """

    chapterPersisted = Signal(str, int, bool, str)  # book_id, index, ok, error
    envelopeComputed = Signal(str, int, list)  # book_id, index, buckets


class _ChapterPersistJob(QRunnable):
    """Write one chapter's WAV + envelope/timeline sidecars, then report."""

    def __init__(
        self,
        library: AudiobookLibrary,
        book_id: str,
        index: int,
        audio: np.ndarray,
        snapshot: RenderSnapshot,
        signals: ChapterPersistSignals,
    ) -> None:
        super().__init__()
        self._library = library
        self._book_id = book_id
        self._index = index
        self._audio = audio
        self._snapshot = snapshot
        self._signals = signals

    def run(self) -> None:
        ok, error = True, ""
        try:
            self._library.save_chapter_audio(
                self._book_id, self._index, self._audio, DEFAULT_SAMPLE_RATE
            )
        except AudiobookError as exc:
            logger.exception("saving rendered chapter audio failed")
            ok, error = False, str(exc)
        if ok:
            # Cosmetic sidecars: a failure degrades silently — the audio is
            # already safely cached (same posture as the old GUI-thread path).
            try:
                buckets = compute_waveform_envelope(self._audio)
                if buckets:
                    self._library.save_chapter_envelope(self._book_id, self._index, buckets)
            except Exception:  # noqa: BLE001 - overview must never fail a render
                logger.exception("saving chapter waveform envelope failed")
            try:
                self._save_timeline()
            except Exception:  # noqa: BLE001 - sync degrades, playback never needed it
                logger.exception("saving chapter timeline failed")
        self._signals.chapterPersisted.emit(self._book_id, self._index, ok, error)

    def _save_timeline(self) -> None:
        snap = self._snapshot
        if not snap.segments or self._audio.size == 0:
            return
        timeline: Timeline
        if sum(snap.segment_samples) == int(self._audio.size):
            timeline = build_timeline(
                snap.text, list(snap.segments), list(snap.segment_samples), DEFAULT_SAMPLE_RATE
            )
        else:
            timeline = estimate_timeline(
                snap.text,
                round(int(self._audio.size) * 1000 / DEFAULT_SAMPLE_RATE),
                list(snap.segments),
            )
        self._library.save_chapter_timeline(self._book_id, self._index, timeline)


class _LegacyEnvelopeJob(QRunnable):
    """Compute (block-wise) + persist the envelope of a pre-sidecar chapter.

    Chapters cached before sidecars existed have no overview; decoding the
    whole WAV for one froze the GUI right after play started. Streams the
    file instead, and lands the result through ``envelopeComputed``.
    """

    def __init__(
        self,
        library: AudiobookLibrary,
        book_id: str,
        index: int,
        wav_path: Path,
        signals: ChapterPersistSignals,
    ) -> None:
        super().__init__()
        self._library = library
        self._book_id = book_id
        self._index = index
        self._wav_path = wav_path
        self._signals = signals

    def run(self) -> None:
        try:
            buckets = compute_waveform_envelope_from_wav(self._wav_path)
        except Exception:  # noqa: BLE001 - unreadable audio: flat overview
            logger.exception("computing chapter waveform envelope failed")
            return
        if not buckets:
            return
        self._signals.envelopeComputed.emit(self._book_id, self._index, buckets)
        try:
            self._library.save_chapter_envelope(self._book_id, self._index, buckets)
        except AudiobookError:  # noqa: BLE001 - persistence is best-effort
            logger.exception("saving computed chapter waveform failed")


class PersistExecutor:
    """Submit chapter persistence jobs; results land on the GUI thread."""

    def __init__(self) -> None:
        self.signals = ChapterPersistSignals()

    def submit_chapter(
        self,
        library: AudiobookLibrary,
        book_id: str,
        index: int,
        audio: np.ndarray,
        snapshot: RenderSnapshot,
    ) -> None:
        raise NotImplementedError

    def submit_legacy_envelope(
        self, library: AudiobookLibrary, book_id: str, index: int, wav_path: Path
    ) -> None:
        raise NotImplementedError

    def flush(self, timeout_ms: int = 5000) -> None:
        """Wait for in-flight jobs (shutdown path)."""
        raise NotImplementedError


class ThreadPoolPersistExecutor(PersistExecutor):
    """Production executor: one background thread (disk-bound, ordered)."""

    def __init__(self) -> None:
        super().__init__()
        self._pool = QThreadPool()
        self._pool.setMaxThreadCount(1)

    def submit_chapter(
        self,
        library: AudiobookLibrary,
        book_id: str,
        index: int,
        audio: np.ndarray,
        snapshot: RenderSnapshot,
    ) -> None:
        self._pool.start(_ChapterPersistJob(library, book_id, index, audio, snapshot, self.signals))

    def submit_legacy_envelope(
        self, library: AudiobookLibrary, book_id: str, index: int, wav_path: Path
    ) -> None:
        self._pool.start(_LegacyEnvelopeJob(library, book_id, index, wav_path, self.signals))

    def flush(self, timeout_ms: int = 5000) -> None:
        self._pool.waitForDone(timeout_ms)


class SyncPersistExecutor(PersistExecutor):
    """Test executor: jobs run inline on the calling (GUI) thread."""

    def submit_chapter(
        self,
        library: AudiobookLibrary,
        book_id: str,
        index: int,
        audio: np.ndarray,
        snapshot: RenderSnapshot,
    ) -> None:
        _ChapterPersistJob(library, book_id, index, audio, snapshot, self.signals).run()

    def submit_legacy_envelope(
        self, library: AudiobookLibrary, book_id: str, index: int, wav_path: Path
    ) -> None:
        _LegacyEnvelopeJob(library, book_id, index, wav_path, self.signals).run()

    def flush(self, timeout_ms: int = 5000) -> None:
        return None
