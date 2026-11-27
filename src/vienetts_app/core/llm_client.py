"""OpenAI-compatible chat transport and provider error handling."""

from __future__ import annotations

import contextlib
import http.client
import json
import math
import re
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener

CANCEL_POLL_S = 0.25
MAX_RESPONSE_BYTES = 8 * 1024 * 1024  # 8 MiB ceiling for HTTP JSON replies
MAX_ERROR_DETAIL_BYTES = 65_536  # 64 KiB ceiling for HTTP error bodies
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)\S+")


class RequestCancelled(Exception):
    """``cancel_check`` turned true while a request was in flight; its reply is discarded."""


class UnsafeRedirectError(URLError):
    """Server attempted a redirect to a disallowed scheme or malformed URL."""


class ResponseTooLargeError(OSError):
    """HTTP response body exceeded the configured ``max_bytes`` ceiling."""


def validate_http_url(url: str) -> str:
    """Validate an ``http://`` or ``https://`` URL and return it trimmed.

    Permits both ``http://`` (for local Ollama / LM Studio / vLLM / whisper.cpp
    servers) and ``https://``, while rejecting non-HTTP schemes (``file://``,
    ``ftp://``, scheme-less URLs), empty/missing hosts, invalid ports, and
    control or whitespace characters.
    """
    if not isinstance(url, str):
        raise ValueError("URL must be a string")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in url):
        raise ValueError("URL contains control characters")
    stripped = url.strip()
    if not stripped or any(ch.isspace() for ch in stripped):
        raise ValueError("URL is empty or contains whitespace")
    parsed = urlsplit(stripped)
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https"):
        raise ValueError(f"unsupported URL scheme: {parsed.scheme or '<missing>'}")
    if not parsed.netloc or not parsed.hostname:
        raise ValueError("URL must include a valid host")
    # Accessing .port validates numeric formatting and 0..65535 bounds.
    _ = parsed.port
    return stripped


def _url_origin(url: str) -> tuple[str, str, int]:
    parsed = urlsplit(url)
    scheme = parsed.scheme.lower()
    host = (parsed.hostname or "").lower()
    port = parsed.port
    if port is None:
        port = 443 if scheme == "https" else 80
    return (scheme, host, port)


def _close_fp(fp: Any) -> None:
    if fp is not None:
        with contextlib.suppress(Exception):
            fp.close()


def _strip_auth_headers(request: Request) -> None:
    for mapping in (request.headers, request.unredirected_hdrs):
        for header_name in [k for k in mapping if k.lower() == "authorization"]:
            del mapping[header_name]


class SafeRedirectHandler(HTTPRedirectHandler):
    """Redirect handler blocking non-HTTP(S) schemes and stripping cross-origin auth.

    Rejects redirects to non-``http``/``https`` schemes (e.g. ``file://``,
    ``ftp://``) with :class:`UnsafeRedirectError`, and strips ``Authorization``
    headers (case-insensitively across ``headers`` and ``unredirected_hdrs``)
    whenever a redirect changes origin ``(scheme, hostname.lower(), port)`` or
    downgrades ``https -> http``.
    """

    def http_error_302(self, req, fp, code, msg, headers):
        location = headers.get("location") or headers.get("uri")
        if location is not None:
            if any(ord(ch) < 32 or ord(ch) == 127 for ch in location):
                _close_fp(fp)
                raise UnsafeRedirectError("refused redirect URL containing control characters")
            scheme = urlsplit(location.strip()).scheme.lower()
            if scheme and scheme not in ("http", "https"):
                _close_fp(fp)
                raise UnsafeRedirectError(f"refused redirect to unsupported scheme: {scheme}")
        return super().http_error_302(req, fp, code, msg, headers)

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        try:
            validated = validate_http_url(newurl)
        except ValueError as exc:
            _close_fp(fp)
            raise UnsafeRedirectError(f"refused redirect to invalid URL: {exc}") from exc
        new = super().redirect_request(req, fp, code, msg, headers, validated)
        if new is None:
            return None
        try:
            old_origin = _url_origin(req.full_url)
            new_origin = _url_origin(new.full_url)
            strip_auth = new_origin != old_origin or (
                old_origin[0] == "https" and new_origin[0] == "http"
            )
        except ValueError:
            strip_auth = True
        if strip_auth:
            _strip_auth_headers(new)
        return new


def build_safe_opener() -> OpenerDirector:
    """Return a ``urllib`` opener configured with :class:`SafeRedirectHandler`."""
    return build_opener(SafeRedirectHandler)


def urlopen(request: Request, timeout: float | None = None):
    """Open ``request`` through :func:`build_safe_opener`."""
    opener = build_safe_opener()
    if timeout is None:
        return opener.open(request)
    return opener.open(request, timeout=timeout)


def cancellable_request(
    request: Request,
    *,
    timeout: float,
    cancel_check: Callable[[], bool] | None = None,
    poll_s: float = CANCEL_POLL_S,
    max_bytes: int | None = None,
) -> bytes:
    """Bounded ``urlopen(request).read()`` that a cancel abandons within ``poll_s``.

    Validates ``request.full_url`` as ``http://``/``https://``, follows only safe
    redirects via :func:`build_safe_opener`, and caps the response body at
    ``max_bytes`` (defaults to :data:`MAX_RESPONSE_BYTES`), raising
    :class:`ResponseTooLargeError` if exceeded.

    With a ``cancel_check`` the blocking request runs on a daemon thread that
    is polled every ``poll_s``; when the check turns true this raises
    :class:`RequestCancelled` at once and the thread's eventual reply (or
    error) is dropped — the socket ``timeout`` still bounds that thread.
    ``HTTPError``/``URLError``/``OSError``/``http.client.HTTPException``
    propagate unchanged either way.
    """
    validate_http_url(request.full_url)
    limit = MAX_RESPONSE_BYTES if max_bytes is None else int(max_bytes)
    if limit <= 0:
        raise ValueError("max_bytes must be positive")

    def _open_and_read() -> bytes:
        with urlopen(request, timeout=timeout) as resp:
            data = resp.read(limit + 1)
        if len(data) > limit:
            raise ResponseTooLargeError(f"HTTP response exceeded {limit} bytes")
        return data

    if cancel_check is None:
        return _open_and_read()
    outcome: list[bytes | BaseException] = []
    done = threading.Event()

    def fetch() -> None:
        try:
            outcome.append(_open_and_read())
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


def _redact_text(text: str, secret: str = "") -> str:
    redacted = _BEARER_RE.sub(r"\1[redacted]", text)
    if secret:
        redacted = redacted.replace(secret, "[redacted]")
    return redacted


def http_error_detail(exc: HTTPError, *, secret: str = "") -> str:
    """Safely extract, bound, and redact an error message from an ``HTTPError``."""
    raw_bytes: bytes | bytearray | str | None = b""
    try:
        raw_bytes = exc.read(MAX_ERROR_DETAIL_BYTES)
    except Exception:  # noqa: BLE001 — fp=None, closed stream, or read error
        raw_bytes = b""
    if isinstance(raw_bytes, bytes | bytearray):
        raw = bytes(raw_bytes).decode("utf-8", errors="replace").strip()
    elif isinstance(raw_bytes, str):
        raw = raw_bytes.strip()
    else:
        raw = ""
    if not raw:
        reason = getattr(exc, "reason", "")
        raw = str(reason).strip() if reason is not None else ""
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return _redact_text(raw, secret)[:300]
    if isinstance(data, dict):
        error = data.get("error")
        if isinstance(error, dict):
            error = error.get("message")
        detail = error or data.get("message") or data.get("detail")
        if isinstance(detail, str):
            return _redact_text(detail.strip(), secret)[:300]
    return _redact_text(raw, secret)[:300]


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
    raw_base_url = config.base_url or ""
    if not raw_base_url.strip():
        raise LlmRefineError("Chưa cấu hình địa chỉ máy chủ LLM (Base URL) trong Cài đặt.")
    model = (config.model or "").strip() or "qwen2.5:7b"
    key = config.api_key.strip()

    def safe(value: str) -> str:
        return _redact_text(value, key)

    try:
        base_url = validate_http_url(raw_base_url).rstrip("/")
        url = validate_http_url(f"{base_url}/chat/completions")
    except ValueError as exc:
        raise LlmRefineError(
            safe(f"Địa chỉ máy chủ LLM không hợp lệ: {exc}"),
            kind="bad_request",
            transient=False,
        ) from exc

    cache_key = (base_url, model)
    retries = 0

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
        try:
            req = Request(url, data=data, headers=headers, method="POST")
            raw_bytes = cancellable_request(
                req, timeout=max(1.0, float(config.timeout_s)), cancel_check=cancel_check
            )
        except RequestCancelled:
            raise LlmRefineError("Đã hủy tác vụ tinh chỉnh AI.", kind="cancelled") from None
        except UnicodeDecodeError as exc:
            raise LlmRefineError(
                "Phản hồi từ LLM không đúng định dạng OpenAI.", kind="format"
            ) from exc
        except (ValueError, UnsafeRedirectError) as exc:
            raise LlmRefineError(
                safe(f"Địa chỉ máy chủ LLM không hợp lệ ({url}): {exc}"),
                kind="bad_request",
                transient=False,
            ) from None
        except ResponseTooLargeError as exc:
            raise LlmRefineError(
                safe(f"Phản hồi từ LLM vượt quá giới hạn kích thước ({url}): {exc}"),
                kind="error",
                transient=False,
            ) from None
        except HTTPError as exc:
            try:
                detail = http_error_detail(exc, secret=key)
                retry_after = exc.headers.get("Retry-After", "") if exc.headers else ""
            finally:
                with contextlib.suppress(Exception):
                    exc.close()
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
        except (URLError, http.client.HTTPException, TimeoutError, OSError) as exc:
            error = LlmRefineError(
                safe(f"Không thể kết nối tới LLM ({url}): {exc}"),
                kind="network",
                transient=True,
            )
            retry_after = ""
        else:
            try:
                raw = raw_bytes.decode("utf-8")
                parsed = json.loads(raw)
                choice = parsed["choices"][0]
                content = choice["message"]["content"]
                if not isinstance(content, str):
                    raise TypeError("content must be a string")
                return ChatResult(content.strip(), str(choice.get("finish_reason") or ""))
            except (
                KeyError,
                IndexError,
                TypeError,
                UnicodeDecodeError,
                json.JSONDecodeError,
            ) as exc:
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
