"""Dedicated QThread worker owning the TTSEngine (§5, FR-1.7, NFR-2).

Phase 2 Task 2: the queue admits immutable ``SynthesisJob`` values (plus
silent ``WarmupOp`` commands). Exactly one worker thread touches the engine.
Every admitted job emits precisely one tagged ``JobTerminal`` through the
``terminal`` signal — via the lock-protected ``_terminalize`` gate — while
``progress``/``chunk_ready`` carry small job metadata so receivers can drop
stale delivery. Cancellation is targeted per job (queued jobs terminalize
immediately; the active job bails at the next safe segment/chunk boundary)
and never clears another job's cancel state. TTS jobs require an artifact
destination and use one incremental streaming path; raw PCM never crosses a
queued Qt signal.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

import numpy as np
from PySide6.QtCore import QThread, Signal

from vienetts_app.core.artifacts import ArtifactWriteError, IncrementalArtifactWriter
from vienetts_app.core.audio import DEFAULT_SAMPLE_RATE, time_stretch_audio
from vienetts_app.core.engine import (
    TTSEngine,
    TTSEngineError,
    split_text_for_streaming,
)
from vienetts_app.core.jobs import (
    JobChunk,
    JobProgress,
    JobTerminal,
    JobTerminalState,
    SynthesisJob,
)
from vienetts_app.core.models import TTSRequest, VoiceOp, WarmupOp
from vienetts_app.core.pcm_transport import TransportClosed
from vienetts_app.core.performance import PerformanceRecorder
from vienetts_app.workers.job_queue import FifoJobQueue, QueueItem

logger = logging.getLogger(__name__)

CANCELLED_MESSAGE = "Cancelled by user"
_CHUNK_METADATA_INTERVAL_NS = 50_000_000


class _JobCancelled(Exception):
    pass


class InferenceWorker(QThread):
    """Serializes tagged inference jobs onto one thread; owns the engine."""

    progress = Signal(object)  # JobProgress
    chunk_ready = Signal(object)  # JobChunk metadata
    terminal = Signal(object)  # JobTerminal — exactly one per admitted job

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
        self._cancel_lock = threading.Lock()
        self._cancel_requested_ids: set[str] = set()
        self._chunk_metadata_lock = threading.Lock()
        self._last_chunk_emit_ns: dict[str, int] = {}
        self._pending_chunk_metadata: dict[str, tuple[int, float]] = {}
        self._monotonic_ns = time.monotonic_ns

    # ── public API (call from any thread) ───────────────────────────────────

    def submit(self, payload: SynthesisJob | WarmupOp) -> bool:
        """Admit one job (or silent warmup); ``False`` once stopping.

        Returns ``True`` only for admitted work — a ``True`` job is guaranteed
        exactly one ``terminal`` event. TTS jobs without an artifact
        destination are rejected before they can invoke the engine.
        """
        if not isinstance(payload, (SynthesisJob, WarmupOp)) or (
            isinstance(payload, SynthesisJob)
            and isinstance(payload.request, TTSRequest)
            and payload.artifact_path is None
        ):
            return False
        with self._admit_lock:
            if self._stop.is_set():
                return False
            self._jobs.put(payload)
        return True

    def cancel_job(self, job_id: str) -> bool:
        """Cancel one job: queued jobs terminalize now, the active job bails
        at its next safe boundary. ``False`` for unknown/finished jobs."""
        with self._terminal_lock:
            if job_id in self._terminal_ids:
                return False
        with self._cancel_lock:
            self._cancel_requested_ids.add(job_id)
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
                with self._cancel_lock:
                    self._cancel_requested_ids.add(self._active_job.id)
                self._active_cancel.set()
        return len(removed)

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
            with self._cancel_lock:
                if job.id in self._cancel_requested_ids:
                    self._active_cancel.set()
        self._performance.mark(job.id, "worker_dequeued")
        try:
            request = job.request
            if isinstance(request, VoiceOp):
                self._process_voice_job(job, request)
                return
            self._emit_progress(job, 0, 0, "init")
            self._process_artifact_stream_job(job, request)
        except (ArtifactWriteError, TransportClosed, TTSEngineError) as exc:
            self._terminalize(job, "failed", error=self._safe_error(exc))
        except Exception as exc:  # noqa: BLE001 - worker must never die silently
            logger.exception("unexpected worker error")
            self._terminalize(job, "failed", error=self._safe_error(exc))
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
        with self._cancel_lock:
            self._cancel_requested_ids.discard(job.id)
        self._clear_chunk_metadata(job.id)
        terminal = JobTerminal(
            job_id=job.id, owner=job.owner, state=state, value=value, error=error
        )
        if state == "completed":
            self._performance.mark(job.id, "worker_completed")
        elif state == "cancelled":
            self._performance.mark(job.id, "worker_cancelled")
        self._performance.finish(job.id, "completed" if state == "completed" else state)
        self.terminal.emit(terminal)
        return True

    def _is_aborted(self) -> bool:
        # _stop counts as a cancel too: shutdown must silence the request
        # that was in flight when it fired, not just user cancels.
        return self._active_cancel.is_set() or self._stop.is_set()

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        """Return a concise failure message without leaking arbitrary errors."""
        if isinstance(exc, (ArtifactWriteError, TransportClosed, TTSEngineError)):
            return str(exc) or "Synthesis failed"
        return "Unexpected synthesis error"

    # ── signal helpers ──────────────────────────────────────────────────────

    def _emit_progress(self, job: SynthesisJob, done: int, total: int, stage: str) -> None:
        self.progress.emit(JobProgress(job.id, done=done, total=total, stage=stage))

    def _emit_chunk_metadata(self, job: SynthesisJob, array: np.ndarray) -> None:
        peak = float(np.max(np.abs(array))) if array.size else 0.0
        with self._chunk_metadata_lock:
            pending_samples, pending_peak = self._pending_chunk_metadata.get(job.id, (0, 0.0))
            samples = pending_samples + int(array.size)
            peak = max(pending_peak, peak)
            now = self._monotonic_ns()
            last = self._last_chunk_emit_ns.get(job.id)
            if last is not None and now - last < _CHUNK_METADATA_INTERVAL_NS:
                self._pending_chunk_metadata[job.id] = (samples, peak)
                return
            self._last_chunk_emit_ns[job.id] = now
            self._pending_chunk_metadata.pop(job.id, None)
        self.chunk_ready.emit(JobChunk(job.id, samples, peak))

    def _flush_chunk_metadata(self, job: SynthesisJob) -> None:
        with self._chunk_metadata_lock:
            pending = self._pending_chunk_metadata.pop(job.id, None)
            if pending is None:
                return
            self._last_chunk_emit_ns[job.id] = self._monotonic_ns()
        self.chunk_ready.emit(JobChunk(job.id, *pending))

    def _clear_chunk_metadata(self, job_id: str) -> None:
        with self._chunk_metadata_lock:
            self._last_chunk_emit_ns.pop(job_id, None)
            self._pending_chunk_metadata.pop(job_id, None)

    def _close_transport(self, job: SynthesisJob, *, discard: bool) -> None:
        transport = job.live_transport
        if transport is None:
            return
        self._performance.observe_max(job.id, "transport_max_bytes", transport.max_available_bytes)
        transport.close(discard=discard)

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
        The terminal value carries the operation result metadata.
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

    def _process_artifact_stream_job(self, job: SynthesisJob, request: TTSRequest) -> None:
        assert job.artifact_path is not None
        writer: IncrementalArtifactWriter | None = None
        saw_first_chunk = False
        saw_first_transport_append = False

        def _emit_audio_chunk(audio_chunk: np.ndarray, *, is_silence: bool = False) -> None:
            nonlocal saw_first_chunk, saw_first_transport_append
            if audio_chunk.size == 0:
                return
            if self._is_aborted():
                raise _JobCancelled
            if not saw_first_chunk and not is_silence:
                saw_first_chunk = True
                self._performance.mark(job.id, "worker_first_chunk")
            assert writer is not None
            writer.append(audio_chunk)
            if job.live_transport is not None:
                try:
                    job.live_transport.put(
                        memoryview(np.ascontiguousarray(audio_chunk, dtype="<f4")).cast("B"),
                        cancelled=lambda: self._is_aborted(),
                    )
                    if audio_chunk.size and not saw_first_transport_append and not is_silence:
                        saw_first_transport_append = True
                        self._performance.mark(job.id, "audio_first_buffer_append")
                except TransportClosed:
                    if self._is_aborted():
                        raise _JobCancelled from None
                    raise
            self._emit_chunk_metadata(job, audio_chunk)

        try:
            writer = IncrementalArtifactWriter(job.id, job.artifact_path)
            segments = split_text_for_streaming(request.text)
            total = len(segments) or 1
            silence_p = request.silence_p if request.silence_p is not None else 0.0
            silence_samples = int(DEFAULT_SAMPLE_RATE * silence_p)
            apply_stretch = request.speed is not None and abs(request.speed - 1.0) >= 1e-3

            for index, segment in enumerate(segments or [request.text]):
                if self._is_aborted():
                    raise _JobCancelled

                if index > 0 and silence_samples > 0:
                    silence_chunk = np.zeros(silence_samples, dtype=np.float32)
                    _emit_audio_chunk(silence_chunk, is_silence=True)

                if apply_stretch:
                    segment_chunks: list[np.ndarray] = []
                    for raw_chunk in self.engine.infer_stream(
                        segment, voice=request.voice, temperature=request.temperature
                    ):
                        if self._is_aborted():
                            raise _JobCancelled
                        segment_chunks.append(np.ascontiguousarray(raw_chunk, dtype=np.float32))
                    if segment_chunks:
                        combined = np.concatenate(segment_chunks)
                        stretched = time_stretch_audio(combined, rate=float(request.speed))  # type: ignore[arg-type]
                        _emit_audio_chunk(stretched)
                else:
                    for raw_chunk in self.engine.infer_stream(
                        segment, voice=request.voice, temperature=request.temperature
                    ):
                        if self._is_aborted():
                            raise _JobCancelled
                        chunk = np.ascontiguousarray(raw_chunk, dtype=np.float32)
                        _emit_audio_chunk(chunk)

                self._flush_chunk_metadata(job)
                self._emit_progress(job, index + 1, total, "synthesizing")
            artifact = writer.finalize()
        except _JobCancelled:
            if writer is not None:
                writer.abort()
            self._close_transport(job, discard=True)
            self._terminalize(job, "cancelled")
            return
        except Exception:
            if writer is not None:
                writer.abort()
            self._close_transport(job, discard=True)
            raise
        self._close_transport(job, discard=False)
        self._performance.observe_max(job.id, "artifact_samples", artifact.samples)
        try:
            self._performance.observe_max(
                job.id, "artifact_bytes_on_disk", artifact.path.stat().st_size
            )
        except OSError:
            logger.debug("could not stat completed synthesis artifact")
        self._terminalize(job, "completed", value=artifact)
