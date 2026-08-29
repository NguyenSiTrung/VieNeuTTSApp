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
import soundfile as sf

DEFAULT_SAMPLE_RATE = 48_000

WAVEFORM_ENVELOPE_BUCKETS = 160  # fixed count → shape stable across widths


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
    magnitudes = np.abs(flat)
    peaks = np.array(
        [
            float(np.max(part)) if part.size else 0.0
            for part in np.array_split(magnitudes, buckets)
        ]
    )
    peaks = np.where(np.isfinite(peaks), peaks, 0.0)
    loudest = float(peaks.max()) if peaks.size else 0.0
    if loudest <= 0.0:
        return [0.0] * int(peaks.size)
    return [min(float(p) / loudest, 1.0) for p in peaks]


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
    sf.write(buf, _validate_mono(audio), sample_rate, subtype="FLOAT", format="WAV")
    return buf.getvalue()


def write_wav_file(
    audio: np.ndarray, path: str | Path, sample_rate: int = DEFAULT_SAMPLE_RATE
) -> Path:
    """Write mono float audio to ``path`` as a 48 kHz-float WAV; returns the path."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sf.write(str(path), _validate_mono(audio), sample_rate, subtype="FLOAT")
    return path


def read_wav(path: str | Path) -> tuple[np.ndarray, int]:
    """Read a WAV file back as (float32 mono, sample_rate)."""
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(path)
    data, sr = sf.read(str(path), dtype="float32", always_2d=False)
    return data, int(sr)


def wav_duration_seconds(path: str | Path) -> float:
    """Duration of a WAV file in seconds (via soundfile metadata)."""
    data, sr = read_wav(path)
    return len(data) / sr
