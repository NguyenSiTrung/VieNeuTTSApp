"""Parent-side Qwen host adapter: spawn, handshake, streaming, cancel, reap.

The host itself is exercised through a scripted stand-in (``FAKE_HOST_SOURCE``)
so every lifecycle path — hangs, crashes, malformed output, OOM, stale
deliveries, partial PCM — is deterministic and torch-free.
"""

from __future__ import annotations

import os
import signal
import sys
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from tests.unit import qwen_host_fake as host_fake

from vienetts_app.core.audio import write_wav_file
from vienetts_app.core.engine import EngineProviderError
from vienetts_app.core.engine_profiles import QWEN_BASE, QWEN_CUSTOM, VIENEU
from vienetts_app.core.models import VoiceOp
from vienetts_app.core.qwen_engine import (
    ENGINE_PROFILE_KEYS,
    RSS_RECYCLE_GROWTH_BYTES,
    ClonePrompt,
    QwenEngine,
    QwenEngineCancelled,
    QwenEngineError,
    QwenEngineProvider,
    _segment_batches,
    batch_bounds_for_ram,
    host_environment,
    host_footprint,
)
from vienetts_app.core.qwen_protocol import MAX_TEXT_CHARS, RUNTIME_INCOMPLETE_CODE
from vienetts_app.core.synthesis_context import SynthesisContext, context_for
from vienetts_app.core.voice_profiles import CloneStore, CloneStoreError
from vienetts_app.workers.qwen_host import PROFILE_ENGINES


def fake_host(tmp_path: Path, mode: str) -> list[str]:
    return host_fake.fake_host(tmp_path, mode)


def engine_for(tmp_path: Path, mode: str, **overrides: Any) -> QwenEngine:
    return host_fake.engine_for(tmp_path, mode, **overrides)


host_log = host_fake.host_log
received = host_fake.received
host_pid = host_fake.host_pid
pid_alive = host_fake.pid_alive
wait_for = host_fake.wait_for


class StreamRun:
    """Consume ``infer_stream`` on a thread so cancel/close can race it."""

    def __init__(self, engine: QwenEngine, job_id: str, text: str = "hello", **kwargs: Any) -> None:
        self.job_id = job_id
        self.chunks: list[np.ndarray] = []
        self.error: BaseException | None = None
        self._done = threading.Event()
        self._thread = threading.Thread(
            target=self._consume, args=(engine, text, kwargs), daemon=True
        )
        self._thread.start()

    def _consume(self, engine: QwenEngine, text: str, kwargs: dict[str, Any]) -> None:
        options: dict[str, Any] = {"language": "en", "speaker": "Ryan"}
        options.update(kwargs)
        try:
            for chunk in engine.infer_stream(text, job_id=self.job_id, **options):
                self.chunks.append(chunk)
        except BaseException as exc:  # noqa: BLE001 — recorded for the test to assert on
            self.error = exc
        finally:
            self._done.set()

    def wait(self, timeout: float = 5.0) -> bool:
        return self._done.wait(timeout)

    def assert_finished(self) -> None:
        assert self.wait(), "the stream never finished"
        self._thread.join(1.0)


@pytest.fixture()
def engines() -> Iterator[list[QwenEngine]]:
    created: list[QwenEngine] = []
    yield created
    for engine in created:
        engine.close()


def start_engine(
    engines: list[QwenEngine], tmp_path: Path, mode: str, **overrides: Any
) -> QwenEngine:
    engine = engine_for(tmp_path, mode, **overrides)
    engines.append(engine)
    return engine


# --------------------------------------------------------------------------- #
# command + environment
# --------------------------------------------------------------------------- #


class TestHostCommandAndEnvironment:
    def test_environment_contract(self, tmp_path: Path) -> None:
        runtime_dir = tmp_path / "runtime"
        base = {
            "PATH": "/usr/bin",
            "PYTHONPATH": "/tmp/attacker",
            "PYTHONHOME": "/tmp/attacker",
            "PYTHONSTARTUP": "/tmp/attacker.py",
            "HF_HUB_OFFLINE": "0",
            "HOME": "/root",
        }
        environment = host_environment(runtime_dir, base)
        assert environment["PATH"] == "/usr/bin"
        assert environment["HOME"] == "/root"
        assert environment["HF_HUB_OFFLINE"] == "1"
        assert environment["TRANSFORMERS_OFFLINE"] == "1"
        assert environment["PYTHONUNBUFFERED"] == "1"
        assert environment["PYTHONDONTWRITEBYTECODE"] == "1"
        assert environment["TOKENIZERS_PARALLELISM"] == "false"
        # An MPS op the runtime does not implement falls back to CPU instead of
        # killing the host (device errors are fatal by design).
        assert environment["PYTORCH_ENABLE_MPS_FALLBACK"] == "1"
        assert "PYTHONHOME" not in environment
        assert "PYTHONSTARTUP" not in environment
        entries = environment["PYTHONPATH"].split(os.pathsep)
        assert entries[0] == str(runtime_dir)
        assert "tmp/attacker" not in environment["PYTHONPATH"]
        assert any(entry.endswith("src") for entry in entries)
        assert base["PYTHONPATH"] == "/tmp/attacker"  # the input mapping is not mutated

        environment = host_environment(None, {"PATH": "/usr/bin"})
        assert any(entry.endswith("src") for entry in environment["PYTHONPATH"].split(os.pathsep))

        environment = host_environment(None, {"PYTORCH_ENABLE_MPS_FALLBACK": "0"})
        assert environment["PYTORCH_ENABLE_MPS_FALLBACK"] == "0"

        # perf 7.4: the app's default BLAS cap is for the VieNeu/ORT process; a
        # torch or ggml host keeps its own thread defaults. A user's own
        # variable still reaches it.
        from vienetts_app.core.performance import apply_blas_thread_cap

        base = {"MKL_NUM_THREADS": "6"}
        apply_blas_thread_cap(1, base)
        environment = host_environment(None, base)
        assert "OPENBLAS_NUM_THREADS" not in environment
        assert environment["MKL_NUM_THREADS"] == "6"
        assert not any(name.startswith("VIENETTS_APPLIED") for name in environment)


# --------------------------------------------------------------------------- #
# initialize
# --------------------------------------------------------------------------- #


class TestInitialize:
    def test_load_selection(self, tmp_path: Path, engines: list[QwenEngine]) -> None:
        engine = start_engine(
            engines,
            tmp_path,
            "ok",
            device="cuda",
            dtype="bfloat16",
            attention="flash_attention_2",
            runtime_dir=tmp_path / "runtime",
        )
        assert engine.is_initialized is False
        engine.initialize()
        assert engine.is_initialized is True
        capabilities = engine.capabilities()
        assert capabilities.profile == "qwen_custom_0_6b"
        assert capabilities.speakers == ("Ryan",)
        assert capabilities.languages == ("en",)
        assert capabilities.supports_clone is False
        assert capabilities.sample_rate == 48000
        (load,) = received(tmp_path, "load")
        assert load["fields"]["profile"] == "customvoice"
        assert load["fields"]["modelDir"].endswith("customvoice")
        assert load["fields"]["sharedDir"].endswith("shared")
        assert load["fields"]["device"] == "cuda"
        assert load["fields"]["dtype"] == "bfloat16"
        assert load["fields"]["attention"] == "flash_attention_2"
        engine.close()
        assert pid_alive(host_pid(tmp_path)) is False

        tmp_path = tmp_path / "case-b"

        engine = start_engine(engines, tmp_path, "ok", device="cuda")
        engine.initialize()
        (load,) = received(tmp_path, "load")
        assert load["fields"]["device"] == "cuda"
        # The locked matrix (qwen-runtime-compatibility.md §1) pins CUDA to
        # bfloat16: leaving the dtype unpinned must never fall back to float32.
        assert load["fields"]["dtype"] == "bfloat16"
        assert load["fields"]["attention"] == "sdpa"

        tmp_path = tmp_path / "case-b"

        engine = start_engine(
            engines, tmp_path, "ok", device="cpu", dtype="float16", attention="eager"
        )
        engine.initialize()
        (load,) = received(tmp_path, "load")
        assert load["fields"]["dtype"] == "float16"
        assert load["fields"]["attention"] == "eager"

    def test_pre_spawn_rejections(self, tmp_path: Path, engines: list[QwenEngine]) -> None:
        engine = start_engine(engines, tmp_path, "ok")
        with pytest.raises(QwenEngineError, match="not initialized"):
            engine.capabilities()

        tmp_path = tmp_path / "case-b"

        with pytest.raises(QwenEngineError, match="profile"):
            engine_for(tmp_path, "ok", profile="vieneu")
        assert host_log(tmp_path) == []  # nothing was spawned

        tmp_path = tmp_path / "case-b"

        with pytest.raises(QwenEngineError, match="unknown engine profile"):
            engine_for(tmp_path, "ok", profile="not_a_profile")
        assert host_log(tmp_path) == []

    def test_the_engine_and_provider_report_the_pytorch_engine_id(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        # The routing pin (Task 4.2): provider_for matches a context's stamped
        # engine to this id, so a GGUF job can never land on the PyTorch host.
        engine = start_engine(engines, tmp_path, "ok")
        assert engine.engine_id == "pytorch"
        assert QwenEngineProvider(engine).engine == "pytorch"

    def test_spawn_failures(self, tmp_path: Path, engines: list[QwenEngine]) -> None:
        engine = start_engine(engines, tmp_path, "ok", command=[str(tmp_path / "no-such-python")])
        with pytest.raises(QwenEngineError, match="could not start the Qwen model host"):
            engine.initialize()
        assert engine.is_initialized is False

        tmp_path = tmp_path / "case-b"

        engine = start_engine(engines, tmp_path, "silent", handshake_timeout=0.15)
        with pytest.raises(QwenEngineError, match="hello"):
            engine.initialize()
        assert engine.is_initialized is False
        wait_for(lambda: not pid_alive(host_pid(tmp_path)), what="the host to be reaped")

        tmp_path = tmp_path / "case-b"

        engine = start_engine(engines, tmp_path, "wrong_handshake")
        with pytest.raises(QwenEngineError, match="before its handshake"):
            engine.initialize()
        assert engine.is_initialized is False
        wait_for(lambda: not pid_alive(host_pid(tmp_path)), what="the foreign host to be reaped")

    def test_load_failures(self, tmp_path: Path, engines: list[QwenEngine]) -> None:
        engine = start_engine(engines, tmp_path, "load_error")
        with pytest.raises(QwenEngineError, match="model directory is missing"):
            engine.initialize()
        assert engine.is_initialized is False
        wait_for(lambda: not pid_alive(host_pid(tmp_path)), what="the host to be reaped")

        tmp_path = tmp_path / "case-b"

        """Only THIS code is fixed from Settings: the UI must see it, not a string."""
        engine = start_engine(engines, tmp_path, "runtime_incomplete")
        with pytest.raises(QwenEngineError, match="module 'sox' is missing"):
            engine.initialize()
        assert engine.last_error_code() == RUNTIME_INCOMPLETE_CODE
        assert "Settings" in engine.last_error_message()

        tmp_path = tmp_path / "case-b"

        """The distinction the recovery path depends on: a bad tree is not a bad runtime."""
        engine = start_engine(engines, tmp_path, "load_error")
        with pytest.raises(QwenEngineError, match="model directory is missing"):
            engine.initialize()
        assert engine.last_error_code() == "load_failed"
        assert engine.last_error_code() != RUNTIME_INCOMPLETE_CODE

    def test_restart_contract(self, tmp_path: Path, engines: list[QwenEngine]) -> None:
        engine = start_engine(engines, tmp_path, "ok")
        engine.initialize()
        pid = host_pid(tmp_path)
        engine.initialize()
        assert host_pid(tmp_path) == pid
        assert len([entry for entry in host_log(tmp_path) if entry["event"] == "start"]) == 1

        tmp_path = tmp_path / "case-b"

        engine = start_engine(engines, tmp_path, "crash_after_pcm")
        engine.initialize()
        with pytest.raises(QwenEngineError):
            list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1"))
        assert engine.is_initialized is False
        engine.initialize()  # a fresh host, without the caller doing anything special
        assert engine.is_initialized is True
        assert len([entry for entry in host_log(tmp_path) if entry["event"] == "start"]) == 2


# --------------------------------------------------------------------------- #
# infer_stream
# --------------------------------------------------------------------------- #


class TestBatchSynthesis:
    """Batched segments through the engine and its provider seam."""

    def test_infer_stream_many_yields_segment_tagged_chunks(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "batch")
        engine.initialize()
        got = list(
            engine.infer_stream_many(
                ["one.", "two."], language="en", speaker="Ryan", job_id="job-1"
            )
        )
        assert [segment for segment, _chunk in got] == [0, 1]
        assert all(chunk.size == 12_000 for _segment, chunk in got)
        (batch,) = received(tmp_path, "synthesize_batch")
        assert batch["job"] == "job-1"
        assert batch["fields"]["texts"] == ["one.", "two."]

    def test_the_provider_groups_segments_into_bounded_batches(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        from vienetts_app.core.qwen_protocol import MAX_BATCH_CHARS, MAX_BATCH_SEGMENTS

        engine = start_engine(engines, tmp_path, "batch")
        engines.append(engine)
        # Explicit protocol bounds: the batch grouping contract is asserted
        # here, independent of the test machine's physical RAM (the RAM-derived
        # default has its own tier tests below).
        provider = QwenEngineProvider(engine, batch_bounds=(MAX_BATCH_SEGMENTS, MAX_BATCH_CHARS))
        context = context_for(QWEN_CUSTOM, language="en", voice_id="Ryan")
        texts = [f"segment {index}." for index in range(MAX_BATCH_SEGMENTS + 1)]
        got = list(provider.infer_stream_segments(texts, context=context, job_id="worker-1"))
        assert [index for index, _chunk in got] == list(range(len(texts)))
        batches = received(tmp_path, "synthesize_batch")
        assert [len(frame["fields"]["texts"]) for frame in batches] == [MAX_BATCH_SEGMENTS, 1]
        assert [frame["fields"]["texts"] for frame in batches][0] == texts[:MAX_BATCH_SEGMENTS]
        assert len({frame["job"] for frame in batches}) == 2  # one protocol job per batch

    def test_infer_stream_many_rejects_a_batch_over_the_character_cap(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        from vienetts_app.core.qwen_protocol import MAX_BATCH_CHARS

        engine = start_engine(engines, tmp_path, "batch")
        engine.initialize()
        # Two half-cap-plus-one segments: a valid count and each within the
        # per-segment bound, but cap + 2 characters in total.
        texts = ["x" * (MAX_BATCH_CHARS // 2 + 1), "y" * (MAX_BATCH_CHARS // 2 + 1)]
        with pytest.raises(QwenEngineError, match="characters in total"):
            list(engine.infer_stream_many(texts, language="en", speaker="Ryan"))
        assert received(tmp_path, "synthesize_batch") == []  # never crossed IPC

    def test_the_provider_splits_batches_on_total_characters_too(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        from vienetts_app.core.qwen_protocol import MAX_BATCH_CHARS, MAX_BATCH_SEGMENTS

        engine = start_engine(engines, tmp_path, "batch")
        engines.append(engine)
        provider = QwenEngineProvider(engine, batch_bounds=(MAX_BATCH_SEGMENTS, MAX_BATCH_CHARS))
        context = context_for(QWEN_CUSTOM, language="en", voice_id="Ryan")
        # Three 900-char segments: two fit one batch, the third would push it
        # past the character bound even though the segment count does not.
        texts = ["x" * 900, "y" * 900, "z" * 900]
        got = list(provider.infer_stream_segments(texts, context=context, job_id="worker-1"))
        assert [index for index, _chunk in got] == [0, 1, 2]
        batches = received(tmp_path, "synthesize_batch")
        assert [frame["fields"]["texts"] for frame in batches] == [texts[:2], texts[2:]]
        assert len({frame["job"] for frame in batches}) == 2  # one protocol job per batch

    def test_a_full_size_segment_still_gets_its_own_batch(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        from vienetts_app.core.qwen_protocol import MAX_BATCH_CHARS, MAX_TEXT_CHARS

        # The cap must never strand a single full-size segment.
        assert MAX_BATCH_CHARS >= MAX_TEXT_CHARS
        engine = start_engine(engines, tmp_path, "batch")
        engines.append(engine)
        provider = QwenEngineProvider(engine)
        context = context_for(QWEN_CUSTOM, language="en", voice_id="Ryan")
        segment = "x" * MAX_TEXT_CHARS
        got = list(
            provider.infer_stream_segments([segment, segment], context=context, job_id="worker-1")
        )
        assert [index for index, _chunk in got] == [0, 1]
        batches = received(tmp_path, "synthesize_batch")
        assert [frame["fields"]["texts"] for frame in batches] == [[segment], [segment]]


class TestLivenessHeartbeats:
    """Heartbeats during an uninterruptible generate: liveness, not progress."""

    def test_a_long_generate_is_kept_alive_by_heartbeats_that_never_reach_the_ui(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        # The fake "generates" for 1.2 s while heartbeating every 50 ms: a
        # frame_timeout far below the generate time must not fail the job.
        engine = start_engine(
            engines, tmp_path, "slow_heartbeat", frame_timeout=0.3, cancel_grace_timeout=5.0
        )
        engine.initialize()
        seen: list[tuple[float, str]] = []
        chunks = list(
            engine.infer_stream(
                "hello",
                language="en",
                speaker="Ryan",
                job_id="job-1",
                on_progress=lambda fraction, stage: seen.append((fraction, stage)),
            )
        )
        assert len(chunks) == 2
        assert seen == [(0.5, "resampling")]  # only fractioned frames reach the UI

    def test_cancel_keeps_a_busy_but_responsive_host_alive(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(
            engines,
            tmp_path,
            "slow_heartbeat",
            cancel_timeout=0.4,
            cancel_grace_timeout=5.0,
        )
        engine.initialize()
        run = StreamRun(engine, "job-1")
        wait_for(lambda: bool(received(tmp_path, "synthesize")), what="the job to reach the host")
        assert engine.cancel("job-1") is True
        run.assert_finished()
        assert isinstance(run.error, QwenEngineCancelled)
        # The host settled the cancel itself once its generate returned: the
        # loaded checkpoint must NOT be thrown away — a reload costs seconds
        # to minutes of spawn + from_pretrained.
        assert pid_alive(host_pid(tmp_path)) is True
        assert engine.is_initialized is True

    def test_cancel_gives_up_on_a_heartbeating_host_after_the_grace(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(
            engines,
            tmp_path,
            "heartbeat_forever",
            cancel_timeout=0.2,
            cancel_grace_timeout=0.4,
        )
        engine.initialize()
        run = StreamRun(engine, "job-1")
        wait_for(lambda: bool(received(tmp_path, "synthesize")), what="the job to reach the host")
        assert engine.cancel("job-1") is True
        wait_for(lambda: not pid_alive(host_pid(tmp_path)), what="the hung host to be reaped")
        run.assert_finished()
        assert isinstance(run.error, QwenEngineCancelled)


class TestInferStream:
    def test_stream_basics(self, tmp_path: Path, engines: list[QwenEngine]) -> None:
        engine = start_engine(engines, tmp_path, "ok")
        chunks = list(
            engine.infer_stream("hello world", language="en", speaker="Ryan", job_id="job-1")
        )
        assert len(chunks) == 2
        assert all(chunk.dtype == np.float32 for chunk in chunks)
        assert [chunk.size for chunk in chunks] == [12000, 12000]
        (synthesize,) = received(tmp_path, "synthesize")
        assert synthesize["job"] == "job-1"
        assert synthesize["fields"] == {"text": "hello world", "language": "en", "speaker": "Ryan"}
        engine.close()

        tmp_path = tmp_path / "case-b"

        engine = start_engine(engines, tmp_path, "ok", profile="qwen_base_0_6b")
        list(
            engine.infer_stream(
                "hello",
                language="en",
                voice_prompt="/tmp/reference.wav",
                ref_text="hello there",
                job_id="job-clone",
            )
        )
        (synthesize,) = received(tmp_path, "synthesize")
        assert synthesize["fields"]["voicePrompt"] == "/tmp/reference.wav"
        assert synthesize["fields"]["refText"] == "hello there"
        assert "speaker" not in synthesize["fields"]

        tmp_path = tmp_path / "case-b"

        seen: list[tuple[float, str]] = []
        engine = start_engine(engines, tmp_path, "ok")
        chunks = list(
            engine.infer_stream(
                "hello",
                language="en",
                speaker="Ryan",
                job_id="job-1",
                on_progress=lambda fraction, stage: seen.append((fraction, stage)),
            )
        )
        assert len(chunks) == 2
        assert seen == [(0.5, "resampling")]

    def test_stream_rejections(self, tmp_path: Path, engines: list[QwenEngine]) -> None:
        engine = start_engine(engines, tmp_path, "fail")
        with pytest.raises(QwenEngineError, match="the model said no"):
            list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1"))
        assert engine.is_initialized is True  # a non-fatal failure keeps the host

        tmp_path = tmp_path / "case-b"

        engine = start_engine(engines, tmp_path, "ok")
        with pytest.raises(QwenEngineError, match="segment"):
            list(
                engine.infer_stream(
                    "x" * (MAX_TEXT_CHARS + 1), language="en", speaker="Ryan", job_id="job-1"
                )
            )
        assert received(tmp_path, "synthesize") == []

    def test_lazy_init_and_job_id(self, tmp_path: Path, engines: list[QwenEngine]) -> None:
        engine = start_engine(engines, tmp_path, "ok")
        assert engine.is_initialized is False
        chunks = list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1"))
        assert len(chunks) == 2
        assert engine.is_initialized is True

        tmp_path = tmp_path / "case-b"

        engine = start_engine(engines, tmp_path, "ok")
        assert len(list(engine.infer_stream("hello", language="en", speaker="Ryan"))) == 2

    def test_fatal_host_failures(self, tmp_path: Path, engines: list[QwenEngine]) -> None:
        engine = start_engine(engines, tmp_path, "oom")
        with pytest.raises(QwenEngineError, match="CUDA out of memory"):
            list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1"))
        assert engine.is_initialized is False
        engine.initialize()
        assert engine.is_initialized is True

        tmp_path = tmp_path / "case-b"

        engine = start_engine(engines, tmp_path, "crash_after_pcm")
        chunks: list[np.ndarray] = []
        with pytest.raises(QwenEngineError) as failure:
            for chunk in engine.infer_stream(
                "hello", language="en", speaker="Ryan", job_id="job-1"
            ):
                chunks.append(chunk)
        assert len(chunks) == 1  # the partial audio the caller must discard
        assert "boom: the host died mid-job" in str(failure.value)
        assert "exited with status 3" in str(failure.value)
        assert engine.is_initialized is False

        tmp_path = tmp_path / "case-b"

        """A silent stdout close from a signal death must say so.

        A kernel OOM kill (macOS's memory manager shoots the largest process)
        leaves nothing on stderr and no frames on stdout — exactly what a
        4 × 2000-character batch produced on a 16 GB Mac mini. The error has
        to carry the exit status or it reads as a mystery.
        """
        if sys.platform == "win32":
            pytest.skip("POSIX signal semantics")
        engine = start_engine(engines, tmp_path, "hang_synthesize", frame_timeout=5.0)
        engine.initialize()
        stream = StreamRun(engine, "job-1")
        wait_for(lambda: received(tmp_path, "synthesize"), what="the synthesize frame")
        os.kill(host_pid(tmp_path), signal.SIGKILL)
        stream.assert_finished()
        assert isinstance(stream.error, QwenEngineError)
        message = str(stream.error)
        assert "closed its output stream" in message
        assert "SIGKILL" in message
        assert "memory" in message
        assert engine.is_initialized is False

    def test_unresponsive_hosts(self, tmp_path: Path, engines: list[QwenEngine]) -> None:
        """A host that dies mid-job must fail the JOB, never the process.

        The app restores the default ``SIGPIPE`` disposition when its own
        stdout is a pipe (``vienetts_app._restore_default_sigpipe``), and that
        default applies to the host pipe too: the cleanup frames this adapter
        writes once the host is gone (a cancel for the abandoned job, then a
        cancel/shutdown on close) would otherwise end the whole app instead of
        raising an actionable engine error.
        """
        if sys.platform == "win32":  # no SIGPIPE on Windows
            pytest.skip("SIGPIPE does not exist on Windows")
        engine = start_engine(engines, tmp_path, "crash_after_pcm")
        previous = signal.getsignal(signal.SIGPIPE)
        signal.signal(signal.SIGPIPE, signal.SIG_DFL)
        try:
            with pytest.raises(QwenEngineError) as failure:
                list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1"))
            assert "boom: the host died mid-job" in str(failure.value)
            # The other dead-pipe writes are just as harmless.
            assert engine.cancel("job-1") is True
            engine.close()
        finally:
            signal.signal(signal.SIGPIPE, previous)
        assert engine.is_initialized is False
        assert received(tmp_path, "shutdown") == []  # the host was already gone

        tmp_path = tmp_path / "case-b"

        engine = start_engine(engines, tmp_path, "hang_synthesize", frame_timeout=0.4)
        with pytest.raises(QwenEngineError, match="timed out"):
            list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1"))
        assert engine.is_initialized is False
        wait_for(lambda: not pid_alive(host_pid(tmp_path)), what="the hung host to be reaped")

        tmp_path = tmp_path / "case-b"

        engine = start_engine(engines, tmp_path, "garbage")
        with pytest.raises(QwenEngineError, match="malformed"):
            list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1"))
        assert engine.is_initialized is False

    def test_stream_job_rules(self, tmp_path: Path, engines: list[QwenEngine]) -> None:
        engine = start_engine(engines, tmp_path, "stale")
        assert (
            len(list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1")))
            == 2
        )
        assert engine.is_initialized is True  # the late pcm did not break the session
        assert (
            len(list(engine.infer_stream("again", language="en", speaker="Ryan", job_id="job-2")))
            == 2
        )

        tmp_path = tmp_path / "case-b"

        engine = start_engine(engines, tmp_path, "slow_pcm")
        stream = engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1")
        next(stream)
        stream.close()
        wait_for(lambda: received(tmp_path, "cancel"), what="the cancel frame")
        (cancel,) = received(tmp_path, "cancel")
        assert cancel["job"] == "job-1"

        tmp_path = tmp_path / "case-b"

        engine = start_engine(engines, tmp_path, "slow_pcm")
        stream = engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1")
        next(stream)
        # The host runs one job at a time; the second submission must fail loudly
        # instead of interleaving two streams.
        with pytest.raises(QwenEngineError, match="could not start Qwen synthesis"):
            next(engine.infer_stream("again", language="en", speaker="Ryan", job_id="job-2"))
        stream.close()

        tmp_path = tmp_path / "case-b"

        engine = start_engine(engines, tmp_path, "unknown_job")
        with pytest.raises(QwenEngineError, match="violated the protocol"):
            list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1"))
        assert engine.is_initialized is False


# --------------------------------------------------------------------------- #
# cancel
# --------------------------------------------------------------------------- #


class TestCancel:
    def test_cancel_reports(self, tmp_path: Path, engines: list[QwenEngine]) -> None:
        engine = start_engine(engines, tmp_path, "ok")
        engine.initialize()
        assert engine.cancel("never-started") is False
        assert received(tmp_path, "cancel") == []

        tmp_path = tmp_path / "case-b"

        engine = start_engine(engines, tmp_path, "graceful_cancel")
        run = StreamRun(engine, "job-cancel")
        wait_for(lambda: received(tmp_path, "synthesize"), what="the job to start")
        assert engine.cancel("job-cancel") is True
        run.assert_finished()
        assert isinstance(run.error, QwenEngineCancelled)
        assert run.chunks == []
        assert engine.is_initialized is True  # a graceful stop keeps the host

    def test_cancel_escalation(self, tmp_path: Path, engines: list[QwenEngine]) -> None:
        engine = start_engine(engines, tmp_path, "slow_cancel", cancel_timeout=0.1)
        run = StreamRun(engine, "job-stubborn")
        wait_for(lambda: received(tmp_path, "synthesize"), what="the job to start")
        pid = host_pid(tmp_path)
        assert engine.cancel("job-stubborn") is True
        run.assert_finished()
        assert isinstance(run.error, QwenEngineCancelled)
        assert engine.is_initialized is False
        wait_for(lambda: not pid_alive(pid), what="the stubborn host to be killed")
        engine.initialize()  # the next job gets a clean host
        assert engine.is_initialized is True

        tmp_path = tmp_path / "case-b"

        engine = start_engine(engines, tmp_path, "graceful_cancel")
        # The request lands before the generator has sent `synthesize`.
        assert engine.cancel("job-early") is False
        run = StreamRun(engine, "job-early")
        run.assert_finished()
        assert isinstance(run.error, QwenEngineCancelled)
        # The job never reached the host: a cancelled-before-start job must
        # not run an autoregressive pass just to be cancelled at its end.
        assert received(tmp_path, "synthesize") == []
        assert engine.is_initialized is False  # not even a host was spawned

        tmp_path = tmp_path / "case-b"

        # The lazy load blocks the stream for a while (a cold checkpoint load
        # takes seconds to minutes in production); a cancel landing inside
        # that window must refuse the job once the load finishes instead of
        # sending it to the host anyway.
        engine = start_engine(engines, tmp_path, "slow_load")
        run = StreamRun(engine, "job-loading")
        wait_for(lambda: bool(received(tmp_path, "load")), what="the host to start loading")
        assert engine.cancel("job-loading") is False  # not sent to the host yet
        run.assert_finished()
        assert isinstance(run.error, QwenEngineCancelled)
        assert received(tmp_path, "synthesize") == []

    def test_cancel_edge_cases(self, tmp_path: Path, engines: list[QwenEngine]) -> None:
        engine = start_engine(
            engines, tmp_path, "kill_required", cancel_timeout=0.1, kill_timeout=0.2
        )
        run = StreamRun(engine, "job-immortal")
        wait_for(lambda: received(tmp_path, "synthesize"), what="the job to start")
        pid = host_pid(tmp_path)
        assert engine.cancel("job-immortal") is True
        run.assert_finished()
        assert isinstance(run.error, QwenEngineCancelled)
        wait_for(lambda: not pid_alive(pid), what="the host that ignores SIGTERM to be killed")

        tmp_path = tmp_path / "case-b"

        engine = start_engine(engines, tmp_path, "crash_after_pcm")
        run = StreamRun(engine, "job-dead")
        run.assert_finished()
        assert isinstance(run.error, QwenEngineError)
        assert engine.is_initialized is False
        assert engine.cancel("job-dead") is True  # the job is over: the host is gone


# --------------------------------------------------------------------------- #
# close + stderr
# --------------------------------------------------------------------------- #


class TestCloseAndStderr:
    def test_close_contract(self, tmp_path: Path, engines: list[QwenEngine]) -> None:
        engine = start_engine(engines, tmp_path, "ok")
        engine.initialize()
        pid = host_pid(tmp_path)
        engine.close()
        assert engine.is_initialized is False
        assert received(tmp_path, "shutdown") != []
        wait_for(lambda: not pid_alive(pid), what="the host to exit")
        engine.close()  # idempotent
        assert len(received(tmp_path, "shutdown")) == 1

        tmp_path = tmp_path / "case-b"

        engine = start_engine(engines, tmp_path, "hang_synthesize")
        run = StreamRun(engine, "job-open")
        wait_for(lambda: received(tmp_path, "synthesize"), what="the job to start")
        pid = host_pid(tmp_path)
        engine.close()
        run.assert_finished()
        assert isinstance(run.error, QwenEngineCancelled)  # the app is shutting down
        assert received(tmp_path, "shutdown") == []
        wait_for(lambda: not pid_alive(pid), what="the busy host to be reaped")

    def test_stderr_tail(self, tmp_path: Path, engines: list[QwenEngine]) -> None:
        engine = start_engine(engines, tmp_path, "noisy")
        list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1"))
        wait_for(lambda: "host log line 39" in engine.stderr_tail(), what="stderr to drain")
        assert len(engine.stderr_tail().splitlines()) <= 20  # bounded, not the whole history

        tmp_path = tmp_path / "case-b"

        engine = start_engine(engines, tmp_path, "ok")
        assert engine.stderr_tail() == ""


class TestProfileVocabulary:
    def test_engine_profiles_map_onto_the_host_protocol_keys(self) -> None:
        assert dict(PROFILE_ENGINES) == dict(ENGINE_PROFILE_KEYS)
        assert set(ENGINE_PROFILE_KEYS) == {"customvoice", "base"}


# --------------------------------------------------------------------------- #
# provider seam
# --------------------------------------------------------------------------- #


class ProviderRun:
    """Consume ``QwenEngineProvider.infer_stream`` on a thread."""

    def __init__(
        self,
        provider: Any,
        job_id: str,
        context: Any,
        text: str = "hello",
        **kwargs: Any,
    ) -> None:
        self.chunks: list[np.ndarray] = []
        self.error: BaseException | None = None
        self._done = threading.Event()
        self._thread = threading.Thread(
            target=self._consume, args=(provider, job_id, context, text, kwargs), daemon=True
        )
        self._thread.start()

    def _consume(self, provider: Any, job_id: str, context: Any, text: str, kwargs: Any) -> None:
        try:
            for chunk in provider.infer_stream(text, context=context, job_id=job_id, **kwargs):
                self.chunks.append(chunk)
        except BaseException as exc:  # noqa: BLE001 — recorded for the test to assert on
            self.error = exc
        finally:
            self._done.set()

    def wait(self, timeout: float = 5.0) -> bool:
        return self._done.wait(timeout)

    def assert_finished(self) -> None:
        assert self.wait(), "the provider stream never finished"
        self._thread.join(1.0)


@pytest.fixture()
def provider_engines() -> Iterator[list[QwenEngine]]:
    created: list[QwenEngine] = []
    yield created
    for engine in created:
        engine.close()


def provider_for(
    engines: list[QwenEngine],
    tmp_path: Path,
    mode: str = "ok",
    *,
    profile: str = "qwen_custom_0_6b",
    **kwargs: Any,
) -> QwenEngineProvider:
    engine = host_fake.engine_for(tmp_path, mode, profile=profile)
    engines.append(engine)
    return QwenEngineProvider(engine, **kwargs)


def custom_context(language: str = "zh", voice_id: str = "Vivian") -> SynthesisContext:
    return context_for(QWEN_CUSTOM, language=language, voice_id=voice_id)


def clone_context(clone_id: str = "clone-1", language: str = "en") -> SynthesisContext:
    return context_for(QWEN_BASE, language=language, clone_id=clone_id)


class TestQwenEngineProvider:
    def test_language_mapping(self, tmp_path: Path, provider_engines: list[QwenEngine]) -> None:
        provider = provider_for(provider_engines, tmp_path)
        chunks = list(provider.infer_stream("你好。", context=custom_context(), job_id="job-1"))
        assert len(chunks) == 2
        assert all(chunk.dtype == np.float32 for chunk in chunks)
        (synthesize,) = received(tmp_path, "synthesize")
        # The language field carries the APP code: the host validates it and
        # maps it to the model's own name ("zh" → "Chinese") itself. Sending the
        # mapped name made every job fail — the host read it as an unknown code.
        assert synthesize["fields"] == {
            "text": "你好。",
            "language": "zh",
            "speaker": "Vivian",
        }
        # The 0.6B host samples with its own settings: no temperature is sent.
        assert "temperature" not in synthesize["fields"]

        tmp_path = tmp_path / "case-b"

        provider = provider_for(provider_engines, tmp_path)
        list(
            provider.infer_stream("hello", context=custom_context(language="auto"), job_id="job-1")
        )
        (synthesize,) = received(tmp_path, "synthesize")
        assert synthesize["fields"]["language"] == "auto"

        tmp_path = tmp_path / "case-b"

        """Defence in depth: an unaccepted code never reaches the wire.

        ``SynthesisContext`` refuses an unsupported language at construction, so
        this context is built around that check on purpose — the provider still
        refuses instead of sending a code the host would reject.
        """
        provider = provider_for(provider_engines, tmp_path)
        context = custom_context()
        object.__setattr__(context, "language", "xx")
        with pytest.raises(QwenEngineError, match="does not support language"):
            list(provider.infer_stream("hello", context=context, job_id="job-1"))
        assert received(tmp_path, "synthesize") == []

    def test_clone_resolution(self, tmp_path: Path, provider_engines: list[QwenEngine]) -> None:
        prompts: list[str] = []

        def resolve(clone_id: str) -> ClonePrompt:
            prompts.append(clone_id)
            return ClonePrompt(reference_path="/tmp/ref.wav", transcript="hello there")

        provider = provider_for(
            provider_engines, tmp_path, profile="qwen_base_0_6b", clone_prompt_for=resolve
        )
        list(provider.infer_stream("hello", context=clone_context(), job_id="job-1"))
        (synthesize,) = received(tmp_path, "synthesize")
        assert prompts == ["clone-1"]
        assert synthesize["fields"]["voicePrompt"] == "/tmp/ref.wav"
        assert synthesize["fields"]["refText"] == "hello there"
        assert "speaker" not in synthesize["fields"]

        tmp_path = tmp_path / "case-b"

        provider = provider_for(provider_engines, tmp_path, profile="qwen_base_0_6b")
        with pytest.raises(QwenEngineError, match="cannot be resolved"):
            list(provider.infer_stream("hello", context=clone_context(), job_id="job-1"))
        assert received(tmp_path, "synthesize") == []  # nothing crossed IPC

        tmp_path = tmp_path / "case-b"

        provider = provider_for(
            provider_engines,
            tmp_path,
            profile="qwen_base_0_6b",
            clone_prompt_for=lambda clone_id: None,
        )
        with pytest.raises(QwenEngineError, match="clone store has no reference clip"):
            list(provider.infer_stream("hello", context=clone_context(), job_id="job-1"))

    def test_segment_jobs_and_cancel(
        self, tmp_path: Path, provider_engines: list[QwenEngine]
    ) -> None:
        provider = provider_for(provider_engines, tmp_path)
        context = custom_context()
        first = list(provider.infer_stream("你好。", context=context, job_id="job-1"))
        second = list(provider.infer_stream("世界。", context=context, job_id="job-1"))
        assert len(first) == len(second) == 2
        sent = received(tmp_path, "synthesize")
        assert len(sent) == 2
        assert sent[0]["job"].startswith("job-1:")
        assert sent[1]["job"].startswith("job-1:")
        assert sent[0]["job"] != sent[1]["job"]  # the host settles an id per segment

        tmp_path = tmp_path / "case-b"

        provider = provider_for(provider_engines, tmp_path, mode="graceful_cancel")
        run = ProviderRun(provider, "job-cancel", custom_context())
        wait_for(lambda: received(tmp_path, "synthesize"), what="the segment to start")
        assert provider.cancel("job-cancel") is True
        run.assert_finished()
        assert isinstance(run.error, QwenEngineCancelled)
        assert run.chunks == []
        (cancel,) = received(tmp_path, "cancel")
        (synthesize,) = received(tmp_path, "synthesize")
        assert cancel["job"] == synthesize["job"]  # the running segment, not the job id

        tmp_path = tmp_path / "case-b"

        provider = provider_for(provider_engines, tmp_path, mode="graceful_cancel")
        assert provider.cancel("job-early") is False  # nothing running yet
        run = ProviderRun(provider, "job-early", custom_context())
        run.assert_finished()
        assert isinstance(run.error, QwenEngineCancelled)
        # The cancelled segment never reached the host: no synthesize (and no
        # cancel either) is ever sent for a job refused before it started.
        assert received(tmp_path, "synthesize") == []
        assert received(tmp_path, "cancel") == []

        tmp_path = tmp_path / "case-b"

        provider = provider_for(provider_engines, tmp_path)
        assert provider.cancel("never-started") is False
        assert received(tmp_path, "cancel") == []

    def test_context_and_size_guards(
        self, tmp_path: Path, provider_engines: list[QwenEngine]
    ) -> None:
        provider = provider_for(provider_engines, tmp_path)
        with pytest.raises(QwenEngineError, match="must carry its engine context"):
            list(provider.infer_stream("hello", job_id="job-1"))

        tmp_path = tmp_path / "case-b"

        provider = provider_for(provider_engines, tmp_path)  # customvoice engine
        with pytest.raises(QwenEngineError, match="serves 'qwen_custom_0_6b'"):
            list(provider.infer_stream("hello", context=clone_context(), job_id="job-1"))

        tmp_path = tmp_path / "case-b"

        provider = provider_for(provider_engines, tmp_path)
        with pytest.raises(QwenEngineError, match="segment"):
            list(
                provider.infer_stream(
                    "x" * (MAX_TEXT_CHARS + 1), context=custom_context(), job_id="job-1"
                )
            )
        assert received(tmp_path, "synthesize") == []

    def test_lifecycle_delegates_to_the_engine(
        self, tmp_path: Path, provider_engines: list[QwenEngine]
    ) -> None:
        provider = provider_for(provider_engines, tmp_path)
        assert provider.profile == "qwen_custom_0_6b"
        assert provider.is_initialized is False
        provider.initialize()
        assert provider.is_initialized is True
        assert provider_engines[0].capabilities().speakers == ("Ryan",)
        provider.close()
        assert provider.is_initialized is False


class TestQwenEngineProviderVoiceOps:
    """Voice operations route to the profile-scoped clone store (Task 4.2)."""

    def store_for(self, tmp_path: Path) -> CloneStore:
        return CloneStore(tmp_path / "clones", now=lambda: datetime(2026, 9, 21, tzinfo=UTC))

    def reference(self, tmp_path: Path, name: str = "ref.wav") -> Path:
        return write_wav_file(np.full(24_000, 0.2, dtype=np.float32), tmp_path / name, 24_000)

    def base_provider(self, engines: list[QwenEngine], tmp_path: Path, store: Any) -> Any:
        return provider_for(engines, tmp_path, profile=QWEN_BASE, clone_store=store)

    def test_add_contract(self, tmp_path: Path, provider_engines: list[QwenEngine]) -> None:
        store = self.store_for(tmp_path)
        provider = self.base_provider(provider_engines, tmp_path, store)
        clip = self.reference(tmp_path)

        value = provider.voice_op(
            VoiceOp(
                op="add",
                name="Ngọc Anh",
                clip_path=str(clip),
                transcript="Xin chào.",
                consent=True,
                profile=QWEN_BASE,
            )
        )

        assert value == {
            "op": "add",
            "name": "Ngọc Anh",
            "cloneId": value["cloneId"],
            "profile": QWEN_BASE,
        }
        clone = store.get(value["cloneId"])
        assert clone.transcript == "Xin chào."
        assert clone.reference_path.is_file()
        # Enrollment is app-side bookkeeping: no host process is started for it.
        assert host_log(tmp_path) == []
        assert provider.is_initialized is False

        tmp_path = tmp_path / "case-b"

        store = self.store_for(tmp_path)
        provider = self.base_provider(provider_engines, tmp_path, store)
        clip = self.reference(tmp_path)

        with pytest.raises(CloneStoreError, match="explicit consent acknowledgement"):
            provider.voice_op(
                VoiceOp(op="add", name="V", clip_path=str(clip), transcript="hi", profile=QWEN_BASE)
            )
        with pytest.raises(CloneStoreError, match="needs the reference transcript"):
            provider.voice_op(
                VoiceOp(op="add", name="V", clip_path=str(clip), consent=True, profile=QWEN_BASE)
            )
        assert store.list() == ()

    def test_remove_contract(self, tmp_path: Path, provider_engines: list[QwenEngine]) -> None:
        store = self.store_for(tmp_path)
        provider = self.base_provider(provider_engines, tmp_path, store)
        by_name = store.enroll(
            name="Theo tên",
            profile=QWEN_BASE,
            reference_clip=self.reference(tmp_path, "a.wav"),
            transcript="một",
            consent=True,
        )
        by_id = store.enroll(
            name="Theo id",
            profile=QWEN_BASE,
            reference_clip=self.reference(tmp_path, "b.wav"),
            transcript="hai",
            consent=True,
        )

        assert provider.voice_op(VoiceOp(op="remove", name="Theo tên", profile=QWEN_BASE)) == {
            "op": "remove",
            "name": "Theo tên",
            "cloneId": by_name.clone_id,
            "profile": QWEN_BASE,
        }
        assert provider.voice_op(VoiceOp(op="remove", name=by_id.clone_id, profile=QWEN_BASE)) == {
            "op": "remove",
            "name": "Theo id",
            "cloneId": by_id.clone_id,
            "profile": QWEN_BASE,
        }
        assert store.list() == ()
        assert not by_name.reference_path.exists()

        tmp_path = tmp_path / "case-b"

        store = self.store_for(tmp_path)
        provider = self.base_provider(provider_engines, tmp_path, store)
        store.enroll(
            name="Ngọc Anh",
            profile=QWEN_BASE,
            reference_clip=self.reference(tmp_path),
            transcript="hi",
            consent=True,
        )

        with pytest.raises(EngineProviderError, match="enrolled clones: Ngọc Anh"):
            provider.voice_op(VoiceOp(op="remove", name="Không có", profile=QWEN_BASE))

        tmp_path = tmp_path / "case-b"

        store = self.store_for(tmp_path)
        provider = self.base_provider(provider_engines, tmp_path, store)
        same_name_vieneu = store.enroll(
            name="Shared",
            profile=VIENEU,
            reference_clip=self.reference(tmp_path, "v.wav"),
            consent=True,
        )
        base = store.enroll(
            name="Shared",
            profile=QWEN_BASE,
            reference_clip=self.reference(tmp_path, "b.wav"),
            transcript="hi",
            consent=True,
        )

        provider.voice_op(VoiceOp(op="remove", name="Shared", profile=QWEN_BASE))

        assert [clone.clone_id for clone in store.list()] == [same_name_vieneu.clone_id]
        assert base.reference_path.exists() is False
        assert same_name_vieneu.reference_path.is_file()

    def test_voice_op_rejections(self, tmp_path: Path, provider_engines: list[QwenEngine]) -> None:
        store = self.store_for(tmp_path)
        provider = self.base_provider(provider_engines, tmp_path, store)
        clone = store.enroll(
            name="V",
            profile=QWEN_BASE,
            reference_clip=self.reference(tmp_path),
            transcript="hi",
            consent=True,
        )

        value = provider.voice_op(VoiceOp(op="remove", name="V"))

        assert value["cloneId"] == clone.clone_id

        tmp_path = tmp_path / "case-b"

        provider = self.base_provider(provider_engines, tmp_path, self.store_for(tmp_path))

        with pytest.raises(EngineProviderError, match="only available on the VieNeu-TTS profile"):
            provider.voice_op(VoiceOp(op="denoise", clip_path="/tmp/ref.wav", profile=QWEN_BASE))

        tmp_path = tmp_path / "case-b"

        provider = provider_for(provider_engines, tmp_path, clone_store=self.store_for(tmp_path))

        with pytest.raises(EngineProviderError, match="fixed speakers and cannot enroll clones"):
            provider.voice_op(VoiceOp(op="remove", name="V", profile=QWEN_CUSTOM))

        tmp_path = tmp_path / "case-b"

        provider = provider_for(provider_engines, tmp_path, profile=QWEN_BASE)

        with pytest.raises(EngineProviderError, match="no clone store configured"):
            provider.voice_op(VoiceOp(op="remove", name="V", profile=QWEN_BASE))

        tmp_path = tmp_path / "case-b"

        provider = self.base_provider(provider_engines, tmp_path, self.store_for(tmp_path))

        with pytest.raises(EngineProviderError, match="serves 'qwen_base_0_6b'"):
            provider.voice_op(VoiceOp(op="remove", name="V", profile=VIENEU))

    def test_prompt_flow(self, tmp_path: Path, provider_engines: list[QwenEngine]) -> None:
        store = self.store_for(tmp_path)
        clone = store.enroll(
            name="Ngọc Anh",
            profile=QWEN_BASE,
            reference_clip=self.reference(tmp_path),
            transcript="Xin chào.",
            consent=True,
        )
        provider = self.base_provider(provider_engines, tmp_path, store)

        list(provider.infer_stream("你好。", context=clone_context(clone.clone_id), job_id="job-1"))

        (synthesize,) = received(tmp_path, "synthesize")
        assert synthesize["fields"]["voicePrompt"] == str(clone.reference_path)
        assert synthesize["fields"]["refText"] == "Xin chào."
        assert "speaker" not in synthesize["fields"]

        tmp_path = tmp_path / "case-b"

        store = self.store_for(tmp_path)
        clone = store.enroll(
            name="Ngọc Anh",
            profile=QWEN_BASE,
            reference_clip=self.reference(tmp_path),
            transcript="Xin chào.",
            consent=True,
        )
        provider = self.base_provider(provider_engines, tmp_path, store)
        context = clone_context(clone.clone_id)

        list(provider.infer_stream("你好。", context=context, job_id="job-1"))
        provider._engine.close()  # a crash/quit drops the host, not the clone
        list(provider.infer_stream("世界。", context=context, job_id="job-2"))

        assert [entry["fields"]["voicePrompt"] for entry in received(tmp_path, "synthesize")] == [
            str(clone.reference_path),
            str(clone.reference_path),
        ]
        starts = [entry for entry in host_log(tmp_path) if entry["event"] == "start"]
        assert len(starts) == 2, "the rebuilt prompt must come from the store, not the dead host"


# --------------------------------------------------------------------------- #
# resource governor: RAM-scaled batch bounds
# --------------------------------------------------------------------------- #


class TestRamScaledBatchBounds:
    """Batch bounds shrink with the machine, never grow past the protocol."""

    def test_ram_tiers(self) -> None:
        from vienetts_app.core.qwen_protocol import MAX_BATCH_CHARS, MAX_BATCH_SEGMENTS

        gib = 1024**3
        assert batch_bounds_for_ram(None) == (MAX_BATCH_SEGMENTS, MAX_BATCH_CHARS)
        # The 16 GB machine the protocol caps were proven on keeps the full batch.
        assert batch_bounds_for_ram(16 * gib) == (MAX_BATCH_SEGMENTS, MAX_BATCH_CHARS)
        assert batch_bounds_for_ram(32 * gib) == (MAX_BATCH_SEGMENTS, MAX_BATCH_CHARS)
        assert batch_bounds_for_ram(8 * gib) == (2, 1024)
        assert batch_bounds_for_ram(4 * gib) == (1, 512)

        from vienetts_app.core.qwen_protocol import MAX_BATCH_CHARS, MAX_BATCH_SEGMENTS

        for ram in (None, 0, 512 * 1024**2, 8 * 1024**3, 64 * 1024**3):
            segments, chars = batch_bounds_for_ram(ram)
            assert segments <= MAX_BATCH_SEGMENTS
            assert chars <= MAX_BATCH_CHARS
            assert segments >= 1 and chars >= 1

    def test_segment_batch_bounds(self) -> None:
        batches = list(_segment_batches(["aa", "bb", "cc"], max_segments=2, max_chars=5))
        assert batches == [["aa", "bb"], ["cc"]]  # "cc" would push the batch past 5 chars

        from vienetts_app.core.qwen_protocol import MAX_BATCH_SEGMENTS

        # Asking for more than the protocol allows is clamped, not honored.
        batches = list(
            _segment_batches(["a"] * (MAX_BATCH_SEGMENTS + 1), max_segments=99, max_chars=10**9)
        )
        assert [len(batch) for batch in batches] == [MAX_BATCH_SEGMENTS, 1]

    def test_provider_ram_bounds(
        self, tmp_path: Path, provider_engines: list[QwenEngine], monkeypatch
    ) -> None:
        from vienetts_app.core import qwen_engine as engine_module

        monkeypatch.setattr(engine_module, "physical_ram_bytes", lambda: 4 * 1024**3)
        provider = provider_for(provider_engines, tmp_path)  # default bounds: derived from RAM
        context = context_for(QWEN_CUSTOM, language="en", voice_id="Ryan")
        texts = ["one.", "two.", "three."]
        got = list(provider.infer_stream_segments(texts, context=context, job_id="worker-1"))
        assert [index for index, _chunk in got] == [0, 1, 2]
        batches = received(tmp_path, "synthesize_batch")
        assert [frame["fields"]["texts"] for frame in batches] == [["one."], ["two."], ["three."]]

        tmp_path = tmp_path / "case-b"

        from vienetts_app.core import qwen_engine as engine_module

        calls: list[int] = []

        def fake_ram() -> int:
            calls.append(1)
            return 4 * 1024**3

        monkeypatch.setattr(engine_module, "physical_ram_bytes", fake_ram)
        provider = provider_for(provider_engines, tmp_path)
        context = context_for(QWEN_CUSTOM, language="en", voice_id="Ryan")
        list(provider.infer_stream_segments(["one."], context=context, job_id="worker-1"))
        list(provider.infer_stream_segments(["two."], context=context, job_id="worker-2"))
        assert len(calls) == 1  # cached for the provider's lifetime

        tmp_path = tmp_path / "case-b"

        from vienetts_app.core import qwen_engine as engine_module

        monkeypatch.setattr(engine_module, "physical_ram_bytes", lambda: 4 * 1024**3)
        provider = provider_for(provider_engines, tmp_path, batch_bounds=(4, 2000))
        context = context_for(QWEN_CUSTOM, language="en", voice_id="Ryan")
        texts = ["one.", "two.", "three."]
        list(provider.infer_stream_segments(texts, context=context, job_id="worker-1"))
        batches = received(tmp_path, "synthesize_batch")
        assert [frame["fields"]["texts"] for frame in batches] == [texts]  # one full batch


# --------------------------------------------------------------------------- #
# resource governor: RSS-growth host recycle
# --------------------------------------------------------------------------- #


class TestRssRecycleAtJobBoundary:
    """A host whose RSS outgrew its baseline is recycled between jobs."""

    @staticmethod
    def counted_footprint(baseline: int, bloated_call: int) -> Any:
        """A sampler whose Nth reading reports ``baseline + RSS_RECYCLE_GROWTH_BYTES``."""
        calls: list[int] = []

        def footprint(_pid: int) -> int | None:
            calls.append(1)
            return baseline + RSS_RECYCLE_GROWTH_BYTES if len(calls) >= bloated_call else baseline

        return footprint

    def test_recycle_decisions(self, tmp_path: Path, engines: list[QwenEngine]) -> None:
        engine = start_engine(
            engines, tmp_path, "ok", footprint=self.counted_footprint(1_000_000_000, 2)
        )
        engine.initialize()
        first_pid = host_pid(tmp_path)

        chunks = list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1"))

        assert chunks  # the job itself still succeeded
        wait_for(
            lambda: len([entry for entry in host_log(tmp_path) if entry["event"] == "start"]) == 2,
            what="the recycled host to start",
        )
        assert host_pid(tmp_path) != first_pid
        assert engine.is_initialized
        (synthesize,) = received(tmp_path, "synthesize")  # ran on the fresh host

        tmp_path = tmp_path / "case-b"

        engine = start_engine(
            engines, tmp_path, "ok", footprint=self.counted_footprint(1_000_000_000, 99)
        )
        engine.initialize()
        first_pid = host_pid(tmp_path)

        list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1"))

        starts = [entry for entry in host_log(tmp_path) if entry["event"] == "start"]
        assert len(starts) == 1
        assert host_pid(tmp_path) == first_pid

        tmp_path = tmp_path / "case-b"

        engine = start_engine(engines, tmp_path, "ok", footprint=lambda _pid: None)
        engine.initialize()

        list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1"))

        assert len([entry for entry in host_log(tmp_path) if entry["event"] == "start"]) == 1

    def test_recycle_edges(self, tmp_path: Path, engines: list[QwenEngine]) -> None:
        bloated = 1_000_000_000 + RSS_RECYCLE_GROWTH_BYTES * 10
        engine = start_engine(
            engines, tmp_path, "ok", footprint=lambda _pid: bloated, rss_growth_recycle_bytes=0
        )
        engine.initialize()

        list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1"))

        assert len([entry for entry in host_log(tmp_path) if entry["event"] == "start"]) == 1

        tmp_path = tmp_path / "case-b"

        def broken(_pid: int) -> int | None:
            raise RuntimeError("no sampler on this machine")

        engine = start_engine(engines, tmp_path, "ok", footprint=broken)
        engine.initialize()  # baseline sample failure must not fail initialization
        assert engine.is_initialized

        chunks = list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1"))
        assert chunks
        assert len([entry for entry in host_log(tmp_path) if entry["event"] == "start"]) == 1

        tmp_path = tmp_path / "case-b"

        if os.name == "nt":
            pytest.skip("the ps-based sampler is POSIX-only")
        assert (host_footprint(os.getpid()) or 0) > 0

    @pytest.mark.skipif(not sys.platform.startswith("linux"), reason="procfs is Linux-only")
    def test_footprint_reads_procfs_on_linux_without_spawning(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        import subprocess

        import vienetts_app.core.qwen_engine as qwen_engine

        def no_spawn(*_args: Any, **_kwargs: Any) -> Any:
            raise AssertionError("host_footprint spawned a process on Linux")

        monkeypatch.setattr(subprocess, "run", no_spawn)
        # Real procfs: the sampler's own process is always resident.
        assert (host_footprint(os.getpid()) or 0) > 0

        # statm's second field is resident pages.
        (tmp_path / "4242").mkdir()
        (tmp_path / "4242" / "statm").write_text("1000 25 10 1 0 50 0\n")
        monkeypatch.setattr(qwen_engine, "PROC_ROOT", tmp_path)
        assert host_footprint(4242) == 25 * os.sysconf("SC_PAGE_SIZE")
        # A process that is gone is unknown, not a spawn.
        assert host_footprint(4343) is None


# --------------------------------------------------------------------------- #
# live prefetch (Task 4.4)
# --------------------------------------------------------------------------- #


def _synthesized(tmp_path: Path) -> list[str]:
    return [str(entry["job"]) for entry in received(tmp_path, "synthesize")]


class TestLivePrefetch:
    """Live segments stay one protocol job each; the next one is sent early."""

    def test_the_next_segment_starts_when_the_previous_one_settles(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "ok")
        stream = engine.infer_stream_prefetched(
            ["one", "two", "three"], language="en", speaker="Ryan", job_ids=["j:1", "j:2", "j:3"]
        )
        assert next(stream)[0] == 0
        # The consumer still holds segment 0's first chunk, yet the host has
        # already been handed segment 1: its terminal settled segment 0.
        wait_for(lambda: len(_synthesized(tmp_path)) == 2, what="the prefetched segment")
        # ... and nothing further: one job ahead of the consumer, never two.
        time.sleep(0.3)
        assert _synthesized(tmp_path) == ["j:1", "j:2"]
        assert [index for index, _chunk in stream] == [0, 1, 1, 2, 2]
        assert [entry["fields"]["text"] for entry in received(tmp_path, "synthesize")] == [
            "one",
            "two",
            "three",
        ]
        assert engine.is_initialized is True

    def test_a_pre_start_cancel_sends_nothing(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "ok")
        engine.initialize()
        assert engine.cancel("p:1") is False
        with pytest.raises(QwenEngineCancelled):
            list(engine.infer_stream_prefetched(["a", "b"], language="en", job_ids=["p:1", "p:2"]))
        assert _synthesized(tmp_path) == []

    def test_cancel_stops_the_prefetched_segment_too(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "cancel_second")
        stream = engine.infer_stream_prefetched(
            ["one", "two", "three"], language="en", job_ids=["c:1", "c:2", "c:3"]
        )
        assert next(stream)[0] == 0
        wait_for(lambda: len(_synthesized(tmp_path)) == 2, what="the prefetched segment")
        # The caller cancels by any job of the sequence; the host's running
        # job is the prefetched one, so that is the one asked to stop.
        assert engine.cancel("c:1") is True
        assert [str(entry["job"]) for entry in received(tmp_path, "cancel")] == ["c:2"]
        with pytest.raises(QwenEngineCancelled):
            list(stream)
        assert _synthesized(tmp_path) == ["c:1", "c:2"]
        assert engine.is_initialized is True, "a cancel must not kill the host"

    def test_abandoning_the_stream_cancels_the_prefetched_segment(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "cancel_second")
        stream = engine.infer_stream_prefetched(
            ["one", "two", "three"], language="en", job_ids=["a:1", "a:2", "a:3"]
        )
        assert next(stream)[0] == 0
        wait_for(lambda: len(_synthesized(tmp_path)) == 2, what="the prefetched segment")
        stream.close()
        wait_for(
            lambda: [str(entry["job"]) for entry in received(tmp_path, "cancel")] == ["a:2"],
            what="the abandoned prefetch's cancel",
        )
        assert _synthesized(tmp_path) == ["a:1", "a:2"]
        assert engine.is_initialized is True

    def test_a_host_crash_in_the_prefetched_segment_fails_like_today(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "crash_second")
        seen: list[int] = []
        with pytest.raises(QwenEngineError) as failure:
            for index, _chunk in engine.infer_stream_prefetched(
                ["one", "two", "three"], language="en", job_ids=["x:1", "x:2", "x:3"]
            ):
                seen.append(index)
        # Segment 0 is delivered whole and in order before segment 1's partial
        # chunk and the crash — the same message a per-segment job raises.
        assert seen == [0, 0, 1]
        assert not isinstance(failure.value, QwenEngineCancelled)
        assert "boom: the host died mid-job" in str(failure.value)
        assert "exited with status 3" in str(failure.value)
        assert _synthesized(tmp_path) == ["x:1", "x:2"]
        assert engine.is_initialized is False

    def test_a_bloated_host_is_recycled_instead_of_handed_the_next_segment(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        samples = iter([1_000, 1_000])  # baseline + segment 0's pre-start check

        def footprint(_pid: int) -> int:
            return next(samples, 1_000 + 4 * 1024**3)

        engine = start_engine(
            engines, tmp_path, "ok", footprint=footprint, rss_growth_recycle_bytes=1024**3
        )
        got = [
            index
            for index, _chunk in engine.infer_stream_prefetched(
                ["one", "two", "three"], language="en", job_ids=["b:1", "b:2", "b:3"]
            )
        ]
        assert got == [0, 0, 1, 1, 2, 2]
        hosts: list[list[str]] = []
        for entry in host_log(tmp_path):
            if entry["event"] == "start":
                hosts.append([])
            elif entry.get("type") == "synthesize":
                hosts[-1].append(str(entry["job"]))
        # The bloated host finished segment 0 and was retired; the fresh one
        # (whose baseline is its own footprint) prefetches again.
        assert hosts == [["b:1"], ["b:2", "b:3"]]

    def test_the_provider_prefetches_and_cancels_by_worker_job(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "cancel_second")
        provider = QwenEngineProvider(engine)
        context = context_for(QWEN_CUSTOM, language="en", voice_id="Ryan")
        stream = provider.infer_stream_prefetched(
            ["one", "two", "three"], context=context, job_id="worker-9"
        )
        assert next(stream)[0] == 0
        wait_for(lambda: len(_synthesized(tmp_path)) == 2, what="the prefetched segment")
        first, second = _synthesized(tmp_path)
        assert first.startswith("worker-9:") and second.startswith("worker-9:")
        assert first != second, "every segment keeps its own protocol job id"
        assert received(tmp_path, "synthesize")[0]["fields"]["speaker"] == "Ryan"
        assert provider.cancel("worker-9") is True
        assert [str(entry["job"]) for entry in received(tmp_path, "cancel")] == [second]
        with pytest.raises(QwenEngineCancelled):
            list(stream)

    def test_the_provider_honours_a_cancel_before_the_first_segment(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "ok")
        provider = QwenEngineProvider(engine)
        context = context_for(QWEN_CUSTOM, language="en", voice_id="Ryan")
        assert provider.cancel("worker-early") is False
        with pytest.raises(QwenEngineCancelled):
            list(
                provider.infer_stream_prefetched(["a", "b"], context=context, job_id="worker-early")
            )
        assert _synthesized(tmp_path) == []
