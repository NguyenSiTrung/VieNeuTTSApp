"""Non-destructive mini-studio model: clips + op stack + render (48 kHz float32)."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import numpy as np

from vienetts_app.core.synthesis_context import SynthesisContext

SAMPLE_RATE = 48_000
MIN_GAIN_DB = -20.0
MAX_GAIN_DB = 12.0

# Rack defaults the QML sliders open with; effective_controls reports these when
# the chain carries no op for that parameter.
DEFAULT_GAP_MS = 500
DEFAULT_FADE_MS = 200


@dataclass(frozen=True)
class StudioClip:
    id: str
    label: str
    text: str
    audio: np.ndarray  # mono float32 @48k
    #: Engine identity that produced ``audio``. ``None`` = produced before engine
    #: provenance existed (VieNeu, the only engine the app had then) or by a
    #: caller that cannot say — see ``synthesis_context.same_engine``.
    context: SynthesisContext | None = None


@dataclass(frozen=True)
class TrimOp:
    start_frame: int
    end_frame: int  # exclusive; -1 = end of mix

    def __post_init__(self):
        if self.start_frame < 0 or (self.end_frame != -1 and self.end_frame <= self.start_frame):
            raise ValueError(f"bad trim range {self.start_frame}:{self.end_frame}")


@dataclass(frozen=True)
class CutOp:
    """Remove [start_frame, end_frame) from the mix, splicing the two sides."""

    start_frame: int
    end_frame: int  # exclusive; -1 = end of mix

    def __post_init__(self):
        if self.start_frame < 0 or (self.end_frame != -1 and self.end_frame <= self.start_frame):
            raise ValueError(f"bad cut range {self.start_frame}:{self.end_frame}")


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


StudioOp = TrimOp | CutOp | FadeOp | GainOp | NormalizeOp | SpeedOp | SilenceTrimOp | GapOp


@dataclass(frozen=True)
class StudioProject:
    clips: tuple = ()
    ops: tuple = ()


def push_op(project: StudioProject, op: StudioOp) -> StudioProject:
    return StudioProject(clips=project.clips, ops=project.ops + (op,))


def parameter_key(op: StudioOp) -> str | None:
    """Return the *setting* key for parameter-like ops, else None.

    Ops that the rack shows as one absolute number share a key and must replace
    each other instead of stacking. NormalizeOp/SilenceTrimOp/TrimOp/CutOp are
    one-shot edits of the audio, so they have no key.
    """
    if isinstance(op, GainOp):
        return "gain"
    if isinstance(op, SpeedOp):
        return "speed"
    if isinstance(op, GapOp):
        return "gap"
    if isinstance(op, FadeOp):
        return "fade_in" if op.edge == "in" else "fade_out"
    return None


def set_parameter_op(project: StudioProject, op: StudioOp) -> StudioProject:
    """Set a parameter instead of stacking it.

    The rack slider is an absolute setting ("gain = -3 dB"), so re-applying it
    must not compound — two +3 dB Applies would otherwise render +6 dB, and two
    1.15x Applies 1.3225x, while the UI can only display one number. The last op
    with the same key is therefore replaced *in place*: the chain position stays
    stable, so a later NormalizeOp keeps its meaning. One-shot ops append.
    """
    key = parameter_key(op)
    if key is None:
        return push_op(project, op)
    ops = list(project.ops)
    for i in range(len(ops) - 1, -1, -1):
        if parameter_key(ops[i]) == key:
            ops[i] = op
            break
    else:
        ops.append(op)
    return StudioProject(clips=project.clips, ops=tuple(ops))


def trim_range(project: StudioProject, start_frame: int, end_frame: int) -> StudioProject:
    """Keep only [start_frame, end_frame) of the mix."""
    return push_op(project, TrimOp(start_frame, end_frame))


def cut_range(project: StudioProject, start_frame: int, end_frame: int) -> StudioProject:
    """Delete [start_frame, end_frame) from the mix."""
    return push_op(project, CutOp(start_frame, end_frame))


def effective_controls(project: StudioProject) -> dict[str, float | int]:
    """The values the UI must display, derived from what render_project does.

    Gain sums because dB is logarithmic (a +3 dB then a -1.5 dB op is one
    +1.5 dB setting); speed multiplies because a factor is a ratio. gap/fade
    follow render_project's last-wins semantics.
    """
    gain = 0.0
    speed = 1.0
    gap: int = DEFAULT_GAP_MS
    fade_in = 0
    fade_out = 0
    last_fade: int | None = None
    for op in project.ops:
        if isinstance(op, GainOp):
            gain += op.db
        elif isinstance(op, SpeedOp):
            speed *= op.factor
        elif isinstance(op, GapOp):
            gap = op.ms
        elif isinstance(op, FadeOp):
            last_fade = op.ms
            if op.edge == "in":
                fade_in = op.ms
            else:
                fade_out = op.ms
    return {
        "gain": gain,
        "speed": speed,
        "gap": gap,
        "fade": DEFAULT_FADE_MS if last_fade is None else last_fade,
        "fadeIn": fade_in,
        "fadeOut": fade_out,
    }


def envelope_for(
    audio: np.ndarray, buckets: int = ENVELOPE_BUCKETS, reference_peak: float | None = None
) -> list[float]:
    """Peak-per-bucket envelope in 0..1, shared by the overview and per-clip audition.

    With no ``reference_peak`` the envelope is normalised against its own peak,
    so the loudest bucket reads 1.0. Callers comparing renders pass a shared
    reference instead (render_overview passes the dry project peak) so that pure
    level ops visibly grow/shrink the picture. A degenerate 0 reference (silent
    project) falls back to the buckets' own peak, matching the old inline code.
    """
    n = len(audio)
    if n == 0:
        return [0.0] * buckets
    peaks: list[float] = []
    for i in range(buckets):
        seg = audio[i * n // buckets : (i + 1) * n // buckets]
        peaks.append(float(np.max(np.abs(seg))) if seg.size else 0.0)
    ref = reference_peak or max(peaks) or 1.0
    return [min(p / ref, 1.0) for p in peaks]


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
        elif isinstance(op, CutOp):
            end = len(mix) if op.end_frame == -1 else min(op.end_frame, len(mix))
            if op.start_frame < end:  # an empty/clamped-away range is a no-op
                mix = np.concatenate([mix[: op.start_frame], mix[end:]])
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


def delete_clip(project: StudioProject, clip_id: str) -> StudioProject:
    """Drop one clip. Ops are preserved and the remaining clips keep their ids.

    Ids are the UI's stable handle, so removing a clip must never renumber the
    others; a project with no clips cannot be rendered, so the last one stays.
    """
    ids = [c.id for c in project.clips]
    if clip_id not in ids:
        raise ValueError(f"unknown clip {clip_id!r}")
    if len(ids) <= 1:
        raise ValueError("cannot delete the last clip")
    return StudioProject(clips=tuple(c for c in project.clips if c.id != clip_id), ops=project.ops)


def splice_clip_audio(
    project: StudioProject,
    clip_id: str,
    new_audio: np.ndarray,
    new_text: str | None = None,
    *,
    context: SynthesisContext | None = None,
) -> StudioProject:
    """Replace one clip's audio with a 10 ms crossfade at its head (old→new).

    ``context`` is the engine identity that produced ``new_audio``: the clip's
    recorded provenance becomes it, because the old audio (and whatever
    produced it) is gone once the splice lands.
    """
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
        txt = new_text if new_text is not None else c.text
        out.append(StudioClip(id=c.id, label=c.label, text=txt, audio=new, context=context))
    if not found:
        raise ValueError(f"unknown clip {clip_id!r}")
    return StudioProject(clips=tuple(out), ops=project.ops)


def render_overview(project: StudioProject) -> tuple[np.ndarray, int, list[float]]:
    """Render once; derive duration + waveform envelope from the same mix.

    The controller used to call ``render_project`` and then ``project_envelope``
    (which renders again) — 2x full-mix DSP per Apply on the GUI thread.

    Bands are normalized against the *dry* project peak (max |sample| over the
    clips, scanned without copying), NOT the rendered mix's own peak. A
    per-render normalization divides every render by its own max, which makes
    pure level ops (gain/normalize) pixel-identical in the overview — Apply
    then Undo looked like they changed nothing. Against the dry peak, level
    ops visibly grow/shrink the waveform and undo restores it exactly. The
    bucketing itself lives in ``envelope_for`` so the per-clip audition draws
    the same shape.
    """
    mix = render_project(project)
    duration_ms = int(len(mix) * 1000 / SAMPLE_RATE) if len(mix) else 0
    if mix.size == 0:
        return mix, duration_ms, [0.0] * ENVELOPE_BUCKETS
    dry_peak = 0.0
    for clip in project.clips:
        if clip.audio.size:
            peak = float(np.max(np.abs(clip.audio)))
            if peak > dry_peak:
                dry_peak = peak
    return mix, duration_ms, envelope_for(mix, reference_peak=dry_peak)


def project_envelope(project: StudioProject) -> list[float]:
    _, _, envelope = render_overview(project)
    return envelope


def load_project_from_artifact(
    path: str, text: str, context: SynthesisContext | None = None
) -> StudioProject:
    """One clip per paragraph; audio sliced proportionally to char length.

    v1 approximation (no per-chunk timing exists upstream): the artifact is
    split into contiguous spans proportional to each paragraph's char count.
    Re-generating a clip replaces its audio wholesale, so drift self-heals.

    ``context`` is the engine identity that produced the artifact; every clip
    inherits it (they all come from this one take).
    """
    from vienetts_app.core.audio import read_wav
    from vienetts_app.core.timeline import split_paragraphs

    audio, sr = read_wav(path)
    if sr != SAMPLE_RATE:
        raise ValueError(f"studio needs 48 kHz, got {sr}")
    paras = split_paragraphs(text)
    if not paras:
        return StudioProject(
            clips=(StudioClip(id="c0", label="1", text=text, audio=audio, context=context),),
            ops=(),
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
                context=context,
            )
        )
        cursor += n
    return StudioProject(clips=tuple(clips), ops=())


def load_project_from_chapters(
    store: object,
    book_id: str,
    indices: list[int],
    texts: list[str],
    contexts: Mapping[int, SynthesisContext] | None = None,
) -> StudioProject:
    """One clip per rendered chapter, each carrying the chapter's provenance.

    ``contexts`` is the book's recorded render provenance keyed by chapter
    index (``audiobook.BookState.contexts``): a chapter rendered before engine
    provenance existed has no entry, and its clip stays "unknown engine" so a
    re-synthesis applies the same legacy rule as the audiobook cache.
    """
    from vienetts_app.core.audio import read_wav

    recorded = contexts or {}
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
                context=recorded.get(int(i)),
            )
        )
    if not clips:
        raise ValueError("no rendered chapter audio to open in studio")
    return StudioProject(clips=tuple(clips), ops=())
