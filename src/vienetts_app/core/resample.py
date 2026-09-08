"""Stateful streaming resampling to the 48 kHz app contract (Phase 2 Task 1)."""

import numpy as np

from vienetts_app.core.audio import DEFAULT_SAMPLE_RATE


class StreamingResampler:
    """Linear-interpolation resampler with carry state across chunk pushes.

    Qwen backends emit native-rate chunks (24 kHz); the artifact writer and
    live transport require 48 kHz mono float32. Resampling each chunk
    independently would restart the interpolation phase at every boundary
    (clicks); this class carries the last input sample plus the fractional
    output position across :meth:`push` calls so a chunked stream matches a
    one-shot resample.

    Output length for N total input samples is ``floor((N - 1) * dst / src)
    + 1`` — within one output sample of the ideal duration. Numpy-only
    (scipy is not a project dependency).
    """

    def __init__(self, src_rate: int, dst_rate: int = DEFAULT_SAMPLE_RATE) -> None:
        if not isinstance(src_rate, int) or isinstance(src_rate, bool) or src_rate <= 0:
            raise ValueError(f"src_rate must be a positive int, got {src_rate!r}")
        if not isinstance(dst_rate, int) or isinstance(dst_rate, bool) or dst_rate <= 0:
            raise ValueError(f"dst_rate must be a positive int, got {dst_rate!r}")
        self._src_rate = src_rate
        self._dst_rate = dst_rate
        self._step = src_rate / dst_rate  # input samples per output sample
        self._carry: np.ndarray = np.zeros(0, dtype=np.float32)
        self._pos = 0.0  # input position of the next output sample
        self._total_in = 0

    @property
    def src_rate(self) -> int:
        return self._src_rate

    @property
    def dst_rate(self) -> int:
        return self._dst_rate

    def push(self, chunk: np.ndarray) -> np.ndarray:
        """Resample one native-rate chunk; empty input yields empty output."""
        data = _as_mono_float32(chunk, allow_empty=True)
        if self._src_rate == self._dst_rate:
            out = np.concatenate([self._carry, data]) if self._carry.size else data.copy()
            self._carry = np.zeros(0, dtype=np.float32)
            self._pos = 0.0
            self._total_in += data.size
            return out
        window = np.concatenate([self._carry, data]) if self._carry.size else data
        if window.size < 2:
            self._carry = window.copy()
            return np.zeros(0, dtype=np.float32)
        # Emit while the interpolation pair (floor, floor+1) is in-window.
        positions = []
        pos = self._pos
        while pos <= window.size - 1:
            # The final in-window position emits only during flush (it has
            # no right neighbor yet — more input may still arrive).
            if pos > window.size - 2:
                break
            positions.append(pos)
            pos += self._step
        out = _interpolate(window, np.asarray(positions, dtype=np.float64))
        # Keep every input sample at-or-after the last emitted pair's left
        # index: unconsumed input plus the one-sample overlap for continuity.
        consumed = int(positions[-1]) if positions else 0
        keep_from = max(consumed, 0)
        self._carry = window[keep_from:].copy()
        self._pos = pos - keep_from
        self._total_in += data.size
        return out

    def flush(self) -> np.ndarray:
        """Emit the tail sample(s) up to the final input position."""
        if self._src_rate == self._dst_rate:
            out = self._carry
            self._carry = np.zeros(0, dtype=np.float32)
            self._pos = 0.0
            return out
        window = self._carry
        if window.size == 0:
            return np.zeros(0, dtype=np.float32)
        positions = []
        pos = self._pos
        while pos <= window.size - 1:
            positions.append(pos)
            pos += self._step
        out = _interpolate(window, np.asarray(positions, dtype=np.float64))
        self._carry = np.zeros(0, dtype=np.float32)
        self._pos = 0.0
        return out

    def reset(self) -> None:
        """Drop carried state (cancellation / new utterance)."""
        self._carry = np.zeros(0, dtype=np.float32)
        self._pos = 0.0
        self._total_in = 0


def _as_mono_float32(chunk: np.ndarray, *, allow_empty: bool = False) -> np.ndarray:
    if not isinstance(chunk, np.ndarray):
        raise ValueError(f"chunk must be a numpy array, got {type(chunk).__name__}")
    if chunk.ndim != 1:
        raise ValueError(f"chunk must be 1-D mono, got {chunk.ndim} dimensions")
    if not allow_empty and chunk.size == 0:
        raise ValueError("chunk must not be empty")
    out = chunk.astype(np.float32, copy=False)
    if not np.all(np.isfinite(out)):
        raise ValueError("chunk must be finite")
    return out


def _interpolate(window: np.ndarray, positions: np.ndarray) -> np.ndarray:
    if positions.size == 0:
        return np.zeros(0, dtype=np.float32)
    left = np.floor(positions).astype(np.int64)
    frac = (positions - left).astype(np.float32)
    # Clamp the right index at the window end (exact-end flush position).
    right = np.minimum(left + 1, window.size - 1)
    return ((1.0 - frac) * window[left] + frac * window[right]).astype(np.float32)


def normalize_chunk(chunk: np.ndarray) -> np.ndarray:
    """Validate one backend chunk as finite mono float32 (routing seam)."""
    return _as_mono_float32(chunk)
