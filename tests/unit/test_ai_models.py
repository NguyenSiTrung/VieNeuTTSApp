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
    available_whisper_cpp_variants,
    download_asr_model,
    download_audiocpp_runtime,
    download_diarization_model,
    download_whisper_cpp_runtime,
    installed_whisper_cpp_variant,
    is_asr_model_installed,
    is_audiocpp_installed,
    is_diarization_model_installed,
    is_whisper_cpp_installed,
    remove_asr_model,
    remove_diarization_model,
    remove_whisper_cpp_runtime,
    resolve_asr_model_path,
    resolve_audiocpp_cli,
    resolve_audiocpp_variant,
    resolve_diarization_model_path,
    resolve_whisper_cpp_cli,
    resolve_whisper_cpp_variant,
)


def test_catalog_contains_nemotron_3_gguf_and_whisper_models() -> None:
    assert "nemotron-3-diarization-q8_0" in DIARIZATION_GGUF_MODELS
    assert "nemotron-3-diarization-bf16" in DIARIZATION_GGUF_MODELS
    q8 = DIARIZATION_GGUF_MODELS["nemotron-3-diarization-q8_0"]
    assert "audio-cpp/Nemotron-3-Diarization-GGUF" in q8.url
    assert q8.filename == "nemotron-3-diarization-q8_0.gguf"

    for key in ("tiny", "base", "small", "medium", "large-v3-turbo"):
        assert key in WHISPER_GGUF_MODELS
        spec = WHISPER_GGUF_MODELS[key]
        assert spec.size_bytes > 0
        assert len(spec.sha256) == 64


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
    monkeypatch.setitem(
        WHISPER_GGUF_MODELS,
        "base",
        replace(WHISPER_GGUF_MODELS["base"], size_bytes=0, sha256=""),
    )
    tar_buf = io.BytesIO()
    with tarfile.open(fileobj=tar_buf, mode="w:gz") as tf:
        bin_bytes = b"#!/usr/bin/env python3\nprint('audiocpp_cli')\n"
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

        assert remove_asr_model("base", tmp_path) is True
        assert is_asr_model_installed("base", tmp_path) is False
    finally:
        server.shutdown()
        server.server_close()


def test_truncated_managed_model_is_not_reported_installed(tmp_path: Path) -> None:
    """A GGUF left truncated by the old downloader must read as "not installed"."""
    from vienetts_app.core.ai_models import asr_models_dir, diarization_models_dir

    spec = DIARIZATION_GGUF_MODELS["nemotron-3-diarization-q8_0"]
    target = diarization_models_dir(tmp_path) / spec.filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"GGUF" + b"\0" * 8)
    assert target.stat().st_size != spec.size_bytes

    assert resolve_diarization_model_path("nemotron-3-diarization-q8_0", tmp_path) is None
    assert is_diarization_model_installed("nemotron-3-diarization-q8_0", tmp_path) is False

    asr_spec = WHISPER_GGUF_MODELS["base"]
    asr_target = asr_models_dir(tmp_path) / asr_spec.filename
    asr_target.parent.mkdir(parents=True, exist_ok=True)
    asr_target.write_bytes(b"GGML" + b"\0" * 8)
    assert asr_target.stat().st_size != asr_spec.size_bytes

    assert resolve_asr_model_path("base", tmp_path) is None
    assert is_asr_model_installed("base", tmp_path) is False


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
        with pytest.raises(AiModelInstallError):
            download_asr_model(
                "base",
                tmp_path,
                url_override=f"http://127.0.0.1:{server.server_port}/model.bin",
            )
    finally:
        server.shutdown()
        server.server_close()
    models_dir = tmp_path / "models" / "diarization"
    assert not list(models_dir.glob("*.gguf"))
    assert not list(models_dir.glob("*.part"))
    asr_dir = tmp_path / "models" / "whisper"
    assert not list(asr_dir.glob("*.bin"))
    assert not list(asr_dir.glob("*.part"))


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
    assert remove_asr_model(str(victim), tmp_path) is False
    assert victim.is_file()

    non_model = tmp_path / "notes.txt"
    non_model.write_text("keep", encoding="utf-8")
    assert resolve_asr_model_path(str(non_model), tmp_path) is None
    assert remove_asr_model(str(non_model), tmp_path) is False
    assert non_model.is_file()

    traversal = f"../../{tmp_path.name}/important.gguf"
    assert remove_diarization_model(traversal, tmp_path / "models") is False
    assert remove_asr_model(traversal, tmp_path / "models") is False
    assert victim.is_file()


def test_unknown_model_id_is_rejected(tmp_path: Path) -> None:
    with pytest.raises(AiModelInstallError, match="không có trong danh mục"):
        download_diarization_model("not-in-catalog", tmp_path, url_override="http://x/y.gguf")
    with pytest.raises(AiModelInstallError, match="không có trong danh mục"):
        download_asr_model("not-in-catalog", tmp_path, url_override="http://x/y.bin")


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
    good = _runtime_tar("#!/usr/bin/env python3\nprint('audiocpp_cli')\n")
    broken = _runtime_tar("#!/usr/bin/env python3\nimport sys\nsys.exit(2)\n")
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
    if sys.platform == "darwin":
        assert "metal" in variants
        assert variants["metal"].filename.startswith("audio-")
        assert resolve_audiocpp_variant("auto").id == "metal"
    else:
        assert "cpu" in variants
        assert variants["cpu"].filename.startswith("audio-")
    if sys.platform.startswith("linux"):
        assert "vulkan" in variants
        assert "vulkan" in variants["vulkan"].filename
        with pytest.raises(AiModelInstallError):
            resolve_audiocpp_variant("cuda")


def _whisper_runtime_tar(script: str) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tf:
        data = script.encode()
        info = tarfile.TarInfo(name="bin/whisper-cli")
        info.size = len(data)
        info.mode = 0o755
        tf.addfile(info, io.BytesIO(data))
        marker = b"ok"
        m_info = tarfile.TarInfo(name="bin/sibling.Marker")
        m_info.size = len(marker)
        m_info.mode = 0o644
        tf.addfile(m_info, io.BytesIO(marker))
    return buf.getvalue()


def test_whisper_cpp_runtime_download_probe_rollback_and_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    variants = available_whisper_cpp_variants()
    assert "cpu" in variants
    assert resolve_whisper_cpp_variant("auto").id == "cpu"
    with pytest.raises(AiModelInstallError):
        resolve_whisper_cpp_variant("nonexistent_device")

    monkeypatch.setattr(sys, "platform", "win32")
    win_variants = available_whisper_cpp_variants()
    assert "cpu" in win_variants and "cuda" in win_variants
    monkeypatch.undo()

    # Script requires sibling.Marker in cwd (proving --help runs with cwd=cli.parent)
    good = _whisper_runtime_tar(
        "#!/usr/bin/env python3\n"
        "from pathlib import Path\n"
        "import sys\n"
        "if not Path('./sibling.Marker').is_file():\n"
        "    sys.exit(1)\n"
        "print('whisper-cli help')\n"
    )
    broken = _whisper_runtime_tar("#!/usr/bin/env python3\nimport sys\nsys.exit(2)\n")
    port, shutdown = _serve({"/whisper-good.tar.gz": good, "/whisper-broken.tar.gz": broken})
    try:
        assert is_whisper_cpp_installed(tmp_path) is False
        cli = download_whisper_cpp_runtime(
            tmp_path, url_override=f"http://127.0.0.1:{port}/whisper-good.tar.gz"
        )
        assert cli.is_file()
        assert is_whisper_cpp_installed(tmp_path) is True
        assert resolve_whisper_cpp_cli(tmp_path) == cli
        assert installed_whisper_cpp_variant(tmp_path) == "cpu"

        with pytest.raises(AiModelInstallError):
            download_whisper_cpp_runtime(
                tmp_path, url_override=f"http://127.0.0.1:{port}/whisper-broken.tar.gz"
            )
        assert resolve_whisper_cpp_cli(tmp_path) == cli
        assert cli.is_file()
        runtimes = tmp_path / "runtimes"
        assert not list(runtimes.glob(".whisper_staging_*"))
        assert not list(runtimes.glob(".whisper_old_*"))

        assert remove_whisper_cpp_runtime(tmp_path) is True
        assert is_whisper_cpp_installed(tmp_path) is False
        assert remove_whisper_cpp_runtime(tmp_path) is False
    finally:
        shutdown()
