"""Parent-side tests for the managed qwentts.cpp engine (Task 4.2).

``QwenGgufEngine`` owns one ``vienetts-qwen-gguf-host`` subprocess: spawn in a
sanitized offline environment with the runtime pack as the child's cwd (ggml
backend discovery), the framed handshake/load/stream/cancel/reap lifecycle,
and the shared SessionState transitions. The scripted fake child
(:mod:`tests.unit.qwen_gguf_host_fake`) exercises every path without a native
library or a checkpoint — including the GGUF load contract: a load frame
without ``format="gguf"`` is answered with an error.
"""

from __future__ import annotations

import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from tests.unit import qwen_gguf_host_fake as gguf_fake

from vienetts_app.core.engine import EngineProviderError, EngineProviders
from vienetts_app.core.qwen_engine import QwenEngineCancelled, QwenEngineError
from vienetts_app.core.qwen_gguf_engine import (
    GGUF_HOST_FLAG,
    GGUF_HOST_MODULE,
    QwenGgufEngine,
    gguf_host_command,
)
from vienetts_app.core.synthesis_context import context_for


@pytest.fixture()
def engines() -> Iterator[list[QwenGgufEngine]]:
    created: list[QwenGgufEngine] = []
    yield created
    for engine in created:
        engine.close()


def start_engine(
    engines: list[QwenGgufEngine], tmp_path: Path, mode: str, **overrides: Any
) -> QwenGgufEngine:
    engine = gguf_fake.engine_for(tmp_path, mode, **overrides)
    engines.append(engine)
    return engine


host_log = gguf_fake.host_log
received = gguf_fake.received
emitted = gguf_fake.emitted
host_pid = gguf_fake.host_pid
host_cwd = gguf_fake.host_cwd
pid_alive = gguf_fake.pid_alive
wait_for = gguf_fake.wait_for
terminals_for = gguf_fake.terminals_for
pcm_after_terminal = gguf_fake.pcm_after_terminal


class StreamRun:
    """Consume ``infer_stream`` on a thread so cancel/close can race it."""

    def __init__(
        self, engine: QwenGgufEngine, job_id: str, text: str = "hello", **kwargs: Any
    ) -> None:
        self.job_id = job_id
        self.chunks: list[np.ndarray] = []
        self.error: BaseException | None = None
        self._done = threading.Event()
        self._thread = threading.Thread(
            target=self._consume, args=(engine, text, kwargs), daemon=True
        )
        self._thread.start()

    def _consume(self, engine: QwenGgufEngine, text: str, kwargs: dict[str, Any]) -> None:
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


# --------------------------------------------------------------------------- #
# command + spawn contract
# --------------------------------------------------------------------------- #


class TestHostCommand:
    def test_command_is_shell_free_and_runs_the_gguf_host_module(self) -> None:
        command = gguf_host_command()
        assert command == [sys.executable, "-m", GGUF_HOST_MODULE]
        assert command[-1] == "vienetts_app.workers.qwen_gguf_host"
        assert all(isinstance(part, str) for part in command)

    def test_the_flag_exists_for_frozen_re_dispatch(self) -> None:
        assert GGUF_HOST_FLAG == "--qwen-gguf-host"
        assert GGUF_HOST_FLAG != "--qwen-host"

    def test_the_child_spawns_inside_the_runtime_pack(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        # ggml discovers its backend modules relative to the process cwd —
        # the pack directory is the only place they are guaranteed to sit.
        pack, _talker, _codec = gguf_fake.gguf_paths(tmp_path)
        engine = start_engine(engines, tmp_path, "ok")
        engine.initialize()
        assert Path(host_cwd(tmp_path)) == pack
        engine.close()


# --------------------------------------------------------------------------- #
# load contract validation
# --------------------------------------------------------------------------- #


class TestLoadContract:
    def test_load_carries_the_gguf_fields(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        pack, talker, codec = gguf_fake.gguf_paths(tmp_path)
        engine = start_engine(engines, tmp_path, "ok", device="metal", quantization="Q4_K_M")
        engine.initialize()
        (load,) = received(tmp_path, "load")
        fields = load["fields"]
        assert fields["profile"] == "customvoice"
        assert fields["format"] == "gguf"
        assert fields["quantization"] == "Q4_K_M"
        assert fields["device"] == "metal"
        assert fields["runtimeDir"] == str(pack)
        assert fields["talkerPath"] == str(talker)
        assert fields["codecPath"] == str(codec)
        # No official-load fields may leak onto the GGUF wire.
        for official_key in ("modelDir", "sharedDir", "dtype", "attention"):
            assert official_key not in fields
        engine.close()

    def test_base_profile_reports_clone_support(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "ok", profile="qwen_base_0_6b")
        engine.initialize()
        capabilities = engine.capabilities()
        assert capabilities.profile == "qwen_base_0_6b"
        assert capabilities.supports_clone is True
        (load,) = received(tmp_path, "load")
        assert load["fields"]["profile"] == "base"
        engine.close()

    def test_mps_is_rejected_before_spawning(self, tmp_path: Path) -> None:
        # The wire speaks ggml's vocabulary: the PyTorch spelling must never
        # reach the native host (the parent maps mps -> metal itself).
        with pytest.raises(QwenEngineError, match="device"):
            gguf_fake.engine_for(tmp_path, "ok", device="mps")
        assert host_log(tmp_path) == []

    def test_an_unknown_device_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(QwenEngineError, match="device"):
            gguf_fake.engine_for(tmp_path, "ok", device="xpu")
        assert host_log(tmp_path) == []

    def test_an_unknown_quantization_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(QwenEngineError, match="quantization"):
            gguf_fake.engine_for(tmp_path, "ok", quantization="Q6_K")
        assert host_log(tmp_path) == []

    def test_a_missing_pack_dir_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(QwenEngineError, match="runtime"):
            gguf_fake.engine_for(tmp_path, "ok", runtime_dir="")
        assert host_log(tmp_path) == []

    def test_missing_model_paths_are_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(QwenEngineError, match="talker"):
            gguf_fake.engine_for(tmp_path, "ok", talker_path="")
        with pytest.raises(QwenEngineError, match="codec"):
            gguf_fake.engine_for(tmp_path, "ok", codec_path="")

    def test_an_unknown_profile_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(QwenEngineError, match="profile"):
            gguf_fake.engine_for(tmp_path, "ok", profile="vieneu")

    def test_a_load_failure_is_actionable_and_reaps(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "load_error")
        with pytest.raises(QwenEngineError, match="model directory is missing"):
            engine.initialize()
        assert engine.is_initialized is False
        assert pid_alive(host_pid(tmp_path)) is False

    def test_an_unavailable_backend_reports_the_code_without_a_fallback(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        # A pack whose backend cannot load reports RUNTIME_INCOMPLETE — the
        # fix is the install in Settings, never a silent device change.
        from vienetts_app.core.qwen_protocol import RUNTIME_INCOMPLETE_CODE

        engine = start_engine(engines, tmp_path, "runtime_incomplete")
        with pytest.raises(QwenEngineError, match="incomplete"):
            engine.initialize()
        assert engine.last_error_code() == RUNTIME_INCOMPLETE_CODE
        assert engine.is_initialized is False

    def test_an_oom_failure_is_reported_without_changing_the_selection(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "oom")
        with pytest.raises(QwenEngineError, match="out of memory"):
            list(engine.infer_stream("hi", language="en", job_id="job-oom"))
        assert engine.is_initialized is False  # fatal: the host poisoned itself
        # Restarting the job keeps the same selection — the error is reported,
        # never worked around by a different device or quantization.
        engine._command = gguf_fake.fake_host(tmp_path, "ok")
        assert list(engine.infer_stream("hi", language="en", job_id="job-ok"))
        loads = received(tmp_path, "load")
        assert [load["fields"]["device"] for load in loads] == ["cpu"] * len(loads)
        assert [load["fields"]["quantization"] for load in loads] == ["Q8_0"] * len(loads)


# --------------------------------------------------------------------------- #
# streaming + batch
# --------------------------------------------------------------------------- #


class TestInferStream:
    def test_streams_float32_chunks_and_settles_ok(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "ok")
        chunks = list(
            engine.infer_stream("hello world", language="en", speaker="Ryan", job_id="job-1")
        )
        assert len(chunks) == 2
        assert all(chunk.dtype == np.float32 for chunk in chunks)
        (synthesize,) = received(tmp_path, "synthesize")
        assert synthesize["fields"] == {
            "text": "hello world",
            "language": "en",
            "speaker": "Ryan",
        }
        assert terminals_for(tmp_path, "job-1") == ["ok"]

    def test_serial_batch_yields_segment_tagged_chunks(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        # The GGUF host runs batch segments serially (one native call each) —
        # valid batching, but never advertised as native parallel generation.
        engine = start_engine(engines, tmp_path, "ok")
        engine.initialize()
        seen = list(
            engine.infer_stream_many(
                ["one", "two", "three"], language="en", speaker="Ryan", job_id="job-b"
            )
        )
        assert [segment for segment, _chunk in seen] == [0, 1, 2]
        (batch,) = received(tmp_path, "synthesize_batch")
        assert batch["fields"]["texts"] == ["one", "two", "three"]

    def test_a_crash_mid_stream_yields_partial_pcm_then_raises(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "crash_after_pcm")
        chunks: list[np.ndarray] = []
        with pytest.raises(QwenEngineError, match="host"):
            for chunk in engine.infer_stream("hello", language="en", job_id="job-x"):
                chunks.append(chunk)
        assert len(chunks) == 1  # what arrived before the crash is yielded
        assert engine.is_initialized is False

    def test_malformed_host_output_is_fatal(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "garbage")
        with pytest.raises(QwenEngineError, match="malformed"):
            list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1"))
        assert engine.is_initialized is False

    def test_stale_frames_for_a_settled_job_are_dropped(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "stale")
        chunks = list(engine.infer_stream("hi", language="en", job_id="job-s"))
        assert len(chunks) == 2
        assert engine.is_initialized is True  # the late pcm did not break the session
        # The stale frame really was sent after the terminal (the fake logs
        # raw writes too) — and the next job still streams cleanly.
        assert pcm_after_terminal(tmp_path, "job-s") != []
        assert list(engine.infer_stream("again", language="en", job_id="job-2"))
        engine.close()

    def test_initialize_is_idempotent(self, tmp_path: Path, engines: list[QwenGgufEngine]) -> None:
        engine = start_engine(engines, tmp_path, "ok")
        engine.initialize()
        engine.initialize()
        assert len(received(tmp_path, "load")) == 1


# --------------------------------------------------------------------------- #
# cancellation
# --------------------------------------------------------------------------- #


class TestCancel:
    def test_cancel_gracefully_settles_the_running_job(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "graceful_cancel")
        run = StreamRun(engine, "job-cancel")
        wait_for(lambda: received(tmp_path, "synthesize"), what="the job to start")
        assert engine.cancel("job-cancel") is True
        run.assert_finished()
        assert isinstance(run.error, QwenEngineCancelled)
        assert run.chunks == []
        assert terminals_for(tmp_path, "job-cancel") == ["cancelled"]
        assert pcm_after_terminal(tmp_path, "job-cancel") == []
        assert engine.is_initialized is True  # a graceful stop keeps the host

    def test_cancel_before_the_first_chunk_is_still_honored(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "graceful_cancel")
        assert engine.cancel("job-early") is False  # nothing running yet
        run = StreamRun(engine, "job-early")
        run.assert_finished()
        assert isinstance(run.error, QwenEngineCancelled)
        assert received(tmp_path, "synthesize") == []  # never sent to the host
        assert engine.is_initialized is False  # not even spawned

    def test_cancel_during_a_cold_load_never_starts_the_generation(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "slow_load")
        run = StreamRun(engine, "job-loading")
        wait_for(lambda: bool(received(tmp_path, "load")), what="the host to start loading")
        assert engine.cancel("job-loading") is False
        run.assert_finished()
        assert isinstance(run.error, QwenEngineCancelled)
        assert received(tmp_path, "synthesize") == []

    def test_cancel_escalates_to_terminate_when_the_host_ignores_it(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "slow_cancel", cancel_timeout=0.3)
        run = StreamRun(engine, "job-stubborn")
        wait_for(lambda: received(tmp_path, "synthesize"), what="the job to start")
        pid = host_pid(tmp_path)
        assert engine.cancel("job-stubborn") is True
        run.assert_finished()
        assert isinstance(run.error, QwenEngineCancelled)
        assert engine.is_initialized is False
        wait_for(lambda: not pid_alive(pid), what="the stubborn host to be reaped")
        engine.initialize()  # the next job gets a clean host
        assert engine.is_initialized is True

    def test_cancel_escalates_to_kill_when_terminate_is_ignored(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        engine = start_engine(
            engines, tmp_path, "kill_required", cancel_timeout=0.3, kill_timeout=0.5
        )
        run = StreamRun(engine, "job-immortal")
        wait_for(lambda: received(tmp_path, "synthesize"), what="the job to start")
        pid = host_pid(tmp_path)
        assert engine.cancel("job-immortal") is True
        run.assert_finished()
        assert isinstance(run.error, QwenEngineCancelled)
        wait_for(lambda: not pid_alive(pid), what="the host that ignores SIGTERM to be killed")


# --------------------------------------------------------------------------- #
# close + restart + resource bounds
# --------------------------------------------------------------------------- #


class TestLifecycle:
    def test_close_sends_shutdown_and_reaps(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "ok")
        engine.initialize()
        pid = host_pid(tmp_path)
        engine.close()
        assert engine.is_initialized is False
        wait_for(lambda: not pid_alive(pid), what="the host to exit")

    def test_close_is_idempotent(self, tmp_path: Path, engines: list[QwenGgufEngine]) -> None:
        engine = start_engine(engines, tmp_path, "ok")
        engine.close()
        engine.initialize()
        engine.close()
        engine.close()

    def test_a_dead_host_restarts_lazily(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "crash_after_pcm")
        with pytest.raises(QwenEngineError):
            list(engine.infer_stream("hi", language="en", job_id="job-1"))
        assert engine.is_initialized is False
        # mode is per-spawn; flip the script's mode by rewriting the command
        engine._command = gguf_fake.fake_host(tmp_path, "ok")
        chunks = list(engine.infer_stream("hi", language="en", job_id="job-2"))
        assert chunks

    def test_at_most_one_model_owner_is_resident(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        # Restart must never leave two loaded hosts alive: the old child is
        # reaped before (never after) the new one loads.
        engine = start_engine(engines, tmp_path, "ok")
        engine.initialize()
        first = host_pid(tmp_path)
        engine.close()
        wait_for(lambda: not pid_alive(first), what="the first host to be reaped")
        engine.initialize()
        second = host_pid(tmp_path)
        assert second != first
        assert pid_alive(first) is False
        assert pid_alive(second) is True
        engine.close()
        assert (
            pid_alive(second) is False
            or wait_for(lambda: not pid_alive(second), timeout=3.0, what="final reap") is None
        )

    def test_handshake_timeout_reaps_a_silent_host(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "silent", handshake_timeout=0.3)
        with pytest.raises(QwenEngineError, match="timed out"):
            engine.initialize()
        assert engine.is_initialized is False


# --------------------------------------------------------------------------- #
# provider routing
# --------------------------------------------------------------------------- #


class TestProviderRouting:
    def test_the_engine_reports_the_native_engine_id(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "ok")
        assert engine.engine_id == "qwentts_cpp"

    def test_provider_for_routes_gguf_contexts_to_the_gguf_engine(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        from vienetts_app.core.qwen_engine import QwenEngineProvider
        from vienetts_app.core.qwen_variants import variant_for

        engine = start_engine(engines, tmp_path, "ok")
        provider = QwenEngineProvider(engine)
        assert provider.engine == "qwentts_cpp"
        providers = EngineProviders(by_profile={"qwen_custom_0_6b": provider}, default=provider)
        context = context_for(
            "qwen_custom_0_6b",
            language="en",
            voice_id="Ryan",
            variant=variant_for("qwen_custom_0_6b", model_format="gguf", quantization="Q8_0"),
        )
        assert providers.provider_for(context) is provider

    def test_provider_for_refuses_a_gguf_context_on_a_pytorch_provider(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        # A gguf-stamped context must never be silently served by the PyTorch
        # host — even if it slipped past the submission gate.
        from tests.unit import qwen_host_fake

        from vienetts_app.core.qwen_engine import QwenEngine, QwenEngineProvider
        from vienetts_app.core.qwen_variants import variant_for

        torch_engine = qwen_host_fake.engine_for(tmp_path, "ok")
        engines2: list[QwenEngine] = [torch_engine]
        try:
            provider = QwenEngineProvider(torch_engine)
            assert provider.engine == "pytorch"
            providers = EngineProviders(by_profile={"qwen_custom_0_6b": provider}, default=provider)
            context = context_for(
                "qwen_custom_0_6b",
                language="en",
                voice_id="Ryan",
                variant=variant_for("qwen_custom_0_6b", model_format="gguf", quantization="Q8_0"),
            )
            with pytest.raises(EngineProviderError, match="qwentts_cpp"):
                providers.provider_for(context)
        finally:
            for engine in engines2:
                engine.close()

    def test_provider_for_refuses_a_pytorch_context_on_the_gguf_engine(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        from vienetts_app.core.qwen_engine import QwenEngineProvider
        from vienetts_app.core.qwen_variants import variant_for

        engine = start_engine(engines, tmp_path, "ok")
        provider = QwenEngineProvider(engine)
        providers = EngineProviders(by_profile={"qwen_custom_0_6b": provider}, default=provider)
        context = context_for(
            "qwen_custom_0_6b",
            language="en",
            voice_id="Ryan",
            variant=variant_for("qwen_custom_0_6b", model_format="official"),
        )
        assert context.engine == "pytorch"
        with pytest.raises(EngineProviderError, match="pytorch"):
            providers.provider_for(context)

    def test_provider_for_accepts_a_context_less_legacy_job(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        # Jobs with no context at all (pre-profile-era payloads) still route
        # to the default provider — engine stamping is a Qwen-context field.
        from vienetts_app.core.qwen_engine import QwenEngineProvider

        engine = start_engine(engines, tmp_path, "ok")
        provider = QwenEngineProvider(engine)
        providers = EngineProviders(by_profile={"qwen_custom_0_6b": provider}, default=provider)
        assert providers.provider_for(None) is provider


# --------------------------------------------------------------------------- #
# provider-level segment batching
# --------------------------------------------------------------------------- #


class TestProviderSegments:
    def test_infer_stream_segments_batches_and_tags_global_indexes(
        self, tmp_path: Path, engines: list[QwenGgufEngine]
    ) -> None:
        from vienetts_app.core.qwen_engine import QwenEngineProvider

        engine = start_engine(engines, tmp_path, "ok")
        provider = QwenEngineProvider(engine, batch_bounds=(2, 10_000))
        provider.initialize()
        context = context_for("qwen_custom_0_6b", language="en", voice_id="Ryan")
        seen = list(
            provider.infer_stream_segments(["a", "b", "c"], context=context, job_id="job-seg")
        )
        assert [index for index, _chunk in seen] == [0, 1, 2]
        # bounds=(2, ...) split three segments into two protocol jobs
        batches = received(tmp_path, "synthesize_batch")
        assert len(batches) == 2
        provider.close()
