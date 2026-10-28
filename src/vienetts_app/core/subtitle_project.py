"""SRT subtitle workspace: stream a cue-aligned track to disk, then persist it.

Layout under the subtitles root (``<data_dir>/subtitles``)::

    <project_id>/project.json           inputs: cues, fit policy, fingerprint,
                                        final stats
    <project_id>/track.wav              rendered audio, streamed cue by cue
    <project_id>/track.timeline.json    measured cue↔time map (karaoke playback)
    <project_id>/track.render.json      render_id of the live track.wav
    <project_id>/units/<sha256>.wav     raw per-unit synthesis cache (LRU)

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

A render commits in a crash-consistent order (FR-5.7): the timeline, then
``project.json``, then ``track.wav``, all stamped with one fresh ``render_id``.
A crash between any two steps leaves ids that disagree, and
:meth:`SubtitleProjectStore.cached_render` refuses such a mix instead of
pairing a new timeline with the previous audio.
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
import threading
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
    read_wav,
    trim_speech_edges,
    write_wav_file,
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
#: ``{"renderId": ...}`` of the live ``track.wav``, written right after the
#: track is promoted (absent for a track committed before render ids existed).
TRACK_RENDER_STAMP_FILENAME = "track.render.json"
UNITS_DIRNAME = "units"
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

#: Bump when the cached unit clip format or the key recipe changes. The
#: version is hashed into every key, so clips written under another version
#: are never misread: they stop matching and age out through LRU eviction.
UNIT_CACHE_VERSION = 1
#: Default byte budget of one project's unit cache (FR-5.1).
UNIT_CACHE_CAP_BYTES = 2 * 1024**3
#: Minimum step between two LRU access stamps, so the order survives
#: filesystems with coarse mtime resolution (NTFS keeps 100 ns).
_UNIT_TICK_NS = 1_000_000
_UNIT_KEY_RE = re.compile(r"^[0-9a-f]{64}$")


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

    ``speaker_voices`` is normalized first (FR-5.7): names are stripped, and
    speakers no cue uses are dropped, since neither changes the audio. A
    normalized map hashes exactly as it always did.
    """
    return _fingerprint_digest(
        cues,
        policy,
        sample_rate,
        voice_key,
        context,
        _active_speaker_voices(cues, speaker_voices),
    )


def _active_speaker_voices(
    cues: Sequence[Cue], speaker_voices: dict[str, str] | None
) -> dict[str, str]:
    """``speaker_voices`` stripped and limited to speakers some cue uses."""
    if not speaker_voices:
        return {}
    used = {cue.speaker.strip() for cue in cues if cue.speaker.strip()}
    active: dict[str, str] = {}
    for raw_key, raw_voice in speaker_voices.items():
        key, voice = str(raw_key or "").strip(), str(raw_voice or "").strip()
        if key in used and voice:
            active[key] = voice
    return active


def _fingerprint_digest(
    cues: Sequence[Cue],
    policy: FitPolicy,
    sample_rate: int,
    voice_key: str,
    context: SynthesisContext | None,
    speaker_map: dict[str, str],
) -> str:
    payload: dict[str, Any] = {
        "version": PROJECT_VERSION,
        "sampleRate": int(sample_rate),
        "voice": str(voice_key),
        "policy": _policy_to_json(policy),
        "cues": [_cue_to_json(cue) for cue in cues],
    }
    if context is not None:
        payload["context"] = context.fingerprint_payload()
    if speaker_map:
        payload["speakerVoices"] = speaker_map
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )
    return hashlib.sha256(encoded).hexdigest()


def _pre_normalization_fingerprint(project: SubtitleProject) -> str:
    """``project``'s digest as hashed before FR-5.7 (raw speaker map, unused
    speakers included); only :meth:`SubtitleProjectStore.cached_render` uses
    it, to keep reusing tracks rendered before the normalization."""
    raw_map = {str(k): str(v) for k, v in project.speaker_voices.items() if k and v}
    return _fingerprint_digest(
        project.cues,
        project.policy,
        project.sample_rate,
        project.voice_key,
        project.context,
        raw_map,
    )


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
    llm_glossary: str = ""
    #: Id of the render this project.json was committed with (FR-5.7). The
    #: timeline and ``track.wav`` carry the same id; ``""`` for a project
    #: committed before render ids existed. Not part of the fingerprint.
    render_id: str = ""

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
    llm_glossary: str = "",
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
    spk_map: dict[str, str] = {}
    for raw_key, raw_voice in (speaker_voices or {}).items():
        key, voice = str(raw_key or "").strip(), str(raw_voice or "").strip()
        if key and voice:
            spk_map[key] = voice
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
        llm_glossary=str(llm_glossary or "")[:4000],
    )


# ── per-unit clip cache (FR-5.1) ─────────────────────────────────────────────


def voice_file_stamp(path: str | Path | None) -> str:
    """``"<bytes>:<mtime_ns>"`` of a voice reference file; ``""`` when unreadable.

    Part of a unit cache key: replacing a cloned voice's reference audio under
    the same id invalidates the clips it produced.
    """
    if not path:
        return ""
    try:
        stat = Path(path).stat()
    except (OSError, ValueError):
        return ""
    return f"{stat.st_size}:{stat.st_mtime_ns}"


class UnitClipCache:
    """Raw synthesized speech-unit clips, one WAV per key, capped by LRU.

    ``<project>/units/<key>.wav`` keeps the engine's output for one unit, before
    any placement, as a float WAV (exact samples, no PCM rounding). A re-render
    then synthesizes only the units whose key is missing and re-runs placement
    for all of them. Recency is the file's mtime, bumped on every hit, so the
    LRU order survives a restart. Each put evicts the least recently used
    clips until the total fits ``cap_bytes``.
    Reads fail soft (``None``), writes are atomic, and one instance is safe to
    share between threads.
    """

    def __init__(self, directory: str | Path, *, cap_bytes: int = UNIT_CACHE_CAP_BYTES) -> None:
        self.directory = Path(directory)
        self.cap_bytes = max(0, int(cap_bytes))
        self._lock = threading.Lock()
        self._index: dict[str, tuple[int, int]] | None = None  # key → (bytes, access ns)
        self._tick = 0

    @staticmethod
    def key(
        text: str,
        *,
        voice_id: str,
        voice_stamp: str = "",
        profile: str = "",
        model_revision: str = "",
        speed: float | None = None,
        context_fingerprint: str = "",
    ) -> str:
        """sha256 of everything that changes one unit's raw synthesis."""
        payload = {
            "version": UNIT_CACHE_VERSION,
            "text": str(text),
            "voiceId": str(voice_id),
            "voiceStamp": str(voice_stamp),
            "profile": str(profile),
            "modelRevision": str(model_revision),
            "speed": None if speed is None else round(float(speed), 4),
            "context": str(context_fingerprint),
        }
        encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def path_for(self, key: str) -> Path | None:
        """``<directory>/<key>.wav``; ``None`` for anything not shaped like a key."""
        if not isinstance(key, str) or _UNIT_KEY_RE.match(key) is None:
            return None
        return self.directory / f"{key}.wav"

    def get(self, key: str, sample_rate: int) -> np.ndarray | None:
        """The cached clip, when present at ``sample_rate``; never raises."""
        path = self.path_for(key)
        if path is None:
            return None
        with self._lock:
            index = self._load_index()
            try:
                audio, rate = read_wav(path)
            except FileNotFoundError:
                index.pop(key, None)
                return None
            except (OSError, RuntimeError, ValueError) as exc:
                logger.warning("dropping unreadable cached unit clip %s (%s)", path, exc)
                with contextlib.suppress(OSError):
                    path.unlink(missing_ok=True)
                index.pop(key, None)
                return None
            if rate != int(sample_rate) or audio.ndim != 1 or audio.size == 0:
                return None
            self._touch(key, path)
            return audio

    def put(self, key: str, audio: np.ndarray, sample_rate: int) -> Path | None:
        """Store one unit's raw clip atomically, then evict down to the cap.

        Returns the clip's path, or ``None`` for an empty clip (a 0-sample unit
        is placed silent and never cached).

        Raises:
            SubtitleProjectError: the key is malformed or the clip can't be written.
        """
        path = self.path_for(key)
        if path is None:
            raise SubtitleProjectError(f"Invalid unit cache key: {key!r}")
        clip = np.asarray(audio, dtype=np.float32)
        if clip.size == 0:
            return None
        temp = self.directory / f".{key}.{uuid.uuid4().hex}.tmp.wav"
        try:
            self.directory.mkdir(parents=True, exist_ok=True)
            write_wav_file(clip, temp, int(sample_rate))
            _replace_retrying(temp, path)
        except (OSError, RuntimeError, ValueError) as exc:
            with contextlib.suppress(OSError):
                temp.unlink(missing_ok=True)
            raise SubtitleProjectError(f"Could not cache the synthesized clip: {exc}") from exc
        with self._lock:
            self._load_index()
            self._touch(key, path)
            self._evict(keep=key)
        return path

    def _load_index(self) -> dict[str, tuple[int, int]]:
        """key → (bytes, last access ns), scanned from disk once per instance."""
        if self._index is None:
            index: dict[str, tuple[int, int]] = {}
            with contextlib.suppress(OSError), os.scandir(self.directory) as entries:
                for entry in entries:
                    key = entry.name[:-4] if entry.name.endswith(".wav") else ""
                    if _UNIT_KEY_RE.match(key) is None:
                        continue
                    with contextlib.suppress(OSError):
                        stat = entry.stat()
                        index[key] = (stat.st_size, stat.st_mtime_ns)
            self._index = index
            self._tick = max((tick for _, tick in index.values()), default=0)
        return self._index

    def _touch(self, key: str, path: Path) -> None:
        """Mark ``key`` most recently used, in memory and on disk (mtime)."""
        assert self._index is not None
        tick = max(time.time_ns(), self._tick + _UNIT_TICK_NS)
        self._tick = tick
        try:
            os.utime(path, ns=(tick, tick))
            size = path.stat().st_size
        except OSError:
            self._index.pop(key, None)
            return
        self._index[key] = (size, tick)

    def _evict(self, *, keep: str) -> None:
        """Drop least recently used clips (never ``keep``) until under the cap."""
        assert self._index is not None
        total = sum(size for size, _ in self._index.values())
        for key, (size, _) in sorted(self._index.items(), key=lambda item: item[1][1]):
            if total <= self.cap_bytes:
                break
            if key == keep:
                continue
            try:
                (self.directory / f"{key}.wav").unlink(missing_ok=True)
            except OSError:
                continue  # held open (Windows): the next put retries it
            del self._index[key]
            total -= size


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


def _replace_retrying(source: Path, target: Path) -> None:
    """``os.replace`` riding out the brief locks Windows keeps on a fresh file."""
    for attempt in range(REPLACE_LOCK_ATTEMPTS):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == REPLACE_LOCK_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_LOCK_DELAY_S)


def _write_json_atomic(path: Path, payload: Any) -> None:
    temp = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        _replace_retrying(temp, path)
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
        _replace_retrying(temp, target)
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

    def render_stamp_path(self, project_id: str) -> Path:
        return self.project_dir(project_id) / TRACK_RENDER_STAMP_FILENAME

    def unit_cache(
        self, project_id: str, *, cap_bytes: int = UNIT_CACHE_CAP_BYTES
    ) -> UnitClipCache:
        """The raw per-unit clip cache of a workspace (``<project>/units/``).

        It outlives every render and track release; only :meth:`remove`
        deletes it, together with the workspace.
        """
        return UnitClipCache(self.project_dir(project_id) / UNITS_DIRNAME, cap_bytes=cap_bytes)

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
        if project.llm_glossary:
            payload["llmGlossary"] = project.llm_glossary[:4000]
        if project.render_id:
            payload["renderId"] = project.render_id
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
                llm_glossary=str(data.get("llmGlossary") or "")[:4000],
                render_id=str(data.get("renderId") or ""),
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

        The timeline, ``project.json`` and ``track.wav`` must also carry the
        same ``render_id`` (FR-5.7), so a crash between commit steps is never
        half-adopted. A render committed before render ids existed carries
        none anywhere; all three agree on ``""`` and it stays reusable.

        Two migration allowances: a project rendered before provenance existed
        records no identity, and its inputs are compared with the pre-context
        fingerprint. Such a render stays reusable while VieNeu — the engine the
        pre-multi-engine app used — is asking for it, and never for another
        profile (``synthesis_context.legacy_render_compatible``). And a render
        whose digest hashed a speaker map with unused or padded entries (before
        FR-5.7 normalized it) is matched against that raw map.
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
        if stored.render_id != self.track_render_id(project.id):
            # load_timeline already paired the timeline with the track; this
            # pairs project.json too (a commit interrupted between its steps).
            return None
        if stored.fingerprint == project.fingerprint:
            return stored
        if project.speaker_voices and stored.fingerprint == _pre_normalization_fingerprint(project):
            return stored  # rendered before speaker maps were normalized
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

    def promote_track(self, project_id: str, part: Path, *, render_id: str = "") -> Path:
        """Atomically replace ``track.wav`` with a fully written part file.

        The track's ``render_id`` stamp is written only after the replace, so a
        crash in between leaves the previous stamp on the new audio — a
        mismatch :meth:`cached_render` refuses. ``""`` clears the stamp.
        """
        target = self.wav_path(project_id)
        stamp = self.render_stamp_path(project_id)
        try:
            _replace_retrying(part, target)
        except OSError as exc:
            with contextlib.suppress(OSError):
                part.unlink(missing_ok=True)
            raise SubtitleProjectError(f"Could not save the rendered track: {exc}") from exc
        try:
            if render_id:
                _write_json_atomic(stamp, {"renderId": str(render_id)})
            else:
                stamp.unlink(missing_ok=True)
        except OSError as exc:
            raise SubtitleProjectError(f"Could not save the rendered track: {exc}") from exc
        return target

    def track_render_id(self, project_id: str) -> str:
        """``render_id`` stamped on the live ``track.wav`` (``""`` when legacy)."""
        data = _read_json(self.render_stamp_path(project_id))
        return str(data.get("renderId") or "") if isinstance(data, dict) else ""

    def save_timeline(self, project_id: str, timeline: Timeline, *, render_id: str = "") -> Path:
        """Atomically persist the measured cue↔time map next to the track."""
        target = self.timeline_path(project_id)
        payload = timeline_to_json(timeline)
        if render_id:
            payload["renderId"] = str(render_id)
        try:
            self.project_dir(project_id).mkdir(parents=True, exist_ok=True)
            _write_json_atomic(target, payload)
        except (OSError, TypeError, ValueError) as exc:
            raise SubtitleProjectError(f"Could not save the track timeline: {exc}") from exc
        return target

    def load_timeline(self, project_id: str) -> Timeline | None:
        """Saved timeline; ``None`` when absent/corrupt, the track is gone, or
        the timeline belongs to another render than ``track.wav`` (FR-5.7)."""
        if not self.has_track(project_id):
            return None
        data = _read_json(self.timeline_path(project_id))
        render_id = str(data.get("renderId") or "") if isinstance(data, dict) else ""
        if render_id != self.track_render_id(project_id):
            return None
        return timeline_from_json(data)


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
    #: The id stamped on the committed timeline, project.json and track.wav.
    render_id: str = ""


def new_render_id() -> str:
    """A fresh id for one render commit (FR-5.7)."""
    return uuid.uuid4().hex


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
        # Per-piece state while add_unit_clip feeds a merged unit through
        # add_clip (kept as state so spies on add_clip still see every cue).
        self._unit_pretrimmed = False
        self._unit_forced_rate: float | None = None
        self._unit_follow = False
        self._unit_split_edges = (False, False)

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
        return self._place(
            cue_index,
            np.asarray(clip, dtype=np.float32).ravel(),
            trim=not self._unit_pretrimmed,
            forced_rate=self._unit_forced_rate,
            follow=self._unit_follow,
            split_edges=self._unit_split_edges,
        )

    def add_unit_clip(
        self,
        unit_indices: Sequence[int],
        clip: np.ndarray,
        *,
        split_fn: Any = split_unit_audio,
    ) -> list[CueFit]:
        """Place a merged speech unit across ``unit_indices`` at a uniform rate.

        Only the first piece anchors to its cue's subtitle start; every later
        piece follows the previous one contiguously, so the unit never gains
        silence its synthesis didn't have. Each split edge gets a 5 ms
        micro-fade (the split itself snaps to a quiet frame in ``split_fn``).
        """
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
        last = len(unit_indices) - 1
        self._unit_pretrimmed = True
        self._unit_forced_rate = forced_rate
        try:
            fits: list[CueFit] = []
            for position, (idx, piece) in enumerate(zip(unit_indices, pieces, strict=True)):
                self._unit_follow = position > 0
                self._unit_split_edges = (position > 0, position < last)
                fits.append(self.add_clip(idx, piece))
            return fits
        finally:
            self._unit_pretrimmed = False
            self._unit_forced_rate = None
            self._unit_follow = False
            self._unit_split_edges = (False, False)

    def add_silent_cue(self, cue_index: int) -> CueFit:
        """Keep a cue's slot when its synthesis produced nothing (never drop text).

        An empty piece inside a merged unit still follows the previous piece,
        so it cannot open a gap before the rest of the unit.
        """
        return self._place(cue_index, None, follow=self._unit_follow)

    def _place(
        self,
        cue_index: int,
        clip: np.ndarray | None,
        *,
        trim: bool = True,
        forced_rate: float | None = None,
        follow: bool = False,
        split_edges: tuple[bool, bool] = (False, False),
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
        fit = plan_cue(
            cue,
            natural_ms,
            project.policy,
            self._prev_end,
            next_start_ms=next_start,
            prev_rate=prev_rate,
            start_ms=self._prev_end if follow else None,
            forced_rate=forced_rate if clip is not None else None,
        )
        if clip is not None:
            block = stretch_clip_to(clip, fit, project.sample_rate)
        else:
            block = np.zeros(0, dtype=np.float32)
        start_frame = max(self._cursor, frames_for_ms(fit.start_ms, project.sample_rate))
        if block.size:
            butt_joined = (
                self._prev_end is not None and self._cursor > 0 and start_frame == self._cursor
            )
            fade_in = butt_joined or split_edges[0]
            fade_out = fit.overflow_ms > 0 or split_edges[1]
            if fade_in or fade_out:
                block = apply_micro_fade(
                    block,
                    project.sample_rate,
                    fade_in=fade_in,
                    fade_out=fade_out,
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
        """Close and commit: the timeline, then the project, then ``track.wav``.

        All three carry one fresh ``render_id`` (FR-5.7). A failure before the
        promote deletes the part file and leaves ``track.wav`` untouched; the
        ids it leaves behind disagree, so the half-commit is never adopted.
        """
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
        render_id = new_render_id()
        total_ms = ms_for_frames(frames, project.sample_rate)
        stats = alignment_stats(self._fits)
        timeline = timeline_from_fits(project.cues, self._fits, project.sample_rate)
        try:
            timeline_path = self._store.save_timeline(project.id, timeline, render_id=render_id)
            self._store.save(
                replace(
                    project,
                    total_ms=total_ms,
                    stats=stats,
                    adjusted=tuple(adjusted_cues(project.cues, self._fits)),
                    cue_rates=tuple(round(float(f.rate), 4) for f in self._fits),
                    render_id=render_id,
                )
            )
            target = self._store.promote_track(project.id, writer.path, render_id=render_id)
        except BaseException:
            writer.abort()  # drops the part; a no-op once promote moved it
            self._done = True
            raise
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
            render_id=render_id,
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
    ``adjusted`` cues by ``delta_ms``, then commits like a render (FR-5.7):
    ``track.timeline.json``, ``project.json``, then ``track.wav``, all under a
    fresh ``render_id``. A failure before the promote leaves ``track.wav``
    untouched and deletes the part file.
    """
    from vienetts_app.core.audio import read_wav_range
    from vienetts_app.core.studio import render_project_for_subtitle_save

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

    render_id = new_render_id()
    try:
        new_timeline, updated_proj = _spliced_metadata(
            sub_proj, timeline, studio_project, window_mix, orig_end, total_frames
        )
        updated_proj = replace(updated_proj, render_id=render_id)
        # FR-5.7: the same crash-consistent order as a render's commit.
        store.save_timeline(sub_proj.id, new_timeline, render_id=render_id)
        store.save(updated_proj)
    except BaseException:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
        raise
    store.promote_track(sub_proj.id, part, render_id=render_id)
    return updated_proj


def _spliced_metadata(
    sub_proj: SubtitleProject,
    timeline: Timeline,
    studio_project: Any,
    window_mix: np.ndarray,
    orig_end: int,
    total_frames: int,
) -> tuple[Timeline, SubtitleProject]:
    """The timeline and project after splicing ``window_mix`` into the track."""
    from vienetts_app.core.studio import GapOp
    from vienetts_app.core.subtitles import cue_char_spans
    from vienetts_app.core.timeline import SegmentSpan

    meta = studio_project.subtitle_meta
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
    return new_timeline, updated_proj
