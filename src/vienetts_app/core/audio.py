"""WAV encode/export helpers (§10).

The engine produces mono ``np.float32 @ 48 kHz``; these helpers turn that
into in-memory WAV bytes (for ``QBuffer`` playback in Phase 4) or files, plus
the peak-normalized envelope downsampling shared by every waveform widget
(app tabs and the audiobook studio).
"""

from __future__ import annotations

import io
from pathlib import Path

import numpy as np

DEFAULT_SAMPLE_RATE = 48_000

WAVEFORM_ENVELOPE_BUCKETS = 160  # fixed count → shape stable across widths


def _sf():
    """soundfile, imported on first encode/read (~55 ms — off the startup path;
    every import here is WAV I/O, never startup work)."""
    import soundfile as sf

    return sf


def compute_waveform_envelope(
    samples: np.ndarray,
    buckets: int = WAVEFORM_ENVELOPE_BUCKETS,
) -> list[float]:
    """Peak-normalized 0..1 envelope of a finished buffer (waveform widgets).

    Fixed bucket count keeps the shape stable across window sizes. max-abs
    per bucket, normalized to the buffer's own loudest bucket so the
    overview always spans the full height; all-silent (or non-finite) audio
    yields all-zero buckets — never a division by zero.
    """
    flat = np.asarray(samples, dtype=np.float32).ravel()
    if flat.size == 0:
        return []
    # Peak per bucket via min/max reductions over split VIEWS. The obvious
    # ``np.abs(flat)`` allocates a full-size temporary (346 MB at 30-min
    # audio) — this runs on the GUI thread at every synthesis completion.
    peaks = [
        max(float(part.max(initial=0.0)), -float(part.min(initial=0.0)))
        for part in np.array_split(flat, buckets)
    ]
    peaks = [p if np.isfinite(p) else 0.0 for p in peaks]
    loudest = max(peaks, default=0.0)
    if loudest <= 0.0:
        return [0.0] * len(peaks)
    return [min(p / loudest, 1.0) for p in peaks]


def _validate_mono(audio: np.ndarray) -> np.ndarray:
    if not isinstance(audio, np.ndarray):
        raise ValueError(f"audio must be a numpy array, got {type(audio).__name__}")
    if audio.ndim != 1:
        raise ValueError(f"audio must be 1-D mono, got {audio.ndim} dimensions")
    if audio.size == 0:
        raise ValueError("audio must not be empty")
    return audio.astype(np.float32, copy=False)


def encode_wav_bytes(audio: np.ndarray, sample_rate: int = DEFAULT_SAMPLE_RATE) -> bytes:
    """Encode mono float audio as an in-memory WAV (RIFF) blob."""
    if sample_rate <= 0:
        raise ValueError(f"sample_rate must be > 0, got {sample_rate}")
    buf = io.BytesIO()
    _sf().write(buf, _validate_mono(audio), sample_rate, subtype="FLOAT", format="WAV")
    return buf.getvalue()


def write_wav_file(
    audio: np.ndarray, path: str | Path, sample_rate: int = DEFAULT_SAMPLE_RATE
) -> Path:
    """Write mono float audio to ``path`` as a 48 kHz-float WAV; returns the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    _sf().write(str(path), _validate_mono(audio), sample_rate, subtype="FLOAT")
    return path


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """Read a WAV file back as (float32 mono, sample_rate)."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    data, sr = _sf().read(str(path), dtype="float32", always_2d=False)
    return data, int(sr)


def compute_waveform_envelope_from_wav(
    path: str | Path,
    buckets: int = WAVEFORM_ENVELOPE_BUCKETS,
    block_frames: int = 1 << 16,
) -> list[float]:
    """Peak-normalized 0..1 envelope computed WITHOUT decoding the whole file.

    Streams the WAV in fixed blocks and reduces each bucket's peak from the
    overlapping slices, so a 30-minute chapter (a ~350 MB float32 decode)
    costs one 256k-frame buffer at a time. Normalization matches
    :func:`compute_waveform_envelope` (loudest bucket → 1.0).
    """
    sf = _sf()
    info = sf.info(str(path))
    total = max(int(info.frames), 1)
    # Boundaries replicate np.array_split exactly (quotient + remainder
    # spread over the first buckets), so the streamed overview is identical
    # to the in-memory one — including files shorter than the bucket count.
    quotient, remainder = divmod(total, buckets)
    indices = np.arange(buckets + 1, dtype=np.int64)
    edges = np.minimum(indices * quotient + np.minimum(indices, remainder), total)
    peaks = np.zeros(buckets, dtype=np.float32)
    pos = 0
    with sf.SoundFile(str(path)) as handle:
        while pos < total:
            block = handle.read(block_frames, dtype="float32", always_2d=False)
            if block.size == 0:
                break
            end = pos + block.shape[0]
            first = max(int(np.searchsorted(edges, pos, side="right")) - 1, 0)
            last = min(int(np.searchsorted(edges, end, side="left")), buckets)
            for i in range(first, last):
                lo = max(int(edges[i]), pos) - pos
                hi = min(int(edges[i + 1]), end) - pos
                if hi > lo:
                    chunk = block[lo:hi]
                    peak = max(float(chunk.max(initial=0.0)), -float(chunk.min(initial=0.0)))
                    if peak > peaks[i]:
                        peaks[i] = peak
            pos = end
    peaks = [float(p) if np.isfinite(p) else 0.0 for p in peaks]
    loudest = max(peaks, default=0.0)
    if loudest <= 0.0:
        return [0.0] * len(peaks)
    return [min(p / loudest, 1.0) for p in peaks]


def wav_duration_seconds(path: str | Path) -> float:
    """Duration of a WAV file in seconds (via soundfile metadata)."""
    data, sr = read_wav(path)
    return len(data) / sr
