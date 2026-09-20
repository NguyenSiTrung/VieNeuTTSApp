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
import logging
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from vienetts_app.core.engine_profiles import (
    QWEN_BASE,
    QWEN_CUSTOM,
    EngineProfileError,
    get_capabilities,
)
from vienetts_app.core.qwen_protocol import (
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

_LOGGER = logging.getLogger(__name__)

HOST_MODULE = "vienetts_app.workers.qwen_host"

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


def host_command() -> list[str]:
    """The shell-free command that starts the host in the managed runtime.

    Frozen builds re-dispatch the packaged executable instead of ``sys.executable``;
    that is the packaging task's contract (Task 7.1), which passes an explicit
    ``command`` here.
    """
    return [sys.executable, "-m", HOST_MODULE]


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
    entries: list[str] = []
    if runtime_dir is not None:
        entries.append(str(runtime_dir))
    entries.append(str(Path(__file__).resolve().parents[2]))  # <app root>/src
    environment["PYTHONPATH"] = os.pathsep.join(entries)
    return environment


class QwenEngine:
    """Owns one Qwen host subprocess for one engine profile."""

    def __init__(
        self,
        profile: str,
        *,
        model_dir: Path,
        shared_dir: Path,
        device: str = "cpu",
        dtype: str = "float32",
        attention: str = "sdpa",
        runtime_dir: Path | None = None,
        command: Sequence[str] | None = None,
        environment: Mapping[str, str] | None = None,
        handshake_timeout: float = 10.0,
        load_timeout: float = 300.0,
        frame_timeout: float = 60.0,
        cancel_timeout: float = 5.0,
        kill_timeout: float = 5.0,
        shutdown_timeout: float = 5.0,
        logger: logging.Logger | None = None,
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
        self._dtype = str(dtype)
        self._attention = str(attention)
        self._runtime_dir = Path(runtime_dir) if runtime_dir is not None else None
        self._command = list(command) if command is not None else None
        self._environment = dict(environment) if environment is not None else None
        self._handshake_timeout = float(handshake_timeout)
        self._load_timeout = float(load_timeout)
        self._frame_timeout = float(frame_timeout)
        self._cancel_timeout = float(cancel_timeout)
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
        self._cancel_requests: dict[str, bool] = {}
        self._force_cancelled: set[str] = set()

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
        self._ensure_ready()
        generation = self._generation
        fields: dict[str, Any] = {"text": text, "language": language}
        if speaker:
            fields["speaker"] = speaker
        if voice_prompt:
            fields["voicePrompt"] = voice_prompt
        if ref_text:
            fields["refText"] = ref_text
        try:
            self._send(Frame(type="synthesize", job=job, fields=fields))
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
        honored by the stream that starts it.
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
        if self._wait_settled(job, self._cancel_timeout):
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

    def stderr_tail(self) -> str:
        """The last few host log lines — diagnostics for a failure message."""
        with self._settled:
            return "\n".join(self._stderr)

    # -- lifecycle internals ------------------------------------------------ #

    def _ensure_ready(self) -> None:
        if not self.is_initialized:
            self.initialize()

    def _spawn(self) -> None:
        self._session = SessionState("parent")
        self._stderr.clear()
        self._ready = False
        self._last_error = None
        self._drain_inbox()
        self._generation += 1
        generation = self._generation
        command = list(self._command) if self._command is not None else host_command()
        environment = host_environment(self._runtime_dir, self._environment)
        creationflags = 0
        if os.name == "nt":
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
                self._publish_closed(generation, "the Qwen model host closed its output stream")
                return
            except (ProtocolError, OSError, ValueError) as exc:
                self._publish_closed(
                    generation, f"the Qwen model host sent malformed output: {exc}"
                )
                return
            with self._settled:
                if generation != self._generation:
                    return  # a previous host's reader must not touch the new session
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
                write_frame(process.stdin, frame)
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
                    str(frame.get("message") or "the Qwen model host could not load the profile")
                )
            raise QwenEngineError(
                f"the Qwen model host answered {frame.type!r} while loading the profile"
            )

    def _stream_job(
        self, job: str, on_progress: ProgressFn | None, generation: int
    ) -> Iterator[np.ndarray]:
        while True:
            frame = self._take(self._frame_timeout, generation, "synthesizing", job=job)
            if frame.type == "pcm":
                yield np.asarray(pcm_from_bytes(frame.payload), dtype=np.float32)
            elif frame.type == "progress":
                if on_progress is not None:
                    on_progress(float(frame.get("fraction", 0.0)), str(frame.get("stage", "")))
            elif frame.type == "error":
                self._last_error = (
                    str(frame.get("code", "")),
                    str(frame.get("message", "")),
                    bool(frame.get("fatal", False)),
                )
                if self._last_error[2]:
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

    def _wait_settled(self, job: str, timeout: float) -> bool:
        deadline = time.monotonic() + timeout
        with self._settled:
            while job not in self._session.settled and time.monotonic() < deadline:
                if not self._ready:
                    break  # the host is gone; the job will never settle
                self._settled.wait(max(0.0, deadline - time.monotonic()))
            return job in self._session.settled

    def _tail_suffix(self) -> str:
        tail = self.stderr_tail()
        return f" (host stderr: {tail})" if tail else ""
