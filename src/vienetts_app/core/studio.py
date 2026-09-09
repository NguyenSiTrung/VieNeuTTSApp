"""Non-destructive mini-studio model: clips + op stack + render (48 kHz float32)."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 48_000
MIN_GAIN_DB = -20.0
MAX_GAIN_DB = 12.0


@dataclass(frozen=True)
class StudioClip:
    id: str
    label: str
    text: str
    audio: np.ndarray  # mono float32 @48k


@dataclass(frozen=True)
class TrimOp:
    start_frame: int
    end_frame: int  # exclusive; -1 = end of mix

    def __post_init__(self):
        if self.start_frame < 0 or (self.end_frame != -1 and self.end_frame <= self.start_frame):
            raise ValueError(f"bad trim range {self.start_frame}:{self.end_frame}")


@dataclass(frozen=True)
class FadeOp:
    edge: str  # "in" | "out"
    ms: int

    def __post_init__(self):
        if self.edge not in ("in", "out"):
            raise ValueError(f"edge must be 'in'/'out', got {self.edge!r}")
        if self.ms < 0:
            raise ValueError("fade ms must be >= 0")


@dataclass(frozen=True)
class GainOp:
    db: float

    def __post_init__(self):
        if not (MIN_GAIN_DB <= self.db <= MAX_GAIN_DB):
            raise ValueError(f"gain {self.db} dB outside [{MIN_GAIN_DB}, {MAX_GAIN_DB}]")


@dataclass(frozen=True)
class NormalizeOp:
    peak: float = 1.0

    def __post_init__(self):
        if not 0.0 < self.peak <= 1.0:
            raise ValueError(f"peak must be in (0, 1], got {self.peak}")


@dataclass(frozen=True)
class SilenceTrimOp:
    threshold_db: float = -50.0


@dataclass(frozen=True)
class GapOp:
    ms: int

    def __post_init__(self):
        if self.ms < 0:
            raise ValueError("gap ms must be >= 0")


MIN_SPEED = 0.5
MAX_SPEED = 2.0
REGEN_CROSSFADE_MS = 10.0
ENVELOPE_BUCKETS = 160


@dataclass(frozen=True)
class SpeedOp:
    factor: float

    def __post_init__(self):
        if not (MIN_SPEED <= self.factor <= MAX_SPEED):
            raise ValueError(f"speed {self.factor} outside [{MIN_SPEED}, {MAX_SPEED}]")


StudioOp = TrimOp | FadeOp | GainOp | NormalizeOp | SpeedOp | SilenceTrimOp | GapOp


@dataclass(frozen=True)
class StudioProject:
    clips: tuple = ()
    ops: tuple = ()


def push_op(project: StudioProject, op: StudioOp) -> StudioProject:
    return StudioProject(clips=project.clips, ops=project.ops + (op,))


def _concat(project: StudioProject, gap_frames: int = 0) -> np.ndarray:
    if not project.clips:
        raise ValueError("studio project has no clips")
    parts = [np.ascontiguousarray(c.audio, dtype=np.float32) for c in project.clips]
    if gap_frames <= 0 or len(parts) == 1:
        return np.concatenate(parts).astype(np.float32)
    gap = np.zeros(gap_frames, dtype=np.float32)
    joined: list[np.ndarray] = [parts[0]]
    for part in parts[1:]:
        joined.append(gap)
        joined.append(part)
    return np.concatenate(joined).astype(np.float32)


def render_project(project: StudioProject) -> np.ndarray:
    gap_frames = 0
    for op in project.ops:
        if isinstance(op, GapOp):
            gap_frames = int(op.ms * SAMPLE_RATE / 1000)
    mix = _concat(project, gap_frames)
    for op in project.ops:
        if isinstance(op, TrimOp):
            end = len(mix) if op.end_frame == -1 else min(op.end_frame, len(mix))
            mix = mix[op.start_frame : end]
        elif isinstance(op, GainOp):
            mix = (mix * (10.0 ** (op.db / 20.0))).astype(np.float32)
        elif isinstance(op, NormalizeOp):
            peak = float(np.max(np.abs(mix))) if mix.size else 0.0
            mix = mix if peak <= 1e-9 else (mix * (op.peak / peak)).astype(np.float32)
        elif isinstance(op, FadeOp):
            n = min(int(SAMPLE_RATE * op.ms / 1000), len(mix))
            if n > 0:
                ramp = np.linspace(0.0, 1.0, n, dtype=np.float32)
                mix = mix.copy()
                if op.edge == "in":
                    mix[:n] *= ramp
                else:
                    mix[-n:] *= ramp[::-1]
        elif isinstance(op, SpeedOp):
            from vienetts_app.core.audio import time_stretch_audio

            mix = time_stretch_audio(mix, op.factor)
        elif isinstance(op, SilenceTrimOp):
            if mix.size == 0:
                raise ValueError("silence trim left nothing")
            amp = 10.0 ** (op.threshold_db / 20.0)
            loud = np.nonzero(np.abs(mix) > amp)[0]
            if loud.size == 0:
                raise ValueError("silence trim left nothing")
            mix = mix[int(loud[0]) : int(loud[-1]) + 1]
        elif isinstance(op, GapOp):
            pass
    return np.ascontiguousarray(mix, dtype=np.float32)


def pop_op(project: StudioProject) -> StudioProject:
    if not project.ops:
        raise ValueError("nothing to undo")
    return StudioProject(clips=project.clips, ops=project.ops[:-1])


def reset_ops(project: StudioProject) -> StudioProject:
    """Clear all applied ops on the project, restoring the untouched audio."""
    return StudioProject(clips=project.clips, ops=())


def move_clip(project: StudioProject, clip_id: str, new_index: int) -> StudioProject:
    clips = list(project.clips)
    ids = [c.id for c in clips]
    if clip_id not in ids:
        raise ValueError(f"unknown clip {clip_id!r}")
    clip = clips.pop(ids.index(clip_id))
    clips.insert(max(0, min(new_index, len(clips))), clip)
    return StudioProject(clips=tuple(clips), ops=project.ops)


def splice_clip_audio(project: StudioProject, clip_id: str, new_audio: np.ndarray) -> StudioProject:
    """Replace one clip's audio with a 10 ms crossfade at its head (old→new)."""
    n_fade = int(SAMPLE_RATE * REGEN_CROSSFADE_MS / 1000)
    out: list[StudioClip] = []
    found = False
    for c in project.clips:
        if c.id != clip_id:
            out.append(c)
            continue
        found = True
        new = np.ascontiguousarray(new_audio, dtype=np.float32)
        old = np.ascontiguousarray(c.audio, dtype=np.float32)
        if 0 < n_fade <= len(old) and n_fade <= len(new):
            blend = np.linspace(0.0, 1.0, n_fade, dtype=np.float32)
            head = old[:n_fade] * (1.0 - blend) + new[:n_fade] * blend
            new = np.concatenate([head, new[n_fade:]])
        out.append(StudioClip(id=c.id, label=c.label, text=c.text, audio=new))
    if not found:
        raise ValueError(f"unknown clip {clip_id!r}")
    return StudioProject(clips=tuple(out), ops=project.ops)


def project_envelope(project: StudioProject) -> list[float]:
    mix = render_project(project)
    if mix.size == 0:
        return [0.0] * ENVELOPE_BUCKETS
    peaks: list[float] = []
    for i in range(ENVELOPE_BUCKETS):
        seg = mix[i * len(mix) // ENVELOPE_BUCKETS : (i + 1) * len(mix) // ENVELOPE_BUCKETS]
        peaks.append(float(np.max(np.abs(seg))) if seg.size else 0.0)
    loudest = max(peaks) or 1.0
    return [min(p / loudest, 1.0) for p in peaks]


def load_project_from_artifact(path: str, text: str) -> StudioProject:
    """One clip per paragraph; audio sliced proportionally to char length.

    v1 approximation (no per-chunk timing exists upstream): the artifact is
    split into contiguous spans proportional to each paragraph's char count.
    Re-generating a clip replaces its audio wholesale, so drift self-heals.
    """
    from vienetts_app.core.audio import read_wav
    from vienetts_app.core.timeline import split_paragraphs

    audio, sr = read_wav(path)
    if sr != SAMPLE_RATE:
        raise ValueError(f"studio needs 48 kHz, got {sr}")
    paras = split_paragraphs(text)
    if not paras:
        return StudioProject(
            clips=(StudioClip(id="c0", label="1", text=text, audio=audio),), ops=()
        )
    total_chars = sum(len(p["text"]) for p in paras) or 1
    clips: list[StudioClip] = []
    cursor = 0
    for i, p in enumerate(paras):
        if i == len(paras) - 1:
            n = len(audio) - cursor
        else:
            n = int(round(len(audio) * len(p["text"]) / total_chars))
        clips.append(
            StudioClip(
                id=f"c{i}",
                label=str(i + 1),
                text=p["text"],
                audio=np.ascontiguousarray(audio[cursor : cursor + n], dtype=np.float32),
            )
        )
        cursor += n
    return StudioProject(clips=tuple(clips), ops=())


def load_project_from_chapters(
    store: object, book_id: str, indices: list[int], texts: list[str]
) -> StudioProject:
    from vienetts_app.core.audio import read_wav

    clips: list[StudioClip] = []
    for i, text in zip(indices, texts, strict=False):
        wav = store.chapter_wav_path(book_id, i)  # AudiobookStore.chapter_wav_path
        if not wav.is_file():
            continue
        audio, sr = read_wav(wav)
        if sr != SAMPLE_RATE:
            raise ValueError(f"studio needs 48 kHz, got {sr}")
        clips.append(
            StudioClip(
                id=f"ch{i}",
                label=f"Ch {i + 1}",
                text=text,
                audio=np.ascontiguousarray(audio, dtype=np.float32),
            )
        )
    if not clips:
        raise ValueError("no rendered chapter audio to open in studio")
    return StudioProject(clips=tuple(clips), ops=())
