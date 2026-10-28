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

import contextlib
import json
import logging
import os
import sys
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

from vienetts_app.core.ai_models import (
    cli_command,
    resolve_audiocpp_cli,
    resolve_diarization_model_path,
)
from vienetts_app.core.audio import read_wav_range, write_wav_file
from vienetts_app.core.processes import CliCancelled, CliTimeout, run_cli
from vienetts_app.core.subtitles import Cue
from vienetts_app.core.video_dub import VideoDubCancelled, VideoDubError, extract_video_audio

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
) -> list[Cue]:
    """Refine each ``Cue``'s ``[speech_start_ms, speech_end_ms]`` inside ``[start_ms, end_ms]``.

    Uses a 10 ms RMS energy VAD over ``wav_path`` when readable and intersects
    with ``turns`` when provided. Keeps ``Cue.start_ms`` and ``Cue.end_ms``
    untouched, and leaves existing bounds unchanged when the WAV slice is
    silent or unreadable and no overlapping turns apply.
    """
    if not cues:
        return []

    wav_p: Path | None = None
    sr = 0
    total_frames = 0
    if wav_path is not None:
        cand = Path(wav_path)
        if cand.is_file():
            try:
                import soundfile as sf

                info = sf.info(str(cand))
                sr = int(info.samplerate)
                total_frames = int(info.frames)
                if sr > 0 and total_frames > 0:
                    wav_p = cand
            except Exception:  # noqa: BLE001
                wav_p = None

    abs_thresh = float(10.0 ** (float(threshold_db) / 20.0))
    pad = max(0, int(pad_ms))
    win_frames = max(1, int(round(sr * 0.010))) if sr > 0 else 1

    out: list[Cue] = []
    for cue in cues:
        vad_bounds: tuple[int, int] | None = None
        if wav_p is not None and sr > 0 and total_frames > 0:
            start_f = max(0, min(total_frames, int(round(cue.start_ms * sr / 1000.0))))
            stop_f = max(start_f, min(total_frames, int(round(cue.end_ms * sr / 1000.0))))
            if stop_f > start_f:
                try:
                    samples, _ = read_wav_range(wav_p, start_f, stop_f)
                    mono = np.asarray(samples, dtype=np.float32).ravel()
                except Exception:  # noqa: BLE001
                    mono = np.empty(0, dtype=np.float32)
                if mono.size > 0:
                    n_wins = mono.size // win_frames
                    if n_wins > 0:
                        framed = mono[: n_wins * win_frames].reshape(n_wins, win_frames)
                        rms = np.sqrt(np.mean(framed * framed, axis=1, dtype=np.float64))
                        rem = mono[n_wins * win_frames :]
                        if rem.size > 0:
                            rem_rms = np.sqrt(np.mean(rem * rem, dtype=np.float64))
                            rms = np.append(rms, rem_rms)
                    else:
                        rms = np.array(
                            [float(np.sqrt(np.mean(mono * mono, dtype=np.float64)))],
                            dtype=np.float64,
                        )
                    peak_rms = float(np.max(rms)) if rms.size > 0 else 0.0
                    if peak_rms > abs_thresh:
                        gate = max(abs_thresh, peak_rms * 0.10)
                        active = np.flatnonzero(rms >= gate)
                        if active.size > 0:
                            first_idx = int(active[0])
                            last_idx = int(active[-1])
                            onset_samples = first_idx * win_frames
                            offset_samples = min(mono.size, (last_idx + 1) * win_frames)
                            raw_start_ms = cue.start_ms + int(round(onset_samples * 1000.0 / sr))
                            raw_end_ms = cue.start_ms + int(round(offset_samples * 1000.0 / sr))
                            v_start = max(cue.start_ms, min(cue.end_ms, raw_start_ms - pad))
                            v_end = max(v_start, min(cue.end_ms, raw_end_ms + pad))
                            if v_end > v_start:
                                vad_bounds = (v_start, v_end)

        turn_bounds: tuple[int, int] | None = None
        if turns:
            overlapping = [
                t for t in turns if min(cue.end_ms, t.end_ms) > max(cue.start_ms, t.start_ms)
            ]
            if overlapping:
                spk = (cue.speaker or "").strip()
                spk_turns = [t for t in overlapping if t.speaker == spk] if spk else []
                chosen_turns = spk_turns if spk_turns else overlapping
                t_start = max(cue.start_ms, min(t.start_ms for t in chosen_turns))
                t_end = min(cue.end_ms, max(t.end_ms for t in chosen_turns))
                if t_end > t_start:
                    turn_bounds = (t_start, t_end)

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
            out.append(replace(cue, speech_start_ms=s_ms, speech_end_ms=e_ms))
        else:
            out.append(cue)
    return out


def assign_speakers_from_turns(
    cues: Sequence[Cue],
    turns: Sequence[RttmTurn],
    *,
    wav_path: str | Path | None = None,
) -> list[Cue]:
    """Assign each ``Cue`` to the RTTM speaker turn with maximum temporal overlap."""
    if not cues:
        return []
    if not turns:
        out = [replace(c, speaker=c.speaker or "SPEAKER_01") for c in cues]
        return refine_cues_speech_bounds(out, wav_path)

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
    return refine_cues_speech_bounds(out, wav_path, turns=turns)


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


def _cluster_embeddings(
    embeddings: np.ndarray,
    max_speakers: int = 0,
    *,
    cancel_check: Callable[[], bool] | None = None,
) -> list[int]:
    """Cosine agglomerative clustering returning 0-based speaker cluster indices.

    ``max_speakers`` is a ceiling, not an exact k: the best silhouette among
    2..ceiling wins. The previous exact-k behaviour turned "at most 4" into
    "exactly 4 labels", splitting a 2-person video into 4 speakers.
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
    if cap == 0 and float(np.max(dist)) < 0.08:
        return [0] * n

    max_k = min(n, cap) if cap else min(6, n)
    clusters: list[list[int]] = [[i] for i in range(n)]
    cluster_sum = dist.astype(np.float64, copy=True)
    sizes = np.ones(n, dtype=np.float64)
    partitions: dict[int, list[int]] = {}

    def _snapshot() -> list[int]:
        lbls = [0] * n
        for cid, members in enumerate(clusters):
            for idx in members:
                lbls[idx] = cid
        return lbls

    if len(clusters) <= max_k:
        partitions[len(clusters)] = _snapshot()

    while len(clusters) > 1:
        if cancel_check is not None and cancel_check():
            raise DiarizationCancelled("Đã hủy phân tách người nói.")
        m = len(clusters)
        avg_dist = cluster_sum / np.outer(sizes, sizes)
        np.fill_diagonal(avg_dist, np.inf)
        flat_idx = int(np.argmin(avg_dist))
        a, b = divmod(flat_idx, m)
        if a > b:
            a, b = b, a
        cluster_sum[a, :] += cluster_sum[b, :]
        cluster_sum[:, a] = cluster_sum[a, :]
        cluster_sum[a, a] = 0.0
        keep = [idx for idx in range(m) if idx != b]
        cluster_sum = cluster_sum[np.ix_(keep, keep)]
        sizes[a] += sizes[b]
        sizes = sizes[keep]
        clusters[a].extend(clusters.pop(b))
        if len(clusters) <= max_k:
            partitions[len(clusters)] = _snapshot()

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
    raw_labels = best_labels if best_k > 1 else [0] * n
    # Renumber clusters in order of first appearance (0 -> SPEAKER_01, 1 -> SPEAKER_02, ...)
    remap: dict[int, int] = {}
    ordered: list[int] = []
    for lbl in raw_labels:
        if lbl not in remap:
            remap[lbl] = len(remap)
        ordered.append(remap[lbl])
    return ordered


TURNS_CACHE_FILENAME = "diar_turns_cache.json"
SHARED_WAV_FILENAME = "source_16k.wav"
WAV_INPUT_16K_FILENAME = "source_wav_16k.wav"
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
    # "auto", or a GPU the installed CPU/Metal runtime cannot serve: let the
    # binary pick the best compiled backend instead of failing on "vulkan".
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


def _turns_cache_key(wav_path: Path, config: DiarizationConfig) -> str:
    try:
        stat = wav_path.stat()
    except OSError:
        return ""
    return f"{wav_path.resolve()}|{stat.st_size}|{stat.st_mtime_ns}|{config.model}"


def _load_cached_turns(work_dir: Path | None, key: str) -> list[RttmTurn] | None:
    if work_dir is None or not key:
        return None
    path = work_dir / TURNS_CACHE_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("key") != key:
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


def _is_16k_mono_wav(path: Path) -> bool:
    try:
        import soundfile as sf

        info = sf.info(str(path))
    except Exception:  # noqa: BLE001 - not a readable WAV: treat as needing conversion
        return False
    return int(info.samplerate) == MODEL_SAMPLE_RATE and int(info.channels) == 1


def _ensure_16k_wav(
    src: Path,
    work_dir: Path | None,
    data_dir: Path | None,
    temp_dir: tempfile.TemporaryDirectory[str] | None,
    *,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    """Return a 16 kHz mono WAV for ``src``, converting and caching it if needed."""
    if src.suffix.lower() == ".wav" and _is_16k_mono_wav(src):
        return src, temp_dir

    filename = SHARED_WAV_FILENAME if src.suffix.lower() != ".wav" else WAV_INPUT_16K_FILENAME
    if work_dir is not None:
        work_dir.mkdir(parents=True, exist_ok=True)
        target = work_dir / filename
    else:
        temp_dir = tempfile.TemporaryDirectory(prefix="vienetts_diar_")
        target = Path(temp_dir.name) / "diar_16k.wav"

    sidecar = target.with_name(target.name + ".json")
    try:
        src_stat = src.stat()
    except OSError:
        src_stat = None
    expected = None
    if src_stat is not None:
        expected = {
            "source": str(src.resolve()),
            "size": src_stat.st_size,
            "mtime_ns": src_stat.st_mtime_ns,
        }
    reusable = False
    if target.is_file() and target.stat().st_size > 0:
        try:
            stored = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = None
        if isinstance(stored, dict) and expected is not None:
            reusable = all(stored.get(k) == v for k, v in expected.items())
        elif src_stat is not None:
            # The ASR step writes this WAV without a sidecar; accept it only
            # when it is at least as new as the media it was extracted from.
            reusable = target.stat().st_mtime >= src_stat.st_mtime
    if reusable:
        return target, temp_dir

    try:
        extract_video_audio(
            src,
            target,
            sample_rate=MODEL_SAMPLE_RATE,
            mono=True,
            data_dir=data_dir,
            cancel_check=cancel_check,
        )
    except VideoDubCancelled as exc:
        raise DiarizationCancelled("Đã hủy phân tách người nói.") from exc
    except VideoDubError as exc:
        raise DiarizationError(str(exc)) from exc
    if expected is not None:
        with contextlib.suppress(OSError):
            sidecar.write_text(json.dumps(expected), encoding="utf-8")
    return target, temp_dir


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
    cached = _load_cached_turns(work_dir, cache_key)
    if cached is not None:
        if progress_cb is not None:
            progress_cb(1.0)
        return assign_speakers_from_turns(cues, cached, wav_path=wav_path)

    if progress_cb is not None:
        progress_cb(0.1)
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
    try:
        try:
            res = run_cli(cmd, timeout=timeout_s, cancel_check=cancel_check)
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
        if res.returncode != 0:
            detail = (res.stderr or res.stdout).strip().splitlines()[-1:] or ["audiocpp_cli failed"]
            raise DiarizationError(f"Lỗi khi chạy Nemotron-3 Diarization GGUF: {detail[0]}")
        if not turns_out.is_file():
            # Verified upstream: an empty result writes no turns file at all.
            if notes is not None:
                notes.append(
                    "Không phát hiện giọng nói nào trong video, nên chưa có nhãn người nói."
                )
            if progress_cb is not None:
                progress_cb(1.0)
            return assign_speakers_from_turns(cues, [], wav_path=wav_path)
        turns = parse_audiocpp_turns(
            turns_out.read_text(encoding="utf-8", errors="replace"),
            sample_rate=MODEL_SAMPLE_RATE,
        )
        if not turns:
            if notes is not None:
                notes.append(
                    "Không phát hiện giọng nói nào trong video, nên chưa có nhãn người nói."
                )
            if progress_cb is not None:
                progress_cb(1.0)
            return assign_speakers_from_turns(cues, [], wav_path=wav_path)
        _store_cached_turns(work_dir, cache_key, turns)
        if progress_cb is not None:
            progress_cb(1.0)
        return assign_speakers_from_turns(cues, turns, wav_path=wav_path)
    finally:
        turns_temp.cleanup()
        with contextlib.suppress(OSError):
            turns_out.unlink(missing_ok=True)


def _run_acoustic_diarization(
    wav_path: Path,
    cues: Sequence[Cue],
    config: DiarizationConfig,
    *,
    progress_cb: Callable[[float], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> list[Cue]:
    import soundfile as sf

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
    return refine_cues_speech_bounds(tagged, wav_path)


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

        def _primary() -> list[Cue]:
            return _run_nemotron_gguf_diarization(
                wav_path,
                cues,
                cfg,
                data_dir=data_dir,
                work_dir=work_dir,
                progress_cb=progress_cb,
                cancel_check=cancel_check,
                notes=notes,
            )

        def _acoustic(scale_from: float = 0.0) -> list[Cue]:
            if progress_cb is None:
                return _run_acoustic_diarization(wav_path, cues, cfg, cancel_check=cancel_check)
            return _run_acoustic_diarization(
                wav_path,
                cues,
                cfg,
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
                result = _acoustic(scale_from=0.1)
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


def select_speaker_reference_clip(
    cues: Sequence[Cue],
    speaker: str,
) -> tuple[int, int, str] | None:
    """Find the best `(start_ms, end_ms, transcript)` window (ideal 3.5–8.0 s) for ``speaker``."""
    target = (speaker or "").strip()
    spk_cues = [c for c in cues if (c.speaker or "").strip() == target]
    if not spk_cues:
        return None

    best_window: tuple[int, int, str] | None = None
    best_score = -1e9

    # Consider single cues and consecutive same-speaker cue runs with small gaps (<= 400 ms)
    for i, first in enumerate(spk_cues):
        start_ms = first.start_ms
        end_ms = first.end_ms
        texts = [first.text]
        total_gap_ms = 0
        for j in range(i, len(spk_cues)):
            if j > i:
                curr = spk_cues[j]
                gap = curr.start_ms - end_ms
                if gap < 0 or gap > 400 or (curr.end_ms - start_ms) > 8500:
                    break
                total_gap_ms += gap
                end_ms = curr.end_ms
                texts.append(curr.text)
            dur_ms = max(1, end_ms - start_ms)
            joined = " ".join(texts).strip()
            # Ideal voice-cloning reference is 3500..7500 ms with minimal internal silence
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


def extract_speaker_reference_wav(
    media_path: str | Path,
    cues: Sequence[Cue],
    speaker: str,
    output_wav: str | Path,
    *,
    data_dir: Path | None = None,
) -> tuple[Path, str]:
    """Extract the cleanest 48 kHz mono WAV reference clip for ``speaker`` + its transcript."""
    sel = select_speaker_reference_clip(cues, speaker)
    if sel is None:
        raise DiarizationError(f"Không tìm thấy đoạn thoại nào của người nói '{speaker}'.")
    start_ms, end_ms, transcript = sel
    src = Path(media_path)
    if not src.is_file():
        raise DiarizationError(f"Không tìm thấy tệp nguồn: {src.name}")

    target = Path(output_wav)
    target.parent.mkdir(parents=True, exist_ok=True)
    if src.suffix.lower() == ".wav":
        import soundfile as sf

        info = sf.info(str(src))
        sr = int(info.samplerate)
        s_frame = max(0, int(round(start_ms * sr / 1000.0)))
        e_frame = max(s_frame + 1, int(round(end_ms * sr / 1000.0)))
        samples, _ = read_wav_range(src, s_frame, e_frame)
        if samples.size == 0:
            raise DiarizationError("Đoạn mẫu giọng trích xuất bị rỗng.")
        write_wav_file(samples, target, sample_rate=sr)
        return target, transcript

    try:
        extract_video_audio(
            src,
            target,
            sample_rate=48_000,
            mono=True,
            start_ms=start_ms,
            end_ms=end_ms,
            data_dir=data_dir,
        )
    except VideoDubError as exc:
        raise DiarizationError(str(exc)) from exc
    return target, transcript
