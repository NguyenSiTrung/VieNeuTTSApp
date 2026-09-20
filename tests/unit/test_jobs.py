"""Immutable job contract (Phase 2 Task 1, TDD RED)."""

import dataclasses
from pathlib import Path

import numpy as np
import pytest

from vienetts_app.core.jobs import JobChunk, JobTerminal, SynthesisJob, new_synthesis_job
from vienetts_app.core.models import TTSRequest, VoiceOp


def test_factory_copies_its_id_into_a_tts_request() -> None:
    job = new_synthesis_job(
        "text",
        "interactive",
        TTSRequest(text="Xin chào", mode="stream"),
    )

    assert job.id
    assert isinstance(job.request, TTSRequest)
    assert job.request.job_id == job.id


def test_job_rejects_mismatched_nested_request_id() -> None:
    with pytest.raises(ValueError, match="must match"):
        SynthesisJob(
            id="a" * 32,
            owner="text",
            kind="interactive",
            priority=0,
            request=TTSRequest(text="Xin chào", job_id="b" * 32),
        )


def test_terminal_rejects_failed_without_error_and_job_is_frozen() -> None:
    with pytest.raises(ValueError, match="failed"):
        JobTerminal(job_id="a" * 32, owner="text", state="failed")

    job = new_synthesis_job("text", "interactive", TTSRequest(text="hi"))
    with pytest.raises(dataclasses.FrozenInstanceError):
        job.priority = 1  # type: ignore[misc]


def test_job_rejects_bad_owner_kind_priority() -> None:
    with pytest.raises(ValueError):
        new_synthesis_job("unknown", "interactive", TTSRequest(text="hi"))
    with pytest.raises(ValueError):
        new_synthesis_job("text", "unknown", TTSRequest(text="hi"))
    with pytest.raises(ValueError):
        new_synthesis_job("text", "interactive", TTSRequest(text="hi"), priority=-1)


def test_voice_op_and_artifact_and_chunk_invariants() -> None:
    job = new_synthesis_job(
        "cloning",
        "voice_op",
        VoiceOp(op="add", name="Clone", clip_path="/tmp/clip.wav"),
    )
    assert job.id
    assert isinstance(job.request, VoiceOp)
    with pytest.raises(dataclasses.FrozenInstanceError):
        job.owner = "text"  # type: ignore[misc]

    job = new_synthesis_job(
        "text",
        "interactive",
        TTSRequest(text="hi"),
        artifact_path="out.wav",
    )
    assert job.artifact_path == Path("out.wav")

    with pytest.raises(TypeError):
        JobChunk(job_id="a" * 32, samples=np.zeros(1, dtype=np.float32))  # type: ignore[call-arg]


def test_terminal_error_invariants() -> None:
    with pytest.raises(ValueError):
        JobTerminal(job_id="a" * 32, owner="text", state="completed", error="boom")
    with pytest.raises(ValueError):
        JobTerminal(job_id="a" * 32, owner="text", state="cancelled", error="boom")
    with pytest.raises(ValueError):
        JobTerminal(job_id="a" * 32, owner="text", state="superseded", error="boom")
    terminal = JobTerminal(job_id="a" * 32, owner="text", state="failed", error="engine exploded")
    assert terminal.error == "engine exploded"


def test_job_exposes_the_request_context_or_none() -> None:
    from vienetts_app.core import engine_profiles as ep
    from vienetts_app.core.synthesis_context import context_for

    context = context_for(ep.QWEN_CUSTOM, language="zh", voice_id="Vivian")
    job = new_synthesis_job(
        "text",
        "interactive",
        TTSRequest(text="你好", voice="Vivian", context=context),
    )
    assert job.context is context

    legacy = new_synthesis_job("text", "interactive", TTSRequest(text="Xin chào"))
    assert legacy.context is None

    voice_job = new_synthesis_job("cloning", "voice_op", VoiceOp(op="remove", name="V"))
    assert voice_job.context is None


def test_factory_can_stamp_a_context_at_admission() -> None:
    from vienetts_app.core import engine_profiles as ep
    from vienetts_app.core.synthesis_context import context_for

    context = context_for(ep.QWEN_CUSTOM, language="en", voice_id="Ryan")
    job = new_synthesis_job(
        "text",
        "interactive",
        TTSRequest(text="Hello"),
        context=context,
    )
    assert job.context is context
    assert job.request.context is context
    assert job.request.job_id == job.id
    assert job.request.voice is None  # the context carries the identity

    # Stamping over an existing, divergent context is a programming error.
    with pytest.raises(ValueError, match="voice"):
        new_synthesis_job(
            "text",
            "interactive",
            TTSRequest(text="Hello", voice="Vivian", context=context),
        )


def test_voice_op_jobs_reject_a_context() -> None:
    from vienetts_app.core import engine_profiles as ep
    from vienetts_app.core.synthesis_context import context_for

    with pytest.raises(TypeError, match="context"):
        new_synthesis_job(
            "cloning",
            "voice_op",
            VoiceOp(op="remove", name="V"),
            context=context_for(ep.VIENEU),
        )
