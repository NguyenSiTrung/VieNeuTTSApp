"""Audio helpers: float32@48k → WAV (bytes + file), read-back via soundfile."""

import io
import time
from pathlib import Path

import numpy as np
import pytest
import soundfile as sf
from tests.unit.wsola_reference import reference_time_stretch

from vienetts_app.core.audio import (
    ExportCancelled,
    apply_micro_fade,
    compute_waveform_envelope,
    compute_waveform_envelope_from_wav,
    encode_wav_bytes,
    export_audio_file,
    export_format_for,
    export_mp3_parts,
    export_wav_file,
    read_wav,
    time_stretch_audio,
    trim_speech_edges,
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

    def test_wav_duration_seconds_reads_header_only(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wav_1s = write_wav_file(tone(48_000, sr=48_000), tmp_path / "one.wav", sample_rate=48_000)
        wav_half = write_wav_file(
            tone(12_000, sr=24_000), tmp_path / "half.wav", sample_rate=24_000
        )

        def _forbid_decode(*_args, **_kwargs):
            raise AssertionError("wav_duration_seconds must not decode PCM samples")

        monkeypatch.setattr(sf, "read", _forbid_decode)
        monkeypatch.setattr(sf.SoundFile, "read", _forbid_decode)
        monkeypatch.setattr(sf.SoundFile, "buffer_read", _forbid_decode)

        assert wav_duration_seconds(wav_1s) == pytest.approx(1.0)
        assert wav_duration_seconds(wav_half) == pytest.approx(0.5)

        with pytest.raises(FileNotFoundError):
            wav_duration_seconds(tmp_path / "missing.wav")
        with pytest.raises(FileNotFoundError):
            wav_duration_seconds(tmp_path)


class TestValidation:
    @pytest.mark.parametrize(
        "bad",
        [
            np.array([], dtype=np.float32),  # empty
            np.zeros((100, 2), dtype=np.float32),  # stereo not produced by the SDK
        ],
    )
    def test_invalid_audio_raises(self, bad: np.ndarray, tmp_path: Path) -> None:
        with pytest.raises(ValueError):
            encode_wav_bytes(bad)

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

    @pytest.mark.parametrize("rate", [0.5, 0.75, 0.8])
    def test_slow_rates_preserve_continuous_tail_energy(self, rate: float) -> None:
        sr = 48_000
        for sig in (
            tone(sr, sr=sr),
            speechy(1.5, sr=sr),
            speechy(300 / sr, sr=sr, seed=300),
            speechy(7_000 / sr, sr=sr, seed=7_000),
        ):
            stretched = time_stretch_audio(sig, rate=rate, sample_rate=sr)
            assert stretched.dtype == np.float32
            assert stretched.size == int(round(sig.size / rate))
            assert np.all(np.isfinite(stretched))

            # Trailing zeros may only come from the 2 ms edge micro-fade endpoint.
            nz = np.flatnonzero(np.abs(stretched) > 1e-7)
            assert nz.size > 0
            tail_zeros = stretched.size - 1 - int(nz[-1])
            assert tail_zeros <= 4, f"rate={rate} n_in={sig.size} had {tail_zeros} trailing zeros"

            # Final 15% before the micro-fade must retain strong continuous energy
            # (no 50 ms or ~40-50% zero-padded tail truncation).
            fade_len = min(stretched.size // 4, int(sr * 0.002))
            tail_start = max(fade_len, int(stretched.size * 0.85))
            tail_end = stretched.size - fade_len
            if tail_end > tail_start:
                tail_rms = float(np.sqrt(np.mean(stretched[tail_start:tail_end] ** 2)))
                overall_rms = float(np.sqrt(np.mean(stretched**2)))
                assert tail_rms > 0.1 * overall_rms


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
    @staticmethod
    def _reference_non_truncated_end(expected: np.ndarray, n_in: int, sr: int = 48_000) -> int:
        """Index up to which the frozen pre-fix reference did not break early.

        ``wsola_reference.reference_time_stretch`` keeps the legacy early-break
        at ``target_pos + frame_len > n_in`` (leaving trailing zeros for slow
        rates). Everything before the last reference frame's overlap region must
        still match the oracle bit-for-bit within float rounding.
        """
        frame_len = int(sr * 0.025)
        if frame_len % 2 != 0:
            frame_len += 1
        if n_in < frame_len * 2:
            frame_len = max(32, (n_in // 3) & ~1)
        nz = np.flatnonzero(np.abs(expected) > 1e-9)
        if nz.size == 0:
            return expected.size
        last_nz = int(nz[-1])
        tail_zeros = expected.size - 1 - last_nz
        if tail_zeros <= 4:
            return expected.size
        return max(0, last_nz + 1 - frame_len)

    @pytest.mark.parametrize("rate", [0.8, 1.25])
    def test_matches_the_reference_within_tolerance(self, rate: float) -> None:
        signal = speechy(1.5)
        expected = reference_time_stretch(signal, rate)
        got = time_stretch_audio(signal, rate)
        assert got.dtype == np.float32
        assert got.size == expected.size == int(round(signal.size / rate))
        prefix = self._reference_non_truncated_end(expected, signal.size)
        error = np.sqrt(np.mean((got[:prefix] - expected[:prefix]) ** 2)) / np.sqrt(
            np.mean(expected[:prefix] ** 2)
        )
        assert error <= WSOLA_PARITY_REL_RMS

    @pytest.mark.parametrize("samples", [300, 7_000])
    def test_short_inputs_match_the_reference(self, samples: int) -> None:
        # Short clips shrink the frame and clip the search window at both ends.
        signal = speechy(samples / 48_000, seed=samples)
        for rate in STRETCH_RATES:
            expected = reference_time_stretch(signal, rate)
            got = time_stretch_audio(signal, rate)
            assert got.size == expected.size
            if rate >= 1.0:
                assert np.allclose(got, expected, atol=1e-5)
            else:
                prefix = self._reference_non_truncated_end(expected, signal.size)
                assert prefix > 0
                assert np.allclose(got[:prefix], expected[:prefix], atol=1e-5)

    @pytest.mark.parametrize("rate", [1.25])
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

    def test_long_cjk_filename_uses_short_uuid_temp_path(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        src = tmp_path / "source.wav"
        write_wav_file(tone(1_200), src)
        # 80 CJK chars = 240 UTF-8 bytes; + ".wav" = 244 bytes (valid POSIX NAME_MAX <= 255),
        # whereas embedding the stem in ".<stem>.<uuid>.part.wav" overflows 255 bytes.
        cjk_stem = "漢" * 80
        dst = tmp_path / f"{cjk_stem}.wav"

        opened_write_names: list[str] = []
        orig_soundfile = sf.SoundFile

        def _spy_soundfile(path, *args, **kwargs):
            if kwargs.get("mode") == "w":
                opened_write_names.append(Path(path).name)
            return orig_soundfile(path, *args, **kwargs)

        monkeypatch.setattr(sf, "SoundFile", _spy_soundfile)
        out = export_wav_file(src, dst)
        assert out == dst
        assert dst.is_file()
        assert len(opened_write_names) == 1
        part_name = opened_write_names[0]
        assert part_name.endswith(".part.wav")
        assert len(part_name.encode("utf-8")) < 64
        assert cjk_stem not in part_name


class TestExportAudioFile:
    def _source(self, tmp_path: Path) -> Path:
        src = tmp_path / "source.wav"
        write_wav_file(tone(4800), src)
        return src

    def test_export_formats_failures_and_format_mapping(self, tmp_path: Path) -> None:
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

        # A missing source fails without leaving a partial dest behind.
        with pytest.raises(FileNotFoundError):
            export_audio_file(tmp_path / "does_not_exist.wav", tmp_path / "bad.mp3")
        assert list(tmp_path.glob("*.part.*")) == []
        assert not (tmp_path / "bad.mp3").exists()

        assert export_format_for(tmp_path / "a.mp3") == "mp3"
        assert export_format_for(tmp_path / "A.MP3") == "mp3"
        assert export_format_for(tmp_path / "a.wav") == "wav"
        assert export_format_for(tmp_path / "a") == "wav"
        assert export_format_for(tmp_path / "a.ogg") == "wav"


def _minimal_id3v24_tag(title: str = "Chapter 1") -> bytes:
    """Build a valid minimal ID3v2.4 header containing a single UTF-8 TIT2 frame."""
    payload = b"\x03" + title.encode("utf-8")

    def _syncsafe(n: int) -> bytes:
        return bytes([(n >> 21) & 0x7F, (n >> 14) & 0x7F, (n >> 7) & 0x7F, n & 0x7F])

    frame = b"TIT2" + _syncsafe(len(payload)) + b"\x00\x00" + payload
    return b"ID3\x04\x00\x00" + _syncsafe(len(frame)) + frame


class TestExportMp3Parts:
    def test_concatenates_parts_with_id3_tag_and_cleans_temps(self, tmp_path: Path) -> None:
        wav1 = write_wav_file(tone(24_000, freq=440.0, sr=48_000), tmp_path / "part1.wav")
        wav2 = write_wav_file(tone(36_000, freq=660.0, sr=48_000), tmp_path / "part2.wav")
        dest = tmp_path / "out" / "book.mp3"
        tag = _minimal_id3v24_tag("Part One")

        result = export_mp3_parts([wav1, wav2], dest, id3_bytes=tag)

        assert result == dest
        assert dest.is_file()
        raw_bytes = dest.read_bytes()
        assert raw_bytes.startswith(tag)

        info = sf.info(str(dest))
        assert info.format == "MP3"
        assert info.subtype == "MPEG_LAYER_III"
        assert info.samplerate == 48_000
        assert info.channels == 1
        expected_dur = wav_duration_seconds(wav1) + wav_duration_seconds(wav2)
        assert info.duration == pytest.approx(expected_dur, abs=0.15)
        assert list(dest.parent.glob("*.part.mp3")) == []

    def test_without_id3_and_empty_id3_bytes(self, tmp_path: Path) -> None:
        wav1 = write_wav_file(tone(12_000), tmp_path / "p1.wav")
        wav2 = write_wav_file(tone(12_000), tmp_path / "p2.wav")

        for name, id3_bytes in [("none.mp3", None), ("empty.mp3", b"")]:
            dest = tmp_path / name
            result = export_mp3_parts([wav1, wav2], dest, id3_bytes=id3_bytes)
            assert result == dest
            assert not dest.read_bytes().startswith(b"ID3")
            info = sf.info(str(dest))
            assert info.format == "MP3"
            assert info.duration == pytest.approx(0.5, abs=0.15)
        assert list(tmp_path.glob("*.part.mp3")) == []

    def test_validation_errors_and_mismatches_leave_no_temps(self, tmp_path: Path) -> None:
        wav_48k_mono = write_wav_file(tone(12_000, sr=48_000), tmp_path / "m48.wav")
        wav_24k_mono = write_wav_file(
            tone(6_000, sr=24_000), tmp_path / "m24.wav", sample_rate=24_000
        )
        wav_48k_stereo = tmp_path / "s48.wav"
        stereo = np.column_stack([tone(12_000, sr=48_000), tone(12_000, sr=48_000)])
        sf.write(str(wav_48k_stereo), stereo, 48_000, subtype="FLOAT")

        with pytest.raises(ValueError, match="sources"):
            export_mp3_parts([], tmp_path / "empty.mp3")

        with pytest.raises(ValueError, match="block_frames"):
            export_mp3_parts([wav_48k_mono], tmp_path / "bad_block.mp3", block_frames=0)

        with pytest.raises(FileNotFoundError):
            export_mp3_parts([wav_48k_mono, tmp_path / "missing.wav"], tmp_path / "missing.mp3")

        dest_sr = tmp_path / "mismatch_sr.mp3"
        dest_sr.write_bytes(b"keep-existing")
        with pytest.raises(ValueError, match="sample rate"):
            export_mp3_parts([wav_48k_mono, wav_24k_mono], dest_sr)
        assert dest_sr.read_bytes() == b"keep-existing"
        assert list(tmp_path.glob("*.part.mp3")) == []

        dest_ch = tmp_path / "mismatch_ch.mp3"
        with pytest.raises(ValueError, match="channel"):
            export_mp3_parts([wav_48k_mono, wav_48k_stereo], dest_ch)
        assert not dest_ch.exists()
        assert list(tmp_path.glob("*.part.mp3")) == []

    def test_cancel_before_start_mid_stream_and_during_id3_copy(self, tmp_path: Path) -> None:
        # ~9 s total so raw MP3 (~84 KiB) exceeds one 64 KiB ID3 copy chunk.
        wav1 = write_wav_file(speechy(4.5, seed=1), tmp_path / "c1.wav")
        wav2 = write_wav_file(speechy(4.5, seed=2), tmp_path / "c2.wav")
        tag = _minimal_id3v24_tag("Cancelled Book")

        # 1. Cancel immediately before starting
        dest_pre = tmp_path / "pre_cancel.mp3"
        with pytest.raises(ExportCancelled):
            export_mp3_parts([wav1, wav2], dest_pre, id3_bytes=tag, cancel_check=lambda: True)
        assert not dest_pre.exists()
        assert list(tmp_path.glob("*.part.mp3")) == []

        # 2. Cancel mid-stream during PCM -> MP3 block encoding
        dest_mid = tmp_path / "mid_cancel.mp3"
        dest_mid.write_bytes(b"untouched")
        checks = 0

        def cancel_after_two_checks() -> bool:
            nonlocal checks
            checks += 1
            return checks >= 3

        with pytest.raises(ExportCancelled):
            export_mp3_parts(
                [wav1, wav2],
                dest_mid,
                id3_bytes=tag,
                block_frames=1024,
                cancel_check=cancel_after_two_checks,
            )
        assert dest_mid.read_bytes() == b"untouched"
        assert list(tmp_path.glob("*.part.mp3")) == []

        # 3. Cancel mid-copy during the ID3v2.4 prepend streaming stage
        dest_id3 = tmp_path / "id3_cancel.mp3"
        tagged_polls = 0
        observed_partial_bytes = 0

        def cancel_mid_id3_copy() -> bool:
            nonlocal tagged_polls, observed_partial_bytes
            tagged_files = list(tmp_path.glob("*.tagged.part.mp3"))
            if tagged_files:
                tagged_polls += 1
                if tagged_polls >= 2:
                    observed_partial_bytes = tagged_files[0].stat().st_size
                    return True
            return False

        with pytest.raises(ExportCancelled):
            export_mp3_parts(
                [wav1, wav2],
                dest_id3,
                id3_bytes=tag,
                cancel_check=cancel_mid_id3_copy,
            )
        assert tagged_polls >= 2
        assert observed_partial_bytes >= 65_536
        assert not dest_id3.exists()
        assert list(tmp_path.glob("*.part.mp3")) == []

    def test_respects_bounded_block_frames_across_multiple_parts(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wav1 = write_wav_file(tone(5_000, freq=330.0), tmp_path / "b1.wav")
        wav2 = write_wav_file(tone(4_500, freq=550.0), tmp_path / "b2.wav")
        dest = tmp_path / "bounded.mp3"
        block_frames = 1024

        read_sizes: list[int] = []
        write_lengths: list[int] = []
        orig_soundfile = sf.SoundFile

        def spying_soundfile(*args, **kwargs):
            handle = orig_soundfile(*args, **kwargs)
            mode = kwargs.get("mode", "r")
            if mode == "r":
                orig_read = handle.read

                def tracked_read(frames=-1, *a, **kw):
                    read_sizes.append(frames)
                    return orig_read(frames, *a, **kw)

                handle.read = tracked_read
            elif mode == "w":
                orig_write = handle.write

                def tracked_write(data):
                    write_lengths.append(len(data))
                    return orig_write(data)

                handle.write = tracked_write
            return handle

        monkeypatch.setattr(sf, "SoundFile", spying_soundfile)

        result = export_mp3_parts([wav1, wav2], dest, block_frames=block_frames)

        assert result == dest
        assert len(read_sizes) > 2
        assert all(size == block_frames for size in read_sizes)
        assert len(write_lengths) >= 10  # ceil(5000/1024)=5 + ceil(4500/1024)=5
        assert all(length <= block_frames for length in write_lengths)
        assert sum(write_lengths) == 9_500

        info = sf.info(str(dest))
        assert info.format == "MP3"
        assert info.duration == pytest.approx(9_500 / 48_000, abs=0.15)
        assert list(tmp_path.glob("*.part.mp3")) == []

    def test_long_cjk_filename_uses_short_uuid_temp_paths_with_id3(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        wav1 = write_wav_file(tone(4_800), tmp_path / "p1.wav")
        cjk_stem = "漢" * 80
        dest = tmp_path / f"{cjk_stem}.mp3"
        tag = _minimal_id3v24_tag("CJK Book")

        opened_write_names: list[str] = []
        orig_soundfile = sf.SoundFile

        def _spy_soundfile(path, *args, **kwargs):
            if kwargs.get("mode") == "w":
                opened_write_names.append(Path(path).name)
            return orig_soundfile(path, *args, **kwargs)

        import os

        replaced_src_names: list[str] = []
        orig_replace = os.replace

        def _spy_replace(src, dst):
            replaced_src_names.append(Path(src).name)
            return orig_replace(src, dst)

        monkeypatch.setattr(sf, "SoundFile", _spy_soundfile)
        monkeypatch.setattr(os, "replace", _spy_replace)

        out = export_mp3_parts([wav1], dest, id3_bytes=tag)
        assert out == dest
        assert dest.is_file()
        assert dest.read_bytes().startswith(tag)
        assert len(opened_write_names) == 1
        assert opened_write_names[0].endswith(".part.mp3")
        assert len(opened_write_names[0].encode("utf-8")) < 64
        assert cjk_stem not in opened_write_names[0]
        assert len(replaced_src_names) == 1
        assert replaced_src_names[0].endswith(".tagged.part.mp3")
        assert len(replaced_src_names[0].encode("utf-8")) < 64
        assert cjk_stem not in replaced_src_names[0]


class TestTrimSpeechEdges:
    def test_strips_leading_and_trailing_silence_keeping_pad_guard(self) -> None:
        sr = 48_000
        leading = np.zeros(4_800, dtype=np.float32)  # 100 ms silence
        active = tone(9_600, sr=sr)  # 200 ms speech
        trailing = np.zeros(7_200, dtype=np.float32)  # 150 ms silence
        clip = np.concatenate([leading, active, trailing])

        trimmed = trim_speech_edges(clip, sample_rate=sr, threshold_db=-45.0, pad_ms=15)
        pad_frames = int(round(sr * 0.015))  # 720 frames
        first_active = int(np.flatnonzero(np.abs(active) > 10.0 ** (-45.0 / 20.0))[0])
        last_active = int(np.flatnonzero(np.abs(active) > 10.0 ** (-45.0 / 20.0))[-1])
        expected_start = 4_800 + first_active - pad_frames
        expected_end = 4_800 + last_active + 1 + pad_frames

        assert trimmed.dtype == np.float32
        assert trimmed.flags["C_CONTIGUOUS"]
        assert np.array_equal(trimmed, clip[expected_start:expected_end])

    def test_preserves_empty_all_silent_and_already_tight_clips(self) -> None:
        empty = np.array([], dtype=np.float32)
        got_empty = trim_speech_edges(empty)
        assert got_empty.dtype == np.float32
        assert got_empty.size == 0

        # Sub-threshold noise (-50 dB < -45 dB threshold) stays untouched
        sub_threshold = np.full(1_200, 10.0 ** (-50.0 / 20.0), dtype=np.float32)
        got_silent = trim_speech_edges(sub_threshold, threshold_db=-45.0)
        assert got_silent.dtype == np.float32
        assert np.array_equal(got_silent, sub_threshold)

        # Already-tight clip clamps pad guard cleanly to [0, size]
        tight = np.full(2_400, 0.25, dtype=np.float32)
        got_tight = trim_speech_edges(tight, pad_ms=20)
        assert got_tight.dtype == np.float32
        assert np.array_equal(got_tight, tight)

        # Float64 input converts to contiguous float32
        f64 = np.concatenate([np.zeros(500), np.full(400, 0.5), np.zeros(500)])
        got_f64 = trim_speech_edges(f64, sample_rate=10_000, pad_ms=10)
        assert got_f64.dtype == np.float32
        assert got_f64.size == 400 + 2 * 100

    def test_validation_rejects_invalid_inputs(self) -> None:
        with pytest.raises(ValueError, match="1-D mono"):
            trim_speech_edges(np.zeros((10, 2), dtype=np.float32))
        with pytest.raises(ValueError, match="sample_rate"):
            trim_speech_edges(np.zeros(10, dtype=np.float32), sample_rate=0)

    def test_block_wise_scan_matches_reference_without_full_array_abs(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        sr = 48_000
        threshold_db = -45.0
        pad_ms = 15
        threshold = float(10.0 ** (threshold_db / 20.0))
        pad_frames = int(round(sr * (pad_ms / 1000.0)))

        def _reference_trim(arr: np.ndarray) -> np.ndarray:
            mono = np.ascontiguousarray(arr, dtype=np.float32)
            if mono.size == 0:
                return mono
            active = np.flatnonzero(np.abs(mono) > threshold)
            if active.size == 0:
                return mono
            start = max(0, int(active[0]) - pad_frames)
            end = min(mono.size, int(active[-1]) + 1 + pad_frames)
            return np.ascontiguousarray(mono[start:end], dtype=np.float32)

        # Multi-block clips: interior speech, block-boundary spikes, single-sample edges,
        # NaN alongside active sample in the same block, and multi-block all-silence.
        cases: list[np.ndarray] = [
            np.concatenate(
                [
                    np.zeros(25_000, dtype=np.float32),
                    tone(40_000, sr=sr),
                    np.zeros(35_000, dtype=np.float32),
                ]
            ),
            np.zeros(50_000, dtype=np.float32),
        ]
        # Exact boundary indices around 16_384
        for idx_pair in [(0, 0), (49_999, 49_999), (16_383, 16_384), (16_384, 32_768)]:
            buf = np.zeros(50_000, dtype=np.float32)
            buf[idx_pair[0]] = 0.2
            buf[idx_pair[1]] = -0.2
            cases.append(buf)
        # NaN in the same block before an active sample must not mask the active sample
        nan_buf = np.zeros(40_000, dtype=np.float32)
        nan_buf[10] = np.nan
        nan_buf[50] = 0.5
        nan_buf[39_900] = -0.5
        nan_buf[39_950] = np.nan
        cases.append(nan_buf)

        expected_results = [_reference_trim(c) for c in cases]

        abs_sizes: list[int] = []
        real_abs = np.abs

        def _spying_abs(arr, *args, **kwargs):
            if isinstance(arr, np.ndarray):
                abs_sizes.append(arr.size)
            return real_abs(arr, *args, **kwargs)

        monkeypatch.setattr(np, "abs", _spying_abs)
        for case, expected in zip(cases, expected_results, strict=True):
            got = trim_speech_edges(case, sample_rate=sr, threshold_db=threshold_db, pad_ms=pad_ms)
            assert got.dtype == np.float32
            assert got.flags["C_CONTIGUOUS"]
            assert np.array_equal(got, expected, equal_nan=True)

        assert abs_sizes, "expected block-wise scan to inspect chunks"
        assert max(abs_sizes) <= 16_384, f"allocated full-array abs of size {max(abs_sizes)}"


class TestApplyMicroFade:
    def test_half_cosine_taper_start_end_and_interior_preservation(self) -> None:
        sr = 48_000
        fade_ms = 5.0
        fade_len = int(round(sr * (fade_ms / 1000.0)))  # 240 samples
        orig = np.ones(2_000, dtype=np.float32)

        both = apply_micro_fade(orig, sample_rate=sr, fade_ms=fade_ms)
        assert both.dtype == np.float32
        assert both.flags["C_CONTIGUOUS"]
        # Does not mutate input array
        assert np.all(orig == 1.0)

        expected_taper = 0.5 * (1.0 - np.cos(np.linspace(0.0, np.pi, fade_len, dtype=np.float32)))
        assert np.allclose(both[:fade_len], expected_taper, atol=1e-6)
        assert np.allclose(both[-fade_len:], expected_taper[::-1], atol=1e-6)
        assert np.array_equal(both[fade_len:-fade_len], orig[fade_len:-fade_len])

        only_in = apply_micro_fade(
            orig, sample_rate=sr, fade_ms=fade_ms, fade_in=True, fade_out=False
        )
        assert np.allclose(only_in[:fade_len], expected_taper, atol=1e-6)
        assert np.array_equal(only_in[fade_len:], orig[fade_len:])

        only_out = apply_micro_fade(
            orig, sample_rate=sr, fade_ms=fade_ms, fade_in=False, fade_out=True
        )
        assert np.array_equal(only_out[:-fade_len], orig[:-fade_len])
        assert np.allclose(only_out[-fade_len:], expected_taper[::-1], atol=1e-6)

        neither = apply_micro_fade(
            orig, sample_rate=sr, fade_ms=fade_ms, fade_in=False, fade_out=False
        )
        assert np.array_equal(neither, orig)
        assert neither is not orig

    def test_short_and_empty_array_safety(self) -> None:
        for size in (0, 1, 2, 3):
            short = np.ones(size, dtype=np.float32)
            got = apply_micro_fade(short, sample_rate=48_000, fade_ms=5.0)
            assert got.dtype == np.float32
            assert np.array_equal(got, short)
            assert got is not short

        # fade_ms = 0 -> fade_len <= 1 -> untouched copy
        clip = np.ones(100, dtype=np.float32)
        got_zero = apply_micro_fade(clip, sample_rate=48_000, fade_ms=0.0)
        assert np.array_equal(got_zero, clip)
        assert got_zero is not clip

        # Clamps fade_len to mono.size // 2 when clip is shorter than 2 * fade_ms
        small = np.ones(20, dtype=np.float32)
        got_small = apply_micro_fade(small, sample_rate=48_000, fade_ms=5.0)
        assert got_small.size == 20
        assert got_small[0] == pytest.approx(0.0, abs=1e-6)
        assert got_small[-1] == pytest.approx(0.0, abs=1e-6)
        assert got_small[9] == pytest.approx(1.0, abs=1e-6)
        assert got_small[10] == pytest.approx(1.0, abs=1e-6)

    def test_validation_rejects_invalid_inputs(self) -> None:
        with pytest.raises(ValueError, match="1-D mono"):
            apply_micro_fade(np.zeros((10, 2), dtype=np.float32))
        with pytest.raises(ValueError, match="sample_rate"):
            apply_micro_fade(np.zeros(10, dtype=np.float32), sample_rate=-1)
