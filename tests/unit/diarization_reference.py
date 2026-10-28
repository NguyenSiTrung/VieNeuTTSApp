"""Pre-Task-5.2 diarization algorithms, kept verbatim as parity oracles (test support).

``core.diarization._cluster_embeddings`` replaced the per-merge O(m²)
recompute + matrix compaction with cached nearest neighbours and
Lance–Williams average-linkage row updates; ``refine_cues_speech_bounds``
replaced one WAV open per cue and a full turn scan per cue with a single open
and a sorted sweep. Their outputs are held to these copies (only the function
names differ).
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace
from pathlib import Path

import numpy as np

from vienetts_app.core.audio import read_wav_range
from vienetts_app.core.diarization import MAX_MODEL_SPEAKERS, DiarizationCancelled, RttmTurn
from vienetts_app.core.subtitles import Cue


def reference_refine_cues_speech_bounds(
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


def reference_cluster_embeddings(
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
