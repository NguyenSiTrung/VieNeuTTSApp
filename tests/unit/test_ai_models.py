"""Unit tests for core/ai_models.py: Whisper ASR, Nemotron-3 GGUF, and audio.cpp downloads."""

from __future__ import annotations

import io
import tarfile
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

from vienetts_app.core.ai_models import (
    DIARIZATION_GGUF_MODELS,
    WHISPER_GGUF_MODELS,
    download_asr_model,
    download_audiocpp_runtime,
    download_diarization_model,
    is_asr_model_installed,
    is_audiocpp_installed,
    is_diarization_model_installed,
    remove_asr_model,
    remove_diarization_model,
    resolve_asr_model_path,
    resolve_audiocpp_cli,
    resolve_diarization_model_path,
)


def test_catalog_contains_nemotron_3_gguf_and_whisper_models() -> None:
    assert "nemotron-3-diarization-q8_0" in DIARIZATION_GGUF_MODELS
    assert "nemotron-3-diarization-bf16" in DIARIZATION_GGUF_MODELS
    q8 = DIARIZATION_GGUF_MODELS["nemotron-3-diarization-q8_0"]
    assert "audio-cpp/Nemotron-3-Diarization-GGUF" in q8.url
    assert q8.filename == "nemotron-3-diarization-q8_0.gguf"

    for key in ("tiny", "base", "small", "medium", "large-v3-turbo"):
        assert key in WHISPER_GGUF_MODELS


def test_download_and_remove_asr_and_diarization_models(tmp_path: Path) -> None:
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w:gz") as tf:
        bin_bytes = b"#!/bin/sh\necho audiocpp_cli\n"
        info = tarfile.TarInfo(name="bin/audiocpp_cli")
        info.size = len(bin_bytes)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(bin_bytes))
    tar_payload = tar_buf.getvalue()

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            data = tar_payload if self.path.endswith(".tar.gz") else b"GGUF_FAKE_WEIGHTS_" * 64
            self.send_response(200)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    port = server.server_port
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        assert is_diarization_model_installed("nemotron-3-diarization-q8_0", tmp_path) is False
        diar_path = download_diarization_model(
            "nemotron-3-diarization-q8_0",
            tmp_path,
            url_override=f"http://127.0.0.1:{port}/nemotron-3-diarization-q8_0.gguf",
        )
        assert diar_path.is_file()
        assert is_diarization_model_installed("nemotron-3-diarization-q8_0", tmp_path) is True
        assert resolve_diarization_model_path("nemotron-3-diarization-q8_0", tmp_path) == diar_path

        assert is_asr_model_installed("base", tmp_path) is False
        asr_path = download_asr_model(
            "base",
            tmp_path,
            url_override=f"http://127.0.0.1:{port}/ggml-base.bin",
        )
        assert asr_path.is_file()
        assert is_asr_model_installed("base", tmp_path) is True
        assert resolve_asr_model_path("base", tmp_path) == asr_path

        cli_path = download_audiocpp_runtime(
            tmp_path,
            url_override=f"http://127.0.0.1:{port}/audio-bin.tar.gz",
        )
        assert cli_path.is_file()
        assert is_audiocpp_installed(tmp_path) is True
        assert resolve_audiocpp_cli(tmp_path) == cli_path

        remove_diarization_model("nemotron-3-diarization-q8_0", tmp_path)
        assert is_diarization_model_installed("nemotron-3-diarization-q8_0", tmp_path) is False

        remove_asr_model("base", tmp_path)
        assert is_asr_model_installed("base", tmp_path) is False
    finally:
        server.shutdown()
        server.server_close()
