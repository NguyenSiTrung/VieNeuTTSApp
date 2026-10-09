"""Framed Qwen IPC protocol: framing, validation, and job transitions."""

from __future__ import annotations

import io
import json
import struct
import time

import numpy as np
import pytest

from vienetts_app.core import qwen_protocol as qp

VALID_FRAMES = {
    "hello": qp.Frame(
        "hello", fields={"host": "vienetts-qwen-host", "platform": "linux", "python": "3.13"}
    ),
    "load": qp.Frame(
        "load",
        fields={
            "profile": "customvoice",
            "modelDir": "/models/customvoice",
            "sharedDir": "/models/shared",
            "device": "cuda",
            "dtype": "bfloat16",
            "attention": "sdpa",
        },
    ),
    "capabilities": qp.Frame(
        "capabilities",
        fields={
            "speakers": ["Ryan"],
            "languages": ["vi", "en"],
            "supportsClone": False,
            "sampleRate": 24000,
        },
    ),
    "synthesize": qp.Frame(
        "synthesize",
        job="job-1",
        fields={"text": "xin chào", "language": "vi", "speaker": "Ryan"},
    ),
    "pcm": qp.Frame(
        "pcm",
        job="job-1",
        payload=qp.pcm_to_bytes([0.5, -0.25, 1.0]),
        fields={"sampleRate": 48000, "seq": 0, "final": False},
    ),
    "progress": qp.Frame("progress", job="job-1", fields={"fraction": 0.5, "stage": "generating"}),
    "cancel": qp.Frame("cancel", job="job-1", fields={"reason": "user"}),
    "terminal": qp.Frame("terminal", job="job-1", fields={"status": "ok", "frames": 3}),
    "error": qp.Frame("error", job="job-1", fields={"code": "oom", "message": "out of memory"}),
    "shutdown": qp.Frame("shutdown", fields={"reason": "app closing"}),
}


class OneByteStream(io.RawIOBase):
    """A reader that returns at most one byte per call (partial-read torture)."""

    def __init__(self, data: bytes) -> None:
        self._buffer = io.BytesIO(data)

    def readable(self) -> bool:
        return True

    def read(self, size: int = -1) -> bytes:
        return self._buffer.read(1 if size < 0 else min(size, 1))


@pytest.mark.parametrize("frame_type", sorted(VALID_FRAMES))
def test_frames_round_trip_through_a_stream(frame_type: str) -> None:
    frame = VALID_FRAMES[frame_type]
    stream = io.BytesIO()

    qp.write_frame(stream, frame)
    stream.seek(0)
    decoded = qp.read_frame(stream)

    assert decoded.type == frame.type
    assert decoded.job == frame.job
    assert decoded.fields == frame.fields
    assert decoded.payload == frame.payload


def test_frame_round_trips() -> None:
    stream = io.BytesIO()
    qp.write_frame(stream, VALID_FRAMES["pcm"])

    decoded = qp.read_frame(OneByteStream(stream.getvalue()))

    assert decoded.type == "pcm"
    assert qp.pcm_from_bytes(decoded.payload) == pytest.approx((0.5, -0.25, 1.0))

    samples = (0.0, 0.5, -0.5, 1.0, -1.0, 1e-6)

    decoded = qp.pcm_from_bytes(qp.pcm_to_bytes(samples))

    assert decoded == pytest.approx(samples, abs=1e-9)
    with pytest.raises(qp.ProtocolError, match="whole float32"):
        qp.pcm_from_bytes(b"\x00\x01\x02")


_STRUCT_SAMPLE = struct.Struct("<f")


def _struct_encode(samples) -> bytes:
    """The pre-vectorization encoder: the byte-exactness oracle."""
    return b"".join(_STRUCT_SAMPLE.pack(float(sample)) for sample in samples)


def _struct_decode(payload: bytes) -> tuple[float, ...]:
    """The pre-vectorization decoder: the value-exactness oracle."""
    return tuple(_STRUCT_SAMPLE.unpack_from(payload, at)[0] for at in range(0, len(payload), 4))


def test_pcm_codec_is_vectorized_and_identical_to_the_struct_codec() -> None:
    rng = np.random.default_rng(7)
    samples = rng.uniform(-1.5, 1.5, 4_097).astype(np.float32)
    payload = _struct_encode(samples)

    decoded = qp.pcm_from_bytes(payload)

    assert isinstance(decoded, np.ndarray)
    assert decoded.dtype == np.float32
    assert decoded.flags.writeable  # a copy, not a view into the frame payload
    assert tuple(float(x) for x in decoded) == _struct_decode(payload)
    # Encoding accepts arrays and plain sequences, byte-for-byte as before.
    assert qp.pcm_to_bytes(samples) == payload
    assert qp.pcm_to_bytes([0.5, -0.25, 1.0, 1e-6]) == _struct_encode([0.5, -0.25, 1.0, 1e-6])
    assert qp.pcm_to_bytes([]) == b""
    assert qp.pcm_from_bytes(b"").shape == (0,)


@pytest.mark.benchmark
def test_pcm_decode_is_at_least_100x_faster_than_the_struct_codec() -> None:
    payload = np.linspace(-1, 1, 24_000, dtype="<f4").tobytes()  # one 0.5 s frame

    def best(fn, repeats: int) -> float:
        timings = []
        for _ in range(repeats):
            start = time.perf_counter()
            fn(payload)
            timings.append(time.perf_counter() - start)
        return min(timings)

    baseline = best(_struct_decode, 5)
    vectorized = best(qp.pcm_from_bytes, 50)

    assert baseline / vectorized >= 100, (baseline, vectorized)

    class Recording(io.BytesIO):
        flushed = False

        def flush(self) -> None:
            self.flushed = True

    stream = Recording()
    qp.write_frame(stream, VALID_FRAMES["progress"])

    assert stream.flushed is True


def test_frame_rejections() -> None:
    huge_header = struct.pack(">I", qp.MAX_HEADER_BYTES + 1)
    with pytest.raises(qp.FrameTooLargeError, match="header length"):
        qp.read_frame(io.BytesIO(huge_header))

    header = json.dumps({"version": 1, "type": "pcm", "job": "j"}).encode()
    declared = struct.pack(">I", len(header)) + header + struct.pack(">I", qp.MAX_PAYLOAD_BYTES + 4)
    with pytest.raises(qp.FrameTooLargeError, match="payload length"):
        qp.read_frame(io.BytesIO(declared))

    oversized = qp.Frame(
        "pcm",
        job="j",
        payload=b"\x00" * (qp.MAX_PAYLOAD_BYTES + 4),
        fields={"sampleRate": 48000, "seq": 0, "final": False},
    )
    with pytest.raises(qp.FrameTooLargeError, match="payload"):
        qp.encode_frame(oversized)

    def framed(header: bytes, payload: bytes = b"") -> io.BytesIO:
        return io.BytesIO(
            struct.pack(">I", len(header)) + header + struct.pack(">I", len(payload)) + payload
        )

    with pytest.raises(qp.ProtocolError, match="not valid JSON"):
        qp.read_frame(framed(b"{not json"))
    with pytest.raises(qp.ProtocolError, match="JSON object"):
        qp.read_frame(framed(b"[1, 2, 3]"))
    with pytest.raises(qp.ProtocolError, match="unsupported protocol version"):
        qp.read_frame(framed(json.dumps({"version": 99, "type": "hello"}).encode()))
    with pytest.raises(qp.ProtocolError, match="no type"):
        qp.read_frame(framed(json.dumps({"version": 1}).encode()))
    with pytest.raises(qp.ProtocolError, match="unknown frame type"):
        qp.read_frame(framed(json.dumps({"version": 1, "type": "explode"}).encode()))
    with pytest.raises(qp.ProtocolError, match="job id must be a string"):
        qp.read_frame(framed(json.dumps({"version": 1, "type": "pcm", "job": 7}).encode()))
    with pytest.raises(qp.ProtocolError, match="not valid JSON"):
        qp.read_frame(framed(b"\xff\xfe"))

    with pytest.raises(qp.EndOfStream):
        qp.read_frame(io.BytesIO(b""))

    stream = io.BytesIO()
    qp.write_frame(stream, VALID_FRAMES["progress"])
    truncated = stream.getvalue()[:-2]
    with pytest.raises(qp.ProtocolError, match="inside a frame"):
        qp.read_frame(io.BytesIO(truncated))


def test_field_validation() -> None:
    with pytest.raises(qp.ProtocolError, match="missing its job id"):
        qp.validate_frame(
            qp.Frame(
                "pcm",
                payload=b"\x00\x00\x00\x00",
                fields={"sampleRate": 48000, "seq": 0, "final": True},
            )
        )
    with pytest.raises(qp.ProtocolError, match="must not carry a job id"):
        qp.validate_frame(
            qp.Frame("hello", job="j", fields={"host": "h", "platform": "linux", "python": "3.13"})
        )
    with pytest.raises(qp.ProtocolError, match="unsupported characters"):
        qp.validate_frame(qp.Frame("cancel", job="bad job!", fields={}))
    with pytest.raises(qp.ProtocolError, match="exceeds 64"):
        qp.validate_frame(qp.Frame("cancel", job="x" * 65, fields={}))

    with pytest.raises(qp.ProtocolError, match="unsupported engine profile"):
        qp.validate_frame(
            qp.Frame(
                "load",
                fields={
                    "profile": "whisper",
                    "modelDir": "/m",
                    "sharedDir": "/s",
                    "device": "cpu",
                    "dtype": "float32",
                    "attention": "sdpa",
                },
            )
        )
    with pytest.raises(qp.ProtocolError, match="unsupported device"):
        qp.validate_frame(
            qp.Frame(
                "load",
                fields={
                    "profile": "base",
                    "modelDir": "/m",
                    "sharedDir": "/s",
                    "device": "tpu",
                    "dtype": "float32",
                    "attention": "sdpa",
                },
            )
        )
    with pytest.raises(qp.ProtocolError, match="non-empty string"):
        qp.validate_frame(qp.Frame("synthesize", job="j", fields={"text": "", "language": "vi"}))
    with pytest.raises(qp.ProtocolError, match="fraction"):
        qp.validate_frame(qp.Frame("progress", job="j", fields={"fraction": 1.5}))
    with pytest.raises(qp.ProtocolError, match="unsupported terminal status"):
        qp.validate_frame(qp.Frame("terminal", job="j", fields={"status": "maybe"}))
    with pytest.raises(qp.ProtocolError, match="speaker list"):
        qp.validate_frame(
            qp.Frame(
                "capabilities",
                fields={
                    "speakers": "Ryan",
                    "languages": ["vi"],
                    "supportsClone": True,
                    "sampleRate": 24000,
                },
            )
        )
    with pytest.raises(qp.ProtocolError, match="boolean"):
        qp.validate_frame(
            qp.Frame(
                "pcm",
                job="j",
                payload=b"\x00\x00\x00\x00",
                fields={"sampleRate": 48000, "seq": 0, "final": "no"},
            )
        )

    with pytest.raises(qp.ProtocolError, match="must not carry a payload"):
        qp.validate_frame(qp.Frame("progress", job="j", payload=b"\x00\x00\x00\x00", fields={}))
    with pytest.raises(qp.ProtocolError, match="whole float32"):
        qp.validate_frame(
            qp.Frame(
                "pcm",
                job="j",
                payload=b"\x00\x00\x00",
                fields={"sampleRate": 48000, "seq": 0, "final": False},
            )
        )


def test_session_transitions() -> None:
    session = qp.SessionState("host")

    with pytest.raises(qp.ProtocolError, match="before load"):
        session.accept(VALID_FRAMES["synthesize"])

    session.accept(VALID_FRAMES["load"])
    session.accept(VALID_FRAMES["synthesize"])

    with pytest.raises(qp.ProtocolError, match="one job at a time"):
        session.accept(VALID_FRAMES["synthesize"])
    with pytest.raises(qp.ProtocolError, match="not running"):
        session.accept(qp.Frame("cancel", job="other", fields={}))
    with pytest.raises(qp.ProtocolError, match="while a job is running"):
        session.accept(VALID_FRAMES["shutdown"])
    with pytest.raises(qp.ProtocolError, match="not a parent command"):
        session.accept(VALID_FRAMES["pcm"])

    session.accept(VALID_FRAMES["cancel"])
    assert session.active == {"job-1"}

    session = qp.SessionState("parent")

    with pytest.raises(qp.ProtocolError, match="not running"):
        session.accept(VALID_FRAMES["pcm"])

    session.accept(VALID_FRAMES["hello"])
    session.record_sent(VALID_FRAMES["load"])
    session.record_sent(
        qp.Frame("synthesize", job="job-1", fields={"text": "hi", "language": "vi"})
    )
    session.accept(VALID_FRAMES["pcm"])
    session.accept(VALID_FRAMES["progress"])
    session.accept(VALID_FRAMES["terminal"])

    assert session.settled == {"job-1"}
    with pytest.raises(qp.StaleFrameError, match="settled job"):
        session.accept(VALID_FRAMES["pcm"])
    with pytest.raises(qp.ProtocolError, match="not a host frame"):
        session.accept(VALID_FRAMES["load"])
    with pytest.raises(qp.ProtocolError, match="not a parent command"):
        session.record_sent(VALID_FRAMES["pcm"])


def _batch_frame(texts: list[str], **fields: object) -> qp.Frame:
    payload: dict[str, object] = {"texts": texts, "language": "zh"}
    payload.update(fields)
    return qp.Frame("synthesize_batch", job="job-1", fields=payload)


class TestBatchSynthesisFrames:
    """``synthesize_batch``: several segments in one host job, tagged output."""

    def test_batch_bounds(self) -> None:
        frame = _batch_frame(["第一句。", "第二句。"], speaker="Ryan")
        stream = io.BytesIO()
        qp.write_frame(stream, frame)
        got = qp.read_frame(io.BytesIO(stream.getvalue()))
        assert got.type == "synthesize_batch"
        assert got.fields["texts"] == ["第一句。", "第二句。"]

        with pytest.raises(qp.ProtocolError, match="texts"):
            qp.validate_frame(_batch_frame([]))
        with pytest.raises(qp.ProtocolError, match="texts"):
            qp.validate_frame(_batch_frame(["x"] * (qp.MAX_BATCH_SEGMENTS + 1)))

        with pytest.raises(qp.ProtocolError, match="texts"):
            qp.validate_frame(_batch_frame(["x" * (qp.MAX_TEXT_CHARS + 1)]))
        with pytest.raises(qp.ProtocolError, match="texts"):
            qp.validate_frame(_batch_frame(["ok.", ""]))

        # A valid count can still be an oversized batch: the segments generate
        # together, and total text past MAX_BATCH_CHARS exhausted a 16 GB Mac
        # mini (the kernel OOM-killed the host mid-generate).
        with pytest.raises(qp.ProtocolError, match="total"):
            qp.validate_frame(_batch_frame(["x" * qp.MAX_TEXT_CHARS] * 2))
        assert qp.validate_frame(_batch_frame(["x" * qp.MAX_TEXT_CHARS]))
        half = qp.MAX_BATCH_CHARS // 2
        assert qp.validate_frame(_batch_frame(["x" * half, "y" * half]))

    def test_batch_jobs_and_tags(self) -> None:
        with pytest.raises(qp.ProtocolError, match="missing its job id"):
            qp.validate_frame(
                qp.Frame("synthesize_batch", fields={"texts": ["a"], "language": "zh"})
            )

        frame = qp.Frame(
            "pcm",
            job="job-1",
            payload=qp.pcm_to_bytes([0.5]),
            fields={"sampleRate": 48000, "seq": 0, "final": True, "segment": 3},
        )
        assert qp.validate_frame(frame).get("segment") == 3
        with pytest.raises(qp.ProtocolError, match="segment"):
            qp.validate_frame(
                qp.Frame(
                    "pcm",
                    job="job-1",
                    payload=qp.pcm_to_bytes([0.5]),
                    fields={"sampleRate": 48000, "seq": 0, "final": True, "segment": -1},
                )
            )

        session = qp.SessionState("host")
        session.accept(VALID_FRAMES["load"])
        session.accept(_batch_frame(["a"]))
        with pytest.raises(qp.ProtocolError, match="one job at a time"):
            session.accept(VALID_FRAMES["synthesize"])


def test_loading_a_profile_is_refused_while_a_job_runs() -> None:
    session = qp.SessionState("host")
    session.accept(VALID_FRAMES["load"])
    session.accept(VALID_FRAMES["synthesize"])
    with pytest.raises(qp.ProtocolError, match="while a job is running"):
        session.accept(VALID_FRAMES["load"])


# --------------------------------------------------------------------------- #
# GGUF load frames (track qwen_gguf_engine_20260923)
# --------------------------------------------------------------------------- #


def _gguf_load(**overrides: object) -> qp.Frame:
    fields: dict[str, object] = {
        "profile": "base",
        "format": "gguf",
        "quantization": "Q8_0",
        "device": "cpu",
        "runtimeDir": "/runtime/linux-x64-cpu",
        "talkerPath": "/models/base-Q8_0/talker.gguf",
        "codecPath": "/models/shared/codec.gguf",
    }
    fields.update(overrides)
    return qp.Frame("load", fields=fields)


class TestGgufLoadFrames:
    def test_gguf_field_contract(self) -> None:
        frame = _gguf_load()
        assert qp.validate_frame(frame).fields["format"] == "gguf"

        assert qp.validate_frame(_gguf_load(device="metal")).fields["device"] == "metal"
        # The official vocabulary's "mps" is not a GGUF device.
        with pytest.raises(qp.ProtocolError, match="device"):
            qp.validate_frame(_gguf_load(device="mps"))
        with pytest.raises(qp.ProtocolError, match="device"):
            qp.validate_frame(_gguf_load(device="auto"))

        for missing in ("quantization", "runtimeDir", "talkerPath", "codecPath"):
            with pytest.raises(qp.ProtocolError):
                frame = _gguf_load()
                del frame.fields[missing]
                qp.validate_frame(frame)

    def test_gguf_validation(self) -> None:
        qp.validate_frame(_gguf_load(quantization="Q4_K_M"))
        with pytest.raises(qp.ProtocolError, match="quantization"):
            qp.validate_frame(_gguf_load(quantization="BF16"))

        with pytest.raises(qp.ProtocolError, match="format"):
            qp.validate_frame(_gguf_load(format="onnx"))

        # format absent → official: the GGUF fields must not satisfy it.
        fields = {
            "profile": "base",
            "quantization": "Q8_0",
            "device": "cpu",
            "runtimeDir": "/r",
            "talkerPath": "/t",
            "codecPath": "/c",
        }
        with pytest.raises(qp.ProtocolError, match="modelDir"):
            qp.validate_frame(qp.Frame("load", fields=fields))
        # An explicit official format still rejects the native device name.
        fields = dict(VALID_FRAMES["load"].fields)
        fields["format"] = "official"
        fields["device"] = "metal"
        with pytest.raises(qp.ProtocolError, match="device"):
            qp.validate_frame(qp.Frame("load", fields=fields))

    def test_gguf_session(self) -> None:
        session = qp.SessionState("host")
        session.accept(_gguf_load())
        session.accept(VALID_FRAMES["synthesize"])
        with pytest.raises(qp.ProtocolError, match="one job at a time"):
            session.accept(_batch_frame(["a"]))

        fields = dict(VALID_FRAMES["synthesize"].fields)
        fields["useVoiceRef"] = True
        qp.validate_frame(qp.Frame("synthesize", job="job-1", fields=fields))
        fields["useVoiceRef"] = "yes"
        with pytest.raises(qp.ProtocolError, match="boolean"):
            qp.validate_frame(qp.Frame("synthesize", job="job-1", fields=fields))
