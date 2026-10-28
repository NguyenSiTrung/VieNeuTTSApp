"""Unit tests for core/video_dub.py: companion discovery, extraction, and stream-copy muxing."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from bisect import bisect_right
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from vienetts_app.core import video_dub
from vienetts_app.core.audio import read_wav, write_wav_file
from vienetts_app.core.video_dub import (
    VideoDubError,
    build_burn_ass,
    dub_overrun_ms,
    extract_video_audio,
    find_companion_srt,
    find_companion_video,
    is_video_path,
    merge_duck_spans,
    mux_dubbed_video,
    probe_has_audio_stream,
    resolve_ffmpeg,
    resolve_ffprobe,
    supports_soft_subtitles,
)

#: Tests that build or inspect real media; the stub-driven ones run everywhere.
requires_ffmpeg = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg required")


def _install_stub_ffmpeg(data_dir: Path, capture: Path) -> None:
    """Managed stub ``ffmpeg`` that records each mux call into ``capture/<n>/``.

    It saves its argv and cwd plus every ``*.cmd`` file in its cwd (the mux
    deletes its scratch dir afterwards), prints real-ffmpeg ``-progress``
    values when asked, and writes the output file. ``ffmpeg -nostdin -i x``
    (the audio probe without ffprobe) reports an audio stream.
    """
    bin_dir = data_dir / "runtimes" / "ffmpeg" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    capture.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "ffmpeg"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import json, os, shutil, sys\n"
        "from pathlib import Path\n"
        "args = sys.argv[1:]\n"
        "if args[-2:-1] == ['-i']:\n"
        "    sys.stderr.write('Stream #0:1: Audio: aac\\n')\n"
        "    sys.exit(1)\n"
        f"capture = Path({str(capture)!r})\n"
        "call = capture / str(len(os.listdir(capture)))\n"
        "call.mkdir()\n"
        "for name in os.listdir('.'):\n"
        "    if name.endswith('.cmd'):\n"
        "        shutil.copy(name, call / name)\n"
        "(call / 'argv.json').write_text(json.dumps({'argv': args, 'cwd': os.getcwd()}))\n"
        "if '-progress' in args:\n"
        "    for line in ('out_time_us=N/A', 'out_time_ms=N/A', 'progress=continue',\n"
        "                 'out_time_us=5000000', 'out_time_ms=5000000',\n"
        "                 'out_time=00:00:05.000000', 'progress=continue',\n"
        "                 'out_time_us=60000000', 'out_time_ms=60000000', 'progress=end'):\n"
        "        print(line, flush=True)\n"
        "Path(args[-1]).write_bytes(b'muxed')\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)


def _install_stub_ffprobe(data_dir: Path, *, duration_s: float) -> Path:
    """Managed stub ``ffprobe``: one audio stream, a 160x90 picture of ``duration_s``."""
    bin_dir = data_dir / "runtimes" / "ffmpeg" / "bin"
    bin_dir.mkdir(parents=True, exist_ok=True)
    stub = bin_dir / "ffprobe"
    stub.write_text(
        "#!/usr/bin/env python3\n"
        "import sys\n"
        "args = sys.argv[1:]\n"
        "if 'stream=index' in args:\n"
        "    print('1')\n"
        "elif 'default=nw=1:nk=1' in args:\n"
        f"    print('{duration_s:.6f}')\n"
        "else:\n"
        f"    print('width=160\\nheight=90\\npix_fmt=yuv420p\\nduration={duration_s:.6f}')\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return stub


def _recorded_call(capture: Path, index: int) -> tuple[list[str], Path]:
    """``(argv, call_dir)`` of the ``index``-th stub ffmpeg mux call."""
    call_dir = capture / str(index)
    record = json.loads((call_dir / "argv.json").read_text(encoding="utf-8"))
    return record["argv"], call_dir


_DUCK_COMMAND = re.compile(
    r"(\d+\.\d{3})-(\d+\.\d{3}) \[enter\] (\S+) volume (\S+), \[leave\] (\S+) volume (\S+);"
)


def _read_duck_commands(
    path: Path, target: str, outside: str
) -> tuple[list[tuple[int, int]], float]:
    """Parse an ``asendcmd`` file → ``([(start_ms, end_ms)], inside_gain)``.

    Every line must switch ``target`` (and only it) to one in-span gain on
    enter and back to ``outside`` — the level the graph initialises it at —
    on leave, so the level between spans always equals the untouched level.
    """
    intervals: list[tuple[int, int]] = []
    inside_values: set[str] = set()
    for line in path.read_text(encoding="ascii").splitlines():
        match = _DUCK_COMMAND.fullmatch(line)
        assert match is not None, line
        start, end, enter_target, inside, leave_target, leave_value = match.groups()
        assert (enter_target, leave_target, leave_value) == (target, target, outside), line
        intervals.append((round(float(start) * 1000), round(float(end) * 1000)))
        inside_values.add(inside)
    assert len(inside_values) == 1
    return intervals, float(inside_values.pop())


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


@requires_ffmpeg
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


def test_extract_video_audio_keeps_the_wav_when_ffmpeg_stderr_is_not_utf8(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Review A: undecodable stderr must not discard a good extraction.

    ffmpeg echoes the input name (UTF-8) and can emit raw bytes from container
    metadata; strict decoding used to raise and the cleanup deleted the WAV.
    """
    data_dir = tmp_path / "data"
    bin_dir = data_dir / "runtimes" / "ffmpeg" / "bin"
    bin_dir.mkdir(parents=True)
    ffmpeg = bin_dir / "ffmpeg"
    ffmpeg.write_text(
        "#!/usr/bin/env python3\n"
        "import sys, wave\n"
        "banner = \"Input #0, mov, from 'Phụ đề.mp4':\\n\".encode('utf-8')\n"
        "sys.stderr.buffer.write(banner + b'  title: \\x81\\n')\n"
        "sys.stderr.buffer.flush()\n"
        "with wave.open(sys.argv[-1], 'wb') as out:\n"
        "    out.setnchannels(1)\n"
        "    out.setsampwidth(2)\n"
        "    out.setframerate(16000)\n"
        "    out.writeframes(b'\\x00\\x00' * 1600)\n",
        encoding="utf-8",
    )
    ffmpeg.chmod(0o755)
    # A 100 s source gets twice its duration instead of the 120 s floor.
    ffprobe = bin_dir / "ffprobe"
    ffprobe.write_text("#!/usr/bin/env python3\nprint('100.000000')\n", encoding="utf-8")
    ffprobe.chmod(0o755)
    video = tmp_path / "Phụ đề.mp4"
    video.write_bytes(b"not decoded by the stub")

    timeouts: list[float | None] = []
    real_run_cli = video_dub.run_cli

    def run_cli_spy(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        timeouts.append(kwargs.get("timeout"))
        return real_run_cli(cmd, **kwargs)

    monkeypatch.setattr(video_dub, "run_cli", run_cli_spy)
    out = extract_video_audio(video, tmp_path / "Phụ đề.wav", data_dir=data_dir)

    assert out == tmp_path / "Phụ đề.wav"
    samples, sr = read_wav(out)
    assert (sr, samples.size) == (16_000, 1_600)
    assert not list(tmp_path.glob(".*.part.wav"))
    assert timeouts[-1] == pytest.approx(200.0)


@requires_ffmpeg
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

    # The mix ends in a -1 dBFS limiter: an over-full float dub (peak 1.4)
    # comes out under full scale instead of clipping in the encoder.
    loud_dub = write_wav_file(
        (1.4 * np.sin(2.0 * np.pi * 880.0 * t)).astype(np.float32),
        tmp_path / "loud_track.wav",
        sample_rate=sr,
    )
    out_loud = mux_dubbed_video(
        video, loud_dub, tmp_path / "out_loud.mp4", mix_mode="replace", embed_subtitles=False
    )
    loud_wav = extract_video_audio(out_loud, tmp_path / "loud_check.wav", sample_rate=48_000)
    loud_samples, _ = read_wav(loud_wav)
    assert float(np.max(np.abs(loud_samples))) < 0.97

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


@requires_ffmpeg
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
        extra = {"subtitle_language": "vi"} if ext == ".mp4" else {}
        out = mux_dubbed_video(src, dub_wav, tmp_path / f"soft{ext}", srt_path=srt_file, **extra)
        subs = [name for kind, name in _streams(out) if kind == "subtitle"]
        assert subs == ([codec] if codec else []), ext

    # The soft track carries the ISO 639-2/B tag a player uses to pick a default.
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


@requires_ffmpeg
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

    monkeypatch.setattr("vienetts_app.core.video_dub.run_cli", fake_run)
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


@requires_ffmpeg
def test_mux_clamp_to_video_is_opt_in(tmp_path: Path) -> None:
    """A dub longer than the picture keeps its words unless clamping is asked for."""
    src = _make_test_video(tmp_path / "short.mp4", duration_s=1.0)
    # 3 s of dub audio against a 1 s picture.
    long_dub = write_wav_file(
        np.zeros(144_000, dtype=np.float32), tmp_path / "long.wav", sample_rate=48_000
    )
    plain_seen: list[float] = []
    default_out = mux_dubbed_video(
        src, long_dub, tmp_path / "default.mp4", progress=plain_seen.append
    )
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
    # Plain and clamped exports both report monotone progress that holds at
    # ≤ 0.95 until the +faststart finalize, then ends at 1.0.
    for values in (plain_seen, seen):
        assert len(values) >= 2, values
        assert values == sorted(values)
        assert values[-1] == pytest.approx(1.0)
        assert all(0.0 <= value <= 0.95 for value in values[:-1])


def test_run_ffmpeg_terminates_when_cancelled(tmp_path: Path) -> None:
    """A hung ffmpeg must not outlive cancelExport (FR-1.4)."""
    import sys
    import time

    from vienetts_app.core.video_dub import _run_ffmpeg

    # Real ffmpeg prints both keys in MICROseconds (out_time_ms is a misnomer).
    script = (
        "import time\n"
        "for i in range(400):\n"
        "    print(f'out_time_us={i * 50_000}', flush=True)\n"
        "    print(f'out_time_ms={i * 50_000}', flush=True)\n"
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


def test_run_ffmpeg_progress_is_microseconds_monotonic_and_capped() -> None:
    """FR-5.6: ``out_time_us`` and ``out_time_ms`` are both µs.

    Progress never runs backwards and stays ≤ 0.95 while ffmpeg runs —
    ``progress=end`` included; the mux reports 1.0 once the file is final.
    """
    import sys

    from vienetts_app.core.video_dub import _run_ffmpeg

    lines = [
        "out_time_us=N/A",
        "out_time_ms=N/A",
        "progress=continue",
        "out_time_us=5000000",
        "out_time_ms=5000000",
        "out_time=00:00:05.000000",
        "progress=continue",
        "out_time_us=3000000",
        "out_time_ms=3000000",
        "progress=continue",
        "out_time_us=60000000",
        "out_time_ms=60000000",
        "progress=end",
    ]
    # An older ffmpeg prints only the misnamed out_time_ms (still µs).
    legacy = [line for line in lines if not line.startswith("out_time_us")]
    for emitted in (lines, legacy):
        seen: list[float] = []
        script = "".join(f"print({line!r}, flush=True)\n" for line in emitted)
        result = _run_ffmpeg(
            [sys.executable, "-c", script], cwd=None, progress=seen.append, duration_ms=10_000
        )
        assert result.returncode == 0
        # 5 s of 10 s is 0.5; the 3 s regression is dropped; the overshoot and
        # progress=end hold at the cap.
        assert seen == [0.5, 0.95]


def test_mux_progress_covers_plain_exports_and_holds_until_finalize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """FR-5.6 / AC-16: a non-burn export without clamping reports progress.

    The yardstick is the longer of the probed video and the dub WAV (read from
    its header when ffprobe is absent); 1.0 comes only after the output is
    promoted. The same call carries the limiter and the ISO 639-2/B tag.
    """
    data_dir = tmp_path / "data"
    capture = tmp_path / "calls"
    _install_stub_ffmpeg(data_dir, capture)
    ffprobe = _install_stub_ffprobe(data_dir, duration_s=20.0)
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"not decoded by the stub")
    dub = write_wav_file(
        np.zeros(80_000, dtype=np.float32), tmp_path / "track.wav", sample_rate=8_000
    )  # 10 s
    srt = tmp_path / "retimed.srt"
    srt.write_text("1\n00:00:00,100 --> 00:00:01,000\nXin chào\n", encoding="utf-8")

    def export(name: str) -> list[tuple[float, bool]]:
        target = tmp_path / name
        seen: list[tuple[float, bool]] = []
        mux_dubbed_video(
            video,
            dub,
            target,
            srt_path=srt,
            subtitle_language="vi",
            progress=lambda value: seen.append((value, target.is_file())),
            data_dir=data_dir,
        )
        return seen

    # 5 s of the 20 s video, then the cap while ffmpeg finalizes, then 1.0
    # with the promoted file in place.
    assert export("probed.mp4") == [(0.25, False), (0.95, False), (1.0, True)]
    ffprobe.unlink()
    monkeypatch.setattr(video_dub, "resolve_ffprobe", lambda data_dir=None: None)
    assert export("fallback.mp4") == [(0.5, False), (0.95, False), (1.0, True)]

    argv, _ = _recorded_call(capture, 1)
    graph = argv[argv.index("-filter_complex") + 1]
    assert graph == "[1:a:0]alimiter=limit=0.891:level=0[aout]"
    maps = [argv[i + 1] for i, arg in enumerate(argv) if arg == "-map"]
    assert maps == ["0:v:0", "[aout]", "2:s:0"]
    assert argv[argv.index("-metadata:s:s:0") + 1] == "language=vie"
    assert argv[argv.index("-movflags") + 1] == "+faststart"


def test_mux_duck_envelope_scales_through_command_files(tmp_path: Path) -> None:
    """FR-5.4 / AC-16: duck spans ride in ``asendcmd`` files passed by name.

    1,500 spans keep the command line < 8 KB. Every span is ducked, a gap
    above ``merge_gap_ms`` stays at full level, and nothing collapses spans to
    a cap. Filtergraph-special characters in the output dir never reach the
    graph (the files sit in ffmpeg's cwd), and the scratch dir is removed.
    """
    data_dir = tmp_path / "data"
    capture = tmp_path / "calls"
    _install_stub_ffmpeg(data_dir, capture)
    _install_stub_ffprobe(data_dir, duration_s=1_500.0)
    video = tmp_path / "movie.mp4"
    video.write_bytes(b"not decoded by the stub")
    dub = write_wav_file(np.zeros(800, dtype=np.float32), tmp_path / "track.wav", sample_rate=8_000)
    out_dir = tmp_path / "Phụ đề, [x]; it's"

    # Padding adds 20 + 80 ms, so these gaps become 1100, 150 (merges), 151, 800.
    gaps = (1_200, 250, 251, 900)
    spans: list[tuple[int, int]] = []
    start = 500
    for i in range(1_500):
        spans.append((start, start + 300))
        start += 300 + gaps[i % 4]
    out = mux_dubbed_video(
        video,
        dub,
        out_dir / "out.mp4",
        mix_mode="duck",
        embed_subtitles=False,
        duck_spans=spans[::-1],
        data_dir=data_dir,
    )
    assert out.is_file()
    assert sorted(p.name for p in out_dir.iterdir()) == ["out.mp4"]
    # A cancelled duck export removes its command files and partial output too.
    with pytest.raises(video_dub.VideoDubCancelled):
        mux_dubbed_video(
            video,
            dub,
            out_dir / "cancelled.mp4",
            mix_mode="duck",
            embed_subtitles=False,
            duck_spans=spans,
            data_dir=data_dir,
            cancel_check=lambda: True,
        )
    assert sorted(p.name for p in out_dir.iterdir()) == ["out.mp4"]

    argv, call_dir = _recorded_call(capture, 0)
    assert len(subprocess.list2cmdline(argv)) < 8 * 1024
    graph = argv[argv.index("-filter_complex") + 1]
    assert "between(" not in graph
    assert not any(ch in graph for ch in "/\\'") and "Phụ" not in graph
    assert graph.endswith(",alimiter=limit=0.891:level=0[aout]")
    # Each command file feeds the volume instance right behind its own
    # asendcmd, which starts at the level the file restores between spans.
    switches = re.findall(r"asendcmd=f=([\w.]+),(volume@\w+)=volume=([\d.]+)", graph)
    assert len(switches) == 2 and len({target for _, target, _ in switches}) == 2
    levels: dict[float, tuple[list[tuple[int, int]], float]] = {}
    for filename, target, outside in switches:
        intervals, inside = _read_duck_commands(call_dir / filename, target, outside)
        levels[float(outside)] = (intervals, inside)
    gate_intervals, gate_inside = levels[0.0]  # sidechain trigger: source only in spans
    bg_intervals, bg_inside = levels[1.0]  # background: an extra -12 dB in spans
    assert gate_inside == 1.0 and bg_inside == pytest.approx(10 ** (-12 / 20), abs=1e-5)
    assert gate_intervals == bg_intervals
    merged_gaps = sum(1 for i in range(len(spans) - 1) if gaps[i % 4] - 100 <= 150)
    assert len(gate_intervals) == len(spans) - merged_gaps
    starts = [s for s, _ in gate_intervals]

    def ducked(t_ms: int) -> bool:
        i = bisect_right(starts, t_ms) - 1
        return i >= 0 and t_ms < gate_intervals[i][1]

    for i, (s, e) in enumerate(spans):
        assert ducked(s) and ducked((s + e) // 2) and ducked(e - 1)
        if i + 1 < len(spans):
            gap = spans[i + 1][0] - e
            assert ducked(e + gap // 2) is (gap - 100 <= 150), (s, e, gap)

    # Scale: 5,000 well-separated spans survive untouched (no collapse cap).
    far = [(1_000 + i * 2_000, 1_500 + i * 2_000) for i in range(5_000)]
    assert merge_duck_spans(far) == [(s - 20, e + 80) for s, e in far]


def test_subtitle_language_tags_use_iso639_2b() -> None:
    """FR-5.5: soft tracks carry ISO 639-2/B codes; anything unknown is ``und``."""
    from vienetts_app.core.video_dub import _subtitle_language_tag

    expected = {
        "vi": "vie",
        "VI": "vie",
        "vie": "vie",
        "en": "eng",
        "zh": "chi",
        "zh-CN": "chi",
        "zh_TW": "chi",
        "zho": "chi",
        "ja": "jpn",
        "ko": "kor",
        "fr": "fre",
        "fra": "fre",
        "de": "ger",
        "es": "spa",
        "pt-BR": "por",
        "xx": "und",
        "klingon": "und",
        "auto": "und",
        "": "und",
    }
    assert {code: _subtitle_language_tag(code) for code in expected} == expected


@requires_ffmpeg
def test_mux_duck_spans_suppresses_trailing_source_speech_when_dub_is_shorter(
    tmp_path: Path,
) -> None:
    """AC-7: duck mode attenuates background audio across the union of original speech
    and dub spans so source actor speech does not bleed through when dub ends early.
    """
    from vienetts_app.core.subtitles import Cue
    from vienetts_app.core.timeline import SegmentSpan
    from vienetts_app.core.video_dub import build_duck_spans

    cues = [Cue(1, 0, 1_100, "Hello there", speech_start_ms=100, speech_end_ms=1_000)]
    placed = [SegmentSpan(0, 11, 100, 350)]  # dub finishes at 350 ms while source speaks to 1000 ms
    spans = build_duck_spans(cues, placed)
    assert spans == [(80, 1_080)]  # padded [100 - 20, 1000 + 80]

    video = _make_test_video(tmp_path / "speech_src.mp4", duration_s=1.6, with_audio=True)
    sr = 48_000
    # Dub audio is active only 100..350 ms and silent from 350..1600 ms.
    dub_samples = np.zeros(int(sr * 1.6), dtype=np.float32)
    t_dub = np.linspace(0.0, 0.25, int(sr * 0.25), endpoint=False, dtype=np.float32)
    dub_samples[int(sr * 0.10) : int(sr * 0.35)] = (
        0.4 * np.sin(2.0 * np.pi * 880.0 * t_dub)
    ).astype(np.float32)
    dub_wav = write_wav_file(dub_samples, tmp_path / "short_dub.wav", sample_rate=sr)

    out_with_spans = mux_dubbed_video(
        video,
        dub_wav,
        tmp_path / "ducked_with_spans.mp4",
        mix_mode="duck",
        bg_gain_db=-6.0,
        embed_subtitles=False,
        duck_spans=spans,
    )
    out_wav = extract_video_audio(out_with_spans, tmp_path / "out_spans.wav", sample_rate=16_000)
    samples, out_sr = read_wav(out_wav)
    # Trailing source speech window (500..950 ms, where dub is already silent) vs
    # post-speech ambient window (1250..1500 ms, after original speech ended).
    trailing_speech_rms = float(
        np.sqrt(np.mean(np.square(samples[int(out_sr * 0.50) : int(out_sr * 0.95)])))
    )
    post_speech_rms = float(
        np.sqrt(np.mean(np.square(samples[int(out_sr * 1.25) : int(out_sr * 1.50)])))
    )
    assert trailing_speech_rms < post_speech_rms * 0.35
