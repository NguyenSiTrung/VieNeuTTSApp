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


def test_terminal_rejects_a_failed_result_without_error() -> None:
    with pytest.raises(ValueError, match="failed"):
        JobTerminal(job_id="a" * 32, owner="text", state="failed")


def test_job_is_frozen() -> None:
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


def test_voice_op_produces_valid_immutable_job() -> None:
    job = new_synthesis_job(
        "cloning",
        "voice_op",
        VoiceOp(op="add", name="Clone", clip_path="/tmp/clip.wav"),
    )
    assert job.id
    assert isinstance(job.request, VoiceOp)
    with pytest.raises(dataclasses.FrozenInstanceError):
        job.owner = "text"  # type: ignore[misc]


def test_terminal_error_invariants() -> None:
    with pytest.raises(ValueError):
        JobTerminal(job_id="a" * 32, owner="text", state="completed", error="boom")
    with pytest.raises(ValueError):
        JobTerminal(job_id="a" * 32, owner="text", state="cancelled", error="boom")
    with pytest.raises(ValueError):
        JobTerminal(job_id="a" * 32, owner="text", state="superseded", error="boom")
    terminal = JobTerminal(job_id="a" * 32, owner="text", state="failed", error="engine exploded")
    assert terminal.error == "engine exploded"


def test_artifact_path_coerced_to_path() -> None:
    job = new_synthesis_job(
        "text",
        "interactive",
        TTSRequest(text="hi"),
        artifact_path="out.wav",
    )
    assert job.artifact_path == Path("out.wav")


def test_chunk_rejects_raw_pcm_samples() -> None:
    with pytest.raises(TypeError):
        JobChunk(job_id="a" * 32, samples=np.zeros(1, dtype=np.float32))  # type: ignore[call-arg]
