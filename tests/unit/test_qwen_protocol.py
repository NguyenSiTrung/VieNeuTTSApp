"""Framed Qwen IPC protocol: framing, validation, and job transitions."""

from __future__ import annotations

import io
import json
import struct

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


def test_partial_reads_reassemble_one_frame() -> None:
    stream = io.BytesIO()
    qp.write_frame(stream, VALID_FRAMES["pcm"])

    decoded = qp.read_frame(OneByteStream(stream.getvalue()))

    assert decoded.type == "pcm"
    assert qp.pcm_from_bytes(decoded.payload) == pytest.approx((0.5, -0.25, 1.0))


def test_pcm_payload_round_trips_float32_samples() -> None:
    samples = (0.0, 0.5, -0.5, 1.0, -1.0, 1e-6)

    decoded = qp.pcm_from_bytes(qp.pcm_to_bytes(samples))

    assert decoded == pytest.approx(samples, abs=1e-9)
    with pytest.raises(qp.ProtocolError, match="whole float32"):
        qp.pcm_from_bytes(b"\x00\x01\x02")


def test_write_frame_flushes_so_the_peer_never_waits() -> None:
    class Recording(io.BytesIO):
        flushed = False

        def flush(self) -> None:
            self.flushed = True

    stream = Recording()
    qp.write_frame(stream, VALID_FRAMES["progress"])

    assert stream.flushed is True


def test_declared_lengths_beyond_the_protocol_bound_are_rejected() -> None:
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


def test_malformed_headers_are_rejected() -> None:
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


def test_stream_end_is_distinguished_from_truncation() -> None:
    with pytest.raises(qp.EndOfStream):
        qp.read_frame(io.BytesIO(b""))

    stream = io.BytesIO()
    qp.write_frame(stream, VALID_FRAMES["progress"])
    truncated = stream.getvalue()[:-2]
    with pytest.raises(qp.ProtocolError, match="inside a frame"):
        qp.read_frame(io.BytesIO(truncated))


def test_job_tagging_rules() -> None:
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


def test_field_validation_catches_bad_values() -> None:
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


def test_payload_is_only_allowed_on_pcm_frames() -> None:
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


def test_host_rejects_invalid_command_transitions() -> None:
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


def test_parent_drops_stale_frames_and_enforces_host_frames() -> None:
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

    def test_a_valid_batch_round_trips_through_a_stream(self) -> None:
        frame = _batch_frame(["第一句。", "第二句。"], speaker="Ryan")
        stream = io.BytesIO()
        qp.write_frame(stream, frame)
        got = qp.read_frame(io.BytesIO(stream.getvalue()))
        assert got.type == "synthesize_batch"
        assert got.fields["texts"] == ["第一句。", "第二句。"]

    def test_the_batch_size_is_bounded(self) -> None:
        with pytest.raises(qp.ProtocolError, match="texts"):
            qp.validate_frame(_batch_frame([]))
        with pytest.raises(qp.ProtocolError, match="texts"):
            qp.validate_frame(_batch_frame(["x"] * (qp.MAX_BATCH_SEGMENTS + 1)))

    def test_each_text_keeps_the_single_segment_bound(self) -> None:
        with pytest.raises(qp.ProtocolError, match="texts"):
            qp.validate_frame(_batch_frame(["x" * (qp.MAX_TEXT_CHARS + 1)]))
        with pytest.raises(qp.ProtocolError, match="texts"):
            qp.validate_frame(_batch_frame(["ok.", ""]))

    def test_the_batch_text_total_is_bounded(self) -> None:
        # A valid count can still be an oversized batch: the segments generate
        # together, and total text past MAX_BATCH_CHARS exhausted a 16 GB Mac
        # mini (the kernel OOM-killed the host mid-generate).
        with pytest.raises(qp.ProtocolError, match="total"):
            qp.validate_frame(_batch_frame(["x" * qp.MAX_TEXT_CHARS] * 2))
        assert qp.validate_frame(_batch_frame(["x" * qp.MAX_TEXT_CHARS]))
        half = qp.MAX_BATCH_CHARS // 2
        assert qp.validate_frame(_batch_frame(["x" * half, "y" * half]))

    def test_a_batch_requires_a_job_id(self) -> None:
        with pytest.raises(qp.ProtocolError, match="missing its job id"):
            qp.validate_frame(
                qp.Frame("synthesize_batch", fields={"texts": ["a"], "language": "zh"})
            )

    def test_pcm_frames_may_tag_their_segment(self) -> None:
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

    def test_a_batch_runs_under_the_one_job_at_a_time_rule(self) -> None:
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
