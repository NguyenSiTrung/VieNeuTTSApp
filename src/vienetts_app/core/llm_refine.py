"""OpenAI-compatible LLM provider for subtitle ASR cleanup, timing condensation, and translation.

Zero external dependencies: talks to ``{base_url}/chat/completions`` via
``urllib.request``, working seamlessly with local Ollama, LM Studio, llama.cpp,
vLLM, or cloud OpenAI / Gemini / DeepSeek / OpenRouter endpoints.
"""

from __future__ import annotations

import ipaddress
import json
import math
import re
import threading
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, ThreadPoolExecutor, wait
from dataclasses import replace
from typing import Any
from urllib.parse import urlparse

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
    positions: Sequence[int] | None = None,
    context_end: int | None = None,
) -> dict[str, Any]:
    items: list[dict[str, Any]] = []
    for offset, cue in enumerate(batch):
        index = positions[offset] if positions is not None else start + offset
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
    end = start + len(batch) if context_end is None else context_end
    after = all_cues[end : end + 2]
    if before:
        envelope["contextBefore"] = [{"speaker": c.speaker, "text": c.text} for c in before]
    if after:
        envelope["contextAfter"] = [{"speaker": c.speaker, "text": c.text} for c in after]
    return envelope


def _auto_concurrency(base_url: str) -> int:
    host = (urlparse(base_url).hostname or "").lower().rstrip(".")
    if host == "localhost" or host.endswith(".local"):
        return 1
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return 3
    return 1 if address.is_private or address.is_loopback or address.is_link_local else 3


def refine_cues_with_llm(
    cues: Sequence[Cue],
    config: LlmConfig,
    *,
    task: str = "refine_asr",
    target_language: str = "vi",
    rate_cap: float = 1.35,
    batch_size: int | None = None,
    glossary: str = "",
    progress_cb: Callable[[float], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
    notes: list[str] | None = None,
) -> list[Cue]:
    """Refine ``cues`` in batches through an OpenAI-compatible LLM endpoint."""
    cue_list = list(cues)
    if not cue_list:
        return []
    step = max(1, int(config.batch_size if batch_size is None else batch_size))
    sys_prompt = _system_prompt_for_task(task, target_language)
    allowed_speakers = {c.speaker for c in cue_list if c.speaker}
    if len(glossary) > 2000 and notes is not None:
        notes.append("Thuật ngữ đã được rút gọn còn 2000 ký tự.")
    batches = [(start, cue_list[start : start + step]) for start in range(0, len(cue_list), step)]
    stopped = threading.Event()

    def cancelled() -> bool:
        return stopped.is_set() or (cancel_check is not None and cancel_check())

    def check_cancel() -> None:
        if cancelled():
            raise LlmRefineError("Đã hủy tác vụ tinh chỉnh AI.", kind="cancelled")

    def request(
        entries: list[tuple[int, Cue]],
        start: int,
        end: int,
        current: dict[int, int] | None = None,
    ) -> ChatResult:
        check_cancel()
        envelope = _build_request(
            [cue for _, cue in entries],
            cue_list,
            start,
            task=task,
            target_language=target_language,
            rate_cap=rate_cap,
            glossary=glossary,
            allowed_speakers=allowed_speakers,
            positions=[position for position, _ in entries],
            context_end=end,
        )
        if current is not None:
            for item in envelope["cues"]:
                item["currentChars"] = current[item["index"]]
        return _post_chat_completion(
            config,
            [
                {"role": "system", "content": sys_prompt},
                {"role": "user", "content": json.dumps(envelope, ensure_ascii=False)},
            ],
            json_mode=True,
            cancel_check=cancelled,
        )

    def parse_request(
        entries: list[tuple[int, Cue]], start: int, end: int, retried: bool = False
    ) -> tuple[list[Cue], list[int]]:
        try:
            reply = request(entries, start, end)
            if reply.finish_reason == "length":
                raise LlmRefineError("Phản hồi LLM bị cắt ngắn.", kind="format")
            return _parse_reply(
                reply.content,
                [cue for _, cue in entries],
                task=task,
                allowed_speakers=allowed_speakers,
            )
        except LlmRefineError as exc:
            if exc.kind != "format":
                raise
            if len(entries) > 5:
                middle = len(entries) // 2
                left, left_missing = parse_request(entries[:middle], start, end)
                right, right_missing = parse_request(entries[middle:], start, end)
                return left + right, left_missing + right_missing
            if not retried:
                return parse_request(entries, start, end, retried=True)
            raise

    def run_batch(start: int, batch: list[Cue]) -> tuple[list[Cue], int, int]:
        end = start + len(batch)
        entries = [(start + offset, cue) for offset, cue in enumerate(batch)]
        budgets: dict[int, int] = {}
        if task == "fit_timing":
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
            budgets = {item["index"]: item["maxChars"] for item in envelope["cues"]}
            entries = [
                (position, cue) for position, cue in entries if len(cue.text) > budgets[cue.index]
            ]
        if not entries:
            return batch, 0, 0
        updated, missing = parse_request(entries, start, end)
        by_index = {cue.index: cue for cue in updated}
        if missing:
            missing_set = set(missing)
            follow_up = [(position, cue) for position, cue in entries if cue.index in missing_set]
            try:
                reply = request(follow_up, start, end)
                if reply.finish_reason == "length":
                    raise LlmRefineError("Phản hồi LLM bị cắt ngắn.", kind="format")
                extra, missing = _parse_reply(
                    reply.content,
                    [cue for _, cue in follow_up],
                    task=task,
                    allowed_speakers=allowed_speakers,
                )
                by_index.update({cue.index: cue for cue in extra})
            except LlmRefineError as exc:
                if exc.kind in ("auth", "not_found", "bad_request", "cancelled"):
                    raise
        if task == "fit_timing":
            unresolved = set(missing)
            correction = [
                (position, by_index[cue.index])
                for position, cue in entries
                if cue.index not in unresolved
                and len(by_index[cue.index].text) > math.ceil(budgets[cue.index] * 1.1)
            ]
            if correction:
                try:
                    current = {cue.index: len(cue.text) for _, cue in correction}
                    reply = request(correction, start, end, current)
                    if reply.finish_reason == "length":
                        raise LlmRefineError("Phản hồi LLM bị cắt ngắn.", kind="format")
                    corrected, _ = _parse_reply(
                        reply.content,
                        [cue for _, cue in correction],
                        task=task,
                        allowed_speakers=allowed_speakers,
                    )
                    for candidate in corrected:
                        previous = by_index[candidate.index]
                        if candidate.text and len(candidate.text) < len(previous.text):
                            by_index[candidate.index] = candidate
                except LlmRefineError as exc:
                    if exc.kind in ("auth", "not_found", "bad_request", "cancelled"):
                        raise
            for _, original in entries:
                if len(by_index[original.index].text) > len(original.text):
                    by_index[original.index] = original
        result = [by_index.get(cue.index, cue) for cue in batch]
        over = sum(len(cue.text) > budgets[cue.index] for cue in result) if budgets else 0
        return result, len(missing), over

    workers = min(len(batches), 8, max(1, config.concurrency or _auto_concurrency(config.base_url)))
    results: list[list[Cue] | None] = [None] * len(batches)
    errors: list[LlmRefineError] = []
    missing_total = over_total = completed = next_batch = 0
    executor = ThreadPoolExecutor(max_workers=workers)
    pending = {}
    try:
        while next_batch < len(batches) or pending:
            check_cancel()
            while next_batch < len(batches) and len(pending) < workers:
                check_cancel()
                start, batch = batches[next_batch]
                pending[executor.submit(run_batch, start, batch)] = next_batch
                next_batch += 1
            finished, _ = wait(pending, return_when=FIRST_COMPLETED)
            for future in finished:
                batch_index = pending.pop(future)
                try:
                    result, missing, over = future.result()
                except LlmRefineError as exc:
                    if exc.kind in ("auth", "not_found", "bad_request", "cancelled"):
                        raise
                    errors.append(exc)
                    result, missing, over = batches[batch_index][1], 0, 0
                results[batch_index] = result
                missing_total += missing
                over_total += over
                completed += 1
                if progress_cb is not None:
                    progress_cb(completed / len(batches))
        check_cancel()
    except BaseException:
        stopped.set()
        raise
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
    if len(errors) == len(batches):
        raise errors[-1]
    if notes is not None:
        if errors:
            notes.append(
                f"{len(errors)}/{len(batches)} lô không xử lý được — giữ nguyên văn bản gốc."
            )
        if missing_total:
            notes.append(f"{missing_total} phụ đề thiếu trong phản hồi — giữ nguyên văn bản gốc.")
        if over_total:
            notes.append(f"{over_total} phụ đề vẫn vượt giới hạn ký tự.")
    return [cue for result in results if result is not None for cue in result]
