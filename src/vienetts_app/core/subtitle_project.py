"""SRT subtitle workspace: stream a cue-aligned track to disk, then persist it.

Layout under the subtitles root (``<data_dir>/subtitles``)::

    <project_id>/project.json           inputs: cues, fit policy, fingerprint,
                                        final stats
    <project_id>/track.wav              rendered audio, streamed cue by cue
    <project_id>/track.timeline.json    measured cue↔time map (karaoke playback)

A subtitle render is cue-by-cue synthesis placed on the SRT clock, so it is
built exactly the way playback consumes it: the controller synthesizes one cue
(or one merged speech unit), hands the clip to :class:`SubtitleTrackRenderer`,
and the renderer plans, stretches, pads the gap and writes. Nothing ever holds
the whole track in RAM — a two-hour film is ~1.4 GB as float32 — so the WAV is
written incrementally (:class:`core.audio.StreamingWavWriter`) and silence is
emitted in bounded chunks.

``project_id`` is derived from the subtitle file's name plus its spoken text, so
re-importing the same file resumes the same workspace. ``fingerprint`` covers
everything that changes the audio (cues, policy, sample rate, voice, and the
engine identity that produced it); when it still matches and ``track.wav``
exists, the render is reused instead of redone (NFR-A1: never re-synthesize
what is cached), and a render made by another engine/profile/language is
invalidated rather than silently adopted (Phase 5 Task 5.3).

Every read degrades instead of crashing (corrupt project → ``None``), and all
writes are atomic (temp file + rename), mirroring the audiobook workspace. A
half-written track is never promoted to ``track.wav``.
"""

from __future__ import annotations

import contextlib
import datetime as _dt
import hashlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from collections.abc import Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

from vienetts_app.core.align import (
    AlignmentStats,
    CueFit,
    FitPolicy,
    adjusted_cues,
    alignment_stats,
    frames_for_ms,
    ms_for_frames,
    plan_cue,
    stretch_clip_to,
    timeline_from_fits,
)
from vienetts_app.core.audio import DEFAULT_SAMPLE_RATE, StreamingWavWriter
from vienetts_app.core.subtitles import Cue, cues_text, format_srt
from vienetts_app.core.synthesis_context import (
    SynthesisContext,
    context_from_payload,
    context_matches,
)
from vienetts_app.core.timeline import Timeline, timeline_from_json, timeline_to_json

logger = logging.getLogger(__name__)

PROJECT_FILENAME = "project.json"
TRACK_FILENAME = "track.wav"
TRACK_TIMELINE_FILENAME = "track.timeline.json"
# Bump when the project.json schema changes incompatibly (load() refuses
# mismatched versions rather than guessing a migration).
PROJECT_VERSION = 1

#: PCM_16 halves the app's float renders (~690 MB for a 2-hour track instead of
#: ~1.4 GB) and every reader goes through ``read_wav``, which converts back.
TRACK_SUBTYPE = "PCM_16"

# Windows keeps a just-closed file briefly locked (AV/indexer); bounded retries
# ride out short holds before failing loudly (same posture as the audiobook store).
REPLACE_LOCK_ATTEMPTS = 4
REPLACE_LOCK_DELAY_S = 0.25

#: ``project_id_for`` output — a caller-supplied id that is anything else is
#: refused before it can become a path segment (no ``..``, no separators).
_PROJECT_ID_RE = re.compile(r"^[0-9a-f]{16}$")


class SubtitleProjectError(RuntimeError):
    """A subtitle workspace operation failed; message is user-actionable."""


# ── serialization ────────────────────────────────────────────────────────────


def _cue_to_json(cue: Cue) -> dict[str, Any]:
    return {"index": cue.index, "startMs": cue.start_ms, "endMs": cue.end_ms, "text": cue.text}


def _cue_from_json(data: Any) -> Cue | None:
    if not isinstance(data, dict):
        return None
    try:
        return Cue(
            index=int(data["index"]),
            start_ms=int(data["startMs"]),
            end_ms=int(data["endMs"]),
            text=str(data["text"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _cues_from_json(data: Any) -> tuple[Cue, ...] | None:
    if not isinstance(data, list):
        return None
    cues: list[Cue] = []
    for entry in data:
        cue = _cue_from_json(entry)
        if cue is None:
            return None
        cues.append(cue)
    return tuple(cues)


def _policy_to_json(policy: FitPolicy) -> dict[str, Any]:
    return {
        "mode": policy.mode,
        "rateCap": float(policy.rate_cap),
        "maxGapMs": int(policy.max_gap_ms),
        "offsetMs": int(policy.offset_ms),
        "mergeSentences": bool(policy.merge_sentences),
    }


def _policy_from_json(data: Any) -> FitPolicy | None:
    if not isinstance(data, dict):
        return None
    try:
        return FitPolicy(
            mode=str(data["mode"]),
            rate_cap=float(data["rateCap"]),
            max_gap_ms=int(data["maxGapMs"]),
            offset_ms=int(data["offsetMs"]),
            merge_sentences=bool(data["mergeSentences"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def _stats_to_json(stats: AlignmentStats) -> dict[str, Any]:
    return {
        "cues": stats.cues,
        "compressed": stats.compressed,
        "maxRate": float(stats.max_rate),
        "overflowed": stats.overflowed,
        "maxOverflowMs": stats.max_overflow_ms,
        "pushed": stats.pushed,
        "totalMs": stats.total_ms,
        "driftMs": stats.drift_ms,
    }


def _stats_from_json(data: Any) -> AlignmentStats | None:
    if not isinstance(data, dict):
        return None
    try:
        return AlignmentStats(
            cues=int(data["cues"]),
            compressed=int(data["compressed"]),
            max_rate=float(data["maxRate"]),
            overflowed=int(data["overflowed"]),
            max_overflow_ms=int(data["maxOverflowMs"]),
            pushed=int(data["pushed"]),
            total_ms=int(data["totalMs"]),
            drift_ms=int(data["driftMs"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def render_fingerprint(
    cues: Sequence[Cue],
    policy: FitPolicy,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    voice_key: str = "",
    context: SynthesisContext | None = None,
) -> str:
    """Stable hash of everything that changes the rendered audio.

    Two renders with the same fingerprint are byte-for-byte the same track, so
    a cached ``track.wav`` can be reused; any change to a cue, the fit policy,
    the sample rate, the voice or the engine identity invalidates it.

    ``context`` is omitted from the payload when absent, so a caller without an
    identity seam (a bare fake app, smoke scenarios) and every project written
    before engine provenance existed keep the fingerprint they always had.
    """
    payload = {
        "version": PROJECT_VERSION,
        "sampleRate": int(sample_rate),
        "voice": str(voice_key),
        "policy": _policy_to_json(policy),
        "cues": [_cue_to_json(cue) for cue in cues],
    }
    if context is not None:
        payload["context"] = context.fingerprint_payload()
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def project_id_for(source_path: str | Path, cues: Sequence[Cue]) -> str:
    """Workspace id for a subtitle file: name + spoken text (same content → same id)."""
    name = Path(source_path).name
    seed = f"{name}\n{cues_text(cues)}".encode()
    return hashlib.sha256(seed).hexdigest()[:16]


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


# ── project model ────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class SubtitleProject:
    """One subtitle workspace: the cues, how they were fitted, and the result."""

    id: str
    title: str
    source_path: str
    cues: tuple[Cue, ...]
    policy: FitPolicy
    sample_rate: int = DEFAULT_SAMPLE_RATE
    voice_key: str = ""
    fingerprint: str = ""
    #: Engine identity this track was rendered with (``None`` for a project
    #: that has never been rendered by an identified engine).
    context: SynthesisContext | None = None
    total_ms: int = 0
    stats: AlignmentStats | None = None
    created_at: str = ""
    #: The cues retimed to the rendered audio — what ``exportSrt`` writes and
    #: what a muxed-back track must use. Empty until the first render lands.
    adjusted: tuple[Cue, ...] = ()

    @property
    def cue_count(self) -> int:
        return len(self.cues)

    @property
    def duration_ms(self) -> int:
        return self.total_ms

    @property
    def rendered(self) -> bool:
        return self.total_ms > 0 and self.stats is not None


def build_project(
    source_path: str | Path,
    cues: Sequence[Cue],
    policy: FitPolicy,
    *,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
    voice_key: str = "",
    title: str = "",
    created_at: str = "",
    context: SynthesisContext | None = None,
) -> SubtitleProject:
    """Assemble a workspace from parsed cues; raises when there is nothing to read.

    Raises:
        SubtitleProjectError: ``cues`` is empty (a subtitle file that yielded no
            cue is refused by the caller with the filename, not silently
            rendered as an empty track).
    """
    cue_tuple = tuple(cues)
    if not cue_tuple:
        raise SubtitleProjectError("The subtitle file has no cues to read.")
    path = Path(source_path)
    return SubtitleProject(
        id=project_id_for(path, cue_tuple),
        title=title or path.stem or "Subtitles",
        source_path=str(path),
        cues=cue_tuple,
        policy=policy,
        sample_rate=int(sample_rate),
        voice_key=str(voice_key),
        fingerprint=render_fingerprint(cue_tuple, policy, sample_rate, voice_key, context),
        context=context,
        created_at=created_at or _utc_now_iso(),
    )


# ── store ────────────────────────────────────────────────────────────────────


def _read_json(path: Path) -> Any:
    """Read JSON; ``None`` on any problem (missing/corrupt) with a warning."""
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("ignoring unreadable JSON %s (%s)", path, exc)
        return None


def _write_json_atomic(path: Path, payload: Any) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        for attempt in range(REPLACE_LOCK_ATTEMPTS):
            try:
                os.replace(temp, path)
                break
            except PermissionError:
                if attempt == REPLACE_LOCK_ATTEMPTS - 1:
                    raise
                time.sleep(REPLACE_LOCK_DELAY_S)
    except Exception:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        raise


def export_srt_file(target: str | Path, cues: Sequence[Cue]) -> Path:
    """Write ``cues`` as UTF-8 SubRip to ``target`` atomically; returns ``target``.

    The write lands under a unique temp name in the target's directory and is
    promoted with the same bounded-retry ``os.replace`` used elsewhere, so a
    failed export never leaves a half-written ``.srt`` — or a stray temp.
    """
    target = Path(target)
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(format_srt(cues), encoding="utf-8")
        for attempt in range(REPLACE_LOCK_ATTEMPTS):
            try:
                os.replace(temp, target)
                break
            except PermissionError:
                if attempt == REPLACE_LOCK_ATTEMPTS - 1:
                    raise
                time.sleep(REPLACE_LOCK_DELAY_S)
    except (OSError, TypeError, ValueError) as exc:
        with contextlib.suppress(OSError):
            temp.unlink(missing_ok=True)
        raise SubtitleProjectError(f"Could not export the subtitle file: {exc}") from exc
    return target


class SubtitleProjectStore:
    """Owns the subtitle workspaces under ``root_dir`` (create-on-demand)."""

    def __init__(self, root_dir: str | Path) -> None:
        self.root = Path(root_dir)

    # ── paths ────────────────────────────────────────────────────────────────

    def project_dir(self, project_id: str) -> Path:
        return self.root / project_id

    def project_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / PROJECT_FILENAME

    def wav_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / TRACK_FILENAME

    def timeline_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / TRACK_TIMELINE_FILENAME

    def has_track(self, project_id: str) -> bool:
        try:
            return self.wav_path(project_id).stat().st_size > 0
        except OSError:
            return False

    # ── project.json ─────────────────────────────────────────────────────────

    def save(self, project: SubtitleProject) -> Path:
        """Atomically persist a project; returns the JSON path."""
        target = self.project_path(project.id)
        payload = {
            "version": PROJECT_VERSION,
            "id": project.id,
            "title": project.title,
            "sourcePath": project.source_path,
            "sampleRate": int(project.sample_rate),
            "voiceKey": project.voice_key,
            "fingerprint": project.fingerprint,
            "context": (
                project.context.fingerprint_payload() if project.context is not None else None
            ),
            "totalMs": int(project.total_ms),
            "createdAt": project.created_at,
            "policy": _policy_to_json(project.policy),
            "cues": [_cue_to_json(cue) for cue in project.cues],
            "adjustedCues": [_cue_to_json(cue) for cue in project.adjusted],
            "stats": _stats_to_json(project.stats) if project.stats is not None else None,
        }
        try:
            self.project_dir(project.id).mkdir(parents=True, exist_ok=True)
            _write_json_atomic(target, payload)
        except (OSError, TypeError, ValueError) as exc:
            raise SubtitleProjectError(f"Could not save the subtitle project: {exc}") from exc
        return target

    def load(self, project_id: str) -> SubtitleProject | None:
        """Saved project; ``None`` when absent or malformed (callers fail soft).

        ``project_id`` must look like a generated id before it becomes a path
        segment, and the payload's ``id`` must match it exactly — a payload
        claiming another identity (or a traversal path) is never returned.
        """
        if not isinstance(project_id, str) or _PROJECT_ID_RE.match(project_id) is None:
            return None
        data = _read_json(self.project_path(project_id))
        if not isinstance(data, dict):
            return None
        try:
            version = int(data.get("version") or 0)
        except (TypeError, ValueError):
            return None
        if version != PROJECT_VERSION:
            return None
        if data.get("id") != project_id:
            return None
        cues = _cues_from_json(data.get("cues"))
        policy = _policy_from_json(data.get("policy"))
        if cues is None or policy is None or not cues:
            return None
        adjusted = _cues_from_json(data.get("adjustedCues"))
        if adjusted is None:
            adjusted = ()  # absent/older payload: no retimed SRT yet
        try:
            sample_rate = int(data.get("sampleRate") or 0)
            if sample_rate <= 0:
                return None
            stats = _stats_from_json(data.get("stats"))
            project = SubtitleProject(
                id=project_id,
                title=str(data.get("title") or ""),
                source_path=str(data.get("sourcePath") or ""),
                cues=cues,
                policy=policy,
                sample_rate=sample_rate,
                voice_key=str(data.get("voiceKey") or ""),
                fingerprint=str(data.get("fingerprint") or ""),
                context=context_from_payload(data.get("context")),
                total_ms=int(data.get("totalMs") or 0),
                stats=stats,
                created_at=str(data.get("createdAt") or ""),
                adjusted=adjusted,
            )
        except (TypeError, ValueError):
            return None
        if not project.fingerprint:
            project = replace(
                project,
                fingerprint=render_fingerprint(
                    project.cues, project.policy, project.sample_rate, project.voice_key
                ),
            )
        return project

    def require(self, project_id: str) -> SubtitleProject:
        """Load or raise a user-actionable error (the user chose this workspace)."""
        project = self.load(project_id)
        if project is None:
            raise SubtitleProjectError(f"Subtitle project '{project_id}' could not be read.")
        return project

    def list_projects(self) -> list[SubtitleProject]:
        """Every readable workspace, newest first (unreadable dirs are skipped)."""
        if not self.root.is_dir():
            return []
        projects = [
            project
            for child in self.root.iterdir()
            if child.is_dir() and (project := self.load(child.name)) is not None
        ]
        projects.sort(key=lambda project: project.created_at, reverse=True)
        return projects

    def remove(self, project_id: str) -> None:
        """Delete a workspace and everything in it (invalid ids are a no-op)."""
        if not isinstance(project_id, str) or _PROJECT_ID_RE.match(project_id) is None:
            return
        directory = self.project_dir(project_id)
        if not directory.exists():
            return
        try:
            shutil.rmtree(directory)
        except OSError as exc:
            raise SubtitleProjectError(f"Could not remove the subtitle project: {exc}") from exc

    def cached_render(self, project: SubtitleProject) -> SubtitleProject | None:
        """The stored, fully rendered project reusable for ``project``; else None.

        A render is only reusable when the track exists, the stored fingerprint
        still matches, the stored project is rendered, and its measured
        timeline is on disk and has one segment per cue — anything less is
        re-rendered, never half-adopted. The fingerprint covers the engine
        identity, so a track another profile/language/voice produced is
        invalidated instead of being adopted (Phase 5 Task 5.3).

        One migration allowance: a project rendered before provenance existed
        records no identity, and its inputs are compared with the pre-context
        fingerprint. Such a render stays reusable while VieNeu — the engine the
        pre-multi-engine app used — is asking for it, and never for another
        profile (``synthesis_context.legacy_render_compatible``).
        """
        if not self.has_track(project.id):
            return None
        stored = self.load(project.id)
        timeline = self.load_timeline(project.id)
        if (
            stored is None
            or not stored.rendered
            or timeline is None
            or len(timeline.segments) != len(stored.cues)
        ):
            return None
        if stored.fingerprint == project.fingerprint:
            return stored
        if stored.context is None and context_matches(None, project.context):
            legacy = render_fingerprint(
                project.cues, project.policy, project.sample_rate, project.voice_key
            )
            if legacy == stored.fingerprint:
                return stored
        return None

    def needs_render(self, project: SubtitleProject) -> bool:
        """True when the cached track is missing or was rendered from other inputs."""
        return self.cached_render(project) is None

    # ── track.wav + track.timeline.json ──────────────────────────────────────

    def prepare_track_part(self, project_id: str) -> Path:
        """A unique ``*.part.wav`` inside the workspace (soundfile infers WAV)."""
        directory = self.project_dir(project_id)
        try:
            directory.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise SubtitleProjectError(f"Could not create the subtitle workspace: {exc}") from exc
        return directory / f"track.{uuid.uuid4().hex}.part.wav"

    def promote_track(self, project_id: str, part: Path) -> Path:
        """Atomically replace ``track.wav`` with a fully written part file."""
        target = self.wav_path(project_id)
        try:
            for attempt in range(REPLACE_LOCK_ATTEMPTS):
                try:
                    os.replace(part, target)
                    break
                except PermissionError:
                    if attempt == REPLACE_LOCK_ATTEMPTS - 1:
                        raise
                    time.sleep(REPLACE_LOCK_DELAY_S)
        except OSError as exc:
            with contextlib.suppress(OSError):
                part.unlink(missing_ok=True)
            raise SubtitleProjectError(f"Could not save the rendered track: {exc}") from exc
        return target

    def save_timeline(self, project_id: str, timeline: Timeline) -> Path:
        """Atomically persist the measured cue↔time map next to the track."""
        target = self.timeline_path(project_id)
        try:
            self.project_dir(project_id).mkdir(parents=True, exist_ok=True)
            _write_json_atomic(target, timeline_to_json(timeline))
        except (OSError, TypeError, ValueError) as exc:
            raise SubtitleProjectError(f"Could not save the track timeline: {exc}") from exc
        return target

    def load_timeline(self, project_id: str) -> Timeline | None:
        """Saved timeline; ``None`` when absent/corrupt or the track is gone."""
        if not self.has_track(project_id):
            return None
        return timeline_from_json(_read_json(self.timeline_path(project_id)))


# ── streaming renderer ───────────────────────────────────────────────────────


@dataclass(frozen=True)
class SubtitleRenderResult:
    """A finished track: where it is, how long it is, and how it was fitted."""

    project_id: str
    wav_path: Path
    timeline_path: Path
    sample_rate: int
    total_ms: int
    stats: AlignmentStats
    fits: tuple[CueFit, ...]
    timeline: Timeline


class SubtitleTrackRenderer:
    """Stream a cue-aligned track to ``track.wav``, one clip at a time.

    The caller feeds clips in cue order (splitting a merged speech unit first).
    The renderer plans each cue forward from the previous end, stretches it to
    the planned length, writes the gap as silence and advances the cursor — the
    exact plan :mod:`core.align` describes, so the rendered WAV and the saved
    timeline agree.

    Usage::

        with SubtitleTrackRenderer(store, project) as renderer:
            for cue_index, clip in clips:
                renderer.add_clip(cue_index, clip)
            result = renderer.finish()

    On an exception inside the ``with`` block the partial file is deleted and
    ``track.wav`` is left untouched.
    """

    def __init__(self, store: SubtitleProjectStore, project: SubtitleProject) -> None:
        self._store = store
        self._project = project
        self._writer: StreamingWavWriter | None = None
        self._fits: list[CueFit] = []
        self._prev_end: int | None = None
        self._cursor = 0
        self._last_index = -1
        self._done = False

    @property
    def fits(self) -> tuple[CueFit, ...]:
        return tuple(self._fits)

    @property
    def frames_written(self) -> int:
        return 0 if self._writer is None else self._writer.frames

    def open(self) -> SubtitleTrackRenderer:
        """Open the part file (idempotent)."""
        if self._done:
            raise SubtitleProjectError("This render has already finished.")
        if self._writer is None:
            part = self._store.prepare_track_part(self._project.id)
            self._writer = StreamingWavWriter(
                part, self._project.sample_rate, subtype=TRACK_SUBTYPE
            ).open()
        return self

    def add_clip(self, cue_index: int, clip: np.ndarray) -> CueFit:
        """Place one cue's synthesized audio and write it to the track."""
        if clip is None or np.asarray(clip).size == 0:
            return self.add_silent_cue(cue_index)
        return self._place(cue_index, np.asarray(clip, dtype=np.float32).ravel())

    def add_silent_cue(self, cue_index: int) -> CueFit:
        """Keep a cue's slot when its synthesis produced nothing (never drop text)."""
        return self._place(cue_index, None)

    def _place(self, cue_index: int, clip: np.ndarray | None) -> CueFit:
        self.open()
        if cue_index != self._last_index + 1:
            raise SubtitleProjectError(
                f"Cue {cue_index} arrived out of order (expected {self._last_index + 1})."
            )
        assert self._writer is not None  # open() above
        project = self._project
        cue = project.cues[cue_index]
        natural_ms = ms_for_frames(int(clip.size), project.sample_rate) if clip is not None else 0
        fit = plan_cue(cue, natural_ms, project.policy, self._prev_end)
        if clip is not None:
            block = stretch_clip_to(clip, fit, project.sample_rate)
        else:
            block = np.zeros(0, dtype=np.float32)
        start_frame = max(self._cursor, frames_for_ms(fit.start_ms, project.sample_rate))
        if start_frame > self._cursor:
            self._writer.write_silence(start_frame - self._cursor)
        if block.size:
            self._writer.write(block)
        self._cursor = start_frame + block.size
        self._prev_end = fit.end_ms
        self._last_index = cue_index
        self._fits.append(fit)
        return fit

    def finish(self) -> SubtitleRenderResult:
        """Close, promote ``track.wav``, save the timeline and update the project."""
        if self._done:
            raise SubtitleProjectError("This render has already finished.")
        self.open()
        assert self._writer is not None
        writer = self._writer
        writer.close()
        frames = writer.frames
        if not self._fits or frames <= 0:
            writer.abort()
            self._done = True
            raise SubtitleProjectError("The render produced no audio.")
        project = self._project
        target = self._store.promote_track(project.id, writer.path)
        total_ms = ms_for_frames(frames, project.sample_rate)
        stats = alignment_stats(self._fits)
        timeline = timeline_from_fits(project.cues, self._fits, project.sample_rate)
        timeline_path = self._store.save_timeline(project.id, timeline)
        self._store.save(
            replace(
                project,
                total_ms=total_ms,
                stats=stats,
                adjusted=tuple(adjusted_cues(project.cues, self._fits)),
            )
        )
        self._done = True
        return SubtitleRenderResult(
            project_id=project.id,
            wav_path=target,
            timeline_path=timeline_path,
            sample_rate=project.sample_rate,
            total_ms=total_ms,
            stats=stats,
            fits=tuple(self._fits),
            timeline=timeline,
        )

    def abort(self) -> None:
        """Discard the partial track (``track.wav`` is untouched)."""
        if self._done:
            return  # already finished/aborted — never touch the live track
        if self._writer is not None:
            self._writer.abort()
        self._done = True

    def __enter__(self) -> SubtitleTrackRenderer:
        return self.open()

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        if exc_type is not None:
            # Cleanup must never mask the exception that raised it; a normal
            # exit still does nothing (the caller invokes finish() itself).
            with contextlib.suppress(Exception):
                self.abort()
