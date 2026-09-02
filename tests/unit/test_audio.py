"""Audio helpers: float32@48k → WAV (bytes + file), read-back via soundfile."""

import io
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from vienetts_app.core.audio import (
    compute_waveform_envelope,
    compute_waveform_envelope_from_wav,
    encode_wav_bytes,
    read_wav,
    wav_duration_seconds,
    write_wav_file,
)


def tone(samples: int = 48_000, freq: float = 440.0, sr: int = 48_000) -> np.ndarray:
    t = np.arange(samples, dtype=np.float32) / sr
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class TestEncodeWavBytes:
    def test_returns_riff_wav_bytes(self) -> None:
        data = encode_wav_bytes(tone(1000))
        assert isinstance(data, bytes)
        assert data[:4] == b"RIFF"
        assert data[8:12] == b"WAVE"

    def test_soundfile_reads_back_at_48k_float32(self) -> None:
        original = tone(2400)
        data = encode_wav_bytes(original)
        got, sr = sf.read(io.BytesIO(data), dtype="float32")
        assert sr == 48_000
        assert got.dtype == np.float32
        assert np.allclose(got, original, atol=1e-6)

    def test_custom_sample_rate_respected(self) -> None:
        data = encode_wav_bytes(tone(100, sr=24_000), sample_rate=24_000)
        _, sr = sf.read(io.BytesIO(data))
        assert sr == 24_000

    def test_float64_input_is_cast_to_float32(self) -> None:
        data = encode_wav_bytes(tone(100).astype(np.float64))
        got, _ = sf.read(io.BytesIO(data), dtype="float32")
        assert got.dtype == np.float32


class TestWriteWavFile:
    def test_writes_valid_wav(self, tmp_path: Path) -> None:
        original = tone(4800)
        path = write_wav_file(original, tmp_path / "out.wav")
        assert path.is_file()
        got, sr = sf.read(str(path), dtype="float32")
        assert sr == 48_000
        assert np.allclose(got, original, atol=1e-6)

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        path = write_wav_file(tone(100), tmp_path / "a" / "b" / "out.wav")
        assert path.is_file()

    def test_accepts_str_path(self, tmp_path: Path) -> None:
        path = write_wav_file(tone(100), str(tmp_path / "s.wav"))
        assert Path(path).is_file()


class TestReadBack:
    def test_wav_duration_seconds(self, tmp_path: Path) -> None:
        path = write_wav_file(tone(48_000), tmp_path / "d.wav")  # exactly 1 s
        assert wav_duration_seconds(path) == pytest.approx(1.0)

    def test_read_wav_returns_data_and_rate(self, tmp_path: Path) -> None:
        original = tone(500)
        path = write_wav_file(original, tmp_path / "r.wav")
        data, sr = read_wav(path)
        assert sr == 48_000
        assert data.dtype == np.float32
        assert np.allclose(data, original, atol=1e-6)


class TestValidation:
    @pytest.mark.parametrize(
        "bad",
        [
            np.array([], dtype=np.float32),  # empty
            np.zeros((100, 2), dtype=np.float32),  # stereo not produced by the SDK
        ],
    )
    def test_invalid_audio_raises(self, bad: np.ndarray) -> None:
        with pytest.raises(ValueError):
            encode_wav_bytes(bad)

    def test_invalid_sample_rate_raises(self) -> None:
        with pytest.raises(ValueError, match="sample_rate"):
            encode_wav_bytes(tone(100), sample_rate=0)

    def test_read_missing_file_raises(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            read_wav(tmp_path / "missing.wav")


class TestComputeWaveformEnvelope:
    """Peak-normalized overview buckets shared by every waveform widget."""

    def test_empty_audio_yields_no_buckets(self) -> None:
        assert compute_waveform_envelope(np.array([], dtype=np.float32)) == []

    def test_buckets_are_peak_normalized(self) -> None:
        audio = np.concatenate(
            [
                np.full(2_400, 0.5, dtype=np.float32),
                np.zeros(2_400, dtype=np.float32),
            ]
        )
        envelope = compute_waveform_envelope(audio, buckets=8)
        assert len(envelope) == 8
        assert max(envelope) == pytest.approx(1.0)
        assert envelope[0] == pytest.approx(1.0)
        assert envelope[-1] == pytest.approx(0.0)

    def test_bucket_count_capped_and_values_clamped(self) -> None:
        audio = np.full(100_000, 4.0, dtype=np.float32)  # overshoot clamps
        envelope = compute_waveform_envelope(audio, buckets=160)
        assert len(envelope) == 160
        assert all(0.0 <= v <= 1.0 for v in envelope)
        assert all(v == pytest.approx(1.0) for v in envelope)

    def test_silence_is_all_zero_not_nan(self) -> None:
        envelope = compute_waveform_envelope(np.zeros(4_800, dtype=np.float32))
        assert envelope
        assert all(v == 0.0 for v in envelope)

    def test_non_finite_samples_treated_as_silence(self) -> None:
        audio = np.array([np.nan, np.inf, -0.5, 0.5], dtype=np.float32)
        envelope = compute_waveform_envelope(audio, buckets=2)
        assert len(envelope) == 2
        assert max(envelope) == pytest.approx(1.0)

    def test_matches_naive_abs_reference(self) -> None:
        # The reduction-based implementation must be bit-for-bit equivalent
        # to the obvious np.abs-per-bucket reference (incl. NaN/inf and
        # bucket counts that don't divide the length).
        rng = np.random.default_rng(2026)
        for size, buckets, poison in [
            (1, 4, False),
            (997, 7, False),
            (48_000, 160, False),
            (5, 3, True),
        ]:
            audio = rng.standard_normal(size).astype(np.float32)
            if poison:
                audio[1] = np.nan
                audio[3] = np.inf
            flat = audio.ravel()
            reference_peaks = [
                float(np.max(np.abs(part))) if part.size else 0.0
                for part in np.array_split(np.abs(flat), buckets)
            ]
            reference_peaks = [p if np.isfinite(p) else 0.0 for p in reference_peaks]
            loudest = max(reference_peaks, default=0.0)
            if loudest <= 0.0:
                expected = [0.0] * len(reference_peaks)
            else:
                expected = [min(p / loudest, 1.0) for p in reference_peaks]
            assert compute_waveform_envelope(audio, buckets=buckets) == expected


class TestComputeWaveformEnvelopeFromWav:
    """Block-wise streaming variant for legacy chapters (no full decode)."""

    def test_matches_in_memory_envelope(self, tmp_path: Path) -> None:
        rng = np.random.default_rng(2026)
        audio = rng.standard_normal(120_000).astype(np.float32) * 0.3
        path = tmp_path / "ch.wav"
        write_wav_file(audio, path)
        streamed = compute_waveform_envelope_from_wav(path, buckets=160)
        in_memory = compute_waveform_envelope(audio, buckets=160)
        assert len(streamed) == 160
        assert streamed == pytest.approx(in_memory, abs=1e-6)

    def test_tiny_file_shorter_than_bucket_count(self, tmp_path: Path) -> None:
        audio = np.array([0.1, -0.9, 0.3, 0.05], dtype=np.float32)
        path = tmp_path / "tiny.wav"
        write_wav_file(audio, path)
        streamed = compute_waveform_envelope_from_wav(path, buckets=160)
        in_memory = compute_waveform_envelope(audio, buckets=160)
        assert len(streamed) == 160
        assert streamed == pytest.approx(in_memory, abs=1e-6)

    def test_silence_is_all_zero(self, tmp_path: Path) -> None:
        path = tmp_path / "quiet.wav"
        write_wav_file(np.zeros(9_600, dtype=np.float32), path)
        envelope = compute_waveform_envelope_from_wav(path)
        assert len(envelope) == 160
        assert all(v == 0.0 for v in envelope)

    def test_uses_small_block_frames_stream(self, tmp_path: Path, monkeypatch) -> None:
        # The streaming contract: with a 1-frame block size the reduction
        # still lands the exact per-bucket peaks (bucket edges exercised).
        audio = np.linspace(-1.0, 1.0, 500, dtype=np.float32)
        path = tmp_path / "lin.wav"
        write_wav_file(audio, path)
        streamed = compute_waveform_envelope_from_wav(path, buckets=16, block_frames=1)
        assert streamed == pytest.approx(compute_waveform_envelope(audio, buckets=16), abs=1e-6)
