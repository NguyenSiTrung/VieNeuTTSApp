"""Speaker diarization and speaker reference clip extraction for multi-speaker video dubbing.

Supports:
1. ``nemotron_gguf`` — ``audiocpp_cli`` + ``Nemotron-3-Diarization-GGUF`` turns,
   the accurate on-device path (CPU/Vulkan/Metal).
2. ``acoustic`` — Built-in zero-dependency NumPy spectral + pitch ($F_0$) + filterbank
   speaker embedding extractor and cosine agglomerative clustering. This is a
   fallback, not a peer of the model: it is labelled in the UI as such.
3. Reference clip selection & slicing (`select_speaker_reference_clip`,
   `extract_speaker_reference_wav`) so any detected speaker can be cloned directly
   from the source video in one click.

``auto`` tries the model, then acoustic, and records the reason for the fallback
in ``notes`` so the UI never presents heuristic labels as model output.
"""

from __future__ import annotations

import bisect
import contextlib
import json
import logging
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Protocol, TypeVar

import numpy as np

from vienetts_app.core.ai_models import (
    cli_command,
    resolve_audiocpp_cli,
    resolve_diarization_model_path,
)
from vienetts_app.core.audio import read_wav_range, write_wav_file
from vienetts_app.core.audio_prep import (
    AudioPrepCancelled,
    AudioPrepError,
    cache_key_paths,
    ensure_source_16k,
)
from vienetts_app.core.onnx_audio import (
    SileroVad,
    SpeakerEmbedder,
    installed_silero_vad,
    installed_speaker_embedder,
)
from vienetts_app.core.processes import CliCancelled, CliTimeout, run_cli
from vienetts_app.core.subtitles import Cue
from vienetts_app.core.video_dub import VideoDubError, extract_video_audio

logger = logging.getLogger(__name__)


class DiarizationError(RuntimeError):
    """Speaker diarization or reference clip extraction failed; message is user-actionable."""


class DiarizationCancelled(DiarizationError):
    """The user cancelled; the ``auto`` fallback chain must never swallow this."""


@dataclass(frozen=True)
class DiarizationConfig:
    backend: str = "auto"  # "auto" | "nemotron_gguf" | "acoustic"
    max_speakers: int = 0  # 0 = auto; >0 = hard ceiling (capped at 8)
    model: str = "nemotron-3-diarization-q8_0"
    device: str = "auto"  # "auto" | "cpu" | "vulkan" | "metal"
    # 0 = automatic (logical cores, capped at 16). Passed to --threads.
    threads: int = 0
    # 0 = derive from audio duration. Guards against a hung/crashed child.
    timeout_s: float = 0.0


@dataclass(frozen=True)
class RttmTurn:
    start_ms: int
    end_ms: int
    speaker: str


def parse_audiocpp_turns(json_text: str, sample_rate: int = 16_000) -> list[RttmTurn]:
    """Parse ``audio.cpp`` ``--turns-out`` / ``speaker_turns`` JSON into ``RttmTurn``s."""
    try:
        payload = json.loads(json_text)
    except json.JSONDecodeError as exc:
        raise DiarizationError(
            f"Tệp kết quả phân tách người nói từ audio.cpp không hợp lệ: {exc}"
        ) from exc

    sr = max(1, int(sample_rate))
    items: list[Any]
    if isinstance(payload, dict):
        # The CLI never writes sample_rate; only the server's SSE wrapper does.
        # Coerce textually to keep a float-ish value from raising ValueError.
        with contextlib.suppress(TypeError, ValueError):
            sr = max(1, int(float(payload.get("sample_rate") or sr)))
        raw_list = payload.get("speaker_turns")
        items = raw_list if isinstance(raw_list, list) else []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    speaker_order: dict[str, str] = {}
    turns: list[RttmTurn] = []
    for entry in items:
        if not isinstance(entry, dict):
            continue
        raw_spk = str(entry.get("speaker_id") or entry.get("speaker") or "speaker_0").strip()
        if raw_spk not in speaker_order:
            speaker_order[raw_spk] = f"SPEAKER_{len(speaker_order) + 1:02d}"
        if "start_sample" in entry and "end_sample" in entry:
            # Tolerate JSON numbers serialized as strings ("1760.0").
            try:
                s_samp = max(0, int(float(entry["start_sample"])))
                e_samp = max(s_samp + 1, int(float(entry["end_sample"])))
            except (TypeError, ValueError):
                continue
            start_ms = int(round(s_samp * 1000.0 / sr))
            end_ms = max(start_ms + 1, int(round(e_samp * 1000.0 / sr)))
        else:
            start_ms = max(0, int(round(float(entry.get("start_time", 0.0)) * 1000.0)))
            end_ms = max(
                start_ms + 1, int(round(float(entry.get("end_time", start_ms / 1000.0)) * 1000.0))
            )
        turns.append(RttmTurn(start_ms, end_ms, speaker_order[raw_spk]))
    return turns


def parse_rttm_turns(rttm_text: str) -> list[RttmTurn]:
    """Parse NIST RTTM ``SPEAKER`` lines and normalize labels to ``SPEAKER_01``..``SPEAKER_NN``."""
    raw_turns: list[tuple[int, int, str]] = []
    speaker_order: dict[str, str] = {}
    for line in rttm_text.splitlines():
        parts = line.strip().split()
        if len(parts) < 8 or parts[0].upper() != "SPEAKER":
            continue
        try:
            start_s = float(parts[3])
            dur_s = float(parts[4])
        except ValueError:
            continue
        raw_label = parts[7]
        if raw_label not in speaker_order:
            speaker_order[raw_label] = f"SPEAKER_{len(speaker_order) + 1:02d}"
        start_ms = max(0, int(round(start_s * 1000)))
        end_ms = max(start_ms + 1, int(round((start_s + max(0.0, dur_s)) * 1000)))
        raw_turns.append((start_ms, end_ms, speaker_order[raw_label]))
    return [RttmTurn(s, e, spk) for s, e, spk in raw_turns]


def refine_cues_speech_bounds(
    cues: Sequence[Cue],
    wav_path: str | Path | None = None,
    *,
    turns: Sequence[RttmTurn] | None = None,
    threshold_db: float = -42.0,
    pad_ms: int = 30,
    data_dir: Path | None = None,
) -> list[Cue]:
    """Refine each ``Cue``'s ``[speech_start_ms, speech_end_ms]`` inside ``[start_ms, end_ms]``.

    Uses a VAD over ``wav_path`` when readable and intersects with ``turns``
    when provided: Silero when its model is installed under ``data_dir``,
    else a 10 ms RMS energy VAD. Keeps ``Cue.start_ms`` and ``Cue.end_ms``
    untouched, and leaves existing bounds unchanged when the WAV slice is
    silent or unreadable and no overlapping turns apply.

    The WAV is opened once, and cues and turns are swept in start order with
    two pointers. Opening the file per cue and scanning every turn per cue
    made a long video's pass O(C * T) plus C file opens.
    """
    if not cues:
        return []

    abs_thresh = float(10.0 ** (float(threshold_db) / 20.0))
    pad = max(0, int(pad_ms))
    wav_file = _open_wav_for_vad(wav_path)
    vad = None if wav_file is None else installed_silero_vad(data_dir)
    order = sorted(range(len(cues)), key=lambda i: cues[i].start_ms)
    pending = sorted(turns or (), key=lambda t: t.start_ms)
    next_turn = 0
    live: list[RttmTurn] = []
    out = list(cues)
    try:
        for i in order:
            cue = cues[i]
            # Cues come in start order, so a turn that ended before this cue
            # starts can overlap no later cue either.
            while next_turn < len(pending) and pending[next_turn].start_ms < cue.end_ms:
                live.append(pending[next_turn])
                next_turn += 1
            live = [t for t in live if t.end_ms > cue.start_ms]

            vad_bounds = (
                None if wav_file is None else _cue_vad_bounds(wav_file, cue, abs_thresh, pad, vad)
            )
            turn_bounds = _cue_turn_bounds(cue, live)
            chosen_bounds: tuple[int, int] | None = None
            if vad_bounds is not None and turn_bounds is not None:
                inter_start = max(vad_bounds[0], turn_bounds[0])
                inter_end = min(vad_bounds[1], turn_bounds[1])
                chosen_bounds = (inter_start, inter_end) if inter_end > inter_start else vad_bounds
            elif vad_bounds is not None:
                chosen_bounds = vad_bounds
            elif turn_bounds is not None:
                chosen_bounds = turn_bounds

            if chosen_bounds is not None:
                s_ms = max(cue.start_ms, min(cue.end_ms, int(chosen_bounds[0])))
                e_ms = max(s_ms, min(cue.end_ms, int(chosen_bounds[1])))
                out[i] = replace(cue, speech_start_ms=s_ms, speech_end_ms=e_ms)
    finally:
        if wav_file is not None:
            with contextlib.suppress(Exception):
                wav_file.close()
    return out


def _open_wav_for_vad(wav_path: str | Path | None) -> Any | None:
    """An open ``soundfile.SoundFile`` with audio in it, or ``None`` (no VAD)."""
    if wav_path is None or not Path(wav_path).is_file():
        return None
    try:
        import soundfile as sf

        wav_file = sf.SoundFile(str(wav_path))
    except Exception:  # noqa: BLE001 - an unreadable WAV only disables the VAD
        return None
    if int(wav_file.samplerate) > 0 and int(wav_file.frames) > 0:
        return wav_file
    wav_file.close()
    return None


def _read_mono_ms(wav_file: Any, start_ms: int, end_ms: int) -> np.ndarray | None:
    """``[start_ms, end_ms)`` of an open ``SoundFile`` as float32 mono, or ``None``.

    ``None`` when the span is empty after clamping to the file or unreadable.
    """
    sr = int(wav_file.samplerate)
    total_frames = int(wav_file.frames)
    start_f = max(0, min(total_frames, int(round(start_ms * sr / 1000.0))))
    stop_f = max(start_f, min(total_frames, int(round(end_ms * sr / 1000.0))))
    if stop_f <= start_f:
        return None
    try:
        wav_file.seek(start_f)
        block = wav_file.read(stop_f - start_f, dtype="float32", always_2d=True)
    except Exception:  # noqa: BLE001 - a bad slice only disables scoring of this span
        return None
    # Downmix: the shared 16 kHz input is mono, but a caller's stereo WAV used
    # to be flattened frame-interleaved, doubling every offset.
    mono = np.ascontiguousarray(block[:, 0] if block.shape[1] == 1 else block.mean(axis=1))
    return mono if mono.size > 0 else None


def _window_rms(mono: np.ndarray, win_frames: int) -> np.ndarray:
    """RMS of each ``win_frames`` window of ``mono``, a short tail window included."""
    n_wins = mono.size // win_frames
    if n_wins > 0:
        framed = mono[: n_wins * win_frames].reshape(n_wins, win_frames)
        rms = np.sqrt(np.mean(framed * framed, axis=1, dtype=np.float64))
        rem = mono[n_wins * win_frames :]
        if rem.size > 0:
            rem_rms = np.sqrt(np.mean(rem * rem, dtype=np.float64))
            rms = np.append(rms, rem_rms)
        return rms
    return np.array([float(np.sqrt(np.mean(mono * mono, dtype=np.float64)))], dtype=np.float64)


def _energy_vad(rms: np.ndarray, abs_thresh: float) -> np.ndarray | None:
    """Indices of the speech windows in ``rms``; ``None`` when the span is silent.

    A window is speech when it reaches both ``abs_thresh`` and 10 % of the
    loudest window, so a quiet room tone under loud speech is not speech.
    """
    peak_rms = float(np.max(rms)) if rms.size > 0 else 0.0
    if peak_rms <= abs_thresh:
        return None
    return np.flatnonzero(rms >= max(abs_thresh, peak_rms * 0.10))


def _speech_extent(
    mono: np.ndarray, sr: int, abs_thresh: float, vad: SileroVad | None
) -> tuple[int, int] | None:
    """First speech sample and end of the last speech in ``mono``; ``None`` when silent.

    Silero when given, else the energy VAD. A Silero failure falls back to
    the energy VAD for this slice rather than losing the bounds.
    """
    if vad is not None:
        try:
            spans = vad.speech_spans(mono, sr)
        except Exception as exc:  # noqa: BLE001 - ORT raises its own types
            logger.warning("Silero VAD failed, using the energy VAD: %s", exc)
        else:
            return (spans[0][0], spans[-1][1]) if spans else None
    win_frames = max(1, int(round(sr * 0.010)))
    active = _energy_vad(_window_rms(mono, win_frames), abs_thresh)
    if active is None or active.size == 0:
        return None
    return int(active[0]) * win_frames, min(mono.size, (int(active[-1]) + 1) * win_frames)


def _cue_vad_bounds(
    wav_file: Any, cue: Cue, abs_thresh: float, pad: int, vad: SileroVad | None = None
) -> tuple[int, int] | None:
    """VAD speech span of ``cue`` (padded, clamped to the cue), or ``None``."""
    mono = _read_mono_ms(wav_file, cue.start_ms, cue.end_ms)
    if mono is None:
        return None
    sr = int(wav_file.samplerate)
    extent = _speech_extent(mono, sr, abs_thresh, vad)
    if extent is None:
        return None
    onset_samples, offset_samples = extent
    raw_start_ms = cue.start_ms + int(round(onset_samples * 1000.0 / sr))
    raw_end_ms = cue.start_ms + int(round(offset_samples * 1000.0 / sr))
    v_start = max(cue.start_ms, min(cue.end_ms, raw_start_ms - pad))
    v_end = max(v_start, min(cue.end_ms, raw_end_ms + pad))
    return (v_start, v_end) if v_end > v_start else None


def _cue_turn_bounds(cue: Cue, turns: Sequence[RttmTurn]) -> tuple[int, int] | None:
    """Span of the turns overlapping ``cue`` (its own speaker's when any), clamped."""
    overlapping = [t for t in turns if min(cue.end_ms, t.end_ms) > max(cue.start_ms, t.start_ms)]
    if not overlapping:
        return None
    spk = (cue.speaker or "").strip()
    spk_turns = [t for t in overlapping if t.speaker == spk] if spk else []
    chosen_turns = spk_turns if spk_turns else overlapping
    t_start = max(cue.start_ms, min(t.start_ms for t in chosen_turns))
    t_end = min(cue.end_ms, max(t.end_ms for t in chosen_turns))
    return (t_start, t_end) if t_end > t_start else None


def assign_speakers_from_turns(
    cues: Sequence[Cue],
    turns: Sequence[RttmTurn],
    *,
    wav_path: str | Path | None = None,
    data_dir: Path | None = None,
) -> list[Cue]:
    """Assign each ``Cue`` to the RTTM speaker turn with maximum temporal overlap."""
    if not cues:
        return []
    if not turns:
        out = [replace(c, speaker=c.speaker or "SPEAKER_01") for c in cues]
        return refine_cues_speech_bounds(out, wav_path, data_dir=data_dir)

    out = []
    for cue in cues:
        overlap_by_spk: dict[str, int] = {}
        for turn in turns:
            ov = min(cue.end_ms, turn.end_ms) - max(cue.start_ms, turn.start_ms)
            if ov > 0:
                overlap_by_spk[turn.speaker] = overlap_by_spk.get(turn.speaker, 0) + ov
        if overlap_by_spk:
            best_spk = max(overlap_by_spk.items(), key=lambda kv: kv[1])[0]
        else:
            # Nearest turn in time
            mid = (cue.start_ms + cue.end_ms) // 2
            nearest = min(
                turns,
                key=lambda t: min(abs(mid - t.start_ms), abs(mid - t.end_ms)),
            )
            best_spk = nearest.speaker
        out.append(replace(cue, speaker=best_spk))
    return refine_cues_speech_bounds(out, wav_path, turns=turns, data_dir=data_dir)


#: One timed word: ``(start_ms, end_ms, text)`` in absolute media time — the
#: shape of ``asr.RawSegment.words``.
WordTiming = tuple[int, int, str]


class WordTimedSegment(Protocol):
    """What :func:`split_segments_at_turns` reads; ``asr.RawSegment`` satisfies it.

    Parts are built with ``dataclasses.replace(..., start_ms, end_ms, text,
    speaker, words, speech_start_ms, speech_end_ms)``, so a segment type must
    be a dataclass with those fields.
    """

    @property
    def start_ms(self) -> int: ...

    @property
    def end_ms(self) -> int: ...

    @property
    def text(self) -> str: ...

    @property
    def words(self) -> tuple[WordTiming, ...]: ...


_SegmentT = TypeVar("_SegmentT", bound=WordTimedSegment)


def split_segments_at_turns(
    segments: Sequence[_SegmentT],
    turns: Sequence[RttmTurn],
    *,
    min_part_ms: int = 600,
    min_words: int = 2,
) -> list[_SegmentT]:
    """Split ASR segments at mid-segment speaker changes, on word boundaries.

    Each word is labelled with the speaker it overlaps most, so a word that
    straddles a turn change goes to the side holding most of it — the cut
    lands on the word boundary nearest the change. Runs of same-speaker words
    become parts; a run shorter than ``min_part_ms`` or with fewer than
    ``min_words`` words is folded into its closer neighbour, so a one-word
    interjection never fragments a sentence. Parts are labelled by max
    overlap, take their speech bounds from their words, and the outer parts
    keep the segment's own edges.

    A segment without words, without a speaker change, or that cannot split
    with every part long enough is returned unchanged; its label stays with
    :func:`assign_speakers_from_turns` (the max-overlap rule).
    """
    if not turns:
        return list(segments)
    ordered = sorted(turns, key=lambda t: (t.start_ms, t.end_ms))
    out: list[_SegmentT] = []
    for seg in segments:
        out.extend(_split_segment_at_turns(seg, ordered, min_part_ms, max(1, min_words)))
    return out


def _split_segment_at_turns(
    seg: _SegmentT, turns: Sequence[RttmTurn], min_part_ms: int, min_words: int
) -> list[_SegmentT]:
    words = tuple(seg.words or ())
    if len(words) < 2 * min_words:
        return [seg]
    near = [t for t in turns if t.end_ms > seg.start_ms and t.start_ms < seg.end_ms]
    if len({t.speaker for t in near}) < 2:
        return [seg]
    labels = _label_words(words, near)
    if labels is None:
        return [seg]

    # Runs of consecutive same-speaker words: [first, stop, speaker].
    runs: list[list[Any]] = []
    for idx, label in enumerate(labels):
        if runs and runs[-1][2] == label:
            runs[-1][1] = idx + 1
        else:
            runs.append([idx, idx + 1, label])

    def bounds(k: int) -> tuple[int, int]:
        first, stop, _ = runs[k]
        start = seg.start_ms if k == 0 else words[first][0]
        end = seg.end_ms if k == len(runs) - 1 else words[stop - 1][1]
        return start, end

    def too_short(k: int) -> bool:
        start, end = bounds(k)
        return runs[k][1] - runs[k][0] < min_words or end - start < min_part_ms

    while len(runs) > 1:
        weak = [k for k in range(len(runs)) if too_short(k)]
        if not weak:
            break
        k = min(weak, key=lambda i: (runs[i][1] - runs[i][0], bounds(i)[1] - bounds(i)[0]))
        if k == 0:
            into = 1
        elif k == len(runs) - 1:
            into = k - 1
        else:
            gap_prev = words[runs[k][0]][0] - words[runs[k - 1][1] - 1][1]
            gap_next = words[runs[k + 1][0]][0] - words[runs[k][1] - 1][1]
            into = k - 1 if gap_prev <= gap_next else k + 1
        lo, hi = min(k, into), max(k, into)
        runs[lo : hi + 1] = [[runs[lo][0], runs[hi][1], runs[into][2]]]
        coalesced: list[list[Any]] = []
        for run in runs:
            if coalesced and coalesced[-1][2] == run[2]:
                coalesced[-1][1] = run[1]
            else:
                coalesced.append(run)
        runs = coalesced
    if len(runs) < 2:
        return [seg]

    tokens = seg.text.split()
    same_tokens = len(tokens) == len(words)
    parts: list[_SegmentT] = []
    for k, (first, stop, label) in enumerate(runs):
        start, end = bounds(k)
        part_words = words[first:stop]
        # Keep the segment's own spelling when its tokens map 1:1 onto the words.
        part_tokens = tokens[first:stop] if same_tokens else [w[2] for w in part_words]
        text = " ".join(part_tokens)
        speech_start = max(start, min(end, part_words[0][0]))
        speech_end = max(speech_start, min(end, part_words[-1][1]))
        parts.append(
            replace(
                seg,  # type: ignore[type-var]
                start_ms=start,
                end_ms=end,
                text=text,
                speaker=_max_overlap_speaker(start, end, turns) or label,
                words=part_words,
                speech_start_ms=speech_start,
                speech_end_ms=speech_end,
            )
        )
    return parts


def _label_words(words: Sequence[WordTiming], turns: Sequence[RttmTurn]) -> list[str] | None:
    """Speaker per word by max overlap; ties keep the previous word's speaker.

    Words outside every turn inherit their neighbour's label. ``None`` when no
    word overlaps any turn.
    """
    labels: list[str | None] = []
    prev: str | None = None
    for w_start, w_end, _ in words:
        w_stop = max(w_end, w_start + 1)
        overlap: dict[str, int] = {}
        for turn in turns:
            ov = min(w_stop, turn.end_ms) - max(w_start, turn.start_ms)
            if ov > 0:
                overlap[turn.speaker] = overlap.get(turn.speaker, 0) + ov
        if overlap:
            best = max(overlap.values())
            leaders = [spk for spk, ov in overlap.items() if ov == best]
            prev = prev if prev in leaders else leaders[0]
            labels.append(prev)
        else:
            labels.append(None)
    known = next((label for label in labels if label is not None), None)
    if known is None:
        return None
    filled: list[str] = []
    for label in labels:
        known = label if label is not None else known
        filled.append(known)
    return filled


def _max_overlap_speaker(start_ms: int, end_ms: int, turns: Sequence[RttmTurn]) -> str:
    overlap: dict[str, int] = {}
    for turn in turns:
        ov = min(end_ms, turn.end_ms) - max(start_ms, turn.start_ms)
        if ov > 0:
            overlap[turn.speaker] = overlap.get(turn.speaker, 0) + ov
    return max(overlap.items(), key=lambda kv: kv[1])[0] if overlap else ""


#: Nemotron-3-Diarization supports at most eight speakers; the UI ceiling is
#: clamped to this so "0..16" can never promise labels the model cannot emit.
MAX_MODEL_SPEAKERS = 8


def _relabel_in_order(cues: Sequence[Cue]) -> list[Cue]:
    """Renumber speaker labels by first appearance (SPEAKER_01, 02, ...)."""
    mapping: dict[str, str] = {}
    out: list[Cue] = []
    for cue in cues:
        label = (cue.speaker or "SPEAKER_01").strip() or "SPEAKER_01"
        if label not in mapping:
            mapping[label] = f"SPEAKER_{len(mapping) + 1:02d}"
        out.append(replace(cue, speaker=mapping[label]))
    return out


def apply_speaker_ceiling(cues: Sequence[Cue], max_speakers: int) -> list[Cue]:
    """Enforce ``max_speakers`` as a ceiling by merging surplus speakers.

    ``max_speakers`` used to be ignored by the Nemotron path (the model decides
    its own count) and treated as an exact k by the acoustic path, so a user
    asking for "at most 3" could get 5 labels. Excess speakers are merged into
    the kept speaker whose turns overlap them most, then labels are renumbered
    in first-appearance order.
    """
    if not cues:
        return []
    cap = int(max_speakers)
    if cap <= 0:
        return list(cues)
    cap = min(cap, MAX_MODEL_SPEAKERS)
    if cap == 1:
        return [replace(c, speaker="SPEAKER_01") for c in cues]
    ordered: list[str] = []
    for cue in cues:
        label = (cue.speaker or "SPEAKER_01").strip() or "SPEAKER_01"
        if label not in ordered:
            ordered.append(label)
    if len(ordered) <= cap:
        return _relabel_in_order(cues)

    durations = {label: 0 for label in ordered}
    for cue in cues:
        label = (cue.speaker or "SPEAKER_01").strip() or "SPEAKER_01"
        durations[label] += max(1, cue.end_ms - cue.start_ms)
    first_seen = {label: i for i, label in enumerate(ordered)}
    kept = sorted(ordered, key=lambda label: (-durations[label], first_seen[label]))[:cap]
    kept_set = set(kept)
    dropped = [label for label in ordered if label not in kept_set]

    kept_intervals = {label: [] for label in kept}
    dropped_intervals: dict[str, list[tuple[int, int]]] = {label: [] for label in dropped}
    for cue in cues:
        label = (cue.speaker or "SPEAKER_01").strip() or "SPEAKER_01"
        if label in kept_set:
            kept_intervals[label].append((cue.start_ms, cue.end_ms))
        else:
            dropped_intervals[label].append((cue.start_ms, cue.end_ms))

    merge_into: dict[str, str] = {}
    for label in dropped:
        best = kept[0]
        best_overlap = -1
        for candidate in kept:
            overlap = sum(
                max(0, min(e1, e2) - max(s1, s2))
                for s1, e1 in dropped_intervals[label]
                for s2, e2 in kept_intervals[candidate]
            )
            if overlap > best_overlap:
                best_overlap = overlap
                best = candidate
        merge_into[label] = best

    merged = [
        replace(cue, speaker=merge_into.get((cue.speaker or "").strip(), cue.speaker or ""))
        for cue in cues
    ]
    return _relabel_in_order(merged)


def remap_speakers_to_previous(previous: Sequence[Cue], updated: Sequence[Cue]) -> list[Cue]:
    """Map fresh diarization labels onto the labels already in the project.

    Re-running diarization renumbered speakers from scratch, so a voice the
    user had cloned for ``SPEAKER_01`` silently moved to another person. New
    labels are matched to old ones by temporal overlap (greedy, highest first);
    unmatched new speakers receive a fresh number after the old maximum.
    """
    if not previous or not updated:
        return list(updated)
    old_order: list[str] = []
    for cue in previous:
        label = (cue.speaker or "").strip()
        if label and label not in old_order:
            old_order.append(label)
    if not old_order:
        return list(updated)
    new_order: list[str] = []
    for cue in updated:
        label = (cue.speaker or "SPEAKER_01").strip() or "SPEAKER_01"
        if label not in new_order:
            new_order.append(label)

    old_intervals: dict[str, list[tuple[int, int]]] = {label: [] for label in old_order}
    new_intervals: dict[str, list[tuple[int, int]]] = {label: [] for label in new_order}
    for cue in previous:
        label = (cue.speaker or "").strip()
        if label in old_intervals:
            old_intervals[label].append((cue.start_ms, cue.end_ms))
    for cue in updated:
        label = (cue.speaker or "SPEAKER_01").strip() or "SPEAKER_01"
        new_intervals[label].append((cue.start_ms, cue.end_ms))

    pairs: list[tuple[int, int, int]] = []
    for ni, new_label in enumerate(new_order):
        for oi, old_label in enumerate(old_order):
            overlap = sum(
                max(0, min(e1, e2) - max(s1, s2))
                for s1, e1 in new_intervals[new_label]
                for s2, e2 in old_intervals[old_label]
            )
            if overlap > 0:
                pairs.append((overlap, ni, oi))
    pairs.sort(reverse=True)
    assignment: dict[str, str] = {}
    used_old: set[str] = set()
    for _overlap, ni, oi in pairs:
        new_label = new_order[ni]
        old_label = old_order[oi]
        if new_label in assignment or old_label in used_old:
            continue
        assignment[new_label] = old_label
        used_old.add(old_label)

    used_numbers = set()
    for label in old_order:
        with contextlib.suppress(ValueError):
            used_numbers.add(int(label.rsplit("_", 1)[-1]))
    next_number = max(used_numbers, default=0) + 1
    for new_label in new_order:
        if new_label in assignment:
            continue
        while next_number in used_numbers:
            next_number += 1
        assignment[new_label] = f"SPEAKER_{next_number:02d}"
        used_numbers.add(next_number)

    return [
        replace(cue, speaker=assignment[(cue.speaker or "SPEAKER_01").strip() or "SPEAKER_01"])
        for cue in updated
    ]


def _cue_acoustic_embedding(samples: np.ndarray, sample_rate: int) -> np.ndarray:
    """Compute a 24-dim L2-normalized acoustic speaker embedding (F0 + spectral + sub-band)."""
    mono = np.asarray(samples, dtype=np.float32).ravel()
    if mono.size < 256:
        return np.zeros(24, dtype=np.float32)

    frame_len = max(128, int(sample_rate * 0.030))
    hop = max(64, frame_len // 2)
    max_samples = max(frame_len, int(6 * sample_rate))
    if mono.size > max_samples:
        mid = (mono.size - max_samples) // 2
        mono = mono[mid : mid + max_samples]
    if mono.size < frame_len:
        mono = np.pad(mono, (0, frame_len - mono.size))

    n_frames = max(1, 1 + (mono.size - frame_len) // hop)
    strides = (mono.strides[0] * hop, mono.strides[0])
    frames = np.lib.stride_tricks.as_strided(mono, shape=(n_frames, frame_len), strides=strides)
    rms = np.sqrt(np.mean(frames * frames, axis=1) + 1e-10)
    # Keep active voiced frames (top 70% energy)
    thresh = float(np.percentile(rms, 30)) if n_frames > 3 else 0.0
    voiced = frames[rms >= thresh]
    if voiced.shape[0] == 0:
        voiced = frames

    win = np.hanning(frame_len).astype(np.float32)
    windowed = voiced * win
    mag = np.abs(np.fft.rfft(windowed, axis=1)) + 1e-8
    freqs = np.fft.rfftfreq(frame_len, d=1.0 / sample_rate)

    # 1. Pitch F0 estimate via cepstrum / autocorrelation over 75..360 Hz
    min_lag = max(1, int(sample_rate / 360.0))
    max_lag = min(frame_len - 1, int(sample_rate / 75.0))
    f0_vals: list[float] = []
    if max_lag > min_lag:
        power = mag * mag
        acorr = np.fft.irfft(power, n=frame_len, axis=1)
        acorr_slice = acorr[:, min_lag:max_lag]
        best_lags = np.argmax(acorr_slice, axis=1) + min_lag
        f0_frames = sample_rate / np.maximum(best_lags, 1)
        f0_vals = [
            float(np.median(f0_frames)) / 300.0,
            float(np.std(f0_frames)) / 100.0,
        ]
    else:
        f0_vals = [0.5, 0.0]

    # 2. Spectral centroid & rolloff
    mag_sum = np.sum(mag, axis=1, keepdims=True)
    centroid = float(np.mean(np.sum(mag * freqs, axis=1, keepdims=True) / mag_sum)) / 4000.0
    cumsum = np.cumsum(mag, axis=1)
    rolloff_idx = np.argmax(cumsum >= 0.85 * mag_sum, axis=1)
    rolloff = float(np.mean(freqs[np.minimum(rolloff_idx, len(freqs) - 1)])) / 6000.0

    # 3. 20 log-spaced sub-band energies (80 Hz .. min(7600 Hz, sr/2))
    hi_freq = min(7600.0, sample_rate * 0.48)
    edges = np.geomspace(80.0, max(200.0, hi_freq), 21)
    band_means = np.zeros(20, dtype=np.float32)
    for b in range(20):
        mask = (freqs >= edges[b]) & (freqs < edges[b + 1])
        if np.any(mask):
            band_energy = np.log10(np.mean(mag[:, mask] ** 2, axis=1) + 1e-8)
            band_means[b] = float(np.mean(band_energy))
    # Cepstral mean normalization across bands so volume differences don't create false speakers
    band_means -= float(np.mean(band_means))
    band_std = float(np.std(band_means)) + 1e-5
    band_means /= band_std

    feat = np.concatenate(
        [
            np.array(
                [f0_vals[0] * 2.5, f0_vals[1], centroid * 1.5, rolloff * 1.5], dtype=np.float32
            ),
            band_means,
        ]
    )
    norm = float(np.linalg.norm(feat))
    return feat / norm if norm > 1e-6 else feat


def _average_linkage_partitions(
    dist: np.ndarray,
    max_k: int,
    *,
    cancel_check: Callable[[], bool] | None = None,
    merge_distances: list[float] | None = None,
) -> dict[int, list[int]]:
    """Average-linkage merge tree of ``dist``, as labels for every k <= ``max_k``.

    ``merge_distances`` (optional) receives the average distance of each
    merge in order; average linkage never decreases it.

    Merges the same pair as recomputing the full average-distance matrix each
    step and taking its row-major ``argmin``, which was O(n^3) overall (11 s
    for 2,000 cues). Here the summed distances are updated in place
    (Lance–Williams) and each row caches its nearest neighbour, so a merge
    rescans only row ``a`` and the rows whose nearest neighbour was ``a`` or
    ``b``. Slots stay in place (the merged cluster keeps the lower slot), so
    the order of active slots is the order of the compacted matrix and every
    tie breaks the same way.
    """
    n = dist.shape[0]
    cluster_sum = dist.astype(np.float64, copy=True)
    sizes = np.ones(n, dtype=np.float64)
    active = np.ones(n, dtype=bool)
    members: list[list[int]] = [[i] for i in range(n)]

    def _rows(rows: np.ndarray) -> np.ndarray:
        avg = cluster_sum[rows] / (sizes[rows, None] * sizes[None, :])
        avg[:, ~active] = np.inf
        avg[np.arange(rows.size), rows] = np.inf
        return avg

    def _rescan(rows: np.ndarray) -> None:
        for lo in range(0, rows.size, 1024):  # bounded temporaries for long videos
            chunk = rows[lo : lo + 1024]
            avg = _rows(chunk)
            nearest[chunk] = np.argmin(avg, axis=1)
            nearest_dist[chunk] = avg[np.arange(chunk.size), nearest[chunk]]

    nearest = np.zeros(n, dtype=np.int64)
    nearest_dist = np.full(n, np.inf, dtype=np.float64)
    _rescan(np.arange(n))

    def _snapshot() -> list[int]:
        lbls = [0] * n
        for cid, slot in enumerate(np.flatnonzero(active)):
            for idx in members[slot]:
                lbls[idx] = cid
        return lbls

    partitions: dict[int, list[int]] = {}
    remaining = n
    if remaining <= max_k:
        partitions[remaining] = _snapshot()

    while remaining > 1:
        if cancel_check is not None and cancel_check():
            raise DiarizationCancelled("Đã hủy phân tách người nói.")
        a = int(np.argmin(nearest_dist))
        b = int(nearest[a])
        if merge_distances is not None:
            merge_distances.append(float(nearest_dist[a]))
        if a > b:
            a, b = b, a
        cluster_sum[a, :] += cluster_sum[b, :]
        cluster_sum[:, a] = cluster_sum[a, :]
        cluster_sum[a, a] = 0.0
        sizes[a] += sizes[b]
        active[b] = False
        nearest_dist[b] = np.inf
        members[a].extend(members[b])
        members[b] = []
        remaining -= 1

        # Row a changed everywhere; a row whose nearest was a or b must rescan.
        stale = active & ((nearest == a) | (nearest == b))
        stale[a] = True
        _rescan(np.flatnonzero(stale))
        # Every other row only gained a new value at column a.
        others = active & ~stale
        to_a = cluster_sum[a] / (sizes[a] * sizes)
        closer = others & ((to_a < nearest_dist) | ((to_a == nearest_dist) & (a < nearest)))
        nearest[closer] = a
        nearest_dist[closer] = to_a[closer]

        if remaining <= max_k:
            partitions[remaining] = _snapshot()
    return partitions


def _cluster_embeddings(
    embeddings: np.ndarray,
    max_speakers: int = 0,
    *,
    cancel_check: Callable[[], bool] | None = None,
    stop_distance: float | None = None,
) -> list[int]:
    """Cosine agglomerative clustering returning 0-based speaker cluster indices.

    ``max_speakers`` is a ceiling, not an exact k: the best silhouette among
    2..ceiling wins. The previous exact-k behaviour turned "at most 4" into
    "exactly 4 labels", splitting a 2-person video into 4 speakers.

    With ``stop_distance`` (calibrated neural embeddings) merging stops at
    the first merge farther apart than it instead, still capped by the
    ceiling: silhouette over-splits a single speaker whose embeddings are
    never near-identical.
    """
    n = embeddings.shape[0]
    if n == 0:
        return []
    if n == 1:
        return [0] * n
    cap = min(int(max_speakers), MAX_MODEL_SPEAKERS) if int(max_speakers) > 0 else 0
    if cap == 1:
        return [0] * n

    sim = np.clip(embeddings @ embeddings.T, -1.0, 1.0)
    dist = 1.0 - sim
    np.fill_diagonal(dist, 0.0)

    # If max pairwise distance is tiny, everyone is the same speaker.
    if stop_distance is None and cap == 0 and float(np.max(dist)) < 0.08:
        return [0] * n

    max_k = min(n, cap) if cap else min(6, n)
    merges: list[float] = []
    partitions = _average_linkage_partitions(
        dist, max_k, cancel_check=cancel_check, merge_distances=merges
    )
    if stop_distance is not None:
        done = next((i for i, d in enumerate(merges) if d > stop_distance), len(merges))
        k = min(n - done, max_k)
        return _first_appearance_order(partitions[k] if k > 1 else [0] * n)

    best_k = 1
    best_score = 0.12  # minimum silhouette threshold to split into >=2 speakers
    best_labels = [0] * n
    for k in range(2, max_k + 1):
        if cancel_check is not None and cancel_check():
            raise DiarizationCancelled("Đã hủy phân tách người nói.")
        cand = partitions.get(k)
        if cand is None:
            continue
        cand_arr = np.asarray(cand, dtype=np.int32)
        mean_to_cluster = np.zeros((n, k), dtype=np.float64)
        counts = np.zeros(k, dtype=np.int32)
        for c_id in range(k):
            mask = cand_arr == c_id
            cnt = int(np.sum(mask))
            counts[c_id] = cnt
            if cnt > 0:
                mean_to_cluster[:, c_id] = np.sum(dist[:, mask], axis=1)
        idx_range = np.arange(n)
        own_c = cand_arr
        own_cnt = counts[own_c]
        a_vals = np.where(
            own_cnt > 1,
            mean_to_cluster[idx_range, own_c] / np.maximum(own_cnt - 1, 1),
            0.0,
        )
        other_means = mean_to_cluster / np.maximum(counts[None, :], 1)
        other_means[idx_range, own_c] = np.inf
        b_vals = np.min(other_means, axis=1)
        denom = np.maximum(np.maximum(a_vals, b_vals), 1e-6)
        mean_sil = float(np.mean((b_vals - a_vals) / denom))
        if mean_sil > best_score:
            best_score = mean_sil
            best_k = k
            best_labels = cand
    return _first_appearance_order(best_labels if best_k > 1 else [0] * n)


def _first_appearance_order(labels: Sequence[int]) -> list[int]:
    """Renumber clusters by first appearance (0 -> SPEAKER_01, 1 -> SPEAKER_02, ...)."""
    remap: dict[int, int] = {}
    return [remap.setdefault(lbl, len(remap)) for lbl in labels]


TURNS_CACHE_FILENAME = "diar_turns_cache.json"
#: Model input is 16 kHz mono; a 48 kHz WAV used to be fed to the CLI as-is,
#: which made every timestamp three times too long.
MODEL_SAMPLE_RATE = 16_000


def _resolve_cli_backend(config: DiarizationConfig, data_dir: Path | None) -> str:
    """Map the settings device to a ``--backend`` value the install supports."""
    if data_dir is not None:
        from vienetts_app.core.ai_models import installed_audiocpp_variant

        installed_variant = installed_audiocpp_variant(data_dir)
    else:
        installed_variant = ""
    device = (config.device or "auto").strip().lower()
    if device == "cpu":
        return "cpu"
    if device == "metal" and sys.platform == "darwin":
        return "metal"
    if device == "vulkan" and installed_variant == "vulkan":
        return "vulkan"
    if device in ("cuda", "auto") and installed_variant.startswith("cuda"):
        # A CUDA build (``cuda12.4``, ``cuda13.3``...) is installed for the
        # NVIDIA GPU; "best" could still settle on a CPU backend.
        return "cuda"
    # "auto" on other builds, or a GPU the installed runtime cannot serve: let
    # the binary pick the best compiled backend instead of failing on it.
    return "best"


def _runtime_is_cpu_only(data_dir: Path | None) -> bool:
    """True when the installed audio.cpp build has no GPU backend compiled in."""
    if data_dir is None:
        return False
    from vienetts_app.core.ai_models import installed_audiocpp_variant

    return installed_audiocpp_variant(data_dir) in ("", "cpu")


def _resolve_threads(config: DiarizationConfig) -> int:
    if int(config.threads) > 0:
        return max(1, min(64, int(config.threads)))
    # f32 on CPU scales to 16 threads in upstream measurements; the old
    # min(8, cpu//2) left ~35% on the table on an 18-core box.
    return max(1, min(16, os.cpu_count() or 4))


def _wav_duration_s(wav_path: Path) -> float:
    try:
        import soundfile as sf

        info = sf.info(str(wav_path))
        if info.samplerate:
            return float(info.frames) / float(info.samplerate)
    except Exception:  # noqa: BLE001 - duration is only a timeout heuristic
        return 0.0
    return 0.0


def _turns_cache_key(
    wav_path: Path, config: DiarizationConfig, *, key_path: Path | None = None
) -> str:
    """Turns-cache key for ``wav_path``; ``key_path`` names it as a legacy alias."""
    try:
        stat = wav_path.stat()
    except OSError:
        return ""
    named = (key_path or wav_path).resolve()
    return f"{named}|{stat.st_size}|{stat.st_mtime_ns}|{config.model}"


def _load_cached_turns(
    work_dir: Path | None, key: str, *, aliases: Sequence[str] = ()
) -> list[RttmTurn] | None:
    """Cached turns stored under ``key`` (or a legacy ``aliases`` key)."""
    if work_dir is None or not key:
        return None
    path = work_dir / TURNS_CACHE_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("key") not in (key, *aliases):
        return None
    turns = data.get("turns")
    if not isinstance(turns, list) or not turns:
        return None
    try:
        return parse_audiocpp_turns(json.dumps(turns))
    except DiarizationError:
        return None


def _store_cached_turns(work_dir: Path | None, key: str, turns: list[RttmTurn]) -> None:
    if work_dir is None or not key or not turns:
        return
    raw = [
        {
            "start_sample": int(round(t.start_ms * MODEL_SAMPLE_RATE / 1000.0)),
            "end_sample": int(round(t.end_ms * MODEL_SAMPLE_RATE / 1000.0)),
            "speaker_id": t.speaker,
        }
        for t in turns
    ]
    with contextlib.suppress(OSError):
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / TURNS_CACHE_FILENAME).write_text(
            json.dumps({"key": key, "turns": raw}), encoding="utf-8"
        )


def _ensure_16k_wav(
    src: Path,
    work_dir: Path | None,
    data_dir: Path | None,
    temp_dir: tempfile.TemporaryDirectory[str] | None,
    *,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    """Return the shared 16 kHz mono WAV for ``src`` (``audio_prep.ensure_source_16k``).

    With a ``work_dir`` this is the media cache's ``source_16k.wav`` that ASR
    already produced, so a WAV transcribed first is never converted again.
    """
    if work_dir is not None:
        cache_dir = work_dir
    else:
        if temp_dir is None:
            temp_dir = tempfile.TemporaryDirectory(prefix="vienetts_diar_")
        cache_dir = Path(temp_dir.name)
    try:
        wav = ensure_source_16k(src, cache_dir, data_dir=data_dir, cancel_check=cancel_check)
    except AudioPrepCancelled as exc:
        raise DiarizationCancelled("Đã hủy phân tách người nói.") from exc
    except AudioPrepError as exc:
        raise DiarizationError(str(exc)) from exc
    return wav, temp_dir


#: The CLI run owns this slice of the Nemotron backend's progress; the turns
#: are parsed and cached in the rest.
_CLI_PROGRESS_START = 0.1
_CLI_PROGRESS_END = 0.95
#: Elapsed-time estimate when the CLI prints nothing parseable: model load plus
#: a CPU f32 real-time factor. Deliberately rough; it only paces the bar.
_NEMOTRON_ESTIMATE_SETUP_S = 5.0
_NEMOTRON_ESTIMATE_RTF = 0.1
_NEMOTRON_ESTIMATE_UNKNOWN_DURATION_S = 600.0

_PERCENT_RE = re.compile(r"(?<![\d.])(\d{1,3}(?:\.\d+)?)\s*%")
_STEP_RE = re.compile(
    r"\[\s*(\d+)\s*/\s*(\d+)\s*\]"
    r"|\b(?:chunk|window|segment|block|step|batch|frame)s?\b\D{0,3}?(\d+)\s*(?:/|of)\s*(\d+)",
    re.IGNORECASE,
)
_FRACTION_RE = re.compile(
    r"\bprogress\b\s*[=:]?\s*(0(?:\.\d+)?|1(?:\.0*)?)(?![\d.%])", re.IGNORECASE
)


def _parse_cli_progress(line: str) -> float | None:
    """Completed fraction reported by one CLI output line, or ``None``.

    Accepts ``42%``, ``[3/20]``, ``chunk 3 of 20`` and ``progress=0.42``.
    """
    match = _PERCENT_RE.search(line)
    if match is not None:
        percent = float(match.group(1))
        return percent / 100.0 if percent <= 100.0 else None
    match = _STEP_RE.search(line)
    if match is not None:
        done, total = (int(g) for g in match.groups() if g is not None)
        return done / total if 0 <= done <= total and total > 0 else None
    match = _FRACTION_RE.search(line)
    return float(match.group(1)) if match is not None else None


class _CliProgress:
    """Relay a native CLI's progress to ``progress_cb`` in ``[start, end]``.

    ``on_line`` (called from the pipe reader threads) maps any line that
    reports progress; once one has, those lines alone drive the bar. Until
    then ``tick`` (polled on the calling thread) estimates from the time on
    ``clock`` against ``expected_s``. Callbacks are serialized, monotonic and
    at least 1 % apart, so two pipes and the poll can never move the bar back;
    after ``close`` (the caller is about to report completion) nothing is.
    """

    def __init__(
        self,
        progress_cb: Callable[[float], None],
        *,
        start: float,
        end: float,
        expected_s: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._progress_cb = progress_cb
        self._start = float(start)
        self._end = float(end)
        self._expected_s = max(1e-3, float(expected_s))
        self._clock = clock
        self._started_at = clock()
        self._lock = threading.Lock()
        self._last = self._start
        self._parsed = False
        self._closed = False

    def on_line(self, line: str) -> None:
        fraction = _parse_cli_progress(line)
        if fraction is None:
            return
        with self._lock:
            self._parsed = True
            self._report(fraction)

    def tick(self) -> None:
        with self._lock:
            if not self._parsed:
                self._report((self._clock() - self._started_at) / self._expected_s)

    def close(self) -> None:
        # A pipe reader can outlive run_cli's join (a grandchild holding the
        # pipe); its late lines must not land after the caller's 1.0.
        with self._lock:
            self._closed = True

    def _report(self, fraction: float) -> None:
        if self._closed:
            return
        value = self._start + (self._end - self._start) * min(1.0, max(0.0, fraction))
        if value <= self._last or (value - self._last < 0.01 and value < self._end):
            return
        self._last = value
        self._progress_cb(value)


def extract_speaker_turns(
    wav_path: Path,
    config: DiarizationConfig | None = None,
    *,
    work_dir: Path | None = None,
    data_dir: Path | None = None,
    progress_cb: Callable[[float], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    notes: list[str] | None = None,
) -> list[RttmTurn]:
    """Speaker turns from the Nemotron-3 audio.cpp CLI for a 16 kHz mono WAV.

    ``wav_path`` is the shared ``source_16k.wav`` (``audio_prep.ensure_source_16k``).
    The turns are cached in ``work_dir``, so ``diarize_cues`` reuses a run made here.
    ``config.threads`` (when > 0) goes to the CLI as is, which lets the transcription
    pipeline give each of ASR and Nemotron half of the budget.

    Returns ``[]`` with a note when no speech was found. Raises
    ``DiarizationError`` when the runtime or model is missing or the run fails (the
    ``auto`` backend falls back on it) and ``DiarizationCancelled`` on cancel.
    """
    config = config or DiarizationConfig()
    cli = resolve_audiocpp_cli(data_dir)
    if cli is None:
        raise DiarizationError(
            "Chưa cài đặt runtime audio.cpp (audiocpp_cli). "
            "Hãy nhấn 'Tải runtime audio.cpp' trong Cài đặt → Mô hình."
        )
    model_path = resolve_diarization_model_path(
        config.model or "nemotron-3-diarization-q8_0", data_dir
    )
    if model_path is None:
        raise DiarizationError(
            f"Chưa tải mô hình Nemotron-3 Diarization GGUF ({config.model}). "
            "Hãy nhấn 'Tải mô hình Diarization' trong Cài đặt → Mô hình."
        )

    cache_key = _turns_cache_key(wav_path, config)
    # A legacy source_wav_16k.wav adopted as source_16k.wav keeps its size and
    # mtime, so turns cached against the old name still apply.
    aliases = [
        _turns_cache_key(wav_path, config, key_path=p) for p in cache_key_paths(wav_path)[1:]
    ]
    cached = _load_cached_turns(work_dir, cache_key, aliases=aliases)
    if cached is not None:
        if progress_cb is not None:
            progress_cb(1.0)
        return cached

    if progress_cb is not None:
        progress_cb(_CLI_PROGRESS_START)
    backend_dev = _resolve_cli_backend(config, data_dir)
    threads = _resolve_threads(config)
    turns_temp = tempfile.TemporaryDirectory(prefix="vienetts_turns_")
    turns_out = Path(turns_temp.name) / "turns.json"
    if work_dir is not None:
        work_dir.mkdir(parents=True, exist_ok=True)
        turns_out = work_dir / f".nemotron_turns_{uuid.uuid4().hex}.json"

    cmd = cli_command(
        cli,
        "--task",
        "diar",
        "--family",
        "nemotron_3_diar",
        "--model",
        str(model_path),
        "--backend",
        backend_dev,
        "--threads",
        str(threads),
        "--audio",
        str(wav_path),
        "--turns-out",
        str(turns_out),
    )
    use_f32 = backend_dev == "cpu" or (backend_dev == "best" and _runtime_is_cpu_only(data_dir))
    if use_f32:
        # Verified upstream: forcing f32 on CPU is ~2x faster than the native
        # mixed q8/BF16 path, and matches the NeMo parity reference. "best"
        # resolves to the CPU backend on a CPU-only install, so it needs the
        # same option or the default device (auto) runs at half speed.
        cmd += ["--session-option", "nemotron_3_diar.weight_type=f32"]

    duration_s = _wav_duration_s(wav_path)
    timeout_s = (
        float(config.timeout_s)
        if float(config.timeout_s) > 0
        else max(180.0, duration_s * 6.0 + 60.0)
    )
    meter: _CliProgress | None = None
    if progress_cb is not None:
        expected_s = _NEMOTRON_ESTIMATE_SETUP_S + _NEMOTRON_ESTIMATE_RTF * (
            duration_s if duration_s > 0 else _NEMOTRON_ESTIMATE_UNKNOWN_DURATION_S
        )
        meter = _CliProgress(
            progress_cb, start=_CLI_PROGRESS_START, end=_CLI_PROGRESS_END, expected_s=expected_s
        )

    def _poll() -> bool:
        # run_cli polls this every 50 ms on this thread: the estimate's clock.
        if meter is not None:
            meter.tick()
        return cancel_check is not None and cancel_check()

    try:
        try:
            res = run_cli(
                cmd,
                timeout=timeout_s,
                cancel_check=_poll,
                on_stdout_line=None if meter is None else meter.on_line,
                on_stderr_line=None if meter is None else meter.on_line,
            )
        except CliCancelled as exc:
            raise DiarizationCancelled("Đã hủy phân tách người nói.") from exc
        except CliTimeout as exc:
            raise DiarizationError(
                f"Phân tách người nói quá thời gian chờ ({timeout_s:.0f}s) — "
                "hãy thử video ngắn hơn hoặc chọn runtime CPU."
            ) from exc
        except OSError as exc:
            # A runtime that cannot start (wrong architecture, no exec bit) is
            # a diarization failure the auto chain can fall back from.
            raise DiarizationError(f"Không thể chạy audiocpp_cli: {exc}") from exc
        finally:
            if meter is not None:
                meter.close()
        if res.returncode != 0:
            detail = (res.stderr or res.stdout).strip().splitlines()[-1:] or ["audiocpp_cli failed"]
            raise DiarizationError(f"Lỗi khi chạy Nemotron-3 Diarization GGUF: {detail[0]}")
        # Verified upstream: an empty result writes no turns file at all.
        turns = (
            parse_audiocpp_turns(
                turns_out.read_text(encoding="utf-8", errors="replace"),
                sample_rate=MODEL_SAMPLE_RATE,
            )
            if turns_out.is_file()
            else []
        )
        if not turns:
            if notes is not None:
                notes.append(
                    "Không phát hiện giọng nói nào trong video, nên chưa có nhãn người nói."
                )
        else:
            _store_cached_turns(work_dir, cache_key, turns)
        if progress_cb is not None:
            progress_cb(1.0)
        return turns
    finally:
        turns_temp.cleanup()
        with contextlib.suppress(OSError):
            turns_out.unlink(missing_ok=True)


def _run_nemotron_gguf_diarization(
    wav_path: Path,
    cues: Sequence[Cue],
    config: DiarizationConfig,
    *,
    data_dir: Path | None = None,
    work_dir: Path | None = None,
    progress_cb: Callable[[float], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    notes: list[str] | None = None,
) -> list[Cue]:
    turns = extract_speaker_turns(
        wav_path,
        config,
        work_dir=work_dir,
        data_dir=data_dir,
        progress_cb=progress_cb,
        cancel_check=cancel_check,
        notes=notes,
    )
    return assign_speakers_from_turns(cues, turns, wav_path=wav_path, data_dir=data_dir)


#: Average-linkage stop distance (1 - cosine) for the installed speaker
#: embedding (CAM++): clusters farther apart than this are different people.
#: Chosen on the DER benchmark, scripts/benchmarks/diarization_der.py.
NEURAL_STOP_DISTANCE = 0.45
#: Cues with less speech than this are attached to the speakers found on
#: the longer ones instead of being clustered (see ``_neural_cue_labels``).
_NEURAL_ANCHOR_MS = 1500


def _neural_cue_vectors(
    wav_path: Path,
    cues: Sequence[Cue],
    embedder: SpeakerEmbedder,
    vad: SileroVad | None,
    *,
    progress_cb: Callable[[float], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[list[np.ndarray | None], list[int]]:
    """Per cue: its speaker embedding (``None`` without audio) and its speech ms.

    Each cue is embedded from its Silero speech only (the whole cue without
    Silero or when it finds none).
    """
    import soundfile as sf

    vectors: list[np.ndarray | None] = []
    speech_ms: list[int] = []
    with sf.SoundFile(str(wav_path)) as wav_file:
        sr = int(wav_file.samplerate)
        for i, cue in enumerate(cues):
            if cancel_check is not None and cancel_check():
                raise DiarizationCancelled("Đã hủy phân tách người nói.")
            mono = _read_mono_ms(wav_file, cue.start_ms, cue.end_ms)
            vec = None
            if mono is not None:
                spans = vad.speech_spans(mono, sr) if vad is not None else []
                if spans:
                    mono = np.concatenate([mono[s:e] for s, e in spans])
                vec = embedder.embed(mono, sr)
            vectors.append(vec)
            speech_ms.append(0 if vec is None else int(mono.size * 1000 // sr))
            if progress_cb is not None:
                progress_cb(min(0.7, (i + 1) / max(1, len(cues)) * 0.7))
    return vectors, speech_ms


def _neural_cue_labels(
    cues: Sequence[Cue],
    vectors: Sequence[np.ndarray | None],
    speech_ms: Sequence[int],
    max_speakers: int,
    *,
    cancel_check: Callable[[], bool] | None = None,
    stop_distance: float | None = NEURAL_STOP_DISTANCE,
) -> list[int]:
    """Cluster labels of the cue embeddings.

    Only cues with at least ``_NEURAL_ANCHOR_MS`` of speech are clustered: a
    short "Vâng." embeds far from its speaker and became a speaker of its
    own. Shorter cues join the nearest cluster centroid, and a cue without an
    embedding takes the label of the nearest clustered cue in time.
    """
    known = [i for i, vec in enumerate(vectors) if vec is not None]
    if not known:
        return [0] * len(cues)
    anchors = [i for i in known if speech_ms[i] >= _NEURAL_ANCHOR_MS]
    if len(anchors) < 2:
        anchors = known
    anchor_labels = _cluster_embeddings(
        np.vstack([vectors[i] for i in anchors]),
        max_speakers=max_speakers,
        cancel_check=cancel_check,
        stop_distance=stop_distance,
    )
    by_index = dict(zip(anchors, anchor_labels, strict=True))
    n_clusters = max(anchor_labels) + 1
    centroids = np.zeros((n_clusters, np.asarray(vectors[anchors[0]]).size))
    for i, lbl in by_index.items():
        centroids[lbl] += vectors[i]
    centroids /= np.maximum(np.linalg.norm(centroids, axis=1, keepdims=True), 1e-12)
    for i in known:
        if i not in by_index:
            by_index[i] = int(np.argmax(centroids @ vectors[i]))
    mids = [(cues[i].start_ms + cues[i].end_ms) / 2.0 for i in anchors]
    labels = []
    for i, cue in enumerate(cues):
        if i not in by_index:
            mid = (cue.start_ms + cue.end_ms) / 2.0
            j = min(range(len(anchors)), key=lambda k: abs(mids[k] - mid))
            by_index[i] = anchor_labels[j]
        labels.append(by_index[i])
    return _first_appearance_order(labels)


def _run_acoustic_diarization(
    wav_path: Path,
    cues: Sequence[Cue],
    config: DiarizationConfig,
    *,
    data_dir: Path | None = None,
    progress_cb: Callable[[float], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> list[Cue]:
    """Cluster cues by voice: the installed speaker model, else 24-dim features."""
    import soundfile as sf

    cluster_ids: list[int] | None = None
    embedder = installed_speaker_embedder(data_dir)
    if embedder is not None:
        try:
            vectors, speech_ms = _neural_cue_vectors(
                wav_path,
                cues,
                embedder,
                installed_silero_vad(data_dir),
                progress_cb=progress_cb,
                cancel_check=cancel_check,
            )
            cluster_ids = _neural_cue_labels(
                cues,
                vectors,
                speech_ms,
                max(0, int(config.max_speakers)),
                cancel_check=cancel_check,
            )
        except DiarizationCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - ORT/fbank raise their own types
            logger.warning("Speaker embedding failed, using the 24-dim fallback: %s", exc)

    if cluster_ids is None:
        info = sf.info(str(wav_path))
        sr = int(info.samplerate)
        total_frames = int(info.frames)
        embeddings: list[np.ndarray] = []
        for i, cue in enumerate(cues):
            if cancel_check is not None and cancel_check():
                raise DiarizationCancelled("Đã hủy phân tách người nói.")
            start_f = max(0, min(total_frames, int(round(cue.start_ms * sr / 1000.0))))
            stop_f = max(start_f, min(total_frames, int(round(cue.end_ms * sr / 1000.0))))
            samples, _ = read_wav_range(wav_path, start_f, stop_f)
            embeddings.append(_cue_acoustic_embedding(samples, sr))
            if progress_cb is not None:
                progress_cb(min(0.7, (i + 1) / max(1, len(cues)) * 0.7))
        emb_matrix = np.vstack(embeddings) if embeddings else np.zeros((0, 24), dtype=np.float32)
        cluster_ids = _cluster_embeddings(
            emb_matrix,
            max_speakers=max(0, int(config.max_speakers)),
            cancel_check=cancel_check,
        )
    if progress_cb is not None:
        progress_cb(1.0)
    tagged = [
        replace(cue, speaker=f"SPEAKER_{cid + 1:02d}")
        for cue, cid in zip(cues, cluster_ids, strict=True)
    ]
    return refine_cues_speech_bounds(tagged, wav_path, data_dir=data_dir)


def diarize_cues(
    media_path: str | Path,
    cues: Sequence[Cue],
    config: DiarizationConfig | None = None,
    *,
    work_dir: Path | None = None,
    data_dir: Path | None = None,
    progress_cb: Callable[[float], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    notes: list[str] | None = None,
) -> list[Cue]:
    """Label each ``Cue`` with ``SPEAKER_01``..``SPEAKER_NN`` from ``media_path``.

    ``notes`` (optional) receives user-facing warnings — the reason ``auto``
    fell back to the acoustic heuristic, or "no speech detected". The UI shows
    them; swallowing them made heuristic labels look like model output.
    """
    if not cues:
        return []
    if cancel_check is not None and cancel_check():
        raise DiarizationCancelled("Đã hủy phân tách người nói.")
    cfg = config or DiarizationConfig()
    src = Path(media_path)
    if not src.is_file():
        raise DiarizationError(
            f"Không tìm thấy tệp âm thanh/video để phân tách người nói: {src.name}"
        )

    temp_dir_obj: tempfile.TemporaryDirectory[str] | None = None
    try:
        wav_path, temp_dir_obj = _ensure_16k_wav(
            src, work_dir, data_dir, None, cancel_check=cancel_check
        )

        if cancel_check is not None and cancel_check():
            raise DiarizationCancelled("Đã hủy phân tách người nói.")

        backend = (cfg.backend or "auto").strip()
        if backend == "nemo":  # legacy setting value; settings.py migrates it
            backend = "nemotron_gguf"

        # Highest progress the model reported, so a fallback resumes from it
        # instead of moving the bar back.
        primary_progress = [0.1]

        def _primary_progress(value: float) -> None:
            primary_progress[0] = max(primary_progress[0], value)
            if progress_cb is not None:
                progress_cb(value)

        def _primary() -> list[Cue]:
            return _run_nemotron_gguf_diarization(
                wav_path,
                cues,
                cfg,
                data_dir=data_dir,
                work_dir=work_dir,
                progress_cb=None if progress_cb is None else _primary_progress,
                cancel_check=cancel_check,
                notes=notes,
            )

        def _acoustic(scale_from: float = 0.0) -> list[Cue]:
            if progress_cb is None:
                return _run_acoustic_diarization(
                    wav_path, cues, cfg, data_dir=data_dir, cancel_check=cancel_check
                )
            return _run_acoustic_diarization(
                wav_path,
                cues,
                cfg,
                data_dir=data_dir,
                progress_cb=lambda p: progress_cb(scale_from + (1.0 - scale_from) * p),
                cancel_check=cancel_check,
            )

        if backend == "nemotron_gguf":
            return apply_speaker_ceiling(_primary(), cfg.max_speakers)
        if backend == "auto":
            try:
                return apply_speaker_ceiling(_primary(), cfg.max_speakers)
            except DiarizationCancelled:
                raise
            except DiarizationError as exc:
                logger.warning("Nemotron-3 diarization unavailable, using acoustic: %s", exc)
                if notes is not None:
                    notes.append(f"Không dùng được mô hình Nemotron-3: {exc}")
            try:
                result = _acoustic(scale_from=primary_progress[0])
            except DiarizationCancelled:
                raise
            if notes is not None:
                notes.append(
                    "Nhãn người nói được tạo bằng phương pháp dự phòng (âm học), "
                    "độ chính xác thấp hơn mô hình Nemotron-3."
                )
            return apply_speaker_ceiling(result, cfg.max_speakers)
        return apply_speaker_ceiling(_acoustic(), cfg.max_speakers)
    finally:
        if temp_dir_obj is not None:
            with contextlib.suppress(OSError):
                temp_dir_obj.cleanup()


#: Reference clips (FR-4.4): runs of a speaker's cues may bridge gaps up to
#: this long, and a clip inside [_REF_MIN_MS, _REF_MAX_MS] needs no trimming.
_REF_RUN_GAP_MS = 400
_REF_MIN_MS = 3_000
_REF_MAX_MS = 12_000
#: Only this many candidates (best on duration and overlap) are read and
#: scored on audio, so a speaker with hundreds of cues stays cheap.
_REF_AUDIO_SHORTLIST = 24
#: A sample at or past this magnitude is clipped.
_REF_CLIP_LEVEL = 0.99
#: RMS level band (dBFS) a clean, usable recording sits in.
_REF_LEVEL_BAND_DB = (-35.0, -6.0)
_REF_VAD_THRESHOLD_DB = -42.0


def select_speaker_reference_clip(
    cues: Sequence[Cue],
    speaker: str,
    *,
    wav_path: str | Path | None = None,
    turns: Sequence[RttmTurn] | None = None,
    data_dir: Path | None = None,
) -> tuple[int, int, str] | None:
    """Best ``(start_ms, end_ms, transcript)`` voice-cloning window for ``speaker``.

    Candidates are single cues and runs of the speaker's consecutive cues.

    Without ``wav_path`` the choice reads cues only (ideal 3.5–7.5 s, few
    gaps): no I/O, cheap enough for the GUI thread to key its clip cache on.

    With ``wav_path`` (the source WAV, any rate) each candidate is scored on
    its audio (FR-4.4): duration within [3, 12] s, the speech ratio (Silero
    when installed under ``data_dir``, else the energy VAD), the RMS level,
    the clipped-sample ratio and how much of it overlaps another speaker's
    turns; duration breaks ties. ``turns`` default to the other speakers'
    cues. An unreadable WAV falls back to the cue-only choice.
    """
    target = (speaker or "").strip()
    spk_cues = [c for c in cues if (c.speaker or "").strip() == target]
    if not spk_cues:
        return None
    wav_file = _open_wav_for_vad(wav_path)
    if wav_file is None:
        return _reference_clip_from_cues(spk_cues)
    try:
        if turns is None:
            others = [(c.start_ms, c.end_ms) for c in cues if (c.speaker or "").strip() != target]
        else:
            others = [(t.start_ms, t.end_ms) for t in turns if t.speaker.strip() != target]
        return _reference_clip_from_audio(
            wav_file, spk_cues, others, installed_silero_vad(data_dir)
        )
    finally:
        with contextlib.suppress(Exception):
            wav_file.close()


def _reference_windows(
    spk_cues: Sequence[Cue], max_span_ms: int
) -> Iterator[tuple[int, int, str, int]]:
    """``(start_ms, end_ms, transcript, total_gap_ms)`` of every cue and cue run.

    A run grows while the next cue starts at most ``_REF_RUN_GAP_MS`` after
    the previous one ends and the run stays within ``max_span_ms``.
    """
    for i, first in enumerate(spk_cues):
        end_ms = first.end_ms
        texts = [first.text]
        total_gap_ms = 0
        for j in range(i, len(spk_cues)):
            if j > i:
                curr = spk_cues[j]
                gap = curr.start_ms - end_ms
                if gap < 0 or gap > _REF_RUN_GAP_MS or (curr.end_ms - first.start_ms) > max_span_ms:
                    break
                total_gap_ms += gap
                end_ms = curr.end_ms
                texts.append(curr.text)
            yield first.start_ms, end_ms, " ".join(texts).strip(), total_gap_ms


def _reference_clip_from_cues(spk_cues: Sequence[Cue]) -> tuple[int, int, str] | None:
    """Cue-only choice: ideal 3.5–7.5 s with minimal internal silence."""
    best_window: tuple[int, int, str] | None = None
    best_score = -1e9
    for start_ms, end_ms, joined, total_gap_ms in _reference_windows(spk_cues, 8500):
        dur_ms = max(1, end_ms - start_ms)
        if 3500 <= dur_ms <= 7500:
            score = (
                100.0
                - abs(dur_ms - 5000) / 100.0
                - total_gap_ms / 50.0
                + min(len(joined), 120) * 0.1
            )
        elif dur_ms < 3500:
            score = (dur_ms / 3500.0) * 60.0 - total_gap_ms / 50.0
        else:
            score = max(10.0, 80.0 - (dur_ms - 7500) / 100.0 - total_gap_ms / 50.0)
        if score > best_score:
            best_score = score
            best_window = (start_ms, end_ms, joined)
    return best_window


def _reference_clip_from_audio(
    wav_file: Any,
    spk_cues: Sequence[Cue],
    others: Sequence[tuple[int, int]],
    vad: SileroVad | None = None,
) -> tuple[int, int, str] | None:
    """Audio-scored choice (FR-4.4); see :func:`select_speaker_reference_clip`.

    Every score is a sum of penalty points, where 10 points is roughly a 2 s
    clip, 5 % clipped samples, a clip that is all silence, or half the clip
    under another speaker. Scores are compared at 0.01-point resolution, so
    near-equal clips go to the longer one.
    """
    covered = _merge_spans(others)
    covered_ends = [end for _, end in covered]
    windows = list(_reference_windows(spk_cues, _REF_MAX_MS))
    cue_penalties = []
    for start_ms, end_ms, _text, _gap in windows:
        dur_ms = max(1, end_ms - start_ms)
        overlap = _covered_ms(covered, covered_ends, start_ms, end_ms) / dur_ms
        too_short_or_long = max(0, _REF_MIN_MS - dur_ms, dur_ms - _REF_MAX_MS) / 1000.0
        cue_penalties.append(10.0 * too_short_or_long + 20.0 * overlap)
    shortlist = sorted(
        range(len(windows)),
        key=lambda k: (cue_penalties[k], windows[k][0] - windows[k][1], k),
    )[:_REF_AUDIO_SHORTLIST]

    sr = int(wav_file.samplerate)
    best: tuple[int, int, str] | None = None
    best_key: tuple[float, int] | None = None
    for k in sorted(shortlist):  # source order, so a full tie keeps the earliest
        start_ms, end_ms, text, _gap = windows[k]
        mono = _read_mono_ms(wav_file, start_ms, end_ms)
        score = -(cue_penalties[k] + _reference_audio_penalty(mono, sr, vad))
        key = (round(score, 2), end_ms - start_ms)
        if best_key is None or key > best_key:
            best_key = key
            best = (start_ms, end_ms, text)
    return best


def _reference_audio_penalty(
    mono: np.ndarray | None, sr: int, vad: SileroVad | None = None
) -> float:
    """Penalty points for a clip's audio: silence, level outside the band, clipping."""
    if mono is None:
        return 30.0  # past the end of the audio: never preferred over real audio
    speech_ratio = None
    if vad is not None:
        try:
            spans = vad.speech_spans(mono, sr)
        except Exception as exc:  # noqa: BLE001 - ORT raises its own types
            logger.warning("Silero VAD failed, using the energy VAD: %s", exc)
        else:
            speech_ratio = sum(end - start for start, end in spans) / mono.size
    if speech_ratio is None:
        rms = _window_rms(mono, max(1, int(round(sr * 0.010))))
        active = _energy_vad(rms, float(10.0 ** (_REF_VAD_THRESHOLD_DB / 20.0)))
        speech_ratio = 0.0 if active is None else active.size / rms.size
    level_db = 20.0 * np.log10(max(1e-5, float(np.sqrt(np.mean(mono * mono, dtype=np.float64)))))
    low, high = _REF_LEVEL_BAND_DB
    clipped_ratio = float(np.mean(np.abs(mono) >= _REF_CLIP_LEVEL))
    return (
        10.0 * (1.0 - speech_ratio)
        + 0.2 * max(0.0, low - level_db, level_db - high)
        + 200.0 * clipped_ratio
    )


def _merge_spans(spans: Sequence[tuple[int, int]]) -> list[tuple[int, int]]:
    """Sorted, disjoint union of ``spans``; empty spans are dropped."""
    merged: list[list[int]] = []
    for start, end in sorted(spans):
        if end <= start:
            continue
        if merged and start <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], end)
        else:
            merged.append([start, end])
    return [(start, end) for start, end in merged]


def _covered_ms(
    merged: Sequence[tuple[int, int]], ends: Sequence[int], start_ms: int, end_ms: int
) -> int:
    """Milliseconds of ``[start_ms, end_ms)`` covered by the disjoint ``merged`` spans."""
    total = 0
    i = bisect.bisect_right(ends, start_ms)
    while i < len(merged) and merged[i][0] < end_ms:
        total += min(end_ms, merged[i][1]) - max(start_ms, merged[i][0])
        i += 1
    return total


#: Voice-cloning reference clips are written as 48 kHz mono.
REFERENCE_SAMPLE_RATE = 48_000


def extract_speaker_reference_wav(
    media_path: str | Path,
    cues: Sequence[Cue],
    speaker: str,
    output_wav: str | Path,
    *,
    data_dir: Path | None = None,
    work_dir: Path | None = None,
    turns: Sequence[RttmTurn] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[Path, str]:
    """Extract the cleanest 48 kHz mono WAV reference clip for ``speaker`` + its transcript.

    Worker thread only: candidates are scored on audio (FR-4.4) — the source
    itself when it is a WAV, else the shared ``source_16k.wav`` in
    ``work_dir`` (the media cache ASR fills; converted here if missing). A
    video without ``work_dir`` gets the cue-only choice.
    """
    src = Path(media_path)
    scoring_wav: Path | None = None
    if src.is_file():
        if src.suffix.lower() == ".wav":
            scoring_wav = src
        elif work_dir is not None:
            try:
                scoring_wav, _ = _ensure_16k_wav(
                    src, work_dir, data_dir, None, cancel_check=cancel_check
                )
            except DiarizationCancelled:
                raise
            except DiarizationError as exc:
                logger.warning("Reference clip scored on cues only: %s", exc)
    sel = select_speaker_reference_clip(
        cues, speaker, wav_path=scoring_wav, turns=turns, data_dir=data_dir
    )
    if sel is None:
        raise DiarizationError(f"Không tìm thấy đoạn thoại nào của người nói '{speaker}'.")
    start_ms, end_ms, transcript = sel
    if not src.is_file():
        raise DiarizationError(f"Không tìm thấy tệp nguồn: {src.name}")

    target = Path(output_wav)
    target.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() == ".wav":
        import soundfile as sf
        import soxr

        # 48 kHz mono like the video branch: a stereo source used to be written
        # frame-interleaved (twice as long) and any other rate kept as-is.
        with sf.SoundFile(str(src)) as wav_file:
            sr = int(wav_file.samplerate)
            mono = _read_mono_ms(wav_file, start_ms, max(end_ms, start_ms + 1))
        if mono is None:
            raise DiarizationError("Đoạn mẫu giọng trích xuất bị rỗng.")
        if sr != REFERENCE_SAMPLE_RATE:
            mono = soxr.resample(mono, sr, REFERENCE_SAMPLE_RATE, quality="VHQ")
        write_wav_file(
            np.asarray(mono, dtype=np.float32), target, sample_rate=REFERENCE_SAMPLE_RATE
        )
        return target, transcript

    try:
        extract_video_audio(
            src,
            target,
            sample_rate=REFERENCE_SAMPLE_RATE,
            mono=True,
            start_ms=start_ms,
            end_ms=end_ms,
            data_dir=data_dir,
        )
    except VideoDubError as exc:
        raise DiarizationError(str(exc)) from exc
    return target, transcript
