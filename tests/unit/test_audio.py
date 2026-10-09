"""Audio helpers: float32@48k → WAV (bytes + file), read-back via soundfile."""

import io
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from tests.unit.wsola_reference import reference_time_stretch

from vienetts_app.core.audio import (
    compute_waveform_envelope,
    compute_waveform_envelope_from_wav,
    encode_wav_bytes,
    export_audio_file,
    export_format_for,
    export_wav_file,
    read_wav,
    time_stretch_audio,
    wav_duration_seconds,
    write_wav_file,
)


def tone(samples: int = 48_000, freq: float = 440.0, sr: int = 48_000) -> np.ndarray:
    t = np.arange(samples, dtype=np.float32) / sr
    return (0.5 * np.sin(2 * np.pi * freq * t)).astype(np.float32)


class TestEncodeWavBytes:
    def test_encode_wav_bytes(self) -> None:
        original = tone(1000)
        data = encode_wav_bytes(original)
        assert isinstance(data, bytes)
        assert data[:4] == b"RIFF"
        assert data[8:12] == b"WAVE"
        # The bytes are themselves a valid 48 kHz float32 WAV of the input.
        got, sr = sf.read(io.BytesIO(data), dtype="float32")
        assert sr == 48_000
        assert got.dtype == np.float32
        assert np.allclose(got, original, atol=1e-6)

        data = encode_wav_bytes(tone(100, sr=24_000), sample_rate=24_000)
        _, sr = sf.read(io.BytesIO(data))
        assert sr == 24_000
        data = encode_wav_bytes(tone(100).astype(np.float64))
        got, _ = sf.read(io.BytesIO(data), dtype="float32")
        assert got.dtype == np.float32


class TestWriteWavFile:
    def test_write_wav_file(self, tmp_path: Path) -> None:
        original = tone(4800)
        path = write_wav_file(original, tmp_path / "out.wav")
        assert path.is_file()
        got, sr = sf.read(str(path), dtype="float32")
        assert sr == 48_000
        assert np.allclose(got, original, atol=1e-6)

        assert write_wav_file(tone(100), tmp_path / "a" / "b" / "out.wav").is_file()
        assert Path(write_wav_file(tone(100), str(tmp_path / "s.wav"))).is_file()


class TestReadBack:
    def test_read_back(self, tmp_path: Path) -> None:
        path = write_wav_file(tone(48_000), tmp_path / "d.wav")  # exactly 1 s
        assert wav_duration_seconds(path) == pytest.approx(1.0)

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

    def test_validation_errors(self, tmp_path: Path) -> None:
        with pytest.raises(ValueError, match="sample_rate"):
            encode_wav_bytes(tone(100), sample_rate=0)

        with pytest.raises(FileNotFoundError):
            read_wav(tmp_path / "missing.wav")


class TestComputeWaveformEnvelope:
    """Peak-normalized overview buckets shared by every waveform widget."""

    def test_envelope_bucket_contract(self) -> None:
        assert compute_waveform_envelope(np.array([], dtype=np.float32)) == []

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

        audio = np.full(100_000, 4.0, dtype=np.float32)  # overshoot clamps
        envelope = compute_waveform_envelope(audio, buckets=160)
        assert len(envelope) == 160
        assert all(0.0 <= v <= 1.0 for v in envelope)
        assert all(v == pytest.approx(1.0) for v in envelope)

    def test_envelope_edge_and_reference(self) -> None:
        envelope = compute_waveform_envelope(np.zeros(4_800, dtype=np.float32))
        assert envelope
        assert all(v == 0.0 for v in envelope)
        audio = np.array([np.nan, np.inf, -0.5, 0.5], dtype=np.float32)
        envelope = compute_waveform_envelope(audio, buckets=2)
        assert len(envelope) == 2
        assert max(envelope) == pytest.approx(1.0)

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

    def test_envelope_from_wav(self, tmp_path: Path, monkeypatch) -> None:
        rng = np.random.default_rng(2026)
        audios = [
            rng.standard_normal(120_000).astype(np.float32) * 0.3,
            np.array([0.1, -0.9, 0.3, 0.05], dtype=np.float32),  # shorter than bucket count
        ]
        for i, audio in enumerate(audios):
            path = tmp_path / f"ch{i}.wav"
            write_wav_file(audio, path)
            streamed = compute_waveform_envelope_from_wav(path, buckets=160)
            in_memory = compute_waveform_envelope(audio, buckets=160)
            assert len(streamed) == 160
            assert streamed == pytest.approx(in_memory, abs=1e-6)

        path = tmp_path / "quiet.wav"
        write_wav_file(np.zeros(9_600, dtype=np.float32), path)
        envelope = compute_waveform_envelope_from_wav(path)
        assert len(envelope) == 160
        assert all(v == 0.0 for v in envelope)

        # The streaming contract: with a 1-frame block size the reduction
        # still lands the exact per-bucket peaks (bucket edges exercised).
        audio = np.linspace(-1.0, 1.0, 500, dtype=np.float32)
        path = tmp_path / "lin.wav"
        write_wav_file(audio, path)
        streamed = compute_waveform_envelope_from_wav(path, buckets=16, block_frames=1)
        assert streamed == pytest.approx(compute_waveform_envelope(audio, buckets=16), abs=1e-6)


class TestTimeStretchAudio:
    def test_rate_contract(self) -> None:
        orig = tone(4800)
        res = time_stretch_audio(orig, rate=1.0)
        assert res is orig or np.array_equal(res, orig)

        for rate, expected_len in [(1.25, 38_400), (0.8, 60_000)]:
            res = time_stretch_audio(tone(48_000), rate=rate)
            assert res.dtype == np.float32
            assert abs(len(res) - expected_len) <= 200

        with pytest.raises(ValueError, match="rate"):
            time_stretch_audio(tone(100), rate=0.0)
        with pytest.raises(ValueError, match="rate"):
            time_stretch_audio(tone(100), rate=-0.5)

        for n in [32, 100, 500, 1024]:
            orig = tone(n)
            res = time_stretch_audio(orig, rate=1.2)
            assert len(res) > 0
            assert not np.isnan(res).any()

    def test_wsola_quality(self) -> None:
        # Generate a harmonic voice-like signal (F0 = 150 Hz, harmonics 1..10)
        # where all energy is at >= 150 Hz and < 50 Hz is completely silent.
        sr = 48_000
        t = np.linspace(0, 1.0, sr, endpoint=False, dtype=np.float32)
        signal = np.zeros_like(t)
        for h in range(1, 10):
            signal += (1.0 / h) * np.sin(2 * np.pi * 150 * h * t)
        signal *= np.hanning(len(t))

        stretched = time_stretch_audio(signal, rate=1.25, sample_rate=sr)
        fft = np.abs(np.fft.rfft(stretched))
        freqs = np.fft.rfftfreq(len(stretched), 1 / sr)
        sub_bass_energy = np.sum(fft[freqs < 60] ** 2)
        total_energy = np.sum(fft**2) + 1e-9
        # In phase vocoder, sub-bass rumble was > 15%; in WSOLA with 50 Hz filter, it is < 0.01%
        assert (sub_bass_energy / total_energy) < 0.001

        orig = np.ones(4800, dtype=np.float32)
        stretched = time_stretch_audio(orig, rate=1.2, sample_rate=48_000)
        # Micro-fade ensures the first and last samples taper to 0 without abrupt step
        assert abs(stretched[0]) < 1e-4
        assert abs(stretched[-1]) < 1e-4
        assert np.all(np.isfinite(stretched))
        assert np.max(np.abs(stretched)) <= 1.05


def speechy(seconds: float, sr: int = 48_000, seed: int = 0) -> np.ndarray:
    """A voiced, gliding, amplitude-modulated signal with a little breath noise."""
    rng = np.random.default_rng(seed)
    t = np.arange(int(seconds * sr)) / sr
    f0 = 120 + 30 * np.sin(2 * np.pi * 0.5 * t)
    phase = 2 * np.pi * np.cumsum(f0) / sr
    voiced = sum(np.sin(k * phase) / k for k in range(1, 8))
    envelope = 0.5 + 0.5 * np.sin(2 * np.pi * 3 * t)
    return (0.3 * voiced * envelope + 0.01 * rng.standard_normal(t.size)).astype(np.float32)


#: Documented WSOLA parity tolerance (Task 5.1). The FFT correlation searches
#: the same window for the same normalized-correlation maximum as the old
#: direct convolution, so outputs match up to float rounding; the bound leaves
#: room only for that, never for a different segment choice.
WSOLA_PARITY_REL_RMS = 1e-4
STRETCH_RATES = (0.5, 0.8, 1.25, 2.0)


class TestWsolaParity:
    @pytest.mark.parametrize("rate", STRETCH_RATES)
    def test_matches_the_reference_within_tolerance(self, rate: float) -> None:
        signal = speechy(3.0)
        expected = reference_time_stretch(signal, rate)
        got = time_stretch_audio(signal, rate)
        assert got.dtype == np.float32
        assert got.size == expected.size == int(round(signal.size / rate))
        error = np.sqrt(np.mean((got - expected) ** 2)) / np.sqrt(np.mean(expected**2))
        assert error <= WSOLA_PARITY_REL_RMS

    @pytest.mark.parametrize("samples", [300, 1_000, 2_500, 4_800, 7_000])
    def test_short_inputs_match_the_reference(self, samples: int) -> None:
        # Short clips shrink the frame and clip the search window at both ends.
        signal = speechy(samples / 48_000, seed=samples)
        for rate in STRETCH_RATES:
            expected = reference_time_stretch(signal, rate)
            got = time_stretch_audio(signal, rate)
            assert got.size == expected.size
            assert np.allclose(got, expected, atol=1e-5)

    @pytest.mark.parametrize("rate", STRETCH_RATES)
    def test_chunk_joins_stay_click_free(self, rate: float) -> None:
        # The live worker stretches chunk by chunk and concatenates.
        signal = speechy(1.0, seed=7)
        half = signal.size // 2
        joined = np.concatenate(
            [time_stretch_audio(signal[:half], rate), time_stretch_audio(signal[half:], rate)]
        )
        reference = np.concatenate(
            [
                reference_time_stretch(signal[:half], rate),
                reference_time_stretch(signal[half:], rate),
            ]
        )
        assert joined.size == reference.size
        join = int(round(half / rate))
        assert abs(joined[join - 1]) < 1e-3 and abs(joined[join]) < 1e-3
        step = np.abs(np.diff(joined))
        # The join is no sharper than the audio itself.
        assert step[join - 3 : join + 3].max() <= step.max()
        assert np.allclose(joined, reference, atol=1e-5)


@pytest.mark.benchmark
def test_wsola_is_at_least_3x_faster_than_the_reference_on_60_s() -> None:
    signal = speechy(60.0)

    def best(fn, repeats: int = 2) -> float:
        timings = []
        for _ in range(repeats):
            start = time.perf_counter()
            fn(signal, 0.8)
            timings.append(time.perf_counter() - start)
        return min(timings)

    baseline = best(reference_time_stretch)
    current = best(time_stretch_audio)
    assert baseline / current >= 3.0, f"{baseline:.2f}s vs {current:.2f}s"


class TestExportWavFile:
    def test_export_content(self, tmp_path: Path) -> None:
        src = tmp_path / "source.wav"
        dst = tmp_path / "dest.wav"
        audio = tone(2400)
        write_wav_file(audio, src)  # float32 WAV
        assert sf.info(str(src)).subtype == "FLOAT"

        res = export_wav_file(src, dst)
        assert res == dst
        assert dst.is_file()

        info = sf.info(str(dst))
        assert info.format == "WAV"
        assert info.subtype == "PCM_16"
        assert info.samplerate == 48_000
        assert info.channels == 1
        assert info.frames == len(audio)

        # Verify RIFF WAVE header has wFormatTag == 1 (PCM)
        header = dst.read_bytes()[:44]
        assert header[:4] == b"RIFF"
        assert header[8:12] == b"WAVE"
        w_format_tag = int.from_bytes(header[20:22], "little")
        assert w_format_tag == 1  # 1 = WAVE_FORMAT_PCM

        src = tmp_path / "source.wav"
        dst = tmp_path / "dest.wav"
        audio = tone(4800)
        write_wav_file(audio, src)

        export_wav_file(src, dst)
        got_data, got_sr = read_wav(dst)
        assert got_sr == 48_000
        assert len(got_data) == len(audio)
        # 16-bit quantization noise is <= 1/32768 (~3e-5); atol=1e-3 is safe
        assert np.allclose(got_data, audio, atol=1e-3)

        src = tmp_path / "overshoot.wav"
        dst = tmp_path / "dest_clamped.wav"
        # Samples exceeding [-1.0, 1.0]
        audio = np.array([-1.2, -0.5, 0.0, 0.5, 1.2], dtype=np.float32)
        write_wav_file(audio, src)

        export_wav_file(src, dst)
        got, _ = read_wav(dst)
        assert got[0] <= -0.999
        assert got[-1] >= 0.999
        assert np.all(np.isfinite(got))

    def test_export_atomicity_and_failures(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src = tmp_path / "source.wav"
        dst = tmp_path / "dest.wav"
        write_wav_file(tone(1000), src)

        # Pre-existing destination
        dst.write_bytes(b"original preserved")

        # Simulate failure during SoundFile write
        orig_soundfile = sf.SoundFile

        def broken_soundfile(*args, **kwargs):
            obj = orig_soundfile(*args, **kwargs)
            if kwargs.get("mode") == "w":

                def boom(*_a, **_k):
                    raise OSError("Disk full simulation")

                obj.write = boom
            return obj

        monkeypatch.setattr(sf, "SoundFile", broken_soundfile)

        with pytest.raises(OSError, match="Disk full simulation"):
            export_wav_file(src, dst)

        # Pre-existing file was NOT overwritten or corrupted
        assert dst.read_bytes() == b"original preserved"
        # No leftover .part.wav files in directory
        parts = list(tmp_path.glob("*.part.wav"))
        assert parts == []

        monkeypatch.undo()
        src = tmp_path / "source.wav"
        dst = tmp_path / "dest.wav"
        write_wav_file(tone(1000), src)

        import os

        orig_replace = os.replace
        attempts = [0]

        def flaky_replace(source, target):
            attempts[0] += 1
            if attempts[0] < 3:
                raise PermissionError(32, "The process cannot access the file")
            return orig_replace(source, target)

        monkeypatch.setattr(os, "replace", flaky_replace)
        res = export_wav_file(src, dst)
        assert res == dst
        assert dst.is_file()
        assert attempts[0] == 3

        monkeypatch.undo()
        with pytest.raises(FileNotFoundError):
            export_wav_file(tmp_path / "does_not_exist.wav", tmp_path / "out.wav")


class TestExportAudioFile:
    def _source(self, tmp_path: Path) -> Path:
        src = tmp_path / "source.wav"
        write_wav_file(tone(4800), src)
        return src

    def test_mp3_export(self, tmp_path: Path) -> None:
        dest = export_audio_file(self._source(tmp_path), tmp_path / "out.mp3")
        assert dest.is_file()
        info = sf.info(str(dest))
        assert info.format == "MP3"
        assert info.subtype == "MPEG_LAYER_III"
        assert info.samplerate == 48_000
        assert info.frames > 0

        dest = export_audio_file(self._source(tmp_path), tmp_path / "OUT.MP3")
        assert sf.info(str(dest)).format == "MP3"
        dest = export_audio_file(self._source(tmp_path), tmp_path / "out.wav")
        assert sf.info(str(dest)).subtype == "PCM_16"
        dest = export_audio_file(self._source(tmp_path), tmp_path / "out")
        assert sf.info(str(dest)).subtype == "PCM_16"

    def test_export_failure_and_format(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            export_audio_file(tmp_path / "does_not_exist.wav", tmp_path / "out.mp3")
        assert list(tmp_path.glob("*.part.*")) == []
        assert not (tmp_path / "out.mp3").exists()

        assert export_format_for(tmp_path / "a.mp3") == "mp3"
        assert export_format_for(tmp_path / "A.MP3") == "mp3"
        assert export_format_for(tmp_path / "a.wav") == "wav"
        assert export_format_for(tmp_path / "a") == "wav"
        assert export_format_for(tmp_path / "a.ogg") == "wav"
