"""Unit tests for core/llm_refine.py: ASR cleanup, timing condensation, and translation."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from vienetts_app.core.llm_refine import (
    LANGUAGE_CHARS_PER_SEC,
    ChatResult,
    LlmConfig,
    LlmRefineError,
    _parse_reply,
    _system_prompt_for_task,
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


def test_max_chars_for_window_language_scaling_and_slack() -> None:
    assert LANGUAGE_CHARS_PER_SEC["vi"] == 14.0
    assert LANGUAGE_CHARS_PER_SEC["en"] == 14.0
    assert LANGUAGE_CHARS_PER_SEC["zh"] == 4.8
    assert LANGUAGE_CHARS_PER_SEC["ja"] == 6.0
    assert LANGUAGE_CHARS_PER_SEC["ko"] == 5.5

    # 2.0s window at max_rate=1.20 across languages
    vi_budget = max_chars_for_window(2000, max_rate=1.20, language="vi")
    en_budget = max_chars_for_window(2000, max_rate=1.20, language="en")
    zh_budget = max_chars_for_window(2000, max_rate=1.20, language="zh")
    ja_budget = max_chars_for_window(2000, max_rate=1.20, language="ja")
    ko_budget = max_chars_for_window(2000, max_rate=1.20, language="ko")

    assert vi_budget == en_budget == 34  # round(2.0 * 14.0 * 1.20)
    assert zh_budget == 12  # round(2.0 * 4.8 * 1.20)
    assert ja_budget == 14  # round(2.0 * 6.0 * 1.20)
    assert ko_budget == 13  # round(2.0 * 5.5 * 1.20)
    assert max_chars_for_window(2000, max_rate=1.20, language="Chinese") == zh_budget

    # Minimum budget is 4 for CJK (cps <= 7.0) and 8 for alphabetic scripts
    assert max_chars_for_window(100, language="zh") == 4
    assert max_chars_for_window(100, language="vi") == 8

    # Post-cue slack increases budget up to 800 ms cap
    with_slack = max_chars_for_window(2000, max_rate=1.20, language="vi", slack_ms=500)
    capped_slack = max_chars_for_window(2000, max_rate=1.20, language="vi", slack_ms=1500)
    max_slack = max_chars_for_window(2000, max_rate=1.20, language="vi", slack_ms=800)
    assert with_slack > vi_budget
    assert with_slack == 42  # round(2.5 * 14.0 * 1.20)
    assert capped_slack == max_slack == 47  # round(2.8 * 14.0 * 1.20)


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
            assert refined[1].speaker == "SPEAKER_01"
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


def test_refine_cues_with_llm_uses_speech_duration_and_post_cue_slack(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured_messages: list[list[dict[str, str]]] = []

    def fake_post(
        config: LlmConfig,
        messages: list[dict[str, str]],
        *,
        temperature: float = 0.2,
        json_mode: bool = False,
        cancel_check=None,
    ) -> ChatResult:
        captured_messages.append(messages)
        assert json_mode is True
        items = json.loads(messages[-1]["content"])["cues"]
        return ChatResult(
            json.dumps(
                [
                    {"index": it["index"], "speaker": it["speaker"], "text": it["text"]}
                    for it in items
                ],
                ensure_ascii=False,
            )
        )

    monkeypatch.setattr("vienetts_app.core.llm_refine._post_chat_completion", fake_post)

    # Cue 1 has a 2500ms caption window but only 1000ms of speech [100, 1100],
    # followed by Cue 2 whose effective_speech_start_ms is 3100 (gap_after_ms = 600ms,
    # borrowable_slack_ms = 480ms).
    # Cue 2 is the final cue so gap_after_ms = 800ms (borrowable_slack_ms = 680ms).
    cues = [
        Cue(
            1,
            0,
            2500,
            "Hello world",
            speaker="SPEAKER_01",
            speech_start_ms=100,
            speech_end_ms=1100,
        ),
        Cue(
            2,
            2900,
            4500,
            "Second line",
            speaker="SPEAKER_02",
            speech_start_ms=3100,
            speech_end_ms=4300,
        ),
    ]
    cfg = LlmConfig()
    refine_cues_with_llm(
        cues,
        cfg,
        task="translate_dub",
        target_language="zh",
        rate_cap=1.40,
        batch_size=1,
    )
    assert len(captured_messages) == 2
    envelope1 = json.loads(captured_messages[0][-1]["content"])
    envelope2 = json.loads(captured_messages[1][-1]["content"])
    item1 = envelope1["cues"][0]
    item2 = envelope2["cues"][0]

    assert set(item1) == {"index", "speaker", "text", "maxChars"}
    assert envelope1["contextAfter"] == [{"speaker": "SPEAKER_02", "text": "Second line"}]
    assert item1["maxChars"] == max_chars_for_window(
        1000,
        max_rate=1.20,
        language="zh",
        slack_ms=480,
    )

    assert set(item2) == {"index", "speaker", "text", "maxChars"}
    assert envelope2["contextBefore"] == [{"speaker": "SPEAKER_01", "text": "Hello world"}]
    assert item2["maxChars"] == max_chars_for_window(
        1200,
        max_rate=1.20,
        language="zh",
        slack_ms=680,
    )


def test_parse_reply_matches_indices_and_preserves_speakers() -> None:
    cues = [
        Cue(1, 0, 1000, "A", speaker="SPEAKER_01"),
        Cue(2, 1000, 2000, "B", speaker="SPEAKER_02"),
        Cue(3, 2000, 3000, "C", speaker="SPEAKER_01"),
    ]
    items = [
        {"index": 1, "text": "first", "speaker": None},
        {"index": 3, "text": "third", "speaker": "SPEAKER_99"},
    ]
    encoded = json.dumps({"cues": items})
    for raw in (
        encoded,
        json.dumps(items),
        f"```json\n{encoded}\n```",
        f"<think>The array has [3] items</think>{encoded}",
        f"{encoded}\nNote: I kept [names] unchanged.",
    ):
        result, missing = _parse_reply(
            raw, cues, task="refine_asr", allowed_speakers={"SPEAKER_01", "SPEAKER_02"}
        )
        assert [c.text for c in result] == ["first", "B", "third"]
        assert [c.speaker for c in result] == ["SPEAKER_01", "SPEAKER_02", "SPEAKER_01"]
        assert missing == [2]
    positional = [{"text": "X", "speaker": ""}, {"text": "Y"}, {"text": "Z"}]
    result, missing = _parse_reply(
        json.dumps(positional), cues, task="refine_asr", allowed_speakers={"SPEAKER_01"}
    )
    assert [c.text for c in result] == ["X", "Y", "Z"] and not missing
    _, missing = _parse_reply(
        json.dumps(positional[:2]), cues, task="refine_asr", allowed_speakers={"SPEAKER_01"}
    )
    assert missing == [1, 2, 3]
    swapped = json.dumps([{"index": 1, "text": "", "speaker": "SPEAKER_02"}])
    for task in ("refine_asr", "translate_dub", "fit_timing"):
        result, _ = _parse_reply(
            swapped, cues, task=task, allowed_speakers={"SPEAKER_01", "SPEAKER_02"}
        )
        assert result[0].text == "A"
        assert result[0].speaker == ("SPEAKER_02" if task == "refine_asr" else "SPEAKER_01")


def test_request_envelope_glossary_context_and_language(monkeypatch: pytest.MonkeyPatch) -> None:
    requests: list[dict] = []

    def fake_post(config, messages, *, temperature=0.2, json_mode=False, cancel_check=None):
        assert json_mode is True
        envelope = json.loads(messages[-1]["content"])
        requests.append(envelope)
        return ChatResult(json.dumps({"cues": envelope["cues"]}))

    monkeypatch.setattr("vienetts_app.core.llm_refine._post_chat_completion", fake_post)
    cues = [Cue(i, i * 1000, i * 1000 + 900, f"line {i}", speaker=f"S{i % 2}") for i in range(1, 7)]
    notes: list[str] = []
    refine_cues_with_llm(cues, LlmConfig(), batch_size=2, glossary="g" * 2100, notes=notes)
    assert all(set(c) == {"index", "speaker", "text"} for c in requests[1]["cues"])
    assert requests[1]["allowedSpeakers"] == ["S0", "S1"]
    assert len(requests[1]["contextBefore"]) == 2 and len(requests[1]["contextAfter"]) == 2
    assert requests[1]["glossary"] == "g" * 2000
    assert notes
    for code, name in (
        ("en", "English"),
        ("ja", "Japanese"),
        ("", "Vietnamese"),
        ("auto", "Vietnamese"),
        ("xx", "xx"),
    ):
        assert f"spoken {name}" in _system_prompt_for_task("translate_dub", code)
