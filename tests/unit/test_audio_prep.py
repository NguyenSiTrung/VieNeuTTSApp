"""Shared 16 kHz prep (``core/audio_prep.py``): one anti-aliased, streamed conversion per media.

ASR and diarization used to convert the same WAV twice (``input_16k.wav`` by
whole-file linear interpolation, ``source_wav_16k.wav`` via ffmpeg). Both now
resolve one ``source_16k.wav`` + sidecar per media cache dir.
"""

from __future__ import annotations

import json
import os
import tracemalloc
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest
import soundfile as sf

from vienetts_app.core import asr, audio_prep, diarization, video_dub
from vienetts_app.core.asr import AsrConfig, RawSegment, transcribe_media_to_cues
from vienetts_app.core.audio_prep import AudioPrepCancelled, ensure_source_16k
from vienetts_app.core.diarization import DiarizationConfig, diarize_cues


def _spy(monkeypatch: pytest.MonkeyPatch, name: str) -> list[Path]:
    """Record the source path of every call to the ``audio_prep`` converter ``name``."""
    calls: list[Path] = []
    real = getattr(audio_prep, name)

    def spy(src: Any, *args: Any, **kwargs: Any) -> Any:
        calls.append(Path(src))
        return real(src, *args, **kwargs)

    monkeypatch.setattr(audio_prep, name, spy)
    return calls


def _write_stereo(path: Path, sr: int, seconds: float) -> Path:
    t = np.arange(int(sr * seconds)) / sr
    tone = (0.3 * np.sin(2 * np.pi * 440.0 * t)).astype(np.float32)
    sf.write(str(path), np.column_stack([tone, -0.5 * tone]), sr, subtype="PCM_16")
    return path


def test_wav_is_converted_once_and_shared_by_asr_then_diarization(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _write_stereo(tmp_path / "talk.wav", 44_100, 2.0)
    resampled = _spy(monkeypatch, "_resample_to_16k")
    extracted = _spy(monkeypatch, "extract_video_audio")
    seen: list[Path] = []

    def fake_whisper(wav_path: Path, config: AsrConfig, **kwargs: Any) -> list[RawSegment]:
        seen.append(wav_path)
        return [RawSegment(0, 1500, "Xin chào các bạn.")]

    def fake_acoustic(wav_path: Path, cues: Any, config: Any, **kwargs: Any) -> list[Any]:
        seen.append(wav_path)
        return [replace(c, speaker="SPEAKER_01") for c in cues]

    monkeypatch.setattr(asr, "_run_faster_whisper", fake_whisper)
    monkeypatch.setattr(diarization, "_run_acoustic_diarization", fake_acoustic)
    work = tmp_path / "cache"
    cues = transcribe_media_to_cues(src, AsrConfig(backend="faster_whisper"), work_dir=work)
    tagged = diarize_cues(src, cues, DiarizationConfig(backend="acoustic"), work_dir=work)

    shared = work / "source_16k.wav"
    assert [c.speaker for c in tagged] == ["SPEAKER_01"]
    assert seen == [shared, shared]
    assert resampled == [src] and extracted == []
    info = sf.info(str(shared))
    assert (info.samplerate, info.channels, info.subtype, info.frames) == (
        16_000,
        1,
        "PCM_16",
        32_000,
    )
    stored = json.loads((work / "source_16k.wav.json").read_text(encoding="utf-8"))
    stat = src.stat()
    assert stored["source"] == str(src.resolve())
    assert (stored["size"], stored["mtime_ns"]) == (stat.st_size, stat.st_mtime_ns)
    assert stored["format"] == audio_prep.SIDECAR_FORMAT
    assert sorted(p.name for p in work.iterdir()) == [
        "asr_segments_cache.json",
        "source_16k.wav",
        "source_16k.wav.json",
    ]

    # An already-16 kHz mono WAV is used in place: nothing is written beside it or cached.
    mono16 = tmp_path / "user" / "voice.wav"
    mono16.parent.mkdir()
    sf.write(str(mono16), np.zeros(16_000, dtype=np.float32), 16_000, subtype="PCM_16")
    assert ensure_source_16k(mono16, tmp_path / "cache16") == mono16
    assert [p.name for p in mono16.parent.iterdir()] == ["voice.wav"]
    assert not (tmp_path / "cache16").exists()
    assert len(resampled) == 1


@pytest.mark.parametrize("legacy", ["input_16k.wav", "source_wav_16k.wav"])
def test_legacy_16k_wav_is_adopted_and_stale_sidecars_reconvert(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy: str
) -> None:
    src = _write_stereo(tmp_path / "talk.wav", 48_000, 1.0)
    stat = src.stat()
    identity = {"source": str(src.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}
    work = tmp_path / "cache"
    work.mkdir()
    legacy_wav = work / legacy
    sf.write(str(legacy_wav), np.full(16_000, 0.1, dtype=np.float32), 16_000, subtype="PCM_16")
    # Pre-versioning sidecar layout, written by the old ASR / diarization helpers.
    (work / f"{legacy}.json").write_text(json.dumps(identity), encoding="utf-8")
    other = next(n for n in audio_prep.LEGACY_16K_FILENAMES if n != legacy)
    sf.write(str(work / other), np.zeros(16_000, dtype=np.float32), 16_000, subtype="PCM_16")
    (work / f"{other}.json").write_text(json.dumps({**identity, "size": 1}), encoding="utf-8")
    stamp = legacy_wav.stat().st_mtime_ns
    resampled = _spy(monkeypatch, "_resample_to_16k")
    extracted = _spy(monkeypatch, "extract_video_audio")

    shared = ensure_source_16k(src, work)

    assert shared == work / "source_16k.wav"
    assert resampled == [] and extracted == []
    assert shared.stat().st_mtime_ns == stamp  # renamed into place, never rewritten
    assert sorted(p.name for p in work.iterdir()) == ["source_16k.wav", "source_16k.wav.json"]
    assert ensure_source_16k(src, work) == shared and resampled == []

    # A sidecar from another converter version is never misread: it reconverts once.
    sidecar = work / "source_16k.wav.json"
    sidecar.write_text(json.dumps({**identity, "format": 999}), encoding="utf-8")
    ensure_source_16k(src, work)
    ensure_source_16k(src, work)
    assert len(resampled) == 1
    # A changed source invalidates the shared WAV as well.
    sf.write(str(src), np.zeros((96_000, 2), dtype=np.float32), 48_000, subtype="PCM_16")
    ensure_source_16k(src, work)
    assert len(resampled) == 2
    assert sf.info(str(shared)).frames == 32_000


def test_resampling_is_anti_aliased(tmp_path: Path) -> None:
    sr = 48_000
    t = np.arange(sr * 2) / sr
    src = tmp_path / "tone10k.wav"
    sf.write(str(src), 0.5 * np.sin(2 * np.pi * 10_000.0 * t), sr, subtype="FLOAT")

    out = ensure_source_16k(src, tmp_path / "cache")

    samples, rate = sf.read(str(out), dtype="float64")
    assert rate == 16_000
    body = samples[4_000:-4_000]
    window = np.hanning(body.size)
    spectrum = np.abs(np.fft.rfft(body * window))
    freqs = np.fft.rfftfreq(body.size, 1.0 / rate)
    alias = spectrum[np.abs(freqs - 6_000.0) < 50.0].max()
    # The level the 0.5-amplitude tone would show had it been in band.
    reference = 0.5 * window.sum() / 2.0
    assert 20.0 * np.log10(alias / reference) < -40.0


def test_conversion_reads_bounded_blocks_and_cancels_between_them(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(audio_prep, "_BLOCK_FRAMES", 4_096)
    src = _write_stereo(tmp_path / "talk.wav", 44_100, 2.0)
    reads: list[int] = []
    real_read = sf.SoundFile.read

    def read_spy(self: sf.SoundFile, frames: int = -1, *args: Any, **kwargs: Any) -> Any:
        reads.append(int(frames))
        return real_read(self, frames, *args, **kwargs)

    monkeypatch.setattr(sf.SoundFile, "read", read_spy)
    progress: list[float] = []

    out = ensure_source_16k(src, tmp_path / "cache", progress_cb=progress.append)

    assert sf.info(str(out)).frames == 32_000
    assert len(reads) == -(-88_200 // 4_096) and 0 < min(reads) <= max(reads) <= 4_096
    assert progress[0] == 0.0 and progress[-1] == 1.0 and progress == sorted(progress)

    cancelled = tmp_path / "cancelled"
    reads.clear()
    with pytest.raises(AudioPrepCancelled):
        ensure_source_16k(src, cancelled, cancel_check=lambda: len(reads) >= 3)
    assert len(reads) == 3
    assert list(cancelled.iterdir()) == []  # no WAV, no sidecar, no .part file


def test_twenty_minute_conversion_stays_under_64_mb(tmp_path: Path) -> None:
    sr = 48_000
    src = tmp_path / "long.wav"
    block = (np.random.default_rng(7).standard_normal(sr * 10) * 0.1).astype(np.float32)
    with sf.SoundFile(str(src), "w", samplerate=sr, channels=1, subtype="PCM_16") as fh:
        for _ in range(120):  # 20 minutes, written in 10 s blocks
            fh.write(block)

    tracemalloc.start()
    try:
        out = ensure_source_16k(src, tmp_path / "cache")
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    assert peak < 64 * 1024 * 1024
    assert sf.info(str(out)).frames == 20 * 60 * 16_000


def test_video_inputs_are_extracted_by_ffmpeg_through_run_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    bin_dir = data_dir / "runtimes" / "ffmpeg" / "bin"
    bin_dir.mkdir(parents=True)
    ffmpeg = bin_dir / "ffmpeg"
    ffmpeg.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, wave\n"
        "with wave.open(sys.argv[-1], 'wb') as out:\n"
        "    out.setnchannels(1)\n"
        "    out.setsampwidth(2)\n"
        "    out.setframerate(16000)\n"
        "    out.writeframes(b'\\x00\\x00' * 1600)\n",
        encoding="utf-8",
    )
    ffprobe = bin_dir / "ffprobe"
    ffprobe.write_text("#!/usr/bin/env python3\nprint('10.000000')\n", encoding="utf-8")
    for stub in (ffmpeg, ffprobe):
        stub.chmod(0o755)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"not decoded by the stub")
    commands: list[list[str]] = []
    real_run_cli = video_dub.run_cli

    def run_cli_spy(cmd: list[str], **kwargs: Any) -> Any:
        commands.append([str(c) for c in cmd])
        return real_run_cli(cmd, **kwargs)

    monkeypatch.setattr(video_dub, "run_cli", run_cli_spy)
    resampled = _spy(monkeypatch, "_resample_to_16k")
    work = tmp_path / "cache"

    out = ensure_source_16k(video, work, data_dir=data_dir)
    again = ensure_source_16k(video, work, data_dir=data_dir)

    assert out == again == work / "source_16k.wav"
    extractions = [c for c in commands if "-ar" in c]
    assert len(extractions) == 1 and resampled == []
    args = extractions[0]
    assert args[args.index("-ar") + 1] == "16000" and args[args.index("-ac") + 1] == "1"
    assert sf.info(str(out)).frames == 1_600
    assert json.loads((work / "source_16k.wav.json").read_text(encoding="utf-8"))["size"] == (
        os.path.getsize(video)
    )
    assert sorted(p.name for p in work.iterdir()) == ["source_16k.wav", "source_16k.wav.json"]
