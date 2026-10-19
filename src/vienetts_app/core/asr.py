"""On-device and OpenAI-compatible Whisper ASR for video/audio subtitle generation.

Supports:
1. ``faster_whisper`` (local CTranslate2 Whisper with VAD filtering).
2. ``whisper_cpp`` (local ``whisper-cli`` / ``whisper-cpp`` binary producing ``.srt``).
3. ``openai_api`` (any OpenAI-compatible ``/v1/audio/transcriptions`` endpoint,
   including local ``whisper.cpp --server``, Speaches/LocalAI, or cloud APIs).
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from vienetts_app.core.subtitles import Cue, clean_cue_text, parse_cues
from vienetts_app.core.video_dub import VideoDubError, extract_video_audio

_CLAUSE_SPLIT_RE = re.compile(r"(?<=[.!?…。！？,;，；:：])\s+")


class AsrError(RuntimeError):
    """Speech recognition failed or no ASR backend is available; message is user-actionable."""


@dataclass(frozen=True)
class AsrConfig:
    backend: str = "auto"  # "auto" | "faster_whisper" | "whisper_cpp" | "openai_api"
    model: str = "base"
    language: str = "auto"
    api_base_url: str = ""
    api_key: str = ""


@dataclass(frozen=True)
class RawSegment:
    start_ms: int
    end_ms: int
    text: str
    speaker: str = ""


def _split_long_segment(
    seg: RawSegment,
    *,
    max_chars: int,
    max_duration_ms: int,
) -> list[RawSegment]:
    text = clean_cue_text(seg.text)
    if not text:
        return []
    duration = max(1, seg.end_ms - seg.start_ms)
    if len(text) <= max_chars and duration <= max_duration_ms:
        return [RawSegment(seg.start_ms, max(seg.start_ms + 1, seg.end_ms), text, seg.speaker)]

    # Split at clause boundaries first, then word boundaries if still over max_chars.
    clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(text) if c.strip()]
    chunks: list[str] = []
    current = ""
    for clause in clauses:
        if len(clause) > max_chars:
            if current:
                chunks.append(current)
                current = ""
            words = clause.split()
            buf = ""
            for w in words:
                cand = f"{buf} {w}".strip() if buf else w
                if len(cand) > max_chars and buf:
                    chunks.append(buf)
                    buf = w
                else:
                    buf = cand
            if buf:
                chunks.append(buf)
        elif not current:
            current = clause
        elif len(current) + 1 + len(clause) <= max_chars:
            current = f"{current} {clause}"
        else:
            chunks.append(current)
            current = clause
    if current:
        chunks.append(current)

    if len(chunks) <= 1:
        return [RawSegment(seg.start_ms, max(seg.start_ms + 1, seg.end_ms), text, seg.speaker)]

    total_chars = max(1, sum(len(c) for c in chunks))
    out: list[RawSegment] = []
    cursor_ms = seg.start_ms
    for i, chunk in enumerate(chunks):
        if i == len(chunks) - 1:
            end_ms = max(cursor_ms + 1, seg.end_ms)
        else:
            span = max(1, int(round(duration * len(chunk) / total_chars)))
            end_ms = min(seg.end_ms, cursor_ms + span)
        out.append(RawSegment(cursor_ms, max(cursor_ms + 1, end_ms), chunk, seg.speaker))
        cursor_ms = end_ms
    return out


def normalize_asr_segments_to_cues(
    segments: Sequence[RawSegment],
    *,
    max_chars: int = 84,
    max_duration_ms: int = 7000,
) -> list[Cue]:
    """Clean, merge micro-fragments, split overlong segments, and return numbered ``Cue``s."""
    cleaned: list[RawSegment] = []
    for seg in segments:
        txt = clean_cue_text(seg.text)
        if not txt:
            continue
        s_ms = max(0, int(seg.start_ms))
        e_ms = max(s_ms + 1, int(seg.end_ms))
        cleaned.append(RawSegment(s_ms, e_ms, txt, str(seg.speaker or "").strip()))

    if not cleaned:
        return []

    # Merge micro-fragments (<400 ms) into the following or preceding segment of the same speaker
    merged: list[RawSegment] = []
    i = 0
    while i < len(cleaned):
        curr = cleaned[i]
        dur = curr.end_ms - curr.start_ms
        if (
            dur < 400
            and i + 1 < len(cleaned)
            and cleaned[i + 1].speaker == curr.speaker
            and (cleaned[i + 1].start_ms - curr.end_ms) <= 150
            and len(curr.text) + 1 + len(cleaned[i + 1].text) <= max_chars
        ):
            nxt = cleaned[i + 1]
            cleaned[i + 1] = RawSegment(
                curr.start_ms,
                nxt.end_ms,
                f"{curr.text} {nxt.text}",
                curr.speaker,
            )
            i += 1
            continue
        merged.append(curr)
        i += 1

    split_all: list[RawSegment] = []
    for seg in merged:
        split_all.extend(
            _split_long_segment(seg, max_chars=max_chars, max_duration_ms=max_duration_ms)
        )

    cues: list[Cue] = []
    prev_end = 0
    for idx, seg in enumerate(split_all, start=1):
        start_ms = max(prev_end, seg.start_ms)
        end_ms = max(start_ms + 1, seg.end_ms)
        cues.append(Cue(idx, start_ms, end_ms, seg.text, speaker=seg.speaker))
        prev_end = end_ms
    return cues


def _run_faster_whisper(
    wav_path: Path,
    config: AsrConfig,
    *,
    progress_cb: Callable[[float, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> list[RawSegment]:
    try:
        import faster_whisper  # type: ignore[import-not-found]
    except Exception as exc:
        raise AsrError(
            "Chưa cài đặt faster-whisper trên môi trường này. "
            "Hãy cài đặt faster-whisper hoặc chọn máy chủ Whisper API trong Cài đặt."
        ) from exc

    model_name = (config.model or "base").strip() or "base"
    lang = None if not config.language or config.language == "auto" else config.language.strip()
    if progress_cb is not None:
        progress_cb(0.1, f"Đang tải mô hình Whisper ({model_name})…")

    try:
        model = faster_whisper.WhisperModel(model_name, device="auto", compute_type="int8")
        segments_iter, info = model.transcribe(
            str(wav_path),
            language=lang,
            vad_filter=True,
        )
        total_s = float(getattr(info, "duration", 0.0) or 0.0)
        raw_segments: list[RawSegment] = []
        for seg in segments_iter:
            if cancel_check is not None and cancel_check():
                raise AsrError("Đã hủy nhận diện giọng nói.")
            start_s = float(getattr(seg, "start", 0.0) or 0.0)
            end_s = float(getattr(seg, "end", start_s + 0.5) or (start_s + 0.5))
            txt = str(getattr(seg, "text", "") or "").strip()
            if txt:
                raw_segments.append(
                    RawSegment(int(round(start_s * 1000)), int(round(end_s * 1000)), txt)
                )
            if progress_cb is not None and total_s > 0:
                progress_cb(
                    min(0.95, 0.15 + 0.8 * (end_s / total_s)),
                    "Đang nhận diện phụ đề (Whisper)…",
                )
    except AsrError:
        raise
    except Exception as exc:
        raise AsrError(f"Lỗi khi chạy faster-whisper: {exc}") from exc

    if progress_cb is not None:
        progress_cb(1.0, "Hoàn tất nhận diện giọng nói.")
    return raw_segments


def _resolve_whisper_cpp_bin(data_dir: Path | None = None) -> Path | None:
    names = ("whisper-cli.exe", "main.exe") if os.name == "nt" else ("whisper-cli", "whisper-cpp")
    if data_dir is not None:
        for name in names:
            cand = Path(data_dir) / "runtimes" / "whisper" / "bin" / name
            if cand.is_file():
                return cand
    for name in names:
        found = shutil.which(name)
        if found:
            return Path(found)
    return None


def _run_whisper_cpp(
    wav_path: Path,
    config: AsrConfig,
    *,
    data_dir: Path | None = None,
    progress_cb: Callable[[float, str], None] | None = None,
) -> list[RawSegment]:
    binary = _resolve_whisper_cpp_bin(data_dir)
    if binary is None:
        raise AsrError("Không tìm thấy thực thi whisper-cli (whisper.cpp).")
    model_path = Path(config.model)
    if not model_path.is_file() and data_dir is not None:
        cand = Path(data_dir) / "models" / "whisper" / f"ggml-{config.model}.bin"
        if cand.is_file():
            model_path = cand
    if not model_path.is_file():
        raise AsrError(f"Không tìm thấy tệp mô hình whisper.cpp: {config.model}")

    if progress_cb is not None:
        progress_cb(0.2, "Đang chạy whisper.cpp…")
    out_prefix = wav_path.parent / f".whisper_out_{uuid.uuid4().hex}"
    srt_out = Path(f"{out_prefix}.srt")
    lang = "auto" if not config.language else config.language
    cmd = [
        str(binary),
        "-m",
        str(model_path),
        "-f",
        str(wav_path),
        "-l",
        lang,
        "-osrt",
        "-of",
        str(out_prefix),
    ]
    try:
        res = subprocess.run(cmd, capture_output=True, text=True, check=False)
        if res.returncode != 0 or not srt_out.is_file():
            detail = (res.stderr or "").strip().splitlines()[-1:] or ["whisper-cli failed"]
            raise AsrError(f"whisper.cpp thất bại: {detail[0]}")
        cues = parse_cues(srt_out.read_text(encoding="utf-8", errors="replace"))
        if progress_cb is not None:
            progress_cb(1.0, "Hoàn tất nhận diện giọng nói.")
        return [RawSegment(c.start_ms, c.end_ms, c.text, c.speaker) for c in cues]
    finally:
        with contextlib.suppress(OSError):
            srt_out.unlink(missing_ok=True)


def _run_openai_whisper_api(
    wav_path: Path,
    config: AsrConfig,
    *,
    progress_cb: Callable[[float, str], None] | None = None,
) -> list[RawSegment]:
    base_url = (config.api_base_url or "http://localhost:11434/v1").strip().rstrip("/")
    if not base_url:
        raise AsrError("Chưa cấu hình địa chỉ máy chủ Whisper API.")
    url = f"{base_url}/audio/transcriptions"
    boundary = f"----VieNeuAsrBoundary{uuid.uuid4().hex}"
    model_name = (config.model or "whisper-1").strip() or "whisper-1"

    parts: list[bytes] = []

    def _add_field(name: str, value: str) -> None:
        parts.append(
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"\r\n\r\n'
            f"{value}\r\n".encode()
        )

    _add_field("model", model_name)
    _add_field("response_format", "verbose_json")
    if config.language and config.language != "auto":
        _add_field("language", config.language)

    audio_bytes = wav_path.read_bytes()
    parts.append(
        f"--{boundary}\r\n"
        f'Content-Disposition: form-data; name="file"; filename="{wav_path.name}"\r\n'
        f"Content-Type: audio/wav\r\n\r\n".encode()
        + audio_bytes
        + b"\r\n"
        + f"--{boundary}--\r\n".encode()
    )
    body = b"".join(parts)

    headers = {
        "Content-Type": f"multipart/form-data; boundary={boundary}",
        "Accept": "application/json",
    }
    if config.api_key.strip():
        headers["Authorization"] = f"Bearer {config.api_key.strip()}"

    if progress_cb is not None:
        progress_cb(0.3, "Đang gửi âm thanh tới máy chủ ASR…")

    req = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=120) as resp:  # noqa: S310
            raw_resp = resp.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise AsrError(f"Không thể kết nối máy chủ ASR ({url}): {exc}") from exc

    if "-->" in raw_resp:
        cues = parse_cues(raw_resp)
        if progress_cb is not None:
            progress_cb(1.0, "Hoàn tất nhận diện giọng nói.")
        return [RawSegment(c.start_ms, c.end_ms, c.text, c.speaker) for c in cues]

    try:
        payload = json.loads(raw_resp)
    except json.JSONDecodeError as exc:
        raise AsrError(f"Phản hồi từ máy chủ ASR không hợp lệ: {exc}") from exc

    segments = payload.get("segments")
    out: list[RawSegment] = []
    if isinstance(segments, list):
        for item in segments:
            if not isinstance(item, dict):
                continue
            txt = str(item.get("text") or "").strip()
            if not txt:
                continue
            start_ms = int(round(float(item.get("start", 0.0)) * 1000))
            end_ms = int(round(float(item.get("end", start_ms / 1000.0 + 1.0)) * 1000))
            spk = str(item.get("speaker") or "").strip()
            out.append(RawSegment(start_ms, max(start_ms + 1, end_ms), txt, spk))
    elif isinstance(payload.get("text"), str) and payload["text"].strip():
        out.append(RawSegment(0, 5000, payload["text"].strip()))

    if progress_cb is not None:
        progress_cb(1.0, "Hoàn tất nhận diện giọng nói.")
    return out


def transcribe_media_to_cues(
    media_path: str | Path,
    config: AsrConfig | None = None,
    *,
    work_dir: Path | None = None,
    data_dir: Path | None = None,
    progress_cb: Callable[[float, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> list[Cue]:
    """Extract 16 kHz audio from ``media_path``, run Whisper ASR, and return normalized ``Cue``s."""
    cfg = config or AsrConfig()
    src = Path(media_path)
    if not src.is_file():
        raise AsrError(f"Không tìm thấy tệp phương tiện: {src.name}")

    temp_dir_obj: tempfile.TemporaryDirectory[str] | None = None
    try:
        if src.suffix.lower() == ".wav":
            wav_path = src
        else:
            if work_dir is not None:
                work_dir.mkdir(parents=True, exist_ok=True)
                wav_path = work_dir / f".asr_{uuid.uuid4().hex}.wav"
            else:
                temp_dir_obj = tempfile.TemporaryDirectory(prefix="vienetts_asr_")
                wav_path = Path(temp_dir_obj.name) / "extracted_16k.wav"
            if progress_cb is not None:
                progress_cb(0.05, "Đang trích xuất âm thanh từ video…")
            try:
                extract_video_audio(src, wav_path, sample_rate=16_000, mono=True, data_dir=data_dir)
            except VideoDubError as exc:
                raise AsrError(str(exc)) from exc

        if cancel_check is not None and cancel_check():
            raise AsrError("Đã hủy nhận diện giọng nói.")

        backend = (cfg.backend or "auto").strip()
        if backend == "faster_whisper":
            raw = _run_faster_whisper(
                wav_path, cfg, progress_cb=progress_cb, cancel_check=cancel_check
            )
        elif backend == "whisper_cpp":
            raw = _run_whisper_cpp(wav_path, cfg, data_dir=data_dir, progress_cb=progress_cb)
        elif backend == "openai_api":
            raw = _run_openai_whisper_api(wav_path, cfg, progress_cb=progress_cb)
        else:
            # auto: try faster_whisper -> whisper_cpp -> openai_api
            last_err: Exception | None = None
            raw = []
            for runner in (
                lambda: _run_faster_whisper(
                    wav_path, cfg, progress_cb=progress_cb, cancel_check=cancel_check
                ),
                lambda: _run_whisper_cpp(wav_path, cfg, data_dir=data_dir, progress_cb=progress_cb),
                lambda: (
                    _run_openai_whisper_api(wav_path, cfg, progress_cb=progress_cb)
                    if cfg.api_base_url.strip()
                    else (_ for _ in ()).throw(AsrError("Chưa cấu hình ASR API"))
                ),
            ):
                try:
                    raw = runner()
                    last_err = None
                    break
                except AsrError as exc:
                    last_err = exc
            if last_err is not None and not raw:
                raise AsrError(
                    "Không tìm thấy bộ nhận diện giọng nói (ASR) khả dụng. "
                    "Hãy cài đặt faster-whisper / whisper.cpp hoặc cấu hình ASR trong Cài đặt."
                ) from last_err

        cues = normalize_asr_segments_to_cues(raw)
        if not cues:
            raise AsrError("Không nhận diện được lời thoại nào trong tệp video/âm thanh.")
        return cues
    finally:
        if temp_dir_obj is not None:
            with contextlib.suppress(OSError):
                temp_dir_obj.cleanup()
