"""Managed catalog and downloader for on-device ASR, Nemotron-3 Diarization GGUF, and audio.cpp.

Provides:
- ``DIARIZATION_GGUF_MODELS``: ``audio-cpp/Nemotron-3-Diarization-GGUF`` weights
  (``nemotron-3-diarization-q8_0.gguf`` and ``nemotron-3-diarization-bf16.gguf``).
- ``WHISPER_GGUF_MODELS``: ``ggerganov/whisper.cpp`` weights
  (``tiny``, ``base``, ``small``, ``medium``, ``large-v3-turbo``).
- ``audiocpp_cli`` runtime discovery and download from ``0xShug0/audio.cpp`` releases.
"""

from __future__ import annotations

import contextlib
import os
import platform
import shutil
import stat
import sys
import tarfile
import time
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

CHUNK_BYTES = 256 * 1024
REPLACE_LOCK_ATTEMPTS = 5
REPLACE_LOCK_DELAY_S = 0.2


class AiModelInstallError(RuntimeError):
    """Downloading or installing an ASR/Diarization model or runtime failed."""


@dataclass(frozen=True)
class DownloadableModelSpec:
    id: str
    label: str
    filename: str
    url: str
    size_mb: float


DIARIZATION_GGUF_MODELS: dict[str, DownloadableModelSpec] = {
    "nemotron-3-diarization-q8_0": DownloadableModelSpec(
        id="nemotron-3-diarization-q8_0",
        label="Nemotron-3 Diarization Q8_0 (102 MB — Nhanh, nhẹ)",
        filename="nemotron-3-diarization-q8_0.gguf",
        url=(
            "https://huggingface.co/audio-cpp/Nemotron-3-Diarization-GGUF"
            "/resolve/main/nemotron-3-diarization-q8_0.gguf"
        ),
        size_mb=101.7,
    ),
    "nemotron-3-diarization-bf16": DownloadableModelSpec(
        id="nemotron-3-diarization-bf16",
        label="Nemotron-3 Diarization BF16 (190 MB — Chuẩn gốc NeMo)",
        filename="nemotron-3-diarization-bf16.gguf",
        url=(
            "https://huggingface.co/audio-cpp/Nemotron-3-Diarization-GGUF"
            "/resolve/main/nemotron-3-diarization-bf16.gguf"
        ),
        size_mb=189.5,
    ),
}

WHISPER_GGUF_MODELS: dict[str, DownloadableModelSpec] = {
    "tiny": DownloadableModelSpec(
        id="tiny",
        label="Whisper Tiny (75 MB — Siêu nhanh)",
        filename="ggml-tiny.bin",
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-tiny.bin",
        size_mb=75.0,
    ),
    "base": DownloadableModelSpec(
        id="base",
        label="Whisper Base (142 MB — Cân bằng, mặc định)",
        filename="ggml-base.bin",
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin",
        size_mb=142.0,
    ),
    "small": DownloadableModelSpec(
        id="small",
        label="Whisper Small (466 MB — Độ chính xác cao)",
        filename="ggml-small.bin",
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin",
        size_mb=466.0,
    ),
    "medium": DownloadableModelSpec(
        id="medium",
        label="Whisper Medium Q5_0 (514 MB — Đa ngôn ngữ chuẩn)",
        filename="ggml-medium-q5_0.bin",
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium-q5_0.bin",
        size_mb=514.0,
    ),
    "large-v3-turbo": DownloadableModelSpec(
        id="large-v3-turbo",
        label="Whisper Large-v3-Turbo Q5_0 (547 MB — Tốt nhất)",
        filename="ggml-large-v3-turbo-q5_0.bin",
        url=(
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin"
        ),
        size_mb=547.0,
    ),
}

AUDIOCPP_RELEASE_TAG = "v0.9.2"


def _promote_file(part: Path, target: Path) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    for attempt in range(REPLACE_LOCK_ATTEMPTS):
        try:
            os.replace(part, target)
            return target
        except PermissionError:
            if attempt == REPLACE_LOCK_ATTEMPTS - 1:
                raise
            time.sleep(REPLACE_LOCK_DELAY_S)
    return target


def _stream_download(
    url: str,
    target: Path,
    *,
    progress_cb: Callable[[float], None] | None = None,
) -> Path:
    target.parent.mkdir(parents=True, exist_ok=True)
    part = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
    req = Request(url, headers={"User-Agent": "VieNeuTTSApp/0.3"})
    try:
        with urlopen(req, timeout=60) as resp:  # noqa: S310
            total = int(resp.headers.get("Content-Length") or 0)
            written = 0
            with part.open("wb") as fh:
                while True:
                    chunk = resp.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    fh.write(chunk)
                    written += len(chunk)
                    if progress_cb is not None and total > 0:
                        progress_cb(min(0.99, written / total))
        if not part.is_file() or part.stat().st_size == 0:
            raise AiModelInstallError(f"Tệp tải về từ {url} bị rỗng.")
        out = _promote_file(part, target)
        if progress_cb is not None:
            progress_cb(1.0)
        return out
    except (HTTPError, URLError, TimeoutError, OSError) as exc:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
        raise AiModelInstallError(f"Không thể tải mô hình từ '{url}': {exc}") from exc


# ── Diarization models (audio-cpp/Nemotron-3-Diarization-GGUF) ───────────────


def diarization_models_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "models" / "diarization"


def resolve_diarization_model_path(
    model_id_or_path: str, data_dir: Path | None = None
) -> Path | None:
    raw = (model_id_or_path or "nemotron-3-diarization-q8_0").strip()
    cand = Path(raw)
    if cand.is_file():
        return cand
    if data_dir is None:
        return None
    base = diarization_models_dir(data_dir)
    spec = DIARIZATION_GGUF_MODELS.get(raw)
    if spec is not None:
        p = base / spec.filename
        if p.is_file() and p.stat().st_size > 0:
            return p
    direct = base / (raw if raw.endswith(".gguf") else f"{raw}.gguf")
    if direct.is_file() and direct.stat().st_size > 0:
        return direct
    return None


def is_diarization_model_installed(model_id: str, data_dir: Path) -> bool:
    return resolve_diarization_model_path(model_id, data_dir) is not None


def download_diarization_model(
    model_id: str,
    data_dir: Path,
    *,
    progress_cb: Callable[[float], None] | None = None,
    url_override: str = "",
) -> Path:
    key = (model_id or "nemotron-3-diarization-q8_0").strip()
    spec = (
        DIARIZATION_GGUF_MODELS.get(key) or DIARIZATION_GGUF_MODELS["nemotron-3-diarization-q8_0"]
    )
    url = url_override.strip() or spec.url
    target = diarization_models_dir(data_dir) / spec.filename
    return _stream_download(url, target, progress_cb=progress_cb)


def remove_diarization_model(model_id: str, data_dir: Path) -> None:
    path = resolve_diarization_model_path(model_id, data_dir)
    if path is not None:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


# ── Whisper ASR models ───────────────────────────────────────────────────────


def asr_models_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "models" / "whisper"


def resolve_asr_model_path(model_id_or_path: str, data_dir: Path | None = None) -> Path | None:
    raw = (model_id_or_path or "base").strip()
    cand = Path(raw)
    if cand.is_file():
        return cand
    if data_dir is None:
        return None
    base = asr_models_dir(data_dir)
    spec = WHISPER_GGUF_MODELS.get(raw)
    if spec is not None:
        p = base / spec.filename
        if p.is_file() and p.stat().st_size > 0:
            return p
    for name in (raw, f"ggml-{raw}.bin", f"{raw}.bin"):
        p = base / name
        if p.is_file() and p.stat().st_size > 0:
            return p
    return None


def is_asr_model_installed(model_id: str, data_dir: Path) -> bool:
    return resolve_asr_model_path(model_id, data_dir) is not None


def download_asr_model(
    model_id: str,
    data_dir: Path,
    *,
    progress_cb: Callable[[float], None] | None = None,
    url_override: str = "",
) -> Path:
    key = (model_id or "base").strip()
    spec = WHISPER_GGUF_MODELS.get(key) or WHISPER_GGUF_MODELS["base"]
    url = url_override.strip() or spec.url
    target = asr_models_dir(data_dir) / spec.filename
    return _stream_download(url, target, progress_cb=progress_cb)


def remove_asr_model(model_id: str, data_dir: Path) -> None:
    path = resolve_asr_model_path(model_id, data_dir)
    if path is not None:
        with contextlib.suppress(OSError):
            path.unlink(missing_ok=True)


# ── audio.cpp Native Runtime (audiocpp_cli) ──────────────────────────────────


def audiocpp_runtime_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "runtimes" / "audiocpp"


def resolve_audiocpp_cli(data_dir: Path | None = None) -> Path | None:
    exe = "audiocpp_cli.exe" if os.name == "nt" else "audiocpp_cli"
    if data_dir is not None:
        rt_dir = audiocpp_runtime_dir(data_dir)
        for cand in (rt_dir / "bin" / exe, rt_dir / exe):
            if cand.is_file():
                return cand
        if rt_dir.is_dir():
            for found in rt_dir.rglob(exe):
                if found.is_file():
                    return found
    on_path = shutil.which("audiocpp_cli")
    return Path(on_path) if on_path else None


def is_audiocpp_installed(data_dir: Path | None = None) -> bool:
    return resolve_audiocpp_cli(data_dir) is not None


def _default_audiocpp_archive_url() -> str:
    base = f"https://github.com/0xShug0/audio.cpp/releases/download/{AUDIOCPP_RELEASE_TAG}"
    machine = platform.machine().lower()
    if sys.platform == "win32":
        return f"{base}/audio-{AUDIOCPP_RELEASE_TAG}-bin-windows-x64-cpu-portable.zip"
    if sys.platform == "darwin":
        if "arm" in machine or "aarch64" in machine:
            return f"{base}/audio-{AUDIOCPP_RELEASE_TAG}-bin-macos-arm64-metal.tar.gz"
        return f"{base}/audio-{AUDIOCPP_RELEASE_TAG}-bin-macos-x64-metal.tar.gz"
    return f"{base}/audio-{AUDIOCPP_RELEASE_TAG}-bin-ubuntu-x64-cpu-portable.tar.gz"


def download_audiocpp_runtime(
    data_dir: Path,
    *,
    progress_cb: Callable[[float], None] | None = None,
    url_override: str = "",
) -> Path:
    url = url_override.strip() or _default_audiocpp_archive_url()
    rt_dir = audiocpp_runtime_dir(data_dir)
    rt_dir.mkdir(parents=True, exist_ok=True)
    suffix = ".zip" if url.lower().endswith(".zip") else ".tar.gz"
    archive_path = rt_dir / f".audiocpp_download_{uuid.uuid4().hex}{suffix}"
    try:
        _stream_download(url, archive_path, progress_cb=progress_cb)
        if suffix == ".zip":
            with zipfile.ZipFile(archive_path, "r") as zf:
                zf.extractall(rt_dir)
        else:
            with tarfile.open(archive_path, "r:*") as tf:
                tf.extractall(rt_dir, filter="data")
    finally:
        with contextlib.suppress(OSError):
            archive_path.unlink(missing_ok=True)

    cli = resolve_audiocpp_cli(data_dir)
    if cli is None:
        raise AiModelInstallError("Không tìm thấy thực thi audiocpp_cli trong gói tải về.")
    if os.name != "nt":
        with contextlib.suppress(OSError):
            cli.chmod(cli.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return cli
