"""Dedicated QThread worker owning the TTSEngine (§5, FR-1.7, NFR-2).

Requests are serialized through a thread-safe queue; exactly one worker
thread touches the engine. Cancel is cooperative: the flag is checked
between stream chunks (the SDK cannot cancel mid-chunk — §11).

The queue carries ``TTSRequest`` (synthesis), ``VoiceOp`` (voice add/remove/
denoise, FR-3.4), or ``None`` (stop sentinel). Voice ops report through the
separate ``voice_op_done`` signal — they produce no synthesis audio, so they
never emit ``done``.
"""

from __future__ import annotations

import logging
import queue
import threading
from typing import Any

import numpy as np
from PySide6.QtCore import QThread, Signal

from vienetts_app.core.engine import TTSEngine, TTSEngineError, split_text_for_streaming
from vienetts_app.core.models import TTSProgress, TTSRequest, VoiceOp

logger = logging.getLogger(__name__)

CANCELLED_MESSAGE = "Cancelled by user"


class InferenceWorker(QThread):
    """Serializes TTS requests onto one thread; owns the single engine."""

    progress = Signal(object)  # TTSProgress
    chunk_ready = Signal(object)  # np.float32 chunk (stream mode)
    done = Signal(object)  # np.ndarray (infer/stream) | list[np.ndarray] (batch)
    error = Signal(str)
    voice_op_done = Signal(object)  # dict payload, see _process_voice_op

    _POLL_SECONDS = 0.05

    def __init__(self, engine: TTSEngine | Any, parent: Any | None = None) -> None:
        super().__init__(parent)
        self.engine = engine
        self._queue: queue.Queue[TTSRequest | VoiceOp | None] = queue.Queue()
        self._cancel = threading.Event()
        self._stop = threading.Event()

    # ── public API (call from any thread) ───────────────────────────────────

    def submit(self, request: TTSRequest | VoiceOp) -> None:
        """Queue a request or voice op.

        The cancel flag is deliberately NOT cleared here: clearing at enqueue
        time (caller thread) reopened a race where cancel(job A in flight)
        followed by submit(job B) un-cancelled A, so A's stale ``done`` landed
        on top of B's state. The flag is cleared at the START of ``_process``
        in the worker thread instead — a fresh job never inherits a cancel,
        while the in-flight job keeps seeing it until the next job begins.
        """
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

    def stop(self) -> bool:
        """Stop the worker thread and release it.

        Sets the cancel flag too, so an in-flight STREAM job bails at its
        next chunk/segment boundary instead of synthesizing on after the
        caller gave up (a plain ``infer`` call cannot be interrupted mid-call).
        Returns True when the thread finished (within the wait budget).
        """
        self._stop.set()
        self._cancel.set()
        self._queue.put(None)
        if not self.wait(5000):
            logger.warning("inference worker did not stop in time")
            return False
        return True

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

    def _process(self, request: TTSRequest | VoiceOp) -> None:
        # Fresh job starts un-cancelled (see submit): clear AFTER dequeue, in
        # this thread, so a cancel aimed at the previous in-flight job stays
        # visible to it right up until this job actually starts. Residual
        # window: a cancel landing between queue.get and this line misses both
        # jobs — accepted (microseconds, consequence is one normal completion).
        self._cancel.clear()
        try:
            if isinstance(request, VoiceOp):
                self._process_voice_op(request)
                return
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

    def _process_voice_op(self, op: VoiceOp) -> None:
        """Run a voice-management job on the engine thread (FR-3.4).

        add/remove persist the voice registry afterwards (redirected away from
        the SDK's site-packages default — engine.persist_voices). denoise
        returns the cleaned clip through the payload at its native 44.1 kHz.
        Errors flow through the shared ``error`` signal like synthesis errors.
        """
        if op.op == "add":
            self.engine.add_voice(op.name, op.clip_path, denoise=op.denoise, save=False)
            self.engine.persist_voices()
            self.voice_op_done.emit({"op": "add", "name": op.name})
        elif op.op == "remove":
            self.engine.remove_voice(op.name, save=False)
            self.engine.persist_voices()
            self.voice_op_done.emit({"op": "remove", "name": op.name})
        else:
            audio, sample_rate = self.engine.denoise(op.clip_path)
            self.voice_op_done.emit({"op": "denoise", "audio": audio, "sample_rate": sample_rate})

    def _check_cancelled(self) -> bool:
        # _stop counts as a cancel too: shutdown() must silence the request
        # that was in flight when it fired, not just user cancels.
        if self._cancel.is_set() or self._stop.is_set():
            self.error.emit(CANCELLED_MESSAGE)
            return True
        return False

    def _process_infer(self, request: TTSRequest) -> None:
        self.progress.emit(TTSProgress(done=0, total=1, stage="synthesizing"))
        audio = self.engine.infer(
            request.text,
            voice=request.voice,
            ref_audio=request.ref_audio,
            temperature=request.temperature,
        )
        if self._check_cancelled():
            return
        self.progress.emit(TTSProgress(done=1, total=1, stage="synthesizing"))
        self.done.emit(audio)

    def _process_stream(self, request: TTSRequest) -> None:
        """Stream synthesis through CHUNKED segmentation (FR-4.6d).

        The text is split into ≤DEFAULT_MAX_CHARS segments at sentence
        boundaries (``split_text_for_streaming``, imported from core.engine —
        deliberately a module-level pure function rather than an engine
        attribute, so the duck-typed engine contract stays ``infer_stream``
        only and test fakes/third-party engines need no changes). Each
        segment is dispatched to its own engine.infer_stream call and chunks
        yielded straight through, bounding the largest single SDK workload
        regardless of document length (ONNX arena plateau, bead u5c).

        Progress becomes meaningful: total = segment count known at submit
        time, done = completed segments. Cancel is cooperative BETWEEN chunks
        AND between segments. Everything else is unchanged: chunk_ready per
        chunk in order, done with the concatenated audio (empty float32 array
        when nothing was produced), CANCELLED_MESSAGE on the error signal,
        exceptions propagating to the _process catch as before.
        """
        # TTSRequest rejects blank text, so segmentation always yields ≥1
        # segment; the guard keeps the concatenation contract total anyway.
        segments = split_text_for_streaming(request.text)
        total = len(segments) if segments else 1
        self.progress.emit(TTSProgress(done=0, total=total, stage="synthesizing"))
        chunks: list[np.ndarray] = []
        for index, segment in enumerate(segments or [request.text]):
            if self._aborted():  # between segments: skip remaining work
                self.error.emit(CANCELLED_MESSAGE)
                return
            for chunk in self.engine.infer_stream(
                segment, voice=request.voice, temperature=request.temperature
            ):
                if self._aborted():
                    self.error.emit(CANCELLED_MESSAGE)
                    return
                chunks.append(np.asarray(chunk, dtype=np.float32))
                self.chunk_ready.emit(chunks[-1])
            self.progress.emit(TTSProgress(done=index + 1, total=total, stage="synthesizing"))
        self.done.emit(np.concatenate(chunks) if chunks else np.zeros(0, dtype=np.float32))

    def _aborted(self) -> bool:
        """True when the job should stop NOW: user cancel or worker shutdown.

        The stream loop polls this at every chunk/segment boundary so a quit
        interrupts a long multi-segment render promptly instead of letting it
        synthesize on after stop() gave up waiting (crash-on-quit fix).
        """
        return self._cancel.is_set() or self._stop.is_set()

    def _process_batch(self, request: TTSRequest) -> None:
        texts = [request.text]
        self.progress.emit(TTSProgress(done=0, total=len(texts), stage="synthesizing"))
        audios = self.engine.infer_batch(texts, voice=request.voice)
        if self._check_cancelled():
            return
        self.progress.emit(TTSProgress(done=len(texts), total=len(texts), stage="synthesizing"))
        self.done.emit(audios)
