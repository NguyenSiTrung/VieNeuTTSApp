"""Non-destructive mini-studio model: clips + op stack + render (48 kHz float32)."""

from __future__ import annotations

import dataclasses
import hashlib
import threading
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

import numpy as np

from vienetts_app.core.align import FitPolicy
from vienetts_app.core.synthesis_context import SynthesisContext

SAMPLE_RATE = 48_000
MAX_STUDIO_SRT_CUES = 50
MIN_GAIN_DB = -20.0
MAX_GAIN_DB = 12.0

# Rack defaults the QML sliders open with; effective_controls reports these when
# the chain carries no op for that parameter.
DEFAULT_GAP_MS = 500
DEFAULT_FADE_MS = 200


@dataclass(frozen=True)
class SubtitleStudioMeta:
    """Subtitle workspace provenance + active window bounds for an SRT StudioProject."""

    project_id: str
    title: str
    total_cues: int
    window_start: int
    window_end: int
    policy: FitPolicy
    sample_rate: int = SAMPLE_RATE
    prev_cue_end_ms: int | None = None
    orig_window_start_frame: int = 0
    orig_window_end_frame: int = 0
    windows: tuple[dict[str, Any], ...] = ()


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
    #: Optional SRT cue metadata (``None`` for non-subtitle clips).
    cue_index: int | None = None
    srt_start_ms: int = 0
    srt_end_ms: int = 0
    placed_start_ms: int = 0
    placed_end_ms: int = 0
    lead_silence_ms: int = 0
    fit_rate: float = 1.0
    overflow_ms: int = 0
    raw_audio: np.ndarray | None = None


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
    #: Undo steps: each entry is the ``ops`` tuple as it was BEFORE one
    #: step, newest last. ``undo_step`` restores the newest entry, so a step
    #: that applied N ops — or replaced a rack value in place, or dropped the
    #: tail of the stack — reverts in one undo. Every op-stack edit the UI
    #: makes is one step: ``commit_ops`` (apply), ``revert_ops`` (a history
    #: chip) and ``reset_ops`` (back to the original take). The raw folding
    #: primitives (``push_op``, ``set_parameter_op``, ``pop_op``) return a
    #: project without history, and ``undo_step`` then falls back to dropping
    #: the newest op. Clip edits keep it (they do not touch ``ops``).
    history: tuple = ()
    #: Present when this Studio project was loaded from a rendered SRT project.
    subtitle_meta: SubtitleStudioMeta | None = None


def push_op(project: StudioProject, op: StudioOp) -> StudioProject:
    return dataclasses.replace(project, ops=project.ops + (op,), history=())


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
    return dataclasses.replace(project, ops=tuple(ops), history=())


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


def _concat(
    project: StudioProject,
    gap_frames: int = 0,
    *,
    has_gap_op: bool = False,
    include_first_lead: bool = False,
) -> np.ndarray:
    if not project.clips:
        raise ValueError("studio project has no clips")
    if project.subtitle_meta is not None and not has_gap_op:
        joined_srt: list[np.ndarray] = []
        for idx, clip in enumerate(project.clips):
            use_lead = idx > 0 or include_first_lead or project.subtitle_meta.window_start == 0
            lead_ms = max(0, int(clip.lead_silence_ms)) if use_lead else 0
            lead_frames = int(round(lead_ms * SAMPLE_RATE / 1000))
            if lead_frames > 0:
                joined_srt.append(np.zeros(lead_frames, dtype=np.float32))
            joined_srt.append(np.ascontiguousarray(clip.audio, dtype=np.float32))
        if not joined_srt:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(joined_srt).astype(np.float32)
    parts = [np.ascontiguousarray(c.audio, dtype=np.float32) for c in project.clips]
    if gap_frames <= 0 or len(parts) == 1:
        return np.concatenate(parts).astype(np.float32)
    gap = np.zeros(gap_frames, dtype=np.float32)
    joined: list[np.ndarray] = [parts[0]]
    for part in parts[1:]:
        joined.append(gap)
        joined.append(part)
    return np.concatenate(joined).astype(np.float32)


# The last time-stretched mix, keyed on (input mix digest, factor) — Task 5.2.
# WSOLA is the one expensive op in a render, and every Apply re-renders the
# whole chain, so a gain/fade/normalize after a speed change used to re-stretch
# the entire mix. One entry: the chain being edited is the only one that
# repeats. Renders run on the background runner, hence the lock.
_stretch_lock = threading.Lock()
_stretch_cache: tuple[tuple[bytes, float], np.ndarray] | None = None


def clear_stretch_cache() -> None:
    """Forget the cached stretched mix (tests; frees its memory)."""
    global _stretch_cache
    with _stretch_lock:
        _stretch_cache = None


def stretch_cache_size() -> int:
    with _stretch_lock:
        return 0 if _stretch_cache is None else 1


def _mix_identity(mix: np.ndarray) -> bytes:
    # A content digest, not ``id()``: equal mixes rebuilt from the same clips
    # and ops must hit, and a reused id of a freed array must not.
    digest = hashlib.sha1(memoryview(mix), usedforsecurity=False).digest()
    return mix.size.to_bytes(8, "little") + digest


def _stretched(mix: np.ndarray, factor: float) -> np.ndarray:
    """``time_stretch_audio(mix, factor)``, reusing the last identical stretch.

    Callers always get their own array: the cached one is never handed out,
    so a caller writing into its render cannot corrupt the next one.
    """
    global _stretch_cache
    from vienetts_app.core.audio import time_stretch_audio

    mix = np.ascontiguousarray(mix, dtype=np.float32)
    key = (_mix_identity(mix), float(factor))
    with _stretch_lock:
        cached = _stretch_cache
    if cached is not None and cached[0] == key:
        return cached[1].copy()
    stretched = time_stretch_audio(mix, factor)
    with _stretch_lock:
        _stretch_cache = (key, stretched.copy())
    return stretched


def _render_project_impl(project: StudioProject, *, include_first_lead: bool = False) -> np.ndarray:
    gap_frames = 0
    has_gap_op = False
    for op in project.ops:
        if isinstance(op, GapOp):
            gap_frames = int(op.ms * SAMPLE_RATE / 1000)
            has_gap_op = True
    mix = _concat(
        project,
        gap_frames,
        has_gap_op=has_gap_op,
        include_first_lead=include_first_lead,
    )
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
            mix = _stretched(mix, op.factor)
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


def render_project(project: StudioProject) -> np.ndarray:
    return _render_project_impl(project, include_first_lead=False)


def render_project_for_subtitle_save(project: StudioProject) -> np.ndarray:
    """Render an SRT window including its leading gap from the previous cue."""
    return _render_project_impl(project, include_first_lead=True)


def pop_op(project: StudioProject) -> StudioProject:
    if not project.ops:
        raise ValueError("nothing to undo")
    return dataclasses.replace(project, ops=project.ops[:-1], history=())


def revert_ops(project: StudioProject, keep: int) -> StudioProject:
    """Keep only the first ``keep`` ops, recorded as ONE undoable step.

    Backs the history chips: Undo right after a revert restores the exact
    pre-revert stack, and the steps recorded before it stay walkable. Keeping
    every op (or more) changes nothing and returns ``project`` unchanged.
    """
    keep = int(keep)
    if keep < 0:
        raise ValueError(f"cannot keep {keep} ops")
    if keep >= len(project.ops):
        return project
    return dataclasses.replace(
        project, ops=project.ops[:keep], history=(*project.history, project.ops)
    )


def reset_ops(project: StudioProject) -> StudioProject:
    """Clear all applied ops (one undoable step), restoring the untouched audio."""
    return revert_ops(project, 0)


# ── pending edits (effects panel) + one-step commit/undo ────────────────────
#
# The effects panel does not touch the op stack while the user experiments:
# edits are STAGED into a small ``pending`` tuple, auditioned through
# ``preview_project`` (a throwaway project — the real one is never mutated),
# and pushed by ``commit_ops`` as ONE history step that ``undo_step`` reverts
# in one go. Range edits (trim/cut) stay immediate timeline actions: their
# frame positions only mean something against the mix the user is looking at.

#: Keys of the idempotent one-shot effects; at most one of each is staged.
_ONE_SHOT_KEYS: dict[type, str] = {NormalizeOp: "normalize", SilenceTrimOp: "silence"}


def staging_key(op: StudioOp) -> str:
    """The pending-list slot ``op`` occupies (one row per key).

    Rack parameters use ``parameter_key`` (``gain``/``speed``/``gap``/
    ``fade_in``/``fade_out``); the idempotent one-shots are ``normalize`` and
    ``silence``. Trim/cut raise ``ValueError``: they cannot be staged.
    """
    key = parameter_key(op) or _ONE_SHOT_KEYS.get(type(op))
    if key is None:
        raise ValueError(f"{type(op).__name__} cannot be staged; apply it directly")
    return key


def stage_op(project: StudioProject, pending: tuple, op: StudioOp) -> tuple:
    """Return ``pending`` with ``op`` staged (``project`` is only read).

    One row per ``staging_key``: re-staging a key replaces its op in place, so
    the row keeps its position (and its order relative to the others). A rack
    parameter whose value is already what the applied stack has — the slider
    dragged back — removes the row instead, because applying it would change
    nothing.
    """
    key = staging_key(op)
    if parameter_key(op) is not None and set_parameter_op(project, op).ops == project.ops:
        return unstage_op(pending, key)
    out = list(pending)
    for i, staged in enumerate(out):
        if staging_key(staged) == key:
            out[i] = op
            return tuple(out)
    return (*out, op)


def unstage_op(pending: tuple, key: str) -> tuple:
    """Return ``pending`` without the row staged under ``key`` (no-op if absent)."""
    return tuple(op for op in pending if staging_key(op) != key)


def _fold_ops(project: StudioProject, ops: Iterable[StudioOp]) -> StudioProject:
    for op in ops:
        project = set_parameter_op(project, op)
    return project


def preview_project(project: StudioProject, pending: Iterable[StudioOp]) -> StudioProject:
    """The project as it would be if ``pending`` were applied — for audition only.

    Ops fold in with ``set_parameter_op`` exactly like ``commit_ops`` does, so
    the preview renders sample-identical to the committed result. The input
    project is untouched (frozen) and the preview carries its history
    unchanged: previewing is not a step. No pending ops returns ``project``.
    """
    pending = tuple(pending)
    if not pending:
        return project
    folded = _fold_ops(project, pending)
    return dataclasses.replace(project, ops=folded.ops)


def commit_ops(project: StudioProject, ops: Iterable[StudioOp]) -> StudioProject:
    """Apply ``ops`` as ONE undoable step (``undo_step`` reverts all of them).

    Raises ``ValueError`` for an empty ``ops``. A commit that leaves the stack
    unchanged (re-applying the current value) records no step and returns
    ``project`` itself.
    """
    ops = tuple(ops)
    if not ops:
        raise ValueError("nothing to apply")
    folded = _fold_ops(project, ops)
    if folded.ops == project.ops:
        return project
    return dataclasses.replace(project, ops=folded.ops, history=(*project.history, project.ops))


def undo_step(project: StudioProject) -> StudioProject:
    """Revert the newest committed step; without recorded steps, drop the newest op.

    Raises ``ValueError`` when the stack is already empty.
    """
    if project.history:
        return dataclasses.replace(project, ops=project.history[-1], history=project.history[:-1])
    return pop_op(project)


def can_undo(project: StudioProject) -> bool:
    """Whether ``undo_step`` would change anything.

    Not the same as "has ops": a revert or a reset can leave the stack empty
    while the step that emptied it is still there to undo.
    """
    return bool(project.history or project.ops)


def undo_target_op(project: StudioProject):
    """The single op the next ``undo_step`` reverts, or ``None``.

    Named only when that step touched exactly one op of the current stack:
    either it appended the newest op (Undo drops it) or it replaced one op in
    place (Undo restores that op's previous value). A step that applied
    several ops, a revert and a reset have no single name.
    """
    if not project.history:
        return project.ops[-1] if project.ops else None
    before, now = project.history[-1], project.ops
    if len(now) == len(before) + 1 and now[:-1] == before:
        return now[-1]
    if len(now) == len(before):
        changed = [i for i, (a, b) in enumerate(zip(before, now, strict=True)) if a != b]
        if len(changed) == 1:
            return now[changed[0]]
    return None


def move_clip(project: StudioProject, clip_id: str, new_index: int) -> StudioProject:
    if project.subtitle_meta is not None:
        raise ValueError("cannot reorder clips in a subtitle project")
    clips = list(project.clips)
    ids = [c.id for c in clips]
    if clip_id not in ids:
        raise ValueError(f"unknown clip {clip_id!r}")
    clip = clips.pop(ids.index(clip_id))
    clips.insert(max(0, min(new_index, len(clips))), clip)
    return dataclasses.replace(project, clips=tuple(clips))


def delete_clip(project: StudioProject, clip_id: str) -> StudioProject:
    """Drop one clip. Ops are preserved and the remaining clips keep their ids.

    Ids are the UI's stable handle, so removing a clip must never renumber the
    others; a project with no clips cannot be rendered, so the last one stays.
    """
    if project.subtitle_meta is not None:
        raise ValueError("cannot delete clips in a subtitle project")
    ids = [c.id for c in project.clips]
    if clip_id not in ids:
        raise ValueError(f"unknown clip {clip_id!r}")
    if len(ids) <= 1:
        raise ValueError("cannot delete the last clip")
    return dataclasses.replace(project, clips=tuple(c for c in project.clips if c.id != clip_id))


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
        out.append(dataclasses.replace(c, text=txt, audio=new, context=context))
    if not found:
        raise ValueError(f"unknown clip {clip_id!r}")
    return dataclasses.replace(project, clips=tuple(out))


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


def subtitle_window_ranges(
    total_cues: int, max_cues: int = MAX_STUDIO_SRT_CUES
) -> list[tuple[int, int]]:
    """Return ``(start_cue, end_cue)`` 0-based slices covering ``total_cues``."""
    total = max(0, int(total_cues))
    step = max(1, int(max_cues))
    return [(s, min(total, s + step)) for s in range(0, total, step)]


def load_project_from_subtitle(
    store: Any,
    project_id: str,
    start_cue: int = 0,
    max_cues: int = MAX_STUDIO_SRT_CUES,
) -> StudioProject:
    """Load a bounded window of cues (<=50) from a rendered SubtitleProject into Studio."""
    from vienetts_app.core.align import frames_for_ms
    from vienetts_app.core.audio import read_wav_range
    from vienetts_app.core.subtitles import format_timestamp

    sub_proj = store.require(project_id)
    timeline = store.load_timeline(project_id)
    if (
        not sub_proj.rendered
        or timeline is None
        or len(timeline.segments) != len(sub_proj.cues)
        or not store.has_track(project_id)
    ):
        raise ValueError("Chưa có âm thanh phụ đề đã tạo để mở trong Studio.")
    if sub_proj.sample_rate != SAMPLE_RATE:
        raise ValueError(f"studio needs 48 kHz, got {sub_proj.sample_rate}")

    total_cues = len(sub_proj.cues)
    cap = max(1, min(int(max_cues), MAX_STUDIO_SRT_CUES))
    start = max(0, min(int(start_cue), total_cues - 1))
    end = min(total_cues, start + cap)

    windows: list[dict[str, Any]] = []
    for w_start in range(0, total_cues, cap):
        w_end = min(total_cues, w_start + cap)
        t0 = int(timeline.segments[w_start].start_ms)
        t1 = int(timeline.segments[w_end - 1].end_ms)
        s0 = format_timestamp(t0).split(",")[0]
        s1 = format_timestamp(t1).split(",")[0]
        if s0.startswith("00:"):
            s0 = s0[3:]
        if s1.startswith("00:"):
            s1 = s1[3:]
        windows.append(
            {
                "start": w_start,
                "end": w_end,
                "startMs": t0,
                "endMs": t1,
                "label": f"{w_start + 1}–{w_end} / {total_cues} ({s0} → {s1})",
            }
        )

    prev_cue_end_ms = None if start == 0 else int(timeline.segments[start - 1].end_ms)
    orig_window_start_frame = (
        0 if prev_cue_end_ms is None else frames_for_ms(prev_cue_end_ms, SAMPLE_RATE)
    )
    orig_window_end_frame = frames_for_ms(int(timeline.segments[end - 1].end_ms), SAMPLE_RATE)
    read_start_frame = frames_for_ms(int(timeline.segments[start].start_ms), SAMPLE_RATE)

    wav_path = store.wav_path(project_id)
    window_audio, sr = read_wav_range(wav_path, read_start_frame, orig_window_end_frame)
    if sr != SAMPLE_RATE:
        raise ValueError(f"studio needs 48 kHz, got {sr}")

    clips: list[StudioClip] = []
    offset_ms = int(sub_proj.policy.offset_ms)
    for i in range(start, end):
        cue = sub_proj.cues[i]
        seg = timeline.segments[i]
        srt_start = max(0, int(cue.start_ms) + offset_ms)
        srt_end = max(srt_start, int(cue.end_ms) + offset_ms)
        placed_start = int(seg.start_ms)
        placed_end = int(seg.end_ms)
        prev_end = 0 if i == 0 else int(timeline.segments[i - 1].end_ms)
        lead_ms = max(0, placed_start - prev_end)
        rel_start = max(0, frames_for_ms(placed_start, SAMPLE_RATE) - read_start_frame)
        rel_end = max(rel_start, frames_for_ms(placed_end, SAMPLE_RATE) - read_start_frame)
        clip_audio = np.ascontiguousarray(window_audio[rel_start:rel_end], dtype=np.float32)
        rate = float(sub_proj.cue_rates[i]) if i < len(sub_proj.cue_rates) else 1.0
        overflow = max(0, placed_end - srt_end)
        clips.append(
            StudioClip(
                id=f"cue{i}",
                label=str(i + 1),
                text=cue.text,
                audio=clip_audio,
                context=sub_proj.context,
                cue_index=i,
                srt_start_ms=srt_start,
                srt_end_ms=srt_end,
                placed_start_ms=placed_start,
                placed_end_ms=placed_end,
                lead_silence_ms=lead_ms,
                fit_rate=rate,
                overflow_ms=overflow,
                raw_audio=clip_audio if abs(rate - 1.0) <= 1e-3 else None,
            )
        )

    meta = SubtitleStudioMeta(
        project_id=sub_proj.id,
        title=sub_proj.title,
        total_cues=total_cues,
        window_start=start,
        window_end=end,
        policy=sub_proj.policy,
        sample_rate=SAMPLE_RATE,
        prev_cue_end_ms=prev_cue_end_ms,
        orig_window_start_frame=orig_window_start_frame,
        orig_window_end_frame=orig_window_end_frame,
        windows=tuple(windows),
    )
    return StudioProject(clips=tuple(clips), ops=(), subtitle_meta=meta)


def refit_subtitle_clips(
    project: StudioProject,
    clip_id: str,
    new_raw_audio: np.ndarray,
    *,
    new_text: str | None = None,
    context: SynthesisContext | None = None,
    custom_rate: float | None = None,
) -> StudioProject:
    """Fit regenerated/updated raw audio into an SRT clip and re-plan downstream window cues."""
    from vienetts_app.core.align import (
        CueFit,
        frames_for_ms,
        ms_for_frames,
        plan_cue,
        stretch_clip_to,
    )
    from vienetts_app.core.subtitles import Cue

    meta = project.subtitle_meta
    if meta is None:
        raise ValueError("project is not a subtitle studio project")
    clips = list(project.clips)
    ids = [c.id for c in clips]
    if clip_id not in ids:
        raise ValueError(f"unknown clip {clip_id!r}")
    if custom_rate is not None:
        rate_val = float(custom_rate)
        if not (MIN_SPEED <= rate_val <= MAX_SPEED):
            raise ValueError(f"speed {rate_val} outside [{MIN_SPEED}, {MAX_SPEED}]")
    else:
        rate_val = None

    target_idx = ids.index(clip_id)
    target = clips[target_idx]
    raw = np.ascontiguousarray(np.asarray(new_raw_audio, dtype=np.float32).ravel())
    natural_ms = ms_for_frames(len(raw), SAMPLE_RATE)
    prev_end_ms = meta.prev_cue_end_ms if target_idx == 0 else clips[target_idx - 1].placed_end_ms
    txt = new_text if new_text is not None else target.text
    ctx = context if context is not None else target.context
    zero_offset_policy = dataclasses.replace(meta.policy, offset_ms=0)

    if rate_val is None:
        cue_obj = Cue(
            index=(target.cue_index or 0) + 1,
            start_ms=target.srt_start_ms,
            end_ms=target.srt_end_ms,
            text=txt,
        )
        fit = plan_cue(cue_obj, natural_ms, zero_offset_policy, prev_end_ms)
        fitted_audio = stretch_clip_to(raw, fit, SAMPLE_RATE)
    else:
        duration_ms = int(round(natural_ms / rate_val)) if rate_val > 0 else natural_ms
        if prev_end_ms is None:
            start_ms = target.srt_start_ms
        elif meta.policy.max_gap_ms > 0:
            start_ms = max(
                int(prev_end_ms),
                min(target.srt_start_ms, int(prev_end_ms) + meta.policy.max_gap_ms),
            )
        else:
            start_ms = max(int(prev_end_ms), target.srt_start_ms)
        end_ms = start_ms + duration_ms
        fit = CueFit(
            index=(target.cue_index or 0) + 1,
            start_ms=start_ms,
            end_ms=end_ms,
            rate=rate_val,
            srt_start_ms=target.srt_start_ms,
            srt_end_ms=target.srt_end_ms,
            natural_ms=natural_ms,
            overflow_ms=max(0, end_ms - target.srt_end_ms),
        )
        target_frames = frames_for_ms(duration_ms, SAMPLE_RATE)
        if target_frames <= 0:
            fitted_audio = np.zeros(0, dtype=np.float32)
        else:
            from vienetts_app.core.audio import time_stretch_audio

            stretched = (
                np.ascontiguousarray(
                    time_stretch_audio(raw, rate=rate_val, sample_rate=SAMPLE_RATE),
                    dtype=np.float32,
                )
                if abs(rate_val - 1.0) > 1e-3
                else raw.copy()
            )
            if stretched.size == target_frames:
                fitted_audio = stretched
            elif stretched.size > target_frames:
                fitted_audio = stretched[:target_frames]
            else:
                padded = np.zeros(target_frames, dtype=np.float32)
                padded[: stretched.size] = stretched
                fitted_audio = padded

    lead_ref = 0 if prev_end_ms is None else int(prev_end_ms)
    clips[target_idx] = dataclasses.replace(
        target,
        text=txt,
        audio=fitted_audio,
        context=ctx,
        placed_start_ms=fit.start_ms,
        placed_end_ms=fit.end_ms,
        lead_silence_ms=max(0, fit.start_ms - lead_ref),
        fit_rate=round(float(fit.rate), 4),
        overflow_ms=fit.overflow_ms,
        raw_audio=raw,
    )

    # Propagate updated placement forward through the remaining clips in the window.
    running_end = fit.end_ms
    for j in range(target_idx + 1, len(clips)):
        cj = clips[j]
        dur_j = max(0, cj.placed_end_ms - cj.placed_start_ms)
        if meta.policy.max_gap_ms > 0:
            start_j = max(running_end, min(cj.srt_start_ms, running_end + meta.policy.max_gap_ms))
        else:
            start_j = max(running_end, cj.srt_start_ms)
        end_j = start_j + dur_j
        clips[j] = dataclasses.replace(
            cj,
            placed_start_ms=start_j,
            placed_end_ms=end_j,
            lead_silence_ms=max(0, start_j - running_end),
            overflow_ms=max(0, end_j - cj.srt_end_ms),
        )
        running_end = end_j

    return dataclasses.replace(project, clips=tuple(clips))


def set_subtitle_clip_speed(
    project: StudioProject,
    clip_id: str,
    custom_rate: float | None = None,
) -> StudioProject:
    """Adjust one SRT clip's speed without re-synthesizing (None = auto-fit to SRT)."""
    from vienetts_app.core.audio import time_stretch_audio

    if project.subtitle_meta is None:
        raise ValueError("project is not a subtitle studio project")
    clips = [c for c in project.clips if c.id == clip_id]
    if not clips:
        raise ValueError(f"unknown clip {clip_id!r}")
    clip = clips[0]
    if clip.raw_audio is not None:
        base = clip.raw_audio
    elif abs(clip.fit_rate - 1.0) > 1e-3 and clip.fit_rate > 0:
        inv_rate = max(MIN_SPEED, min(MAX_SPEED, 1.0 / float(clip.fit_rate)))
        base = np.ascontiguousarray(
            time_stretch_audio(clip.audio, rate=inv_rate, sample_rate=SAMPLE_RATE),
            dtype=np.float32,
        )
    else:
        base = clip.audio
    return refit_subtitle_clips(project, clip_id, base, custom_rate=custom_rate)
