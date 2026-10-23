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
    build_burn_ass,
    dub_overrun_ms,
    extract_video_audio,
    find_companion_srt,
    find_companion_video,
    is_video_path,
    mux_dubbed_video,
    probe_has_audio_stream,
    resolve_ffmpeg,
    resolve_ffprobe,
    supports_soft_subtitles,
)

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")


# Source codecs each container accepts (WebM is VP8/VP9 + Opus/Vorbis only).
_TEST_CODECS = {
    ".webm": ("libvpx-vp9", "libopus"),
    ".avi": ("mpeg4", "aac"),
}


def _make_test_video(
    path: Path, *, duration_s: float = 1.2, with_audio: bool = True, size: str = "160x90"
) -> Path:
    ffmpeg = str(resolve_ffmpeg())
    vcodec, acodec = _TEST_CODECS.get(path.suffix.lower(), ("libx264", "aac"))
    cmd = [
        ffmpeg,
        "-nostdin",
        "-y",
        "-f",
        "lavfi",
        "-i",
        f"color=c=black:s={size}:r=15:d={duration_s}",
    ]
    if with_audio:
        cmd += [
            "-f",
            "lavfi",
            "-i",
            f"sine=frequency=440:sample_rate=16000:duration={duration_s}",
            "-c:v",
            vcodec,
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            acodec,
            "-shortest",
            str(path),
        ]
    else:
        cmd += [
            "-c:v",
            vcodec,
            "-pix_fmt",
            "yuv420p",
            "-an",
            str(path),
        ]
    subprocess.run(cmd, check=True, capture_output=True)
    return path


def _streams(path: Path) -> list[tuple[str, str]]:
    ffprobe = resolve_ffprobe()
    if ffprobe is None:
        pytest.skip("ffprobe required")
    res = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type,codec_name",
            "-of",
            "csv=p=0",
            str(path),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return [tuple(line.split(",")[::-1]) for line in res.stdout.split()]  # (type, name)


def _peak_luma(video: Path, at_s: float) -> int:
    """Brightest gray pixel of the frame at ``at_s`` — subtitles are white on black."""
    res = subprocess.run(
        [
            str(resolve_ffmpeg()),
            "-nostdin",
            "-v",
            "error",
            "-ss",
            f"{at_s:.2f}",
            "-i",
            str(video),
            "-frames:v",
            "1",
            "-f",
            "rawvideo",
            "-pix_fmt",
            "gray",
            "-",
        ],
        capture_output=True,
        check=True,
    )
    return int(np.frombuffer(res.stdout, dtype=np.uint8).max())


def _has_subtitles_filter() -> bool:
    res = subprocess.run(
        [str(resolve_ffmpeg()), "-hide_banner", "-filters"],
        capture_output=True,
        text=True,
        check=False,
    )
    return " subtitles " in res.stdout


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


def test_mux_subtitles_soft_track_per_container_and_burn_in(tmp_path: Path) -> None:
    sr = 48_000
    dub_wav = write_wav_file(
        np.zeros(int(sr * 1.2), dtype=np.float32), tmp_path / "track.wav", sample_rate=sr
    )
    srt_file = tmp_path / "retimed.srt"
    srt_file.write_text(
        "1\n00:00:00,100 --> 00:00:01,100\nLồng tiếng hoàn tất.\n", encoding="utf-8"
    )

    # Soft track: a codec the container can carry; AVI cannot carry one at all.
    expected = {".mp4": "mov_text", ".mkv": "subrip", ".webm": "webvtt", ".avi": None}
    for ext, codec in expected.items():
        assert supports_soft_subtitles(f"clip{ext.upper()}") is (codec is not None)
        src = _make_test_video(tmp_path / f"src{ext}")
        extra = {"subtitle_language": "vie"} if ext == ".mp4" else {}
        out = mux_dubbed_video(src, dub_wav, tmp_path / f"soft{ext}", srt_path=srt_file, **extra)
        subs = [name for kind, name in _streams(out) if kind == "subtitle"]
        assert subs == ([codec] if codec else []), ext

    # The soft track carries the language tag a player uses to pick a default.
    res = subprocess.run(
        [
            str(resolve_ffprobe()),
            "-v",
            "error",
            "-select_streams",
            "s:0",
            "-show_entries",
            "stream_tags=language",
            "-of",
            "default=nw=1:nk=1",
            str(tmp_path / "soft.mp4"),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    assert res.stdout.strip() == "vie"

    if not _has_subtitles_filter():
        pytest.skip("ffmpeg built without libass (no subtitles filter)")
    # Burn-in draws the text into the frames (no subtitle stream), in replace
    # and duck mode, for both the stream-copy-hostile AVI and MP4 targets.
    for ext, mode in ((".mp4", "duck"), (".avi", "replace")):
        src = _make_test_video(tmp_path / f"burn_src{ext}", size="320x180")
        assert _peak_luma(src, 0.5) < 40
        out = mux_dubbed_video(
            src,
            dub_wav,
            tmp_path / f"burned{ext}",
            srt_path=srt_file,
            mix_mode=mode,
            subtitle_mode="burn",
        )
        kinds = [kind for kind, _ in _streams(out)]
        assert kinds.count("subtitle") == 0 and "video" in kinds and "audio" in kinds
        assert _peak_luma(out, 0.5) > 160, ext

    # embed_subtitles=False wins over the burn mode: the picture stays clean.
    plain_src = tmp_path / "burn_src.mp4"
    plain = mux_dubbed_video(
        plain_src,
        dub_wav,
        tmp_path / "plain.mp4",
        srt_path=srt_file,
        embed_subtitles=False,
        subtitle_mode="burn",
    )
    assert _peak_luma(plain, 0.5) < 40


def test_mux_burn_in_reports_missing_libass(tmp_path: Path, monkeypatch) -> None:
    video = _make_test_video(tmp_path / "movie.mp4")
    dub_wav = write_wav_file(
        np.zeros(4_800, dtype=np.float32), tmp_path / "track.wav", sample_rate=48_000
    )
    srt_file = tmp_path / "retimed.srt"
    srt_file.write_text("1\n00:00:00,000 --> 00:00:00,050\nx\n", encoding="utf-8")
    seen: list[list[str]] = []

    def fake_run(cmd, **kwargs):
        seen.append([str(c) for c in cmd])
        return subprocess.CompletedProcess(
            cmd,
            1,
            "",
            "[AVFilterGraph @ 0x1] No such filter: 'ass'\nError initializing filters\n",
        )

    monkeypatch.setattr("vienetts_app.core.video_dub.subprocess.run", fake_run)
    with pytest.raises(VideoDubError, match="libass"):
        mux_dubbed_video(
            video, dub_wav, tmp_path / "out.mp4", srt_path=srt_file, subtitle_mode="burn"
        )
    # Burn-in restyles into ASS so libass gets an explicit, matched style.
    ffmpeg_calls = [call for call in seen if "ffmpeg" in call[0]]
    assert ffmpeg_calls, "expected an ffmpeg invocation"
    burn_cmd = ffmpeg_calls[-1]
    filter_arg = burn_cmd[burn_cmd.index("-filter_complex") + 1]
    assert "ass=" in filter_arg
    assert "copy" not in burn_cmd
    assert not list(tmp_path.glob(".*.part.mp4"))


def test_burn_ass_style_scales_with_frame_and_caps_lines() -> None:
    """The ASS script carries a resolution-matched style, not libass defaults."""
    landscape = build_burn_ass([(0, 1_000, "Xin chào")], width=1920, height=1080)
    portrait = build_burn_ass([(0, 1_000, "Xin chào")], width=1080, height=1920)

    def _style(script: str) -> dict[str, str]:
        line = next(ln for ln in script.splitlines() if ln.startswith("Style: Default,"))
        keys = [
            "Name", "Fontname", "Fontsize", "PrimaryColour", "SecondaryColour",
            "OutlineColour", "BackColour", "Bold", "Italic", "Underline",
            "StrikeOut", "ScaleX", "ScaleY", "Spacing", "Angle", "BorderStyle",
            "Outline", "Shadow", "Alignment", "MarginL", "MarginR", "MarginV",
            "Encoding",
        ]  # fmt: skip
        return dict(zip(keys, line[len("Style: ") :].split(","), strict=True))

    land_style = _style(landscape)
    port_style = _style(portrait)
    # PlayRes is pinned to the real frame so font size is a true fraction.
    assert "PlayResX: 1920" in landscape and "PlayResY: 1080" in landscape
    assert "PlayResX: 1080" in portrait and "PlayResY: 1920" in portrait
    # A portrait frame is taller, so its font is proportionally larger in
    # pixels but the same fraction of the picture.
    assert int(port_style["Fontsize"]) > int(land_style["Fontsize"])
    land_ratio = int(land_style["Fontsize"]) / 1080
    port_ratio = int(port_style["Fontsize"]) / 1920
    assert abs(land_ratio - port_ratio) < 0.005
    assert land_style["Fontname"] == "Be Vietnam Pro"
    assert "WrapStyle: 0" in landscape

    # A long cue shrinks to fit instead of covering the frame with text.
    long_cue = "đây là một câu phụ đề rất dài " * 4
    script = build_burn_ass([(0, 1_000, long_cue)], width=1080, height=1920)
    event = next(ln for ln in script.splitlines() if ln.startswith("Dialogue:"))
    body = event.split(",,")[-1]
    assert body.count("\\N") + 1 <= 5


def test_burn_ass_escapes_text_libass_would_misread() -> None:
    """Braces and ``-->`` in cue text must not be consumed or re-parsed."""
    script = build_burn_ass([(0, 1_000, "A --> B {thẻ} \\N test")], width=1280, height=720)
    event = next(ln for ln in script.splitlines() if ln.startswith("Dialogue:"))
    body = event.split(",,")[-1]
    # Braces are escaped so libass draws them instead of eating the block.
    assert "\\{thẻ\\}" in body
    # The arrow carries a zero-width space so ffmpeg's SRT reader cannot read
    # this line as a new cue header (which silently dropped the whole cue).
    assert "-\u200b->" in body
    assert " --> " not in body
    # A literal backslash from the user is doubled, not interpreted as \N.
    assert "\\\\N" in body


def test_mux_clamp_to_video_is_opt_in(tmp_path: Path) -> None:
    """A dub longer than the picture keeps its words unless clamping is asked for."""
    src = _make_test_video(tmp_path / "short.mp4", duration_s=1.0)
    # 3 s of dub audio against a 1 s picture.
    long_dub = write_wav_file(
        np.zeros(144_000, dtype=np.float32), tmp_path / "long.wav", sample_rate=48_000
    )
    default_out = mux_dubbed_video(src, long_dub, tmp_path / "default.mp4")
    seen: list[float] = []
    clamped = mux_dubbed_video(
        src, long_dub, tmp_path / "clamped.mp4", clamp_to_video=True, progress=seen.append
    )

    def _duration(path: Path) -> float:
        res = subprocess.run(
            [
                str(resolve_ffprobe()),
                "-v",
                "error",
                "-show_entries",
                "format=duration",
                "-of",
                "default=nw=1:nk=1",
                str(path),
            ],
            capture_output=True,
            text=True,
            check=True,
        )
        return float(res.stdout.strip())

    # Speech is never cut by default...
    assert _duration(default_out) > 0.9
    # ...but an explicit clamp trims to the picture.
    assert _duration(clamped) <= 1.1
    assert dub_overrun_ms(src, long_dub) > 500
    # The mux reports monotone progress ending at 1.0.
    assert seen, "expected progress callbacks"
    assert seen[-1] == pytest.approx(1.0)
    assert all(0.0 <= value <= 1.0 for value in seen)


def test_run_ffmpeg_terminates_when_cancelled(tmp_path: Path) -> None:
    """A hung ffmpeg must not outlive cancelExport (FR-1.4)."""
    import sys
    import time

    from vienetts_app.core.video_dub import _run_ffmpeg

    script = (
        "import time\n"
        "for i in range(400):\n"
        "    print(f'out_time_ms={i * 1000}', flush=True)\n"
        "    print('progress=continue', flush=True)\n"
        "    time.sleep(0.05)\n"
    )
    calls = {"n": 0}
    progress: list[float] = []

    def cancel_check() -> bool:
        calls["n"] += 1
        return calls["n"] > 3

    started = time.monotonic()
    result = _run_ffmpeg(
        [sys.executable, "-c", script],
        cwd=None,
        progress=progress.append,
        duration_ms=60_000,
        cancel_check=cancel_check,
    )
    elapsed = time.monotonic() - started
    assert result.returncode != 0
    assert elapsed < 10
