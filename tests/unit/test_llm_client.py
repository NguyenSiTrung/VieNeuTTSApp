"""Tests for the OpenAI-compatible transport."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from vienetts_app.core import llm_client
from vienetts_app.core.llm_client import LlmConfig, LlmRefineError, post_chat_completion


@pytest.fixture
def provider():
    responses: list[tuple[int, object, dict[str, str]]] = []
    requests: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            requests.append(json.loads(self.rfile.read(int(self.headers["Content-Length"]))))
            status, body, headers = responses.pop(0)
            data = body.encode() if isinstance(body, str) else json.dumps(body).encode()
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
