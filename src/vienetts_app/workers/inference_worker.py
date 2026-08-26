"""Dedicated QThread worker owning the TTSEngine (§5, FR-1.7, NFR-2).

Requests are serialized through a thread-safe queue; exactly one worker
thread touches the engine. Cancel is cooperative: the flag is checked
between stream chunks (the SDK cannot cancel mid-chunk — §11).
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

import numpy as np
from PySide6.QtCore import QThread, Signal

from vienetts_app.core.engine import TTSEngine, TTSEngineError
from vienetts_app.core.models import TTSProgress, TTSRequest

logger = logging.getLogger(__name__)

CANCELLED_MESSAGE = "Cancelled by user"


class InferenceWorker(QThread):
    """Serializes TTS requests onto one thread; owns the single engine."""

    progress = Signal(object)  # TTSProgress
    chunk_ready = Signal(object)  # np.float32 chunk (stream mode)
    done = Signal(object)  # np.ndarray (infer/stream) | list[np.ndarray] (batch)
    error = Signal(str)

    _POLL_SECONDS = 0.05

    def __init__(self, engine: TTSEngine | Any, parent: Any | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self._queue: queue.Queue[TTSRequest | None] = queue.Queue()
        self._cancel = threading.Event()
        self._stop = threading.Event()

    # ── public API (call from any thread) ───────────────────────────────────

    def submit(self, request: TTSRequest) -> None:
        """Queue a request; clears any stale cancel flag."""
        self._cancel.clear()
        self._queue.put(request)

    def cancel(self) -> None:
        """Cooperative cancel: stops the in-flight request at the next chunk
        boundary and drops everything still queued."""
        self._cancel.set()
        while True:
            try:
                self._queue.get_nowait()
            except queue.Empty:
                break

    def stop(self) -> None:
        """Stop the worker thread and release it."""
        self._stop.set()
        self._queue.put(None)
        if not self.wait(5000):
            logger.warning("inference worker did not stop in time")

    # ── worker thread body ──────────────────────────────────────────────────

    def run(self) -> None:  # noqa: D102 – QThread override
        while not self._stop.is_set():
            try:
                request = self._queue.get(timeout=self._POLL_SECONDS)
            except queue.Empty:
                continue
            if request is None or self._stop.is_set():
                break
            self._process(request)
        logger.debug("inference worker loop exited")

    def _process(self, request: TTSRequest) -> None:
        try:
            self.progress.emit(TTSProgress(done=0, total=0, stage="init"))
            if request.mode == "stream":
                self._process_stream(request)
            elif request.mode == "batch":
                self._process_batch(request)
            else:
                self._process_infer(request)
        except TTSEngineError as exc:
            self.error.emit(str(exc))
        except Exception as exc:  # noqa: BLE001 - worker must never die silently
            logger.exception("unexpected worker error")
            self.error.emit(f"Unexpected error: {exc}")

    def _check_cancelled(self) -> bool:
        if self._cancel.is_set():
            self.error.emit(CANCELLED_MESSAGE)
            return True
        return False

    def _process_infer(self, request: TTSRequest) -> None:
        self.progress.emit(TTSProgress(done=0, total=1, stage="synthesizing"))
        audio = self.engine.infer(request.text, voice=request.voice, ref_audio=request.ref_audio)
        if self._check_cancelled():
            return
        self.progress.emit(TTSProgress(done=1, total=1, stage="synthesizing"))
        self.done.emit(audio)

    def _process_stream(self, request: TTSRequest) -> None:
        chunks: list[np.ndarray] = []
        for chunk in self.engine.infer_stream(request.text, voice=request.voice):
            if self._cancel.is_set():
                self.error.emit(CANCELLED_MESSAGE)
                return
            chunks.append(np.asarray(chunk, dtype=np.float32))
            self.chunk_ready.emit(chunks[-1])
            self.progress.emit(
                TTSProgress(done=len(chunks), total=len(chunks), stage="synthesizing")
            )
        self.done.emit(np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32))

    def _process_batch(self, request: TTSRequest) -> None:
        texts = [request.text]
        self.progress.emit(TTSProgress(done=0, total=len(texts), stage="synthesizing"))
        audios = self.engine.infer_batch(texts, voice=request.voice)
        if self._check_cancelled():
            return
        self.progress.emit(TTSProgress(done=len(texts), total=len(texts), stage="synthesizing"))
        self.done.emit(audios)
