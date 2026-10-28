"""OpenAI-compatible LLM provider for subtitle ASR cleanup, timing condensation, and translation.

Zero external dependencies: talks to ``{base_url}/chat/completions`` via
``urllib.request``, working seamlessly with local Ollama, LM Studio, llama.cpp,
vLLM, or cloud OpenAI / Gemini / DeepSeek / OpenRouter endpoints.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

from vienetts_app.core.llm_client import ChatResult, LlmConfig, LlmRefineError, post_chat_completion
from vienetts_app.core.subtitles import Cue, clean_cue_text

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


LANGUAGE_CHARS_PER_SEC: dict[str, float] = {
    "auto": 14.0,
    "vi": 14.0,
    "vietnamese": 14.0,
    "en": 14.0,
    "english": 14.0,
    "fr": 14.0,
    "french": 14.0,
    "es": 14.5,
    "spanish": 14.5,
    "it": 14.5,
    "italian": 14.5,
    "de": 13.5,
    "german": 13.5,
    "pt": 14.0,
    "portuguese": 14.0,
    "ru": 13.0,
    "russian": 13.0,
    "zh": 4.8,
    "chinese": 4.8,
    "ja": 6.0,
    "japanese": 6.0,
    "ko": 5.5,
    "korean": 5.5,
}


def _resolve_chars_per_sec(language: str, chars_per_sec: float | None) -> float:
    if chars_per_sec is not None:
        return float(chars_per_sec)
    key = (language or "").strip().lower()
    if not key:
        return 14.0
    if key in LANGUAGE_CHARS_PER_SEC:
        return LANGUAGE_CHARS_PER_SEC[key]
    base = re.split(r"[-_\s(]", key, maxsplit=1)[0]
    return LANGUAGE_CHARS_PER_SEC.get(base, 14.0)


def max_chars_for_window(
    duration_ms: int,
    *,
    chars_per_sec: float | None = None,
    max_rate: float = 1.25,
    language: str = "vi",
    slack_ms: int = 0,
) -> int:
    """Maximum comfortable spoken character count for a ``duration_ms`` subtitle window."""
    cps = _resolve_chars_per_sec(language, chars_per_sec)
    effective_ms = max(0, int(duration_ms)) + max(0, min(int(slack_ms), 800))
    secs = max(0.1, float(effective_ms) / 1000.0)
    budget = int(round(secs * cps * min(float(max_rate), 1.35)))
    min_chars = 4 if cps <= 7.0 else 8
    return max(min_chars, budget)


def _post_chat_completion(
    config: LlmConfig,
    messages: list[dict[str, str]],
    *,
    temperature: float | None = 0.2,
    json_mode: bool = False,
    cancel_check: Callable[[], bool] | None = None,
) -> ChatResult:
    return post_chat_completion(
        config, messages, temperature=temperature, json_mode=json_mode, cancel_check=cancel_check
    )


def probe_llm_connection(config: LlmConfig) -> tuple[bool, str]:
    """Send a lightweight ping completion to verify ``config``."""
    try:
        reply = _post_chat_completion(
            replace(config, max_retries=0),
            [
                {"role": "system", "content": "Reply with OK."},
                {"role": "user", "content": "TEST_PING"},
            ],
            temperature=0.0,
        )
        return True, f"Kết nối thành công ({config.model}): {reply.content[:40]}"
    except LlmRefineError as exc:
        return False, str(exc)


test_llm_connection = probe_llm_connection
test_llm_connection.__test__ = False  # type: ignore[attr-defined]


def _system_prompt_for_task(task: str, target_language: str) -> str:
    if task == "fit_timing":
        return (
            "You are a professional video dubbing script editor. "
            "For each subtitle cue in the JSON array, if its text is longer than `maxChars`, "
            "condense/rephrase it naturally in the same language so its character length is "
            "at most `maxChars` while preserving the core meaning and spoken tone. "
            "Return ONLY a valid JSON array of objects with `index`, `speaker`, and `text`."
        )
    if task == "translate_dub":
        clean_lang = (target_language or "").strip()
        lang = (
            "Vietnamese" if not clean_lang or clean_lang.lower() in ("auto", "vi") else clean_lang
        )
        return (
            f"You are a professional video dubbing translator. "
            f"Translate each cue's `text` into natural spoken {lang} for voiceover dubbing. "
            f"Keep each translated cue concise (ideally <= `maxChars` characters) so it fits the "
            f"speaker's timing window without rushing. "
            f"Return ONLY a valid JSON array of objects with `index`, `speaker`, and `text`."
        )
    # Default: refine_asr
    return (
        "You are an expert ASR & speaker diarization post-editor for video dubbing. "
        "Fix speech recognition typos, homophones, capitalization, and punctuation in each cue's "
        "`text`, and fix obvious `speaker` label inconsistencies across conversational turns. "
        "Keep the same language and preserve every cue's `index`. "
        "Return ONLY a valid JSON array of objects with `index`, `speaker`, and `text`."
    )


def _parse_refined_batch(raw_reply: str, batch: Sequence[Cue]) -> list[Cue]:
    match = _JSON_ARRAY_RE.search(raw_reply)
    json_str = match.group(0) if match is not None else raw_reply
    try:
        items = json.loads(json_str)
    except json.JSONDecodeError as exc:
        raise LlmRefineError(f"LLM không trả về mảng JSON hợp lệ: {exc}") from exc
    if not isinstance(items, list):
        raise LlmRefineError("Phản hồi LLM không phải danh sách phụ đề JSON.")

    by_index: dict[int, dict[str, Any]] = {}
    for entry in items:
        if isinstance(entry, dict) and "index" in entry:
            try:
                by_index[int(entry["index"])] = entry
            except (TypeError, ValueError):
                continue

    updated: list[Cue] = []
    for pos, orig in enumerate(batch):
        item = by_index.get(orig.index)
        if item is None and pos < len(items) and isinstance(items[pos], dict):
            item = items[pos]
        if item is None:
            updated.append(orig)
            continue
        new_text = clean_cue_text(str(item.get("text") or ""))
        new_spk = str(item.get("speaker") if "speaker" in item else orig.speaker or "").strip()
        updated.append(
            replace(
                orig,
                text=new_text if new_text else orig.text,
                speaker=new_spk,
            )
        )
    return updated


def refine_cues_with_llm(
    cues: Sequence[Cue],
    config: LlmConfig,
    *,
    task: str = "refine_asr",
    target_language: str = "vi",
    rate_cap: float = 1.35,
    batch_size: int = 25,
    progress_cb: Callable[[float], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> list[Cue]:
    """Refine ``cues`` in batches through an OpenAI-compatible LLM endpoint."""
    cue_list = list(cues)
    if not cue_list:
        return []
    step = max(1, int(batch_size))
    sys_prompt = _system_prompt_for_task(task, target_language)
    out: list[Cue] = []

    for start in range(0, len(cue_list), step):
        if cancel_check is not None and cancel_check():
            raise LlmRefineError("Đã hủy tác vụ tinh chỉnh AI.")
        batch = cue_list[start : start + step]
        req_items: list[dict[str, Any]] = []
        for offset, c in enumerate(batch):
            idx = start + offset
            if idx + 1 < len(cue_list):
                gap_after_ms = max(0, cue_list[idx + 1].effective_speech_start_ms - c.end_ms)
            else:
                gap_after_ms = 800
            borrowable_slack_ms = max(0, min(gap_after_ms - 120, 800))
            req_items.append(
                {
                    "index": c.index,
                    "startMs": c.start_ms,
                    "endMs": c.end_ms,
                    "durationMs": c.duration_ms,
                    "speechDurationMs": c.speech_duration_ms,
                    "gapAfterMs": gap_after_ms,
                    "maxChars": max_chars_for_window(
                        c.speech_duration_ms,
                        max_rate=min(float(rate_cap), 1.20),
                        language=target_language,
                        slack_ms=borrowable_slack_ms,
                    ),
                    "speaker": c.speaker,
                    "text": c.text,
                }
            )
        user_prompt = json.dumps(req_items, ensure_ascii=False)
        reply = _post_chat_completion(
            config,
            [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        out.extend(_parse_refined_batch(reply.content, batch))
        if progress_cb is not None:
            progress_cb(min(1.0, len(out) / len(cue_list)))

    return out
