"""Immutable inference job contract (Phase 2 Task 1).

Value objects only: no Qt, no worker threading.
"""

from __future__ import annotations

import dataclasses
import re
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Literal

if TYPE_CHECKING:
    from vienetts_app.core.pcm_transport import BoundedPcmTransport

from vienetts_app.core.models import TTSRequest, VoiceOp

JobOwner = Literal["text", "paragraph", "audiobook", "cloning"]
JobKind = Literal["interactive", "requested_chapter", "prefetch", "bulk", "voice_op"]
JobTerminalState = Literal["completed", "cancelled", "failed", "superseded"]
JobRequest = TTSRequest | VoiceOp

_OWNERS = frozenset(("text", "paragraph", "audiobook", "cloning"))
_KINDS = frozenset(("interactive", "requested_chapter", "prefetch", "bulk", "voice_op"))
_TERMINALS = frozenset(("completed", "cancelled", "failed", "superseded"))
_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def _check_job_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("job id must be a non-empty, non-blank string")
    if _ID_RE.fullmatch(value) is None:
        raise ValueError("job id must be UUID-hex-shaped (32 hex chars)")
    return value


@dataclass(frozen=True)
class SynthesisJob:
    id: str
    owner: JobOwner
    kind: JobKind
    priority: int
    request: JobRequest
    artifact_path: Path | None = None
    cache_fingerprint: str | None = None
    live_transport: BoundedPcmTransport | None = field(default=None, compare=False, repr=False)

    def __post_init__(self) -> None:
        _check_job_id(self.id)
        if self.owner not in _OWNERS:
            raise ValueError(f"owner must be one of {sorted(_OWNERS)}, got {self.owner!r}")
        if self.kind not in _KINDS:
            raise ValueError(f"kind must be one of {sorted(_KINDS)}, got {self.kind!r}")
        if not isinstance(self.priority, int) or isinstance(self.priority, bool):
            raise ValueError(f"priority must be a non-negative integer, got {self.priority!r}")
        if self.priority < 0:
            raise ValueError(f"priority must be a non-negative integer, got {self.priority!r}")
        if not isinstance(self.request, (TTSRequest, VoiceOp)):
            raise ValueError(
                f"request must be TTSRequest or VoiceOp, got {type(self.request).__name__}"
            )
        if isinstance(self.request, TTSRequest) and self.request.job_id not in (None, self.id):
            raise ValueError("TTSRequest.job_id must match the enclosing SynthesisJob id")
        if isinstance(self.artifact_path, str):
            object.__setattr__(self, "artifact_path", Path(self.artifact_path))
        if self.artifact_path is not None and not isinstance(self.artifact_path, Path):
            raise ValueError("artifact_path must be a Path, string, or None")
        if self.cache_fingerprint is not None and (
            not isinstance(self.cache_fingerprint, str) or not self.cache_fingerprint.strip()
        ):
            raise ValueError("cache_fingerprint must be a non-blank string or None")


@dataclass(frozen=True)
class JobProgress:
    job_id: str
    done: int
    total: int
    stage: str

    def __post_init__(self) -> None:
        _check_job_id(self.job_id)
        for field_name in ("done", "total"):
            value = getattr(self, field_name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 0:
                raise ValueError(f"{field_name} must be a non-negative integer")
        if not isinstance(self.stage, str) or not self.stage.strip():
            raise ValueError("stage must be a non-empty string")


@dataclass(frozen=True)
class JobChunk:
    job_id: str
    sample_count: int
    peak: float

    def __post_init__(self) -> None:
        _check_job_id(self.job_id)
        if (
            not isinstance(self.sample_count, int)
            or isinstance(self.sample_count, bool)
            or self.sample_count < 0
        ):
            raise ValueError("sample_count must be a non-negative integer")
        if not isinstance(self.peak, (int, float)) or isinstance(self.peak, bool):
            raise ValueError("peak must be a number")


@dataclass(frozen=True)
class JobTerminal:
    job_id: str
    owner: JobOwner
    state: JobTerminalState
    value: object | None = None
    error: str = ""

    def __post_init__(self) -> None:
        _check_job_id(self.job_id)
        if self.owner not in _OWNERS:
            raise ValueError(f"owner must be one of {sorted(_OWNERS)}, got {self.owner!r}")
        if self.state not in _TERMINALS:
            raise ValueError(f"state must be one of {sorted(_TERMINALS)}, got {self.state!r}")
        if not isinstance(self.error, str):
            raise ValueError("error must be a string")
        if self.state == "failed":
            if not self.error.strip():
                raise ValueError("failed terminal requires a nonblank error")
        elif self.error != "":
            raise ValueError(f"{self.state} terminal must not carry an error")


def new_synthesis_job(
    owner: JobOwner,
    kind: JobKind,
    request: JobRequest,
    *,
    priority: int = 0,
    artifact_path: Path | None = None,
    cache_fingerprint: str | None = None,
) -> SynthesisJob:
    job_id = uuid.uuid4().hex
    if isinstance(request, TTSRequest):
        if request.job_id not in (None, job_id):
            raise ValueError("TTSRequest.job_id must match the enclosing SynthesisJob id")
        request = dataclasses.replace(request, job_id=job_id)
    return SynthesisJob(
        id=job_id,
        owner=owner,
        kind=kind,
        priority=priority,
        request=request,
        artifact_path=artifact_path,
        cache_fingerprint=cache_fingerprint,
    )
