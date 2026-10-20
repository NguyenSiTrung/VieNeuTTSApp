"""Video inspection, audio extraction, and stream-copy dubbing muxer (`ffmpeg`).

All video muxing uses ``-c:v copy`` so video streams are remuxed at disk speed
without re-encoding. Outputs are written to a unique ``.part<ext>`` file in the
destination directory and atomically promoted via ``os.replace``.
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

REPLACE_LOCK_ATTEMPTS = 5
REPLACE_LOCK_DELAY_S = 0.2


class VideoDubError(RuntimeError):
    """A video extraction or muxing operation failed; message is user-actionable."""


def is_video_path(path: str | Path) -> bool:
    """True when ``path`` has a supported video container suffix."""
    return Path(path).suffix.lower() in _VIDEO_EXT_SET


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
    data_dir: Path | None = None,
) -> Path:
    """Mux ``dub_wav_path`` onto ``video_path`` with ``-c:v copy`` (no video re-encode)."""
    vpath = Path(video_path)
    apath = Path(dub_wav_path)
    if not vpath.is_file():
        raise VideoDubError(f"Không tìm thấy tệp video nguồn: {vpath.name}")
    if not apath.is_file():
        raise VideoDubError(f"Không tìm thấy tệp âm thanh lồng tiếng: {apath.name}")

    target = Path(output_path)
    target.parent.mkdir(parents=True, exist_ok=True)
    ext = target.suffix.lower() or vpath.suffix.lower() or ".mp4"
    if not target.suffix:
        target = target.with_suffix(ext)
    part = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.part{ext}")

    ffmpeg = resolve_ffmpeg(data_dir)
    use_duck = str(mix_mode) == "duck" and probe_has_audio_stream(vpath, data_dir=data_dir)
    gain_db = max(-36.0, min(0.0, float(bg_gain_db)))

    has_srt = (
        embed_subtitles
        and srt_path is not None
        and Path(srt_path).is_file()
        and ext in (".mp4", ".mov", ".m4v", ".mkv")
    )

    cmd: list[str] = [
        str(ffmpeg),
        "-nostdin",
        "-y",
        "-i",
        str(vpath),
        "-i",
        str(apath),
    ]
    if has_srt and srt_path is not None:
        cmd += ["-i", str(Path(srt_path))]

    if use_duck:
        filter_str = (
            "[0:a:0]asplit=2[bg_src][sil_ref];"
            "[sil_ref]volume=0[sil];"
            "[1:a:0][sil]amix=inputs=2:duration=longest:dropout_transition=0:"
            "weights=1 0:normalize=0,asplit=2[sc][dub];"
            f"[bg_src]volume={gain_db:.1f}dB[bg_att];"
            "[bg_att][sc]sidechaincompress=threshold=0.015:ratio=6:attack=20:release=300[bg];"
            "[bg][dub]amix=inputs=2:duration=longest:dropout_transition=0:"
            "weights=1 1:normalize=0[aout]"
        )
        cmd += [
            "-filter_complex",
            filter_str,
            "-map",
            "0:v:0",
            "-map",
            "[aout]",
        ]
    else:
        cmd += [
            "-map",
            "0:v:0",
            "-map",
            "1:a:0",
        ]

    if has_srt:
        sub_codec = "srt" if ext == ".mkv" else "mov_text"
        cmd += ["-map", "2:s:0", "-c:s", sub_codec, "-disposition:s:0", "default"]
    audio_codec = "libopus" if ext == ".webm" else "aac"
    cmd += [
        "-c:v",
        "copy",
        "-c:a",
        audio_codec,
        "-b:a",
        "192k",
    ]
    if ext in (".mp4", ".mov", ".m4v"):
        cmd += ["-movflags", "+faststart"]
    cmd.append(str(part))

    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
            detail = (res.stderr or "").strip().splitlines()[-1:] or ["ffmpeg mux failed"]
            raise VideoDubError(f"Không thể ghép âm thanh vào video '{vpath.name}': {detail[0]}")
        return _promote_atomic(part, target)
    except Exception:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
        raise
