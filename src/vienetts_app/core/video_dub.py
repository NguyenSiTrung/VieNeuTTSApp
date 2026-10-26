"""Video inspection, audio extraction, and stream-copy dubbing muxer (`ffmpeg`).

Video muxing uses ``-c:v copy`` so video streams are remuxed at disk speed
without re-encoding — except when subtitles are burned into the picture, which
must re-encode the frames. Outputs are written to a unique ``.part<ext>`` file
in the destination directory and atomically promoted via ``os.replace``.

Burn-in always writes an ASS script rather than handing libass the raw SRT.
That buys three things a bare ``subtitles=`` call cannot: an explicit
resolution-matched style (libass's default size is tuned for 16:9, so portrait
9:16 clips got text twice as tall as intended), a hard line cap with adaptive
font shrinking so a long cue cannot cover the picture, and full control over
escaping — ``{…}`` blocks and ``-->`` inside cue text are otherwise
reinterpreted by the subtitle parser and silently lose or drop the line.
"""

from __future__ import annotations

import contextlib
import os
import queue
import shutil
import subprocess
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

VIDEO_EXTENSIONS: tuple[str, ...] = (".mp4", ".mkv", ".mov", ".webm", ".avi", ".m4v")
_VIDEO_EXT_SET = frozenset(VIDEO_EXTENSIONS)

#: Bundled Be Vietnam Pro TTFs, used as the burn-in ``fontsdir`` so exported
#: subtitles match the app's own typography (and carry Vietnamese glyphs even
#: on a host with no Vietnamese font installed).
FONTS_DIR = Path(__file__).resolve().parent.parent / "ui" / "qml" / "fonts"
_BURN_FONT_NAME = "Be Vietnam Pro"

#: Burn-in style, expressed as fractions of the frame so one rule covers
#: landscape, portrait, and square. Tuned against ffmpeg 6.1/libass: a
#: 1080x1920 portrait clip lands at ~11.6% frame height / 3 lines, matching the
#: ~10.7% a 16:9 clip already got from libass's defaults.
_BURN_FONT_SIZE_RATIO = 0.05
_BURN_MARGIN_X_RATIO = 0.05
_BURN_MARGIN_V_RATIO = 0.045
_BURN_OUTLINE_RATIO = 0.07
_BURN_SHADOW_RATIO = 0.035
#: WrapStyle 0 = smart wrapping, plus a hard cap so a runaway cue cannot cover
#: the picture (libass's default lets a long line fill the whole frame).
_BURN_MAX_LINES = 3
#: Hard ceiling for a pathological cue that cannot fit :data:`_BURN_MAX_LINES`
#: even at the floor font size — still bounded, just more forgiving.
_BURN_ABS_MAX_LINES = 5
#: Floor for the adaptive shrink applied to overlong cues, as a fraction of the
#: base size — keeps text legible even when it must shrink to fit.
_BURN_MIN_FONT_SCALE = 0.55
#: Approximate advance width of Be Vietnam Pro, used to pre-wrap text to
#: :data:`_BURN_MAX_LINES` before libass ever sees it.
_BURN_CHAR_WIDTH_RATIO = 0.52

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


@dataclass(frozen=True)
class VideoInfo:
    """The parts of a video's video stream the muxer needs to stay faithful."""

    width: int = 0
    height: int = 0
    pix_fmt: str = ""
    duration_ms: int = 0


def probe_video_info(video_path: str | Path, *, data_dir: Path | None = None) -> VideoInfo:
    """Best-effort ``VideoInfo`` for ``video_path``; zeros when unknown.

    Never raises: burn-in must still work when ffprobe is absent or the file
    is odd, so every field falls back to a value that keeps the output sane.
    """
    vpath = Path(video_path)
    ffprobe = resolve_ffprobe(data_dir)
    if ffprobe is None or not vpath.is_file():
        return VideoInfo()
    res = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height,pix_fmt",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1",
            str(vpath),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0:
        return VideoInfo()
    fields: dict[str, str] = {}
    for line in (res.stdout or "").splitlines():
        key, _, value = line.partition("=")
        if key and value:
            fields[key.strip()] = value.strip()
    duration_ms = 0
    with contextlib.suppress(ValueError):
        duration_ms = int(float(fields.get("duration", "0")) * 1000)
    width = height = 0
    with contextlib.suppress(ValueError):
        width = int(fields.get("width", "0"))
        height = int(fields.get("height", "0"))
    return VideoInfo(
        width=max(0, width),
        height=max(0, height),
        pix_fmt=fields.get("pix_fmt", ""),
        duration_ms=max(0, duration_ms),
    )


def _ass_timestamp(ms: int) -> str:
    """``ms`` → ASS ``H:MM:SS.cc`` (centiseconds, as the format requires)."""
    total_cs = max(0, int(ms)) // 10
    hours, rem = divmod(total_cs, 360_000)
    minutes, rem = divmod(rem, 6_000)
    seconds, centis = divmod(rem, 100)
    return f"{hours:d}:{minutes:02d}:{seconds:02d}.{centis:02d}"


def _ass_escape(text: str) -> str:
    """Make ``text`` literal inside an ASS dialogue line.

    Backslashes start ASS escapes, and ``{…}`` opens an override block — both
    are consumed by libass rather than drawn, so a cue like ``a {b} c`` used to
    render as ``a  c``. Doubling the backslash and bracing the braces keeps the
    characters the user typed on screen.
    """
    out = text.replace("\\", "\\\\")
    return out.replace("{", "\\{").replace("}", "\\}")


def _sanitize_cue_text(text: str) -> str:
    """Flatten cue text to something a subtitle parser cannot misread.

    ``-->`` inside a cue body is indistinguishable from a cue header: ffmpeg
    re-parses the file, sees an empty script, and burns a subtitle-free video
    without reporting an error. The same goes for a bare line that looks like a
    timestamp. Newlines are flattened too, so a multi-line cue cannot forge a
    second cue.
    """
    cleaned = " ".join(str(text or "").split())
    if "-->" in cleaned:
        # A zero-width space keeps the arrow visible but not parseable as a
        # cue separator (U+200B is not a legal digit/space in the timestamp
        # grammar, so ffmpeg's SRT reader no longer matches it).
        cleaned = cleaned.replace("-->", "-\u200b->")
    return cleaned


def _wrap_lines(text: str, max_chars: int) -> list[str]:
    """Word-wrap ``text`` to ``max_chars`` per line, without dropping words."""
    max_chars = max(8, int(max_chars))
    words = str(text or "").split()
    if not words:
        return []
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if current and len(candidate) > max_chars:
            lines.append(current)
            current = word
        else:
            current = candidate
    if current:
        lines.append(current)
    return lines


def _fit_cue(text: str, *, base_font: int, max_chars: int) -> tuple[int, list[str]]:
    """Wrap ``text`` and pick the largest font that fits the line budget.

    Rather than truncating an overlong cue (which silently drops spoken words),
    the font shrinks until the text fits :data:`_BURN_MAX_LINES`. A pathological
    cue that still will not fit at the floor size gets up to
    :data:`_BURN_ABS_MAX_LINES` lines instead — covering the frame is worse than
    a smaller block, but losing the words is worse still.
    """
    floor = max(10, round(base_font * _BURN_MIN_FONT_SCALE))
    for scale in (1.0, 0.9, 0.8, 0.7, 0.6, 0.5):
        font = max(floor, round(base_font * scale))
        # A smaller font fits more characters per line, so recompute the budget.
        chars = max(8, int(max_chars * (base_font / max(1, font))))
        lines = _wrap_lines(text, chars)
        if len(lines) <= _BURN_MAX_LINES:
            return font, lines
    chars = max(8, int(max_chars * (base_font / floor)))
    lines = _wrap_lines(text, chars)
    if len(lines) <= _BURN_ABS_MAX_LINES:
        return floor, lines
    # Truly extreme cue: keep as much as the absolute cap allows.
    kept = lines[:_BURN_ABS_MAX_LINES]
    kept[-1] = kept[-1].rstrip() + "…"
    return floor, kept


def _burn_style(width: int, height: int) -> tuple[int, int, int, int, int, int, int, int]:
    """``(font, outline, shadow, margin_l, margin_r, margin_v, play_w, play_h)``."""
    w = max(320, int(width or 1920))
    h = max(240, int(height or 1080))
    font = max(10, round(h * _BURN_FONT_SIZE_RATIO))
    outline = max(1, round(font * _BURN_OUTLINE_RATIO))
    shadow = max(0, round(font * _BURN_SHADOW_RATIO))
    margin_x = max(8, round(w * _BURN_MARGIN_X_RATIO))
    margin_v = max(8, round(h * _BURN_MARGIN_V_RATIO))
    return font, outline, shadow, margin_x, margin_x, margin_v, w, h


def build_burn_ass(
    cues: list[tuple[int, int, str]],
    *,
    width: int,
    height: int,
    font_name: str = _BURN_FONT_NAME,
) -> str:
    """Render ``(start_ms, end_ms, text)`` cues as a styled ASS script.

    PlayRes is pinned to the real frame size so every ratio in
    :data:`_BURN_FONT_SIZE_RATIO` and friends is a true fraction of the
    picture — that is what keeps a 9:16 clip's captions from rendering twice as
    tall as a 16:9 clip's.
    """
    font, outline, shadow, ml, mr, mv, play_w, play_h = _burn_style(width, height)
    max_chars = max(12, int((play_w - 2 * ml) / max(1.0, font * _BURN_CHAR_WIDTH_RATIO)))
    # A cue that needs a smaller font gets its own style line, so long lines
    # shrink to fit instead of covering the frame or losing words.
    styles: dict[int, tuple[int, int]] = {}

    def _style_name(font_size: int) -> str:
        if font_size == font:
            return "Default"
        if font_size not in styles:
            scaled_outline = max(1, round(font_size * _BURN_OUTLINE_RATIO))
            scaled_shadow = max(0, round(font_size * _BURN_SHADOW_RATIO))
            styles[font_size] = (scaled_outline, scaled_shadow)
        return f"Small{font_size}"

    header = (
        "[Script Info]\n"
        "ScriptType: v4.00+\n"
        f"PlayResX: {play_w}\n"
        f"PlayResY: {play_h}\n"
        "WrapStyle: 0\n"
        "ScaledBorderAndShadow: yes\n"
        "YCbCr Matrix: TV.709\n"
        "\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, "
        "ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, "
        "MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Default,{font_name},{font},&H00FFFFFF,&H000000FF,&H00000000,"
        f"&H80000000,0,0,0,0,100,100,0,0,1,{outline},{shadow},2,{ml},{mr},{mv},1\n"
    )
    events: list[str] = []
    for start_ms, end_ms, text in cues:
        if end_ms <= start_ms:
            continue
        cleaned = _sanitize_cue_text(text)
        if not cleaned:
            continue
        cue_font, lines = _fit_cue(cleaned, base_font=font, max_chars=max_chars)
        if not lines:
            continue
        body = "\\N".join(_ass_escape(line) for line in lines)
        events.append(
            f"Dialogue: 0,{_ass_timestamp(start_ms)},{_ass_timestamp(end_ms)},"
            f"{_style_name(cue_font)},,0,0,0,,{body}\n"
        )
    for font_size, (s_outline, s_shadow) in sorted(styles.items()):
        header += (
            f"Style: Small{font_size},{font_name},{font_size},&H00FFFFFF,"
            f"&H000000FF,&H00000000,&H80000000,0,0,0,0,100,100,0,0,1,"
            f"{s_outline},{s_shadow},2,{ml},{mr},{mv},1\n"
        )
    header += (
        "\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, "
        "Effect, Text\n"
    )
    return header + "".join(events)


def _burn_pix_fmt(source_pix_fmt: str) -> str:
    """Keep the source's bit depth so HDR/10-bit footage is not silently crushed.

    Burn-in re-encodes, and forcing 8-bit while the stream still advertises
    ``bt2020nc``/``smpte2084`` produces a file players tone-map wrongly. 10-bit
    sources keep a 10-bit (or higher) format; everything else gets 8-bit
    yuv420p, which every consumer decoder handles.
    """
    fmt = str(source_pix_fmt or "").lower()
    if "10" in fmt or "12" in fmt or "16" in fmt:
        return "yuv420p10le"
    return "yuv420p"


def dub_overrun_ms(
    video_path: str | Path,
    dub_wav_path: str | Path,
    *,
    data_dir: Path | None = None,
) -> int:
    """How many ms the dub audio runs past the source video (0 when it fits).

    Callers surface this as a warning: the exported file keeps every spoken
    word, so the picture freezes for the overrun instead of the audio being cut.
    """
    info = probe_video_info(video_path, data_dir=data_dir)
    if info.duration_ms <= 0:
        return 0
    ffprobe = resolve_ffprobe(data_dir)
    if ffprobe is None:
        return 0
    res = subprocess.run(
        [
            str(ffprobe),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=nw=1:nk=1",
            str(dub_wav_path),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    with contextlib.suppress(ValueError):
        dub_ms = int(float((res.stdout or "0").strip() or 0) * 1000)
        return max(0, dub_ms - info.duration_ms)
    return 0


def _read_srt_cues(srt_path: Path) -> list[tuple[int, int, str]]:
    """Parse ``srt_path`` into ``(start_ms, end_ms, text)`` for burn-in.

    Deliberately tolerant: burn-in should not fail on a cue block the strict
    reader would reject, so malformed blocks are skipped rather than raised.
    """
    try:
        raw = srt_path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return []
    cues: list[tuple[int, int, str]] = []
    for block in raw.replace("\r\n", "\n").replace("\r", "\n").split("\n\n"):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        # The timing line may be first, or second behind the cue number.
        timing_index = next((i for i, line in enumerate(lines) if "-->" in line), -1)
        if timing_index < 0:
            continue
        start_text, _, end_text = lines[timing_index].partition("-->")
        start_ms = _parse_srt_timestamp(start_text)
        end_ms = _parse_srt_timestamp(end_text)
        if start_ms is None or end_ms is None:
            continue
        body = " ".join(" ".join(lines[timing_index + 1 :]).split())
        if not body:
            continue
        cues.append((start_ms, end_ms, body))
    return cues


def _parse_srt_timestamp(value: str) -> int | None:
    """``HH:MM:SS,mmm`` (or ``.`` milliseconds) → ms; ``None`` when unparseable."""
    text = str(value or "").strip().replace(".", ",")
    head, _, millis = text.partition(",")
    parts = head.split(":")
    if len(parts) != 3:
        return None
    try:
        hours, minutes, seconds = (int(part) for part in parts)
        fraction = int((millis.strip() or "0").ljust(3, "0")[:3])
    except ValueError:
        return None
    return ((hours * 60 + minutes) * 60 + seconds) * 1000 + fraction


def _run_ffmpeg(
    cmd: list[str],
    *,
    cwd: str | None,
    progress: Callable[[float], None] | None,
    duration_ms: int,
    cancel_check: Callable[[], bool] | None = None,
) -> subprocess.CompletedProcess[str]:
    """Run ffmpeg, optionally streaming ``-progress`` into ``progress``.

    Burn-in can run for minutes on a long clip, so the caller gets a real
    fraction rather than a silent wait. Without a ``progress`` callback and
    without ``cancel_check`` this is a plain capture run. With a
    ``cancel_check`` the process is terminated when it returns True — a hung
    ffmpeg must never permanently disable the caller.
    """
    if progress is None and cancel_check is None:
        return subprocess.run(cmd, capture_output=True, text=True, check=False, cwd=cwd)

    stderr_chunks: list[str] = []
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        cwd=cwd,
    )
    assert proc.stdout is not None  # noqa: S101 - guaranteed by PIPE

    def drain_stderr() -> None:
        # A separate reader keeps a chatty ffmpeg from filling the pipe and
        # deadlocking the stdout loop below.
        if proc.stderr is not None:
            stderr_chunks.append(proc.stderr.read())

    stderr_reader = threading.Thread(target=drain_stderr, daemon=True)
    stderr_reader.start()

    lines: queue.Queue[str | None] = queue.Queue()

    def read_stdout() -> None:
        for line in proc.stdout:  # type: ignore[union-attr]
            lines.put(line)
        lines.put(None)

    stdout_reader = threading.Thread(target=read_stdout, daemon=True)
    stdout_reader.start()

    while True:
        if cancel_check is not None and cancel_check():
            proc.terminate()
            with contextlib.suppress(subprocess.TimeoutExpired):
                proc.wait(timeout=5)
            if proc.poll() is None:
                proc.kill()
            break
        try:
            line = lines.get(timeout=0.1)
        except queue.Empty:
            continue
        if line is None:
            break
        if progress is not None:
            key, _, value = line.strip().partition("=")
            if key == "out_time_ms":
                with contextlib.suppress(ValueError):
                    done_ms = int(value)
                    if duration_ms > 0:
                        with contextlib.suppress(Exception):
                            progress(max(0.0, min(0.99, done_ms / duration_ms)))
            elif key == "progress" and value == "end":
                with contextlib.suppress(Exception):
                    progress(1.0)
    proc.wait()
    stderr_reader.join(timeout=5)
    return subprocess.CompletedProcess(cmd, proc.returncode, "", "".join(stderr_chunks))


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


def merge_duck_spans(
    spans: Sequence[tuple[int, int]],
    *,
    pad_before_ms: int = 20,
    pad_after_ms: int = 80,
    merge_gap_ms: int = 150,
    max_spans: int = 200,
) -> list[tuple[int, int]]:
    """Normalize, pad, and coalesce ``(start_ms, end_ms)`` ducking intervals."""
    padded: list[tuple[int, int]] = []
    for s_raw, e_raw in spans:
        s = int(s_raw)
        e = int(e_raw)
        if e <= s or e <= 0:
            continue
        padded.append((max(0, s - pad_before_ms), e + pad_after_ms))
    if not padded:
        return []
    padded.sort(key=lambda item: (item[0], item[1]))
    merged: list[tuple[int, int]] = [padded[0]]
    for s, e in padded[1:]:
        prev_s, prev_e = merged[-1]
        if s <= prev_e + merge_gap_ms:
            merged[-1] = (prev_s, max(prev_e, e))
        else:
            merged.append((s, e))
    while len(merged) > max_spans and len(merged) >= 2:
        best_idx = min(
            range(len(merged) - 1),
            key=lambda idx: merged[idx + 1][0] - merged[idx][1],
        )
        merged[best_idx : best_idx + 2] = [(merged[best_idx][0], merged[best_idx + 1][1])]
    return merged


def build_duck_spans(
    cues: Sequence[Any],
    placed_spans: Sequence[Any] | None = None,
    *,
    offset_ms: int = 0,
) -> list[tuple[int, int]]:
    """Compute union of original speech windows and dubbed audio windows per cue."""
    raw: list[tuple[int, int]] = []
    for i, cue in enumerate(cues):
        sp_start = getattr(cue, "effective_speech_start_ms", getattr(cue, "start_ms", 0))
        sp_end = getattr(cue, "effective_speech_end_ms", getattr(cue, "end_ms", 0))
        orig_s = max(0, int(sp_start) + offset_ms)
        orig_e = max(orig_s, int(sp_end) + offset_ms)
        s_ms, e_ms = orig_s, orig_e
        if placed_spans is not None and i < len(placed_spans):
            seg = placed_spans[i]
            dub_s = int(getattr(seg, "start_ms", orig_s))
            dub_e = int(getattr(seg, "end_ms", orig_e))
            if dub_e > dub_s:
                s_ms = min(orig_s, dub_s)
                e_ms = max(orig_e, dub_e)
        if e_ms > s_ms:
            raw.append((s_ms, e_ms))
    return merge_duck_spans(raw)


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
    subtitle_language: str = "",
    clamp_to_video: bool = False,
    duck_spans: Sequence[tuple[int, int]] | None = None,
    progress: Callable[[float], None] | None = None,
    data_dir: Path | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> Path:
    """Mux ``dub_wav_path`` onto ``video_path``.

    With ``embed_subtitles`` and an existing ``srt_path``, ``subtitle_mode``
    picks how the cues ship: ``"soft"`` adds a player-toggleable track (stream
    copy, no video re-encode; skipped for containers without one, see
    :func:`supports_soft_subtitles`), ``"burn"`` draws them into the frames via
    a styled ASS script and re-encodes the video. Otherwise the video is
    stream-copied.

    ``subtitle_language`` tags the soft track so players can auto-select it.
    ``clamp_to_video`` trims the result to the source picture's duration. It is
    OFF by default on purpose: a dub that outran the video must never lose its
    last words, so the caller decides between a frozen tail and truncated
    speech (see :func:`dub_overrun_ms`). Neither the subtitle exporter nor the
    Studio splice passes it: both keep every spoken word and surface the
    overrun as a warning instead. It stays as an API-only opt-in for callers
    that explicitly prefer a hard cut. ``progress`` is called with 0.0–1.0
    while ffmpeg runs (burn-in can take minutes). ``cancel_check`` is polled
    while ffmpeg runs; returning True terminates it and raises
    :class:`VideoDubError` (the partial file is discarded).
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

    info = probe_video_info(vpath, data_dir=data_dir) if (burn or clamp_to_video) else VideoInfo()

    # The ass filter takes a filtergraph-escaped path, and Windows drive
    # colons/backslashes make that fragile: burn from a scratch directory
    # holding plain-named copies and run ffmpeg inside it (every other path
    # above is absolute). ``fonts/`` there is what libass reads via fontsdir.
    burn_dir: Path | None = None
    burn_ass: Path | None = None
    if burn:
        burn_dir = target.with_name(f".{uuid.uuid4().hex}.burn")
        burn_dir.mkdir(parents=True, exist_ok=True)
        burn_ass = burn_dir / "sub.ass"
        if FONTS_DIR.is_dir():
            with contextlib.suppress(OSError):
                shutil.copytree(FONTS_DIR, burn_dir / "fonts")

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
    if burn_ass is not None:
        # fontsdir lets libass find the bundled Be Vietnam Pro even when the
        # host has no Vietnamese font. Both the script name and fonts dir are
        # RELATIVE to burn_dir (see below), so no Windows drive colon ever
        # reaches the filtergraph. Wrapping is controlled by WrapStyle in the
        # ASS header itself — the filter has no such option.
        style_opts = [f"filename={burn_ass.name}"]
        if FONTS_DIR.is_dir():
            style_opts.append("fontsdir=fonts")
        filters.append(f"[0:v:0]ass={':'.join(style_opts)}[vout]")
        video_out = "[vout]"
    audio_out = "1:a:0"
    if use_duck:
        if duck_spans is not None:
            raw_duck_spans = [(int(s), int(e)) for s, e in duck_spans]
        elif srt_path is not None and Path(srt_path).is_file():
            raw_duck_spans = [(s, e) for s, e, _ in _read_srt_cues(Path(srt_path))]
        else:
            raw_duck_spans = []
        merged_duck = merge_duck_spans(raw_duck_spans)
        if merged_duck:
            enable_expr = "+".join(
                f"between(t,{s / 1000.0:.3f},{e / 1000.0:.3f})" for s, e in merged_duck
            )
            filters.append(
                "[0:a:0]asplit=3[bg_src][sc_ref][sil_ref];"
                "[sil_ref]volume=0[sil];"
                f"[sc_ref]volume=volume=0:enable='not({enable_expr})'[sc_span];"
                "[1:a:0][sil]amix=inputs=2:duration=longest:dropout_transition=0:"
                "weights=1 0:normalize=0,asplit=2[dub_sc][dub];"
                "[dub_sc][sc_span]amix=inputs=2:duration=longest:dropout_transition=0:"
                "weights=1 1:normalize=0[sc];"
                f"[bg_src]volume={gain_db:.1f}dB,"
                f"volume=volume=-12.0dB:enable='{enable_expr}'[bg_att];"
                "[bg_att][sc]sidechaincompress=threshold=0.015:ratio=6:attack=20:release=300[bg];"
                "[bg][dub]amix=inputs=2:duration=longest:dropout_transition=0:"
                "weights=1 1:normalize=0[aout]"
            )
        else:
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
        language = str(subtitle_language or "").strip().lower()
        if language and language not in ("auto", "und"):
            cmd += ["-metadata:s:s:0", f"language={language[:3]}"]
            cmd += ["-metadata:s:s:0", "title=Phụ đề"]
    if burn:
        cmd += list(_BURN_VIDEO_ARGS.get(ext, _BURN_VIDEO_DEFAULT))
        # Burn-in re-encodes; keep the source's bit depth or an HDR-tagged file
        # ends up 8-bit and gets tone-mapped wrongly by every player.
        pix_fmt = _burn_pix_fmt(info.pix_fmt)
        if "-pix_fmt" in cmd:
            cmd[cmd.index("-pix_fmt") + 1] = pix_fmt
        else:
            cmd += ["-pix_fmt", pix_fmt]
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
    # A dub longer than the picture would otherwise leave a frozen tail (and a
    # container duration that lies about the video). -t caps both streams.
    if clamp_to_video and info.duration_ms > 0:
        cmd += ["-t", f"{info.duration_ms / 1000.0:.3f}"]
    if progress is not None:
        cmd += ["-progress", "pipe:1", "-nostats"]
    cmd.append(str(part))

    try:
        if burn_ass is not None and srt_path is not None:
            # The caller hands us an SRT; restyle it into ASS so libass gets an
            # explicit, resolution-matched style instead of its 16:9 defaults.
            burn_ass.write_text(
                build_burn_ass(
                    _read_srt_cues(Path(srt_path)),
                    width=info.width,
                    height=info.height,
                ),
                encoding="utf-8",
            )
        res = _run_ffmpeg(
            cmd,
            cwd=str(burn_dir) if burn_dir is not None else None,
            progress=progress,
            duration_ms=info.duration_ms,
            cancel_check=cancel_check,
        )
        if cancel_check is not None and cancel_check():
            raise VideoDubError("Đã hủy xuất video.")
        if res.returncode != 0 or not part.is_file() or part.stat().st_size == 0:
            stderr = res.stderr or ""
            if burn and "No such filter: 'ass'" in stderr:
                raise VideoDubError(
                    "Bản ffmpeg hiện tại không hỗ trợ in phụ đề lên hình (thiếu libass). "
                    "Hãy cài bản ffmpeg đầy đủ hoặc chọn kiểu phụ đề bật/tắt trong trình phát."
                )
            detail = stderr.strip().splitlines()[-1:] or ["ffmpeg mux failed"]
            raise VideoDubError(f"Không thể ghép âm thanh vào video '{vpath.name}': {detail[0]}")
        if progress is not None:
            with contextlib.suppress(Exception):
                progress(1.0)
        return _promote_atomic(part, target)
    except Exception:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
        raise
    finally:
        if burn_dir is not None:
            with contextlib.suppress(OSError):
                shutil.rmtree(burn_dir, ignore_errors=True)
