"""On-device and OpenAI-compatible Whisper ASR for video/audio subtitle generation.

Supports:
1. ``faster_whisper`` (local CTranslate2 Whisper with VAD filtering).
2. ``whisper_cpp`` (local ``whisper-cli`` / ``whisper-cpp`` binary; segments and word
   timings come from its JSON-full ``-ojf`` output).
3. ``openai_api`` (any OpenAI-compatible ``/v1/audio/transcriptions`` endpoint,
   including local ``whisper.cpp --server``, Speaches/LocalAI, or cloud APIs).
"""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import tempfile
import unicodedata
import uuid
import zlib
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import numpy as np

from vienetts_app.core.ai_models import (
    WHISPER_VAD_MODEL_ID,
    audio_model_path,
    cli_command,
    resolve_asr_model_path,
    resolve_whisper_cpp_cli,
)
from vienetts_app.core.audio import StreamingWavWriter, read_wav_range
from vienetts_app.core.audio_prep import (
    AudioPrepCancelled,
    AudioPrepError,
    cache_key_paths,
    ensure_source_16k,
)
from vienetts_app.core.diarization import refine_cues_speech_bounds
from vienetts_app.core.llm_client import http_error_detail
from vienetts_app.core.processes import CliCancelled, CliTimeout, run_cli
from vienetts_app.core.subtitles import NON_SPEECH_LABELS, Cue, clean_cue_text, parse_cues

_CLAUSE_SPLIT_RE = re.compile(r"(?<=[.!?…。！？,;，；:：])\s+")
_WHISPER_PP_RE = re.compile(r"progress\s*=\s*(\d+)\s*%", re.IGNORECASE)
_NON_SPEECH_TOKEN_RE = re.compile(
    r"(?:\[\s*(?:blank_audio|music|applause|laughter|silence|noise|inaudible|sound|sfx|cheering|cough|sigh)\s*\]"
    r"|\(\s*(?:blank_audio|music|applause|laughter|silence|noise|inaudible|sound|sfx|cheering|cough|sigh)\s*\)"
    r"|[♪♫]+)",
    re.IGNORECASE,
)
# whisper.cpp special tokens (``[_BEG_]``, ``[_TT_150]``, ``[_SOT_]``…) carry no text.
_WHISPER_SPECIAL_TOKEN_RE = re.compile(r"\[_[^\]]*\]")
MODEL_SAMPLE_RATE = 16_000
API_CHUNK_SECONDS = 600
ASR_CACHE_FILENAME = "asr_segments_cache.json"
# Bumped whenever the backends' output changes (2: whisper.cpp ``-mc 0``/VAD/JSON-full
# words), so a segments cache written by an older version re-runs instead of being reused.
ASR_VERSION = 2
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
    api_model: str = ""


@dataclass(frozen=True)
class RawSegment:
    start_ms: int
    end_ms: int
    text: str
    speaker: str = ""
    words: tuple[tuple[int, int, str], ...] = ()
    speech_start_ms: int | None = None
    speech_end_ms: int | None = None
    # Decoder scores for :func:`filter_hallucinations`; None when the backend has none.
    avg_logprob: float | None = None
    no_speech_prob: float | None = None
    compression_ratio: float | None = None


_SCORE_FIELDS = ("avg_logprob", "no_speech_prob", "compression_ratio")


def _opt_float(value: Any) -> float | None:
    """A backend's numeric score as float; None when absent or not a number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    return float(value)


def _strip_non_speech(text: str) -> str:
    stripped = _NON_SPEECH_TOKEN_RE.sub(" ", text or "")
    cleaned = clean_cue_text(stripped)
    if not cleaned or not any(ch.isalnum() for ch in cleaned):
        return ""
    return cleaned


def _extract_words(
    raw_words: Any,
    seg_start_ms: int,
    seg_end_ms: int,
) -> tuple[tuple[int, int, str], ...]:
    if not raw_words:
        return ()
    parsed: list[tuple[int, int, str]] = []
    for item in raw_words:
        if isinstance(item, tuple | list) and len(item) >= 3:
            try:
                w_start = int(round(float(item[0])))
                w_end = int(round(float(item[1])))
                w_txt = _strip_non_speech(str(item[2] or ""))
            except (TypeError, ValueError):
                continue
        elif isinstance(item, dict):
            w_raw = str(item.get("word") or item.get("text") or "").strip()
            w_txt = _strip_non_speech(w_raw)
            try:
                if "start_ms" in item or "end_ms" in item:
                    w_start = int(round(float(item.get("start_ms", seg_start_ms))))
                    w_end = int(round(float(item.get("end_ms", w_start))))
                else:
                    w_start = int(round(float(item.get("start", seg_start_ms / 1000.0)) * 1000))
                    w_end = int(round(float(item.get("end", w_start / 1000.0)) * 1000))
            except (TypeError, ValueError):
                continue
        else:
            w_raw = str(getattr(item, "word", None) or getattr(item, "text", None) or "").strip()
            w_txt = _strip_non_speech(w_raw)
            try:
                w_start = int(round(float(getattr(item, "start", seg_start_ms / 1000.0)) * 1000))
                w_end = int(round(float(getattr(item, "end", w_start / 1000.0)) * 1000))
            except (TypeError, ValueError):
                continue
        if not w_txt:
            continue
        w_start = max(seg_start_ms, min(seg_end_ms, w_start))
        w_end = max(w_start, min(seg_end_ms, w_end))
        parsed.append((w_start, w_end, w_txt))
    return tuple(parsed)


def _clamp_speech_bounds(
    start_ms: int,
    end_ms: int,
    speech_start_ms: int | None,
    speech_end_ms: int | None,
    words: tuple[tuple[int, int, str], ...] = (),
) -> tuple[int | None, int | None]:
    s_bound = speech_start_ms
    e_bound = speech_end_ms
    if s_bound is None and words:
        s_bound = words[0][0]
    if e_bound is None and words:
        e_bound = words[-1][1]
    if s_bound is not None:
        s_bound = max(start_ms, min(end_ms, int(s_bound)))
    if e_bound is not None:
        lo = s_bound if s_bound is not None else start_ms
        e_bound = max(lo, min(end_ms, int(e_bound)))
    return s_bound, e_bound


def _partition_words_for_chunks(
    words: tuple[tuple[int, int, str], ...],
    chunks: Sequence[str],
) -> list[tuple[tuple[int, int, str], ...]] | None:
    if not words or len(words) < len(chunks):
        return None
    token_counts = [len(c.split()) for c in chunks]
    if sum(token_counts) == len(words) and all(cnt > 0 for cnt in token_counts):
        grouped: list[tuple[tuple[int, int, str], ...]] = []
        cursor = 0
        for cnt in token_counts:
            grouped.append(words[cursor : cursor + cnt])
            cursor += cnt
        return grouped

    grouped = []
    w_idx = 0
    n_words = len(words)
    n_chunks = len(chunks)
    for c_idx, chunk in enumerate(chunks):
        if c_idx == n_chunks - 1:
            rem = words[w_idx:]
            if not rem:
                return None
            grouped.append(rem)
            break
        target_chars = max(1, len(re.sub(r"\s+", "", chunk)))
        max_end = n_words - (n_chunks - 1 - c_idx)
        acc_chars = 0
        start_idx = w_idx
        while w_idx < max_end:
            w_chars = max(1, len(re.sub(r"\s+", "", words[w_idx][2])))
            if (
                w_idx > start_idx
                and acc_chars + w_chars > target_chars
                and abs(target_chars - acc_chars) <= abs((acc_chars + w_chars) - target_chars)
            ):
                break
            acc_chars += w_chars
            w_idx += 1
            if acc_chars >= target_chars:
                break
        if w_idx == start_idx:
            w_idx += 1
        grouped.append(words[start_idx:w_idx])
    return grouped if len(grouped) == n_chunks and all(grouped) else None


def _split_long_segment(
    seg: RawSegment,
    *,
    max_chars: int,
    max_duration_ms: int,
) -> list[RawSegment]:
    text = clean_cue_text(seg.text)
    if not text:
        return []
    seg_end_ms = max(seg.start_ms + 1, seg.end_ms)
    duration = max(1, seg_end_ms - seg.start_ms)
    if len(text) <= max_chars and duration <= max_duration_ms:
        sp_start, sp_end = _clamp_speech_bounds(
            seg.start_ms, seg_end_ms, seg.speech_start_ms, seg.speech_end_ms, seg.words
        )
        return [
            RawSegment(
                seg.start_ms,
                seg_end_ms,
                text,
                seg.speaker,
                words=seg.words,
                speech_start_ms=sp_start,
                speech_end_ms=sp_end,
            )
        ]

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
        sp_start, sp_end = _clamp_speech_bounds(
            seg.start_ms, seg_end_ms, seg.speech_start_ms, seg.speech_end_ms, seg.words
        )
        return [
            RawSegment(
                seg.start_ms,
                seg_end_ms,
                text,
                seg.speaker,
                words=seg.words,
                speech_start_ms=sp_start,
                speech_end_ms=sp_end,
            )
        ]

    word_groups = _partition_words_for_chunks(seg.words, chunks)
    if word_groups is not None:
        out: list[RawSegment] = []
        prev_end = seg.start_ms
        for i, (chunk, c_words) in enumerate(zip(chunks, word_groups, strict=True)):
            w_first_start = c_words[0][0]
            w_last_end = max(w_first_start + 1, c_words[-1][1])
            c_start = seg.start_ms if i == 0 else max(prev_end, w_first_start)
            if i == len(chunks) - 1:
                c_end = max(c_start + 1, seg_end_ms)
            else:
                c_end = max(c_start + 1, min(seg_end_ms, w_last_end))
            sp_start, sp_end = _clamp_speech_bounds(
                c_start, c_end, w_first_start, w_last_end, c_words
            )
            out.append(
                RawSegment(
                    c_start,
                    c_end,
                    chunk,
                    seg.speaker,
                    words=c_words,
                    speech_start_ms=sp_start,
                    speech_end_ms=sp_end,
                )
            )
            prev_end = c_end
        return out

    total_chars = max(1, sum(len(c) for c in chunks))
    out = []
    cursor_ms = seg.start_ms
    for i, chunk in enumerate(chunks):
        if i == len(chunks) - 1:
            end_ms = max(cursor_ms + 1, seg_end_ms)
        else:
            span = max(1, int(round(duration * len(chunk) / total_chars)))
            end_ms = max(cursor_ms + 1, min(seg_end_ms, cursor_ms + span))
        c_sp_start = seg.speech_start_ms if i == 0 else None
        c_sp_end = seg.speech_end_ms if i == len(chunks) - 1 else None
        if c_sp_start is not None or c_sp_end is not None:
            c_sp_start = c_sp_start if c_sp_start is not None else cursor_ms
            c_sp_end = c_sp_end if c_sp_end is not None else end_ms
            c_sp_start, c_sp_end = _clamp_speech_bounds(cursor_ms, end_ms, c_sp_start, c_sp_end)
        out.append(
            RawSegment(
                cursor_ms,
                end_ms,
                chunk,
                seg.speaker,
                speech_start_ms=c_sp_start,
                speech_end_ms=c_sp_end,
            )
        )
        cursor_ms = end_ms
    return out


def _merge_two_segments(a: RawSegment, b: RawSegment) -> RawSegment:
    s_ms = a.start_ms
    e_ms = max(s_ms + 1, b.end_ms)
    words = a.words + b.words
    has_bounds = (
        a.speech_start_ms is not None
        or a.speech_end_ms is not None
        or b.speech_start_ms is not None
        or b.speech_end_ms is not None
        or bool(words)
    )
    if has_bounds:
        sp_start = (
            a.speech_start_ms
            if a.speech_start_ms is not None
            else (a.words[0][0] if a.words else a.start_ms)
        )
        sp_end = (
            b.speech_end_ms
            if b.speech_end_ms is not None
            else (b.words[-1][1] if b.words else b.end_ms)
        )
        sp_start, sp_end = _clamp_speech_bounds(s_ms, e_ms, sp_start, sp_end, words)
    else:
        sp_start, sp_end = None, None
    return RawSegment(
        s_ms,
        e_ms,
        f"{a.text} {b.text}",
        a.speaker,
        words=words,
        speech_start_ms=sp_start,
        speech_end_ms=sp_end,
    )


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
        words = _extract_words(seg.words, s_ms, e_ms) if seg.words else ()
        sp_start, sp_end = _clamp_speech_bounds(
            s_ms, e_ms, seg.speech_start_ms, seg.speech_end_ms, words
        )
        cleaned.append(
            RawSegment(
                s_ms,
                e_ms,
                txt,
                spk,
                words=words,
                speech_start_ms=sp_start,
                speech_end_ms=sp_end,
            )
        )

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
            cleaned[i + 1] = _merge_two_segments(curr, nxt)
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
            merged[-1] = _merge_two_segments(prev, curr)
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
        sp_start, sp_end = _clamp_speech_bounds(
            start_ms, end_ms, seg.speech_start_ms, seg.speech_end_ms, seg.words
        )
        cues.append(
            Cue(
                idx,
                start_ms,
                end_ms,
                seg.text,
                speaker=seg.speaker,
                speech_start_ms=sp_start,
                speech_end_ms=sp_end,
            )
        )
        prev_end = end_ms
    return cues


# Whisper's own decode-failure gates (openai-whisper / faster-whisper defaults).
NO_SPEECH_PROB_THRESHOLD = 0.6
LOGPROB_THRESHOLD = -1.0
COMPRESSION_RATIO_THRESHOLD = 2.4
# Below this a suspicious line (outro phrase, 2× repeat) counts as unconfident.
LOW_CONFIDENCE_LOGPROB = -0.7
OUTRO_TAIL_MS = 30_000
# Outro/credit lines Whisper is known to invent over silence or music. Matched as whole
# words after normalization; removed only when unconfident or in a speechless tail.
HALLUCINATION_PHRASES: tuple[str, ...] = (
    "subscribe cho kênh",
    "Ghiền Mì Gõ",
    "La La School",
    "hãy đăng ký kênh",
    "để không bỏ lỡ những video hấp dẫn",
    "Cảm ơn các bạn đã theo dõi",
    "Cảm ơn các bạn đã xem video",
    "Hẹn gặp lại các bạn trong những video tiếp theo",
    "Phụ đề được thực hiện bởi",
    "cộng đồng Amara.org",
    "Subtitles by the Amara.org community",
    "Thanks for watching",
    "Thank you for watching",
    "Please subscribe",
    "like and subscribe",
)


def _normalized_line(text: str) -> str:
    """Casefolded speech words of ``text``, punctuation dropped, for line comparison."""
    folded = unicodedata.normalize("NFC", _strip_non_speech(text)).casefold()
    return " ".join("".join(ch if ch.isalnum() else " " for ch in folded).split())


_HALLUCINATION_LINES = tuple(_normalized_line(p) for p in HALLUCINATION_PHRASES)


def _compression_ratio(text: str) -> float:
    """openai-whisper's measure: UTF-8 length over its zlib-compressed length."""
    data = text.encode("utf-8")
    return len(data) / len(zlib.compress(data)) if data else 0.0


def _low_confidence(seg: RawSegment) -> bool:
    return (seg.avg_logprob is not None and seg.avg_logprob < LOW_CONFIDENCE_LOGPROB) or (
        seg.no_speech_prob is not None and seg.no_speech_prob > NO_SPEECH_PROB_THRESHOLD
    )


def filter_hallucinations(
    segments: Sequence[RawSegment],
    *,
    duration_ms: int,
    notes: list[str] | None = None,
    has_speech: Callable[[int, int], bool] | None = None,
) -> list[RawSegment]:
    """Drop the segments Whisper is known to invent; counts go to ``notes``.

    In order:

    1. ``no_speech_prob > 0.6`` with ``avg_logprob < -1.0``. Only when the backend
       supplies both: faster-whisper and API ``verbose_json`` do, whisper.cpp's
       JSON has no ``no_speech_prob``, so this rule never fires for it.
    2. ``compression_ratio > 2.4`` — the backend's own (faster-whisper, API) or
       else the segment text's (whisper.cpp, SRT).
    3. A run of ≥ 3 consecutive identical normalized lines keeps its first; a
       2× repeat is collapsed only when one of the two is unconfident.
    4. A known outro/credit line (:data:`HALLUCINATION_PHRASES`) is removed when
       unconfident, or when it starts in the final 30 s of ``duration_ms`` and
       ``has_speech(start_ms, end_ms)`` (a VAD over the audio) finds no speech.
       Without ``has_speech`` only the confidence test applies.
    """
    silent = repetitive = repeated = outros = 0
    scored: list[RawSegment] = []
    for seg in segments:
        if (
            seg.no_speech_prob is not None
            and seg.avg_logprob is not None
            and seg.no_speech_prob > NO_SPEECH_PROB_THRESHOLD
            and seg.avg_logprob < LOGPROB_THRESHOLD
        ):
            silent += 1
            continue
        ratio = seg.compression_ratio
        if ratio is None:
            ratio = _compression_ratio(seg.text)
        if ratio > COMPRESSION_RATIO_THRESHOLD:
            repetitive += 1
            continue
        scored.append(seg)

    lines = [_normalized_line(seg.text) for seg in scored]
    collapsed: list[RawSegment] = []
    i = 0
    while i < len(scored):
        j = i + 1
        while lines[i] and j < len(scored) and lines[j] == lines[i]:
            j += 1
        run = scored[i:j]
        if len(run) >= 3 or (len(run) == 2 and any(_low_confidence(s) for s in run)):
            collapsed.append(run[0])
            repeated += len(run) - 1
        else:
            collapsed.extend(run)
        i = j

    total_ms = duration_ms if duration_ms > 0 else max((s.end_ms for s in segments), default=0)
    tail_start_ms = total_ms - OUTRO_TAIL_MS
    kept: list[RawSegment] = []
    for seg in collapsed:
        padded = f" {_normalized_line(seg.text)} "
        if any(f" {phrase} " in padded for phrase in _HALLUCINATION_LINES) and (
            _low_confidence(seg)
            or (
                seg.start_ms >= tail_start_ms
                and has_speech is not None
                and not has_speech(seg.start_ms, seg.end_ms)
            )
        ):
            outros += 1
            continue
        kept.append(seg)

    counts = (
        (silent, "không có lời nói"),
        (repetitive, "lặp chữ"),
        (repeated, "dòng lặp lại"),
        (outros, "câu kết/quảng bá kênh"),
    )
    dropped = len(segments) - len(kept)
    if notes is not None and dropped:
        detail = ", ".join(f"{n} {label}" for n, label in counts if n)
        notes.append(f"Đã lọc {dropped} đoạn nghi là ảo giác của Whisper: {detail}.")
    return kept


def _energy_speech_check(wav_path: Path, threshold_db: float = -42.0) -> Callable[[int, int], bool]:
    """``has_speech`` for :func:`filter_hallucinations`: any 10 ms frame above the gate.

    The same −42 dBFS energy gate as ``refine_cues_speech_bounds``; an unreadable
    slice counts as speech, so the segment is kept.
    """
    gate = 10.0 ** (threshold_db / 20.0)
    win = MODEL_SAMPLE_RATE // 100

    def has_speech(start_ms: int, end_ms: int) -> bool:
        try:
            samples, _ = read_wav_range(
                wav_path, start_ms * MODEL_SAMPLE_RATE // 1000, end_ms * MODEL_SAMPLE_RATE // 1000
            )
        except Exception:  # noqa: BLE001 - unknown audio keeps the segment
            return True
        frames = samples[: samples.size // win * win].reshape(-1, win)
        if frames.size == 0:
            return bool(samples.size) and float(np.sqrt(np.mean(samples * samples))) > gate
        return bool(np.any(np.sqrt(np.mean(frames * frames, axis=1)) > gate))

    return has_speech


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
            word_timestamps=True,
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
                seg_start_ms = int(round(start_s * 1000))
                seg_end_ms = max(seg_start_ms + 1, int(round(end_s * 1000)))
                seg_words = _extract_words(getattr(seg, "words", None), seg_start_ms, seg_end_ms)
                sp_start, sp_end = _clamp_speech_bounds(
                    seg_start_ms, seg_end_ms, None, None, seg_words
                )
                raw_segments.append(
                    RawSegment(
                        seg_start_ms,
                        seg_end_ms,
                        txt,
                        words=seg_words,
                        speech_start_ms=sp_start,
                        speech_end_ms=sp_end,
                        **{name: _opt_float(getattr(seg, name, None)) for name in _SCORE_FIELDS},
                    )
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


def _rejoin_utf8(text: str) -> str:
    """Rebuild characters whose UTF-8 bytes ``surrogateescape`` kept as lone surrogates."""
    try:
        return text.encode("utf-8", "surrogateescape").decode("utf-8", "replace")
    except UnicodeEncodeError:  # a lone surrogate from a JSON \u escape, not a raw byte
        return text.encode("utf-8", "surrogatepass").decode("utf-8", "replace")


def _merge_whisper_tokens(
    tokens: Any, seg_start_ms: int, seg_end_ms: int
) -> tuple[tuple[tuple[int, int, str], ...], float | None]:
    """whisper.cpp ``-ojf`` tokens → ``(words, avg_logprob)``.

    A token that opens with a space starts a word; any other text token (a
    sub-word piece, punctuation, the tail of a split UTF-8 character) extends
    the current one. Special tokens are skipped. Words are clamped to the
    segment; without per-token offsets there are no words. ``avg_logprob`` is
    the mean log-probability of the text tokens.
    """
    pieces: list[list[Any]] = []  # [start_ms, end_ms, raw text]
    logprobs: list[float] = []
    timed = True
    for tok in tokens if isinstance(tokens, list) else ():
        if not isinstance(tok, dict):
            continue
        text = str(tok.get("text") or "")
        if not text.strip() or _WHISPER_SPECIAL_TOKEN_RE.fullmatch(text.strip()):
            continue
        prob = tok.get("p")
        if isinstance(prob, int | float) and not isinstance(prob, bool) and 0.0 <= prob <= 1.0:
            logprobs.append(math.log(max(float(prob), 1e-10)))
        try:
            t0 = int(tok["offsets"]["from"])
            t1 = max(t0, int(tok["offsets"]["to"]))
        except (KeyError, TypeError, ValueError):
            timed = False
            t0 = t1 = 0
        if pieces and not text.startswith(" "):
            pieces[-1][1] = max(pieces[-1][1], t1)
            pieces[-1][2] += text
        else:
            pieces.append([t0, t1, text])
    avg_logprob = sum(logprobs) / len(logprobs) if logprobs else None
    if not timed:
        return (), avg_logprob
    raw_words = [(start, end, _rejoin_utf8(text)) for start, end, text in pieces]
    return _extract_words(raw_words, seg_start_ms, seg_end_ms), avg_logprob


def _parse_whisper_json(raw: bytes) -> list[RawSegment]:
    """Segments with word timings from ``whisper-cli -ojf`` (JSON-full) output.

    Builds before b5454 write each token's bytes unescaped, so one character
    can be split across two tokens (invalid UTF-8 in each JSON string): the
    file is decoded with ``surrogateescape`` and the bytes are rejoined per
    word. Offsets are in ms. Raises ``ValueError`` on anything but JSON-full.
    """
    payload = json.loads(raw.decode("utf-8", errors="surrogateescape"), strict=False)
    transcription = payload.get("transcription") if isinstance(payload, dict) else None
    if not isinstance(transcription, list):
        raise ValueError("missing 'transcription'")
    out: list[RawSegment] = []
    for item in transcription:
        if not isinstance(item, dict):
            continue
        try:
            start_ms = max(0, int(item["offsets"]["from"]))
            end_ms = max(start_ms + 1, int(item["offsets"]["to"]))
        except (KeyError, TypeError, ValueError):
            continue
        words, avg_logprob = _merge_whisper_tokens(item.get("tokens"), start_ms, end_ms)
        text = _rejoin_utf8(str(item.get("text") or "")).strip()
        if not text:
            continue
        sp_start, sp_end = _clamp_speech_bounds(start_ms, end_ms, None, None, words)
        out.append(
            RawSegment(
                start_ms,
                end_ms,
                text,
                words=words,
                speech_start_ms=sp_start,
                speech_end_ms=sp_end,
                avg_logprob=avg_logprob,
            )
        )
    return out


def _read_whisper_cpp_output(json_out: Path, srt_out: Path) -> list[RawSegment]:
    """Parse the JSON-full output; the SRT is read only when no valid JSON was written."""
    if json_out.is_file():
        with contextlib.suppress(OSError, ValueError):
            return _parse_whisper_json(json_out.read_bytes())
    if not srt_out.is_file() or srt_out.stat().st_size == 0:
        return []
    try:
        cues = parse_cues(srt_out.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        raise AsrError(f"Tệp phụ đề từ whisper.cpp không hợp lệ: {exc}") from exc
    return [RawSegment(c.start_ms, c.end_ms, c.text, c.speaker) for c in cues]


def _run_whisper_cpp(
    wav_path: Path,
    config: AsrConfig,
    *,
    out_dir: Path,
    data_dir: Path | None = None,
    progress_cb: Callable[[float, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> list[RawSegment]:
    """Run ``whisper-cli``; its output files go to ``out_dir`` (the media cache) and are removed."""
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
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AsrError(f"Không thể tạo thư mục tạm cho whisper.cpp: {exc}") from exc
    # Absolute: whisper-cli runs with cwd=binary.parent.
    out_prefix = out_dir.absolute() / f".whisper_out_{uuid.uuid4().hex}"
    json_out = Path(f"{out_prefix}.json")
    srt_out = Path(f"{out_prefix}.srt")
    lang = "auto" if not config.language else config.language
    threads = _resolve_threads(config)
    # -mc 0: no previous-text context, which stops repetition loops from carrying over
    # windows. -ojf: segments plus per-token offsets/probabilities; -osrt is the fallback.
    cmd = cli_command(
        binary,
        "-m",
        str(model_path),
        "-f",
        str(wav_path),
        "-l",
        lang,
        "-t",
        str(threads),
        "-mc",
        "0",
        "-sns",
        "-pp",
        "-ojf",
        "-osrt",
        "-of",
        str(out_prefix),
    )
    vad_model = audio_model_path(WHISPER_VAD_MODEL_ID, data_dir)
    if vad_model is not None:
        cmd.extend(["--vad", "-vm", str(vad_model)])
    if (config.device or "auto").strip().lower() == "cpu":
        cmd.append("-ng")

    duration_s = _wav_duration_s(wav_path)
    timeout_s = (
        float(config.timeout_s)
        if float(config.timeout_s) > 0
        else max(180.0, duration_s * 6.0 + 60.0)
    )

    def _on_stderr_line(line: str) -> None:
        if progress_cb is None:
            return
        m = _WHISPER_PP_RE.search(line)
        if m is not None:
            pct = min(100, max(0, int(m.group(1))))
            progress_cb(min(0.95, 0.2 + 0.75 * (pct / 100.0)), "Đang chạy whisper.cpp…")

    try:
        try:
            res = run_cli(
                cmd,
                cwd=binary.parent,
                timeout=timeout_s,
                cancel_check=cancel_check,
                on_stderr_line=_on_stderr_line,
            )
        except CliCancelled as exc:
            raise AsrCancelled("Đã hủy nhận diện giọng nói.") from exc
        except CliTimeout as exc:
            raise AsrError(
                f"whisper.cpp quá thời gian chờ ({timeout_s:.1f}s) — "
                "hãy thử mô hình nhẹ hơn hoặc chọn thiết bị CPU."
            ) from exc
        except OSError as exc:
            # A binary that cannot start (wrong architecture, no exec bit) is
            # an ASR failure the auto chain can fall back from, not a crash.
            raise AsrError(f"Không thể chạy whisper-cli: {exc}") from exc
        if res.returncode != 0:
            detail = (res.stderr or res.stdout).strip().splitlines()[-1:] or ["whisper-cli failed"]
            raise AsrError(f"whisper.cpp thất bại: {detail[0]}")
        segments = _read_whisper_cpp_output(json_out, srt_out)
        if progress_cb is not None:
            progress_cb(1.0, "Hoàn tất nhận diện giọng nói.")
        return segments
    finally:
        for out_file in (json_out, srt_out):
            with contextlib.suppress(OSError):
                out_file.unlink(missing_ok=True)


def _upload_whisper_api(
    wav_path: Path,
    config: AsrConfig,
    *,
    progress_cb: Callable[[float, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    prompt: str = "",
    timeout: float = 120,
) -> list[RawSegment]:
    if cancel_check is not None and cancel_check():
        raise AsrCancelled("Đã hủy nhận diện giọng nói.")
    base_url = (config.api_base_url or "http://localhost:11434/v1").strip().rstrip("/")
    if not base_url:
        raise AsrError("Chưa cấu hình địa chỉ máy chủ Whisper API.")
    url = f"{base_url}/audio/transcriptions"
    boundary = f"----VieNeuAsrBoundary{uuid.uuid4().hex}"
    model_name = (config.api_model or "whisper-1").strip() or "whisper-1"

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
    if prompt:
        _add_field("prompt", prompt[-200:])

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
    for attempt in range(2):
        if cancel_check is not None and cancel_check():
            raise AsrCancelled("Đã hủy nhận diện giọng nói.")
        try:
            with urlopen(req, timeout=timeout) as resp:  # noqa: S310
                raw_resp = resp.read().decode("utf-8", errors="replace")
            break
        except HTTPError as exc:
            message = f"Máy chủ ASR HTTP {exc.code} ({url}): {http_error_detail(exc)}"
            transient = exc.code in (408, 409, 429) or exc.code >= 500
        except (URLError, TimeoutError, OSError) as exc:
            message = f"Không thể kết nối máy chủ ASR ({url}): {exc}"
            transient = True
        if not transient or attempt == 1:
            key = config.api_key.strip()
            raise AsrError(message.replace(key, "[redacted]") if key else message) from None

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
    top_words_raw = payload.get("words") if isinstance(payload, dict) else None
    out: list[RawSegment] = []
    if isinstance(segments, list):
        for item in segments:
            if not isinstance(item, dict):
                continue
            txt = str(item.get("text") or "").strip()
            if not txt:
                continue
            start_ms = int(round(float(item.get("start", 0.0)) * 1000))
            end_ms = max(
                start_ms + 1,
                int(round(float(item.get("end", start_ms / 1000.0 + 1.0)) * 1000)),
            )
            spk = str(item.get("speaker") or "").strip()
            seg_words = _extract_words(item.get("words"), start_ms, end_ms)
            if not seg_words and isinstance(top_words_raw, list) and top_words_raw:
                matching_top = []
                for w_item in top_words_raw:
                    if isinstance(w_item, dict):
                        with contextlib.suppress(TypeError, ValueError):
                            ws = int(round(float(w_item.get("start", -1.0)) * 1000))
                            we = int(round(float(w_item.get("end", ws / 1000.0)) * 1000))
                            if min(end_ms, max(ws + 1, we)) > max(start_ms, ws):
                                matching_top.append(w_item)
                seg_words = _extract_words(matching_top, start_ms, end_ms)
            sp_start, sp_end = _clamp_speech_bounds(start_ms, end_ms, None, None, seg_words)
            out.append(
                RawSegment(
                    start_ms,
                    end_ms,
                    txt,
                    spk,
                    words=seg_words,
                    speech_start_ms=sp_start,
                    speech_end_ms=sp_end,
                    **{name: _opt_float(item.get(name)) for name in _SCORE_FIELDS},
                )
            )
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
        seg_words = _extract_words(top_words_raw, 0, dur_ms)
        sp_start, sp_end = _clamp_speech_bounds(0, dur_ms, None, None, seg_words)
        out.append(
            RawSegment(
                0,
                dur_ms,
                payload["text"].strip(),
                words=seg_words,
                speech_start_ms=sp_start,
                speech_end_ms=sp_end,
            )
        )

    if progress_cb is not None:
        progress_cb(1.0, "Hoàn tất nhận diện giọng nói.")
    return out


def _plan_api_chunks(wav_path: Path) -> list[tuple[int, int]]:
    import soundfile as sf

    info = sf.info(str(wav_path))
    rate = int(info.samplerate)
    total = int(info.frames)
    limit = max(1, int(API_CHUNK_SECONDS * rate))
    frame_size = max(1, int(rate * 0.2))
    chunks: list[tuple[int, int]] = []
    start = 0
    while total - start > limit:
        boundary = start + limit
        search_start = max(start, boundary - 15 * rate)
        samples, _ = read_wav_range(wav_path, search_start, boundary)
        count = len(samples) // frame_size
        if count:
            frames = samples[: count * frame_size].reshape(count, frame_size)
            levels = np.mean(frames * frames, axis=1)
            quietest = count - 1 - int(np.argmin(levels[::-1]))
            cut = search_start + (quietest + 1) * frame_size
        else:
            cut = boundary
        cut = max(start + 1, min(boundary, cut))
        chunks.append((start, cut))
        start = cut
    if start < total:
        chunks.append((start, total))
    return chunks


def _run_openai_whisper_api(
    wav_path: Path,
    config: AsrConfig,
    *,
    progress_cb: Callable[[float, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> list[RawSegment]:
    if cancel_check is not None and cancel_check():
        raise AsrCancelled("Đã hủy nhận diện giọng nói.")
    chunks = _plan_api_chunks(wav_path)
    if len(chunks) <= 1 and wav_path.stat().st_size < 24_900_000:
        timeout = max(120.0, 0.5 * _wav_duration_s(wav_path))
        return _upload_whisper_api(
            wav_path, config, progress_cb=progress_cb, cancel_check=cancel_check, timeout=timeout
        )

    import soundfile as sf

    rate = int(sf.info(str(wav_path)).samplerate)
    result: list[RawSegment] = []
    prompt = ""
    with tempfile.TemporaryDirectory(prefix="vieneu-asr-") as directory:
        for index, (start, stop) in enumerate(chunks):
            if cancel_check is not None and cancel_check():
                raise AsrCancelled("Đã hủy nhận diện giọng nói.")
            path = Path(directory) / f"chunk_{index:04d}.wav"
            with StreamingWavWriter(path, sample_rate=rate) as writer:
                for position in range(start, stop, rate * 10):
                    samples, _ = read_wav_range(wav_path, position, min(stop, position + rate * 10))
                    writer.write(samples)
            if progress_cb is not None:
                progress_cb(index / len(chunks), "Đang gửi âm thanh tới máy chủ ASR…")
            segments = _upload_whisper_api(
                path,
                config,
                cancel_check=cancel_check,
                prompt=prompt,
                timeout=max(120.0, 0.5 * (stop - start) / rate),
            )
            prompt = " ".join(segment.text for segment in segments)[-200:]
            offset = int(round(start * 1000 / rate))
            result.extend(
                replace(
                    segment,
                    start_ms=segment.start_ms + offset,
                    end_ms=segment.end_ms + offset,
                    words=tuple((a + offset, b + offset, text) for a, b, text in segment.words),
                    speech_start_ms=(
                        segment.speech_start_ms + offset
                        if segment.speech_start_ms is not None
                        else None
                    ),
                    speech_end_ms=(
                        segment.speech_end_ms + offset
                        if segment.speech_end_ms is not None
                        else None
                    ),
                )
                for segment in segments
            )
            if progress_cb is not None:
                progress_cb((index + 1) / len(chunks), "Đang nhận diện giọng nói…")
    return result


def _ensure_16k_wav(
    src: Path,
    work_dir: Path | None,
    data_dir: Path | None,
    temp_dir: tempfile.TemporaryDirectory[str] | None,
    *,
    progress_cb: Callable[[float, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> tuple[Path, tempfile.TemporaryDirectory[str] | None]:
    """Return the shared 16 kHz mono WAV for ``src`` (``audio_prep.ensure_source_16k``).

    With a ``work_dir`` this is the media cache's ``source_16k.wav``, which
    diarization reuses; without one it lives in a temp dir the caller cleans.
    """
    if work_dir is not None:
        cache_dir = work_dir
    else:
        if temp_dir is None:
            temp_dir = tempfile.TemporaryDirectory(prefix="vienetts_asr_")
        cache_dir = Path(temp_dir.name)

    def report(fraction: float) -> None:
        if progress_cb is not None:
            progress_cb(0.05 + 0.05 * fraction, "Đang trích xuất âm thanh…")

    try:
        wav = ensure_source_16k(
            src,
            cache_dir,
            data_dir=data_dir,
            cancel_check=cancel_check,
            progress_cb=report if progress_cb is not None else None,
        )
    except AudioPrepCancelled as exc:
        raise AsrCancelled("Đã hủy nhận diện giọng nói.") from exc
    except AudioPrepError as exc:
        raise AsrError(str(exc)) from exc
    return wav, temp_dir


def _asr_cache_key(
    wav_path: Path,
    config: AsrConfig,
    *,
    data_dir: Path | None = None,
    key_path: Path | None = None,
) -> str:
    """Segments-cache key for ``wav_path``; ``key_path`` names it as a legacy alias.

    ``:vad`` marks a backend that may run whisper.cpp while its Silero VAD model is
    installed, so installing or removing the model re-runs; keys without it are
    unchanged, so caches (and legacy aliases) from before still hit.
    """
    try:
        st = wav_path.stat()
    except OSError:
        return ""
    lang = (config.language or "auto").strip() or "auto"
    model = (config.model or "base").strip() or "base"
    backend = (config.backend or "auto").strip() or "auto"
    named = (key_path or wav_path).resolve()
    key = f"{named}:{st.st_size}:{st.st_mtime_ns}:{backend}:{model}:{lang}"
    if config.api_base_url.strip():
        key += f":{config.api_base_url.strip()}:{config.api_model.strip()}"
    vad_model = audio_model_path(WHISPER_VAD_MODEL_ID, data_dir)
    if backend in ("auto", "whisper_cpp") and vad_model is not None:
        key += ":vad"
    return key


def _load_cached_segments(
    work_dir: Path | None, key: str, *, aliases: Sequence[str] = ()
) -> list[RawSegment] | None:
    """Cached segments stored under ``key`` (or a legacy ``aliases`` key).

    A cache from another :data:`ASR_VERSION` (or an unversioned one) is a miss.
    """
    if work_dir is None or not key:
        return None
    path = work_dir / ASR_CACHE_FILENAME
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or data.get("key") not in (key, *aliases):
        return None
    if data.get("asr_version") != ASR_VERSION:
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
        words = _extract_words(item.get("words"), s_ms, e_ms)
        sp_start: int | None = None
        sp_end: int | None = None
        if item.get("speech_start_ms") is not None:
            with contextlib.suppress(TypeError, ValueError):
                sp_start = int(item["speech_start_ms"])
        if item.get("speech_end_ms") is not None:
            with contextlib.suppress(TypeError, ValueError):
                sp_end = int(item["speech_end_ms"])
        sp_start, sp_end = _clamp_speech_bounds(s_ms, e_ms, sp_start, sp_end, words)
        out.append(
            RawSegment(
                s_ms,
                e_ms,
                txt,
                str(item.get("speaker") or "").strip(),
                words=words,
                speech_start_ms=sp_start,
                speech_end_ms=sp_end,
                **{name: _opt_float(item.get(name)) for name in _SCORE_FIELDS},
            )
        )
    return out or None


def _store_cached_segments(work_dir: Path | None, key: str, segments: Sequence[RawSegment]) -> None:
    if work_dir is None or not key or not segments:
        return
    raw: list[dict[str, Any]] = []
    for s in segments:
        item: dict[str, Any] = {
            "start_ms": s.start_ms,
            "end_ms": s.end_ms,
            "text": s.text,
            "speaker": s.speaker,
        }
        if s.words:
            item["words"] = [[w[0], w[1], w[2]] for w in s.words]
        if s.speech_start_ms is not None:
            item["speech_start_ms"] = int(s.speech_start_ms)
        if s.speech_end_ms is not None:
            item["speech_end_ms"] = int(s.speech_end_ms)
        for name in _SCORE_FIELDS:
            score = getattr(s, name)
            if score is not None:
                item[name] = float(score)
        raw.append(item)
    payload = {"asr_version": ASR_VERSION, "key": key, "segments": raw}
    with contextlib.suppress(OSError):
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / ASR_CACHE_FILENAME).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
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
            src, work_dir, data_dir, None, progress_cb=progress_cb, cancel_check=cancel_check
        )
        if cancel_check is not None and cancel_check():
            raise AsrCancelled("Đã hủy nhận diện giọng nói.")
        # whisper-cli output goes to the media cache (or this run's temp dir): a 16 kHz
        # mono WAV is used in place, and nothing may be written beside the user's file.
        out_dir = work_dir if work_dir is not None else Path(temp_dir_obj.name)

        cache_key = _asr_cache_key(wav_path, cfg, data_dir=data_dir)
        # A legacy input_16k.wav adopted as source_16k.wav keeps its size and
        # mtime, so segments cached against the old name still apply.
        aliases = [
            _asr_cache_key(wav_path, cfg, data_dir=data_dir, key_path=p)
            for p in cache_key_paths(wav_path)[1:]
        ]
        cached = _load_cached_segments(work_dir, cache_key, aliases=aliases)
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
                    out_dir=out_dir,
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
                        out_dir=out_dir,
                        data_dir=data_dir,
                        progress_cb=progress_cb,
                        cancel_check=cancel_check,
                    ),
                )
                runners: list[tuple[str, Callable[[], list[RawSegment]]]] = (
                    [wcpp_entry, fw_entry] if whisper_cpp_ready else [fw_entry, wcpp_entry]
                )
                api_url = cfg.api_base_url.strip().rstrip("/")
                if api_url:
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

        # The cache keeps the raw segments with their scores; filtering runs on every
        # load, so a better filter needs no re-transcription.
        raw = filter_hallucinations(
            raw,
            duration_ms=int(round(_wav_duration_s(wav_path) * 1000)),
            notes=notes,
            has_speech=_energy_speech_check(wav_path),
        )
        cues = normalize_asr_segments_to_cues(raw)
        if not cues:
            raise AsrError("Không nhận diện được lời thoại nào trong tệp video/âm thanh.")
        return refine_cues_speech_bounds(cues, wav_path)
    finally:
        if temp_dir_obj is not None:
            with contextlib.suppress(OSError):
                temp_dir_obj.cleanup()
