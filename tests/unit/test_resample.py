"""Stateful streaming resampling to the 48 kHz app contract (Phase 2 Task 1)."""

import numpy as np
import pytest

from vienetts_app.core.resample import StreamingResampler, normalize_chunk


def sine(n: int, rate: int, freq: float = 440.0) -> np.ndarray:
    t = np.arange(n, dtype=np.float64) / rate
    return (0.5 * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


def resample_oneshot(data: np.ndarray, src: int, dst: int) -> np.ndarray:
    r = StreamingResampler(src, dst)
    out = r.push(data)
    return np.concatenate([out, r.flush()])


def resample_chunked(data: np.ndarray, src: int, dst: int, sizes: list[int]) -> np.ndarray:
    r = StreamingResampler(src, dst)
    parts = []
    off = 0
    for size in sizes:
        parts.append(r.push(data[off : off + size]))
        off += size
    assert off == data.size
    parts.append(r.flush())
    return np.concatenate(parts)


class TestValidation:
    def test_bad_rates_rejected(self) -> None:
        for bad in (0, -24000, 24.5, "24000", True, None):
            with pytest.raises(ValueError, match="src_rate"):
                StreamingResampler(bad, 48000)  # type: ignore[arg-type]
        with pytest.raises(ValueError, match="dst_rate"):
            StreamingResampler(24000, 0)

    def test_non_mono_rejected(self) -> None:
        r = StreamingResampler(24000, 48000)
        with pytest.raises(ValueError, match="1-D mono"):
            r.push(np.zeros((100, 2), dtype=np.float32))
        with pytest.raises(ValueError, match="numpy array"):
            r.push([0.0, 0.1])  # type: ignore[arg-type]

    def test_non_finite_rejected(self) -> None:
        r = StreamingResampler(24000, 48000)
        bad = np.zeros(100, dtype=np.float32)
        bad[10] = np.nan
        with pytest.raises(ValueError, match="finite"):
            r.push(bad)

    def test_float64_coerced(self) -> None:
        r = StreamingResampler(48000, 48000)
        out = r.push(np.zeros(10, dtype=np.float64))
        assert out.dtype == np.float32


class TestUpsample2x:
    def test_output_length_within_one_sample(self) -> None:
        data = sine(24000, 24000)
        out = resample_oneshot(data, 24000, 48000)
        assert abs(len(out) - 2 * len(data)) <= 1

    def test_chunked_matches_oneshot(self) -> None:
        data = sine(24000, 24000)
        want = resample_oneshot(data, 24000, 48000)
        for sizes in ([24000], [6000, 6000, 6000, 6000], [1] * 24000, [7, 999, 3, 22991]):
            got = resample_chunked(data, 24000, 48000, sizes)
            assert len(got) == len(want)
            np.testing.assert_allclose(got, want, rtol=1e-5, atol=1e-6)

    def test_sine_fidelity(self) -> None:
        data = sine(24000, 24000)
        out = resample_oneshot(data, 24000, 48000)
        ideal = sine(len(out), 48000)
        corr = float(np.corrcoef(out.astype(np.float64), ideal.astype(np.float64))[0, 1])
        assert corr > 0.999

    def test_no_boundary_click(self) -> None:
        data = sine(48000, 24000)
        halves = resample_chunked(data, 24000, 48000, [24000, 24000])
        joint = np.concatenate([halves[: len(halves) // 2], halves[len(halves) // 2 :]])
        jumps = np.abs(np.diff(joint))
        assert jumps.max() < 0.05  # smooth sine never jumps; a phase reset would spike

    def test_empty_push_and_flush(self) -> None:
        r = StreamingResampler(24000, 48000)
        assert r.push(np.zeros(0, dtype=np.float32)).size == 0
        assert r.flush().size == 0
        out = r.push(sine(100, 24000))
        tail = r.flush()
        assert len(out) + len(tail) >= 199

    def test_reset_drops_state(self) -> None:
        r = StreamingResampler(24000, 48000)
        r.push(sine(1000, 24000))
        r.reset()
        data = sine(24000, 24000)
        got = np.concatenate([r.push(data), r.flush()])
        np.testing.assert_allclose(got, resample_oneshot(data, 24000, 48000), rtol=1e-5, atol=1e-6)


class TestPassthroughAndDownsample:
    def test_equal_rates_identity(self) -> None:
        data = sine(48000, 48000)
        assert np.array_equal(resample_chunked(data, 48000, 48000, [10000, 38000]), data)

    def test_downsample_length_bound(self) -> None:
        data = sine(48000, 48000)
        out = resample_oneshot(data, 48000, 24000)
        assert abs(len(out) - len(data) // 2) <= 1


class TestNormalizeChunk:
    def test_validates_mono_float32(self) -> None:
        out = normalize_chunk(np.zeros(16, dtype=np.float64))
        assert out.dtype == np.float32
        with pytest.raises(ValueError, match="1-D mono"):
            normalize_chunk(np.zeros((16, 1), dtype=np.float32))
