"""Audio helpers: float32@48k → WAV (bytes + file), read-back via soundfile."""

import io
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from vienetts_app.core.audio import (
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
