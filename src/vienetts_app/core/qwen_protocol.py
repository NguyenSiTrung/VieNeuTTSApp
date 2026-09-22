"""Framed IPC protocol between the app and the isolated Qwen model host.

The host runs in its own interpreter with its own site-packages, so the pipe is
the only contract between the two processes. Every frame is:

    header_len (4 bytes, big-endian)
    header     (UTF-8 JSON object, <= MAX_HEADER_BYTES)
    payload_len (4 bytes, big-endian)
    payload    (binary, <= MAX_PAYLOAD_BYTES)

Only ``pcm`` frames carry a payload: bounded little-endian float32 samples, so
the parent can hand them straight to the audio transport. Everything else is a
validated, job-tagged JSON header.

Frames are versioned, tagged with the immutable job id, and checked for valid
per-job transitions on both ends (:class:`SessionState`), so a host that emits a
late frame after a terminal, or a parent that synthesizes without loading, is
rejected instead of silently corrupting a job.
"""

from __future__ import annotations

import json
import struct
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import IO, Any, Literal

PROTOCOL_VERSION = 1

FRAME_TYPES = frozenset(
    {
        "hello",
        "load",
        "capabilities",
        "synthesize",
        "synthesize_batch",
        "pcm",
        "progress",
        "cancel",
        "terminal",
        "error",
        "shutdown",
    }
)

# Frames that only make sense for one job and therefore require a job id.
JOB_FRAMES = frozenset({"synthesize", "synthesize_batch", "pcm", "progress", "cancel", "terminal"})
# Frames that only exist for one job but may omit it (host-level failures).
OPTIONAL_JOB_FRAMES = frozenset({"error"})
# Frames that must never carry a job id.
UNJOBED_FRAMES = frozenset({"hello", "load", "capabilities", "shutdown"})

TERMINAL_STATUSES = frozenset({"ok", "cancelled", "failed"})
PROFILE_KEYS = frozenset({"customvoice", "base"})
DEVICES = frozenset({"cpu", "cuda", "mps"})

MAX_HEADER_BYTES = 64 * 1024
MAX_PAYLOAD_BYTES = 4 * 1024 * 1024
MAX_TEXT_CHARS = 2000
#: Largest batch one ``synthesize_batch`` frame may carry: the segments one
#: ``generate_*`` call produces together. Bounded so a batch's KV cache and
#: output stay in the same memory range as a few single segments.
MAX_BATCH_SEGMENTS = 4
#: Largest *total* text one ``synthesize_batch`` frame may carry across all of
#: its segments. Batch generation holds every segment's prompt, KV cache and
#: codec activations at once — in the pinned matrix's float32 on MPS — and a
#: 4 × MAX_TEXT_CHARS batch exhausted a 16 GB Mac mini: the kernel's memory
#: manager SIGKILLed the host mid-generate and the parent saw only a bare
#: closed-stream EOF. Capping the total keeps every batch inside what the
#: single-segment interactive path already proves fits, so the export fast
#: path amortizes prefill without ever multiplying worst-case memory. Must
#: stay >= MAX_TEXT_CHARS, or a single full-size segment could never batch.
MAX_BATCH_CHARS = MAX_TEXT_CHARS
MAX_JOB_ID = 64
MAX_MESSAGE_CHARS = 2000

#: Error code for a host load failure caused by the *runtime* rather than the
#: model: the promoted ``site-packages`` cannot import the stack a load needs
#: (a module is missing, or a native library will not load). The parent keys
#: user-facing recovery off it — no profile, model or device choice can fix it,
#: only reinstalling the runtime can — so it travels in the error frame instead
#: of being flattened into the generic ``load_failed``.
RUNTIME_INCOMPLETE_CODE = "runtime_incomplete"

_HEADER_LENGTH = struct.Struct(">I")
_PAYLOAD_LENGTH = struct.Struct(">I")
_SAMPLE = struct.Struct("<f")


class ProtocolError(ValueError):
    """A frame is malformed, unsupported, or invalid for the current state."""


class FrameTooLargeError(ProtocolError):
    """A declared frame length exceeds the protocol bound."""


class EndOfStream(EOFError):
    """The stream ended cleanly at a frame boundary."""


class StaleFrameError(ProtocolError):
    """A frame arrived for a job that already settled (drop it, do not crash)."""


@dataclass(frozen=True)
class Frame:
    type: str
    job: str = ""
    payload: bytes = b""
    fields: Mapping[str, Any] = field(default_factory=dict)

    def header(self) -> dict[str, Any]:
        header: dict[str, Any] = {"version": PROTOCOL_VERSION, "type": self.type}
        if self.job:
            header["job"] = self.job
        header.update(self.fields)
        return header

    def get(self, name: str, default: Any = None) -> Any:
        return self.fields.get(name, default)


def _require_str(fields: Mapping[str, Any], name: str, *, maximum: int) -> str:
    value = fields.get(name)
    if not isinstance(value, str) or not value:
        raise ProtocolError(f"frame field {name!r} must be a non-empty string")
    if len(value) > maximum:
        raise ProtocolError(f"frame field {name!r} exceeds {maximum} characters")
    return value


def _require_bool(fields: Mapping[str, Any], name: str) -> bool:
    value = fields.get(name)
    if not isinstance(value, bool):
        raise ProtocolError(f"frame field {name!r} must be a boolean")
    return value


def _require_int(fields: Mapping[str, Any], name: str, *, minimum: int) -> int:
    value = fields.get(name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ProtocolError(f"frame field {name!r} must be an integer")
    if value < minimum:
        raise ProtocolError(f"frame field {name!r} must be >= {minimum}")
    return value


def _optional_float(
    fields: Mapping[str, Any], name: str, *, minimum: float, maximum: float
) -> None:
    value = fields.get(name)
    if value is None:
        return
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise ProtocolError(f"frame field {name!r} must be a number")
    if not minimum <= float(value) <= maximum:
        raise ProtocolError(f"frame field {name!r} must be within [{minimum}, {maximum}]")


def validate_job_id(job: str) -> str:
    if not job:
        raise ProtocolError("frame is missing its job id")
    if len(job) > MAX_JOB_ID:
        raise ProtocolError(f"job id exceeds {MAX_JOB_ID} characters")
    if not all(character.isalnum() or character in "._:-" for character in job):
        raise ProtocolError("job id contains unsupported characters")
    return job


def _validate_speaking_fields(fields: Mapping[str, Any]) -> None:
    """Optional voice fields shared by ``synthesize`` and ``synthesize_batch``."""
    for optional in ("speaker", "instruct", "voicePrompt", "refText"):
        if optional in fields and not isinstance(fields[optional], str):
            raise ProtocolError(f"frame field {optional!r} must be a string")
    if "seed" in fields:
        _require_int(fields, "seed", minimum=0)


def _validate_header_fields(frame_type: str, fields: Mapping[str, Any]) -> None:
    if frame_type == "hello":
        _require_str(fields, "host", maximum=64)
        _require_str(fields, "platform", maximum=32)
        _require_str(fields, "python", maximum=32)
        _optional_float(fields, "sampleRate", minimum=1, maximum=384_000)
    elif frame_type == "load":
        profile = _require_str(fields, "profile", maximum=32)
        if profile not in PROFILE_KEYS:
            raise ProtocolError(f"unsupported engine profile: {profile}")
        _require_str(fields, "modelDir", maximum=4096)
        _require_str(fields, "sharedDir", maximum=4096)
        device = _require_str(fields, "device", maximum=16)
        if device not in DEVICES:
            raise ProtocolError(f"unsupported device: {device}")
        _require_str(fields, "dtype", maximum=16)
        _require_str(fields, "attention", maximum=32)
    elif frame_type == "capabilities":
        speakers = fields.get("speakers")
        languages = fields.get("languages")
        if not isinstance(speakers, list) or not all(isinstance(item, str) for item in speakers):
            raise ProtocolError("capabilities frame needs a speaker list")
        if not isinstance(languages, list) or not all(isinstance(item, str) for item in languages):
            raise ProtocolError("capabilities frame needs a language list")
        _require_bool(fields, "supportsClone")
        _require_int(fields, "sampleRate", minimum=1)
    elif frame_type == "synthesize":
        _require_str(fields, "text", maximum=MAX_TEXT_CHARS)
        _require_str(fields, "language", maximum=32)
        _validate_speaking_fields(fields)
    elif frame_type == "synthesize_batch":
        texts = fields.get("texts")
        if not isinstance(texts, list) or not 1 <= len(texts) <= MAX_BATCH_SEGMENTS:
            raise ProtocolError(
                f"frame field 'texts' must be a list of 1..{MAX_BATCH_SEGMENTS} strings"
            )
        for item in texts:
            if not isinstance(item, str) or not item or len(item) > MAX_TEXT_CHARS:
                raise ProtocolError(
                    "frame field 'texts' entries must be non-empty strings of at most "
                    f"{MAX_TEXT_CHARS} characters"
                )
        total = sum(len(item) for item in texts)
        if total > MAX_BATCH_CHARS:
            raise ProtocolError(
                f"frame field 'texts' must carry at most {MAX_BATCH_CHARS} characters in "
                f"total, got {total} — split the batch across jobs"
            )
        _require_str(fields, "language", maximum=32)
        _validate_speaking_fields(fields)
    elif frame_type == "pcm":
        _require_int(fields, "sampleRate", minimum=1)
        _require_int(fields, "seq", minimum=0)
        _require_bool(fields, "final")
        if "segment" in fields:
            _require_int(fields, "segment", minimum=0)
    elif frame_type == "progress":
        _optional_float(fields, "fraction", minimum=0.0, maximum=1.0)
        if "stage" in fields and not isinstance(fields["stage"], str):
            raise ProtocolError("frame field 'stage' must be a string")
    elif frame_type == "terminal":
        status = _require_str(fields, "status", maximum=16)
        if status not in TERMINAL_STATUSES:
            raise ProtocolError(f"unsupported terminal status: {status}")
        if "frames" in fields:
            _require_int(fields, "frames", minimum=0)
        _optional_float(fields, "audioSeconds", minimum=0.0, maximum=86_400.0)
        if "error" in fields and not isinstance(fields["error"], str):
            raise ProtocolError("frame field 'error' must be a string")
    elif frame_type == "error":
        _require_str(fields, "code", maximum=64)
        _require_str(fields, "message", maximum=MAX_MESSAGE_CHARS)
        if "fatal" in fields:
            _require_bool(fields, "fatal")
    elif frame_type == "cancel" or frame_type == "shutdown":
        if "reason" in fields and not isinstance(fields["reason"], str):
            raise ProtocolError("frame field 'reason' must be a string")


def validate_frame(frame: Frame) -> Frame:
    """Validate a frame in isolation: version, type, job tag, fields, payload."""
    if frame.type not in FRAME_TYPES:
        raise ProtocolError(f"unknown frame type: {frame.type!r}")
    if frame.type in JOB_FRAMES:
        validate_job_id(frame.job)
    elif frame.type in UNJOBED_FRAMES and frame.job:
        raise ProtocolError(f"{frame.type} frame must not carry a job id")
    elif frame.type in OPTIONAL_JOB_FRAMES and frame.job:
        validate_job_id(frame.job)

    if frame.payload:
        if frame.type != "pcm":
            raise ProtocolError(f"{frame.type} frame must not carry a payload")
        if len(frame.payload) % _SAMPLE.size:
            raise ProtocolError("pcm payload must contain whole float32 samples")
        if len(frame.payload) > MAX_PAYLOAD_BYTES:
            raise FrameTooLargeError("pcm payload exceeds the protocol bound")

    _validate_header_fields(frame.type, frame.fields)
    return frame


def pcm_to_bytes(samples: Sequence[float]) -> bytes:
    """Encode float32 samples for a ``pcm`` frame payload."""
    return b"".join(_SAMPLE.pack(float(sample)) for sample in samples)


def pcm_from_bytes(payload: bytes) -> tuple[float, ...]:
    """Decode a ``pcm`` frame payload into float32 samples."""
    if len(payload) % _SAMPLE.size:
        raise ProtocolError("pcm payload must contain whole float32 samples")
    return tuple(_SAMPLE.unpack_from(payload, offset)[0] for offset in range(0, len(payload), 4))


def encode_frame(frame: Frame, *, validate: bool = True) -> bytes:
    """Serialise a frame, header first, with both lengths prefixed."""
    if validate:
        validate_frame(frame)
    header = json.dumps(frame.header(), separators=(",", ":"), sort_keys=True).encode("utf-8")
    if len(header) > MAX_HEADER_BYTES:
        raise FrameTooLargeError("header exceeds the protocol bound")
    if len(frame.payload) > MAX_PAYLOAD_BYTES:
        raise FrameTooLargeError("payload exceeds the protocol bound")
    return b"".join(
        (
            _HEADER_LENGTH.pack(len(header)),
            header,
            _PAYLOAD_LENGTH.pack(len(frame.payload)),
            frame.payload,
        )
    )


def _read_exact(stream: IO[bytes], count: int) -> bytes:
    chunks: list[bytes] = []
    remaining = count
    while remaining:
        chunk = stream.read(remaining)
        if not chunk:
            if remaining == count:
                raise EndOfStream("stream ended at a frame boundary")
            raise ProtocolError("stream ended inside a frame")
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)


def read_frame(stream: IO[bytes]) -> Frame:
    """Read one frame, tolerating short reads and rejecting oversized ones."""
    header_length = _HEADER_LENGTH.unpack(_read_exact(stream, _HEADER_LENGTH.size))[0]
    if header_length == 0 or header_length > MAX_HEADER_BYTES:
        raise FrameTooLargeError(f"declared header length is out of range: {header_length}")
    raw_header = _read_exact(stream, header_length)
    payload_length = _PAYLOAD_LENGTH.unpack(_read_exact(stream, _PAYLOAD_LENGTH.size))[0]
    if payload_length > MAX_PAYLOAD_BYTES:
        raise FrameTooLargeError(f"declared payload length is out of range: {payload_length}")
    payload = _read_exact(stream, payload_length) if payload_length else b""
    try:
        header = json.loads(raw_header.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ProtocolError("frame header is not valid JSON") from exc
    if not isinstance(header, Mapping):
        raise ProtocolError("frame header must be a JSON object")
    version = header.get("version")
    if version != PROTOCOL_VERSION:
        raise ProtocolError(f"unsupported protocol version: {version!r}")
    frame_type = header.get("type")
    if not isinstance(frame_type, str):
        raise ProtocolError("frame header has no type")
    job = header.get("job", "")
    if job is not None and not isinstance(job, str):
        raise ProtocolError("frame header job id must be a string")
    fields = {key: value for key, value in header.items() if key not in {"version", "type", "job"}}
    return validate_frame(Frame(type=frame_type, job=job or "", payload=payload, fields=fields))


def write_frame(stream: IO[bytes], frame: Frame) -> None:
    """Write one frame and flush it, so the peer never waits on a buffer."""
    stream.write(encode_frame(frame))
    flush = getattr(stream, "flush", None)
    if callable(flush):
        flush()


Role = Literal["parent", "host"]


class SessionState:
    """Track per-job frame ordering for one end of the pipe.

    The host refuses to synthesize twice for one job and the parent drops frames
    for a job that already settled, which is what makes late host output
    harmless instead of corrupting the next job.
    """

    def __init__(self, role: Role) -> None:
        self.role: Role = role
        self.loaded_profile = ""
        self.active: set[str] = set()
        self.settled: set[str] = set()

    def accept(self, frame: Frame) -> None:
        """Validate a received frame against the session, raising on violation."""
        validate_frame(frame)
        if frame.job and frame.job in self.settled:
            raise StaleFrameError(f"frame for settled job: {frame.job}")
        self._transition(frame, sent=False)

    def record_sent(self, frame: Frame) -> None:
        """Register an outgoing frame, so the peer's answers can be validated."""
        validate_frame(frame)
        self._transition(frame, sent=True)

    def _transition(self, frame: Frame, *, sent: bool) -> None:
        if self.role == "parent":
            handler = self._parent_sent if sent else self._parent_received
        else:
            handler = self._host_sent if sent else self._host_received
        handler(frame)
        if frame.type == "terminal":
            self.active.discard(frame.job)
            self.settled.add(frame.job)

    def _parent_sent(self, frame: Frame) -> None:
        if frame.type == "load":
            if self.active:
                raise ProtocolError("cannot load a profile while a job is running")
            self.loaded_profile = str(frame.fields["profile"])
        elif frame.type in ("synthesize", "synthesize_batch"):
            if not self.loaded_profile:
                raise ProtocolError("synthesize sent before load")
            if self.active:
                raise ProtocolError("the host runs one job at a time")
            self.active.add(frame.job)
        elif frame.type == "cancel":
            if frame.job not in self.active:
                raise ProtocolError(f"cancel for a job that is not running: {frame.job}")
        elif frame.type == "shutdown":
            if self.active:
                raise ProtocolError("cannot shut down while a job is running")
        else:
            raise ProtocolError(f"frame {frame.type} is not a parent command")

    def _parent_received(self, frame: Frame) -> None:
        if frame.type in {"pcm", "progress", "terminal"}:
            if frame.job not in self.active:
                raise ProtocolError(f"{frame.type} for a job that is not running: {frame.job}")
        elif frame.type == "capabilities":
            if self.active:
                raise ProtocolError("capabilities arrived while a job is running")
        elif frame.type in {"hello", "error"}:
            return
        else:
            raise ProtocolError(f"frame {frame.type} is not a host frame")

    def _host_received(self, frame: Frame) -> None:
        if frame.type == "load":
            if self.active:
                raise ProtocolError("cannot load a profile while a job is running")
            self.loaded_profile = str(frame.fields["profile"])
        elif frame.type in ("synthesize", "synthesize_batch"):
            if not self.loaded_profile:
                raise ProtocolError("synthesize arrived before load")
            if self.active:
                raise ProtocolError("the host runs one job at a time")
            self.active.add(frame.job)
        elif frame.type == "cancel":
            if frame.job not in self.active:
                raise ProtocolError(f"cancel for a job that is not running: {frame.job}")
        elif frame.type == "shutdown":
            if self.active:
                raise ProtocolError("cannot shut down while a job is running")
        else:
            raise ProtocolError(f"frame {frame.type} is not a parent command")

    def _host_sent(self, frame: Frame) -> None:
        if frame.type in {"pcm", "progress", "terminal"}:
            if frame.job not in self.active:
                raise ProtocolError(f"{frame.type} for a job that is not running: {frame.job}")
        elif frame.type == "capabilities":
            if self.active:
                raise ProtocolError("capabilities sent while a job is running")
        elif frame.type in {"hello", "error"}:
            return
        else:
            raise ProtocolError(f"frame {frame.type} is not a host frame")
