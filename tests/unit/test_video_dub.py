"""Unit tests for core/video_dub.py: companion discovery, extraction, and stream-copy muxing."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import numpy as np
import pytest

from vienetts_app.core.audio import read_wav, write_wav_file
from vienetts_app.core.video_dub import (
    VideoDubError,
    extract_video_audio,
    find_companion_srt,
    find_companion_video,
    is_video_path,
    mux_dubbed_video,
    probe_has_audio_stream,
    resolve_ffmpeg,
)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")


def _make_test_video(path: Path, *, duration_s: float = 1.2, with_audio: bool = True) -> Path:
    ffmpeg = str(resolve_ffmpeg())
    cmd = [
        ffmpeg,
        "-nostdin",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s=160x90:r=15:d={duration_s}",
    ]
    if with_audio:
        cmd += [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=16000:duration={duration_s}",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-shortest",
            str(path),
        ]
    else:
        cmd += [
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(path),
        ]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


def test_companion_discovery_and_video_path_detection(tmp_path: Path) -> None:
    assert is_video_path("clip.mp4") is True
    assert is_video_path("clip.MKV") is True
    assert is_video_path("clip.srt") is False
    assert is_video_path("clip.wav") is False

    video = tmp_path / "lesson.mp4"
    video.write_bytes(b"fake")
    assert find_companion_srt(video) is None

    lang_srt = tmp_path / "lesson.vi.srt"
    lang_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nXin chào\n", encoding="utf-8")
    assert find_companion_srt(video) == lang_srt

    exact_srt = tmp_path / "lesson.srt"
    exact_srt.write_text("1\n00:00:00,000 --> 00:00:01,000\nChính xác\n", encoding="utf-8")
    assert find_companion_srt(video) == exact_srt
    assert find_companion_video(exact_srt) == video
    assert find_companion_video(lang_srt) == video


def test_extract_video_audio_full_and_sliced(tmp_path: Path) -> None:
    video = _make_test_video(tmp_path / "source.mp4", duration_s=1.5, with_audio=True)
    assert probe_has_audio_stream(video) is True

    full_wav = extract_video_audio(video, tmp_path / "full.wav", sample_rate=16_000)
    samples, sr = read_wav(full_wav)
    assert sr == 16_000
    assert 20_000 <= samples.size <= 28_000

    sliced_wav = extract_video_audio(
        video,
        tmp_path / "slice.wav",
        sample_rate=48_000,
        start_ms=200,
        end_ms=700,
    )
    slice_samples, slice_sr = read_wav(sliced_wav)
    assert slice_sr == 48_000
    # 500 ms at 48 kHz = 24,000 frames (± a few codec frames)
    assert 18_000 <= slice_samples.size <= 30_000


def test_mux_dubbed_video_replace_and_duck_modes(tmp_path: Path) -> None:
    video = _make_test_video(tmp_path / "movie.mp4", duration_s=1.2, with_audio=True)
    silent_video = _make_test_video(tmp_path / "silent.mp4", duration_s=1.2, with_audio=False)
    assert probe_has_audio_stream(silent_video) is False

    # Create a 1.2s dubbed WAV at 48 kHz (880 Hz tone).
    sr = 48_000
    t = np.linspace(0.0, 1.2, int(sr * 1.2), endpoint=False, dtype=np.float32)
    dub_audio = 0.5 * np.sin(2.0 * np.pi * 880.0 * t).astype(np.float32)
    dub_wav = write_wav_file(dub_audio, tmp_path / "track.wav", sample_rate=sr)

    srt_file = tmp_path / "retimed.srt"
    srt_file.write_text(
        "1\n00:00:00,100 --> 00:00:01,000\nLồng tiếng hoàn tất.\n",
        encoding="utf-8",
    )

    # 1. Replace mode + soft subtitles in .mp4
    out_replace = mux_dubbed_video(
        video,
        dub_wav,
        tmp_path / "out_replace.mp4",
        srt_path=srt_file,
        mix_mode="replace",
        embed_subtitles=True,
    )
    assert out_replace.is_file() and out_replace.stat().st_size > 0
    assert probe_has_audio_stream(out_replace) is True

    # 2. Duck mode (mixes ducked source audio + dub audio without truncating short/long dubs)
    short_dub = write_wav_file(
        dub_audio[: int(sr * 0.5)], tmp_path / "short_track.wav", sample_rate=sr
    )
    out_duck = mux_dubbed_video(
        video,
        short_dub,
        tmp_path / "out_duck.mp4",
        srt_path=srt_file,
        mix_mode="duck",
        bg_gain_db=-16.0,
        embed_subtitles=False,
    )
    assert out_duck.is_file() and out_duck.stat().st_size > 0
    duck_wav = extract_video_audio(out_duck, tmp_path / "duck_check.wav", sample_rate=16_000)
    duck_samples, _ = read_wav(duck_wav)
    # Even though short_dub is only 0.5s, ducked output preserves the full ~1.2s video audio
    assert duck_samples.size >= int(16_000 * 1.0)

    # When dub audio overflows video duration (1.6s vs 1.2s), duck mode does not truncate speech
    long_t = np.linspace(0.0, 1.6, int(sr * 1.6), endpoint=False, dtype=np.float32)
    long_dub = write_wav_file(
        (0.5 * np.sin(2.0 * np.pi * 880.0 * long_t)).astype(np.float32),
        tmp_path / "long_track.wav",
        sample_rate=sr,
    )
    out_long_duck = mux_dubbed_video(
        video,
        long_dub,
        tmp_path / "out_long_duck.mp4",
        mix_mode="duck",
        bg_gain_db=-16.0,
        embed_subtitles=False,
    )
    long_duck_wav = extract_video_audio(
        out_long_duck, tmp_path / "long_duck_check.wav", sample_rate=16_000
    )
    long_duck_samples, _ = read_wav(long_duck_wav)
    assert long_duck_samples.size >= int(16_000 * 1.45)

    # 3. Duck mode on a silent source video falls back cleanly to replace
    out_silent_duck = mux_dubbed_video(
        silent_video,
        dub_wav,
        tmp_path / "out_silent_duck.mp4",
        mix_mode="duck",
        bg_gain_db=-16.0,
        embed_subtitles=False,
    )
    assert out_silent_duck.is_file() and out_silent_duck.stat().st_size > 0
    assert probe_has_audio_stream(out_silent_duck) is True

    with pytest.raises(VideoDubError):
        extract_video_audio(tmp_path / "missing.mp4", tmp_path / "out.wav")
