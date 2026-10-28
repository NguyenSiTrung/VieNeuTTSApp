"""Unit tests for core/ai_models.py: Whisper ASR, Nemotron-3 GGUF, and audio.cpp downloads."""

from __future__ import annotations

import ast
import hashlib
import io
import json
import platform
import sys
import tarfile
import threading
import urllib.request
import zipfile
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

from vienetts_app.core import ai_models
from vienetts_app.core.ai_models import (
    AUDIO_MODELS,
    DIARIZATION_GGUF_MODELS,
    FFMPEG_CELLS,
    WHISPER_GGUF_MODELS,
    AiModelDownloadCancelled,
    AiModelInstallError,
    _stream_download,
    audio_model_catalog,
    audio_model_path,
    audio_model_status,
    audiocpp_unavailable_reason,
    available_audiocpp_variants,
    available_whisper_cpp_variants,
    download_asr_model,
    download_audio_model,
    download_audiocpp_runtime,
    download_diarization_model,
    download_whisper_cpp_runtime,
    ffmpeg_runtime_dir,
    ffmpeg_runtime_status,
    install_ffmpeg_runtime,
    installed_audiocpp_variant,
    installed_whisper_cpp_variant,
    is_asr_model_installed,
    is_audiocpp_installed,
    is_diarization_model_installed,
    is_whisper_cpp_installed,
    load_media_runtime_manifest,
    parse_media_runtime_manifest,
    remove_asr_model,
    remove_audio_model,
    remove_diarization_model,
    remove_ffmpeg_runtime,
    remove_whisper_cpp_runtime,
    resolve_asr_model_path,
    resolve_audiocpp_cli,
    resolve_audiocpp_variant,
    resolve_diarization_model_path,
    resolve_whisper_cpp_cli,
    resolve_whisper_cpp_variant,
)

PROJECT_ROOT = Path(__file__).resolve().parents[2]
SPEC_PATH = PROJECT_ROOT / "packaging" / "vienetts-app.spec"


def _host(monkeypatch: pytest.MonkeyPatch, system: str, machine: str) -> None:
    """Pretend to run on ``system``/``machine`` for the variant/cell tables."""
    monkeypatch.setattr(sys, "platform", system)
    monkeypatch.setattr(platform, "machine", lambda: machine)


def _local_download(monkeypatch: pytest.MonkeyPatch, files: dict[str, Path]) -> list[dict]:
    """Serve catalog URLs from local files through the REAL ``_stream_download``.

    The size/SHA-256 checks still run on the bytes; ``calls`` records what the
    installer asked for. An unmapped URL fails the test (no network).
    """
    calls: list[dict] = []

    def fake(url: str, target: Path, **kwargs):
        calls.append({"url": url, **kwargs})
        assert url in files, f"unexpected download: {url}"
        return _stream_download(files[url].as_uri(), target, **kwargs)

    # urlopen's default opener builds an SSL context on first use, and with
    # sys.platform patched to "win32" that loads Windows-only certificate
    # APIs. A file-only opener never builds one, whatever ran before.
    file_opener = urllib.request.OpenerDirector()
    file_opener.add_handler(urllib.request.FileHandler())
    monkeypatch.setattr(urllib.request, "_opener", file_opener)
    monkeypatch.setattr(ai_models, "_stream_download", fake)
    return calls


def _no_download(monkeypatch: pytest.MonkeyPatch) -> None:
    def fail(url: str, target: Path, **kwargs):
        raise AssertionError(f"must not download {url}")

    monkeypatch.setattr(ai_models, "_stream_download", fail)


def _write_manifest(path: Path, runtimes: dict) -> Path:
    path.write_text(json.dumps({"schema": 1, "runtimes": runtimes}), encoding="utf-8")
    return path


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


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
    # The label that says "mặc định" names the model Settings actually defaults to.
    from vienetts_app.core.models import Settings

    marked = [k for k, s in WHISPER_GGUF_MODELS.items() if "mặc định" in s.label.lower()]
    assert marked == [Settings.asr_model]


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

    # A managed runtime/model path that is a symlink out of the data dir is
    # refused instead of following it.
    data_dir = tmp_path / "data"
    outside = tmp_path / "outside"
    (outside / "bin").mkdir(parents=True)
    (outside / "bin" / "ffmpeg").write_text("keep", encoding="utf-8")
    onnx_victim = outside / "silero_vad.onnx"
    onnx_victim.write_bytes(b"x" * AUDIO_MODELS["silero-vad"].size_bytes)
    vad_target = data_dir / "models" / "vad" / "silero_vad.onnx"
    vad_target.parent.mkdir(parents=True)
    ffmpeg_runtime_dir(data_dir).parent.mkdir(parents=True)
    try:
        ffmpeg_runtime_dir(data_dir).symlink_to(outside, target_is_directory=True)
        vad_target.symlink_to(onnx_victim)
    except OSError:
        pytest.skip("symlinks unavailable on this host")
    assert remove_ffmpeg_runtime(data_dir) is False
    assert (outside / "bin" / "ffmpeg").is_file()
    assert remove_audio_model("silero-vad", data_dir) is False
    assert onnx_victim.is_file()
    assert remove_audio_model("../../outside/silero_vad.onnx", data_dir) is False


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


def _zip_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        for name, data in members.items():
            zf.writestr(name, data)
    return buf.getvalue()


def test_runtime_variants_for_platform(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probes: list[str] = []

    def driver(version: str | None):
        def probe() -> str | None:
            probes.append("probe")
            return version

        return probe

    offered: list = []

    _host(monkeypatch, "darwin", "arm64")
    variants = available_audiocpp_variants()
    assert set(variants) == {"metal"}
    assert variants["metal"].filename == "audio-v0.9.2-bin-macos-arm64-metal.tar.gz"
    assert resolve_audiocpp_variant("auto").id == "metal"
    offered += variants.values()

    _host(monkeypatch, "linux", "x86_64")
    variants = available_audiocpp_variants()
    assert set(variants) == {"cpu", "vulkan"}
    assert "vulkan" in variants["vulkan"].filename
    with pytest.raises(AiModelInstallError):
        resolve_audiocpp_variant("cuda")
    offered += variants.values()

    # Upstream ships no Linux arm64 build: never offer the x64 one.
    _host(monkeypatch, "linux", "aarch64")
    assert available_audiocpp_variants() == {}
    assert audiocpp_unavailable_reason() == "unsupported"
    with pytest.raises(AiModelInstallError, match="unsupported"):
        resolve_audiocpp_variant("auto")
    assert probes == []  # the NVIDIA probe is Windows-only

    # Windows: the CUDA CLI zips need an NVIDIA driver new enough for them.
    _host(monkeypatch, "win32", "AMD64")
    monkeypatch.setattr(ai_models, "_nvidia_driver_cuda_version", driver(None))
    assert set(available_audiocpp_variants()) == {"cpu", "vulkan"}
    assert audiocpp_unavailable_reason() == ""
    with pytest.raises(AiModelInstallError):
        resolve_audiocpp_variant("cuda")
    monkeypatch.setattr(ai_models, "_nvidia_driver_cuda_version", driver("12.2"))
    assert set(available_audiocpp_variants()) == {"cpu", "vulkan", "cuda12.4"}
    assert resolve_audiocpp_variant("cuda").id == "cuda12.4"
    monkeypatch.setattr(ai_models, "_nvidia_driver_cuda_version", driver("13.0"))
    variants = available_audiocpp_variants()
    assert set(variants) == {"cpu", "vulkan", "cuda12.4", "cuda13.3"}
    assert resolve_audiocpp_variant("cuda").id == "cuda13.3"
    assert resolve_audiocpp_variant("auto").id == "cpu"
    cuda12 = variants["cuda12.4"]
    assert (cuda12.filename, cuda12.size_bytes) == (
        "audio-v0.9.2-bin-windows-x64-cuda12.4.zip",
        463_532_839,
    )
    assert [c.filename for c in cuda12.companions] == [
        "audio-v0.9.2-cudart-windows-x64-cuda12.4.zip"
    ]
    assert variants["cuda13.3"].filename == "audio-v0.9.2-bin-windows-x64-cuda13.3.zip"
    assert variants["cuda13.3"].size_bytes == 275_142_402
    offered += variants.values()

    # Every offered archive (and companion) is pinned by size and SHA-256.
    for variant in offered:
        for asset in (variant, *variant.companions):
            assert asset.size_bytes > 0 and len(asset.sha256) == 64, asset.filename

    # A CUDA install fetches the CLI zip AND its cudart companion, both
    # verified, into one runtime dir.
    cli_zip = _zip_bytes(
        {"audiocpp_cli": b"#!/usr/bin/env python3\nprint('audiocpp_cli')\n", "ggml-cuda.dll": b"g"}
    )
    cudart_zip = _zip_bytes({"cudart64_12.dll": b"cudart"})
    base = "https://github.com/0xShug0/audio.cpp/releases/download/v0.9.2"
    pinned = replace(
        cuda12,
        size_bytes=len(cli_zip),
        sha256=_sha(cli_zip),
        companions=(
            replace(cuda12.companions[0], size_bytes=len(cudart_zip), sha256=_sha(cudart_zip)),
        ),
    )
    monkeypatch.setattr(
        ai_models, "available_audiocpp_variants", lambda: {"cuda12.4": pinned, "cpu": pinned}
    )
    (tmp_path / "cli.zip").write_bytes(cli_zip)
    (tmp_path / "cudart.zip").write_bytes(cudart_zip)
    calls = _local_download(
        monkeypatch,
        {
            f"{base}/{cuda12.filename}": tmp_path / "cli.zip",
            f"{base}/{cuda12.companions[0].filename}": tmp_path / "cudart.zip",
        },
    )
    data_dir = tmp_path / "data"
    cli = download_audiocpp_runtime(data_dir, variant="cuda12.4")
    assert (cli.parent / "cudart64_12.dll").is_file()
    assert installed_audiocpp_variant(data_dir) == "cuda12.4"
    assert [(c["expected_size"], c["expected_sha256"]) for c in calls] == [
        (len(cli_zip), _sha(cli_zip)),
        (len(cudart_zip), _sha(cudart_zip)),
    ]


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
    _host(monkeypatch, "win32", "AMD64")
    win_variants = available_whisper_cpp_variants()
    assert "cpu" in win_variants and "cuda" in win_variants
    _host(monkeypatch, "linux", "x86_64")
    variants = available_whisper_cpp_variants()
    assert set(variants) == {"cpu"}
    assert resolve_whisper_cpp_variant("auto").id == "cpu"
    with pytest.raises(AiModelInstallError):
        resolve_whisper_cpp_variant("nonexistent_device")

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


def test_whisper_cpp_macos_cells_come_from_the_manifest_and_unpublished_never_downloads(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The shipped manifest publishes no macOS cell yet: guidance, no download.
    _no_download(monkeypatch)
    _host(monkeypatch, "darwin", "arm64")
    variants = available_whisper_cpp_variants()
    assert set(variants) == {"cpu", "metal"}
    assert "Homebrew" in variants["metal"].unavailable_reason
    # The Metal build also runs on CPU (``-ng``), so every device maps to it.
    for requested in ("", "auto", "metal", "cpu"):
        assert resolve_whisper_cpp_variant(requested).cell == "macos-arm64-metal"
    assert resolve_whisper_cpp_variant("auto").id == "metal"
    with pytest.raises(AiModelInstallError, match="Homebrew"):
        download_whisper_cpp_runtime(tmp_path, variant="auto")

    _host(monkeypatch, "darwin", "x86_64")
    variants = available_whisper_cpp_variants()
    assert set(variants) == {"cpu"}
    assert variants["cpu"].cell == "macos-x64-cpu"
    assert resolve_whisper_cpp_variant("metal").cell == "macos-x64-cpu"
    with pytest.raises(AiModelInstallError, match="Homebrew"):
        download_whisper_cpp_runtime(tmp_path, variant="cpu")
    assert not (tmp_path / "runtimes").exists()
    for variant in variants.values():
        assert "ubuntu" not in variant.filename and "ubuntu" not in variant.url

    # Once published, the cell's pinned pack is downloaded and verified.
    pack = _whisper_runtime_tar("#!/usr/bin/env python3\nprint('whisper-cli help')\n")
    (tmp_path / "pack.tar.gz").write_bytes(pack)
    url = "https://nguyensitrung.github.io/VieNeuTTSApp/packs/whisper-cli-macos-arm64-metal.tar.gz"
    cell = {
        "published": True,
        "url": url,
        "size": len(pack),
        "sha256": _sha(pack),
        "archive": "tar.gz",
        "version": "b5454",
        "license": "MIT",
        "source": "https://github.com/ggml-org/whisper.cpp/tree/b5454",
    }
    manifest = _write_manifest(
        tmp_path / "manifest.json", {"whisper_cpp": {"cells": {"macos-arm64-metal": cell}}}
    )
    monkeypatch.setattr(ai_models, "MEDIA_RUNTIME_MANIFEST_PATH", manifest)
    calls = _local_download(monkeypatch, {url: tmp_path / "pack.tar.gz"})
    _host(monkeypatch, "darwin", "arm64")
    assert available_whisper_cpp_variants()["metal"].unavailable_reason == ""
    cli = download_whisper_cpp_runtime(tmp_path, variant="auto")
    assert cli.is_file()
    assert installed_whisper_cpp_variant(tmp_path) == "metal"
    assert [(c["expected_size"], c["expected_sha256"]) for c in calls] == [(len(pack), _sha(pack))]


def _ffmpeg_stub(tool: str, version: str, encoders: tuple[str, ...], filters: tuple[str, ...]):
    """A Python stand-in answering ``-version`` / ``-encoders`` / ``-filters``."""
    return (
        "#!/usr/bin/env python3\n"
        "import sys\n"
        f"ENCODERS = {list(encoders)!r}\n"
        f"FILTERS = {list(filters)!r}\n"
        "args = sys.argv[1:]\n"
        "if '-version' in args:\n"
        f"    print('{tool} version {version} Copyright (c) 2000-2026 the FFmpeg developers')\n"
        "elif '-encoders' in args:\n"
        "    print('Encoders:')\n"
        "    print(' V..... = Video')\n"
        "    print(' A..... = Audio')\n"
        "    print(' ------')\n"
        "    for name in ENCODERS:\n"
        "        print(f' V....D {name:<20} stub encoder')\n"
        "elif '-filters' in args:\n"
        "    print('Filters:')\n"
        "    print('  T.. = Timeline support')\n"
        "    print('  V = Video input/output')\n"
        "    for name in FILTERS:\n"
        "        print(f' ..C {name:<17} V->V       stub filter')\n"
        "else:\n"
        "    sys.exit(1)\n"
    ).encode()


def _ffmpeg_pack(version: str, encoders: tuple[str, ...], filters: tuple[str, ...]) -> bytes:
    # A zip, like the Windows packs: no exec bits, so the installer must chmod.
    return _zip_bytes(
        {
            "bin/ffmpeg": _ffmpeg_stub("ffmpeg", version, encoders, filters),
            "bin/ffprobe": _ffmpeg_stub("ffprobe", version, (), ()),
            "LICENSE": b"GPL-3.0-or-later",
            "SOURCE.txt": b"https://github.com/BtbN/FFmpeg-Builds",
        }
    )


def test_ffmpeg_runtime_install_verifies_probes_rolls_back_and_removes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    encoders = ("libx264", "aac", "libvpx-vp9", "libopus")
    filters = ("scale", "subtitles", "volume")
    packs = {
        "good": _ffmpeg_pack("7.1-good", encoders, filters),
        # Exact names only: the ``libx264rgb`` decoy must not satisfy libx264.
        "no-x264": _ffmpeg_pack("7.1-x", ("libx264rgb", "aac", "libvpx-vp9"), filters),
        "no-subs": _ffmpeg_pack("7.1-s", encoders, ("scale", "ass")),
    }
    base = "https://nguyensitrung.github.io/VieNeuTTSApp/packs"
    files: dict[str, Path] = {}
    for name, data in packs.items():
        (tmp_path / f"{name}.zip").write_bytes(data)
        files[f"{base}/ffmpeg-{name}.zip"] = tmp_path / f"{name}.zip"
    calls = _local_download(monkeypatch, files)
    manifest = tmp_path / "manifest.json"
    monkeypatch.setattr(ai_models, "MEDIA_RUNTIME_MANIFEST_PATH", manifest)
    _host(monkeypatch, "linux", "x86_64")

    def publish(name: str, *, published: bool = True, sha256: str = "") -> None:
        data = packs[name]
        cell = {
            "published": published,
            "url": f"{base}/ffmpeg-{name}.zip" if published else "",
            "size": len(data) if published else 0,
            "sha256": (sha256 or _sha(data)) if published else "",
            "archive": "zip",
            "version": name,
            "license": "GPL-3.0-or-later",
            "source": "https://github.com/BtbN/FFmpeg-Builds",
        }
        _write_manifest(manifest, {"ffmpeg": {"cells": {"linux-x64": cell}}})

    data_dir = tmp_path / "data"
    publish("good")
    progress: list[float] = []
    ffmpeg = install_ffmpeg_runtime(data_dir, progress_cb=progress.append)
    assert ffmpeg.parent == ffmpeg_runtime_dir(data_dir) / "bin"
    assert progress[-1] == 1.0
    status = ffmpeg_runtime_status(data_dir)
    assert (status.cell, status.downloadable, status.managed) == ("linux-x64", True, True)
    assert status.ffmpeg == ffmpeg
    assert status.ffprobe is not None and status.ffprobe.parent == ffmpeg.parent
    assert status.version == "7.1-good"
    before = ffmpeg.read_bytes()

    # A corrupt pack and packs missing a required encoder/filter are all
    # rejected; the working runtime survives each attempt untouched.
    for name, sha256, reason in (
        ("good", "0" * 64, "SHA-256"),
        ("no-x264", "", "libx264"),
        ("no-subs", "", "subtitles"),
    ):
        publish(name, sha256=sha256)
        with pytest.raises(AiModelInstallError, match=reason):
            install_ffmpeg_runtime(data_dir)
        assert ffmpeg.read_bytes() == before
        assert ffmpeg_runtime_status(data_dir).version == "7.1-good"
    assert not list((data_dir / "runtimes").glob(".ffmpeg_*"))
    assert all(c["expected_size"] > 0 and len(c["expected_sha256"]) == 64 for c in calls)

    # An unpublished cell is guidance only — never a download.
    publish("good", published=False)
    _no_download(monkeypatch)
    status = ffmpeg_runtime_status(data_dir)
    assert status.downloadable is False and "ffmpeg" in status.note
    with pytest.raises(AiModelInstallError, match="ffmpeg"):
        install_ffmpeg_runtime(data_dir)
    assert ffmpeg.read_bytes() == before

    assert remove_ffmpeg_runtime(data_dir) is True
    assert not ffmpeg_runtime_dir(data_dir).exists()
    assert ffmpeg_runtime_status(data_dir).managed is False
    assert remove_ffmpeg_runtime(data_dir) is False


def test_audio_model_catalog_downloads_verify_cancel_and_remove(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    catalog = {spec.id: spec for spec in audio_model_catalog()}
    assert set(catalog) == {"whisper-vad-silero-v6.2.0", "silero-vad", "speaker-embedding"}
    for spec in catalog.values():
        assert spec.size_bytes > 0 and len(spec.sha256) == 64, spec.id
        assert spec.license in ("MIT", "Apache-2.0"), spec.id  # spec D2 / FR-4.5

    # >1 download chunk each, so the cancel below lands mid-transfer.
    payloads = {
        model_id: (model_id.encode() * 40_000)[: ai_models.CHUNK_BYTES + 1000 + i]
        for i, model_id in enumerate(catalog)
    }
    files: dict[str, Path] = {}
    for model_id, spec in catalog.items():
        src = tmp_path / f"{model_id}.src"
        src.write_bytes(payloads[model_id])
        files[spec.url] = src
        monkeypatch.setitem(
            AUDIO_MODELS,
            model_id,
            replace(spec, size_bytes=len(payloads[model_id]), sha256=_sha(payloads[model_id])),
        )
    calls = _local_download(monkeypatch, files)
    data_dir = tmp_path / "data"
    models = data_dir / "models"
    expected_dir = {
        "whisper-vad-silero-v6.2.0": models / "whisper",
        "silero-vad": models / "vad",
        "speaker-embedding": models / "speaker",
    }
    for model_id in catalog:
        assert audio_model_path(model_id, data_dir) is None
        path = download_audio_model(model_id, data_dir)
        assert path.parent == expected_dir[model_id]
        assert audio_model_path(model_id, data_dir) == path
        status = audio_model_status(model_id, data_dir)
        assert status.installed and status.path == path
    assert [c["expected_sha256"] for c in calls] == [_sha(payloads[m]) for m in catalog]

    # Cancel mid-download: nothing (not even a part file) is left behind.
    assert remove_audio_model("silero-vad", data_dir) is True
    cancel = threading.Event()
    with pytest.raises(AiModelDownloadCancelled):
        download_audio_model(
            "silero-vad",
            data_dir,
            progress_cb=lambda _p: cancel.set(),
            cancel_check=cancel.is_set,
        )
    assert list((models / "vad").iterdir()) == []

    # Wrong bytes fail the SHA-256 pin; a truncated file reads as missing.
    monkeypatch.setitem(
        AUDIO_MODELS, "silero-vad", replace(AUDIO_MODELS["silero-vad"], sha256="0" * 64)
    )
    with pytest.raises(AiModelInstallError, match="SHA-256"):
        download_audio_model("silero-vad", data_dir)
    assert audio_model_path("silero-vad", data_dir) is None
    speaker = audio_model_path("speaker-embedding", data_dir)
    assert speaker is not None
    speaker.write_bytes(b"short")
    assert audio_model_path("speaker-embedding", data_dir) is None
    assert audio_model_status("speaker-embedding", data_dir).installed is False

    with pytest.raises(AiModelInstallError, match="không có trong danh mục"):
        download_audio_model("not-in-catalog", data_dir)
    assert remove_audio_model("whisper-vad-silero-v6.2.0", data_dir) is True
    assert audio_model_path("whisper-vad-silero-v6.2.0", data_dir) is None
    assert remove_audio_model("whisper-vad-silero-v6.2.0", data_dir) is False


def _spec_datas() -> list[str]:
    for node in ast.walk(ast.parse(SPEC_PATH.read_text(encoding="utf-8"))):
        targets = getattr(node, "targets", [])
        if (
            isinstance(node, ast.Assign)
            and any(getattr(target, "id", "") == "datas" for target in targets)
            and isinstance(node.value, ast.List)
        ):
            return [ast.unparse(element) for element in node.value.elts]
    raise AssertionError("the spec has no literal `datas = [...]` block")


def test_media_runtime_manifest_is_frozen_data_and_strictly_validated(tmp_path: Path) -> None:
    # Loaders read these via ``Path(__file__).with_name(...)``, which
    # PyInstaller never collects: an unlisted manifest makes every frozen
    # lookup report "not available".
    core = PROJECT_ROOT / "src" / "vienetts_app" / "core"
    manifests = sorted(p.name for p in core.glob("*_manifests.json"))
    assert "media_runtime_manifests.json" in manifests
    datas = _spec_datas()
    for name in manifests:
        assert any(
            f"src/vienetts_app/core/{name}" in entry and "'vienetts_app/core'" in entry
            for entry in datas
        ), f"spec does not bundle {name}"

    shipped = load_media_runtime_manifest()
    assert set(shipped.cells("ffmpeg")) == set(FFMPEG_CELLS)
    assert {"macos-arm64-metal", "macos-x64-cpu"} <= set(shipped.cells("whisper_cpp"))

    good = {
        "published": True,
        "url": "https://nguyensitrung.github.io/VieNeuTTSApp/packs/ffmpeg-linux-x64.tar.gz",
        "size": 10,
        "sha256": "a" * 64,
        "archive": "tar.gz",
        "license": "GPL-3.0-or-later",
        "source": "https://github.com/BtbN/FFmpeg-Builds",
    }

    def doc(cell: dict, *, schema: object = 1, cell_id: str = "linux-x64") -> dict:
        return {"schema": schema, "runtimes": {"ffmpeg": {"cells": {cell_id: cell}}}}

    parsed = parse_media_runtime_manifest(doc(good)).cell("ffmpeg", "linux-x64")
    assert parsed is not None and parsed.published and parsed.size_bytes == 10
    for bad in (
        doc(good, schema=2),
        doc(good, cell_id="linux-riscv64"),
        doc({**good, "sha256": ""}),
        doc({**good, "size": True}),
        doc({**good, "url": "http://nguyensitrung.github.io/VieNeuTTSApp/packs/f.tar.gz"}),
        doc({**good, "url": "https://example.com/ffmpeg-linux-x64.tar.gz"}),
        doc({**good, "license": ""}),
        # An unpublished cell must not carry a download recipe.
        doc({**good, "published": False}),
    ):
        with pytest.raises(ValueError):
            parse_media_runtime_manifest(bad)
    # A corrupt data file means "nothing published", never a crash.
    broken = tmp_path / "manifest.json"
    broken.write_text("{", encoding="utf-8")
    assert load_media_runtime_manifest(broken).cells("ffmpeg") == {}
