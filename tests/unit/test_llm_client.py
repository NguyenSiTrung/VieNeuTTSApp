"""Tests for the OpenAI-compatible transport."""

from __future__ import annotations

import http.client
import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.error import HTTPError, URLError
from urllib.request import Request

import pytest

from vienetts_app.core import llm_client
from vienetts_app.core.llm_client import LlmConfig, LlmRefineError, post_chat_completion


@pytest.fixture
def provider():
    responses: list[tuple[int, object, dict[str, str]]] = []
    requests: list[dict] = []
    release = threading.Event()  # set in teardown, so a stalled reply never outlives the test

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            status, body, headers = responses.pop(0)
            if isinstance(body, threading.Event):  # stall: signal arrival, hold the reply 5 s
                body.set()
                release.wait(5)
                body = {}
            data = (
                body
                if isinstance(body, bytes)
                else (body.encode() if isinstance(body, str) else json.dumps(body).encode())
            )
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    llm_client.reset_capability_cache()
    try:
        yield (
            LlmConfig(base_url=f"http://127.0.0.1:{server.server_port}/v1", api_key="secret"),
            (
                responses,
                requests,
            ),
        )
    finally:
        release.set()
        server.shutdown()
        server.server_close()


def test_http_error_taxonomy_and_secrets(provider) -> None:
    config, (responses, _) = provider
    for status, kind, transient in (
        (401, "auth", False),
        (403, "auth", False),
        (404, "not_found", False),
        (400, "bad_request", False),
        (422, "bad_request", False),
        (429, "rate_limited", True),
        (500, "server", True),
        (503, "server", True),
        (408, "server", True),
        (409, "server", True),
    ):
        responses.append((status, {"error": {"message": "provider says secret is invalid"}}, {}))
        with pytest.raises(LlmRefineError) as caught:
            post_chat_completion(
                LlmConfig(base_url=config.base_url, api_key="secret", max_retries=0), []
            )
        assert (caught.value.status, caught.value.kind, caught.value.transient) == (
            status,
            kind,
            transient,
        )
        assert "provider says" in str(caught.value)
        assert "secret" not in str(caught.value)

    responses.append((400, "plain provider error " + "x" * 500, {}))
    with pytest.raises(LlmRefineError, match="plain provider error") as caught:
        post_chat_completion(config, [])
    assert len(str(caught.value)) < 400


def test_retries_retry_after_and_cancellation(provider, monkeypatch) -> None:
    config, (responses, _) = provider
    sleeps: list[float] = []
    monkeypatch.setattr(llm_client, "_sleep", sleeps.append)
    success = {"choices": [{"message": {"content": "OK"}, "finish_reason": "stop"}]}
    responses.extend([(503, "unavailable", {}), (503, "unavailable", {}), (200, success, {})])
    result = post_chat_completion(config, [])
    assert (result.content, result.finish_reason) == ("OK", "stop")
    assert sum(sleeps) == 3
    assert max(sleeps) <= 0.25
    sleeps.clear()
    responses.extend([(429, "slow down", {"Retry-After": "2"}), (200, success, {})])
    post_chat_completion(config, [])
    assert sum(sleeps) == 2
    sleeps.clear()
    responses.extend([(429, "slow down", {"Retry-After": "120"}), (200, success, {})])
    post_chat_completion(config, [])
    assert sum(sleeps) == 30
    sleeps.clear()
    responses.append((503, "unavailable", {}))
    with pytest.raises(LlmRefineError) as caught:
        post_chat_completion(config, [], cancel_check=lambda: bool(sleeps))
    assert caught.value.kind == "cancelled"
    # With a cancel_check the request runs on a worker thread; a normal reply is returned.
    responses.append((200, success, {}))
    assert post_chat_completion(config, [], cancel_check=lambda: False).content == "OK"
    # A stalled provider is abandoned within one poll of the cancel, not at its 5 s reply.
    arrived = threading.Event()
    responses.append((200, arrived, {}))
    started = time.monotonic()
    with pytest.raises(LlmRefineError) as caught:
        post_chat_completion(config, [], cancel_check=arrived.is_set)
    assert caught.value.kind == "cancelled"
    assert time.monotonic() - started < 1.0


def test_capability_fallback_cache_and_envelope(provider) -> None:
    config, (responses, requests) = provider
    success = {"choices": [{"message": {"content": "OK"}}]}
    responses.extend(
        [
            (400, {"error": {"message": "temperature unsupported"}}, {}),
            (422, {"detail": "response_format unsupported"}, {}),
            (200, success, {}),
            (200, success, {}),
            (200, success, {}),
            (200, {"unexpected": []}, {}),
        ]
    )
    result = post_chat_completion(config, [], json_mode=True)
    assert result.finish_reason == ""
    assert "temperature" in requests[0] and "response_format" in requests[0]
    assert "temperature" not in requests[1] and "response_format" in requests[1]
    assert "temperature" not in requests[2] and "response_format" not in requests[2]
    post_chat_completion(config, [], json_mode=True)
    assert "temperature" not in requests[3] and "response_format" not in requests[3]
    post_chat_completion(LlmConfig(base_url=config.base_url, model="different"), [], json_mode=True)
    assert "temperature" in requests[4] and "response_format" in requests[4]
    llm_client.reset_capability_cache()
    with pytest.raises(LlmRefineError) as caught:
        post_chat_completion(config, [], json_mode=True)
    assert caught.value.kind == "format"
    assert "temperature" in requests[5] and "response_format" in requests[5]


def test_network_errors(monkeypatch) -> None:
    def timeout(*args, **kwargs):
        raise TimeoutError("timed out")

    monkeypatch.setattr(llm_client, "urlopen", timeout)
    with pytest.raises(LlmRefineError) as caught:
        post_chat_completion(LlmConfig(max_retries=0), [])
    assert caught.value.kind == "network" and caught.value.transient
    monkeypatch.undo()
    with pytest.raises(LlmRefineError) as caught:
        post_chat_completion(LlmConfig(base_url="http://127.0.0.1:1", max_retries=0), [])
    assert caught.value.kind == "network" and caught.value.transient


@pytest.mark.parametrize(
    "bad_url",
    [
        "file:///etc/passwd",
        "ftp://example.com/v1",
        "localhost:11434/v1",
        "not-a-url",
        "http:///v1",
        "http://:8080/v1",
        "http://localhost:11434/v1\r\nX-Inject: 1",
        "http://local host:11434/v1",
        "http://localhost:99999/v1",
        "ftp://secret-token@example.com/v1",
    ],
)
def test_url_scheme_validation_rejects_unsafe_and_malformed_urls(bad_url: str) -> None:
    with pytest.raises(LlmRefineError) as caught:
        post_chat_completion(
            LlmConfig(base_url=bad_url, api_key="secret-token", max_retries=2),
            [],
        )
    assert caught.value.kind == "bad_request"
    assert caught.value.transient is False
    assert "secret-token" not in str(caught.value)


def test_validate_http_url_and_cancellable_request_scheme_guard() -> None:
    assert (
        llm_client.validate_http_url("  http://localhost:11434/v1  ") == "http://localhost:11434/v1"
    )
    assert llm_client.validate_http_url("https://api.openai.com/v1") == "https://api.openai.com/v1"
    assert llm_client.validate_http_url("http://[::1]:8000/v1") == "http://[::1]:8000/v1"
    with pytest.raises(ValueError):
        llm_client.cancellable_request(Request("file:///etc/passwd"), timeout=1.0)


def test_safe_redirect_handler_strips_auth_and_blocks_unsafe_schemes() -> None:
    handler = llm_client.SafeRedirectHandler()

    # Same-origin (case-insensitive host, explicit vs implicit default port) keeps Authorization.
    same_req = Request(
        "https://api.example.com:443/v1/chat",
        headers={"Authorization": "Bearer secret"},
        method="GET",
    )
    same_new = handler.redirect_request(
        same_req, None, 302, "Found", {}, "https://API.EXAMPLE.COM/v2/chat"
    )
    assert same_new is not None
    assert same_new.get_header("Authorization") == "Bearer secret"

    # Cross-host, cross-port, and https->http downgrade all strip Authorization case-insensitively.
    for start_url, target_url in (
        ("https://api.example.com/v1/chat", "https://other.example.com/v1/chat"),
        ("http://127.0.0.1:8000/v1/chat", "http://127.0.0.1:8001/v1/chat"),
        ("https://api.example.com/v1/chat", "http://api.example.com/v1/chat"),
        ("https://api.example.com:8443/v1/chat", "http://api.example.com:8443/v1/chat"),
    ):
        req = Request(start_url, headers={"Authorization": "Bearer secret"}, method="GET")
        req.headers["authorization"] = "Bearer secret-lower"
        req.unredirected_hdrs["AUTHORIZATION"] = "Bearer secret-unredir"
        redirected = handler.redirect_request(req, None, 302, "Found", {}, target_url)
        assert redirected is not None
        assert not any(k.lower() == "authorization" for k in redirected.headers)
        assert not any(k.lower() == "authorization" for k in redirected.unredirected_hdrs)

    # Non-HTTP(S) redirect targets are rejected with UnsafeRedirectError (subclass of URLError).
    for unsafe_target in ("file:///etc/passwd", "ftp://example.com/v1"):
        req = Request("http://127.0.0.1:8000/v1/chat", method="GET")
        with pytest.raises(llm_client.UnsafeRedirectError) as exc_info:
            handler.redirect_request(req, None, 302, "Found", {}, unsafe_target)
        assert isinstance(exc_info.value, URLError)


def test_live_http_redirects_strip_auth_cross_origin_and_reject_unsafe_schemes() -> None:
    seen_b_auth: list[str | None] = []
    seen_a_same_origin_auth: list[str | None] = []
    redirect_targets: list[str] = []
    ok_body = json.dumps({"choices": [{"message": {"content": "OK"}}]}).encode()

    class HandlerB(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            seen_b_auth.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(ok_body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    class HandlerA(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            if length:
                self.rfile.read(length)
            target = redirect_targets.pop(0)
            self.send_response(302)
            self.send_header("Location", target)
            self.end_headers()

        def do_GET(self) -> None:  # noqa: N802
            seen_a_same_origin_auth.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.end_headers()
            self.wfile.write(ok_body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server_a = HTTPServer(("127.0.0.1", 0), HandlerA)
    server_b = HTTPServer(("127.0.0.1", 0), HandlerB)
    threading.Thread(target=server_a.serve_forever, daemon=True).start()
    threading.Thread(target=server_b.serve_forever, daemon=True).start()
    llm_client.reset_capability_cache()
    try:
        cfg = LlmConfig(
            base_url=f"http://127.0.0.1:{server_a.server_port}/v1",
            api_key="top-secret",
            max_retries=2,
        )
        # Cross-origin (different port): Authorization must be stripped.
        redirect_targets.append(f"http://127.0.0.1:{server_b.server_port}/v1/chat/completions")
        assert post_chat_completion(cfg, []).content == "OK"
        assert seen_b_auth == [None]

        # Same-origin relative redirect: Authorization is preserved.
        redirect_targets.append("/v2/chat/completions")
        assert post_chat_completion(cfg, []).content == "OK"
        assert seen_a_same_origin_auth == ["Bearer top-secret"]

        # Unsafe scheme redirects fail immediately (non-transient bad_request).
        for unsafe_loc in ("file:///etc/passwd", "ftp://127.0.0.1/v1"):
            redirect_targets.append(unsafe_loc)
            with pytest.raises(LlmRefineError) as caught:
                post_chat_completion(cfg, [])
            assert caught.value.kind == "bad_request"
            assert caught.value.transient is False
            assert "top-secret" not in str(caught.value)
    finally:
        server_a.shutdown()
        server_a.server_close()
        server_b.shutdown()
        server_b.server_close()


def test_bounded_response_read_http_exception_and_unicode_decode(provider, monkeypatch) -> None:
    config, (responses, _) = provider

    # Oversized response fails cleanly without retrying.
    monkeypatch.setattr(llm_client, "MAX_RESPONSE_BYTES", 128)
    responses.append((200, b"x" * 256, {}))
    with pytest.raises(LlmRefineError) as caught:
        post_chat_completion(LlmConfig(base_url=config.base_url, max_retries=2), [])
    assert caught.value.transient is False

    # Direct cancellable_request enforces max_bytes with ResponseTooLargeError.
    responses.append((200, b"y" * 200, {}))
    req = Request(f"{config.base_url}/chat/completions", data=b"{}", method="POST")
    with pytest.raises(llm_client.ResponseTooLargeError):
        llm_client.cancellable_request(req, timeout=5.0, max_bytes=64)

    monkeypatch.undo()

    # Invalid UTF-8 response body maps to format error rather than uncaught UnicodeDecodeError.
    responses.append((200, b"\xff\xfe\x80\x81", {}))
    with pytest.raises(LlmRefineError) as caught:
        post_chat_completion(LlmConfig(base_url=config.base_url, max_retries=0), [])
    assert caught.value.kind == "format"

    # http.client.HTTPException (e.g. RemoteDisconnected) is caught cleanly as network error.
    def disconnect(*args, **kwargs):
        raise http.client.RemoteDisconnected("remote end closed connection")

    monkeypatch.setattr(llm_client, "urlopen", disconnect)
    with pytest.raises(LlmRefineError) as caught:
        post_chat_completion(LlmConfig(base_url=config.base_url, max_retries=0), [])
    assert caught.value.kind == "network" and caught.value.transient


def test_http_error_detail_guards_unreadable_oversized_non_utf8_and_redacts() -> None:
    url = "https://api.example.com/v1/chat/completions"

    # fp=None must not raise AttributeError.
    no_fp = HTTPError(url, 502, "Bad Gateway", {}, None)
    assert "Bad Gateway" in llm_client.http_error_detail(no_fp)

    # Closed stream must not raise ValueError.
    closed_fp = io.BytesIO(b"secret body")
    closed_fp.close()
    closed_err = HTTPError(url, 500, "Server Closed", {}, closed_fp)
    assert "Server Closed" in llm_client.http_error_detail(closed_err)

    # Broken stream raising OSError falls back cleanly.
    class BrokenStream:
        def read(self, n: int = -1) -> bytes:
            raise OSError("socket reset")

        def close(self) -> None:
            return

    broken_err = HTTPError(url, 503, "Reset Reason", {}, BrokenStream())
    assert "Reset Reason" in llm_client.http_error_detail(broken_err)

    # Oversized error body is capped at MAX_ERROR_DETAIL_BYTES and output bounded to 300 chars.
    big_fp = io.BytesIO(b"E" * (llm_client.MAX_ERROR_DETAIL_BYTES * 3))
    big_err = HTTPError(url, 500, "Huge", {}, big_fp)
    detail = llm_client.http_error_detail(big_err)
    assert big_fp.tell() <= llm_client.MAX_ERROR_DETAIL_BYTES
    assert len(detail) <= 300

    # Non-UTF-8 bytes + Bearer token + explicit secret (even straddling cutoff) are redacted.
    raw = b"\xff\xfe auth failed Bearer sk-live-9999 " + (b"a" * 260) + b" my-super-secret-key tail"
    non_utf8_err = HTTPError(url, 401, "Unauthorized", {}, io.BytesIO(raw))
    redacted = llm_client.http_error_detail(non_utf8_err, secret="my-super-secret-key")
    assert "sk-live-9999" not in redacted
    assert "my-super-secret-key" not in redacted
    assert "[redacted]" in redacted
    assert len(redacted) <= 300
