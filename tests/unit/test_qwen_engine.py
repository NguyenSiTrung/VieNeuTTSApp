"""Parent-side Qwen host adapter: spawn, handshake, streaming, cancel, reap.

The host itself is exercised through a scripted stand-in (``FAKE_HOST_SOURCE``)
so every lifecycle path — hangs, crashes, malformed output, OOM, stale
deliveries, partial PCM — is deterministic and torch-free.
"""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import numpy as np
import pytest
from tests.unit import qwen_host_fake as host_fake

from vienetts_app.core.engine_profiles import QWEN_BASE, QWEN_CUSTOM
from vienetts_app.core.qwen_engine import (
    ENGINE_PROFILE_KEYS,
    ClonePrompt,
    QwenEngine,
    QwenEngineCancelled,
    QwenEngineError,
    QwenEngineProvider,
    host_command,
    host_environment,
)
from vienetts_app.core.qwen_protocol import MAX_TEXT_CHARS
from vienetts_app.core.synthesis_context import SynthesisContext, context_for
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
    def test_command_is_shell_free_and_runs_the_host_module(self) -> None:
        command = host_command()
        assert command == [sys.executable, "-m", "vienetts_app.workers.qwen_host"]
        assert all(isinstance(part, str) for part in command)

    def test_environment_is_sanitized_offline_and_keeps_the_caller_env_intact(
        self, tmp_path: Path
    ) -> None:
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
        assert "PYTHONHOME" not in environment
        assert "PYTHONSTARTUP" not in environment
        entries = environment["PYTHONPATH"].split(os.pathsep)
        assert entries[0] == str(runtime_dir)
        assert "tmp/attacker" not in environment["PYTHONPATH"]
        assert any(entry.endswith("src") for entry in entries)
        assert base["PYTHONPATH"] == "/tmp/attacker"  # the input mapping is not mutated

    def test_environment_without_a_runtime_dir_still_reaches_the_app_package(self) -> None:
        environment = host_environment(None, {"PATH": "/usr/bin"})
        assert any(entry.endswith("src") for entry in environment["PYTHONPATH"].split(os.pathsep))


# --------------------------------------------------------------------------- #
# initialize
# --------------------------------------------------------------------------- #


class TestInitialize:
    def test_handshake_and_load_use_the_configured_selection(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
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

    def test_capabilities_require_initialization(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "ok")
        with pytest.raises(QwenEngineError, match="not initialized"):
            engine.capabilities()

    def test_unknown_profile_is_rejected_before_spawning(self, tmp_path: Path) -> None:
        with pytest.raises(QwenEngineError, match="profile"):
            engine_for(tmp_path, "ok", profile="vieneu")
        assert host_log(tmp_path) == []  # nothing was spawned

    def test_unknown_engine_id_is_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(QwenEngineError, match="unknown engine profile"):
            engine_for(tmp_path, "ok", profile="not_a_profile")
        assert host_log(tmp_path) == []

    def test_a_host_that_cannot_be_spawned_reports_why(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "ok", command=[str(tmp_path / "no-such-python")])
        with pytest.raises(QwenEngineError, match="could not start the Qwen model host"):
            engine.initialize()
        assert engine.is_initialized is False

    def test_handshake_timeout_reaps_a_silent_host(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "silent", handshake_timeout=0.4)
        with pytest.raises(QwenEngineError, match="hello"):
            engine.initialize()
        assert engine.is_initialized is False
        wait_for(lambda: not pid_alive(host_pid(tmp_path)), what="the host to be reaped")

    def test_a_foreign_host_build_is_rejected_at_the_handshake(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "wrong_handshake")
        with pytest.raises(QwenEngineError, match="before its handshake"):
            engine.initialize()
        assert engine.is_initialized is False
        wait_for(lambda: not pid_alive(host_pid(tmp_path)), what="the foreign host to be reaped")

    def test_load_failure_is_actionable_and_reaps_the_host(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "load_error")
        with pytest.raises(QwenEngineError, match="model directory is missing"):
            engine.initialize()
        assert engine.is_initialized is False
        wait_for(lambda: not pid_alive(host_pid(tmp_path)), what="the host to be reaped")

    def test_initialize_is_idempotent(self, tmp_path: Path, engines: list[QwenEngine]) -> None:
        engine = start_engine(engines, tmp_path, "ok")
        engine.initialize()
        pid = host_pid(tmp_path)
        engine.initialize()
        assert host_pid(tmp_path) == pid
        assert len([entry for entry in host_log(tmp_path) if entry["event"] == "start"]) == 1

    def test_a_dead_host_restarts_lazily(self, tmp_path: Path, engines: list[QwenEngine]) -> None:
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


class TestInferStream:
    def test_streams_float32_chunks_and_settles_ok(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
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

    def test_base_clone_fields_are_forwarded(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
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

    def test_progress_is_reported_without_breaking_the_stream(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
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

    def test_a_failed_terminal_raises_the_host_message(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "fail")
        with pytest.raises(QwenEngineError, match="the model said no"):
            list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1"))
        assert engine.is_initialized is True  # a non-fatal failure keeps the host

    def test_oversized_text_is_rejected_before_ipc(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "ok")
        with pytest.raises(QwenEngineError, match="segment"):
            list(
                engine.infer_stream(
                    "x" * (MAX_TEXT_CHARS + 1), language="en", speaker="Ryan", job_id="job-1"
                )
            )
        assert received(tmp_path, "synthesize") == []

    def test_infer_stream_initializes_lazily(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "ok")
        assert engine.is_initialized is False
        chunks = list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1"))
        assert len(chunks) == 2
        assert engine.is_initialized is True

    def test_a_generated_job_id_is_accepted(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "ok")
        assert len(list(engine.infer_stream("hello", language="en", speaker="Ryan"))) == 2

    def test_device_oom_is_fatal_and_the_next_job_restarts_the_host(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "oom")
        with pytest.raises(QwenEngineError, match="CUDA out of memory"):
            list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1"))
        assert engine.is_initialized is False
        engine.initialize()
        assert engine.is_initialized is True

    def test_a_crash_mid_stream_yields_partial_pcm_then_raises(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "crash_after_pcm")
        chunks: list[np.ndarray] = []
        with pytest.raises(QwenEngineError) as failure:
            for chunk in engine.infer_stream(
                "hello", language="en", speaker="Ryan", job_id="job-1"
            ):
                chunks.append(chunk)
        assert len(chunks) == 1  # the partial audio the caller must discard
        assert "boom: the host died mid-job" in str(failure.value)
        assert engine.is_initialized is False

    def test_a_hung_generation_times_out_and_is_reaped(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "hang_synthesize", frame_timeout=0.4)
        with pytest.raises(QwenEngineError, match="timed out"):
            list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1"))
        assert engine.is_initialized is False
        wait_for(lambda: not pid_alive(host_pid(tmp_path)), what="the hung host to be reaped")

    def test_malformed_host_output_is_fatal(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "garbage")
        with pytest.raises(QwenEngineError, match="malformed"):
            list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1"))
        assert engine.is_initialized is False

    def test_stale_frames_for_a_settled_job_are_dropped(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
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

    def test_abandoning_the_stream_cancels_the_job(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "slow_pcm")
        stream = engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1")
        next(stream)
        stream.close()
        wait_for(lambda: received(tmp_path, "cancel"), what="the cancel frame")
        (cancel,) = received(tmp_path, "cancel")
        assert cancel["job"] == "job-1"

    def test_a_second_job_while_one_runs_is_rejected(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "slow_pcm")
        stream = engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1")
        next(stream)
        # The host runs one job at a time; the second submission must fail loudly
        # instead of interleaving two streams.
        with pytest.raises(QwenEngineError, match="could not start Qwen synthesis"):
            next(engine.infer_stream("again", language="en", speaker="Ryan", job_id="job-2"))
        stream.close()

    def test_a_protocol_violation_from_the_host_is_fatal(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "unknown_job")
        with pytest.raises(QwenEngineError, match="violated the protocol"):
            list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1"))
        assert engine.is_initialized is False


# --------------------------------------------------------------------------- #
# cancel
# --------------------------------------------------------------------------- #


class TestCancel:
    def test_cancel_for_an_unknown_job_reports_nothing_to_stop(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "ok")
        engine.initialize()
        assert engine.cancel("never-started") is False
        assert received(tmp_path, "cancel") == []

    def test_cancel_gracefully_settles_the_running_job(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "graceful_cancel")
        run = StreamRun(engine, "job-cancel")
        wait_for(lambda: received(tmp_path, "synthesize"), what="the job to start")
        assert engine.cancel("job-cancel") is True
        run.assert_finished()
        assert isinstance(run.error, QwenEngineCancelled)
        assert run.chunks == []
        assert engine.is_initialized is True  # a graceful stop keeps the host

    def test_cancel_escalates_to_terminate_when_the_host_ignores_it(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "slow_cancel", cancel_timeout=0.3)
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

    def test_cancel_before_the_first_chunk_is_still_honored(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "graceful_cancel")
        # The request lands before the generator has sent `synthesize`.
        assert engine.cancel("job-early") is False
        run = StreamRun(engine, "job-early")
        run.assert_finished()
        assert isinstance(run.error, QwenEngineCancelled)
        assert received(tmp_path, "cancel") != []

    def test_cancel_escalates_to_kill_when_terminate_is_ignored(
        self, tmp_path: Path, engines: list[QwenEngine]
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

    def test_cancel_on_a_host_that_already_died_reports_the_job_stopped(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
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
    def test_close_sends_shutdown_and_reaps(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "ok")
        engine.initialize()
        pid = host_pid(tmp_path)
        engine.close()
        assert engine.is_initialized is False
        assert received(tmp_path, "shutdown") != []
        wait_for(lambda: not pid_alive(pid), what="the host to exit")
        engine.close()  # idempotent
        assert len(received(tmp_path, "shutdown")) == 1

    def test_close_while_a_job_runs_terminates_instead_of_shutting_down(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "hang_synthesize")
        run = StreamRun(engine, "job-open")
        wait_for(lambda: received(tmp_path, "synthesize"), what="the job to start")
        pid = host_pid(tmp_path)
        engine.close()
        run.assert_finished()
        assert isinstance(run.error, QwenEngineCancelled)  # the app is shutting down
        assert received(tmp_path, "shutdown") == []
        wait_for(lambda: not pid_alive(pid), what="the busy host to be reaped")

    def test_stderr_is_drained_and_kept_as_a_bounded_tail(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
        engine = start_engine(engines, tmp_path, "noisy")
        list(engine.infer_stream("hello", language="en", speaker="Ryan", job_id="job-1"))
        wait_for(lambda: "host log line 39" in engine.stderr_tail(), what="stderr to drain")
        assert len(engine.stderr_tail().splitlines()) <= 20  # bounded, not the whole history

    def test_stderr_tail_is_empty_before_anything_is_written(
        self, tmp_path: Path, engines: list[QwenEngine]
    ) -> None:
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
    def test_context_maps_language_and_speaker(
        self, tmp_path: Path, provider_engines: list[QwenEngine]
    ) -> None:
        provider = provider_for(provider_engines, tmp_path)
        chunks = list(provider.infer_stream("你好。", context=custom_context(), job_id="job-1"))
        assert len(chunks) == 2
        assert all(chunk.dtype == np.float32 for chunk in chunks)
        (synthesize,) = received(tmp_path, "synthesize")
        assert synthesize["fields"] == {
            "text": "你好。",
            "language": "Chinese",
            "speaker": "Vivian",
        }
        # The 0.6B host samples with its own settings: no temperature is sent.
        assert "temperature" not in synthesize["fields"]

    def test_auto_language_maps_to_the_model_name(
        self, tmp_path: Path, provider_engines: list[QwenEngine]
    ) -> None:
        provider = provider_for(provider_engines, tmp_path)
        list(
            provider.infer_stream("hello", context=custom_context(language="auto"), job_id="job-1")
        )
        (synthesize,) = received(tmp_path, "synthesize")
        assert synthesize["fields"]["language"] == "Auto"

    def test_a_clone_context_uses_the_resolved_prompt(
        self, tmp_path: Path, provider_engines: list[QwenEngine]
    ) -> None:
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

    def test_an_unresolvable_clone_is_actionable(
        self, tmp_path: Path, provider_engines: list[QwenEngine]
    ) -> None:
        provider = provider_for(provider_engines, tmp_path, profile="qwen_base_0_6b")
        with pytest.raises(QwenEngineError, match="cannot be resolved"):
            list(provider.infer_stream("hello", context=clone_context(), job_id="job-1"))
        assert received(tmp_path, "synthesize") == []  # nothing crossed IPC

    def test_a_missing_resolver_reports_the_same_way(
        self, tmp_path: Path, provider_engines: list[QwenEngine]
    ) -> None:
        provider = provider_for(
            provider_engines,
            tmp_path,
            profile="qwen_base_0_6b",
            clone_prompt_for=lambda clone_id: None,
        )
        with pytest.raises(QwenEngineError, match="clone store has no reference clip"):
            list(provider.infer_stream("hello", context=clone_context(), job_id="job-1"))

    def test_every_segment_gets_its_own_protocol_job_id(
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

    def test_cancel_targets_the_running_segment(
        self, tmp_path: Path, provider_engines: list[QwenEngine]
    ) -> None:
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

    def test_cancel_before_the_first_segment_is_still_honored(
        self, tmp_path: Path, provider_engines: list[QwenEngine]
    ) -> None:
        provider = provider_for(provider_engines, tmp_path, mode="graceful_cancel")
        assert provider.cancel("job-early") is False  # nothing running yet
        run = ProviderRun(provider, "job-early", custom_context())
        run.assert_finished()
        assert isinstance(run.error, QwenEngineCancelled)
        assert received(tmp_path, "cancel") != []

    def test_cancel_for_an_unknown_job_sends_nothing(
        self, tmp_path: Path, provider_engines: list[QwenEngine]
    ) -> None:
        provider = provider_for(provider_engines, tmp_path)
        assert provider.cancel("never-started") is False
        assert received(tmp_path, "cancel") == []

    def test_a_context_is_required(
        self, tmp_path: Path, provider_engines: list[QwenEngine]
    ) -> None:
        provider = provider_for(provider_engines, tmp_path)
        with pytest.raises(QwenEngineError, match="must carry its engine context"):
            list(provider.infer_stream("hello", job_id="job-1"))

    def test_a_mismatched_context_is_rejected(
        self, tmp_path: Path, provider_engines: list[QwenEngine]
    ) -> None:
        provider = provider_for(provider_engines, tmp_path)  # customvoice engine
        with pytest.raises(QwenEngineError, match="serves 'qwen_custom_0_6b'"):
            list(provider.infer_stream("hello", context=clone_context(), job_id="job-1"))

    def test_oversized_segments_are_rejected_before_ipc(
        self, tmp_path: Path, provider_engines: list[QwenEngine]
    ) -> None:
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
