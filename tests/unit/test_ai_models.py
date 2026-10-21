"""Unit tests for core/ai_models.py: Whisper ASR, Nemotron-3 GGUF, and audio.cpp downloads."""

from __future__ import annotations

import io
import sys
import tarfile
import threading
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from vienetts_app.core.ai_models import (
    DIARIZATION_GGUF_MODELS,
    WHISPER_GGUF_MODELS,
    AiModelInstallError,
    _stream_download,
    available_audiocpp_variants,
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
    resolve_audiocpp_variant,
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


def test_download_and_remove_asr_and_diarization_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The fake payload is not the real 102 MB GGUF, so the managed-size guard
    # has to be opted out of for this end-to-end download/remove test.
    monkeypatch.setitem(
        DIARIZATION_GGUF_MODELS,
        "nemotron-3-diarization-q8_0",
        replace(DIARIZATION_GGUF_MODELS["nemotron-3-diarization-q8_0"], size_bytes=0, sha256=""),
    )
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


def test_truncated_managed_model_is_not_reported_installed(tmp_path: Path) -> None:
    """A GGUF left truncated by the old downloader must read as "not installed"."""
    from vienetts_app.core.ai_models import diarization_models_dir

    spec = DIARIZATION_GGUF_MODELS["nemotron-3-diarization-q8_0"]
    target = diarization_models_dir(tmp_path) / spec.filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"GGUF" + b"\0" * 8)
    assert target.stat().st_size != spec.size_bytes

    assert resolve_diarization_model_path("nemotron-3-diarization-q8_0", tmp_path) is None
    assert is_diarization_model_installed("nemotron-3-diarization-q8_0", tmp_path) is False


def _serve(payload: dict[str, bytes]):
    """Start a one-route HTTP server; yields (port, shutdown)."""

    class _Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            body = payload.get(self.path)
            if body is None:
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = HTTPServer(("127.0.0.1", 0), _Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    def shutdown() -> None:
        server.shutdown()
        server.server_close()

    return server.server_port, shutdown


def test_truncated_body_is_rejected_and_part_file_removed(tmp_path: Path) -> None:
    """A short body (proxy/CDN cut) must never be promoted to an installed GGUF."""

    class _Truncating(BaseHTTPRequestHandler):
        def do_GET(self) -> None:  # noqa: N802
            self.send_response(200)
            self.send_header("Content-Length", "1000000")
            self.end_headers()
            self.wfile.write(b"x" * 1000)

        def log_message(self, format: str, *args: object) -> None:  # noqa: A002
            return

    server = HTTPServer(("127.0.0.1", 0), _Truncating)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        with pytest.raises(AiModelInstallError):
            download_diarization_model(
                "nemotron-3-diarization-q8_0",
                tmp_path,
                url_override=f"http://127.0.0.1:{server.server_port}/model.gguf",
            )
    finally:
        server.shutdown()
        server.server_close()
    models_dir = tmp_path / "models" / "diarization"
    assert not list(models_dir.glob("*.gguf"))
    assert not list(models_dir.glob("*.part"))


def test_sha256_mismatch_is_rejected(tmp_path: Path) -> None:
    port, shutdown = _serve({"/model.gguf": b"not-the-real-weights"})
    try:
        with pytest.raises(AiModelInstallError):
            _stream_download(
                f"http://127.0.0.1:{port}/model.gguf",
                tmp_path / "model.gguf",
                expected_size=len(b"not-the-real-weights"),
                expected_sha256="0" * 64,
            )
    finally:
        shutdown()
    assert not (tmp_path / "model.gguf").exists()


def test_remove_diarization_model_refuses_paths_outside_app_dir(tmp_path: Path) -> None:
    victim = tmp_path / "important.gguf"
    victim.write_bytes(b"keep me")
    assert remove_diarization_model(str(victim), tmp_path) is False
    assert victim.is_file()

    traversal = f"../../{tmp_path.name}/important.gguf"
    assert remove_diarization_model(traversal, tmp_path / "models") is False
    assert victim.is_file()


def test_unknown_model_id_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AiModelInstallError):
        download_diarization_model("not-in-catalog", tmp_path, url_override="http://x/y.gguf")


def _runtime_tar(script: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = script.encode()
        info = tarfile.TarInfo(name="bin/audiocpp_cli")
        info.size = len(data)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(data))
    return buf.getvalue()


def test_broken_runtime_archive_keeps_previous_install(tmp_path: Path) -> None:
    good = _runtime_tar("#!/bin/sh\necho audiocpp_cli\n")
    broken = _runtime_tar("#!/bin/sh\nexit 2\n")
    port, shutdown = _serve({"/good.tar.gz": good, "/broken.tar.gz": broken})
    try:
        old = download_audiocpp_runtime(
            tmp_path, url_override=f"http://127.0.0.1:{port}/good.tar.gz"
        )
        assert old.is_file()
        with pytest.raises(AiModelInstallError):
            download_audiocpp_runtime(
                tmp_path, url_override=f"http://127.0.0.1:{port}/broken.tar.gz"
            )
    finally:
        shutdown()
    assert resolve_audiocpp_cli(tmp_path) == old
    assert old.is_file()
    runtimes = tmp_path / "runtimes"
    assert not list(runtimes.glob(".audiocpp_staging_*"))
    assert not list(runtimes.glob(".audiocpp_old_*"))


def test_runtime_variants_for_platform() -> None:
    variants = available_audiocpp_variants()
    assert "cpu" in variants
    assert variants["cpu"].filename.startswith("audio-")
    if sys.platform.startswith("linux"):
        assert "vulkan" in variants
        assert "vulkan" in variants["vulkan"].filename
        with pytest.raises(AiModelInstallError):
            resolve_audiocpp_variant("cuda")
