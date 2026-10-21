"""OpenAI-compatible LLM provider for subtitle ASR cleanup, timing condensation, and translation.

Zero external dependencies: talks to ``{base_url}/chat/completions`` via
``urllib.request``, working seamlessly with local Ollama, LM Studio, llama.cpp,
vLLM, or cloud OpenAI / Gemini / DeepSeek / OpenRouter endpoints.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from vienetts_app.core.subtitles import Cue, clean_cue_text

_JSON_ARRAY_RE = re.compile(r"\[.*\]", re.DOTALL)


class LlmRefineError(RuntimeError):
    """An LLM refinement request failed; message is user-actionable."""


@dataclass(frozen=True)
class LlmConfig:
    base_url: str = "http://localhost:11434/v1"
    api_key: str = ""
    model: str = "qwen2.5:7b"
    timeout_s: float = 60.0


def max_chars_for_window(
    duration_ms: int,
    *,
    chars_per_sec: float = 14.0,
    max_rate: float = 1.25,
) -> int:
    """Maximum comfortable spoken character count for a ``duration_ms`` subtitle window."""
    secs = max(0.1, float(duration_ms) / 1000.0)
    budget = int(round(secs * float(chars_per_sec) * min(float(max_rate), 1.35)))
    return max(8, budget)


def _post_chat_completion(
    config: LlmConfig,
    messages: list[dict[str, str]],
    *,
    temperature: float = 0.2,
) -> str:
    base_url = (config.base_url or "").strip().rstrip("/")
    if not base_url:
        raise LlmRefineError("Chưa cấu hình địa chỉ máy chủ LLM (Base URL) trong Cài đặt.")
    model = (config.model or "").strip() or "qwen2.5:7b"
    url = f"{base_url}/chat/completions"

    payload = {
        "model": model,
        "messages": messages,
        "temperature": float(temperature),
    }
    data = json.dumps(payload, ensure_ascii=False).encode()
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    if config.api_key.strip():
        headers["Authorization"] = f"Bearer {config.api_key.strip()}"

    req = Request(url, data=data, headers=headers, method="POST")
    try:
        with urlopen(req, timeout=max(1.0, float(config.timeout_s))) as resp:  # noqa: S310
            raw = resp.read().decode("utf-8", errors="replace")
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        raise LlmRefineError(f"Không thể kết nối tới LLM ({url}): {exc}") from exc

    try:
        parsed = json.loads(raw)
        choices = parsed.get("choices") or []
        content = choices[0]["message"]["content"]
        return str(content or "").strip()
    except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
        raise LlmRefineError(f"Phản hồi từ LLM không đúng định dạng OpenAI: {exc}") from exc


def probe_llm_connection(config: LlmConfig) -> tuple[bool, str]:
    """Send a lightweight ping completion to verify ``config``."""
    try:
        reply = _post_chat_completion(
            config,
            [
                {"role": "system", "content": "Reply with OK."},
                {"role": "user", "content": "TEST_PING"},
            ],
            temperature=0.0,
        )
        return True, f"Kết nối thành công ({config.model}): {reply[:40]}"
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
        req_items = [
            {
                "index": c.index,
                "startMs": c.start_ms,
                "endMs": c.end_ms,
                "durationMs": c.duration_ms,
                "maxChars": max_chars_for_window(c.duration_ms, max_rate=rate_cap),
                "speaker": c.speaker,
                "text": c.text,
            }
            for c in batch
        ]
        user_prompt = json.dumps(req_items, ensure_ascii=False)
        reply = _post_chat_completion(
            config,
            [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": user_prompt},
            ],
        )
        out.extend(_parse_refined_batch(reply, batch))
        if progress_cb is not None:
            progress_cb(min(1.0, len(out) / len(cue_list)))

    return out
