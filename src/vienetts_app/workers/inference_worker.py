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
from collections import deque
from collections.abc import Iterator
from typing import Any

import numpy as np
from PySide6.QtCore import QThread, Signal

from vienetts_app.core.artifacts import ArtifactWriteError, IncrementalArtifactWriter
from vienetts_app.core.audio import DEFAULT_SAMPLE_RATE, time_stretch_audio
from vienetts_app.core.engine import (
    DEFAULT_MAX_CHARS,
    EngineProvider,
    EngineProviderError,
    EngineProviders,
    TTSEngine,
    TTSEngineError,
    segment_limit_for,
    split_text_for_profile,
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
# Retire-registry bound (Windows crash audit 2026-09-07): terminal/cancel IDs
# are write-only bookkeeping — one entry per finished job, never read again
# after the job settles. Without a cap a long audiobook/batch session leaks
# one entry per chapter forever. 4096 settled jobs of headroom is far beyond
# any live window: only the current + queued jobs are ever looked up.
_RETIRED_ID_RETAIN = 4096


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
        engine: TTSEngine | Any | None,
        parent: Any | None = None,
        performance_recorder: PerformanceRecorder | None = None,
        providers: EngineProviders | None = None,
    ) -> None:
        super().__init__(parent)
        self.engine = engine
        # Which engine serves which profile. Without an explicit set, the single
        # in-process VieNeu engine serves everything (legacy callers, tests).
        # The set is immutable: switching profiles builds a new one, so a job
        # that is already running can never change engines mid-flight.
        self._providers = providers if providers is not None else EngineProviders.for_engine(engine)
        self._performance = performance_recorder or PerformanceRecorder()
        self._jobs = FifoJobQueue()
        self._admit_lock = threading.Lock()
        self._stop = threading.Event()
        self._active_lock = threading.Lock()
        self._active_job: SynthesisJob | None = None
        # True from the moment a job leaves the queue until it is processed:
        # the switch gate must see admitted work even in the window before
        # _process() installs _active_job.
        self._dequeued_job = False
        self._active_cancel = threading.Event()
        self._terminal_lock = threading.Lock()
        # Insertion-ordered set of settled job IDs (dict as ordered set):
        # capped at _RETIRED_ID_RETAIN, oldest evicted — see module note.
        self._terminal_ids: dict[str, None] = {}
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

    def has_pending_work(self) -> bool:
        """True while a job is running, being processed, or queued.

        The profile-switch gate asks this before tearing an engine down: a
        switch must never race work that is already admitted. A job taken off
        the queue counts even before ``_process`` marks it active (there is a
        window between ``take`` and that mark), and a warmup does not count —
        it is silent engine preparation the incoming profile redoes anyway.
        """
        with self._active_lock:
            if self._active_job is not None or self._dequeued_job:
                return True
        return bool(self._jobs.pending_jobs())

    def cancel_job(self, job_id: str) -> bool:
        """Cancel one job: queued jobs terminalize now, the active job bails
        at its next safe boundary. ``False`` for unknown/finished jobs."""
        with self._terminal_lock:
            if job_id in self._terminal_ids:
                return False
        self._remember_cancel_request(job_id)
        removed = self._jobs.cancel(job_id)
        if removed is not None:
            self._terminalize(removed, "cancelled")
            return True
        return bool(self._signal_active_cancel(job_id=job_id))

    def cancel_owner(self, owner: str) -> int:
        """Cancel every queued job of ``owner``; signal the active one if it
        matches. Returns the queued-job count (transitional exact shape)."""
        removed = self._jobs.cancel_owner(owner)  # type: ignore[arg-type]
        for job in removed:
            self._terminalize(job, "cancelled")
        with self._active_lock:
            active = self._active_job
            if active is not None and active.owner == owner:
                self._remember_cancel_request(active.id)
        self._signal_active_cancel(owner=owner)
        return len(removed)

    def _remember_cancel_request(self, job_id: str) -> None:
        """Record a cancel request, trimming stale entries past the cap.

        The marker must be installed BEFORE the queue/active check: a cancel
        landing between ``queue.take`` and the per-job event install in
        ``_process`` is only honored via this set. Unknown IDs are kept too
        (they are indistinguishable from in-flight races here) but evicted
        oldest-first past the cap so random-ID spam cannot leak memory; the
        fresh marker is always added last so trimming never drops it.
        """
        with self._cancel_lock:
            while len(self._cancel_requested_ids) >= _RETIRED_ID_RETAIN:
                self._cancel_requested_ids.pop()
            self._cancel_requested_ids.add(job_id)

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
            active = self._active_job
        if active is not None:
            self._cancel_provider_job(active)
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
            if isinstance(item, SynthesisJob):
                # Marked before _process() so the pending-work probe cannot
                # miss a job that is out of the queue but not yet active.
                with self._active_lock:
                    self._dequeued_job = True
            try:
                self._process(item)
            finally:
                if isinstance(item, SynthesisJob):
                    with self._active_lock:
                        self._dequeued_job = False
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
            self._terminal_ids[job.id] = None
            while len(self._terminal_ids) > _RETIRED_ID_RETAIN:
                self._terminal_ids.pop(next(iter(self._terminal_ids)))
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

    def _provider_for(self, job: SynthesisJob) -> EngineProvider:
        """The provider that owns ``job``: synthesis context or voice profile."""
        request = job.request
        if isinstance(request, VoiceOp):
            return self._providers.provider_for_profile(request.profile)
        return self._providers.provider_for(job.context)

    def _signal_active_cancel(self, *, job_id: str = "", owner: str = "") -> bool:
        """Set the active job's cancel event when it matches; True when it did.

        The engine provider is asked to stop *outside* the lock: an in-engine
        cancel can wait for the model host to settle the job, and the worker
        thread needs this lock to reach its next chunk boundary.
        """
        with self._active_lock:
            job = self._active_job
            if job is None or (job_id and job.id != job_id) or (owner and job.owner != owner):
                return False
            self._active_cancel.set()
        self._cancel_provider_job(job)
        return True

    def _cancel_provider_job(self, job: SynthesisJob) -> None:
        """Ask the job's own engine to stop, in addition to the worker's event.

        Qwen generations can take a while to reach the next chunk boundary, so
        the provider cancels in-engine (request → terminate → kill) and the
        worker's event covers VieNeu, which cannot be interrupted mid-call.
        Best-effort: a provider that cannot be resolved or reached must not stop
        the worker's own cancellation from succeeding.
        """
        try:
            provider = self._provider_for(job)
        except EngineProviderError as exc:
            logger.debug("no engine provider to cancel for job %s: %s", job.id, exc)
            return
        try:
            provider.cancel(job.id)
        except Exception:  # noqa: BLE001 - the worker's own cancel still applies
            logger.debug("engine provider cancel failed for job %s", job.id, exc_info=True)

    def _provider_chunks(
        self, provider: EngineProvider, job: SynthesisJob, segment: str
    ) -> Iterator[np.ndarray]:
        """One segment's chunks; a provider failure during a cancel is a cancel.

        An engine that is torn down to stop a job (the Qwen host is terminated
        when it will not stop on request) raises from ``infer_stream``; that is
        the job's cancellation, not a synthesis failure.
        """
        request = job.request
        try:
            yield from provider.infer_stream(
                segment,
                context=job.context,
                voice=getattr(request, "voice", None),
                temperature=getattr(request, "temperature", None),
                job_id=job.id,
            )
        except _JobCancelled:
            raise
        except Exception:
            if self._is_aborted():
                raise _JobCancelled from None
            raise

    def _job_chunks(
        self, provider: EngineProvider, job: SynthesisJob, texts: list[str]
    ) -> Iterator[tuple[int, np.ndarray]]:
        """``(segment index, chunk)`` for a whole artifact job, in segment order.

        Export-only multi-segment jobs batch when their provider knows how —
        Qwen generation is batch-native, so up to ``MAX_BATCH_SEGMENTS``
        segments share one host call and one autoregressive pass each instead
        of running one pass per segment. Live jobs keep the per-segment path:
        first-chunk latency beats throughput when a listener is waiting.
        """
        request = job.request
        batched = (
            job.live_transport is None
            and len(texts) > 1
            and callable(getattr(provider, "infer_stream_segments", None))
        )
        try:
            if batched:
                yield from provider.infer_stream_segments(
                    texts,
                    context=job.context,
                    voice=getattr(request, "voice", None),
                    temperature=getattr(request, "temperature", None),
                    job_id=job.id,
                )
                return
            for index, text in enumerate(texts):
                for chunk in self._provider_chunks(provider, job, text):
                    yield index, chunk
        except _JobCancelled:
            raise
        except Exception:
            if self._is_aborted():
                raise _JobCancelled from None
            raise

    @staticmethod
    def _ordered_chunks(
        stream: Iterator[tuple[int, np.ndarray]],
        lookaside: deque[tuple[int, np.ndarray]],
        index: int,
    ) -> Iterator[np.ndarray]:
        """One segment's slice of a job's ordered chunk stream.

        The stream is shared by every segment of the job and is strictly
        ordered: an item beyond ``index`` is pushed back for its own segment
        (it is the boundary, not a defect), while an item before ``index``
        means the engine broke its ordering promise.
        """
        while True:
            if not lookaside:
                item = next(stream, None)
                if item is None:
                    return
                lookaside.append(item)
            found, chunk = lookaside[0]
            if found < index:
                raise RuntimeError(
                    f"engine segments arrived out of order: got {found} "
                    f"while segment {index} streams"
                )
            if found > index:
                return
            lookaside.popleft()
            yield chunk

    @staticmethod
    def _safe_error(exc: Exception) -> str:
        """Return an actionable failure message with details."""
        if isinstance(
            exc, (ArtifactWriteError, EngineProviderError, TransportClosed, TTSEngineError)
        ):
            return str(exc) or "Synthesis failed"
        msg = str(exc).strip()
        if isinstance(exc, MemoryError) or "out of memory" in msg.lower():
            return f"Out of memory: {msg}" if msg else "Out of memory"
        if isinstance(exc, PermissionError):
            return f"File access denied: {msg}" if msg else "File access denied"
        if isinstance(exc, OSError):
            return f"Disk or file system error: {msg}" if msg else "Disk or file system error"
        if msg:
            return f"Synthesis error: {msg}"
        return f"Unexpected synthesis error ({type(exc).__name__})"

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
        condition and surfaces the actionable message then. Only the default
        provider is warmed: it is the profile the app currently has active, and
        warming every registered profile would keep two model stacks resident.
        """
        provider = self._providers.default
        if provider is None:
            logger.debug("no default engine provider to prewarm")
            return
        try:
            provider.initialize()
        except Exception:  # noqa: BLE001 - see docstring: prewarm is best-effort
            logger.info("background engine prewarm skipped (will retry on first use)")

    def _process_voice_job(self, job: SynthesisJob, op: VoiceOp) -> None:
        """Run a voice-management job on its own profile's provider (FR-3.4).

        The provider owns the operation: VieNeu keeps its SDK voice registry
        (persisted into app data), Qwen Base enrolls/removes clones in the
        profile-scoped clone store. denoise returns the cleaned clip through
        the payload at its native 44.1 kHz; the terminal value carries the
        operation result metadata.
        """
        provider = self._providers.provider_for_profile(op.profile)
        self._terminalize(job, "completed", value=provider.voice_op(op))

    def _process_artifact_stream_job(self, job: SynthesisJob, request: TTSRequest) -> None:
        assert job.artifact_path is not None
        # Resolve the engine ONCE per job from the job's immutable context, and
        # before any writer exists: a job can never switch engines mid-flight,
        # and a provider failure leaves no partial artifact behind.
        provider = self._provider_for(job)
        context = job.context
        segment_limit = segment_limit_for(context.profile) if context else DEFAULT_MAX_CHARS
        language = context.language if context else ""
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
            segments = split_text_for_profile(
                request.text, language=language, max_chars=segment_limit
            )
            texts = list(segments or [request.text])
            total = len(texts)
            silence_p = request.silence_p if request.silence_p is not None else 0.0
            silence_samples = int(DEFAULT_SAMPLE_RATE * silence_p)
            apply_stretch = request.speed is not None and abs(request.speed - 1.0) >= 1e-3

            # One ordered (segment index, chunk) stream per job; batches on
            # providers that know how, one segment at a time otherwise.
            chunk_stream = iter(self._job_chunks(provider, job, texts))
            boundary: deque[tuple[int, np.ndarray]] = deque()
            for index, _segment in enumerate(texts):
                if self._is_aborted():
                    raise _JobCancelled

                if index > 0 and silence_samples > 0:
                    silence_chunk = np.zeros(silence_samples, dtype=np.float32)
                    _emit_audio_chunk(silence_chunk, is_silence=True)

                if apply_stretch:
                    # Per-chunk WSOLA: stretching each SDK chunk as it arrives
                    # keeps RAM bounded by one chunk plus the WSOLA frame
                    # buffers. The old whole-segment path accumulated every
                    # chunk, concatenated, then stretched — a long segment
                    # held 2× its audio plus WSOLA's output/norm buffers and
                    # spiked Windows RSS (crash audit 2026-09-07). Chunk joins
                    # stay click-free via time_stretch_audio's edge micro-fades.
                    for raw_chunk in self._ordered_chunks(chunk_stream, boundary, index):
                        if self._is_aborted():
                            raise _JobCancelled
                        chunk = np.ascontiguousarray(raw_chunk, dtype=np.float32)
                        if chunk.size == 0:
                            continue
                        stretched = time_stretch_audio(chunk, rate=float(request.speed))  # type: ignore[arg-type]
                        _emit_audio_chunk(stretched)
                else:
                    for raw_chunk in self._ordered_chunks(chunk_stream, boundary, index):
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
