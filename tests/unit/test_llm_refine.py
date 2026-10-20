"""Unit tests for core/llm_refine.py: ASR cleanup, timing condensation, and translation."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from vienetts_app.core.llm_refine import (
    LlmConfig,
    LlmRefineError,
    max_chars_for_window,
    probe_llm_connection,
    refine_cues_with_llm,
)
from vienetts_app.core.subtitles import Cue


def test_max_chars_for_window_scales_with_duration_and_rate() -> None:
    # 2.0s window at 14 chars/sec * 1.25 rate = 35 chars
    assert max_chars_for_window(2000, chars_per_sec=14.0, max_rate=1.25) == 35
    # Short window still allows a sensible minimum (>=8 chars)
    assert max_chars_for_window(200) >= 8


def test_llm_refine_modes_and_connection_probe() -> None:
    received_requests: list[dict] = []

    class _MockLlmHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            length = int(self.headers.get("Content-Length", "0"))
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            received_requests.append(body)

            user_msg = body["messages"][-1]["content"]
            # Return markdown-fenced JSON to verify resilient extraction
            if "TEST_PING" in user_msg:
                reply_content = "OK"
            else:
                reply_items = [
                    {
                        "index": 1,
                        "speaker": "SPEAKER_01",
                        "text": "Xin chào mọi người.",
                    },
                    {
                        "index": 2,
                        "speaker": "SPEAKER_02",
                        "text": "Hôm nay trời đẹp quá.",
                    },
                ]
                reply_content = "```json\n" + json.dumps(reply_items, ensure_ascii=False) + "\n```"

            resp = {
                "choices": [
                    {
                        "message": {
                            "role": "assistant",
                            "content": reply_content,
                        }
                    }
                ]
            }
            encoded = json.dumps(resp, ensure_ascii=False).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = HTTPServer(("127.0.0.1", 0), _MockLlmHandler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        cfg = LlmConfig(
            base_url=f"http://127.0.0.1:{port}/v1",
            api_key="local-key",
            model="qwen2.5:7b",
        )
        ok, status = probe_llm_connection(cfg)
        assert ok is True
        assert "qwen2.5:7b" in status

        cues = [
            Cue(1, 0, 1500, "xin chao moi nguoi", speaker="SPEAKER_01"),
            Cue(
                2,
                1600,
                3000,
                "Hôm nay thời tiết bên ngoài thực sự rất là đẹp và dễ chịu vô cùng.",
                speaker="SPEAKER_01",
            ),
        ]
        for task in ("refine_asr", "fit_timing", "translate_dub"):
            refined = refine_cues_with_llm(cues, cfg, task=task, target_language="auto")
            assert len(refined) == 2
            assert refined[0].text == "Xin chào mọi người."
            assert refined[1].text == "Hôm nay trời đẹp quá."
            assert refined[1].speaker == "SPEAKER_02"
            assert refined[0].start_ms == 0 and refined[0].end_ms == 1500
        translate_sys = received_requests[-1]["messages"][0]["content"]
        assert "Vietnamese" in translate_sys
        assert "spoken auto" not in translate_sys

        with pytest.raises(LlmRefineError):
            refine_cues_with_llm(cues, cfg, task="refine_asr", cancel_check=lambda: True)
    finally:
        server.shutdown()
        server.server_close()

    with pytest.raises(LlmRefineError):
        refine_cues_with_llm(
            cues,
            LlmConfig(base_url="http://127.0.0.1:1/v1", timeout_s=0.5),
            task="refine_asr",
        )
