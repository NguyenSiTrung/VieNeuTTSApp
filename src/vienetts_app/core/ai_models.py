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
import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
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

from vienetts_app.core.managed_install import free_space_bytes, safe_remove

CHUNK_BYTES = 256 * 1024
REPLACE_LOCK_ATTEMPTS = 5
REPLACE_LOCK_DELAY_S = 0.2
# A managed runtime/model download must not start when the volume cannot hold
# the artifact plus extraction headroom.
INSTALL_SPACE_HEADROOM = 64 * 1024 * 1024
RUNTIME_META_FILENAME = "runtime.json"


class AiModelInstallError(RuntimeError):
    """Downloading or installing an ASR/Diarization model or runtime failed."""


@dataclass(frozen=True)
class DownloadableModelSpec:
    id: str
    label: str
    filename: str
    url: str
    size_mb: float
    # Exact byte size and SHA-256 when the upstream artifact is immutable.
    # ``size_mb`` is the rounded, human-facing label; these two are what makes
    # a truncated or corrupted download detectable instead of "installed".
    size_bytes: int = 0
    sha256: str = ""


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
        size_bytes=106_675_136,
        sha256="9a737455bd10123bcf1e036d9a0b07b6e8c42e7d0dd1a5ee4141dc386db46d0b",
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
        size_bytes=198_720_928,
        sha256="84f6f0f12b9ecf2615548427e884534a72fedbca32d00872d95c0f72a078c953",
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
    expected_size: int = 0,
    expected_sha256: str = "",
    min_free_bytes: int = 0,
) -> Path:
    """Download ``url`` to ``target`` atomically, refusing a partial artifact.

    The previous implementation only rejected a zero-byte file: an interrupted
    transfer, a proxy that closes the body early, or a corrupted CDN response
    all produced a file that ``is_*_installed()`` accepted, and the native
    runtime then failed to load it (which the ``auto`` diarization backend
    swallowed). Three independent checks now guard the promotion:

    * ``Content-Length`` (when the server sends one) must equal the byte count;
    * ``expected_size`` (catalog knowledge) must equal the byte count;
    * ``expected_sha256`` (catalog knowledge) must match the streamed digest.

    The part file is always removed on failure, so a retry starts clean.
    """
    target.parent.mkdir(parents=True, exist_ok=True)
    if min_free_bytes:
        free = free_space_bytes(target.parent)
        if free is not None and free < min_free_bytes:
            raise AiModelInstallError(
                f"Không đủ dung lượng trống để tải {target.name} "
                f"(cần {min_free_bytes / (1024 * 1024):.0f} MB, "
                f"còn {free / (1024 * 1024):.0f} MB)."
            )
    part = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
    digest = hashlib.sha256()
    req = Request(url, headers={"User-Agent": "VieNeuTTSApp/0.3"})
    try:
        with urlopen(req, timeout=60) as resp:  # noqa: S310
            declared = int(resp.headers.get("Content-Length") or 0)
            total = expected_size or declared
            written = 0
            with part.open("wb") as fh:
                while True:
                    chunk = resp.read(CHUNK_BYTES)
                    if not chunk:
                        break
                    fh.write(chunk)
                    digest.update(chunk)
                    written += len(chunk)
                    if progress_cb is not None and total > 0:
                        progress_cb(min(0.99, written / total))
        if written == 0:
            raise AiModelInstallError(f"Tệp tải về từ {url} bị rỗng.")
        if expected_size and written != expected_size:
            raise AiModelInstallError(
                f"Tệp {target.name} tải về không đủ: {written:,} / {expected_size:,} byte. "
                "Kết nối có thể đã bị ngắt — hãy thử tải lại."
            )
        if declared and written != declared:
            raise AiModelInstallError(
                f"Tệp {target.name} tải về bị thiếu so với Content-Length "
                f"({written:,} / {declared:,} byte) — hãy thử tải lại."
            )
        if expected_sha256 and digest.hexdigest() != expected_sha256:
            raise AiModelInstallError(
                f"Tệp {target.name} không khớp mã kiểm tra SHA-256 — "
                "bản tải về bị hỏng, hãy thử tải lại."
            )
        out = _promote_file(part, target)
        if progress_cb is not None:
            progress_cb(1.0)
        return out
    except AiModelInstallError:
        with contextlib.suppress(OSError):
            part.unlink(missing_ok=True)
        raise
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
    """Resolve a diarization GGUF from an id or an explicit ``.gguf`` path.

    An explicit path is honoured only when it is a readable ``.gguf``/``.bin``
    file: the setting is user-writable, and the old ``is_file()`` check let any
    file (a video, a settings file) masquerade as an installed model — and be
    deleted by :func:`remove_diarization_model`.
    """
    raw = (model_id_or_path or "nemotron-3-diarization-q8_0").strip()
    cand = Path(raw)
    if cand.is_file() and cand.suffix.lower() in (".gguf", ".bin"):
        return cand
    if data_dir is None:
        return None
    base = diarization_models_dir(data_dir)
    spec = DIARIZATION_GGUF_MODELS.get(raw)
    if spec is not None:
        p = base / spec.filename
        if p.is_file() and _managed_model_size_ok(p, spec):
            return p
    direct = base / (raw if raw.endswith(".gguf") else f"{raw}.gguf")
    known_filename = spec is not None and direct == base / spec.filename
    if not known_filename and direct.is_file() and direct.stat().st_size > 0:
        return direct
    return None


def _managed_model_size_ok(path: Path, spec: DownloadableModelSpec) -> bool:
    """Reject a managed model file whose size does not match the catalog.

    The old downloader wrote straight to the final path, so an interrupted
    download left a truncated GGUF that ``is_file() and size > 0`` accepted as
    installed: the CLI then failed with an opaque GGUF parse error and there was
    no way to retry from the UI. A size mismatch now means "not installed", so
    the settings card offers the download button again.
    """
    expected = int(getattr(spec, "size_bytes", 0) or 0)
    try:
        size = path.stat().st_size
    except OSError:
        return False
    return size > 0 and (expected <= 0 or size == expected)


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
    spec = DIARIZATION_GGUF_MODELS.get(key)
    if spec is None:
        # An unknown id used to fall back to the Q8_0 artifact while the
        # resolver kept looking for ``<id>.gguf``: every launch reported "not
        # installed" and downloaded 102 MB again. Fail loudly instead.
        raise AiModelInstallError(
            f"Mô hình diarization {key!r} không có trong danh mục của ứng dụng."
        )
    url = url_override.strip() or spec.url
    target = diarization_models_dir(data_dir) / spec.filename
    # A URL override is a test/self-hosted artifact: the catalog size and hash
    # describe the Hugging Face file, so they only apply to that file.
    return _stream_download(
        url,
        target,
        progress_cb=progress_cb,
        expected_size=0 if url_override.strip() else spec.size_bytes,
        expected_sha256="" if url_override.strip() else spec.sha256,
        min_free_bytes=0 if url_override.strip() else spec.size_bytes + INSTALL_SPACE_HEADROOM,
    )


def remove_diarization_model(model_id: str, data_dir: Path) -> bool:
    """Remove an installed diarization GGUF, refusing paths outside the app dir.

    The model setting is user-writable, so the resolved path may point at any
    file on disk. Only a ``.gguf``/``.bin`` file inside ``models/diarization``
    is ever deleted; anything else is reported as not removed.
    """
    path = resolve_diarization_model_path(model_id, data_dir)
    if path is None:
        return False
    base = diarization_models_dir(data_dir).resolve()
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if not resolved.is_relative_to(base) or resolved.suffix.lower() not in (".gguf", ".bin"):
        return False
    with contextlib.suppress(OSError):
        resolved.unlink(missing_ok=True)
        return not resolved.exists()
    return False


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


@dataclass(frozen=True)
class AudiocppRuntimeVariant:
    """One downloadable ``audiocpp_cli`` build.

    The variant is picked from the ``diarization_device`` setting; the CPU and
    Metal archives were the only ones the app knew about. Upstream also ships
    a Linux/Windows Vulkan build that contains the CLI (verified in v0.9.2)
    and no Linux CUDA CLI at all, so "cuda" is intentionally not offered here.
    """

    id: str
    label: str
    filename: str


def available_audiocpp_variants() -> dict[str, AudiocppRuntimeVariant]:
    tag = AUDIOCPP_RELEASE_TAG
    if sys.platform == "win32":
        return {
            "cpu": AudiocppRuntimeVariant(
                "cpu",
                "CPU (tương thích rộng)",
                f"audio-{tag}-bin-windows-x64-cpu-portable.zip",
            ),
            "vulkan": AudiocppRuntimeVariant(
                "vulkan",
                "Vulkan GPU (nhanh hơn trên GPU rời/tích hợp)",
                f"audio-{tag}-bin-windows-x64-vulkan-portable.zip",
            ),
        }
    if sys.platform == "darwin":
        machine = platform.machine().lower()
        arch = "arm64" if ("arm" in machine or "aarch64" in machine) else "x64"
        return {
            "metal": AudiocppRuntimeVariant(
                "metal",
                "Metal GPU (Apple Silicon/Intel)",
                f"audio-{tag}-bin-macos-{arch}-metal.tar.gz",
            )
        }
    return {
        "cpu": AudiocppRuntimeVariant(
            "cpu",
            "CPU (tương thích rộng)",
            f"audio-{tag}-bin-ubuntu-x64-cpu-portable.tar.gz",
        ),
        "vulkan": AudiocppRuntimeVariant(
            "vulkan",
            "Vulkan GPU (nhanh hơn trên GPU rời/tích hợp)",
            f"audio-{tag}-bin-ubuntu-x64-vulkan-portable.tar.gz",
        ),
    }


def default_audiocpp_variant() -> str:
    return "metal" if sys.platform == "darwin" else "cpu"


def resolve_audiocpp_variant(requested: str = "") -> AudiocppRuntimeVariant:
    variants = available_audiocpp_variants()
    key = (requested or "").strip().lower()
    if not key or key == "auto":
        key = default_audiocpp_variant()
    variant = variants.get(key)
    if variant is None:
        offered = ", ".join(sorted(variants))
        raise AiModelInstallError(
            f"Không có bản runtime audio.cpp cho lựa chọn {requested!r} trên hệ điều hành này. "
            f"Các bản khả dụng: {offered}."
        )
    return variant


def audiocpp_archive_url(variant: AudiocppRuntimeVariant) -> str:
    base = f"https://github.com/0xShug0/audio.cpp/releases/download/{AUDIOCPP_RELEASE_TAG}"
    return f"{base}/{variant.filename}"


def installed_audiocpp_variant(data_dir: Path) -> str:
    """Variant id recorded by the last successful install, or ""."""
    try:
        meta_path = audiocpp_runtime_dir(data_dir) / RUNTIME_META_FILENAME
        meta = json.loads(meta_path.read_text("utf-8"))
    except (OSError, ValueError):
        return ""
    variant = meta.get("variant") if isinstance(meta, dict) else None
    return variant if isinstance(variant, str) else ""


def _extract_archive(archive_path: Path, destination: Path) -> None:
    """Extract a zip/tar under ``destination`` with traversal protection."""
    if archive_path.suffix.lower() == ".zip":
        with zipfile.ZipFile(archive_path, "r") as zf:
            root = destination.resolve()
            for member in zf.infolist():
                target = (destination / member.filename).resolve()
                if not target.is_relative_to(root):
                    raise AiModelInstallError("Gói runtime chứa đường dẫn không hợp lệ.")
            zf.extractall(destination)
        return
    with tarfile.open(archive_path, "r:*") as tf:
        root = destination.resolve()
        for member in tf.getmembers():
            target = (destination / member.name).resolve()
            if not target.is_relative_to(root):
                raise AiModelInstallError("Gói runtime chứa đường dẫn không hợp lệ.")
        tf.extractall(destination, filter="data")


def _locate_cli(root: Path) -> Path | None:
    exe = "audiocpp_cli.exe" if os.name == "nt" else "audiocpp_cli"
    for cand in (root / "bin" / exe, root / exe):
        if cand.is_file():
            return cand
    return next((p for p in root.rglob(exe) if p.is_file()), None)


def _probe_runtime(cli: Path) -> None:
    """Run ``audiocpp_cli --help`` once so a broken archive is rejected.

    A partial extraction, a missing shared library, or a Vulkan build on a
    machine without a Vulkan loader all produce a binary that exists but
    cannot run. Without this probe such an install reported success and every
    diarization run failed later.
    """
    try:
        proc = subprocess.run(
            [str(cli), "--help"],
            capture_output=True,
            timeout=30,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise AiModelInstallError(
            f"Runtime audio.cpp không chạy được trên máy này ({exc}). "
            "Hãy thử bản CPU nếu đang dùng bản GPU."
        ) from exc
    if proc.returncode not in (0, 1):
        detail = (proc.stderr or proc.stdout or b"").decode("utf-8", "replace").strip()
        raise AiModelInstallError(
            "Runtime audio.cpp tải về nhưng không khởi động được"
            + (f": {detail[:300]}" if detail else ".")
        )


def download_audiocpp_runtime(
    data_dir: Path,
    *,
    progress_cb: Callable[[float], None] | None = None,
    url_override: str = "",
    variant: str = "",
) -> Path:
    """Download, extract, validate and atomically install ``audiocpp_cli``.

    Extraction happens in a staging directory; the previous runtime stays
    untouched until the new one has been probed. A failed download or a
    non-runnable archive therefore never destroys a working install.
    """
    runtime_variant = resolve_audiocpp_variant(variant)
    url = url_override.strip() or audiocpp_archive_url(runtime_variant)
    rt_dir = audiocpp_runtime_dir(data_dir)
    root = rt_dir.parent
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".audiocpp_staging_{uuid.uuid4().hex}"
    backup = rt_dir.with_name(f".audiocpp_old_{uuid.uuid4().hex}")
    staging.mkdir(parents=True, exist_ok=False)
    suffix = ".zip" if url.lower().endswith(".zip") else ".tar.gz"
    archive_path = staging / f"runtime{suffix}"
    try:
        _stream_download(url, archive_path, progress_cb=progress_cb)
        _extract_archive(archive_path, staging)
        with contextlib.suppress(OSError):
            archive_path.unlink(missing_ok=True)
        cli = _locate_cli(staging)
        if cli is None:
            raise AiModelInstallError("Không tìm thấy thực thi audiocpp_cli trong gói tải về.")
        if os.name != "nt":
            with contextlib.suppress(OSError):
                cli.chmod(cli.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        _probe_runtime(cli)
        (staging / RUNTIME_META_FILENAME).write_text(
            json.dumps({"variant": runtime_variant.id, "tag": AUDIOCPP_RELEASE_TAG}),
            encoding="utf-8",
        )
        if rt_dir.exists():
            os.replace(rt_dir, backup)
        try:
            os.replace(staging, rt_dir)
        except OSError as exc:
            if backup.exists():
                with contextlib.suppress(OSError):
                    os.replace(backup, rt_dir)
            raise AiModelInstallError(f"Không thể kích hoạt runtime audio.cpp mới: {exc}") from exc
        if backup.exists():
            safe_remove(backup)
    finally:
        with contextlib.suppress(OSError):
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    installed = resolve_audiocpp_cli(data_dir)
    if installed is None:
        raise AiModelInstallError("Không tìm thấy thực thi audiocpp_cli sau khi cài đặt.")
    return installed


def remove_audiocpp_runtime(data_dir: Path) -> bool:
    rt_dir = audiocpp_runtime_dir(data_dir)
    if not rt_dir.is_dir():
        return False
    safe_remove(rt_dir)
    return not rt_dir.exists()
