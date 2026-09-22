"""Parent-side adapter for the isolated Qwen model host (Phase 3 Task 3.3).

The host (``workers/qwen_host.py``) is a separate interpreter with its own
dependency stack; this module is the app-side owner of its lifecycle:

* **spawn** — shell-free ``python -m`` in a sanitized, offline environment
  (inherited ``PYTHON*`` variables are dropped so nothing can redirect imports,
  and every Hugging Face entry point is forced offline);
* **handshake** — the first frame must be ``hello``; a profile is loaded once
  and its reported capabilities are cached for the UI;
* **stream** — ``infer_stream`` sends one bounded segment and yields 48 kHz
  float32 chunks as the host resamples them, with a per-frame timeout;
  ``infer_stream_many`` sends up to ``MAX_BATCH_SEGMENTS`` segments as one
  ``synthesize_batch`` job (batch-native generation) and yields
  ``(segment, chunk)`` pairs for export throughput;
* **cancel** — a request first, then terminate, then kill, escalating only when
  the host does not settle the job in time;
* **reap** — every failure path (timeout, crash, malformed output, fatal device
  error) tears the child down, and the next call lazily starts a clean one.

A reader thread owns the receive side so cancellation can be observed while the
calling thread is blocked in ``infer_stream``; job ids and frame transitions are
still validated by the shared :class:`~vienetts_app.core.qwen_protocol.SessionState`.
"""

from __future__ import annotations

import contextlib
import ctypes
import itertools
import logging
import os
import queue
import signal
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, TYPE_CHECKING, Any

import numpy as np

from vienetts_app.core.engine import EngineProviderError
from vienetts_app.core.engine_profiles import (
    QWEN_BASE,
    QWEN_CUSTOM,
    EngineProfileError,
    get_capabilities,
    host_precision,
    language_model_name,
)
from vienetts_app.core.qwen_protocol import (
    MAX_BATCH_CHARS,
    MAX_BATCH_SEGMENTS,
    MAX_TEXT_CHARS,
    EndOfStream,
    Frame,
    ProtocolError,
    SessionState,
    StaleFrameError,
    pcm_from_bytes,
    read_frame,
    write_frame,
)
from vienetts_app.core.text_segmentation import QWEN_MAX_CHARS

_LOGGER = logging.getLogger(__name__)

_GIB = 1024**3

#: Host RSS growth (bytes) over the post-load baseline at which the parent
#: recycles the host at the next job boundary instead of waiting for the
#: kernel's memory manager to do it mid-job (VieNeuTTSApp-mbzv: macOS SIGKILLed
#: the host at 22.7 GB and the running chapter was lost). The resident model's
#: absolute size is machine-dependent; *growth* over what the load itself needs
#: is the machine-independent signal of allocator bloat.
RSS_RECYCLE_GROWTH_BYTES = int(1.5 * _GIB)

#: Physical-RAM tiers (bytes) for batch bounds: at least one tier fits the
#: 16 GB machine the batch char cap was proven on; below that the batch shrinks
#: so a legal batch can never exceed what the machine survives.
_BATCH_TIER_FULL_BYTES = 16 * _GIB
_BATCH_TIER_REDUCED_BYTES = 8 * _GIB


def physical_ram_bytes() -> int | None:
    """Total physical RAM in bytes, best effort — ``None`` when undetectable."""
    try:
        if sys.platform == "darwin":
            return int(os.sysconf("HW_MEMSIZE"))
        if sys.platform.startswith("linux"):
            for line in Path("/proc/meminfo").read_text(encoding="utf-8").splitlines():
                if line.startswith("MemTotal:"):
                    return int(line.split()[1]) * 1024
            return None
        if os.name == "nt":

            class _MemoryStatusEx(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong),
                    ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong),
                    ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _MemoryStatusEx()
            status.dwLength = ctypes.sizeof(status)
            if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
                return int(status.ullTotalPhys)
            return None
    except (OSError, ValueError, AttributeError):
        return None
    return None


def host_footprint(pid: int) -> int | None:
    """Current resident bytes of ``pid``, best effort — ``None`` when unknown.

    ``ps`` covers macOS and Linux (RSS in 1 KiB pages of output); Windows asks
    for the working set through ``GetProcessMemoryInfo``. Sampled only at job
    boundaries, so the spawn cost is noise next to a seconds-long synthesis.
    """
    try:
        if os.name == "nt":

            class _ProcessMemoryCounters(ctypes.Structure):
                _fields_ = [
                    ("cb", ctypes.c_ulong),
                    ("PageFaultCount", ctypes.c_ulong),
                    ("PeakWorkingSetSize", ctypes.c_size_t),
                    ("WorkingSetSize", ctypes.c_size_t),
                    ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                    ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                    ("PagefileUsage", ctypes.c_size_t),
                    ("PeakPagefileUsage", ctypes.c_size_t),
                ]

            kernel32 = ctypes.windll.kernel32
            handle = kernel32.OpenProcess(0x0400, False, int(pid))  # PROCESS_QUERY_INFORMATION
            if not handle:
                return None
            counters = _ProcessMemoryCounters()
            counters.cb = ctypes.sizeof(counters)
            try:
                if not ctypes.windll.psapi.GetProcessMemoryInfo(
                    handle, ctypes.byref(counters), counters.cb
                ):
                    return None
                return int(counters.WorkingSetSize)
            finally:
                kernel32.CloseHandle(handle)
        output = subprocess.run(
            ("ps", "-o", "rss=", "-p", str(int(pid))),
            capture_output=True,
            text=True,
            timeout=2.0,
            check=False,
        ).stdout.strip()
        return int(output.split()[0]) * 1024 if output else None
    except (OSError, ValueError, subprocess.SubprocessError, AttributeError):
        return None


def batch_bounds_for_ram(total_bytes: int | None) -> tuple[int, int]:
    """``(max segments, max chars)`` one batch may carry on a machine this big.

    The protocol bounds (:data:`MAX_BATCH_SEGMENTS`, :data:`MAX_BATCH_CHARS`)
    are the ceiling proven on the 16 GB Mac mini that sized them; smaller
    machines shrink the batch so its worst case stays a multiple of the
    interactive single-segment worst case they can still survive. Unknown RAM
    keeps the protocol bounds — an unknown machine is not assumed small. Never
    exceeds the protocol values, which stay the wire-format authority.
    """
    segments = MAX_BATCH_SEGMENTS
    if total_bytes is not None:
        if total_bytes < _BATCH_TIER_REDUCED_BYTES:
            segments = 1  # only the interactive worst case, nothing batched
        elif total_bytes < _BATCH_TIER_FULL_BYTES:
            segments = 2
    return min(segments, MAX_BATCH_SEGMENTS), min(MAX_BATCH_CHARS, segments * QWEN_MAX_CHARS)


if TYPE_CHECKING:
    # The clone store (core.voice_profiles) imports this module for ClonePrompt,
    # so its type stays structural here and the dependency runs one way.
    from vienetts_app.core.models import VoiceOp

#: Platform seam for the host spawn: a windowed (``console=False``) app must
#: ask Windows for ``CREATE_NO_WINDOW`` or the child flashes a console window.
IS_WINDOWS = os.name == "nt"

#: SIGPIPE exists on POSIX only; the guard below is a no-op without it.
HAS_SIGPIPE = hasattr(signal, "SIGPIPE")

HOST_MODULE = "vienetts_app.workers.qwen_host"

#: The flag the packaged executable re-dispatches ITSELF with to become the
#: model host: a frozen build has no separate interpreter to hand a module
#: name to (Task 7.1).
HOST_FLAG = "--qwen-host"

#: The flag that turns the host into its import-check mode: same interpreter,
#: same environment and same import path as a load, but it imports the runtime
#: stack and reports the result instead of speaking the frame protocol. It is
#: how the app proves a promoted runtime is importable without importing torch
#: into its own process.
HOST_CHECK_FLAG = "--qwen-host-check"

#: Environment variable carrying the managed runtime's ``site-packages`` to
#: the host. ``PYTHONPATH`` suffices for a source checkout, but a frozen host
#: cannot use it (PyInstaller's importer ignores it), so the host puts this
#: directory on ``sys.path`` itself before the first heavy import.
RUNTIME_ENV = "VIENETTS_QWEN_RUNTIME"

# Protocol profile key ↔ engine profile id. Mirrors the host's ``PROFILE_ENGINES``;
# ``test_qwen_engine.py`` asserts the two stay identical.
ENGINE_PROFILE_KEYS: Mapping[str, str] = {"customvoice": QWEN_CUSTOM, "base": QWEN_BASE}

# Host stderr is diagnostic only: keep a bounded tail for error messages instead
# of buffering an unbounded log in the app process.
STDERR_TAIL_LINES = 20

# Cancel requests that arrive before their job reaches the host (the UI can
# cancel a job the worker has not submitted yet); bounded like the worker's own
# retired-id registry so a stale request cannot live forever.
CANCEL_MEMORY = 32

# Sentinel for "no pending cancel" (a pending entry stores ``None``).
_MISSING = object()

_STRIPPED_ENVIRONMENT = (
    "PYTHONPATH",
    "PYTHONHOME",
    "PYTHONSTARTUP",
    "PYTHONEXECUTABLE",
    "PYTHONWARNINGS",
    "__PYVENV_LAUNCHER__",
)

ProgressFn = Callable[[float, str], None]


class QwenEngineError(RuntimeError):
    """The Qwen host could not serve the request; the message is actionable."""


class QwenEngineCancelled(QwenEngineError):
    """The job was stopped on request (or the host was torn down for it)."""


@dataclass(frozen=True)
class QwenEngineCapabilities:
    """What the loaded checkpoint reported, in app-level ids."""

    profile: str
    speakers: tuple[str, ...]
    languages: tuple[str, ...]
    supports_clone: bool
    sample_rate: int

    @classmethod
    def from_frame(cls, profile: str, frame: Frame) -> QwenEngineCapabilities:
        return cls(
            profile=profile,
            speakers=tuple(str(item) for item in frame.get("speakers", ())),
            languages=tuple(str(item) for item in frame.get("languages", ())),
            supports_clone=bool(frame.get("supportsClone", False)),
            sample_rate=int(frame.get("sampleRate", 0)),
        )


@dataclass(frozen=True)
class _HostClosed:
    """The host's output stream ended (cleanly or not) with this reason."""

    reason: str


def exit_status_note(process: subprocess.Popen[bytes]) -> str:
    """Why the host is gone, for the cases where the death *is* the diagnosis.

    A Python failure inside the host arrives as ``error``/``terminal`` frames;
    a stdout close with no frames means the process died at the OS level. A
    real incident looked exactly like that and read as a bare "closed its
    output stream": macOS's memory manager SIGKILLs the largest process when a
    generation exhausts RAM (a 4 × 2000-character batch did on a 16 GB Mac
    mini), leaving nothing on stderr. Naming the exit status turns the generic
    EOF into the answer.
    """
    try:
        process.wait(timeout=0.5)
    except (subprocess.TimeoutExpired, OSError, ValueError):
        return ""  # stdout merely closed; the process itself is still running
    code = process.returncode
    if code is None or code == 0:
        return ""
    if code < 0:
        number = -code
        if number == getattr(signal, "SIGKILL", 9):
            return (
                " — the host process was killed by SIGKILL, almost certainly the "
                "system's memory manager reclaiming RAM: free memory or use "
                "smaller/shorter synthesis batches"
            )
        return f" — the host process died from signal {number}"
    return f" — the host process exited with status {code}"


def is_frozen() -> bool:
    """True inside a PyInstaller build (the app, or its re-dispatched host)."""
    return bool(getattr(sys, "frozen", False))


def host_command() -> list[str]:
    """The shell-free command that starts the host in the managed runtime.

    A source checkout runs ``-m vienetts_app.workers.qwen_host`` in this
    interpreter. A frozen build has no separate interpreter to hand a module
    name to, so the packaged executable re-dispatches ITSELF with
    ``HOST_FLAG``: same binary, same version, and the heavy stack still comes
    from the managed runtime (never from the bundle).
    """
    if is_frozen():
        return [sys.executable, HOST_FLAG]
    return [sys.executable, "-m", HOST_MODULE]


def host_check_command() -> list[str]:
    """The command that runs the host's import check in the managed runtime.

    The host binary with ``HOST_CHECK_FLAG`` appended: the frozen build
    re-dispatches itself (the flag rides along with ``HOST_FLAG``), a source
    checkout starts the same module the host is started with. Used by the
    runtime manager to verify a promoted install before committing it.
    """
    return [*host_command(), HOST_CHECK_FLAG]


def host_environment(
    runtime_dir: Path | None = None, base: Mapping[str, str] | None = None
) -> dict[str, str]:
    """Sanitized, offline environment for the host process.

    Inherited ``PYTHON*`` variables are removed (they can redirect imports in the
    child), the managed runtime directory and the app package root go on
    ``PYTHONPATH`` so ``-m vienetts_app.workers.qwen_host`` resolves from a source
    checkout, and offline flags are forced: the host may only read the verified
    local trees.
    """
    environment = dict(os.environ if base is None else base)
    for name in _STRIPPED_ENVIRONMENT:
        environment.pop(name, None)
    environment["HF_HUB_OFFLINE"] = "1"
    environment["TRANSFORMERS_OFFLINE"] = "1"
    environment["PYTHONUNBUFFERED"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["TOKENIZERS_PARALLELISM"] = "false"
    # An MPS op the managed runtime does not implement falls back to CPU
    # instead of raising a fatal device error (which restarts the host and
    # reloads the checkpoint). Setdefault: an explicit choice is honored.
    environment.setdefault("PYTORCH_ENABLE_MPS_FALLBACK", "1")
    entries: list[str] = []
    if runtime_dir is not None:
        entries.append(str(runtime_dir))
    entries.append(str(Path(__file__).resolve().parents[2]))  # <app root>/src
    environment["PYTHONPATH"] = os.pathsep.join(entries)
    if runtime_dir is not None:
        # Belt-and-braces for a frozen host: it cannot rely on PYTHONPATH, so
        # it reads this and puts the directory on sys.path itself.
        environment[RUNTIME_ENV] = str(runtime_dir)
    return environment


def _write_frame_safely(stream: IO[bytes], frame: Frame) -> None:
    """Write one frame so a dead host's pipe can never kill the app.

    CPython ignores ``SIGPIPE`` by default, but the app restores the default
    disposition when its own stdout is a pipe (``_restore_default_sigpipe`` in
    the package root), so a closed stdout ends it quietly. That default covers
    THIS pipe too: a host that dies mid-job (crash, OOM, external kill) turns
    the next frame write into a process-fatal signal.

    Blocking the signal on the writing thread is not enough: macOS delivers
    write-generated ``SIGPIPE`` process-directed, so the kernel hands it to
    any thread that has not blocked it (Qt's native threads, a pytest-xdist
    receiver) and SIG_DFL ends the whole process. CPython also refuses
    ``signal.signal`` off the main thread, which is where the inference
    worker writes from. So swap the C disposition to ``SIG_IGN`` around the
    write — no signal is generated at all, the failed write surfaces as the
    ``OSError`` the caller turns into an actionable :class:`QwenEngineError`,
    and the previous disposition is put back exactly.
    """
    if not HAS_SIGPIPE:
        write_frame(stream, frame)
        return
    with _SIGPIPE_SWAP_LOCK:
        previous = _libc().signal(signal.SIGPIPE, 1)  # SIG_IGN
        try:
            write_frame(stream, frame)
        finally:
            _libc().signal(signal.SIGPIPE, previous)


_LIBC: ctypes.CDLL | None = None
_SIGPIPE_SWAP_LOCK = threading.Lock()


def _libc() -> ctypes.CDLL:
    """The C library, with ``signal`` typed to preserve handler pointers."""
    global _LIBC
    if _LIBC is None:
        libc = ctypes.CDLL(None)
        libc.signal.restype = ctypes.c_void_p
        libc.signal.argtypes = [ctypes.c_int, ctypes.c_void_p]
        _LIBC = libc
    return _LIBC


class QwenEngine:
    """Owns one Qwen host subprocess for one engine profile."""

    def __init__(
        self,
        profile: str,
        *,
        model_dir: Path,
        shared_dir: Path,
        device: str = "cpu",
        dtype: str = "",
        attention: str = "",
        runtime_dir: Path | None = None,
        command: Sequence[str] | None = None,
        environment: Mapping[str, str] | None = None,
        handshake_timeout: float = 10.0,
        load_timeout: float = 300.0,
        frame_timeout: float = 60.0,
        cancel_timeout: float = 5.0,
        cancel_grace_timeout: float = 300.0,
        kill_timeout: float = 5.0,
        shutdown_timeout: float = 5.0,
        logger: logging.Logger | None = None,
        footprint: Callable[[int], int | None] | None = None,
        rss_growth_recycle_bytes: int = RSS_RECYCLE_GROWTH_BYTES,
    ) -> None:
        try:
            capabilities = get_capabilities(profile)
        except EngineProfileError as exc:
            raise QwenEngineError(str(exc)) from exc
        profile_key = next(
            (key for key, engine_id in ENGINE_PROFILE_KEYS.items() if engine_id == profile), ""
        )
        if not profile_key:
            raise QwenEngineError(
                f"engine profile {profile!r} is not served by the isolated Qwen model host — "
                f"choose one of: {', '.join(sorted(ENGINE_PROFILE_KEYS.values()))}"
            )
        self.profile = str(profile)
        self._profile_key = profile_key
        self._label = capabilities.label
        self._model_dir = Path(model_dir)
        self._shared_dir = Path(shared_dir)
        self._device = str(device)
        # Unpinned precision follows the LOCKED runtime matrix (host_precision):
        # an explicit value always wins, but a caller that just names a device
        # can never silently land on float32 CUDA again.
        try:
            locked_dtype, locked_attention = host_precision(self._device)
        except EngineProfileError as exc:
            raise QwenEngineError(str(exc)) from exc
        self._dtype = str(dtype) or locked_dtype
        self._attention = str(attention) or locked_attention
        self._runtime_dir = Path(runtime_dir) if runtime_dir is not None else None
        self._command = list(command) if command is not None else None
        self._environment = dict(environment) if environment is not None else None
        self._handshake_timeout = float(handshake_timeout)
        self._load_timeout = float(load_timeout)
        self._frame_timeout = float(frame_timeout)
        self._cancel_timeout = float(cancel_timeout)
        self._cancel_grace_timeout = float(cancel_grace_timeout)
        self._kill_timeout = float(kill_timeout)
        self._shutdown_timeout = float(shutdown_timeout)
        self._log = logger if logger is not None else _LOGGER
        self._process: subprocess.Popen[bytes] | None = None
        self._threads: list[threading.Thread] = []
        self._inbox: queue.Queue[object] = queue.Queue()
        self._stderr: deque[str] = deque(maxlen=STDERR_TAIL_LINES)
        self._session = SessionState("parent")
        self._lock = threading.RLock()
        self._settled = threading.Condition(self._lock)
        self._generation = 0
        self._ready = False
        self._capabilities: QwenEngineCapabilities | None = None
        self._last_error: tuple[str, str, bool] | None = None
        self._last_frame_ns = 0.0
        self._cancel_requests: dict[str, bool] = {}
        self._force_cancelled: set[str] = set()
        self._footprint = footprint if footprint is not None else host_footprint
        self._rss_growth_recycle_bytes = int(rss_growth_recycle_bytes)
        self._rss_baseline: int | None = None

    # -- public API --------------------------------------------------------- #

    @property
    def is_initialized(self) -> bool:
        process = self._process
        return bool(self._ready and process is not None and process.poll() is None)

    def initialize(self) -> None:
        """Spawn the host, complete the handshake, and load the profile."""
        if self.is_initialized:
            return
        if self._process is not None:
            self.close()  # a dead or half-open host from a previous attempt
        self._spawn()
        generation = self._generation
        try:
            self._await_hello(generation)
            try:
                self._send(self._load_frame())
            except ProtocolError as exc:
                raise QwenEngineError(f"could not ask the Qwen host to load: {exc}") from exc
            self._capabilities = self._await_capabilities(generation)
        except BaseException:
            self._abort_host(generation, "the Qwen model host failed to initialize")
            raise
        self._ready = True
        # Baseline for the recycle check: what the loaded model itself costs on
        # this machine, sampled before any generation has touched the caches.
        self._rss_baseline = self._sample_footprint()
        self._log.info(
            "Qwen host ready: %s (%s/%s/%s)",
            self._label,
            self._device,
            self._dtype,
            self._attention,
        )

    def capabilities(self) -> QwenEngineCapabilities:
        """Capabilities reported by the loaded checkpoint (requires ``initialize``)."""
        if self._capabilities is None or not self.is_initialized:
            raise QwenEngineError("the Qwen engine is not initialized — call initialize() first")
        return self._capabilities

    def infer_stream(
        self,
        text: str,
        *,
        language: str,
        speaker: str = "",
        voice_prompt: str = "",
        ref_text: str = "",
        job_id: str = "",
        on_progress: ProgressFn | None = None,
    ) -> Iterator[np.ndarray]:
        """Synthesize one bounded segment, yielding 48 kHz mono float32 chunks.

        The host is started lazily when needed. A ``cancel`` request that arrives
        before the first chunk is still honored. If the caller abandons the
        iterator, the host job is cancelled too, so no work is left running.
        """
        if len(text) > MAX_TEXT_CHARS:
            raise QwenEngineError(
                f"a Qwen segment is limited to {MAX_TEXT_CHARS} characters, got {len(text)} — "
                "split the text before it crosses IPC"
            )
        job = job_id or uuid.uuid4().hex
        fields: dict[str, Any] = {"text": text, "language": language}
        self._add_voice_fields(fields, speaker, voice_prompt, ref_text)
        for _segment, chunk in self._run_job(
            Frame(type="synthesize", job=job, fields=fields), job, on_progress
        ):
            yield chunk

    def infer_stream_many(
        self,
        texts: Sequence[str],
        *,
        language: str,
        speaker: str = "",
        voice_prompt: str = "",
        ref_text: str = "",
        job_id: str = "",
        on_progress: ProgressFn | None = None,
    ) -> Iterator[tuple[int, np.ndarray]]:
        """Synthesize up to :data:`MAX_BATCH_SEGMENTS` segments in ONE host job.

        Yields ``(segment index, chunk)`` pairs in segment order. The host feeds
        the whole batch to one ``generate_*`` call (batch-native codec
        generation), so the segments share prefill instead of running an
        autoregressive pass each. The batch is also bounded by
        :data:`MAX_BATCH_CHARS` in total — batch generation holds every
        segment's memory at once, and an oversized batch exhausted a 16 GB Mac
        mini — so use :meth:`QwenEngineProvider.infer_stream_segments`, which
        splits accordingly. Lifecycle (lazy start, pre-start cancel,
        abandonment) is exactly :meth:`infer_stream`'s — one protocol job.
        """
        segments = [str(text) for text in texts]
        if not segments:
            return
        if len(segments) > MAX_BATCH_SEGMENTS:
            raise QwenEngineError(
                f"a Qwen batch is limited to {MAX_BATCH_SEGMENTS} segments, got {len(segments)}"
            )
        for text in segments:
            if len(text) > MAX_TEXT_CHARS:
                raise QwenEngineError(
                    f"a Qwen segment is limited to {MAX_TEXT_CHARS} characters, "
                    f"got {len(text)} — split the text before it crosses IPC"
                )
        total = sum(len(text) for text in segments)
        if total > MAX_BATCH_CHARS:
            raise QwenEngineError(
                f"a Qwen batch is limited to {MAX_BATCH_CHARS} characters in total, "
                f"got {total} — split it across jobs"
            )
        job = job_id or uuid.uuid4().hex
        fields: dict[str, Any] = {"texts": segments, "language": language}
        self._add_voice_fields(fields, speaker, voice_prompt, ref_text)
        yield from self._run_job(
            Frame(type="synthesize_batch", job=job, fields=fields), job, on_progress
        )

    @staticmethod
    def _add_voice_fields(
        fields: dict[str, Any], speaker: str, voice_prompt: str, ref_text: str
    ) -> None:
        if speaker:
            fields["speaker"] = speaker
        if voice_prompt:
            fields["voicePrompt"] = voice_prompt
        if ref_text:
            fields["refText"] = ref_text

    def _run_job(
        self,
        frame: Frame,
        job: str,
        on_progress: ProgressFn | None,
    ) -> Iterator[tuple[int, np.ndarray]]:
        """Send one synthesize frame and stream its job; shared lifecycle."""
        self._ensure_ready()
        self._recycle_if_bloated()
        generation = self._generation
        try:
            self._send(frame)
        except ProtocolError as exc:
            raise QwenEngineError(f"could not start Qwen synthesis: {exc}") from exc
        try:
            if self._take_cancel_request(job):
                self._log.debug("job %s was cancelled before it started", job)
                with contextlib.suppress(QwenEngineError, ProtocolError, OSError):
                    self._send(Frame(type="cancel", job=job))
            yield from self._stream_job(job, on_progress, generation)
        finally:
            with self._settled:
                self._cancel_requests.pop(job, None)
                abandoned = job in self._session.active
                self._force_cancelled.discard(job)
            if abandoned:
                # The caller stopped iterating: stop the host's work as well.
                with contextlib.suppress(QwenEngineError, ProtocolError, OSError):
                    self._send(Frame(type="cancel", job=job))

    def cancel(self, job_id: str) -> bool:
        """Stop a job: request first, then terminate, then kill.

        Returns True when a running job was asked to stop. A request for a job
        that has not reached the host yet returns False but is remembered and
        honored by the stream that starts it. The escalation to terminate is
        SILENCE-based (see :meth:`_wait_cancelled`): a host that keeps proving
        liveness is left alone to settle the cancel when its uninterruptible
        generate returns.
        """
        job = str(job_id)
        with self._settled:
            self._remember_cancel(job)
            active = job in self._session.active
        if not active:
            return False
        self._log.debug("requesting a Qwen host cancel for job %s", job)
        with contextlib.suppress(QwenEngineError, ProtocolError, OSError):
            self._send(Frame(type="cancel", job=job))
        if self._wait_cancelled(job):
            return True
        if not self.is_initialized:
            return True  # the host died on its own; the job is over either way
        self._log.warning(
            "the Qwen host did not stop job %s within %.1fs; terminating it",
            job,
            self._cancel_timeout,
        )
        with self._settled:
            self._force_cancelled.add(job)
        self._abort_host(self._generation, f"job {job} would not stop")
        return True

    def close(self) -> None:
        """Shut the host down and reap it; never leaves a child behind."""
        process = self._process
        if process is None:
            self._ready = False
            return
        with self._settled:
            busy = bool(self._session.active)
            for job in self._session.active:
                self._force_cancelled.add(job)  # a shutdown is a cancel for that job
            self._ready = False
        if not busy and process.poll() is None:
            with contextlib.suppress(QwenEngineError, ProtocolError, OSError, ValueError):
                self._send(Frame(type="shutdown"))
            self._wait_exit(self._shutdown_timeout)
        if process.poll() is None:
            self._terminate_process()
        self._join_threads()
        self._close_pipes()
        self._process = None
        self._capabilities = None
        self._rss_baseline = None

    def stderr_tail(self) -> str:
        """The last few host log lines - diagnostics for a failure message."""
        with self._settled:
            return "\n".join(self._stderr)

    def last_error_code(self) -> str:
        """The host's last error code (``""`` when the host has not failed).

        Read after a failed job to tell a runtime problem from a model or device
        problem: an incomplete runtime (:data:`RUNTIME_INCOMPLETE_CODE`) is
        fixed from Settings, while the others are fixed by another profile,
        model or device.
        """
        with self._settled:
            return self._last_error[0] if self._last_error else ""

    def last_error_message(self) -> str:
        """The host's last error message (``""`` when the host has not failed)."""
        with self._settled:
            return self._last_error[1] if self._last_error else ""

    def _record_error(self, frame: Frame, fallback: str) -> tuple[str, bool]:
        """Remember the host's last error; returns the message to raise + fatality."""
        message = str(frame.get("message") or "") or fallback
        fatal = bool(frame.get("fatal", False))
        self._last_error = (
            str(frame.get("code", "")),
            message,
            fatal,
        )
        return message, fatal

    # -- lifecycle internals ------------------------------------------------ #

    def _sample_footprint(self) -> int | None:
        """The host's current RSS in bytes; ``None`` when it cannot be known."""
        process = self._process
        if process is None:
            return None
        try:
            return self._footprint(process.pid)
        except Exception:  # noqa: BLE001 — a sampler failure must never fail a job
            return None

    def _recycle_if_bloated(self) -> None:
        """Retire a host whose RSS outgrew its baseline — before the kernel does.

        The host releases its accelerator caches after every job, but MPS/CUDA
        fallback copies and allocator fragmentation can still ratchet the
        resident footprint upward across a long export, and the mbzv incident
        ended with macOS SIGKILLing the largest process mid-job: a lost chapter
        and a bare closed-stream error. Sampled only at job boundaries (this is
        called from :meth:`_run_job`, before the synthesize frame is written),
        so the trade is a clean shutdown plus a lazy respawn — seconds of model
        reload — for making the mid-job kill unreachable. Growth over the
        post-load baseline is the signal, not the absolute number: the resident
        model's size is machine-dependent, bloat is not.
        """
        if (
            not self.is_initialized
            or self._rss_growth_recycle_bytes <= 0
            or self._rss_baseline is None
        ):
            return
        current = self._sample_footprint()
        if current is None:
            return
        growth = current - self._rss_baseline
        if growth < self._rss_growth_recycle_bytes:
            return
        self._log.warning(
            "Qwen host RSS grew from %.2f GiB to %.2f GiB (+%.2f GiB since the model "
            "loaded); recycling the host before the next job",
            self._rss_baseline / _GIB,
            current / _GIB,
            growth / _GIB,
        )
        self.close()
        self._ensure_ready()  # a fresh host, loaded before this job starts

    def _ensure_ready(self) -> None:
        if not self.is_initialized:
            self.initialize()

    def _spawn(self) -> None:
        self._session = SessionState("parent")
        self._stderr.clear()
        self._ready = False
        self._last_error = None
        self._last_frame_ns = time.monotonic()
        self._drain_inbox()
        self._generation += 1
        generation = self._generation
        command = list(self._command) if self._command is not None else host_command()
        environment = host_environment(self._runtime_dir, self._environment)
        creationflags = 0
        if IS_WINDOWS:
            creationflags = int(getattr(subprocess, "CREATE_NO_WINDOW", 0))
        try:
            process = subprocess.Popen(
                command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=environment,
                shell=False,
                bufsize=0,
                creationflags=creationflags,
            )
        except OSError as exc:
            raise QwenEngineError(f"could not start the Qwen model host: {exc}") from exc
        self._process = process
        drain = threading.Thread(
            target=self._drain_stderr, args=(process,), name="qwen-host-stderr", daemon=True
        )
        reader = threading.Thread(
            target=self._read_frames,
            args=(process, generation, drain),
            name="qwen-host-reader",
            daemon=True,
        )
        self._threads = [reader, drain]
        for thread in self._threads:
            thread.start()

    def _read_frames(
        self, process: subprocess.Popen[bytes], generation: int, drain: threading.Thread
    ) -> None:
        stream = process.stdout
        while stream is not None:
            try:
                frame = read_frame(stream)
            except EndOfStream:
                # Let the stderr drain catch up so a crash's last lines are in
                # the message the caller sees.
                drain.join(timeout=0.5)
                self._publish_closed(
                    generation,
                    f"the Qwen model host closed its output stream{exit_status_note(process)}",
                )
                return
            except (ProtocolError, OSError, ValueError) as exc:
                self._publish_closed(
                    generation, f"the Qwen model host sent malformed output: {exc}"
                )
                return
            with self._settled:
                if generation != self._generation:
                    return  # a previous host's reader must not touch the new session
                self._last_frame_ns = time.monotonic()  # any frame proves the host alive
                try:
                    self._session.accept(frame)
                except StaleFrameError as exc:
                    self._log.debug("dropping a stale host frame: %s", exc)
                    continue
                except ProtocolError as exc:
                    self._publish_closed(
                        generation, f"the Qwen model host violated the protocol: {exc}"
                    )
                    return
                if frame.type == "terminal":
                    self._settled.notify_all()
            self._inbox.put(frame)

    def _drain_stderr(self, process: subprocess.Popen[bytes]) -> None:
        stream = process.stderr
        if stream is None:
            return
        for raw in iter(stream.readline, b""):
            line = raw.decode("utf-8", "replace").rstrip()
            if not line:
                continue
            with self._settled:
                self._stderr.append(line)
            self._log.debug("qwen host: %s", line)

    def _publish_closed(self, generation: int, reason: str) -> None:
        with self._settled:
            if generation != self._generation:
                return
            self._ready = False
            self._inbox.put(_HostClosed(reason))
            self._settled.notify_all()

    def _drain_inbox(self) -> None:
        while True:
            try:
                self._inbox.get_nowait()
            except queue.Empty:
                return

    def _mark_dead(self, generation: int) -> None:
        with self._settled:
            if generation == self._generation:
                self._ready = False

    def _abort_host(self, generation: int, reason: str) -> None:
        with self._settled:
            if generation != self._generation:
                return
            self._ready = False
        self._log.warning("stopping the Qwen model host: %s", reason)
        self._terminate_process()

    def _terminate_process(self) -> None:
        process = self._process
        if process is None or process.poll() is not None:
            return
        with contextlib.suppress(OSError):
            process.terminate()
        if not self._wait_exit(self._kill_timeout):
            with contextlib.suppress(OSError):
                process.kill()
            self._wait_exit(self._kill_timeout)

    def _wait_exit(self, timeout: float) -> bool:
        process = self._process
        if process is None:
            return True
        try:
            process.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            return False
        except OSError:
            return True
        return True

    def _join_threads(self) -> None:
        for thread in self._threads:
            thread.join(timeout=1.0)
        self._threads = []

    def _close_pipes(self) -> None:
        process = self._process
        if process is None:
            return
        for stream in (process.stdin, process.stdout, process.stderr):
            if stream is None:
                continue
            with contextlib.suppress(OSError, ValueError):
                stream.close()

    # -- protocol internals ------------------------------------------------- #

    def _load_frame(self) -> Frame:
        return Frame(
            type="load",
            fields={
                "profile": self._profile_key,
                "modelDir": str(self._model_dir),
                "sharedDir": str(self._shared_dir),
                "device": self._device,
                "dtype": self._dtype,
                "attention": self._attention,
            },
        )

    def _send(self, frame: Frame) -> None:
        process = self._process
        if process is None or process.stdin is None:
            raise QwenEngineError("the Qwen model host is not running")
        with self._settled:
            self._session.record_sent(frame)
            try:
                _write_frame_safely(process.stdin, frame)
            except (OSError, ValueError) as exc:
                raise QwenEngineError(f"the Qwen model host is unreachable: {exc}") from exc

    def _take(self, timeout: float, generation: int, what: str, job: str = "") -> Frame:
        """Wait for the next host frame, or fail the host and raise."""
        deadline = time.monotonic() + timeout
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                self._abort_host(generation, f"the host timed out while {what}")
                raise QwenEngineError(
                    f"the Qwen model host timed out after {timeout:.1f}s while {what}"
                )
            try:
                item = self._inbox.get(timeout=remaining)
            except queue.Empty:
                continue
            if isinstance(item, _HostClosed):
                if job and job in self._force_cancelled:
                    raise QwenEngineCancelled(f"the Qwen model host was stopped while {what}")
                raise QwenEngineError(f"{item.reason} ({what}){self._tail_suffix()}")
            return item

    def _await_hello(self, generation: int) -> None:
        frame = self._take(self._handshake_timeout, generation, "waiting for the host hello")
        if frame.type != "hello":
            raise QwenEngineError(
                f"the Qwen model host said {frame.type!r} before its handshake — "
                "it is not the expected host build"
            )
        self._log.debug(
            "Qwen host handshake: host=%s platform=%s python=%s",
            frame.get("host"),
            frame.get("platform"),
            frame.get("python"),
        )

    def _await_capabilities(self, generation: int) -> QwenEngineCapabilities:
        while True:
            frame = self._take(self._load_timeout, generation, "loading the profile")
            if frame.type == "capabilities":
                return QwenEngineCapabilities.from_frame(self.profile, frame)
            if frame.type == "error":
                raise QwenEngineError(
                    self._record_error(frame, "the Qwen model host could not load the profile")[0]
                )
            raise QwenEngineError(
                f"the Qwen model host answered {frame.type!r} while loading the profile"
            )

    def _stream_job(
        self, job: str, on_progress: ProgressFn | None, generation: int
    ) -> Iterator[tuple[int, np.ndarray]]:
        while True:
            frame = self._take(self._frame_timeout, generation, "synthesizing", job=job)
            if frame.type == "pcm":
                yield (
                    int(frame.get("segment", 0)),
                    np.asarray(pcm_from_bytes(frame.payload), dtype=np.float32),
                )
            elif frame.type == "progress":
                # A fraction-less progress frame is a liveness heartbeat from
                # the host's beater during a blocking generate: real for the
                # cancel logic, invisible to UI progress (no fraction = no
                # meaningful position, and forwarding would regress progress).
                fraction = frame.fields.get("fraction")
                if on_progress is not None and fraction is not None:
                    on_progress(float(fraction), str(frame.get("stage", "")))
            elif frame.type == "error":
                _, fatal = self._record_error(frame, "")
                if fatal:
                    self._mark_dead(generation)
            elif frame.type == "terminal":
                status = str(frame.get("status", ""))
                if status == "ok":
                    return
                if status == "cancelled":
                    raise QwenEngineCancelled("the Qwen job was cancelled")
                message = str(frame.get("error", "")) or (
                    self._last_error[1] if self._last_error else ""
                )
                raise QwenEngineError(message or "the Qwen model host failed the job")
            else:
                self._log.debug("ignoring an unexpected %s frame for job %s", frame.type, job)

    def _remember_cancel(self, job: str) -> None:
        self._cancel_requests[job] = True
        while len(self._cancel_requests) > CANCEL_MEMORY:
            self._cancel_requests.pop(next(iter(self._cancel_requests)))

    def _take_cancel_request(self, job: str) -> bool:
        with self._settled:
            return self._cancel_requests.pop(job, None) is not None

    def _wait_cancelled(self, job: str) -> bool:
        """Wait for the cancelled job to settle; ``False`` when the host must die.

        ``generate_*`` is uninterruptible: a busy host settles the cancel when
        the call returns, and heartbeats keep arriving while it works — killing
        it would throw away a loaded checkpoint (seconds to minutes to reload).
        So escalation is SILENCE-based: no frame at all for ``cancel_timeout``
        means hung whatever the wall clock says, and ``cancel_grace_timeout``
        is the hard ceiling for a host that keeps beating but never settles.
        """
        deadline = time.monotonic() + self._cancel_grace_timeout
        poll = max(0.02, self._cancel_timeout / 4.0)
        with self._settled:
            while job not in self._session.settled:
                if not self._ready:
                    break  # the host is gone; the job will never settle
                now = time.monotonic()
                if now >= deadline:
                    return False  # beating but never settling: hard ceiling
                if self._last_frame_ns and now - self._last_frame_ns >= self._cancel_timeout:
                    return False  # silent: hung, not busy
                self._settled.wait(min(poll, max(0.0, deadline - now)))
            return job in self._session.settled

    def _tail_suffix(self) -> str:
        tail = self.stderr_tail()
        return f" (host stderr: {tail})" if tail else ""


# ── engine-provider seam (Task 3.4) ─────────────────────────────────────────

# One protocol job id per *segment*: the host settles an id when its terminal
# arrives, so a second synthesize with the same id would make the host's own
# frames look stale. A process-wide counter keeps ids unique and readable
# (``<worker job id>:<n>``) without a per-job map that could leak.
_SEGMENT_SEQUENCE = itertools.count(1)


def _segment_batches(
    segments: Sequence[str],
    *,
    max_segments: int = MAX_BATCH_SEGMENTS,
    max_chars: int = MAX_BATCH_CHARS,
) -> Iterator[list[str]]:
    """Group segments into the batches :meth:`infer_stream_many` accepts.

    Two bounds, both respected greedily in order: at most ``max_segments``
    segments per job (never more than the protocol frame bound) and at most
    ``max_chars`` characters of text per job (the memory bound — a batch
    generates all of its segments at once, and an oversized batch exhausted a
    16 GB Mac mini). The bounds are clamped to the protocol values so a caller
    can only ever shrink a batch, never grow one. A segment larger than the
    char cap still gets its own job: the protocol allows it, and refusing here
    would strand text the single-segment path can synthesize.
    """
    max_segments = max(1, min(int(max_segments), MAX_BATCH_SEGMENTS))
    max_chars = max(1, min(int(max_chars), MAX_BATCH_CHARS))
    batch: list[str] = []
    batch_chars = 0
    for text in segments:
        if batch and (len(batch) >= max_segments or batch_chars + len(text) > max_chars):
            yield batch
            batch = []
            batch_chars = 0
        batch.append(text)
        batch_chars += len(text)
    if batch:
        yield batch


@dataclass(frozen=True)
class ClonePrompt:
    """A resolved Qwen Base clone: reference audio plus its transcript.

    Produced by the profile-scoped clone store (Task 4.1); the host rebuilds
    the reusable prompt from these two values after every load.
    """

    reference_path: str
    transcript: str


class QwenEngineProvider:
    """:class:`QwenEngine` behind the worker's ``EngineProvider`` seam.

    Maps a job's immutable ``SynthesisContext`` onto the host protocol:

    - the app-level language code becomes the model's language name
      (``zh`` → ``Chinese``, ``auto`` → ``Auto``);
    - a preset voice becomes the CustomVoice ``speaker``;
    - an enrolled clone is resolved to its reference clip + transcript and the
      host rebuilds the prompt after load.

    Every segment of a job gets its own protocol job id, so ``cancel`` maps the
    worker's job id onto the segment that is running and remembers a request
    that arrives before the first segment starts.

    ``clone_store`` is the profile-scoped catalog (Task 4.1). When it is given,
    ``voice_op`` enrolls/removes clones through it and ``clone_id`` contexts are
    resolved with it unless ``clone_prompt_for`` overrides that. The runtime
    prompt itself is only ever built (and cached) inside the model host, so
    nothing engine-specific is persisted by the app.
    """

    def __init__(
        self,
        engine: QwenEngine,
        *,
        clone_prompt_for: Callable[[str], ClonePrompt | None] | None = None,
        clone_store: Any | None = None,
        batch_bounds: tuple[int, int] | None = None,
    ) -> None:
        self._engine = engine
        self._clone_store = clone_store
        self._clone_prompt_for = clone_prompt_for or (
            clone_store.prompt_for if clone_store is not None else None
        )
        # Batch bounds for :meth:`infer_stream_segments`: an explicit pair wins
        # (tests, callers that know better); otherwise the machine's physical
        # RAM picks a tier once and it is cached for the provider's lifetime.
        self._explicit_batch_bounds = batch_bounds
        self._ram_batch_bounds: tuple[int, int] | None = None
        self._lock = threading.Lock()
        self._active: dict[str, str] = {}
        self._pending: dict[str, None] = {}

    def _batch_bounds(self) -> tuple[int, int]:
        if self._explicit_batch_bounds is not None:
            return self._explicit_batch_bounds
        if self._ram_batch_bounds is None:
            self._ram_batch_bounds = batch_bounds_for_ram(physical_ram_bytes())
        return self._ram_batch_bounds

    @property
    def profile(self) -> str:
        return self._engine.profile

    @property
    def is_initialized(self) -> bool:
        return self._engine.is_initialized

    def initialize(self) -> None:
        """Start the host and load this profile (idempotent)."""
        self._engine.initialize()

    def close(self) -> None:
        self._engine.close()

    def infer_stream(
        self,
        text: str,
        *,
        context: Any = None,
        voice: str | None = None,
        temperature: float | None = None,
        job_id: str = "",
    ) -> Iterator[np.ndarray]:
        """Synthesize one bounded segment, yielding 48 kHz float32 chunks.

        ``temperature`` is ignored: the CustomVoice/Base 0.6B host samples with
        its own fixed settings and does not expose a temperature control (the
        profile's ``generation_controls`` do not list one).
        """
        self._require_context(context)
        capabilities = get_capabilities(self._engine.profile)
        selection = self._selection(capabilities, context, voice)

        worker_job = str(job_id) or uuid.uuid4().hex
        protocol_job = f"{worker_job}:{next(_SEGMENT_SEQUENCE)}"
        with self._lock:
            cancelled = self._pending.pop(worker_job, _MISSING) is not _MISSING
            self._active[worker_job] = protocol_job
        try:
            if cancelled:
                # A cancel that landed before this segment started is still a
                # cancel: the engine remembers it and refuses to run the job.
                self._log_cancelled(worker_job)
                self._engine.cancel(protocol_job)
            yield from self._engine.infer_stream(text, job_id=protocol_job, **selection)
        finally:
            with self._lock:
                if self._active.get(worker_job) == protocol_job:
                    self._active.pop(worker_job, None)

    def infer_stream_segments(
        self,
        texts: Sequence[str],
        *,
        context: Any = None,
        voice: str | None = None,
        temperature: float | None = None,
        job_id: str = "",
    ) -> Iterator[tuple[int, np.ndarray]]:
        """Synthesize a job's segments in bounded batches; yields ``(index, chunk)``.

        The EXPORT fast path: segments share one host job and one ``generate_*``
        call — up to :data:`~vienetts_app.core.qwen_protocol.MAX_BATCH_SEGMENTS`
        of them and at most :data:`~vienetts_app.core.qwen_protocol.
        MAX_BATCH_CHARS` of text — so a chapter's segments amortize prompt/
        prefill work instead of running an autoregressive pass each. The bounds
        also scale DOWN with the machine's physical RAM
        (:func:`batch_bounds_for_ram`): a batch holds every segment's
        activations at once, and the protocol ceiling is only proven on a 16 GB
        machine. Yields stay in segment order and carry the caller's global
        index. Interactive jobs keep :meth:`infer_stream` — first chunk latency
        beats throughput there.
        """
        self._require_context(context)
        capabilities = get_capabilities(self._engine.profile)
        selection = self._selection(capabilities, context, voice)

        worker_job = str(job_id) or uuid.uuid4().hex
        segments = [str(text) for text in texts]
        max_segments, max_chars = self._batch_bounds()
        start = 0
        for batch in _segment_batches(segments, max_segments=max_segments, max_chars=max_chars):
            protocol_job = f"{worker_job}:{next(_SEGMENT_SEQUENCE)}"
            with self._lock:
                cancelled = self._pending.pop(worker_job, _MISSING) is not _MISSING
                self._active[worker_job] = protocol_job
            try:
                if cancelled:
                    self._log_cancelled(worker_job)
                    self._engine.cancel(protocol_job)
                for offset, chunk in self._engine.infer_stream_many(
                    batch, job_id=protocol_job, **selection
                ):
                    yield start + offset, chunk
            finally:
                with self._lock:
                    if self._active.get(worker_job) == protocol_job:
                        self._active.pop(worker_job, None)
            start += len(batch)

    def _require_context(self, context: Any) -> None:
        if context is None:
            raise QwenEngineError(
                "a Qwen job must carry its engine context — the profile, language and "
                "voice/clone come from it"
            )
        profile = str(getattr(context, "profile", ""))
        if profile and profile != self.profile:
            raise QwenEngineError(
                f"this provider serves {self.profile!r}, not {profile!r} — "
                "resolve the provider from the job's context"
            )

    def _selection(self, capabilities: Any, context: Any, voice: str | None) -> dict[str, Any]:
        """The host speaking selection one job's segments all share."""
        selection: dict[str, Any] = {"language": self._language(capabilities, context)}
        if getattr(context, "clone_id", ""):
            prompt = self._resolve_clone(capabilities, str(context.clone_id))
            selection["voice_prompt"] = prompt.reference_path
            selection["ref_text"] = prompt.transcript
        else:
            speaker = self._speaker(capabilities, context, voice)
            if speaker:
                selection["speaker"] = speaker
        return selection

    def cancel(self, job_id: str) -> bool:
        """Stop the running segment of ``job_id`` (request → terminate → kill).

        Returns True when a running segment was asked to stop; a request that
        arrives before the job's first segment returns False but is remembered
        and honored by that segment.
        """
        worker_job = str(job_id)
        with self._lock:
            protocol_job = self._active.get(worker_job, "")
            if not protocol_job:
                self._remember_pending(worker_job)
                return False
        return self._engine.cancel(protocol_job)

    def voice_op(self, op: VoiceOp) -> dict[str, Any]:
        """Enroll/remove a clone in the profile-scoped store (never the host).

        The store owns the metadata and the app-owned reference copy; the model
        host builds (and caches) the runtime prompt from them at synthesis time,
        so no engine object is ever persisted. ``denoise`` has no Qwen
        implementation and is rejected with the reason instead of ignored.
        """
        capabilities = get_capabilities(self._engine.profile)
        if op.profile and op.profile != self.profile:
            raise EngineProviderError(
                f"this provider serves {self.profile!r}, not {op.profile!r} — "
                "resolve the provider from the operation's profile"
            )
        if not capabilities.supports_cloning:
            raise EngineProviderError(
                f"{capabilities.label} uses fixed speakers and cannot enroll clones — "
                "switch to an engine profile that supports cloning"
            )
        store = self._clone_store
        if store is None:
            raise EngineProviderError(
                "this worker has no clone store configured — Qwen3-TTS Base clones are "
                "enrolled in the app's profile-scoped clone store"
            )
        if op.op == "add":
            clone = store.enroll(
                name=op.name,
                profile=self.profile,
                reference_clip=op.clip_path,
                transcript=op.transcript,
                consent=op.consent,
            )
        elif op.op == "remove":
            clone = self._find_clone(store, str(op.name))
            store.remove(clone.clone_id)
        else:
            raise EngineProviderError(
                f"reference cleanup (denoise) is only available on the VieNeu-TTS "
                f"profile, not {capabilities.label}"
            )
        return {
            "op": op.op,
            "name": clone.name,
            "cloneId": clone.clone_id,
            "profile": clone.profile,
        }

    def _find_clone(self, store: Any, key: str) -> Any:
        """Resolve a remove target by clone id first, then by display name."""
        enrolled = store.list(profile=self.profile)
        wanted = key.strip()
        for clone in enrolled:
            if clone.clone_id == wanted:
                return clone
        for clone in enrolled:
            if clone.name.casefold() == wanted.casefold():
                return clone
        known = ", ".join(sorted(clone.name for clone in enrolled)) or "none"
        raise EngineProviderError(
            f"no clone named or identified by {wanted!r} is enrolled for "
            f"{self.profile} — enrolled clones: {known}"
        )

    # ── context mapping ─────────────────────────────────────────────────────

    def _language(self, capabilities: Any, context: Any) -> str:
        """The app language code, which the host resolves to a model language.

        The host owns the code → name mapping (``language_model_name``) and
        validates the code before it maps, so the provider forwards the code
        unchanged: pre-mapping it here sent ``"English"``/``"Auto"`` where the
        host reads a code, failing every Qwen job with "does not support
        language 'English'" (2026-09-21).
        """
        code = str(getattr(context, "language", "") or "")
        try:
            language_model_name(capabilities, code)  # validates the code only
        except EngineProfileError as exc:
            raise QwenEngineError(str(exc)) from exc
        return code

    def _speaker(self, capabilities: Any, context: Any, voice: str | None) -> str:
        speaker = str(getattr(context, "voice_id", "") or voice or "").strip()
        if capabilities.profile == QWEN_CUSTOM and not speaker:
            names = ", ".join(option.voice_id for option in capabilities.voices)
            raise QwenEngineError(
                f"{capabilities.label} needs a fixed speaker — choose one of: {names}"
            )
        return speaker

    def _resolve_clone(self, capabilities: Any, clone_id: str) -> ClonePrompt:
        prompt = self._clone_prompt_for(clone_id) if self._clone_prompt_for else None
        if prompt is None:
            raise QwenEngineError(
                f"clone {clone_id!r} cannot be resolved for {capabilities.label} — "
                "the profile-scoped clone store has no reference clip for it"
            )
        return prompt

    def _remember_pending(self, worker_job: str) -> None:
        """Record a pre-start cancel. Caller holds ``self._lock``."""
        self._pending[worker_job] = None
        while len(self._pending) > CANCEL_MEMORY:
            self._pending.pop(next(iter(self._pending)))

    def _log_cancelled(self, worker_job: str) -> None:
        _LOGGER.debug("Qwen job %s was cancelled before its segment started", worker_job)
