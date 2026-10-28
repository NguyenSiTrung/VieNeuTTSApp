"""OpenAI-compatible chat transport and provider error handling."""

from __future__ import annotations

import json
import math
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CANCEL_POLL_S = 0.25


class RequestCancelled(Exception):
    """``cancel_check`` turned true while a request was in flight; its reply is discarded."""


def cancellable_request(
    request: Request,
    *,
    timeout: float,
    cancel_check: Callable[[], bool] | None = None,
    poll_s: float = CANCEL_POLL_S,
) -> bytes:
    """``urlopen(request).read()`` that a cancel abandons within ``poll_s``.

    With a ``cancel_check`` the blocking request runs on a daemon thread that
    is polled every ``poll_s``; when the check turns true this raises
    :class:`RequestCancelled` at once and the thread's eventual reply (or
    error) is dropped — the socket ``timeout`` still bounds that thread.
    ``HTTPError``/``URLError``/``OSError`` propagate unchanged either way.
    """
    if cancel_check is None:
        with urlopen(request, timeout=timeout) as resp:  # noqa: S310
            return resp.read()
    outcome: list[bytes | BaseException] = []
    done = threading.Event()

    def fetch() -> None:
        try:
            with urlopen(request, timeout=timeout) as resp:  # noqa: S310
                outcome.append(resp.read())
        except BaseException as exc:  # noqa: BLE001 — re-raised on the caller's thread
            outcome.append(exc)
        finally:
            done.set()

    threading.Thread(target=fetch, name="cancellable-request", daemon=True).start()
    while not done.wait(poll_s):
        if cancel_check():
            raise RequestCancelled
    result = outcome[0]
    if isinstance(result, BaseException):
        raise result
    return result


class LlmRefineError(RuntimeError):
    """An LLM refinement request failed; message is user-actionable."""

    def __init__(
        self,
        message: str,
        *,
        status: int | None = None,
        kind: str = "error",
        transient: bool = False,
    ) -> None:
        super().__init__(message)
        self.status = status
        self.kind = kind
        self.transient = transient


@dataclass(frozen=True)
class LlmConfig:
    base_url: str = "http://localhost:11434/v1"
    api_key: str = ""
    model: str = "qwen2.5:7b"
    timeout_s: float = 120.0
    max_retries: int = 2
    concurrency: int = 0
    batch_size: int = 25


@dataclass(frozen=True)
class ChatResult:
    content: str
    finish_reason: str = ""


_capability_lock = threading.Lock()
_unsupported: dict[tuple[str, str], set[str]] = {}
_sleep = time.sleep


def reset_capability_cache() -> None:
    with _capability_lock:
        _unsupported.clear()


def http_error_detail(exc: HTTPError) -> str:
    raw = exc.read(4096).decode("utf-8", errors="replace").strip()
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return raw[:300]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            error = error.get("message")
        detail = error or data.get("message") or data.get("detail")
        if isinstance(detail, str):
            return detail.strip()[:300]
    return raw[:300]


def _check_cancel(cancel_check: Callable[[], bool] | None) -> None:
    if cancel_check is not None and cancel_check():
        raise LlmRefineError("Đã hủy tác vụ tinh chỉnh AI.", kind="cancelled")


def _wait(delay: float, cancel_check: Callable[[], bool] | None) -> None:
    while delay > 0:
        _check_cancel(cancel_check)
        interval = min(delay, 0.25)
        _sleep(interval)
        delay -= interval
    _check_cancel(cancel_check)


def _status_kind(status: int) -> tuple[str, bool]:
    if status in (401, 403):
        return "auth", False
    if status == 404:
        return "not_found", False
    if status in (400, 422):
        return "bad_request", False
    if status == 429:
        return "rate_limited", True
    if status >= 500 or status in (408, 409):
        return "server", True
    return "error", False


def post_chat_completion(
    config: LlmConfig,
    messages: list[dict[str, str]],
    *,
    temperature: float | None = 0.2,
    json_mode: bool = False,
    cancel_check: Callable[[], bool] | None = None,
) -> ChatResult:
    base_url = (config.base_url or "").strip().rstrip("/")
    if not base_url:
        raise LlmRefineError("Chưa cấu hình địa chỉ máy chủ LLM (Base URL) trong Cài đặt.")
    model = (config.model or "").strip() or "qwen2.5:7b"
    url = f"{base_url}/chat/completions"
    key = config.api_key.strip()
    cache_key = (base_url, model)
    retries = 0

    def safe(value: str) -> str:
        return value.replace(key, "[redacted]") if key else value

    while True:
        _check_cancel(cancel_check)
        with _capability_lock:
            unsupported = set(_unsupported.get(cache_key, ()))
        payload: dict = {"model": model, "messages": messages}
        if temperature is not None and "temperature" not in unsupported:
            payload["temperature"] = float(temperature)
        if json_mode and "response_format" not in unsupported:
            payload["response_format"] = {"type": "json_object"}
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if key:
            headers["Authorization"] = f"Bearer {key}"
        data = json.dumps(payload, ensure_ascii=False).encode()
        req = Request(url, data=data, headers=headers, method="POST")
        try:
            raw = cancellable_request(
                req, timeout=max(1.0, float(config.timeout_s)), cancel_check=cancel_check
            ).decode("utf-8", errors="replace")
        except RequestCancelled:
            raise LlmRefineError("Đã hủy tác vụ tinh chỉnh AI.", kind="cancelled") from None
        except HTTPError as exc:
            detail = http_error_detail(exc)
            lowered = detail.lower()
            downgrade = next(
                (
                    name
                    for name in ("temperature", "response_format")
                    if exc.code in (400, 422) and name in payload and name in lowered
                ),
                None,
            )
            if downgrade:
                with _capability_lock:
                    _unsupported.setdefault(cache_key, set()).add(downgrade)
                continue
            kind, transient = _status_kind(exc.code)
            error = LlmRefineError(
                safe(f"LLM HTTP {exc.code} ({url}): {detail}"),
                status=exc.code,
                kind=kind,
                transient=transient,
            )
            retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
        except (URLError, TimeoutError, OSError) as exc:
            error = LlmRefineError(
                safe(f"Không thể kết nối tới LLM ({url}): {exc}"),
                kind="network",
                transient=True,
            )
            retry_after = ""
        else:
            try:
                parsed = json.loads(raw)
                choice = parsed["choices"][0]
                content = choice["message"]["content"]
                if not isinstance(content, str):
                    raise TypeError("content must be a string")
                return ChatResult(content.strip(), str(choice.get("finish_reason") or ""))
            except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
                raise LlmRefineError(
                    "Phản hồi từ LLM không đúng định dạng OpenAI.", kind="format"
                ) from exc
        if not error.transient or retries >= max(0, int(config.max_retries)):
            raise error from None
        delay = float(2**retries)
        try:
            advised = float(retry_after)
            if math.isfinite(advised) and advised >= 0:
                delay = min(advised, 30.0)
        except ValueError:
            pass
        retries += 1
        _wait(delay, cancel_check)
