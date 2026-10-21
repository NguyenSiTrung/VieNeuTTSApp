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
import subprocess
import tempfile
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np

from vienetts_app.core.ai_models import resolve_asr_model_path, resolve_whisper_cpp_cli
from vienetts_app.core.audio import write_wav_file
from vienetts_app.core.subtitles import NON_SPEECH_LABELS, Cue, clean_cue_text, parse_cues
from vienetts_app.core.video_dub import VideoDubError, extract_video_audio

_CLAUSE_SPLIT_RE = re.compile(r"(?<=[.!?…。！？,;，；:：])\s+")
_WHISPER_PP_RE = re.compile(r"progress\s*=\s*(\d+)\s*%", re.IGNORECASE)
_NON_SPEECH_TOKEN_RE = re.compile(
    r"(?:\[\s*(?:blank_audio|music|applause|laughter|silence|noise|inaudible|sound|sfx|cheering|cough|sigh)\s*\]"
    r"|\(\s*(?:blank_audio|music|applause|laughter|silence|noise|inaudible|sound|sfx|cheering|cough|sigh)\s*\)"
    r"|[♪♫]+)",
    re.IGNORECASE,
)
MODEL_SAMPLE_RATE = 16_000
SHARED_WAV_FILENAME = "source_16k.wav"
WAV_INPUT_16K_FILENAME = "input_16k.wav"
ASR_CACHE_FILENAME = "asr_segments_cache.json"
_DEFAULT_OLLAMA_URLS = frozenset(
    {
        "http://localhost:11434/v1",
        "http://127.0.0.1:11434/v1",
    }
)
_FASTER_WHISPER_CACHE: dict[tuple[Any, str, str, str, int], Any] = {}


class AsrError(RuntimeError):
    """Speech recognition failed or no ASR backend is available; message is user-actionable."""


class AsrCancelled(AsrError):
    """The user cancelled speech recognition; auto fallback must never swallow this."""


@dataclass(frozen=True)
class AsrConfig:
    backend: str = "auto"  # "auto" | "faster_whisper" | "whisper_cpp" | "openai_api"
    model: str = "base"
    language: str = "auto"
    api_base_url: str = ""
    api_key: str = ""
    device: str = "auto"  # "auto" | "cpu" | "cuda" | "metal"
    threads: int = 0  # 0 = automatic (min(16, os.cpu_count() or 4))
    timeout_s: float = 0.0  # 0 = derive from audio duration


@dataclass(frozen=True)
class RawSegment:
    start_ms: int
    end_ms: int
    text: str
    speaker: str = ""


def _strip_non_speech(text: str) -> str:
    stripped = _NON_SPEECH_TOKEN_RE.sub(" ", text or "")
    cleaned = clean_cue_text(stripped)
    if not cleaned or not any(ch.isalnum() for ch in cleaned):
        return ""
    return cleaned


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

    eff_max_chars = max_chars
    if max_duration_ms > 0 and duration > max_duration_ms:
        eff_max_chars = min(max_chars, max(12, int(len(text) * max_duration_ms / duration)))

    # Split at clause boundaries first, then word boundaries if still over eff_max_chars.
    clauses = [c.strip() for c in _CLAUSE_SPLIT_RE.split(text) if c.strip()]
    chunks: list[str] = []
    current = ""
    for clause in clauses:
        if len(clause) > eff_max_chars:
            if current:
                chunks.append(current)
                current = ""
            words = clause.split()
            buf = ""
            for w in words:
                cand = f"{buf} {w}".strip() if buf else w
                if len(cand) > eff_max_chars and buf:
                    chunks.append(buf)
                    buf = w
                else:
                    buf = cand
            if buf:
                chunks.append(buf)
        elif not current:
            current = clause
        elif len(current) + 1 + len(clause) <= eff_max_chars:
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
        txt = _strip_non_speech(seg.text)
        if not txt:
            continue
        raw_spk = str(seg.speaker or "").strip()
        norm_spk = raw_spk.upper().replace(" ", "_").replace("-", "_")
        spk = "" if norm_spk in NON_SPEECH_LABELS else raw_spk
        s_ms = max(0, int(seg.start_ms))
        e_ms = max(s_ms + 1, int(seg.end_ms))
        cleaned.append(RawSegment(s_ms, e_ms, txt, spk))

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
        if (
            dur < 400
            and merged
            and merged[-1].speaker == curr.speaker
            and (curr.start_ms - merged[-1].end_ms) <= 150
            and len(merged[-1].text) + 1 + len(curr.text) <= max_chars
        ):
            prev = merged[-1]
            merged[-1] = RawSegment(
                prev.start_ms,
                curr.end_ms,
                f"{prev.text} {curr.text}",
                prev.speaker,
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


def _resolve_threads(config: AsrConfig) -> int:
    if int(config.threads) > 0:
        return max(1, min(64, int(config.threads)))
    return max(1, min(16, int(os.cpu_count() or 4)))


def _wav_duration_s(wav_path: Path) -> float:
    try:
        import soundfile as sf

        info = sf.info(str(wav_path))
        sr = max(1, int(info.samplerate))
        return max(0.0, float(info.frames) / float(sr))
    except Exception:  # noqa: BLE001
        return 0.0


def _run_faster_whisper(
    wav_path: Path,
    config: AsrConfig,
    *,
    progress_cb: Callable[[float, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> list[RawSegment]:
    if cancel_check is not None and cancel_check():
        raise AsrCancelled("Đã hủy nhận diện giọng nói.")
    try:
        import faster_whisper  # type: ignore[import-not-found]
    except Exception as exc:
        raise AsrError(
            "Chưa cài đặt faster-whisper trên môi trường này. "
            "Hãy cài đặt faster-whisper hoặc chọn máy chủ Whisper API trong Cài đặt."
        ) from exc

    model_name = (config.model or "base").strip() or "base"
    lang = None if not config.language or config.language == "auto" else config.language.strip()
    req_dev = (config.device or "auto").strip().lower()
    fw_device = req_dev if req_dev in ("cpu", "cuda") else "auto"
    compute_type = "int8_float16" if fw_device == "cuda" else "int8"
    threads = _resolve_threads(config)
    if progress_cb is not None:
        progress_cb(0.1, f"Đang tải mô hình Whisper ({model_name})…")

    try:
        cache_key = (faster_whisper.WhisperModel, model_name, fw_device, compute_type, threads)
        model = _FASTER_WHISPER_CACHE.get(cache_key)
        if model is None:
            _FASTER_WHISPER_CACHE.clear()
            model = faster_whisper.WhisperModel(
                model_name,
                device=fw_device,
                compute_type=compute_type,
                cpu_threads=threads,
            )
            _FASTER_WHISPER_CACHE[cache_key] = model
        segments_iter, info = model.transcribe(
            str(wav_path),
            language=lang,
            vad_filter=True,
            condition_on_previous_text=False,
        )
        total_s = float(getattr(info, "duration", 0.0) or 0.0)
        raw_segments: list[RawSegment] = []
        for seg in segments_iter:
            if cancel_check is not None and cancel_check():
                raise AsrCancelled("Đã hủy nhận diện giọng nói.")
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
    return resolve_whisper_cpp_cli(data_dir)


def _run_whisper_cpp(
    wav_path: Path,
    config: AsrConfig,
    *,
    data_dir: Path | None = None,
    progress_cb: Callable[[float, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> list[RawSegment]:
    if cancel_check is not None and cancel_check():
        raise AsrCancelled("Đã hủy nhận diện giọng nói.")
    binary = _resolve_whisper_cpp_bin(data_dir)
    if binary is None:
        raise AsrError("Không tìm thấy thực thi whisper-cli (whisper.cpp).")
    model_path = resolve_asr_model_path(config.model, data_dir)
    if model_path is None or not model_path.is_file():
        raise AsrError(f"Không tìm thấy tệp mô hình whisper.cpp: {config.model}")

    if progress_cb is not None:
        progress_cb(0.2, "Đang chạy whisper.cpp…")
    out_prefix = wav_path.parent / f".whisper_out_{uuid.uuid4().hex}"
    srt_out = Path(f"{out_prefix}.srt")
    lang = "auto" if not config.language else config.language
    threads = _resolve_threads(config)
    cmd = [
        str(binary),
        "-m",
        str(model_path),
        "-f",
        str(wav_path),
        "-l",
        lang,
        "-t",
        str(threads),
        "-sns",
        "-pp",
        "-osrt",
        "-of",
        str(out_prefix),
    ]
    if (config.device or "auto").strip().lower() == "cpu":
        cmd.append("-ng")

    duration_s = _wav_duration_s(wav_path)
    timeout_s = (
        float(config.timeout_s)
        if float(config.timeout_s) > 0
        else max(180.0, duration_s * 6.0 + 60.0)
    )
    started = time.monotonic()
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []

    def _read_pipe(stream: Any, sink: list[str], *, parse_progress: bool = False) -> None:
        if stream is None:
            return
        try:
            for line in stream:
                sink.append(line)
                if parse_progress and progress_cb is not None:
                    m = _WHISPER_PP_RE.search(line)
                    if m is not None:
                        pct = min(100, max(0, int(m.group(1))))
                        progress_cb(
                            min(0.95, 0.2 + 0.75 * (pct / 100.0)),
                            "Đang chạy whisper.cpp…",
                        )
        except Exception:  # noqa: BLE001
            pass
        finally:
            with contextlib.suppress(Exception):
                stream.close()

    try:
        proc = subprocess.Popen(
            cmd,
            cwd=str(binary.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        t_out = threading.Thread(target=_read_pipe, args=(proc.stdout, stdout_lines), daemon=True)
        t_err = threading.Thread(
            target=_read_pipe,
            args=(proc.stderr, stderr_lines),
            kwargs={"parse_progress": True},
            daemon=True,
        )
        t_out.start()
        t_err.start()
        while True:
            if cancel_check is not None and cancel_check():
                proc.kill()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=1.0)
                t_out.join(timeout=0.5)
                t_err.join(timeout=0.5)
                raise AsrCancelled("Đã hủy nhận diện giọng nói.")
            if time.monotonic() - started > timeout_s:
                proc.kill()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=1.0)
                t_out.join(timeout=0.5)
                t_err.join(timeout=0.5)
                raise AsrError(
                    f"whisper.cpp quá thời gian chờ ({timeout_s:.1f}s) — "
                    "hãy thử mô hình nhẹ hơn hoặc chọn thiết bị CPU."
                )
            try:
                proc.wait(timeout=0.05)
                break
            except subprocess.TimeoutExpired:
                continue
        t_out.join(timeout=1.0)
        t_err.join(timeout=1.0)
        stderr_text = "".join(stderr_lines)
        stdout_text = "".join(stdout_lines)
        if proc.returncode != 0:
            detail = (stderr_text or stdout_text).strip().splitlines()[-1:] or [
                "whisper-cli failed"
            ]
            raise AsrError(f"whisper.cpp thất bại: {detail[0]}")
        if not srt_out.is_file() or srt_out.stat().st_size == 0:
            if progress_cb is not None:
                progress_cb(1.0, "Hoàn tất nhận diện giọng nói.")
            return []
        try:
            cues = parse_cues(srt_out.read_text(encoding="utf-8", errors="replace"))
        except Exception as exc:
            raise AsrError(f"Tệp phụ đề từ whisper.cpp không hợp lệ: {exc}") from exc
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
    cancel_check: Callable[[], bool] | None = None,
) -> list[RawSegment]:
    if cancel_check is not None and cancel_check():
        raise AsrCancelled("Đã hủy nhận diện giọng nói.")
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

    if cancel_check is not None and cancel_check():
        raise AsrCancelled("Đã hủy nhận diện giọng nói.")

    stripped = raw_resp.lstrip()
    is_json = stripped.startswith("{") or stripped.startswith("[")
    if not is_json and "-->" in raw_resp:
        try:
            cues = parse_cues(raw_resp)
        except Exception as exc:
            raise AsrError(f"Phản hồi SRT từ máy chủ ASR không hợp lệ: {exc}") from exc
        if progress_cb is not None:
            progress_cb(1.0, "Hoàn tất nhận diện giọng nói.")
        return [RawSegment(c.start_ms, c.end_ms, c.text, c.speaker) for c in cues]

    try:
        payload = json.loads(raw_resp)
    except json.JSONDecodeError as exc:
        raise AsrError(f"Phản hồi từ máy chủ ASR không hợp lệ: {exc}") from exc

    segments = payload.get("segments") if isinstance(payload, dict) else None
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
    elif (
        isinstance(payload, dict)
        and isinstance(payload.get("text"), str)
        and payload["text"].strip()
    ):
        dur_s = 0.0
        with contextlib.suppress(TypeError, ValueError):
            dur_s = float(payload.get("duration") or 0.0)
        if dur_s <= 0.0:
            dur_s = _wav_duration_s(wav_path)
        if dur_s <= 0.0:
            dur_s = 5.0
        dur_ms = max(1000, int(round(dur_s * 1000)))
        out.append(RawSegment(0, dur_ms, payload["text"].strip()))

    if progress_cb is not None:
        progress_cb(1.0, "Hoàn tất nhận diện giọng nói.")
    return out


def _is_16k_mono_wav(path: Path) -> bool:
    try:
        import soundfile as sf

        info = sf.info(str(path))
    except Exception:  # noqa: BLE001
        return False
    return int(info.samplerate) == MODEL_SAMPLE_RATE and int(info.channels) == 1


def _convert_wav_to_16k_mono(src: Path, target: Path) -> bool:
    try:
        import soundfile as sf

        data, sr = sf.read(str(src), dtype="float32", always_2d=True)
        mono = np.mean(data, axis=1, dtype=np.float32) if data.shape[1] > 1 else data[:, 0]
        if int(sr) != MODEL_SAMPLE_RATE and mono.size > 0:
            out_len = max(1, int(round(mono.size * MODEL_SAMPLE_RATE / float(sr))))
            x_old = np.linspace(0.0, 1.0, mono.size, endpoint=False)
            x_new = np.linspace(0.0, 1.0, out_len, endpoint=False)
            mono = np.interp(x_new, x_old, mono).astype(np.float32)
        write_wav_file(mono, target, sample_rate=MODEL_SAMPLE_RATE)
        return True
    except Exception:  # noqa: BLE001
        return False


def _ensure_16k_wav(
    src: Path,
    work_dir: Path | None,
    data_dir: Path | None,
    temp_dir: tempfile.TemporaryDirectory[str] | None,
    *,
    progress_cb: Callable[[float, str], None] | None = None,
) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    """Return a 16 kHz mono WAV for ``src``, converting and caching it with a sidecar."""
    if src.suffix.lower() == ".wav" and _is_16k_mono_wav(src):
        return src, temp_dir

    filename = SHARED_WAV_FILENAME if src.suffix.lower() != ".wav" else WAV_INPUT_16K_FILENAME
    if work_dir is not None:
        work_dir.mkdir(parents=True, exist_ok=True)
        target = work_dir / filename
    else:
        temp_dir = tempfile.TemporaryDirectory(prefix="vienetts_asr_")
        target = Path(temp_dir.name) / "extracted_16k.wav"

    sidecar = target.with_name(target.name + ".json")
    try:
        src_stat = src.stat()
    except OSError:
        src_stat = None
    expected = None
    if src_stat is not None:
        expected = {
            "source": str(src.resolve()),
            "size": src_stat.st_size,
            "mtime_ns": src_stat.st_mtime_ns,
        }
    reusable = False
    if target.is_file() and target.stat().st_size > 0 and _is_16k_mono_wav(target):
        try:
            stored = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            stored = None
        if isinstance(stored, dict) and expected is not None:
            reusable = all(stored.get(k) == v for k, v in expected.items())
        elif src_stat is not None:
            reusable = target.stat().st_mtime >= src_stat.st_mtime
    if reusable:
        return target, temp_dir

    if progress_cb is not None:
        progress_cb(0.05, "Đang trích xuất âm thanh từ video…")
    if src.suffix.lower() != ".wav" or not _convert_wav_to_16k_mono(src, target):
        try:
            extract_video_audio(
                src, target, sample_rate=MODEL_SAMPLE_RATE, mono=True, data_dir=data_dir
            )
        except VideoDubError as exc:
            raise AsrError(str(exc)) from exc
    if expected is not None:
        with contextlib.suppress(OSError):
            sidecar.write_text(json.dumps(expected), encoding="utf-8")
    return target, temp_dir


def _asr_cache_key(wav_path: Path, config: AsrConfig) -> str:
    try:
        st = wav_path.stat()
    except OSError:
        return ""
    lang = (config.language or "auto").strip() or "auto"
    model = (config.model or "base").strip() or "base"
    backend = (config.backend or "auto").strip() or "auto"
    return f"{wav_path.resolve()}:{st.st_size}:{st.st_mtime_ns}:{backend}:{model}:{lang}"


def _load_cached_segments(work_dir: Path | None, key: str) -> list[RawSegment] | None:
    if work_dir is None or not key:
        return None
    path = work_dir / ASR_CACHE_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("key") != key:
        return None
    segs = data.get("segments")
    if not isinstance(segs, list) or not segs:
        return None
    out: list[RawSegment] = []
    for item in segs:
        if not isinstance(item, dict):
            continue
        txt = str(item.get("text") or "").strip()
        if not txt:
            continue
        try:
            s_ms = max(0, int(item.get("start_ms", 0)))
            e_ms = max(s_ms + 1, int(item.get("end_ms", s_ms + 1)))
        except (TypeError, ValueError):
            continue
        out.append(RawSegment(s_ms, e_ms, txt, str(item.get("speaker") or "").strip()))
    return out or None


def _store_cached_segments(work_dir: Path | None, key: str, segments: Sequence[RawSegment]) -> None:
    if work_dir is None or not key or not segments:
        return
    raw = [
        {
            "start_ms": s.start_ms,
            "end_ms": s.end_ms,
            "text": s.text,
            "speaker": s.speaker,
        }
        for s in segments
    ]
    with contextlib.suppress(OSError):
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / ASR_CACHE_FILENAME).write_text(
            json.dumps({"key": key, "segments": raw}, ensure_ascii=False), encoding="utf-8"
        )


def transcribe_media_to_cues(
    media_path: str | Path,
    config: AsrConfig | None = None,
    *,
    work_dir: Path | None = None,
    data_dir: Path | None = None,
    progress_cb: Callable[[float, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    notes: list[str] | None = None,
) -> list[Cue]:
    """Extract 16 kHz audio from ``media_path``, run Whisper ASR, and return normalized ``Cue``s."""
    if cancel_check is not None and cancel_check():
        raise AsrCancelled("Đã hủy nhận diện giọng nói.")
    cfg = config or AsrConfig()
    src = Path(media_path)
    if not src.is_file():
        raise AsrError(f"Không tìm thấy tệp phương tiện: {src.name}")

    temp_dir_obj: tempfile.TemporaryDirectory[str] | None = None
    try:
        wav_path, temp_dir_obj = _ensure_16k_wav(
            src, work_dir, data_dir, None, progress_cb=progress_cb
        )
        if cancel_check is not None and cancel_check():
            raise AsrCancelled("Đã hủy nhận diện giọng nói.")

        cache_key = _asr_cache_key(wav_path, cfg)
        cached = _load_cached_segments(work_dir, cache_key)
        if cached is not None:
            if progress_cb is not None:
                progress_cb(1.0, "Hoàn tất nhận diện giọng nói.")
            raw = cached
        else:
            backend = (cfg.backend or "auto").strip()
            if backend == "faster_whisper":
                raw = _run_faster_whisper(
                    wav_path, cfg, progress_cb=progress_cb, cancel_check=cancel_check
                )
            elif backend == "whisper_cpp":
                raw = _run_whisper_cpp(
                    wav_path,
                    cfg,
                    data_dir=data_dir,
                    progress_cb=progress_cb,
                    cancel_check=cancel_check,
                )
            elif backend == "openai_api":
                raw = _run_openai_whisper_api(
                    wav_path, cfg, progress_cb=progress_cb, cancel_check=cancel_check
                )
            else:
                whisper_cpp_ready = (
                    _resolve_whisper_cpp_bin(data_dir) is not None
                    and resolve_asr_model_path(cfg.model, data_dir) is not None
                )
                fw_entry = (
                    "faster-whisper",
                    lambda: _run_faster_whisper(
                        wav_path, cfg, progress_cb=progress_cb, cancel_check=cancel_check
                    ),
                )
                wcpp_entry = (
                    "whisper.cpp",
                    lambda: _run_whisper_cpp(
                        wav_path,
                        cfg,
                        data_dir=data_dir,
                        progress_cb=progress_cb,
                        cancel_check=cancel_check,
                    ),
                )
                runners: list[tuple[str, Callable[[], list[RawSegment]]]] = (
                    [wcpp_entry, fw_entry] if whisper_cpp_ready else [fw_entry, wcpp_entry]
                )
                api_url = cfg.api_base_url.strip().rstrip("/")
                if api_url and api_url not in _DEFAULT_OLLAMA_URLS:
                    runners.append(
                        (
                            "openai_api",
                            lambda: _run_openai_whisper_api(
                                wav_path, cfg, progress_cb=progress_cb, cancel_check=cancel_check
                            ),
                        )
                    )

                raw = []
                succeeded = False
                prior_failures: list[str] = []
                real_errors: list[AsrError] = []
                last_err: AsrError | None = None
                for name, runner in runners:
                    try:
                        raw = runner()
                        succeeded = True
                        break
                    except AsrCancelled:
                        raise
                    except AsrError as exc:
                        last_err = exc
                        msg = str(exc)
                        is_missing = (
                            "Chưa cài đặt faster-whisper" in msg
                            or "Không tìm thấy thực thi whisper-cli" in msg
                            or "Không tìm thấy tệp mô hình whisper.cpp" in msg
                        )
                        if not is_missing:
                            real_errors.append(exc)
                        prior_failures.append(f"Không dùng được {name}: {msg}")
                if succeeded:
                    if notes is not None and prior_failures:
                        notes.extend(prior_failures)
                else:
                    if real_errors:
                        raise real_errors[0]
                    raise AsrError(
                        "Không tìm thấy bộ nhận diện giọng nói (ASR) khả dụng. "
                        "Hãy cài đặt faster-whisper / whisper.cpp hoặc cấu hình ASR trong Cài đặt."
                    ) from last_err
            if raw:
                _store_cached_segments(work_dir, cache_key, raw)

        cues = normalize_asr_segments_to_cues(raw)
        if not cues:
            raise AsrError("Không nhận diện được lời thoại nào trong tệp video/âm thanh.")
        return cues
    finally:
        if temp_dir_obj is not None:
            with contextlib.suppress(OSError):
                temp_dir_obj.cleanup()
