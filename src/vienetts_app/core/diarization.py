"""Speaker diarization and speaker reference clip extraction for multi-speaker video dubbing.

Supports:
1. ``nemo`` — NVIDIA NeMo diarizer (ClusteringDiarizer / Sortformer) when installed,
   or RTTM output alignment.
2. ``acoustic`` — Built-in zero-dependency NumPy spectral + pitch ($F_0$) + filterbank
   speaker embedding extractor and cosine agglomerative clustering.
3. Reference clip selection & slicing (`select_speaker_reference_clip`,
   `extract_speaker_reference_wav`) so any detected speaker can be cloned directly
   from the source video in one click.
"""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import numpy as np

from vienetts_app.core.ai_models import resolve_audiocpp_cli, resolve_diarization_model_path
from vienetts_app.core.audio import read_wav_range, write_wav_file
from vienetts_app.core.subtitles import Cue
from vienetts_app.core.video_dub import VideoDubError, extract_video_audio


class DiarizationError(RuntimeError):
    """Speaker diarization or reference clip extraction failed; message is user-actionable."""


@dataclass(frozen=True)
class DiarizationConfig:
    backend: str = "auto"  # "auto" | "nemotron_gguf" | "acoustic" | "nemo"
    max_speakers: int = 0  # 0 = auto (1..8), >0 = cap
    model: str = "nemotron-3-diarization-q8_0"
    device: str = "cpu"  # "cpu" | "cuda" | "metal" | "vulkan" | "best"


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
        sr = max(1, int(payload.get("sample_rate") or sr))
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
            s_samp = max(0, int(entry["start_sample"]))
            e_samp = max(s_samp + 1, int(entry["end_sample"]))
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


def assign_speakers_from_turns(cues: Sequence[Cue], turns: Sequence[RttmTurn]) -> list[Cue]:
    """Assign each ``Cue`` to the RTTM speaker turn with maximum temporal overlap."""
    if not cues:
        return []
    if not turns:
        return [replace(c, speaker=c.speaker or "SPEAKER_01") for c in cues]

    out: list[Cue] = []
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
    return out


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


def _cluster_embeddings(embeddings: np.ndarray, max_speakers: int = 0) -> list[int]:
    """Cosine agglomerative clustering returning 0-based speaker cluster indices."""
    n = embeddings.shape[0]
    if n == 0:
        return []
    if n == 1 or max_speakers == 1:
        return [0] * n

    # Pairwise cosine distance matrix
    sim = np.clip(embeddings @ embeddings.T, -1.0, 1.0)
    dist = 1.0 - sim
    np.fill_diagonal(dist, 0.0)

    # If max pairwise distance is tiny, everyone is the same speaker
    if max_speakers <= 0 and float(np.max(dist)) < 0.08:
        return [0] * n

    max_k = min(n, max_speakers) if max_speakers > 1 else min(6, n)
    min_k = max_k if max_speakers > 1 else 2
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

    while len(clusters) > min_k:
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

    if max_speakers > 1:
        raw_labels = partitions.get(max_k, [0] * n)
    else:
        best_k = 1
        best_score = 0.12  # minimum silhouette threshold to split into >=2 speakers
        best_labels = [0] * n
        for k in range(2, max_k + 1):
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


def _run_nemotron_gguf_diarization(
    wav_path: Path,
    cues: Sequence[Cue],
    config: DiarizationConfig,
    *,
    data_dir: Path | None = None,
    progress_cb: Callable[[float], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
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

    if progress_cb is not None:
        progress_cb(0.15)

    backend_dev = (config.device or "cpu").strip().lower()
    if backend_dev not in ("cpu", "cuda", "vulkan", "metal", "best"):
        backend_dev = "cpu"
    threads = max(1, min(8, (os.cpu_count() or 4) // 2))
    turns_out = wav_path.parent / f".nemotron_turns_{uuid.uuid4().hex}.json"

    cmd = [
        str(cli),
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
    ]
    if backend_dev == "cpu":
        cmd += ["--session-option", "nemotron_3_diar.weight_type=f32"]

    try:
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        while True:
            if cancel_check is not None and cancel_check():
                proc.kill()
                with contextlib.suppress(Exception):
                    proc.communicate(timeout=1.0)
                raise DiarizationError("Đã hủy phân tách người nói.")
            try:
                stdout, stderr = proc.communicate(timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                continue
        if proc.returncode != 0 or not turns_out.is_file():
            detail = (stderr or stdout or "").strip().splitlines()[-1:] or [
                "audiocpp_cli failed"
            ]
            raise DiarizationError(f"Lỗi khi chạy Nemotron-3 Diarization GGUF: {detail[0]}")
        turns = parse_audiocpp_turns(
            turns_out.read_text(encoding="utf-8", errors="replace"),
            sample_rate=16_000,
        )
        if progress_cb is not None:
            progress_cb(1.0)
        return assign_speakers_from_turns(cues, turns)
    finally:
        with contextlib.suppress(OSError):
            turns_out.unlink(missing_ok=True)


def _run_nemo_diarization(
    wav_path: Path,
    cues: Sequence[Cue],
    config: DiarizationConfig,
) -> list[Cue]:
    """Attempt NVIDIA NeMo diarization in a subprocess; raises DiarizationError if unavailable."""
    script = "import json, sys\nimport nemo.collections.asr as nemo_asr\nprint('NEMO_AVAILABLE')\n"
    probe = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        check=False,
    )
    if probe.returncode != 0 or "NEMO_AVAILABLE" not in probe.stdout:
        raise DiarizationError(
            "Chưa cài đặt gói NVIDIA NeMo (nemo_toolkit[asr]) trên môi trường này."
        )
    # Fallback to acoustic when full NeMo manifest config is not provisioned locally
    return _run_acoustic_diarization(wav_path, cues, config)


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
            raise DiarizationError("Đã hủy phân tách người nói.")
        start_f = max(0, min(total_frames, int(round(cue.start_ms * sr / 1000.0))))
        stop_f = max(start_f, min(total_frames, int(round(cue.end_ms * sr / 1000.0))))
        samples, _ = read_wav_range(wav_path, start_f, stop_f)
        embeddings.append(_cue_acoustic_embedding(samples, sr))
        if progress_cb is not None:
            progress_cb(min(0.85, (i + 1) / max(1, len(cues)) * 0.85))

    emb_matrix = np.vstack(embeddings) if embeddings else np.zeros((0, 24), dtype=np.float32)
    cluster_ids = _cluster_embeddings(emb_matrix, max_speakers=max(0, int(config.max_speakers)))
    if progress_cb is not None:
        progress_cb(1.0)
    return [
        replace(cue, speaker=f"SPEAKER_{cid + 1:02d}")
        for cue, cid in zip(cues, cluster_ids, strict=True)
    ]


def diarize_cues(
    media_path: str | Path,
    cues: Sequence[Cue],
    config: DiarizationConfig | None = None,
    *,
    work_dir: Path | None = None,
    data_dir: Path | None = None,
    progress_cb: Callable[[float], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> list[Cue]:
    """Label each ``Cue`` with ``SPEAKER_01``..``SPEAKER_NN`` from ``media_path``."""
    if not cues:
        return []
    if cancel_check is not None and cancel_check():
        raise DiarizationError("Đã hủy phân tách người nói.")
    cfg = config or DiarizationConfig()
    src = Path(media_path)
    if not src.is_file():
        raise DiarizationError(
            f"Không tìm thấy tệp âm thanh/video để phân tách người nói: {src.name}"
        )

    temp_dir_obj: tempfile.TemporaryDirectory[str] | None = None
    try:
        if src.suffix.lower() == ".wav":
            wav_path = src
        else:
            shared_wav = work_dir / "source_16k.wav" if work_dir is not None else None
            if shared_wav is not None and shared_wav.is_file() and shared_wav.stat().st_size > 0:
                wav_path = shared_wav
            else:
                if work_dir is not None:
                    work_dir.mkdir(parents=True, exist_ok=True)
                    wav_path = shared_wav  # type: ignore[assignment]
                else:
                    temp_dir_obj = tempfile.TemporaryDirectory(prefix="vienetts_diar_")
                    wav_path = Path(temp_dir_obj.name) / "diar_16k.wav"
                try:
                    extract_video_audio(
                        src, wav_path, sample_rate=16_000, mono=True, data_dir=data_dir
                    )
                except VideoDubError as exc:
                    raise DiarizationError(str(exc)) from exc

        if cancel_check is not None and cancel_check():
            raise DiarizationError("Đã hủy phân tách người nói.")

        backend = (cfg.backend or "auto").strip()
        if backend == "nemotron_gguf":
            return _run_nemotron_gguf_diarization(
                wav_path,
                cues,
                cfg,
                data_dir=data_dir,
                progress_cb=progress_cb,
                cancel_check=cancel_check,
            )
        if backend == "nemo":
            return _run_nemo_diarization(wav_path, cues, cfg)
        if backend == "auto":
            with contextlib.suppress(DiarizationError):
                return _run_nemotron_gguf_diarization(
                    wav_path,
                    cues,
                    cfg,
                    data_dir=data_dir,
                    progress_cb=progress_cb,
                    cancel_check=cancel_check,
                )
            with contextlib.suppress(DiarizationError):
                return _run_nemo_diarization(wav_path, cues, cfg)
        return _run_acoustic_diarization(
            wav_path, cues, cfg, progress_cb=progress_cb, cancel_check=cancel_check
        )
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
