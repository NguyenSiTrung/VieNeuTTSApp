"""The isolated Qwen model-host executable (Phase 3 Task 3.2).

This module is the *child* half of the framed protocol in
:mod:`vienetts_app.core.qwen_protocol`: it runs inside the checksum-pinned
managed runtime, owns the only resident copy of a Qwen checkpoint, and talks to
the app over stdin/stdout. Nothing here imports Qt, the app's audio stack, or
``qwen_tts``/``torch`` at module load — the heavy imports happen when a ``load``
frame actually arrives, so the app and the test suite stay torch-free.

Responsibilities:

* load exactly one profile (``customvoice`` or ``base``) from **local verified
  paths only**: the managed per-profile tree and the shared tokenizer tree are
  merged into one read-only link view, then opened with
  ``local_files_only=True`` and ``trust_remote_code=False``;
* report the capabilities the loaded checkpoint actually exposes;
* run an **import check** on request (``HOST_CHECK_FLAG``): import the stack a
  load needs and print the verdict as one JSON line, so the app can reject a
  promoted runtime it could never use instead of failing mid-synthesis;
* synthesize one bounded segment per ``synthesize`` frame — or several per
  ``synthesize_batch`` (one batch-native ``generate_*`` call in the SDK's
  streaming text mode, streamed as ``segment``-tagged frames in order; the
  SDK's default non-streaming mode NaNs padded multi-item batches) — resampling
  24 kHz model output to the 48 kHz app contract through **one stateful
  resampler per segment** and emitting bounded float32 ``pcm`` frames
  (``seq``/``final``);
* keep stdout strictly for frames — every log line is one JSON object on
  stderr — and never accumulate a whole utterance: the parent bounds the text
  before it crosses IPC and each segment is streamed out in bounded frames;
* stop promptly on ``cancel``, settle every job exactly once, and exit cleanly
  on ``shutdown`` or peer close.

A device/OOM failure is *fatal by design*: the job is settled ``failed`` with a
``fatal`` error frame and the host exits, so the parent lazily restarts a clean
interpreter instead of retrying on a poisoned accelerator context.
"""

from __future__ import annotations

import contextlib
import gc
import json
import os
import platform
import queue
import shutil
import sys
import threading
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any

import numpy as np

from vienetts_app.core.engine_profiles import (
    APP_SAMPLE_RATE,
    QWEN_BASE,
    QWEN_CUSTOM,
    QWEN_SOURCE_RATE,
    EngineProfileError,
    get_capabilities,
    language_model_name,
    validate_selection,
)
from vienetts_app.core.qwen_engine import HOST_CHECK_FLAG, RUNTIME_ENV
from vienetts_app.core.qwen_protocol import (
    MAX_MESSAGE_CHARS,
    RUNTIME_INCOMPLETE_CODE,
    EndOfStream,
    Frame,
    ProtocolError,
    SessionState,
    StaleFrameError,
    read_frame,
    write_frame,
)

HOST_NAME = "vienetts-qwen-host"

#: Intra-op thread override for the host process (F4): unset = torch's own
#: physical-core default. See :func:`configure_torch_threads`.
THREADS_ENV = "QWEN_NUM_THREADS"

# Bounded emission: 0.5 s of 48 kHz mono float32 per frame (96 KB payload,
# comfortably below the protocol bound) and 1 s of native audio per resampler
# push, which is the cancellation granularity inside one segment.
PCM_FRAME_SAMPLES = 24_000
RESAMPLE_CHUNK_SAMPLES = 24_000

#: Liveness heartbeat interval while a blocking ``generate_*`` call owns the
#: host thread: a fraction-less ``progress`` frame, far below the parent's
#: frame timeout, proving "busy" instead of "hung".
HEARTBEAT_SECONDS = 5.0

SUPPORTED_DTYPES = ("float32", "float16", "bfloat16")
SUPPORTED_ATTENTION = ("eager", "sdpa", "flash_attention_2")

# Protocol profile keys → engine profile ids.
PROFILE_ENGINES: Mapping[str, str] = {"customvoice": QWEN_CUSTOM, "base": QWEN_BASE}

# The merged load view lives next to the managed trees, so hard links always
# stay on one filesystem and the view is trivially removable.
LOAD_VIEW_DIR = ".load"
INSTALL_METADATA_NAME = "install.json"

_DEVICE_ERROR_MARKERS = (
    "out of memory",
    "cuda",
    "cudnn",
    "mps",
    "accelerator",
    "device-side",
    "device assert",
)

#: Appended to a load failure caused by an incomplete runtime. The user cannot
#: fix a missing module by switching profile, model or device — only the
#: runtime card in Settings can, so the message names that one action.
RUNTIME_REPAIR_HINT = (
    "Repair the managed Qwen runtime in Settings and try again; if the load keeps "
    "failing, this build's pinned runtime is incomplete."
)

LogFn = Callable[..., None]
ModelLoader = Callable[..., object]


class QwenHostError(RuntimeError):
    """A host-side failure with a frame error code and a fatal/retryable verdict."""

    def __init__(self, message: str, *, code: str = "host_error", fatal: bool = False) -> None:
        super().__init__(message)
        self.code = code
        self.fatal = fatal


class QwenLoadError(QwenHostError):
    """The requested profile cannot be loaded from the verified local trees."""

    def __init__(self, message: str, *, code: str = "load_failed") -> None:
        super().__init__(message, code=code)


class QwenRuntimeIncompleteError(QwenLoadError):
    """The promoted runtime cannot import the stack a load needs.

    Distinct from a bad model tree: no profile, model or device choice can fix
    it — the managed ``site-packages`` is missing a module (or a native library
    fails to load), so the parent reports a runtime problem the user repairs
    from Settings instead of a model problem.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message, code=RUNTIME_INCOMPLETE_CODE)


class QwenGenerationError(QwenHostError):
    """The loaded checkpoint could not produce usable audio for this segment."""

    def __init__(self, message: str, *, code: str = "generation_failed") -> None:
        super().__init__(message, code=code)


# --------------------------------------------------------------------------- #
# logging (stdout carries frames and nothing else)
# --------------------------------------------------------------------------- #


def log_to_stderr(event: str, **fields: Any) -> None:
    """Write one structured JSON log line to stderr; never raise on a bad stderr."""
    stream = sys.stderr
    if stream is None:
        return
    try:
        stream.write(json.dumps({"host": HOST_NAME, "event": event, **fields}, sort_keys=True))
        stream.write("\n")
        stream.flush()
    except (OSError, ValueError, AttributeError):  # a closed stderr must not kill the host
        pass


def configure_import_path(environment: Mapping[str, str] | None = None) -> list[str]:
    """Put the managed runtime on ``sys.path`` before the first heavy import.

    A source-checkout host is started with the runtime already on
    ``PYTHONPATH``; a FROZEN host cannot be — PyInstaller's importer ignores
    it — so the parent passes the directory in ``RUNTIME_ENV`` and it is
    inserted here. ``torch``/``transformers``/``qwen_tts`` therefore stay out
    of the app bundle and come from the managed runtime at load time.
    Returns the entries actually added (the caller logs them).
    """
    source = os.environ if environment is None else environment
    entry = (source.get(RUNTIME_ENV) or "").strip()
    if not entry or entry in sys.path:
        return []
    sys.path.insert(0, entry)
    return [entry]


def _message(text: str) -> str:
    return text[:MAX_MESSAGE_CHARS]


# --------------------------------------------------------------------------- #
# stateful 24 kHz → 48 kHz resampling (ported from the prior Qwen branch)
# --------------------------------------------------------------------------- #


class StreamingResampler:
    """Linear-interpolation resampler carrying state across chunk pushes.

    Qwen emits native-rate audio (24 kHz) while the artifact writer and the live
    transport expect 48 kHz mono float32. Resampling each chunk independently
    would restart the interpolation phase at every boundary (clicks); this class
    carries the last input sample plus the fractional output position across
    :meth:`push` calls, so a chunked stream matches a one-shot resample.

    Output length for N total input samples is ``floor((N - 1) * dst / src) + 1``
    — within one output sample of the ideal duration. NumPy-only (scipy is not a
    project dependency).
    """

    def __init__(self, src_rate: int, dst_rate: int = APP_SAMPLE_RATE) -> None:
        if not isinstance(src_rate, int) or isinstance(src_rate, bool) or src_rate <= 0:
            raise ValueError(f"src_rate must be a positive int, got {src_rate!r}")
        if not isinstance(dst_rate, int) or isinstance(dst_rate, bool) or dst_rate <= 0:
            raise ValueError(f"dst_rate must be a positive int, got {dst_rate!r}")
        self._src_rate = src_rate
        self._dst_rate = dst_rate
        self._step = src_rate / dst_rate  # input samples per output sample
        self._carry: np.ndarray = np.zeros(0, dtype=np.float32)
        self._pos = 0.0  # input position of the next output sample

    @property
    def src_rate(self) -> int:
        return self._src_rate

    @property
    def dst_rate(self) -> int:
        return self._dst_rate

    def push(self, chunk: np.ndarray) -> np.ndarray:
        """Resample one native-rate chunk; empty input yields empty output."""
        data = _as_mono_float32(chunk)
        if self._src_rate == self._dst_rate:
            out = np.concatenate([self._carry, data]) if self._carry.size else data.copy()
            self._carry = np.zeros(0, dtype=np.float32)
            self._pos = 0.0
            return out
        window = np.concatenate([self._carry, data]) if self._carry.size else data
        if window.size < 2:
            self._carry = window.copy()
            return np.zeros(0, dtype=np.float32)
        # Emit every position <= window.size - 2: the final in-window position
        # has no right neighbour yet and more input may still arrive (flush
        # emits it). Positions are generated by multiplication, not repeated
        # addition — same values for the 2×/½× ratios this resampler runs.
        positions = _positions(self._pos, self._step, window.size - 2)
        out = _interpolate(window, positions)
        # Keep every input sample at or after the last emitted pair's left index:
        # unconsumed input plus the one-sample overlap that keeps continuity.
        consumed = int(positions[-1]) if positions.size else 0
        keep_from = max(consumed, 0)
        self._carry = window[keep_from:].copy()
        self._pos = self._pos + positions.size * self._step - keep_from
        return out

    def flush(self) -> np.ndarray:
        """Emit the tail sample(s) up to the final input position."""
        if self._src_rate == self._dst_rate:
            out = self._carry
            self._carry = np.zeros(0, dtype=np.float32)
            self._pos = 0.0
            return out
        window = self._carry
        if window.size == 0:
            return np.zeros(0, dtype=np.float32)
        positions = _positions(self._pos, self._step, window.size - 1)
        out = _interpolate(window, positions)
        self._carry = np.zeros(0, dtype=np.float32)
        self._pos = 0.0
        return out

    def reset(self) -> None:
        """Drop carried state (cancellation / new job)."""
        self._carry = np.zeros(0, dtype=np.float32)
        self._pos = 0.0


def _as_mono_float32(chunk: np.ndarray) -> np.ndarray:
    if not isinstance(chunk, np.ndarray):
        raise ValueError(f"chunk must be a numpy array, got {type(chunk).__name__}")
    if chunk.ndim != 1:
        raise ValueError(f"chunk must be 1-D mono, got {chunk.ndim} dimensions")
    out = chunk.astype(np.float32, copy=False)
    if not np.all(np.isfinite(out)):
        raise ValueError("chunk must be finite")
    return out


def _positions(start: float, step: float, limit: float) -> np.ndarray:
    """Output positions ``start + k * step`` for every value ``<= limit``.

    Vectorized form of the old per-sample accumulation loop (one Python
    iteration per output sample — ~4 ms per audio-second): the values are
    identical for the exact binary ratios the resampler runs (0.5×/1×/2×), and
    the ``<= limit`` mask reproduces the loop's emission rule exactly. Never
    returns a position past ``limit``, so ``_interpolate``'s clamped right
    neighbour stays in bounds.
    """
    count = int((limit - start) / step) + 2
    if count <= 0:
        return np.zeros(0, dtype=np.float64)
    candidates = start + np.arange(count, dtype=np.float64) * step
    return candidates[candidates <= limit]


def _interpolate(window: np.ndarray, positions: np.ndarray) -> np.ndarray:
    if positions.size == 0:
        return np.zeros(0, dtype=np.float32)
    left = np.floor(positions).astype(np.int64)
    frac = (positions - left).astype(np.float32)
    right = np.minimum(left + 1, window.size - 1)
    return ((1.0 - frac) * window[left] + frac * window[right]).astype(np.float32)


def _pcm_payload(samples: np.ndarray) -> bytes:
    """Little-endian float32 bytes — exactly what ``pcm_from_bytes`` decodes."""
    return np.ascontiguousarray(samples, dtype="<f4").tobytes()


def _emit_frames(
    job: str,
    samples: np.ndarray,
    seq: int,
    emitted: int,
    emit: Callable[[Frame], None],
    *,
    final: bool,
    segment: int | None = None,
) -> tuple[int, int]:
    """Emit one resampled buffer as bounded 48 kHz frames; returns ``(seq, emitted)``."""
    for start in range(0, samples.size, PCM_FRAME_SAMPLES):
        chunk = samples[start : start + PCM_FRAME_SAMPLES]
        last = final and start + PCM_FRAME_SAMPLES >= samples.size
        fields: dict[str, Any] = {"sampleRate": APP_SAMPLE_RATE, "seq": seq, "final": last}
        if segment is not None:
            fields["segment"] = segment
        emit(Frame(type="pcm", job=job, payload=_pcm_payload(chunk), fields=fields))
        seq += 1
        emitted += chunk.size
    return seq, emitted


# --------------------------------------------------------------------------- #
# load view: one directory, both verified trees
# --------------------------------------------------------------------------- #


def build_load_view(profile_dir: Path, shared_dir: Path, load_dir: Path) -> Path:
    """Merge the verified trees into the single path ``from_pretrained`` needs.

    ``Qwen3TTSModel.from_pretrained`` resolves the text tokenizer, the
    generation config and the hardcoded ``speech_tokenizer/`` subfolder from one
    path, while the managed install keeps the 686 MB tokenizer/speech-tokenizer
    tree shared between both profiles. The load view *is* that one path: every
    file is hard-linked (symlinked, or copied as a last resort) into place, so
    building it costs no disk space and no verification is skipped.
    """
    profile_dir = Path(profile_dir)
    shared_dir = Path(shared_dir)
    load_dir = Path(load_dir)
    if not profile_dir.is_dir():
        raise QwenLoadError(f"model directory is missing: {profile_dir}")
    if not shared_dir.is_dir():
        raise QwenLoadError(f"shared tokenizer directory is missing: {shared_dir}")
    entries = [*_tree_files(shared_dir), *_tree_files(profile_dir)]  # profile wins a collision
    if not entries:
        raise QwenLoadError(f"model directory is empty: {profile_dir}")
    try:
        load_dir.mkdir(parents=True, exist_ok=True)
        for relative, source in entries:
            target = load_dir / relative
            if _is_linked(target, source):
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_symlink() or target.exists():
                target.unlink()
            _link_file(source, target)
    except OSError as exc:
        raise QwenLoadError(f"could not prepare the model load directory: {exc}") from exc
    return load_dir


def remove_load_view(load_dir: Path) -> None:
    """Delete a load view (links only): best effort, never a symlinked directory."""
    path = Path(load_dir)
    if path.is_symlink():
        return
    shutil.rmtree(path, ignore_errors=True)


def _tree_files(root: Path) -> list[tuple[str, Path]]:
    """Every regular file under ``root`` as ``(relative posix path, path)``."""
    return [
        (path.relative_to(root).as_posix(), path)
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != INSTALL_METADATA_NAME
    ]


def _is_linked(target: Path, source: Path) -> bool:
    try:
        return target.exists() and os.path.samefile(target, source)
    except OSError:
        return False


def _link_file(source: Path, target: Path) -> None:
    """Hard link, then symlink, then copy — same-filesystem links are the norm."""
    for link in (os.link, os.symlink):
        try:
            link(source, target)
            return
        except (OSError, NotImplementedError):
            continue
    shutil.copyfile(source, target)


# --------------------------------------------------------------------------- #
# capabilities
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class HostCapabilities:
    """What the loaded checkpoint can actually serve, in app-level ids."""

    profile: str
    speakers: tuple[str, ...]
    languages: tuple[str, ...]
    supports_clone: bool
    sample_rate: int

    def frame_fields(self) -> dict[str, Any]:
        """Fields of the ``capabilities`` frame (job-less, so no job tag)."""
        return {
            "speakers": list(self.speakers),
            "languages": list(self.languages),
            "supportsClone": self.supports_clone,
            "sampleRate": self.sample_rate,
        }


def describe_capabilities(
    profile: str, model: object | None = None, *, log: LogFn | None = None
) -> HostCapabilities:
    """Report the pinned table, narrowed by what the checkpoint reports."""
    engine_id = PROFILE_ENGINES.get(profile)
    if engine_id is None:
        raise QwenHostError(
            f"unsupported Qwen profile {profile!r} — expected one of: "
            f"{', '.join(sorted(PROFILE_ENGINES))}"
        )
    caps = get_capabilities(engine_id)
    return HostCapabilities(
        profile=profile,
        speakers=_reported_speakers(caps, model, log=log),
        languages=_reported_languages(caps, model, log=log),
        supports_clone=caps.supports_cloning,
        sample_rate=APP_SAMPLE_RATE,
    )


def _model_reported(model: object | None, method: str) -> tuple[str, ...] | None:
    """Ask the loaded checkpoint what it supports; ``None`` when it does not say."""
    reader = getattr(model, method, None)
    if not callable(reader):
        return None
    try:
        reported = reader()
    except Exception:  # noqa: BLE001 — a runtime reporting quirk must not kill the host
        return None
    if not isinstance(reported, (list, tuple)):
        return None
    names = tuple(str(item) for item in reported if isinstance(item, str) and item.strip())
    return names or None


def _reported_speakers(caps: Any, model: object | None, *, log: LogFn | None) -> tuple[str, ...]:
    pinned = tuple(voice.voice_id for voice in caps.voices)
    reported = _model_reported(model, "get_supported_speakers")
    if reported is None:
        return pinned
    wanted = {name.lower() for name in reported}
    mapped = tuple(name for name in pinned if name.lower() in wanted)
    if mapped:
        return mapped
    _fallback(log, "speakers", reported)
    return pinned


def _reported_languages(caps: Any, model: object | None, *, log: LogFn | None) -> tuple[str, ...]:
    pinned = tuple(option.code for option in caps.languages)
    reported = _model_reported(model, "get_supported_languages")
    if reported is None:
        return pinned
    wanted = {name.lower() for name in reported}
    mapped = tuple(
        option.code
        for option in caps.languages
        if option.model_name.lower() in wanted or (option.is_auto and "auto" in wanted)
    )
    if mapped:
        return mapped
    _fallback(log, "languages", reported)
    return pinned


def _fallback(log: LogFn | None, kind: str, reported: Sequence[str]) -> None:
    (log if log is not None else log_to_stderr)(
        "capabilities_fallback", kind=kind, reported=list(reported)
    )


# --------------------------------------------------------------------------- #
# the host itself
# --------------------------------------------------------------------------- #


def describe_import_failure(exc: BaseException) -> str:
    """One line naming what the managed runtime could not import.

    A ``ModuleNotFoundError`` names the module missing from the pinned closure;
    anything else (a native library that will not load, a wheel built for
    another interpreter) can only be reported as the interpreter phrased it.
    """
    module = str(getattr(exc, "name", "") or "")
    if isinstance(exc, ModuleNotFoundError) and module:
        return f"the managed Qwen runtime is incomplete: Python module {module!r} is missing"
    return f"the managed Qwen runtime cannot import its stack: {exc}"


def import_qwen_sdk() -> Any:
    """Import torch and the ``qwen_tts`` SDK from the managed runtime.

    Never at module load: the app and its test suite stay free of the isolated
    runtime's dependency stack. Import failures propagate untouched — the load
    path turns them into a :class:`QwenRuntimeIncompleteError` and the import
    check reports them as a verdict, so the classification lives in one place.
    """
    import torch  # noqa: PLC0415 — provided by the managed runtime, never by the app

    del torch  # imported to prove the native stack loads, not to use it here
    from qwen_tts import Qwen3TTSModel  # noqa: PLC0415 — optional runtime, lazy by design

    return Qwen3TTSModel


def check_runtime_imports() -> tuple[bool, str]:
    """Import the stack a load needs, without opening a checkpoint or a socket.

    Returns ``(ok, detail)``; ``detail`` names the missing module (or the import
    failure) when the runtime cannot serve a load. The app's runtime manager
    runs this through :data:`HOST_CHECK_FLAG` before committing an install, so a
    pinned closure that installs but cannot import is rejected there instead of
    failing every synthesis afterwards.
    """
    try:
        import_qwen_sdk()
    except ImportError as exc:
        return False, describe_import_failure(exc)
    except Exception as exc:  # noqa: BLE001 — a native stack that will not load is not ImportError
        return False, f"the managed Qwen runtime failed to import: {exc}"
    return True, ""


def default_model_loader(
    path: Path, *, device: str, dtype: str, attention: str, profile: str
) -> object:
    """Open one checkpoint from local paths only — no remote code, no network.

    ``qwen_tts``/``torch`` are imported here and never at module load, so the app
    and its test suite stay free of the isolated runtime's dependency stack. A
    runtime that cannot import them is reported as the runtime problem it is:
    the checkpoint and its trees are irrelevant when the closure is incomplete.
    """
    del profile  # the merged load view already carries the profile identity
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
    try:
        sdk = import_qwen_sdk()
    except ImportError as exc:
        raise QwenRuntimeIncompleteError(
            _message(f"{describe_import_failure(exc)}. {RUNTIME_REPAIR_HINT}")
        ) from exc

    return sdk.from_pretrained(
        str(path),
        device_map=device,
        dtype=_torch_dtype(dtype),
        attn_implementation=attention,
        local_files_only=True,
        trust_remote_code=False,
    )


def lower_process_priority() -> bool:
    """Drop this process's CPU priority so synthesis yields to the desktop.

    Generation is a batch workload: on the CPU device it otherwise fills every
    physical core torch can see, and everything the user is doing — this app's
    own UI and playback included — competes with it at equal priority. Lower
    priority changes nothing on an idle machine (the host still gets every
    idle cycle) and under contention the machine stays responsive. Windows
    maps to BELOW_NORMAL_PRIORITY_CLASS; POSIX nudges nice +5, which a
    non-root process is always allowed to do to itself. Best effort: a
    platform that refuses is reported and synthesis runs as before.
    """
    try:
        if os.name == "nt":
            import ctypes  # noqa: PLC0415 — Windows only, never at module load

            below_normal_priority_class = 0x00004000
            kernel32 = ctypes.windll.kernel32
            return bool(
                kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), below_normal_priority_class)
            )
        os.nice(5)
        return True
    except Exception:  # noqa: BLE001 — a refused priority change must not stop synthesis
        return False


def configure_torch_threads() -> dict[str, int]:
    """Pin torch's thread posture for this single-stream inference host.

    Inter-op parallelism is pinned to 1: the host runs one sequential
    ``generate_*`` at a time, so torch's physical-core-sized inter-op pool only
    adds contention (PyTorch's own single-stream inference guidance). torch
    refuses a second ``set_num_interop_threads`` once parallel work has started,
    and stub builds lack the calls entirely — every failure is swallowed and
    the read-back reports the posture actually in effect. Intra-op stays at
    torch's physical-core default unless ``QWEN_NUM_THREADS`` pins it: the
    evidence knob for hybrid P/E-core CPUs and container quotas
    (``scripts/spike/qwen_runtime_probe.py --num-threads`` drives the same
    setting for measurements). Never raises — the torch-free test host stays
    torch-free (``{}`` means "unconfigured runtime").
    """
    try:
        import torch  # noqa: PLC0415 — provided by the managed runtime, never by the app
    except ImportError:
        return {}
    with contextlib.suppress(Exception):  # refused after parallel work, or a stub build
        torch.set_num_interop_threads(1)
    raw = os.environ.get(THREADS_ENV, "").strip()
    if raw:
        try:
            value = int(raw)
        except ValueError:
            value = 0
        if value > 0:
            with contextlib.suppress(Exception):  # degrade to the runtime default
                torch.set_num_threads(value)
    try:
        return {
            "intra": int(torch.get_num_threads()),
            "inter": int(torch.get_num_interop_threads()),
        }
    except Exception:  # noqa: BLE001 — a stub without getters is unconfigured
        return {}


def _torch_dtype(dtype: str) -> Any:
    import torch  # noqa: PLC0415 — provided by the managed runtime, never by the app

    resolved = getattr(torch, dtype, None)
    if resolved is None:
        raise QwenLoadError(f"the Qwen runtime has no dtype {dtype!r}")
    return resolved


@contextlib.contextmanager
def _heartbeats(job: str, emit: Callable[[Frame], None]) -> Iterator[None]:
    """Emit fraction-less ``progress`` heartbeats while a blocking generate runs.

    ``generate_*`` is one uninterruptible call that returns nothing until the
    whole segment exists. Without these, a generate longer than the parent's
    frame timeout fails a perfectly healthy host, and a cancel has no way to
    tell "busy" from "hung" apart from killing the loaded model. A fraction-less
    ``progress`` frame is exactly that signal: liveness only, never UI progress
    (the parent filters on ``fraction``). The beater is stopped and drained
    before the caller emits anything else, so writes never interleave.
    """
    stop = threading.Event()
    drained = threading.Event()

    def beat() -> None:
        while not stop.wait(HEARTBEAT_SECONDS):
            emit(Frame(type="progress", job=job, fields={"stage": "generating"}))
        drained.set()

    thread = threading.Thread(target=beat, name="qwen-host-liveness", daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        drained.wait(timeout=HEARTBEAT_SECONDS + 1.0)


class QwenModelHost:
    """Host-side state: one loaded checkpoint, one job at a time, one resampler."""

    def __init__(
        self,
        *,
        loader: ModelLoader | None = None,
        log: LogFn | None = None,
        resampler_factory: Callable[..., StreamingResampler] = StreamingResampler,
    ) -> None:
        self._loader = loader
        self._log = log if log is not None else log_to_stderr
        self._resampler_factory = resampler_factory
        self._model: object | None = None
        self._profile = ""
        self._engine_id = ""
        self._capabilities: HostCapabilities | None = None
        self._load_view: Path | None = None
        self._prompts: dict[tuple[str, str], object] = {}
        self._fatal = False

    @property
    def loaded(self) -> bool:
        return self._model is not None

    @property
    def profile(self) -> str:
        return self._profile

    @property
    def capabilities(self) -> HostCapabilities | None:
        return self._capabilities

    @property
    def fatal(self) -> bool:
        """True once a device/OOM failure made this process unfit to continue."""
        return self._fatal

    def load(self, fields: Mapping[str, Any]) -> HostCapabilities:
        """Validate a ``load`` frame, swap the resident model, and report capabilities."""
        profile = str(fields.get("profile", ""))
        engine_id = PROFILE_ENGINES.get(profile)
        if engine_id is None:
            raise QwenLoadError(
                f"unsupported Qwen profile {profile!r} — expected one of: "
                f"{', '.join(sorted(PROFILE_ENGINES))}"
            )
        caps = get_capabilities(engine_id)
        device = str(fields.get("device", ""))
        if device not in caps.devices:
            raise QwenLoadError(
                f"unsupported device {device!r} for {caps.label} — "
                f"supported: {', '.join(caps.devices)}"
            )
        dtype = str(fields.get("dtype", ""))
        if dtype not in SUPPORTED_DTYPES:
            raise QwenLoadError(
                f"unsupported dtype {dtype!r} — supported: {', '.join(SUPPORTED_DTYPES)}"
            )
        attention = str(fields.get("attention", ""))
        if attention not in SUPPORTED_ATTENTION:
            raise QwenLoadError(
                f"unsupported attention implementation {attention!r} — "
                f"supported: {', '.join(SUPPORTED_ATTENTION)}"
            )
        model_dir = Path(str(fields.get("modelDir", "")))
        shared_dir = Path(str(fields.get("sharedDir", "")))
        if not model_dir.is_dir():
            raise QwenLoadError(f"model directory is missing: {model_dir}")
        if not shared_dir.is_dir():
            raise QwenLoadError(f"shared tokenizer directory is missing: {shared_dir}")

        self.close()  # exactly one large model owner may be resident
        view = build_load_view(model_dir, shared_dir, model_dir.parent / LOAD_VIEW_DIR / profile)
        loader = self._loader if self._loader is not None else default_model_loader
        try:
            model = loader(view, device=device, dtype=dtype, attention=attention, profile=profile)
        except QwenLoadError:
            remove_load_view(view)
            raise
        except Exception as exc:  # noqa: BLE001 — the runtime reports anything
            remove_load_view(view)
            raise QwenLoadError(f"could not load {caps.label}: {exc}") from exc
        self._model = model
        self._profile = profile
        self._engine_id = engine_id
        self._load_view = view
        self._prompts = {}
        self._capabilities = describe_capabilities(profile, model, log=self._log)
        self._log(
            "loaded",
            profile=profile,
            device=device,
            dtype=dtype,
            attention=attention,
            threads=configure_torch_threads(),
            dir=str(view),
        )
        return self._capabilities

    def close(self) -> None:
        """Drop the resident model, its cached clone prompts, and the load view."""
        if self._model is not None:
            self._model = None
            _release_accelerator()
        self._profile = ""
        self._engine_id = ""
        self._capabilities = None
        self._prompts = {}
        if self._load_view is not None:
            remove_load_view(self._load_view)
            self._load_view = None

    def _prepare(self, fields: Mapping[str, Any]) -> tuple[str, str, str, str]:
        """Validate one speaking selection: ``(language_name, speaker, prompt, ref_text)``.

        Shared by ``synthesize`` and ``synthesize_batch`` so both accept exactly
        the same selections with the same messages (including the "use VieNeu
        for Vietnamese" hint the shared validator carries).
        """
        caps = get_capabilities(self._engine_id)
        language = str(fields.get("language", ""))
        speaker = str(fields.get("speaker", ""))
        prompt_path = str(fields.get("voicePrompt", ""))
        ref_text = str(fields.get("refText", ""))
        is_custom = self._engine_id == QWEN_CUSTOM
        try:
            # The shared validator keeps host and app messages identical (including
            # the "use VieNeu for Vietnamese" hint); for Base the host's clone
            # identity is the reference clip the parent already resolved.
            validate_selection(
                self._engine_id,
                language=language,
                voice_id=speaker if is_custom else "",
                clone_id="" if is_custom else prompt_path,
            )
            if not is_custom:
                if not ref_text.strip():
                    raise QwenHostError(
                        "Qwen3-TTS Base needs the reference transcript for its clone prompt"
                    )
                if not Path(prompt_path).is_file():
                    raise QwenHostError(f"reference clip is missing: {prompt_path}")
            language_name = language_model_name(caps, language)
        except EngineProfileError as exc:
            raise QwenHostError(str(exc), code="unsupported_selection") from exc
        return language_name, speaker, prompt_path, ref_text

    def _generate_selected(
        self,
        job: str,
        texts: list[str],
        emit: Callable[[Frame], None],
        selection: tuple[str, str, str, str],
    ) -> tuple[list[Any], int] | Frame:
        """One uninterruptible generate for ``texts``; wavs or a failure terminal."""
        language_name, speaker, prompt_path, ref_text = selection
        try:
            with _heartbeats(job, emit):
                wavs, rate = self._generate(
                    texts[0] if len(texts) == 1 else list(texts),
                    language_name,
                    speaker,
                    prompt_path,
                    ref_text,
                )
        except QwenHostError as exc:
            return self._failure(job, exc.code, str(exc), fatal=exc.fatal, emit=emit)
        except Exception as exc:  # noqa: BLE001 — the model runtime reports anything
            detail = f"{type(exc).__name__}: {exc}"
            return self._failure(
                job, "generation_failed", detail, fatal=_is_device_error(exc), emit=emit
            )
        produced = list(wavs) if isinstance(wavs, (list, tuple)) else []
        if len(produced) != len(texts):
            return self._failure(
                job,
                "generation_failed",
                f"the Qwen model returned {len(produced)} segments for a batch of {len(texts)}",
                emit=emit,
            )
        return produced, int(rate)

    def synthesize(
        self,
        job: str,
        fields: Mapping[str, Any],
        emit: Callable[[Frame], None],
        *,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> Frame:
        """Generate one bounded segment and stream it; always returns one terminal."""
        if self._model is None or self._capabilities is None:
            return self._failure(job, "not_loaded", "no Qwen model is loaded", emit=emit)
        try:
            selection = self._prepare(fields)
        except QwenHostError as exc:
            return self._failure(job, exc.code, str(exc), emit=emit)

        generated = self._generate_selected(job, [str(fields.get("text", ""))], emit, selection)
        if isinstance(generated, Frame):
            return generated
        wavs, rate = generated

        try:
            samples = _validate_audio(wavs, rate)
        except QwenHostError as exc:
            return self._failure(job, exc.code, str(exc), emit=emit)
        seq, emitted, cancelled_terminal = self._stream_pcm(job, samples, emit, cancelled=cancelled)
        if cancelled_terminal is not None:
            return cancelled_terminal
        seconds = round(emitted / APP_SAMPLE_RATE, 3)
        self._log("job_ok", job=job, frames=seq, seconds=seconds)
        return Frame(
            type="terminal",
            job=job,
            fields={"status": "ok", "frames": seq, "audioSeconds": seconds},
        )

    def synthesize_batch(
        self,
        job: str,
        fields: Mapping[str, Any],
        emit: Callable[[Frame], None],
        *,
        cancelled: Callable[[], bool] = lambda: False,
    ) -> Frame:
        """Generate several bounded segments in ONE model call; stream in order.

        ``qwen_tts`` generation is batch-native (lists in, per-item trimming,
        batched codec decode), so an export job's segments amortize their
        prompt/prefill instead of running one autoregressive pass each. Each
        segment streams as its own ``segment``-tagged frames; a cancel settles
        once the uninterruptible call returns and never streams later segments.
        """
        if self._model is None or self._capabilities is None:
            return self._failure(job, "not_loaded", "no Qwen model is loaded", emit=emit)
        texts = [str(item) for item in fields.get("texts", ())]
        try:
            selection = self._prepare(fields)
        except QwenHostError as exc:
            return self._failure(job, exc.code, str(exc), emit=emit)

        generated = self._generate_selected(job, texts, emit, selection)
        if isinstance(generated, Frame):
            return generated
        wavs, rate = generated

        seq = 0
        emitted = 0
        for index, wav in enumerate(wavs):
            if cancelled():
                self._log("job_cancelled", job=job, frames=seq)
                return Frame(
                    type="terminal", job=job, fields={"status": "cancelled", "frames": seq}
                )
            try:
                samples = _validate_audio([wav], rate)
            except QwenHostError as exc:
                return self._failure(job, exc.code, str(exc), emit=emit)
            seq, segment_emitted, cancelled_terminal = self._stream_pcm(
                job,
                samples,
                emit,
                cancelled=cancelled,
                segment=index,
                seq_start=seq,
                fraction_base=index / len(wavs),
                fraction_span=1 / len(wavs),
            )
            if cancelled_terminal is not None:
                return cancelled_terminal
            emitted += segment_emitted
        seconds = round(emitted / APP_SAMPLE_RATE, 3)
        self._log("job_ok", job=job, frames=seq, seconds=seconds, segments=len(wavs))
        return Frame(
            type="terminal",
            job=job,
            fields={
                "status": "ok",
                "frames": seq,
                "audioSeconds": seconds,
                "segments": len(wavs),
            },
        )

    # -- internals ---------------------------------------------------------- #

    def _generate(
        self,
        text: str | Sequence[str],
        language_name: str,
        speaker: str,
        prompt_path: str,
        ref_text: str,
    ) -> tuple[Any, Any]:
        model = self._model
        # A multi-segment batch must turn the SDK's non-streaming mode OFF.
        # qwen_tts 0.1.1 left-pads the batched prompts in that mode and emits
        # NaN logits whenever the items differ in length, which surfaces as
        # "probability tensor contains either `inf`, `nan` or element < 0" from
        # the sampler and fails the whole export job. Measured on the real
        # 0.6B CustomVoice checkpoint: two unequal segments NaN with the
        # default, return both wavs with the flag off, and equal-length items
        # are fine either way (docs/performance/qwen-runtime-compatibility.md).
        # A one-segment call keeps the SDK default, so the interactive path is
        # bit-for-bit what it always was.
        batch_mode = {"non_streaming_mode": False} if _is_multi_segment(text) else {}
        if self._engine_id == QWEN_CUSTOM:
            # `instruct` is deliberately not forwarded: the 0.6B CustomVoice
            # checkpoint ignores it, and the product must not imply otherwise.
            return model.generate_custom_voice(  # type: ignore[attr-defined]
                text=text, language=language_name, speaker=speaker, **batch_mode
            )
        prompt = self._clone_prompt(prompt_path, ref_text)
        return model.generate_voice_clone(  # type: ignore[attr-defined]
            text=text, language=language_name, voice_clone_prompt=prompt, **batch_mode
        )

    def _clone_prompt(self, path: str, ref_text: str) -> object:
        """Build (once) and reuse the reusable clone prompt for one reference clip."""
        resolved = str(Path(path).resolve())
        key = (resolved, ref_text)
        cached = self._prompts.get(key)
        if cached is not None:
            return cached
        builder = getattr(self._model, "create_voice_clone_prompt", None)
        if not callable(builder):
            raise QwenHostError(
                "the loaded Qwen runtime cannot build clone prompts",
                code="clone_prompt_unsupported",
            )
        prompt = builder(ref_audio=resolved, ref_text=ref_text)
        self._prompts[key] = prompt
        self._log("clone_prompt_built", reference=resolved)
        return prompt

    def _stream_pcm(
        self,
        job: str,
        samples: np.ndarray,
        emit: Callable[[Frame], None],
        *,
        cancelled: Callable[[], bool],
        segment: int | None = None,
        seq_start: int = 0,
        fraction_base: float = 0.0,
        fraction_span: float = 1.0,
    ) -> tuple[int, int, Frame | None]:
        """Resample one segment through one stateful stream and emit bounded frames.

        Frames leave as soon as a chunk has been resampled — a one-chunk
        lookahead is what keeps ``final`` exact — so a cancel mid-segment leaves
        a partial stream for the parent to discard instead of a buffered
        whole-segment write. Returns ``(next_seq, emitted_samples,
        cancelled_terminal)``: the terminal is ``None`` on success and the
        caller builds its own ``ok`` terminal, so a batch can account every
        segment into one.

        """
        resampler = self._resampler_factory(QWEN_SOURCE_RATE, APP_SAMPLE_RATE)
        seq = seq_start
        emitted = 0
        pending: np.ndarray | None = None
        total = samples.size
        for start in range(0, total, RESAMPLE_CHUNK_SAMPLES):
            if cancelled():
                self._log("job_cancelled", job=job, frames=seq)
                return (
                    seq,
                    emitted,
                    Frame(type="terminal", job=job, fields={"status": "cancelled", "frames": seq}),
                )
            chunk = samples[start : start + RESAMPLE_CHUNK_SAMPLES]
            out = resampler.push(chunk)
            if pending is not None:
                seq, emitted = _emit_frames(
                    job, pending, seq, emitted, emit, final=False, segment=segment
                )
            pending = out
            emit(
                Frame(
                    type="progress",
                    job=job,
                    fields={
                        "fraction": min(
                            1.0, fraction_base + fraction_span * (start + chunk.size) / total
                        ),
                        "stage": "resampling",
                    },
                )
            )
        tail = resampler.flush()
        if pending is None:
            pending = tail
        elif tail.size:
            pending = np.concatenate([pending, tail])
        seq, emitted = _emit_frames(job, pending, seq, emitted, emit, final=True, segment=segment)
        return seq, emitted, None

    def _failure(
        self,
        job: str,
        code: str,
        message: str,
        *,
        fatal: bool = False,
        emit: Callable[[Frame], None],
    ) -> Frame:
        detail = _message(message)
        self._log("job_failed", job=job, code=code, fatal=fatal, message=detail)
        emit(
            Frame(
                type="error",
                job=job,
                fields={"code": code, "message": detail, "fatal": fatal},
            )
        )
        if fatal:
            self._fatal = True
            self.close()
        return Frame(type="terminal", job=job, fields={"status": "failed", "error": detail})


def _validate_audio(wavs: Any, rate: Any) -> np.ndarray:
    """Validate one generated segment: 24 kHz, mono, finite, non-empty."""
    if int(rate) != QWEN_SOURCE_RATE:
        raise QwenGenerationError(
            f"Qwen sample-rate drift: the model returned {rate} Hz, "
            f"expected {QWEN_SOURCE_RATE} Hz — update QWEN_SOURCE_RATE",
            code="sample_rate_drift",
        )
    if not isinstance(wavs, (list, tuple)) or not wavs:
        raise QwenGenerationError("the Qwen model returned no audio")
    audio = np.asarray(wavs[0], dtype=np.float32)
    if audio.ndim != 1:
        raise QwenGenerationError(
            f"the Qwen model returned {audio.ndim}-D audio; mono output is required"
        )
    if audio.size == 0:
        raise QwenGenerationError("the Qwen model returned empty audio")
    if not np.all(np.isfinite(audio)):
        raise QwenGenerationError("the Qwen model returned non-finite audio")
    return np.ascontiguousarray(audio)


def _is_device_error(exc: Exception) -> bool:
    """Device/OOM failures poison the runtime; the host restarts instead of retrying."""
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(marker in text for marker in _DEVICE_ERROR_MARKERS)


def _is_multi_segment(text: Any) -> bool:
    """True for the list form of a batch call (one string is a single segment)."""
    return isinstance(text, (list, tuple)) and len(text) > 1


def _release_accelerator() -> None:
    """Best-effort accelerator cache release that never imports torch into a torch-free host."""
    gc.collect()
    module = sys.modules.get("torch")
    if module is None:
        return
    try:
        if module.cuda.is_available():
            module.cuda.empty_cache()
    except Exception:  # noqa: BLE001 — a stub or partial build must degrade, not crash
        pass
    try:
        mps = getattr(getattr(module, "backends", None), "mps", None)
        if mps is not None and mps.is_available():
            module.mps.empty_cache()
    except Exception:  # noqa: BLE001 — a stub or partial build must degrade, not crash
        pass


# --------------------------------------------------------------------------- #
# frame loop
# --------------------------------------------------------------------------- #


class _CancelRequests:
    """Job ids the peer asked to stop; written by the reader, read by the job loop."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ids: set[str] = set()

    def add(self, job: str) -> None:
        with self._lock:
            self._ids.add(job)

    def discard(self, job: str) -> None:
        with self._lock:
            self._ids.discard(job)

    def contains(self, job: str) -> bool:
        with self._lock:
            return job in self._ids


def hello_frame() -> Frame:
    """The first frame on the pipe: host identity, platform, and frame sample rate."""
    return Frame(
        type="hello",
        fields={
            "host": HOST_NAME,
            "platform": f"{sys.platform}-{platform.machine()}",
            "python": f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            "sampleRate": APP_SAMPLE_RATE,
        },
    )


def serve(
    reader: IO[bytes],
    writer: IO[bytes],
    *,
    loader: ModelLoader | None = None,
    log: LogFn | None = None,
    resampler_factory: Callable[..., StreamingResampler] = StreamingResampler,
) -> int:
    """Run the host frame loop until ``shutdown`` or peer close; returns an exit code.

    A separate reader thread exists for one reason: a ``cancel`` must be visible
    while the main thread is inside a blocking ``generate_*`` call. Transition
    violations are still caught at read time through the shared
    :class:`~vienetts_app.core.qwen_protocol.SessionState`.
    """
    emit_log = log if log is not None else log_to_stderr
    host = QwenModelHost(loader=loader, log=emit_log, resampler_factory=resampler_factory)
    session = SessionState("host")
    state_lock = threading.Lock()
    cancelled = _CancelRequests()
    inbox: queue.Queue[object] = queue.Queue()

    def emit(frame: Frame) -> None:
        with state_lock:
            session.record_sent(frame)
            # Inside the lock: the liveness beater emits from its own thread
            # while the job loop is inside a blocking generate, and frames on
            # the wire must never interleave.
            write_frame(writer, frame)

    def read_frames() -> None:
        while True:
            try:
                frame = read_frame(reader)
            except EndOfStream:
                inbox.put(None)
                return
            except ProtocolError as exc:
                inbox.put(exc)
                return
            try:
                with state_lock:
                    session.accept(frame)
            except StaleFrameError as exc:
                emit_log("stale_frame", frame=frame.type, job=frame.job, error=str(exc))
                continue
            except ProtocolError as exc:
                inbox.put(exc)
                return
            if frame.type == "cancel":
                cancelled.add(frame.job)
                emit_log("cancel_requested", job=frame.job)
            inbox.put(frame)

    threading.Thread(target=read_frames, name="qwen-host-reader", daemon=True).start()
    exit_code = 0
    try:
        emit(hello_frame())
        while True:
            item = inbox.get()
            if item is None:
                emit_log("peer_closed")
                break
            if isinstance(item, ProtocolError):
                emit_log("protocol_error", error=str(item))
                exit_code = 2
                break
            frame = item
            if frame.type == "load":
                try:
                    capabilities = host.load(frame.fields)
                except (QwenHostError, EngineProfileError) as exc:
                    emit_log("load_failed", error=str(exc))
                    emit(
                        Frame(
                            type="error",
                            fields={
                                # The exception's own code: an incomplete runtime
                                # (RUNTIME_INCOMPLETE_CODE) must stay
                                # distinguishable from a bad model tree, because
                                # only one of the two is fixed from Settings.
                                "code": str(getattr(exc, "code", "") or "load_failed"),
                                "message": _message(str(exc)),
                                "fatal": False,
                            },
                        )
                    )
                else:
                    emit(Frame(type="capabilities", fields=capabilities.frame_fields()))
            elif frame.type in ("synthesize", "synthesize_batch"):
                handler = host.synthesize if frame.type == "synthesize" else host.synthesize_batch
                try:
                    terminal = handler(
                        frame.job,
                        frame.fields,
                        emit,
                        cancelled=lambda job=frame.job: cancelled.contains(job),
                    )
                finally:
                    cancelled.discard(frame.job)
                emit(terminal)
                # Between jobs the host drops its generation caches: an export
                # runs hundreds of jobs in this one process, and without this
                # the MPS/CUDA allocator's cached blocks ratchet the resident
                # footprint upward until the OS's memory manager notices (the
                # mbzv OOM kill). The parent's RSS watchdog recycles the host
                # if growth over its baseline still crosses the threshold.
                _release_accelerator()
                if host.fatal:
                    emit_log("fatal_exit")
                    exit_code = 1
                    break
            elif frame.type == "shutdown":
                emit_log("shutdown")
                break
            # `cancel` needs no action here: the reader thread already recorded it.
    except (BrokenPipeError, OSError) as exc:
        emit_log("write_failed", error=str(exc))
        exit_code = 1
    finally:
        host.close()
    return exit_code


def check_main() -> int:
    """Import-check mode: one JSON verdict line on stdout, exit 0/1.

    Runs in the same interpreter, environment and import path a load uses, so
    the parent can prove a promoted runtime is importable without importing
    torch into its own process. No protocol frames are written, and stdout is
    left untouched for the verdict.
    """
    runtime_entries = configure_import_path()
    ok, detail = check_runtime_imports()
    log_to_stderr("import_check", ok=ok, runtimePath=bool(runtime_entries), detail=detail)
    stream = sys.stdout
    if stream is None:
        return 2
    stream.write(json.dumps({"ok": ok, "detail": detail}))
    stream.write("\n")
    stream.flush()
    return 0 if ok else 1


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point of the isolated interpreter.

    With :data:`HOST_CHECK_FLAG` the process runs the import check instead of
    the frame loop; otherwise every input arrives over the pipe.
    """
    args = list(sys.argv[1:] if argv is None else argv)
    if HOST_CHECK_FLAG in args:
        return check_main()
    stdin = getattr(sys.stdin, "buffer", None)
    stdout = getattr(sys.stdout, "buffer", None)
    if stdin is None or stdout is None:
        log_to_stderr("no_stdio")
        return 2
    # Library chatter (transformers/tqdm) must never corrupt the frame stream.
    if sys.stderr is None:
        with contextlib.suppress(OSError):
            sys.stderr = open(os.devnull, "w")  # noqa: PTH123,SIM115 — kept as stdio
    with contextlib.suppress(Exception):
        sys.stdout = sys.stderr
    runtime_entries = configure_import_path()
    log_to_stderr("starting", pid=os.getpid(), runtimePath=bool(runtime_entries))
    log_to_stderr("priority", lowered=lower_process_priority())
    return serve(stdin, stdout)


if __name__ == "__main__":  # pragma: no cover - process entry point
    raise SystemExit(main())
