"""Dedicated QThread worker owning the TTSEngine (§5, FR-1.7, NFR-2).

Phase 2 Task 2: the queue admits immutable ``SynthesisJob`` values (plus
silent ``WarmupOp`` commands). Exactly one worker thread touches the engine.
Every admitted job emits precisely one tagged ``JobTerminal`` through the
``terminal`` signal — via the lock-protected ``_terminalize`` gate — while
``progress``/``chunk_ready`` carry the job ID so receivers can drop stale
delivery. Cancellation is targeted per job (queued jobs terminalize
immediately; the active job bails at the next safe segment/chunk boundary)
and never clears another job's cancel state.

Transitional legacy adapters (Task 3 removes the last callers): payloads
submitted as bare ``TTSRequest``/``VoiceOp`` are wrapped into jobs and ALSO
drive the old ``done``/``error``/``voice_op_done`` signals with the previous
semantics (queued drops silent, in-flight cancel on ``error``). ``cancel()``
is the deprecated global cancel; new code uses ``cancel_job``/``cancel_owner``.
``WarmupOp`` stays a silent non-job command in both paths.
"""

from __future__ import annotations

import dataclasses
import logging
import threading
import uuid
from typing import Any

import numpy as np
from PySide6.QtCore import QThread, Signal

from vienetts_app.core.engine import TTSEngine, TTSEngineError, split_text_for_streaming
from vienetts_app.core.jobs import (
    JobChunk,
    JobProgress,
    JobTerminal,
    JobTerminalState,
    SynthesisJob,
)
from vienetts_app.core.models import TTSProgress, TTSRequest, VoiceOp, WarmupOp
from vienetts_app.core.performance import PerformanceRecorder
from vienetts_app.workers.job_queue import FifoJobQueue, QueueItem

logger = logging.getLogger(__name__)

CANCELLED_MESSAGE = "Cancelled by user"

# Stream accumulation buffer: 1M float32 samples (≈22 s of 48 kHz, 4 MB) —
# most requests fit without a single growth reallocation. See
# _process_stream_job for why accumulation is one in-place buffer, not a list.
_ACCUM_INITIAL_SAMPLES = 1 << 20


class InferenceWorker(QThread):
    """Serializes tagged inference jobs onto one thread; owns the engine."""

    progress = Signal(object)  # JobProgress (TTSProgress for legacy submits)
    chunk_ready = Signal(object)  # JobChunk (raw ndarray for legacy submits)
    terminal = Signal(object)  # JobTerminal — exactly one per admitted job
    done = Signal(object)  # legacy adapter: audio for legacy-submitted requests
    error = Signal(str)  # legacy adapter: message for legacy-submitted requests
    voice_op_done = Signal(object)  # one-release adapter, see _terminalize

    _POLL_SECONDS = 0.05

    def __init__(
        self,
        engine: TTSEngine | Any,
        parent: Any | None = None,
        performance_recorder: PerformanceRecorder | None = None,
    ) -> None:
        super().__init__(parent)
        self.engine = engine
        self._performance = performance_recorder or PerformanceRecorder()
        self._jobs = FifoJobQueue()
        self._admit_lock = threading.Lock()
        self._stop = threading.Event()
        self._active_lock = threading.Lock()
        self._active_job: SynthesisJob | None = None
        self._active_cancel = threading.Event()
        self._terminal_lock = threading.Lock()
        self._terminal_ids: set[str] = set()
        self._legacy_lock = threading.Lock()
        self._legacy_ids: set[str] = set()

    # ── public API (call from any thread) ───────────────────────────────────

    def submit(self, payload: SynthesisJob | TTSRequest | VoiceOp | WarmupOp) -> bool:
        """Admit one job (or silent warmup); ``False`` once stopping.

        Returns ``True`` only for admitted work — a ``True`` job is guaranteed
        exactly one ``terminal`` event. Bare ``TTSRequest``/``VoiceOp``
        payloads are wrapped into jobs (transitional; Task 3 migrates the
        callers) and additionally drive the legacy signals.
        """
        item, legacy = self._wrap(payload)
        with self._admit_lock:
            if self._stop.is_set():
                return False
            if legacy and isinstance(item, SynthesisJob):
                with self._legacy_lock:
                    self._legacy_ids.add(item.id)
            self._jobs.put(item)
        return True

    def cancel_job(self, job_id: str) -> bool:
        """Cancel one job: queued jobs terminalize now, the active job bails
        at its next safe boundary. ``False`` for unknown/finished jobs."""
        with self._terminal_lock:
            if job_id in self._terminal_ids:
                return False
        removed = self._jobs.cancel(job_id)
        if removed is not None:
            self._terminalize(removed, "cancelled")
            return True
        with self._active_lock:
            if self._active_job is not None and self._active_job.id == job_id:
                self._active_cancel.set()
                return True
        return False

    def cancel_owner(self, owner: str) -> int:
        """Cancel every queued job of ``owner``; signal the active one if it
        matches. Returns the queued-job count (transitional exact shape)."""
        removed = self._jobs.cancel_owner(owner)  # type: ignore[arg-type]
        for job in removed:
            self._terminalize(job, "cancelled")
        with self._active_lock:
            if self._active_job is not None and self._active_job.owner == owner:
                self._active_cancel.set()
        return len(removed)

    def cancel(self) -> None:
        """Deprecated global cancel (transitional; Task 3 removes the caller).

        Drops everything still queued (cancelled terminals, legacy-silent)
        and bails the active job at its next safe boundary.
        """
        with self._active_lock:
            self._active_cancel.set()
        for job in self._jobs.cancel_all():
            self._terminalize(job, "cancelled")

    def stop(self) -> bool:
        """Stop the worker thread and release it.

        Every still-admitted pending job terminalizes ``cancelled`` exactly
        once; the active job bails at its next safe boundary (a plain
        ``infer`` call cannot be interrupted mid-call). Returns True when the
        thread finished (within the wait budget).
        """
        with self._admit_lock:
            self._stop.set()
            pending = self._jobs.cancel_all()
        for job in pending:
            self._terminalize(job, "cancelled")
        with self._active_lock:
            self._active_cancel.set()
        self._jobs.wake()
        if not self.wait(5000):
            logger.warning("inference worker did not stop in time")
            return False
        return True

    # ── worker thread body ──────────────────────────────────────────────────

    def run(self) -> None:  # noqa: D102 – QThread override
        while not self._stop.is_set():
            item = self._jobs.take(self._POLL_SECONDS)
            if item is None:
                continue
            if self._stop.is_set():
                # Admitted but never started: still owed exactly one terminal.
                if isinstance(item, SynthesisJob):
                    self._terminalize(item, "cancelled")
                break
            self._process(item)
        logger.debug("inference worker loop exited")

    def _process(self, item: QueueItem) -> None:
        if isinstance(item, WarmupOp):
            self._process_warmup()
            return
        job = item
        # Fresh per-job cancel event, installed in this thread: a cancel aimed
        # at the previous job can never leak into this one, and submit() never
        # clears anything (see the race note the old global flag carried).
        with self._active_lock:
            self._active_job = job
            self._active_cancel = threading.Event()
        self._performance.mark(job.id, "worker_dequeued")
        try:
            request = job.request
            if isinstance(request, VoiceOp):
                self._process_voice_job(job, request)
                return
            self._emit_progress(job, 0, 0, "init")
            if request.mode == "stream":
                self._process_stream_job(job, request)
            elif request.mode == "batch":
                self._process_batch_job(job, request)
            else:
                self._process_infer_job(job, request)
        except TTSEngineError as exc:
            self._terminalize(job, "failed", error=str(exc) or "Unknown engine error")
        except Exception as exc:  # noqa: BLE001 - worker must never die silently
            logger.exception("unexpected worker error")
            self._terminalize(job, "failed", error=f"Unexpected error: {exc}")
        finally:
            with self._active_lock:
                self._active_job = None

    # ── terminal gate ───────────────────────────────────────────────────────

    def _terminalize(
        self,
        job: SynthesisJob,
        state: JobTerminalState,
        *,
        value: object | None = None,
        error: str = "",
    ) -> bool:
        """Emit the job's single terminal; ``False`` if already terminalized."""
        with self._terminal_lock:
            if job.id in self._terminal_ids:
                return False
            self._terminal_ids.add(job.id)
        terminal = JobTerminal(
            job_id=job.id, owner=job.owner, state=state, value=value, error=error
        )
        self._performance.finish(job.id, "completed" if state == "completed" else state)
        self.terminal.emit(terminal)
        legacy = self._is_legacy(job.id)
        if state == "completed" and isinstance(job.request, VoiceOp):
            # One-release adapter: new voice-op consumers read the terminal
            # value; the legacy controller still needs voice_op_done.
            self.voice_op_done.emit(value)
        elif legacy and state == "completed":
            self.done.emit(value)
        elif legacy and state == "failed":
            self.error.emit(error)
        # Cancelled terminals stay silent here by design: queued drops were
        # silent before, and the in-flight boundary path below adds the
        # legacy error where one is owed.
        return True

    def _abort_job(self, job: SynthesisJob) -> None:
        """Terminalize an actively-cancelled job at a safe worker boundary."""
        self._performance.mark(job.id, "worker_cancelled")
        self._terminalize(job, "cancelled")
        if self._is_legacy(job.id):
            self.error.emit(CANCELLED_MESSAGE)

    def _is_aborted(self) -> bool:
        # _stop counts as a cancel too: shutdown must silence the request
        # that was in flight when it fired, not just user cancels.
        return self._active_cancel.is_set() or self._stop.is_set()

    def _is_legacy(self, job_id: str) -> bool:
        with self._legacy_lock:
            return job_id in self._legacy_ids

    # ── admission wrapping ──────────────────────────────────────────────────

    @staticmethod
    def _wrap(payload: SynthesisJob | TTSRequest | VoiceOp | WarmupOp) -> tuple[QueueItem, bool]:
        """Normalize a submission to a queue item; flag = legacy dual-emit."""
        if isinstance(payload, (SynthesisJob, WarmupOp)):
            return payload, False
        if isinstance(payload, TTSRequest):
            job_id = payload.job_id or uuid.uuid4().hex
            request = (
                payload if payload.job_id == job_id else dataclasses.replace(payload, job_id=job_id)
            )
            return (
                SynthesisJob(
                    id=job_id, owner="text", kind="interactive", priority=0, request=request
                ),
                True,
            )
        if isinstance(payload, VoiceOp):
            return (
                SynthesisJob(
                    id=uuid.uuid4().hex,
                    owner="cloning",
                    kind="voice_op",
                    priority=0,
                    request=payload,
                ),
                True,
            )
        raise TypeError(f"unsupported worker payload: {type(payload).__name__}")

    # ── signal helpers ──────────────────────────────────────────────────────

    def _emit_progress(self, job: SynthesisJob, done: int, total: int, stage: str) -> None:
        self.progress.emit(JobProgress(job.id, done=done, total=total, stage=stage))
        if self._is_legacy(job.id):
            self.progress.emit(TTSProgress(done=done, total=total, stage=stage))  # type: ignore[arg-type]

    def _emit_chunk(self, job: SynthesisJob, array: np.ndarray) -> None:
        # Bounded safe copy: the accumulation buffer below is reused as the
        # stream grows, so the cross-thread event must own its samples.
        self.chunk_ready.emit(JobChunk(job.id, array.copy()))
        if self._is_legacy(job.id):
            self.chunk_ready.emit(array)

    # ── engine paths ────────────────────────────────────────────────────────

    def _process_warmup(self) -> None:
        """Load the model without synthesizing (background prewarm).

        Silent on BOTH outcomes by design: a warmup that cannot load the
        engine (weights missing, offline cache) must not raise an error
        banner or touch busy state — the first real request re-hits the same
        condition and surfaces the actionable message then. Duck-typed engines
        without ``initialize`` (test fakes, third-party) are skipped.
        """
        initialize = getattr(self.engine, "initialize", None)
        if not callable(initialize):
            return
        try:
            initialize()
        except Exception:  # noqa: BLE001 - see docstring: prewarm is best-effort
            logger.info("background engine prewarm skipped (will retry on first use)")

    def _process_voice_job(self, job: SynthesisJob, op: VoiceOp) -> None:
        """Run a voice-management job on the engine thread (FR-3.4).

        add/remove persist the voice registry afterwards (redirected away from
        the SDK's site-packages default — engine.persist_voices). denoise
        returns the cleaned clip through the payload at its native 44.1 kHz.
        The terminal value carries the same dict the legacy ``voice_op_done``
        adapter emits.
        """
        if op.op == "add":
            self.engine.add_voice(op.name, op.clip_path, denoise=op.denoise, save=False)
            self.engine.persist_voices()
            self._terminalize(job, "completed", value={"op": "add", "name": op.name})
        elif op.op == "remove":
            self.engine.remove_voice(op.name, save=False)
            self.engine.persist_voices()
            self._terminalize(job, "completed", value={"op": "remove", "name": op.name})
        else:
            audio, sample_rate = self.engine.denoise(op.clip_path)
            self._terminalize(
                job,
                "completed",
                value={"op": "denoise", "audio": audio, "sample_rate": sample_rate},
            )

    def _process_infer_job(self, job: SynthesisJob, request: TTSRequest) -> None:
        """Whole-buffer synthesis through CHUNKED dispatch (bead 8jm).

        One ``engine.infer`` call over a long text makes the SDK join and
        retain the full audio inside a single call (~2.5 GB RSS plateau at
        document scale, spike §18). Dispatching ≤DEFAULT_MAX_CHARS segments —
        the mechanism that already holds the stream path at the ~1.1 GB arena
        plateau (bead u5c) — bounds each SDK workload app-side. Text below
        the cap is a single segment and keeps the exact one-call behavior;
        above it, the SDK's inter-chunk silence gaps are not inserted at
        segment boundaries (the stream path's documented trade-off). Cancel
        is cooperative BETWEEN segments; a single call remains
        uninterruptible mid-call (§11).
        """
        segments = split_text_for_streaming(request.text)
        total = len(segments) if segments else 1
        self._emit_progress(job, 0, total, "synthesizing")
        parts: list[np.ndarray] = []
        for index, segment in enumerate(segments or [request.text]):
            if self._is_aborted():  # between segments: skip remaining work
                self._abort_job(job)
                return
            self._performance.mark(job.id, "engine_call_started")
            part = self.engine.infer(
                segment,
                voice=request.voice,
                ref_audio=request.ref_audio,
                temperature=request.temperature,
            )
            parts.append(np.asarray(part, dtype=np.float32))
            self._emit_progress(job, index + 1, total, "synthesizing")
        if len(parts) == 1:
            audio = parts[0]  # single segment: the engine's own buffer, no copy
        elif parts:
            audio = np.concatenate(parts)
        else:
            audio = np.zeros(0, dtype=np.float32)
        self._performance.observe_max(job.id, "concatenated_audio_bytes", int(audio.nbytes))
        self._terminalize(job, "completed", value=audio)

    def _process_stream_job(self, job: SynthesisJob, request: TTSRequest) -> None:
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
        AND between segments. The terminal carries the concatenated audio
        (empty float32 array when nothing was produced); exceptions propagate
        to the _process catch as failed terminals.
        """
        # TTSRequest rejects blank text, so segmentation always yields ≥1
        # segment; the guard keeps the concatenation contract total anyway.
        segments = split_text_for_streaming(request.text)
        total = len(segments) if segments else 1
        self._emit_progress(job, 0, total, "synthesizing")
        # Single-buffer accumulation: the old chunk list + np.concatenate
        # held the finished audio TWICE at completion (peak 2× RSS — the
        # direct driver of the conservative 60k-char cap, bead bzm/75v). One
        # float32 buffer grows by doubling; JobChunk events carry safe copies
        # and the terminal carries the final view, so peak stays ≈1×
        # (plus one ≤50% growth transient per doubling).
        buffer = np.zeros(_ACCUM_INITIAL_SAMPLES, dtype=np.float32)
        written = 0
        first_chunk = True
        for index, segment in enumerate(segments or [request.text]):
            if self._is_aborted():  # between segments: skip remaining work
                self._abort_job(job)
                return
            self._performance.mark(job.id, "engine_call_started")
            for chunk in self.engine.infer_stream(
                segment, voice=request.voice, temperature=request.temperature
            ):
                if self._is_aborted():
                    self._abort_job(job)
                    return
                array = np.asarray(chunk, dtype=np.float32)
                end = written + int(array.size)
                if end > buffer.size:
                    grown = np.zeros(max(buffer.size * 2, end), dtype=np.float32)
                    grown[:written] = buffer[:written]
                    buffer = grown
                buffer[written:end] = array
                written = end
                self._performance.increment(job.id, "chunks_produced")
                self._performance.observe_max(job.id, "retained_chunk_bytes", int(written * 4))
                if first_chunk:
                    self._performance.mark(job.id, "worker_first_chunk")
                    first_chunk = False
                self._emit_chunk(job, array)
            self._emit_progress(job, index + 1, total, "synthesizing")
        audio = buffer[:written] if written else np.zeros(0, dtype=np.float32)
        self._performance.observe_max(job.id, "concatenated_audio_bytes", int(audio.nbytes))
        self._terminalize(job, "completed", value=audio)

    def _process_batch_job(self, job: SynthesisJob, request: TTSRequest) -> None:
        texts = [request.text]
        self._emit_progress(job, 0, len(texts), "synthesizing")
        self._performance.mark(job.id, "engine_call_started")
        audios = self.engine.infer_batch(texts, voice=request.voice)
        if self._is_aborted():
            self._abort_job(job)
            return
        self._emit_progress(job, len(texts), len(texts), "synthesizing")
        self._terminalize(job, "completed", value=audios)
