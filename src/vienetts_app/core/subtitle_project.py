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
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

import numpy as np

from vienetts_app.core.align import (
    MODE_DUB,
    AlignmentStats,
    CueFit,
    FitPolicy,
    adjusted_cues,
    alignment_stats,
    frames_for_ms,
    ms_for_frames,
    plan_cue,
    split_unit_audio,
    stretch_clip_to,
    timeline_from_fits,
)
from vienetts_app.core.audio import (
    DEFAULT_SAMPLE_RATE,
    StreamingWavWriter,
    apply_micro_fade,
    trim_speech_edges,
)
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
    out: dict[str, Any] = {
        "index": cue.index,
        "startMs": cue.start_ms,
        "endMs": cue.end_ms,
        "text": cue.text,
    }
    if cue.speaker:
        out["speaker"] = cue.speaker
    if cue.speech_start_ms is not None:
        out["speechStartMs"] = int(cue.speech_start_ms)
    if cue.speech_end_ms is not None:
        out["speechEndMs"] = int(cue.speech_end_ms)
    return out


def _cue_from_json(data: Any) -> Cue | None:
    if not isinstance(data, dict):
        return None
    raw_speech_start = data.get("speechStartMs")
    if raw_speech_start is not None and (
        isinstance(raw_speech_start, bool) or not isinstance(raw_speech_start, int)
    ):
        return None
    raw_speech_end = data.get("speechEndMs")
    if raw_speech_end is not None and (
        isinstance(raw_speech_end, bool) or not isinstance(raw_speech_end, int)
    ):
        return None
    try:
        return Cue(
            index=int(data["index"]),
            start_ms=int(data["startMs"]),
            end_ms=int(data["endMs"]),
            text=str(data["text"]),
            speaker=str(data.get("speaker") or ""),
            speech_start_ms=raw_speech_start,
            speech_end_ms=raw_speech_end,
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
    speaker_voices: dict[str, str] | None = None,
) -> str:
    """Stable hash of everything that changes the rendered audio.

    Two renders with the same fingerprint are byte-for-byte the same track, so
    a cached ``track.wav`` can be reused; any change to a cue, the fit policy,
    the sample rate, the voice or the engine identity invalidates it.

    ``context`` is omitted from the payload when absent, so a caller without an
    identity seam (a bare fake app, smoke scenarios) and every project written
    before engine provenance existed keep the fingerprint they always had.
    """
    payload: dict[str, Any] = {
        "version": PROJECT_VERSION,
        "sampleRate": int(sample_rate),
        "voice": str(voice_key),
        "policy": _policy_to_json(policy),
        "cues": [_cue_to_json(cue) for cue in cues],
    }
    if context is not None:
        payload["context"] = context.fingerprint_payload()
    if speaker_voices:
        active_map = {str(k): str(v) for k, v in sorted(speaker_voices.items()) if k and v}
        if active_map:
            payload["speakerVoices"] = active_map
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
    #: Per-cue WSOLA speed factors from the last render (1.0 = unstretched).
    cue_rates: tuple[float, ...] = ()
    #: Optional source video path and video dubbing configuration.
    video_path: str = ""
    speaker_voices: dict[str, str] = field(default_factory=dict)
    video_mix_mode: str = "duck"
    video_bg_gain_db: float = -16.0
    embed_subtitles: bool = True
    #: How embedded subtitles ship: ``"soft"`` (player-toggleable track) or
    #: ``"burn"`` (drawn into the picture; the export re-encodes the video).
    subtitle_embed_mode: str = "soft"

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
    video_path: str = "",
    speaker_voices: dict[str, str] | None = None,
    video_mix_mode: str = "duck",
    video_bg_gain_db: float = -16.0,
    embed_subtitles: bool = True,
    subtitle_embed_mode: str = "soft",
    project_id: str = "",
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
    spk_map = (
        {str(k): str(v) for k, v in speaker_voices.items() if k and v} if speaker_voices else {}
    )
    mix_mode = "replace" if str(video_mix_mode) == "replace" else "duck"
    bg_gain = max(-36.0, min(0.0, float(video_bg_gain_db)))
    return SubtitleProject(
        id=str(project_id) if project_id else project_id_for(path, cue_tuple),
        title=title or path.stem or "Subtitles",
        source_path=str(path),
        cues=cue_tuple,
        policy=policy,
        sample_rate=int(sample_rate),
        voice_key=str(voice_key),
        fingerprint=render_fingerprint(
            cue_tuple, policy, sample_rate, voice_key, context, speaker_voices=spk_map
        ),
        context=context,
        created_at=created_at or _utc_now_iso(),
        video_path=str(video_path or ""),
        speaker_voices=spk_map,
        video_mix_mode=mix_mode,
        video_bg_gain_db=bg_gain,
        embed_subtitles=bool(embed_subtitles),
        subtitle_embed_mode="burn" if str(subtitle_embed_mode) == "burn" else "soft",
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


def export_srt_file(
    target: str | Path, cues: Sequence[Cue], *, include_speakers: bool = False
) -> Path:
    """Write ``cues`` as UTF-8 SubRip to ``target`` atomically; returns ``target``.

    ``include_speakers`` prefixes each line with ``[SPEAKER_01]`` so the label
    survives export; the in-app preview already shows it, but the exported file
    used to drop it silently.

    The write lands under a unique temp name in the target's directory and is
    promoted with the same bounded-retry ``os.replace`` used elsewhere, so a
    failed export never leaves a half-written ``.srt`` — or a stray temp.
    """
    target = Path(target)
    temp = target.with_name(f".{target.name}.{uuid.uuid4().hex}.tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        temp.write_text(format_srt(cues, include_speakers=include_speakers), encoding="utf-8")
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
            "cueRates": [round(float(r), 4) for r in project.cue_rates],
            "stats": _stats_to_json(project.stats) if project.stats is not None else None,
            "videoPath": project.video_path,
            "speakerVoices": {str(k): str(v) for k, v in project.speaker_voices.items() if k and v},
            "videoMixMode": project.video_mix_mode,
            "videoBgGainDb": float(project.video_bg_gain_db),
            "embedSubtitles": bool(project.embed_subtitles),
            "subtitleEmbedMode": project.subtitle_embed_mode,
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
        raw_rates = data.get("cueRates")
        cue_rates: tuple[float, ...] = ()
        if isinstance(raw_rates, list) and len(raw_rates) == len(cues):
            with contextlib.suppress(TypeError, ValueError):
                cue_rates = tuple(max(0.5, min(2.0, float(r))) for r in raw_rates)
        raw_spk_voices = data.get("speakerVoices")
        speaker_voices: dict[str, str] = {}
        if isinstance(raw_spk_voices, dict):
            for k, v in raw_spk_voices.items():
                if isinstance(k, str) and isinstance(v, str) and k.strip() and v.strip():
                    speaker_voices[k.strip()] = v.strip()
        raw_mix = str(data.get("videoMixMode") or "duck")
        mix_mode = "replace" if raw_mix == "replace" else "duck"
        try:
            bg_gain_db = max(-36.0, min(0.0, float(data.get("videoBgGainDb", -16.0))))
        except (TypeError, ValueError):
            bg_gain_db = -16.0
        embed_subs = bool(data.get("embedSubtitles", True))
        embed_mode = "burn" if str(data.get("subtitleEmbedMode") or "") == "burn" else "soft"
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
                cue_rates=cue_rates,
                video_path=str(data.get("videoPath") or ""),
                speaker_voices=speaker_voices,
                video_mix_mode=mix_mode,
                video_bg_gain_db=bg_gain_db,
                embed_subtitles=embed_subs,
                subtitle_embed_mode=embed_mode,
            )
        except (TypeError, ValueError):
            return None
        if not project.fingerprint:
            project = replace(
                project,
                fingerprint=render_fingerprint(
                    project.cues,
                    project.policy,
                    project.sample_rate,
                    project.voice_key,
                    speaker_voices=project.speaker_voices,
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
        trim = not getattr(self, "_unit_pretrimmed", False)
        forced_rate = getattr(self, "_unit_forced_rate", None)
        return self._place(
            cue_index,
            np.asarray(clip, dtype=np.float32).ravel(),
            trim=trim,
            forced_rate=forced_rate,
        )

    def add_unit_clip(
        self,
        unit_indices: Sequence[int],
        clip: np.ndarray,
        *,
        split_fn: Any = split_unit_audio,
    ) -> list[CueFit]:
        """Place a merged speech unit across ``unit_indices`` at a uniform rate."""
        if not unit_indices:
            return []
        if clip is None or np.asarray(clip).size == 0:
            return [self.add_silent_cue(idx) for idx in unit_indices]
        project = self._project
        if len(unit_indices) == 1:
            pieces = split_fn(
                np.asarray(clip, dtype=np.float32).ravel(),
                unit_indices,
                project.cues,
                project.sample_rate,
            )
            return [self.add_clip(unit_indices[0], pieces[0])]
        trimmed = trim_speech_edges(np.asarray(clip, dtype=np.float32).ravel(), project.sample_rate)
        pieces = split_fn(trimmed, unit_indices, project.cues, project.sample_rate)
        forced_rate: float | None = None
        if project.policy.mode == MODE_DUB:
            first_idx = unit_indices[0]
            last_idx = unit_indices[-1]
            first_cue = project.cues[first_idx]
            last_cue = project.cues[last_idx]
            sp_start = first_cue.effective_speech_start_ms
            sp_end = max(sp_start, last_cue.effective_speech_end_ms)
            unit_cue = Cue(
                index=first_cue.index,
                start_ms=first_cue.start_ms,
                end_ms=max(first_cue.start_ms, last_cue.end_ms),
                text=first_cue.text,
                speaker=first_cue.speaker,
                speech_start_ms=sp_start,
                speech_end_ms=sp_end,
            )
            after_idx = last_idx + 1
            next_start = (
                max(
                    0,
                    int(project.cues[after_idx].effective_speech_start_ms)
                    + project.policy.offset_ms,
                )
                if after_idx < len(project.cues)
                else None
            )
            prev_rate = (
                self._fits[-1].rate
                if self._fits
                and first_cue.speaker
                and first_cue.speaker == project.cues[first_idx - 1].speaker
                else None
            )
            unit_natural_ms = ms_for_frames(int(trimmed.size), project.sample_rate)
            unit_fit = plan_cue(
                unit_cue,
                unit_natural_ms,
                project.policy,
                self._prev_end,
                next_start_ms=next_start,
                prev_rate=prev_rate,
            )
            forced_rate = unit_fit.rate
        self._unit_pretrimmed = True
        self._unit_forced_rate = forced_rate
        try:
            return [
                self.add_clip(idx, piece) for idx, piece in zip(unit_indices, pieces, strict=True)
            ]
        finally:
            self._unit_pretrimmed = False
            self._unit_forced_rate = None

    def add_silent_cue(self, cue_index: int) -> CueFit:
        """Keep a cue's slot when its synthesis produced nothing (never drop text)."""
        return self._place(cue_index, None)

    def _place(
        self,
        cue_index: int,
        clip: np.ndarray | None,
        *,
        trim: bool = True,
        forced_rate: float | None = None,
    ) -> CueFit:
        self.open()
        if cue_index != self._last_index + 1:
            raise SubtitleProjectError(
                f"Cue {cue_index} arrived out of order (expected {self._last_index + 1})."
            )
        assert self._writer is not None  # open() above
        project = self._project
        cue = project.cues[cue_index]
        if clip is not None and trim:
            clip = trim_speech_edges(clip, project.sample_rate)
        natural_ms = ms_for_frames(int(clip.size), project.sample_rate) if clip is not None else 0
        next_start = (
            max(
                0,
                int(project.cues[cue_index + 1].effective_speech_start_ms)
                + project.policy.offset_ms,
            )
            if cue_index + 1 < len(project.cues)
            else None
        )
        prev_rate = (
            self._fits[-1].rate
            if self._fits and cue.speaker and cue.speaker == project.cues[cue_index - 1].speaker
            else None
        )
        base_fit = plan_cue(
            cue,
            natural_ms,
            project.policy,
            self._prev_end,
            next_start_ms=next_start,
            prev_rate=prev_rate,
        )
        if forced_rate is not None and clip is not None:
            rate = float(forced_rate)
            duration_ms = int(round(natural_ms / rate)) if rate > 0 else natural_ms
            end_ms = base_fit.start_ms + duration_ms
            overflow_limit = (
                max(base_fit.srt_end_ms, next_start - 120)
                if next_start is not None and project.policy.mode == MODE_DUB
                else base_fit.srt_end_ms
            )
            fit = CueFit(
                index=base_fit.index,
                start_ms=base_fit.start_ms,
                end_ms=end_ms,
                rate=rate,
                srt_start_ms=base_fit.srt_start_ms,
                srt_end_ms=base_fit.srt_end_ms,
                natural_ms=natural_ms,
                overflow_ms=max(0, end_ms - base_fit.srt_end_ms) if end_ms > overflow_limit else 0,
            )
        else:
            fit = base_fit
        if clip is not None:
            block = stretch_clip_to(clip, fit, project.sample_rate)
        else:
            block = np.zeros(0, dtype=np.float32)
        start_frame = max(self._cursor, frames_for_ms(fit.start_ms, project.sample_rate))
        if block.size:
            butt_joined = (
                self._prev_end is not None and self._cursor > 0 and start_frame == self._cursor
            )
            truncated = fit.overflow_ms > 0
            if butt_joined or truncated:
                block = apply_micro_fade(
                    block,
                    project.sample_rate,
                    fade_in=butt_joined,
                    fade_out=truncated,
                )
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
                cue_rates=tuple(round(float(f.rate), 4) for f in self._fits),
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


SPLICE_CHUNK_FRAMES = 65_536


def splice_studio_window_into_subtitle(
    store: SubtitleProjectStore,
    studio_project: Any,
) -> SubtitleProject:
    """Splice a Studio-edited SRT window back into ``track.wav`` and update metadata.

    Streams the untouched prefix and suffix around ``[orig_window_start_frame,
    orig_window_end_frame)`` in bounded 64k-frame chunks, writes the rendered
    Studio window in between, shifts downstream timeline segments and
    ``adjusted`` cues by ``delta_ms``, and atomically promotes the updated
    ``track.wav``, ``track.timeline.json``, and ``project.json``.
    """
    from vienetts_app.core.audio import read_wav_range
    from vienetts_app.core.studio import render_project_for_subtitle_save
    from vienetts_app.core.subtitles import cue_char_spans
    from vienetts_app.core.timeline import SegmentSpan

    meta = getattr(studio_project, "subtitle_meta", None)
    if meta is None:
        raise SubtitleProjectError("Studio project is not linked to a subtitle project.")
    sub_proj = store.require(meta.project_id)
    timeline = store.load_timeline(meta.project_id)
    source_wav = store.wav_path(meta.project_id)
    if (
        not sub_proj.rendered
        or timeline is None
        or len(timeline.segments) != len(sub_proj.cues)
        or not source_wav.is_file()
    ):
        raise SubtitleProjectError("Subtitle project has no rendered track to update.")

    window_mix = render_project_for_subtitle_save(studio_project)
    orig_start = max(0, int(meta.orig_window_start_frame))
    orig_end = max(orig_start, int(meta.orig_window_end_frame))

    part = store.prepare_track_part(sub_proj.id)
    writer = StreamingWavWriter(part, sub_proj.sample_rate)
    try:
        cursor = 0
        while cursor < orig_start:
            stop = min(orig_start, cursor + SPLICE_CHUNK_FRAMES)
            chunk, _ = read_wav_range(source_wav, cursor, stop)
            if chunk.size == 0:
                break
            writer.write(chunk)
            cursor += int(chunk.size)
        writer.write(window_mix)
        cursor = orig_end
        while True:
            chunk, _ = read_wav_range(source_wav, cursor, cursor + SPLICE_CHUNK_FRAMES)
            if chunk.size == 0:
                break
            writer.write(chunk)
            cursor += int(chunk.size)
        writer.close()
        total_frames = writer.frames
    except BaseException:
        with contextlib.suppress(Exception):
            writer.abort()
        raise

    store.promote_track(sub_proj.id, part)
    total_ms = ms_for_frames(total_frames, sub_proj.sample_rate)

    cues = list(sub_proj.cues)
    for clip in studio_project.clips:
        idx = getattr(clip, "cue_index", None)
        if idx is not None and 0 <= idx < len(cues):
            old_cue = cues[idx]
            if clip.text != old_cue.text:
                cues[idx] = replace(old_cue, text=clip.text)
    updated_cues = tuple(cues)

    window_base_ms = 0 if meta.prev_cue_end_ms is None else int(meta.prev_cue_end_ms)
    old_window_end_ms = ms_for_frames(orig_end, sub_proj.sample_rate)
    new_window_duration_ms = ms_for_frames(len(window_mix), sub_proj.sample_rate)
    new_window_end_ms = window_base_ms + new_window_duration_ms
    delta_ms = new_window_end_ms - old_window_end_ms

    from vienetts_app.core.studio import GapOp

    gap_ops = [op for op in getattr(studio_project, "ops", ()) if isinstance(op, GapOp)]
    gap_override_ms = max(0, int(gap_ops[-1].ms)) if gap_ops else None
    raw_bounds: list[tuple[int, int]] = []
    cursor_ms = window_base_ms
    for c_idx, clip in enumerate(studio_project.clips):
        dur_ms = max(0, int(clip.placed_end_ms) - int(clip.placed_start_ms))
        if gap_override_ms is not None and c_idx > 0:
            s_raw = cursor_ms + gap_override_ms
            e_raw = s_raw + dur_ms
        elif gap_override_ms is not None and c_idx == 0:
            s_raw = window_base_ms + max(0, int(clip.lead_silence_ms))
            e_raw = s_raw + dur_ms
        else:
            s_raw = int(clip.placed_start_ms)
            e_raw = int(clip.placed_end_ms)
        raw_bounds.append((s_raw, e_raw))
        cursor_ms = e_raw

    unscaled_span_ms = max(1, raw_bounds[-1][1] - window_base_ms) if raw_bounds else 1
    scale = (
        new_window_duration_ms / unscaled_span_ms
        if abs(new_window_duration_ms - unscaled_span_ms) > 2
        else 1.0
    )

    seg_bounds: list[tuple[int, int]] = [
        (int(seg.start_ms), int(seg.end_ms)) for seg in timeline.segments
    ]
    rates_list = (
        list(sub_proj.cue_rates)
        if len(sub_proj.cue_rates) == len(updated_cues)
        else [1.0] * len(updated_cues)
    )

    for clip, (raw_s_ms, raw_e_ms) in zip(studio_project.clips, raw_bounds, strict=True):
        idx = getattr(clip, "cue_index", None)
        if idx is None or not (0 <= idx < len(updated_cues)):
            continue
        s_ms = window_base_ms + int(round((raw_s_ms - window_base_ms) * scale))
        e_ms = window_base_ms + int(round((raw_e_ms - window_base_ms) * scale))
        if e_ms < s_ms:
            e_ms = s_ms
        seg_bounds[idx] = (s_ms, e_ms)
        rates_list[idx] = round(float(clip.fit_rate), 4)

    for idx in range(int(meta.window_end), len(updated_cues)):
        old_s, old_e = seg_bounds[idx]
        s_ms = max(0, old_s + delta_ms)
        e_ms = max(s_ms, old_e + delta_ms)
        seg_bounds[idx] = (s_ms, e_ms)

    char_spans = cue_char_spans(updated_cues)
    new_segments = tuple(
        SegmentSpan(cs, ce, s_ms, e_ms)
        for (cs, ce), (s_ms, e_ms) in zip(char_spans, seg_bounds, strict=True)
    )
    new_timeline = Timeline(new_segments, approximate=False)
    store.save_timeline(sub_proj.id, new_timeline)

    offset_ms = int(sub_proj.policy.offset_ms)
    fits_for_stats = [
        CueFit(
            index=cue.index,
            start_ms=s_ms,
            end_ms=e_ms,
            rate=rates_list[i],
            srt_start_ms=max(0, int(cue.effective_speech_start_ms) + offset_ms),
            srt_end_ms=max(0, int(cue.end_ms) + offset_ms),
            natural_ms=max(0, e_ms - s_ms),
            overflow_ms=max(0, e_ms - max(0, int(cue.end_ms) + offset_ms)),
        )
        for i, (cue, (s_ms, e_ms)) in enumerate(zip(updated_cues, seg_bounds, strict=True))
    ]
    adjusted_list = adjusted_cues(updated_cues, fits_for_stats)
    stats = alignment_stats(fits_for_stats)
    updated_proj = replace(
        sub_proj,
        cues=updated_cues,
        fingerprint=render_fingerprint(
            updated_cues,
            sub_proj.policy,
            sub_proj.sample_rate,
            sub_proj.voice_key,
            sub_proj.context,
            speaker_voices=sub_proj.speaker_voices,
        ),
        total_ms=total_ms,
        stats=stats,
        adjusted=tuple(adjusted_list),
        cue_rates=tuple(rates_list),
    )
    store.save(updated_proj)
    return updated_proj
