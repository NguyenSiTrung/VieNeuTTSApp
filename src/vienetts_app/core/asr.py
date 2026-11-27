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
import datetime
import email.utils
import http.client
import importlib.util
import json
import math
import os
import platform
import re
import sys
import tempfile
import threading
import time
import unicodedata
import uuid
import zlib
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request

import numpy as np

from vienetts_app.core.ai_models import (
    WHISPER_GGUF_MODELS,
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
from vienetts_app.core.llm_client import (
    RequestCancelled,
    ResponseTooLargeError,
    UnsafeRedirectError,
    cancellable_request,
    http_error_detail,
    validate_http_url,
)
from vienetts_app.core.processes import CliCancelled, CliTimeout, find_executable, run_cli
from vienetts_app.core.subtitles import NON_SPEECH_LABELS, Cue, clean_cue_text, parse_cues

_CLAUSE_SPLIT_RE = re.compile(r"(?<=[.!?…,;:])\s+|(?<=[。！？，；：])\s*")
# Han ideographs + Japanese kana (scripts written without ASCII spaces).
_CJK_CHAR_RE = re.compile(
    r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff"
    r"\u3041-\u3096\u309d-\u309f\u30a1-\u30fa\u30fc-\u30ff\uff66-\uff9f"
    r"\U00020000-\U0002fa1f]"
)
_WHISPER_PP_RE = re.compile(r"progress\s*=\s*(\d+)\s*%", re.IGNORECASE)
_NON_SPEECH_TOKEN_RE = re.compile(
    r"(?:\[\s*(?:blank_audio|music|applause|laughter|silence|noise|inaudible|sound|sfx|cheering|cough|sigh)\s*\]"
    r"|\(\s*(?:blank_audio|music|applause|laughter|silence|noise|inaudible|sound|sfx|cheering|cough|sigh)\s*\)"
    r"|[♪♫]+)",
    re.IGNORECASE,
)
# whisper.cpp special tokens (``[_BEG_]``, ``[_TT_150]``, ``[_SOT_]``…) carry no text.
_WHISPER_SPECIAL_TOKEN_RE = re.compile(r"\[_[^\]]*\]")
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)\S+")
MODEL_SAMPLE_RATE = 16_000
API_CHUNK_SECONDS = 600
_API_MAX_RETRIES = 2
_API_RETRY_BASE_DELAY_S = 0.5
_API_RETRY_MAX_DELAY_S = 30.0
_API_RETRY_POLL_S = 0.1
ASR_CACHE_FILENAME = "asr_segments_cache.json"
# Bumped whenever the backends' output changes (2: whisper.cpp ``-mc 0``/VAD/JSON-full
# words), so a segments cache written by an older version re-runs instead of being reused.
ASR_VERSION = 2
# One loaded faster-whisper model, reused across runs and released once idle for
# FASTER_WHISPER_IDLE_S (see release_idle_asr_models) or by release_asr_models().
_FASTER_WHISPER_CACHE: dict[tuple[Any, str, str, str, int], Any] = {}
FASTER_WHISPER_IDLE_S = 600.0
_clock: Callable[[], float] = time.monotonic
_fw_lock = threading.Lock()
_fw_active = 0  # runs currently using the cached model
_fw_last_used = 0.0  # _clock() when the last run started or ended
_fw_generation = 0  # incremented on release so in-flight loads do not re-populate cache


class AsrError(RuntimeError):
    """Speech recognition failed or no ASR backend is available; message is user-actionable."""


class AsrCancelled(AsrError):
    """The user cancelled speech recognition; auto fallback must never swallow this."""


class AsrBackendMissing(AsrError):
    """A backend is not installed (package, ``whisper-cli`` or model file).

    Auto mode moves on to the next backend only on this type; any other
    :class:`AsrError` is a real failure and is surfaced, never uploaded instead.
    """


_CLOUD_NOTE = "Đã dùng Whisper API (đám mây)"


@dataclass(frozen=True)
class AsrConfig:
    backend: str = "auto"  # "auto" | "faster_whisper" | "whisper_cpp" | "openai_api"
    model: str = "base"
    language: str = "auto"
    api_base_url: str = ""
    api_key: str = ""
    device: str = "auto"  # "auto" | "cpu" | "cuda" | "metal"
    threads: int = 0  # 0 = automatic (default_asr_threads())
    timeout_s: float = 0.0  # 0 = derive from audio duration
    api_model: str = ""
    # Auto mode uploads to ``api_base_url`` only when this is set and no local backend
    # is installed; an explicit ``openai_api`` backend always uploads.
    cloud_fallback: bool = False


@dataclass
class _RunReport:
    """What one backend run learned besides its segments; kept only when the run succeeds."""

    language: str = ""
    notes: list[str] = field(default_factory=list)
    backend: str = ""
    model: str = ""

    def set_language(self, value: Any) -> None:
        if not self.language and isinstance(value, str) and value.strip():
            self.language = value.strip()[:32]


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


def _parse_finite_float(value: Any) -> float | None:
    """Parse a finite non-boolean numeric value as float, else ``None``."""
    if value is None or isinstance(value, bool):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    return num if math.isfinite(num) else None


def _opt_float(value: Any) -> float | None:
    """A backend's numeric score as float; None when absent or not a finite number."""
    if isinstance(value, bool) or not isinstance(value, int | float):
        return None
    num = float(value)
    return num if math.isfinite(num) else None


def _redact_secret(text: str, secret: str = "") -> str:
    redacted = _BEARER_RE.sub(r"\1[redacted]", text)
    if secret:
        redacted = redacted.replace(secret, "[redacted]")
    return redacted


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
            s_val = _parse_finite_float(item[0])
            e_val = _parse_finite_float(item[1])
            if s_val is None or e_val is None:
                continue
            w_start = int(round(s_val))
            w_end = int(round(e_val))
            w_txt = _strip_non_speech(str(item[2] or ""))
        elif isinstance(item, dict):
            w_raw = str(item.get("word") or item.get("text") or "").strip()
            w_txt = _strip_non_speech(w_raw)
            if "start_ms" in item or "end_ms" in item:
                s_val = (
                    _parse_finite_float(item["start_ms"])
                    if "start_ms" in item
                    else float(seg_start_ms)
                )
                if s_val is None:
                    continue
                e_val = _parse_finite_float(item["end_ms"]) if "end_ms" in item else s_val
                if e_val is None:
                    continue
                w_start = int(round(s_val))
                w_end = int(round(e_val))
            else:
                s_val = (
                    _parse_finite_float(item["start"]) if "start" in item else seg_start_ms / 1000.0
                )
                if s_val is None:
                    continue
                e_val = _parse_finite_float(item["end"]) if "end" in item else s_val
                if e_val is None:
                    continue
                w_start = int(round(s_val * 1000))
                w_end = int(round(e_val * 1000))
        else:
            w_raw = str(getattr(item, "word", None) or getattr(item, "text", None) or "").strip()
            w_txt = _strip_non_speech(w_raw)
            s_val = _parse_finite_float(getattr(item, "start", seg_start_ms / 1000.0))
            if s_val is None:
                continue
            e_val = _parse_finite_float(getattr(item, "end", s_val))
            if e_val is None:
                continue
            w_start = int(round(s_val * 1000))
            w_end = int(round(e_val * 1000))
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

    # Split at clause boundaries first, then word boundaries (or character boundaries
    # for spaceless CJK clauses) if still over eff_max_chars.
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
                if len(w) > eff_max_chars:
                    if buf:
                        chunks.append(buf)
                        buf = ""
                    for pos in range(0, len(w), eff_max_chars):
                        chunks.append(w[pos : pos + eff_max_chars])
                    continue
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
        else:
            sep = (
                ""
                if (
                    _CJK_CHAR_RE.search(clause[0])
                    and (_CJK_CHAR_RE.search(current[-1]) or current[-1] in "。！？，；：、")
                )
                else " "
            )
            if len(current) + len(sep) + len(clause) <= eff_max_chars:
                current = f"{current}{sep}{clause}"
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


def _sysctl(name: str) -> str | None:
    """``sysctl -n name`` (macOS), or None when it cannot be read."""
    binary = find_executable("sysctl", well_known_dirs=("/usr/sbin", "/sbin", "/usr/bin"))
    if binary is None:
        return None
    try:
        res = run_cli([str(binary), "-n", name], timeout=5.0)
    except (OSError, CliTimeout):
        return None
    return res.stdout if res.returncode == 0 else None


def default_asr_threads(
    *,
    system: str | None = None,
    machine: str | None = None,
    sysctl: Callable[[str], str | None] = _sysctl,
    cpu_count: Callable[[], int | None] = os.cpu_count,
) -> int:
    """Automatic ASR thread count, capped at 8.

    Apple Silicon: its performance-core count (``hw.perflevel0.physicalcpu``),
    since work spread onto efficiency cores finishes later. Elsewhere, or when
    sysctl cannot be read: half the logical CPUs (SMT siblings add little).
    """
    system = platform.system() if system is None else system
    machine = platform.machine() if machine is None else machine
    if system == "Darwin" and machine.lower() in ("arm64", "aarch64"):
        try:
            perf_cores = int(str(sysctl("hw.perflevel0.physicalcpu") or "").strip())
        except ValueError:
            perf_cores = 0
        if perf_cores > 0:
            return min(8, perf_cores)
    return max(1, min(8, int(cpu_count() or 2) // 2))


def _resolve_threads(config: AsrConfig) -> int:
    if int(config.threads) > 0:
        return max(1, min(64, int(config.threads)))
    return default_asr_threads()


def release_asr_models() -> None:
    """Drop the cached faster-whisper model (app shutdown, memory pressure).

    A run still using it keeps its own reference until it finishes; an in-flight
    model load observes the bumped generation and skips re-inserting into cache.
    """
    global _fw_generation
    with _fw_lock:
        _fw_generation += 1
        _FASTER_WHISPER_CACHE.clear()


def release_idle_asr_models(*, idle_s: float = FASTER_WHISPER_IDLE_S) -> bool:
    """Release the cached faster-whisper model once no run has used it for ``idle_s``.

    Polled — at the start of every transcription and from the app's periodic
    tick — rather than by a timer thread. Returns True when a model was released.
    """
    global _fw_generation
    with _fw_lock:
        if not _FASTER_WHISPER_CACHE or _fw_active or _clock() - _fw_last_used < idle_s:
            return False
        _fw_generation += 1
        _FASTER_WHISPER_CACHE.clear()
        return True


@contextlib.contextmanager
def _faster_whisper_in_use() -> Iterator[None]:
    """Mark the cached model busy, so the idle release never fires mid-run."""
    global _fw_active, _fw_last_used
    with _fw_lock:
        _fw_active += 1
        _fw_last_used = _clock()
    try:
        yield
    finally:
        with _fw_lock:
            _fw_active -= 1
            _fw_last_used = _clock()


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
    report: _RunReport | None = None,
) -> list[RawSegment]:
    if cancel_check is not None and cancel_check():
        raise AsrCancelled("Đã hủy nhận diện giọng nói.")
    try:
        import faster_whisper  # type: ignore[import-not-found]
    except Exception as exc:
        raise AsrBackendMissing(
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
        with _faster_whisper_in_use():
            return _transcribe_faster_whisper(
                faster_whisper,
                wav_path,
                model_name=model_name,
                lang=lang,
                fw_device=fw_device,
                compute_type=compute_type,
                threads=threads,
                progress_cb=progress_cb,
                cancel_check=cancel_check,
                report=report,
            )
    except AsrError:
        raise
    except Exception as exc:
        raise AsrError(f"Lỗi khi chạy faster-whisper: {exc}") from exc


def _transcribe_faster_whisper(
    faster_whisper: Any,
    wav_path: Path,
    *,
    model_name: str,
    lang: str | None,
    fw_device: str,
    compute_type: str,
    threads: int,
    progress_cb: Callable[[float, str], None] | None,
    cancel_check: Callable[[], bool] | None,
    report: _RunReport | None,
) -> list[RawSegment]:
    cache_key = (faster_whisper.WhisperModel, model_name, fw_device, compute_type, threads)
    with _fw_lock:
        load_gen = _fw_generation
        model = _FASTER_WHISPER_CACHE.get(cache_key)
        if model is None:
            _FASTER_WHISPER_CACHE.clear()  # free the previous model before loading another
    if model is None:
        model = faster_whisper.WhisperModel(
            model_name,
            device=fw_device,
            compute_type=compute_type,
            cpu_threads=threads,
        )
        with _fw_lock:
            if _fw_generation == load_gen:
                _FASTER_WHISPER_CACHE[cache_key] = model
    if cancel_check is not None and cancel_check():
        raise AsrCancelled("Đã hủy nhận diện giọng nói.")
    if report is not None:
        report.backend = "faster_whisper"
        report.model = model_name
    segments_iter, info = model.transcribe(
        str(wav_path),
        language=lang,
        vad_filter=True,
        condition_on_previous_text=False,
        word_timestamps=True,
    )
    total_s = float(getattr(info, "duration", 0.0) or 0.0)
    if report is not None:
        report.set_language(getattr(info, "language", None))
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
            sp_start, sp_end = _clamp_speech_bounds(seg_start_ms, seg_end_ms, None, None, seg_words)
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


def _has_incomplete_utf8_tail(text: str) -> bool:
    """True when ``text`` ends with surrogateescape raw bytes forming an incomplete UTF-8 char."""
    if not text or not (0xDC80 <= ord(text[-1]) <= 0xDCFF):
        return False
    try:
        text.encode("utf-8", "surrogateescape").decode("utf-8")
    except UnicodeDecodeError:
        return True
    return False


def _starts_with_utf8_continuation(text: str) -> bool:
    return bool(text) and 0xDC80 <= ord(text[0]) <= 0xDCBF


def _is_cjk_char(ch: str) -> bool:
    return bool(ch) and _CJK_CHAR_RE.match(ch) is not None


def _ends_with_cjk(text: str) -> bool:
    rejoined = _rejoin_utf8(text).rstrip()
    return bool(rejoined) and _is_cjk_char(rejoined[-1])


def _split_cjk_piece(start_ms: int, end_ms: int, text: str) -> list[tuple[int, int, str]]:
    """Split a piece containing CJK script into timed units (1 per CJK char, Latin intact)."""
    if not _CJK_CHAR_RE.search(text):
        return [(start_ms, end_ms, text)]
    units: list[str] = []
    leading_punct = ""
    latin_buf = ""

    def _flush_latin() -> None:
        nonlocal latin_buf, leading_punct
        if latin_buf:
            units.append(leading_punct + latin_buf)
            leading_punct = ""
            latin_buf = ""

    for ch in text:
        if ch.isspace():
            _flush_latin()
            continue
        if _is_cjk_char(ch):
            _flush_latin()
            units.append(leading_punct + ch)
            leading_punct = ""
        elif ch.isalnum():
            latin_buf += ch
        else:
            if latin_buf:
                latin_buf += ch
            elif units:
                units[-1] += ch
            else:
                leading_punct += ch
    _flush_latin()
    if leading_punct:
        if units:
            units[-1] += leading_punct
        else:
            units.append(leading_punct)
    if len(units) <= 1:
        return [(start_ms, end_ms, units[0] if units else text)]

    total = max(1, sum(len(u) for u in units))
    span = max(0, end_ms - start_ms)
    out: list[tuple[int, int, str]] = []
    acc = 0
    cursor = start_ms
    for idx, unit in enumerate(units):
        acc += len(unit)
        u_end = (
            end_ms
            if idx == len(units) - 1
            else min(end_ms, start_ms + int(round(span * acc / total)))
        )
        u_end = max(cursor, u_end)
        out.append((cursor, u_end, unit))
        cursor = u_end
    return out


def _merge_whisper_tokens(
    tokens: Any, seg_start_ms: int, seg_end_ms: int
) -> tuple[tuple[tuple[int, int, str], ...], float | None]:
    """whisper.cpp ``-ojf`` tokens → ``(words, avg_logprob)``.

    A token that opens with whitespace (or starts/follows a CJK character once
    any split UTF-8 byte sequence is complete) starts a word; sub-word Latin
    pieces, punctuation, and UTF-8 byte continuations extend the current one.
    Multi-character CJK tokens are split into per-character timed units so long
    CJK segments without ASCII spaces remain splittable. Special tokens are
    skipped. Words are clamped to the segment; without per-token offsets there
    are no words. ``avg_logprob`` is the mean log-probability of the text tokens.
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
        if not pieces:
            pieces.append([t0, t1, text])
        elif _has_incomplete_utf8_tail(pieces[-1][2]) or _starts_with_utf8_continuation(text):
            pieces[-1][1] = max(pieces[-1][1], t1)
            pieces[-1][2] += text
        elif (
            text[0].isspace()
            or _is_cjk_char(text[0])
            or (
                _ends_with_cjk(pieces[-1][2])
                and (text[0].isalnum() or 0xDCC0 <= ord(text[0]) <= 0xDCFF)
            )
        ):
            pieces.append([t0, t1, text])
        else:
            pieces[-1][1] = max(pieces[-1][1], t1)
            pieces[-1][2] += text
    avg_logprob = sum(logprobs) / len(logprobs) if logprobs else None
    if not timed:
        return (), avg_logprob
    raw_words: list[tuple[int, int, str]] = []
    for start, end, raw_text in pieces:
        raw_words.extend(_split_cjk_piece(start, end, _rejoin_utf8(raw_text)))
    return _extract_words(raw_words, seg_start_ms, seg_end_ms), avg_logprob


def _parse_whisper_json(raw: bytes) -> tuple[list[RawSegment], str]:
    """Segments with word timings, and the language, from ``whisper-cli -ojf`` output.

    Builds before b5454 write each token's bytes unescaped, so one character
    can be split across two tokens (invalid UTF-8 in each JSON string): the
    file is decoded with ``surrogateescape`` and the bytes are rejoined per
    word. Offsets are in ms. Raises ``ValueError`` on anything but JSON-full.
    """
    payload = json.loads(raw.decode("utf-8", errors="surrogateescape"), strict=False)
    transcription = payload.get("transcription") if isinstance(payload, dict) else None
    if not isinstance(transcription, list):
        raise ValueError("missing 'transcription'")
    result = payload.get("result")
    language = result.get("language") if isinstance(result, dict) else None
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
    return out, language.strip() if isinstance(language, str) else ""


def _read_whisper_cpp_output(json_out: Path, srt_out: Path) -> tuple[list[RawSegment], str]:
    """Parse the JSON-full output; the SRT (no language) is read only without valid JSON."""
    if json_out.is_file():
        with contextlib.suppress(OSError, ValueError):
            return _parse_whisper_json(json_out.read_bytes())
    if not srt_out.is_file() or srt_out.stat().st_size == 0:
        return [], ""
    try:
        cues = parse_cues(srt_out.read_text(encoding="utf-8", errors="replace"))
    except Exception as exc:
        raise AsrError(f"Tệp phụ đề từ whisper.cpp không hợp lệ: {exc}") from exc
    return [RawSegment(c.start_ms, c.end_ms, c.text, c.speaker) for c in cues], ""


_WHISPER_MODEL_DOWNLOAD_HINT = "Hãy tải mô hình trong Cài đặt → Mô hình → Mô hình Whisper (GGUF)."


def _whisper_model_choice(model: str, data_dir: Path | None) -> tuple[Path, str] | None:
    """``(model file, catalog id it fell back to)`` — the id is "" for the configured model."""
    path = resolve_asr_model_path((model or "base").strip() or "base", data_dir)
    if path is not None:
        return path, ""
    if data_dir is None:
        return None
    for spec in sorted(WHISPER_GGUF_MODELS.values(), key=lambda s: s.size_bytes, reverse=True):
        path = resolve_asr_model_path(spec.id, data_dir)
        if path is not None:
            return path, spec.id
    return None


def resolve_whisper_model(
    model: str, data_dir: Path | None, *, notes: list[str] | None = None
) -> Path:
    """The whisper.cpp model file for ``model``.

    The configured model when it is installed; otherwise the largest installed
    catalog model, with a note. Raises :class:`AsrBackendMissing` naming the
    Settings download when no model is installed.
    """
    requested = (model or "base").strip() or "base"
    choice = _whisper_model_choice(requested, data_dir)
    if choice is None:
        raise AsrBackendMissing(
            f"Chưa tải mô hình Whisper {requested} cho whisper.cpp. {_WHISPER_MODEL_DOWNLOAD_HINT}"
        )
    path, fallback = choice
    if fallback and notes is not None:
        shown = requested if requested in WHISPER_GGUF_MODELS else Path(requested).name
        notes.append(f"Chưa tải mô hình Whisper {shown}, đã dùng mô hình {fallback} có sẵn.")
    return path


def _run_whisper_cpp(
    wav_path: Path,
    config: AsrConfig,
    *,
    out_dir: Path,
    data_dir: Path | None = None,
    progress_cb: Callable[[float, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    report: _RunReport | None = None,
) -> list[RawSegment]:
    """Run ``whisper-cli``; its output files go to ``out_dir`` (the media cache) and are removed."""
    if cancel_check is not None and cancel_check():
        raise AsrCancelled("Đã hủy nhận diện giọng nói.")
    binary = _resolve_whisper_cpp_bin(data_dir)
    if binary is None:
        raise AsrBackendMissing(
            "Chưa cài runtime whisper.cpp (whisper-cli). "
            "Hãy bấm 'Cài runtime whisper.cpp' trong Cài đặt → Mô hình."
        )
    requested_model = (config.model or "base").strip() or "base"
    choice = _whisper_model_choice(requested_model, data_dir)
    model_path = resolve_whisper_model(
        config.model, data_dir, notes=report.notes if report is not None else None
    )
    if report is not None:
        report.backend = "whisper_cpp"
        report.model = (choice[1] or requested_model) if choice is not None else requested_model

    if progress_cb is not None:
        progress_cb(0.2, "Đang chạy whisper.cpp…")
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise AsrError(f"Không thể tạo thư mục tạm cho whisper.cpp: {exc}") from exc
    # Absolute paths everywhere because whisper-cli runs with cwd=binary_abs.parent.
    binary_abs = binary.absolute()
    model_abs = model_path.absolute()
    wav_abs = wav_path.absolute()
    out_prefix = out_dir.absolute() / f".whisper_out_{uuid.uuid4().hex}"
    json_out = Path(f"{out_prefix}.json")
    srt_out = Path(f"{out_prefix}.srt")
    lang = "auto" if not config.language else config.language
    threads = _resolve_threads(config)
    # -mc 0: no previous-text context, which stops repetition loops from carrying over
    # windows. -ojf: segments plus per-token offsets/probabilities; -osrt is the fallback.
    cmd = cli_command(
        binary_abs,
        "-m",
        str(model_abs),
        "-f",
        str(wav_abs),
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
        cmd.extend(["--vad", "-vm", str(vad_model.absolute())])
    if (config.device or "auto").strip().lower() == "cpu":
        cmd.append("-ng")

    duration_s = _wav_duration_s(wav_abs)
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
                cwd=binary_abs.parent,
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
        segments, language = _read_whisper_cpp_output(json_out, srt_out)
        if report is not None:
            report.set_language(language)
        if progress_cb is not None:
            progress_cb(1.0, "Hoàn tất nhận diện giọng nói.")
        return segments
    finally:
        for out_file in (json_out, srt_out):
            with contextlib.suppress(OSError):
                out_file.unlink(missing_ok=True)


def _whisper_api_url(config: AsrConfig) -> str:
    """Validate and build the ``/audio/transcriptions`` HTTP(S) endpoint URL."""
    raw_base = config.api_base_url if config.api_base_url else "http://localhost:11434/v1"
    if not raw_base.strip():
        raise AsrError("Chưa cấu hình địa chỉ máy chủ Whisper API.")
    api_key = config.api_key.strip()
    try:
        base_url = validate_http_url(raw_base).rstrip("/")
        return validate_http_url(f"{base_url}/audio/transcriptions")
    except ValueError as exc:
        raise AsrError(
            _redact_secret(f"Địa chỉ máy chủ ASR không hợp lệ: {exc}", api_key)
        ) from None


def _parse_retry_after(header_value: str | None) -> float | None:
    """Parse HTTP ``Retry-After`` seconds or HTTP-date into a non-negative delay."""
    if not header_value:
        return None
    raw = header_value.strip()
    if not raw:
        return None
    try:
        seconds = float(raw)
    except ValueError:
        pass
    else:
        return seconds if math.isfinite(seconds) and seconds >= 0.0 else None
    try:
        dt = email.utils.parsedate_to_datetime(raw)
    except (TypeError, ValueError, OverflowError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return max(0.0, dt.timestamp() - time.time())


def _wait_retry_delay(delay_s: float, cancel_check: Callable[[], bool] | None) -> None:
    """Wait ``delay_s`` via ``Event.wait``, waking promptly if ``cancel_check`` turns true."""
    if cancel_check is not None and cancel_check():
        raise AsrCancelled("Đã hủy nhận diện giọng nói.")
    if delay_s <= 0:
        return
    stopper = threading.Event()
    deadline = _clock() + delay_s
    poll_s = max(0.001, float(_API_RETRY_POLL_S))
    while True:
        if cancel_check is not None and cancel_check():
            raise AsrCancelled("Đã hủy nhận diện giọng nói.")
        remaining = deadline - _clock()
        if remaining <= 0:
            return
        stopper.wait(min(remaining, poll_s))


def _attach_top_level_words(
    segments: list[RawSegment], top_words_raw: list[Any]
) -> list[RawSegment]:
    """Attach top-level API ``words`` to segments without words in O((S + W) log(S + W))."""
    need_words = [i for i, seg in enumerate(segments) if not seg.words]
    if not need_words or not top_words_raw:
        return segments
    parsed_words: list[tuple[int, int, int, int, int, str]] = []
    for w_idx, w_item in enumerate(top_words_raw):
        if not isinstance(w_item, dict):
            continue
        w_txt = _strip_non_speech(str(w_item.get("word") or w_item.get("text") or "").strip())
        if not w_txt:
            continue
        if "start" in w_item:
            ws_s = _parse_finite_float(w_item["start"])
            if ws_s is None:
                continue
        else:
            ws_s = -1.0
        if "end" in w_item:
            we_s = _parse_finite_float(w_item["end"])
            if we_s is None:
                continue
        else:
            we_s = ws_s
        ws = int(round(ws_s * 1000))
        we = int(round(we_s * 1000))
        parsed_words.append((ws, max(ws + 1, we), w_idx, ws, we, w_txt))
    if not parsed_words:
        return segments

    parsed_words.sort(key=lambda w: (w[0], w[2]))
    out = list(segments)
    w_ptr = 0
    active: list[tuple[int, int, int, int, int, str]] = []
    for seg_idx in sorted(need_words, key=lambda i: out[i].start_ms):
        seg = out[seg_idx]
        while w_ptr < len(parsed_words) and parsed_words[w_ptr][0] < seg.end_ms:
            active.append(parsed_words[w_ptr])
            w_ptr += 1
        active = [w for w in active if w[1] > seg.start_ms]
        matched = [w for w in active if w[0] < seg.end_ms]
        if matched:
            matched.sort(key=lambda w: w[2])
            clamped = tuple(
                (
                    (c_s := max(seg.start_ms, min(seg.end_ms, raw_ws))),
                    max(c_s, min(seg.end_ms, raw_we)),
                    w_txt,
                )
                for _ws, _we_eff, _oi, raw_ws, raw_we, w_txt in matched
            )
            sp_start, sp_end = _clamp_speech_bounds(seg.start_ms, seg.end_ms, None, None, clamped)
            out[seg_idx] = replace(
                seg, words=clamped, speech_start_ms=sp_start, speech_end_ms=sp_end
            )
    return out


def _upload_whisper_api(
    wav_path: Path,
    config: AsrConfig,
    *,
    progress_cb: Callable[[float, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    prompt: str = "",
    timeout: float = 120,
    report: _RunReport | None = None,
) -> list[RawSegment]:
    if cancel_check is not None and cancel_check():
        raise AsrCancelled("Đã hủy nhận diện giọng nói.")
    url = _whisper_api_url(config)
    api_key = config.api_key.strip()
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
    # OpenAI returns word timings only when asked; segments must be asked for too.
    _add_field("timestamp_granularities[]", "word")
    _add_field("timestamp_granularities[]", "segment")
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
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"

    if progress_cb is not None:
        progress_cb(0.3, "Đang gửi âm thanh tới máy chủ ASR…")

    max_retries = max(0, int(_API_MAX_RETRIES))
    raw_bytes = b""
    for attempt in range(max_retries + 1):
        if cancel_check is not None and cancel_check():
            raise AsrCancelled("Đã hủy nhận diện giọng nói.")
        retry_after = ""
        try:
            req = Request(url, data=body, headers=dict(headers), method="POST")
            raw_bytes = cancellable_request(req, timeout=timeout, cancel_check=cancel_check)
            break
        except RequestCancelled:
            raise AsrCancelled("Đã hủy nhận diện giọng nói.") from None
        except (ValueError, UnsafeRedirectError) as exc:
            raise AsrError(
                _redact_secret(f"Địa chỉ máy chủ ASR không hợp lệ ({url}): {exc}", api_key)
            ) from None
        except ResponseTooLargeError as exc:
            raise AsrError(
                _redact_secret(
                    f"Phản hồi từ máy chủ ASR vượt quá giới hạn kích thước ({url}): {exc}",
                    api_key,
                )
            ) from None
        except HTTPError as exc:
            try:
                detail = http_error_detail(exc, secret=api_key)
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
            finally:
                with contextlib.suppress(Exception):
                    exc.close()
            message = f"Máy chủ ASR HTTP {exc.code} ({url}): {detail}"
            transient = exc.code in (408, 409, 429) or exc.code >= 500
        except (URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
            message = f"Không thể kết nối máy chủ ASR ({url}): {exc}"
            transient = True
        if not transient or attempt >= max_retries:
            raise AsrError(_redact_secret(message, api_key)) from None
        delay = min(float(_API_RETRY_BASE_DELAY_S) * (2**attempt), float(_API_RETRY_MAX_DELAY_S))
        advised = _parse_retry_after(retry_after)
        if advised is not None:
            delay = min(advised, float(_API_RETRY_MAX_DELAY_S))
        _wait_retry_delay(delay, cancel_check)

    if cancel_check is not None and cancel_check():
        raise AsrCancelled("Đã hủy nhận diện giọng nói.")

    if report is not None:
        report.backend = "openai_api"
        report.model = model_name

    raw_resp = raw_bytes.decode("utf-8", errors="replace")
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

    if report is not None and isinstance(payload, dict):
        report.set_language(payload.get("language"))
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
            if "start" in item:
                start_s = _parse_finite_float(item["start"])
                if start_s is None:
                    continue
            else:
                start_s = 0.0
            if "end" in item:
                end_s = _parse_finite_float(item["end"])
                if end_s is None:
                    continue
            else:
                end_s = start_s + 1.0
            start_ms = max(0, int(round(start_s * 1000)))
            end_ms = max(start_ms + 1, int(round(end_s * 1000)))
            spk = str(item.get("speaker") or "").strip()
            seg_words = _extract_words(item.get("words"), start_ms, end_ms)
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
        if isinstance(top_words_raw, list) and top_words_raw:
            out = _attach_top_level_words(out, top_words_raw)
    elif (
        isinstance(payload, dict)
        and isinstance(payload.get("text"), str)
        and payload["text"].strip()
    ):
        dur_s = _parse_finite_float(payload.get("duration")) or 0.0
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


def _read_mono_frames(wav_file: Any, start_frame: int, stop_frame: int) -> np.ndarray:
    total = int(wav_file.frames)
    lo = max(0, min(total, int(start_frame)))
    hi = max(lo, min(total, int(stop_frame)))
    if hi <= lo:
        return np.empty(0, dtype=np.float32)
    wav_file.seek(lo)
    block = wav_file.read(hi - lo, dtype="float32", always_2d=True)
    if block.size == 0:
        return np.empty(0, dtype=np.float32)
    mono = block[:, 0] if block.shape[1] == 1 else block.mean(axis=1, dtype=np.float32)
    return np.ascontiguousarray(mono, dtype=np.float32)


def _plan_api_chunks_from_handle(wav_file: Any) -> list[tuple[int, int]]:
    rate = int(wav_file.samplerate)
    total = int(wav_file.frames)
    limit = max(1, int(API_CHUNK_SECONDS * rate))
    frame_size = max(1, int(rate * 0.2))
    chunks: list[tuple[int, int]] = []
    start = 0
    while total - start > limit:
        boundary = start + limit
        search_start = max(start, boundary - 15 * rate)
        samples = _read_mono_frames(wav_file, search_start, boundary)
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


def _plan_api_chunks(wav_path: Path, *, wav_file: Any | None = None) -> list[tuple[int, int]]:
    if wav_file is not None:
        return _plan_api_chunks_from_handle(wav_file)
    import soundfile as sf

    with sf.SoundFile(str(wav_path)) as fh:
        return _plan_api_chunks_from_handle(fh)


def _run_openai_whisper_api(
    wav_path: Path,
    config: AsrConfig,
    *,
    progress_cb: Callable[[float, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    report: _RunReport | None = None,
) -> list[RawSegment]:
    """Transcribe through ``/audio/transcriptions``, in silence-cut chunks for long audio."""
    if cancel_check is not None and cancel_check():
        raise AsrCancelled("Đã hủy nhận diện giọng nói.")
    _whisper_api_url(config)

    import soundfile as sf

    with sf.SoundFile(str(wav_path)) as src_wav:
        chunks = _plan_api_chunks(wav_path, wav_file=src_wav)
        rate = int(src_wav.samplerate)
        total_frames = int(src_wav.frames)
        single_upload = len(chunks) <= 1 and wav_path.stat().st_size < 24_900_000
        single_timeout = max(120.0, 0.5 * (total_frames / max(1, rate)))
        if not single_upload:
            result: list[RawSegment] = []
            prompt = ""
            block_frames = max(1, rate * 10)
            with tempfile.TemporaryDirectory(prefix="vieneu-asr-") as directory:
                for index, (start, stop) in enumerate(chunks):
                    if cancel_check is not None and cancel_check():
                        raise AsrCancelled("Đã hủy nhận diện giọng nói.")
                    path = Path(directory) / f"chunk_{index:04d}.wav"
                    try:
                        with StreamingWavWriter(path, sample_rate=rate) as writer:
                            for position in range(start, stop, block_frames):
                                samples = _read_mono_frames(
                                    src_wav, position, min(stop, position + block_frames)
                                )
                                if samples.size:
                                    writer.write(samples)
                        if progress_cb is not None:
                            progress_cb(index / len(chunks), "Đang gửi âm thanh tới máy chủ ASR…")
                        segments = _upload_whisper_api(
                            path,
                            config,
                            cancel_check=cancel_check,
                            prompt=prompt,
                            timeout=max(120.0, 0.5 * (stop - start) / max(1, rate)),
                            report=report,
                        )
                    finally:
                        with contextlib.suppress(OSError):
                            path.unlink(missing_ok=True)
                    prompt = " ".join(segment.text for segment in segments)[-200:]
                    offset = int(round(start * 1000 / max(1, rate)))
                    result.extend(
                        replace(
                            segment,
                            start_ms=segment.start_ms + offset,
                            end_ms=segment.end_ms + offset,
                            words=tuple(
                                (a + offset, b + offset, text) for a, b, text in segment.words
                            ),
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

    return _upload_whisper_api(
        wav_path,
        config,
        progress_cb=progress_cb,
        cancel_check=cancel_check,
        timeout=single_timeout,
        report=report,
    )


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


def _has_faster_whisper() -> bool:
    if "faster_whisper" in sys.modules:
        return sys.modules["faster_whisper"] is not None
    try:
        return importlib.util.find_spec("faster_whisper") is not None
    except (ImportError, ValueError):
        return False


def _resolve_auto_target(config: AsrConfig, data_dir: Path | None) -> tuple[str, str]:
    """Predict ``(resolved_backend, resolved_model)`` for ``backend='auto'`` caching."""
    model = (config.model or "base").strip() or "base"
    if _resolve_whisper_cpp_bin(data_dir) is not None:
        choice = _whisper_model_choice(model, data_dir)
        if choice is not None:
            return "whisper_cpp", (choice[1] or model)
    if _has_faster_whisper():
        return "faster_whisper", model
    if config.cloud_fallback and config.api_base_url.strip():
        return "openai_api", ((config.api_model or "whisper-1").strip() or "whisper-1")
    return "none", model


def _asr_cache_key(
    wav_path: Path,
    config: AsrConfig,
    *,
    data_dir: Path | None = None,
    key_path: Path | None = None,
    resolved_backend: str = "",
    resolved_model: str = "",
) -> str:
    """Segments-cache key for ``wav_path``; ``key_path`` names it as a legacy alias.

    In ``auto`` mode the resolved backend and model are encoded too, so
    installing/uninstalling ``whisper-cli``, ``faster-whisper``, or a catalog
    model never serves a mismatched cache entry. ``:vad`` marks whisper.cpp
    runs while its Silero VAD model is installed, and ``:fallback=<id>`` marks
    fallback-model whisper.cpp runs.
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
    effective_backend = backend
    if backend == "auto":
        if not resolved_backend:
            resolved_backend, resolved_model = _resolve_auto_target(config, data_dir)
        elif not resolved_model:
            resolved_model = model
        effective_backend = resolved_backend
        key += f":resolved={resolved_backend}:{resolved_model}"
    if effective_backend == "whisper_cpp":
        if audio_model_path(WHISPER_VAD_MODEL_ID, data_dir) is not None:
            key += ":vad"
        choice = _whisper_model_choice(model, data_dir)
        if choice is not None and choice[1] and _resolve_whisper_cpp_bin(data_dir) is not None:
            key += f":fallback={choice[1]}"
    return key


def _load_cached_segments(
    work_dir: Path | None, key: str, *, aliases: Sequence[str] = ()
) -> tuple[list[RawSegment], str] | None:
    """``(segments, detected language)`` stored under ``key`` (or a legacy ``aliases`` key).

    A cache from another :data:`ASR_VERSION` (or an unversioned one) is a miss; the
    optional ``language`` is "" in caches written before it was stored.
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
    if not out:
        return None
    language = data.get("language")
    return out, language.strip() if isinstance(language, str) else ""


def _store_cached_segments(
    work_dir: Path | None, key: str, segments: Sequence[RawSegment], *, language: str = ""
) -> None:
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
    payload: dict[str, Any] = {"asr_version": ASR_VERSION, "key": key, "segments": raw}
    if language:
        payload["language"] = language
    with contextlib.suppress(OSError):
        work_dir.mkdir(parents=True, exist_ok=True)
        (work_dir / ASR_CACHE_FILENAME).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )


def _run_asr_backends(
    wav_path: Path,
    cfg: AsrConfig,
    *,
    out_dir: Path,
    data_dir: Path | None,
    progress_cb: Callable[[float, str], None] | None,
    cancel_check: Callable[[], bool] | None,
) -> tuple[list[RawSegment], _RunReport]:
    """Run the configured backend, or the ``auto`` chain.

    Auto tries the local backends (whisper.cpp first when it has a model). Only
    :class:`AsrBackendMissing` moves on: any other failure is real and is raised
    once no local backend succeeded. The cloud API runs only when every local
    backend is missing, ``cfg.cloud_fallback`` is set and an ASR URL is configured.
    """

    def faster_whisper(report: _RunReport) -> list[RawSegment]:
        return _run_faster_whisper(
            wav_path, cfg, progress_cb=progress_cb, cancel_check=cancel_check, report=report
        )

    def whisper_cpp(report: _RunReport) -> list[RawSegment]:
        return _run_whisper_cpp(
            wav_path,
            cfg,
            out_dir=out_dir,
            data_dir=data_dir,
            progress_cb=progress_cb,
            cancel_check=cancel_check,
            report=report,
        )

    def openai_api(report: _RunReport) -> list[RawSegment]:
        raw = _run_openai_whisper_api(
            wav_path, cfg, progress_cb=progress_cb, cancel_check=cancel_check, report=report
        )
        report.notes.append(_CLOUD_NOTE)  # every cloud transcription says so
        return raw

    backend = (cfg.backend or "auto").strip()
    explicit = {
        "faster_whisper": faster_whisper,
        "whisper_cpp": whisper_cpp,
        "openai_api": openai_api,
    }.get(backend)
    if explicit is not None:
        report = _RunReport()
        return explicit(report), report

    # whisper.cpp goes first whenever it can run, on the configured or a fallback model.
    local = [("whisper.cpp", whisper_cpp), ("faster-whisper", faster_whisper)]
    whisper_cpp_model = _whisper_model_choice(cfg.model, data_dir)
    if _resolve_whisper_cpp_bin(data_dir) is None or whisper_cpp_model is None:
        local.reverse()
    failures: list[str] = []
    real_errors: list[AsrError] = []
    missing: AsrBackendMissing | None = None
    for name, runner in local:
        report = _RunReport()
        try:
            raw = runner(report)
        except AsrCancelled:
            raise
        except AsrError as exc:
            failures.append(f"Không dùng được {name}: {exc}")
            if isinstance(exc, AsrBackendMissing):
                missing = exc
            else:
                real_errors.append(exc)
            continue
        report.notes[:0] = failures
        return raw, report
    if real_errors:
        raise real_errors[0]
    if cfg.cloud_fallback and cfg.api_base_url.strip():
        report = _RunReport(notes=failures)
        return openai_api(report), report
    raise AsrBackendMissing(
        "Không tìm thấy bộ nhận diện giọng nói (ASR) khả dụng. Hãy tải mô hình Whisper "
        "và cài runtime whisper.cpp trong Cài đặt → Mô hình, hoặc cấu hình Whisper API "
        "và bật dùng Whisper API (đám mây) khi không có ASR cục bộ."
    ) from missing


def transcribe_segments(
    wav_path: Path,
    config: AsrConfig | None = None,
    *,
    work_dir: Path | None = None,
    data_dir: Path | None = None,
    progress_cb: Callable[[float, str], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    notes: list[str] | None = None,
) -> list[RawSegment]:
    """Run Whisper ASR on a prepared 16 kHz mono WAV and return the filtered raw segments.

    The segments keep their word timings, so a caller can split them at speaker turns
    (``diarization.split_segments_at_turns``) before ``segments_to_cues``. Segments are
    cached in ``work_dir`` (the media cache) like ``transcribe_media_to_cues`` does.
    """
    release_idle_asr_models()
    if cancel_check is not None and cancel_check():
        raise AsrCancelled("Đã hủy nhận diện giọng nói.")
    cfg = config or AsrConfig()
    wav_path = Path(wav_path)
    temp_dir_obj: tempfile.TemporaryDirectory[str] | None = None
    try:
        # whisper-cli output goes to the media cache (or this run's temp dir): a 16 kHz
        # mono WAV is used in place, and nothing may be written beside the user's file.
        if work_dir is not None:
            out_dir = work_dir
        else:
            temp_dir_obj = tempfile.TemporaryDirectory(prefix="vienetts_asr_")
            out_dir = Path(temp_dir_obj.name)

        cache_key = _asr_cache_key(wav_path, cfg, data_dir=data_dir)
        # A legacy input_16k.wav adopted as source_16k.wav keeps its size and
        # mtime, so segments cached against the old name still apply.
        aliases = [
            _asr_cache_key(wav_path, cfg, data_dir=data_dir, key_path=p)
            for p in cache_key_paths(wav_path)[1:]
        ]
        cached = _load_cached_segments(work_dir, cache_key, aliases=aliases)
        run_notes: list[str] = []
        if cached is not None:
            if progress_cb is not None:
                progress_cb(1.0, "Hoàn tất nhận diện giọng nói.")
            raw, language = cached
        else:
            raw, report = _run_asr_backends(
                wav_path,
                cfg,
                out_dir=out_dir,
                data_dir=data_dir,
                progress_cb=progress_cb,
                cancel_check=cancel_check,
            )
            language = report.language
            run_notes = report.notes
            if raw:
                store_key = (
                    _asr_cache_key(
                        wav_path,
                        cfg,
                        data_dir=data_dir,
                        resolved_backend=report.backend,
                        resolved_model=report.model,
                    )
                    if (cfg.backend or "auto").strip() == "auto" and report.backend
                    else cache_key
                )
                _store_cached_segments(work_dir, store_key, raw, language=language)
        if notes is not None:
            notes.extend(run_notes)
            if language and (cfg.language or "auto").strip().lower() in ("", "auto"):
                notes.append(f"Ngôn ngữ nhận diện được: {language}.")

        # The cache keeps the raw segments with their scores; filtering runs on every
        # load, so a better filter needs no re-transcription.
        return filter_hallucinations(
            raw,
            duration_ms=int(round(_wav_duration_s(wav_path) * 1000)),
            notes=notes,
            has_speech=_energy_speech_check(wav_path),
        )
    finally:
        if temp_dir_obj is not None:
            with contextlib.suppress(OSError):
                temp_dir_obj.cleanup()


def segments_to_cues(
    segments: Sequence[RawSegment], wav_path: Path, *, data_dir: Path | None = None
) -> list[Cue]:
    """Normalize raw ASR segments into ``Cue``s with refined speech bounds.

    With a ``data_dir`` the speech bounds come from Silero VAD when it is installed.
    """
    cues = normalize_asr_segments_to_cues(list(segments))
    if not cues:
        raise AsrError("Không nhận diện được lời thoại nào trong tệp video/âm thanh.")
    return refine_cues_speech_bounds(cues, wav_path, data_dir=data_dir)


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
    release_idle_asr_models()
    if cancel_check is not None and cancel_check():
        raise AsrCancelled("Đã hủy nhận diện giọng nói.")
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
        raw = transcribe_segments(
            wav_path,
            config,
            work_dir=work_dir,
            data_dir=data_dir,
            progress_cb=progress_cb,
            cancel_check=cancel_check,
            notes=notes,
        )
        return segments_to_cues(raw, wav_path, data_dir=data_dir)
    finally:
        if temp_dir_obj is not None:
            with contextlib.suppress(OSError):
                temp_dir_obj.cleanup()
