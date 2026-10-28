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

_REASONING_RE = re.compile(r"<think\b[^>]*>.*?</think\s*>", re.DOTALL | re.IGNORECASE)


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

LANGUAGE_NAMES = {
    "vi": "Vietnamese",
    "en": "English",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "de": "German",
    "fr": "French",
    "ru": "Russian",
    "pt": "Portuguese",
    "es": "Spanish",
    "it": "Italian",
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
    contract = (
        "Context is read-only; never return or change context cues. "
        'Return only a JSON object {"cues": [{"index": ..., "text": ..., "speaker": ...}]}.'
    )
    if task == "fit_timing":
        return (
            "You are a professional video dubbing script editor. "
            "For each subtitle cue, if its text is longer than `maxChars`, "
            "condense/rephrase it naturally in the same language so its character length is "
            "at most `maxChars` while preserving the core meaning and spoken tone. " + contract
        )
    if task == "translate_dub":
        clean_lang = (target_language or "").strip()
        lang = LANGUAGE_NAMES.get(clean_lang.lower(), clean_lang) if clean_lang else "Vietnamese"
        if clean_lang.lower() == "auto":
            lang = "Vietnamese"
        return (
            f"You are a professional video dubbing translator. "
            f"Translate each cue's `text` into natural spoken {lang} for voiceover dubbing. "
            f"Keep each translated cue concise (ideally <= `maxChars` characters) so it fits the "
            f"speaker's timing window without rushing. " + contract
        )
    # Default: refine_asr
    return (
        "You are an expert ASR & speaker diarization post-editor for video dubbing. "
        "Fix speech recognition typos, homophones, capitalization, and punctuation in each cue's "
        "`text`, and fix obvious `speaker` label inconsistencies across conversational turns. "
        "Use only labels in `allowedSpeakers`. "
        "Keep the same language and preserve every cue's `index`. " + contract
    )


def _strip_reasoning(raw: str) -> str:
    return _REASONING_RE.sub("", raw)


def _parse_reply(
    raw_reply: str,
    batch: Sequence[Cue],
    *,
    task: str,
    allowed_speakers: set[str],
) -> tuple[list[Cue], list[int]]:
    text = _strip_reasoning(raw_reply)
    decoder = json.JSONDecoder()
    items = None
    for pos, char in enumerate(text):
        if char not in "[{":
            continue
        try:
            value, _ = decoder.raw_decode(text, pos)
        except json.JSONDecodeError:
            continue
        candidate = value.get("cues") if isinstance(value, dict) else value
        if isinstance(candidate, list) and all(isinstance(item, dict) for item in candidate):
            items = candidate
            break
    if items is None:
        raise LlmRefineError("LLM không trả về danh sách phụ đề JSON hợp lệ.", kind="format")

    by_index: dict[int, dict[str, Any]] = {}
    for entry in items:
        if "index" in entry:
            try:
                index = entry["index"]
                if isinstance(index, bool) or not isinstance(index, int | str):
                    continue
                by_index[int(index)] = entry
            except (TypeError, ValueError):
                continue

    updated: list[Cue] = []
    missing: list[int] = []
    positional = len(items) == len(batch) and not any("index" in item for item in items)
    for pos, orig in enumerate(batch):
        item = items[pos] if positional else by_index.get(orig.index)
        if item is None:
            updated.append(orig)
            missing.append(orig.index)
            continue
        new_text = clean_cue_text(str(item.get("text") or ""))
        speaker = item.get("speaker")
        new_spk = speaker.strip() if isinstance(speaker, str) else ""
        if task != "refine_asr" or not new_spk or new_spk not in allowed_speakers:
            new_spk = orig.speaker
        updated.append(
            replace(
                orig,
                text=new_text if new_text else orig.text,
                speaker=new_spk,
            )
        )
    return updated, missing


def _build_request(
    batch: Sequence[Cue],
    all_cues: Sequence[Cue],
    start: int,
    *,
    task: str,
    target_language: str,
    rate_cap: float,
    glossary: str,
    allowed_speakers: set[str],
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for offset, cue in enumerate(batch):
        index = start + offset
        item: dict[str, Any] = {"index": cue.index, "speaker": cue.speaker, "text": cue.text}
        if task in ("translate_dub", "fit_timing"):
            gap = (
                max(0, all_cues[index + 1].effective_speech_start_ms - cue.end_ms)
                if index + 1 < len(all_cues)
                else 800
            )
            item["maxChars"] = max_chars_for_window(
                cue.speech_duration_ms,
                max_rate=min(float(rate_cap), 1.20),
                language=target_language,
                slack_ms=max(0, min(gap - 120, 800)),
            )
        items.append(item)
    envelope: dict[str, Any] = {"cues": items}
    if glossary:
        envelope["glossary"] = glossary[:2000]
    if task == "refine_asr" and allowed_speakers:
        envelope["allowedSpeakers"] = sorted(allowed_speakers)
    before = all_cues[max(0, start - 3) : start]
    after = all_cues[start + len(batch) : start + len(batch) + 2]
    if before:
        envelope["contextBefore"] = [{"speaker": c.speaker, "text": c.text} for c in before]
    if after:
        envelope["contextAfter"] = [{"speaker": c.speaker, "text": c.text} for c in after]
    return envelope


def refine_cues_with_llm(
    cues: Sequence[Cue],
    config: LlmConfig,
    *,
    task: str = "refine_asr",
    target_language: str = "vi",
    rate_cap: float = 1.35,
    batch_size: int = 25,
    glossary: str = "",
    progress_cb: Callable[[float], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    notes: list[str] | None = None,
) -> list[Cue]:
    """Refine ``cues`` in batches through an OpenAI-compatible LLM endpoint."""
    cue_list = list(cues)
    if not cue_list:
        return []
    step = max(1, int(batch_size))
    sys_prompt = _system_prompt_for_task(task, target_language)
    allowed_speakers = {c.speaker for c in cue_list if c.speaker}
    if len(glossary) > 2000 and notes is not None:
        notes.append("Thuật ngữ đã được rút gọn còn 2000 ký tự.")
    out: list[Cue] = []

    for start in range(0, len(cue_list), step):
        if cancel_check is not None and cancel_check():
            raise LlmRefineError("Đã hủy tác vụ tinh chỉnh AI.")
        batch = cue_list[start : start + step]
        envelope = _build_request(
            batch,
            cue_list,
            start,
            task=task,
            target_language=target_language,
            rate_cap=rate_cap,
            glossary=glossary,
            allowed_speakers=allowed_speakers,
        )
        user_prompt = json.dumps(envelope, ensure_ascii=False)
        reply = _post_chat_completion(
            config,
            [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
            json_mode=True,
            cancel_check=cancel_check,
        )
        updated, _ = _parse_reply(
            reply.content, batch, task=task, allowed_speakers=allowed_speakers
        )
        out.extend(updated)
        if progress_cb is not None:
            progress_cb(min(1.0, len(out) / len(cue_list)))

    return out
