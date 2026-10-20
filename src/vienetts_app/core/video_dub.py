"""Video inspection, audio extraction, and stream-copy dubbing muxer (`ffmpeg`).

Video muxing uses ``-c:v copy`` so video streams are remuxed at disk speed
without re-encoding — except when subtitles are burned into the picture, which
must re-encode the frames. Outputs are written to a unique ``.part<ext>`` file
in the destination directory and atomically promoted via ``os.replace``.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path

VIDEO_EXTENSIONS: tuple[str, ...] = (".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v")
_VIDEO_EXT_SET = frozenset(VIDEO_EXTENSIONS)

#: Soft (player-toggleable) subtitle codec per output container. AVI has no
#: text subtitle stream, so it can only show subtitles burned into the picture.
_SOFT_SUBTITLE_CODECS: dict[str, str] = {
    ".mp4": "mov_text",
    ".mov": "mov_text",
    ".m4v": "mov_text",
    ".mkv": "srt",
    ".webm": "webvtt",
}

#: Video encoder per output container for burn-in (the frames change, so no
#: stream copy). Anything not listed gets H.264.
_BURN_VIDEO_ARGS: dict[str, tuple[str, ...]] = {
    ".webm": (
        "-c:v", "libvpx-vp9", "-crf", "32", "-b:v", "0",
        "-deadline", "good", "-cpu-used", "4", "-row-mt", "1",
    ),
    ".avi": ("-c:v", "mpeg4", "-q:v", "2"),
}  # fmt: skip
_BURN_VIDEO_DEFAULT: tuple[str, ...] = (
    "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p",
)  # fmt: skip

REPLACE_LOCK_ATTEMPTS = 5
REPLACE_LOCK_DELAY_S = 0.2


class VideoDubError(RuntimeError):
    """A video extraction or muxing operation failed; message is user-actionable."""


def is_video_path(path: str | Path) -> bool:
    """True when ``path`` has a supported video container suffix."""
    return Path(path).suffix.lower() in _VIDEO_EXT_SET


def supports_soft_subtitles(path: str | Path) -> bool:
    """True when ``path``'s container can carry a player-toggleable subtitle track."""
    return Path(path).suffix.lower() in _SOFT_SUBTITLE_CODECS


def find_companion_srt(video_path: str | Path) -> Path | None:
    """Discover a companion ``.srt`` file next to ``video_path``."""
    vpath = Path(video_path)
    parent = vpath.parent
    if not parent.is_dir():
        return None
    exact = parent / f"{vpath.stem}.srt"
    if exact.is_file():
        return exact
    # Check language-suffixed companions like lesson.vi.srt, lesson.en.srt
    candidates = sorted(
        p
        for p in parent.glob(f"{vpath.stem}.*.srt")
        if p.is_file() and p.name.lower().endswith(".srt")
    )
    return candidates[0] if candidates else None


def find_companion_video(srt_path: str | Path) -> Path | None:
    """Discover a companion video file next to ``srt_path``."""
    spath = Path(srt_path)
    parent = spath.parent
    if not parent.is_dir():
        return None
    stems = [spath.stem]
    if "." in spath.stem:
        base_stem = spath.stem.rsplit(".", 1)[0]
        if base_stem:
            stems.append(base_stem)
    for stem in stems:
        for ext in VIDEO_EXTENSIONS:
            candidate = parent / f"{stem}{ext}"
            if candidate.is_file():
                return candidate
            upper = parent / f"{stem}{ext.upper()}"
            if upper.is_file():
                return upper
    return None


def resolve_ffmpeg(data_dir: Path | None = None) -> Path:
    """Locate ``ffmpeg`` in the managed data dir or on ``PATH``."""
    exe = "ffmpeg.exe" if os.name == "nt" else "ffmpeg"
    if data_dir is not None:
        managed = Path(data_dir) / "runtimes" / "ffmpeg" / "bin" / exe
        if managed.is_file():
            return managed
    found = shutil.which("ffmpeg")
    if found:
        return Path(found)
    raise VideoDubError(
        "Không tìm thấy công cụ ffmpeg trên hệ thống. "
        "Hãy cài đặt ffmpeg để trích xuất âm thanh và ghép video."
    )


def resolve_ffprobe(data_dir: Path | None = None) -> Path | None:
    """Locate ``ffprobe`` if available."""
    exe = "ffprobe.exe" if os.name == "nt" else "ffprobe"
    if data_dir is not None:
        managed = Path(data_dir) / "runtimes" / "ffmpeg" / "bin" / exe
        if managed.is_file():
            return managed
    found = shutil.which("ffprobe")
    return Path(found) if found else None


def probe_has_audio_stream(video_path: str | Path, *, data_dir: Path | None = None) -> bool:
    """Return True when ``video_path`` contains at least one audio stream."""
    vpath = Path(video_path)
    if not vpath.is_file():
        return False
    ffprobe = resolve_ffprobe(data_dir)
    if ffprobe is not None:
        res = subprocess.run(
            [
                str(ffprobe),
                "-v",
                "error",
                "-select_streams",
                "a",
                "-show_entries",
                "stream=index",
                "-of",
                "csv=p=0",
                str(vpath),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        return res.returncode == 0 and bool(res.stdout.strip())
    ffmpeg = resolve_ffmpeg(data_dir)
    res = subprocess.run(
        [str(ffmpeg), "-nostdin", "-i", str(vpath)],
        capture_output=True,
        text=True,
        check=False,
    )
    return "Audio:" in (res.stderr or "")


def _promote_atomic(part: Path, target: Path) -> Path:
    for attempt in range(REPLACE_LOCK_ATTEMPTS):
        try:
            os.replace(part, target)
            return target
        except PermissionError:
            if attempt == REPLACE_LOCK_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_LOCK_DELAY_S)
    return target


def extract_video_audio(
    video_path: str | Path,
    output_wav: str | Path,
    *,
    sample_rate: int = 16_000,
    mono: bool = True,
    start_ms: int | None = None,
    end_ms: int | None = None,
    data_dir: Path | None = None,
) -> Path:
    """Extract mono/stereo PCM WAV from ``video_path`` (optionally sliced)."""
    vpath = Path(video_path)
    if not vpath.is_file():
        raise VideoDubError(f"Không tìm thấy tệp video: {vpath.name}")
    target = Path(output_wav)
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.part.wav")
    ffmpeg = resolve_ffmpeg(data_dir)

    cmd: list[str] = [str(ffmpeg), "-nostdin", "-y"]
    if start_ms is not None and start_ms > 0:
        cmd += ["-ss", f"{start_ms / 1000.0:.3f}"]
    if end_ms is not None and (start_ms is None or end_ms > start_ms):
        cmd += ["-to", f"{end_ms / 1000.0:.3f}"]
    cmd += [
        "-i",
        str(vpath),
        "-vn",
        "-ac",
        "1" if mono else "2",
        "-ar",
        str(int(sample_rate)),
        "-c:a",
        "pcm_s16le",
        "-f",
        "wav",
        str(part),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
            detail = (res.stderr or "").strip().splitlines()[-1:] or ["ffmpeg failed"]
            raise VideoDubError(f"Không thể trích xuất âm thanh từ '{vpath.name}': {detail[0]}")
        return _promote_atomic(part, target)
    except Exception:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
        raise


def mux_dubbed_video(
    video_path: str | Path,
    dub_wav_path: str | Path,
    output_path: str | Path,
    *,
    srt_path: str | Path | None = None,
    mix_mode: str = "replace",
    bg_gain_db: float = -16.0,
    embed_subtitles: bool = True,
    subtitle_mode: str = "soft",
    data_dir: Path | None = None,
) -> Path:
    """Mux ``dub_wav_path`` onto ``video_path``.

    With ``embed_subtitles`` and an existing ``srt_path``, ``subtitle_mode``
    picks how the cues ship: ``"soft"`` adds a player-toggleable track (stream
    copy, no video re-encode; skipped for containers without one, see
    :func:`supports_soft_subtitles`), ``"burn"`` draws them into the frames
    with libass and re-encodes the video. Otherwise the video is stream-copied.
    """
    vpath = Path(video_path).absolute()
    apath = Path(dub_wav_path).absolute()
    if not vpath.is_file():
        raise VideoDubError(f"Không tìm thấy tệp video nguồn: {vpath.name}")
    if not apath.is_file():
        raise VideoDubError(f"Không tìm thấy tệp âm thanh lồng tiếng: {apath.name}")

    target = Path(output_path).absolute()
    target.parent.mkdir(parents=True, exist_ok=True)
    ext = target.suffix.lower() or vpath.suffix.lower() or ".mp4"
    if not target.suffix:
        target = target.with_suffix(ext)
    part = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.part{ext}")

    ffmpeg = resolve_ffmpeg(data_dir)
    use_duck = str(mix_mode) == "duck" and probe_has_audio_stream(vpath, data_dir=data_dir)
    gain_db = max(-36.0, min(0.0, float(bg_gain_db)))

    want_subs = embed_subtitles and srt_path is not None and Path(srt_path).is_file()
    burn = want_subs and str(subtitle_mode) == "burn"
    soft_codec = _SOFT_SUBTITLE_CODECS.get(ext) if want_subs and not burn else None

    # The subtitles filter takes a filtergraph-escaped path, and Windows drive
    # colons/backslashes make that fragile: burn from a copy with a plain name
    # and run ffmpeg in its directory (every other path above is absolute).
    burn_srt = target.with_name(f".{uuid.uuid4().hex}.burn.srt") if burn else None

    cmd: list[str] = [
        str(ffmpeg),
        "-nostdin",
        "-y",
        "-i",
        str(vpath),
        "-i",
        str(apath),
    ]
    if soft_codec is not None and srt_path is not None:
        cmd += ["-i", str(Path(srt_path))]

    filters: list[str] = []
    video_out = "0:v:0"
    if burn_srt is not None:
        filters.append(f"[0:v:0]subtitles=filename={burn_srt.name}[vout]")
        video_out = "[vout]"
    audio_out = "1:a:0"
    if use_duck:
        filters.append(
            "[0:a:0]asplit=2[bg_src][sil_ref];"
            "[sil_ref]volume=0[sil];"
            "[1:a:0][sil]amix=inputs=2:duration=longest:dropout_transition=0:"
            "weights=1 0:normalize=0,asplit=2[sc][dub];"
            f"[bg_src]volume={gain_db:.1f}dB[bg_att];"
            "[bg_att][sc]sidechaincompress=threshold=0.015:ratio=6:attack=20:release=300[bg];"
            "[bg][dub]amix=inputs=2:duration=longest:dropout_transition=0:"
            "weights=1 1:normalize=0[aout]"
        )
        audio_out = "[aout]"
    if filters:
        cmd += ["-filter_complex", ";".join(filters)]
    cmd += ["-map", video_out, "-map", audio_out]

    if soft_codec is not None:
        cmd += ["-map", "2:s:0", "-c:s", soft_codec, "-disposition:s:0", "default"]
    if burn:
        cmd += list(_BURN_VIDEO_ARGS.get(ext, _BURN_VIDEO_DEFAULT))
    else:
        cmd += ["-c:v", "copy"]
    audio_codec = "libopus" if ext == ".webm" else "aac"
    cmd += [
        "-c:a",
        audio_codec,
        "-b:a",
        "192k",
    ]
    if ext in (".mp4", ".mov", ".m4v"):
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(part))

    try:
        if burn_srt is not None and srt_path is not None:
            shutil.copyfile(srt_path, burn_srt)
        res = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            check=False,
            cwd=str(burn_srt.parent) if burn_srt is not None else None,
        )
        if res.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
            stderr = res.stderr or ""
            if burn and "No such filter: 'subtitles'" in stderr:
                raise VideoDubError(
                    "Bản ffmpeg hiện tại không hỗ trợ in phụ đề lên hình (thiếu libass). "
                    "Hãy cài bản ffmpeg đầy đủ hoặc chọn kiểu phụ đề bật/tắt trong trình phát."
                )
            detail = stderr.strip().splitlines()[-1:] or ["ffmpeg mux failed"]
            raise VideoDubError(f"Không thể ghép âm thanh vào video '{vpath.name}': {detail[0]}")
        return _promote_atomic(part, target)
    except Exception:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
        raise
    finally:
        if burn_srt is not None:
            with contextlib.suppress(OSError):
                burn_srt.unlink(missing_ok=True)
