"""Managed catalog and downloader for on-device ASR, Nemotron-3 Diarization GGUF, and audio.cpp.

Provides:
- ``DIARIZATION_GGUF_MODELS``: ``audio-cpp/Nemotron-3-Diarization-GGUF`` weights
  (``nemotron-3-diarization-q8_0.gguf`` and ``nemotron-3-diarization-bf16.gguf``).
- ``WHISPER_GGUF_MODELS``: ``ggerganov/whisper.cpp`` weights
  (``tiny``, ``base``, ``small``, ``medium``, ``large-v3-turbo``).
- ``AUDIO_MODELS``: optional Silero VAD (whisper.cpp ggml + ONNX) and ONNX
  speaker-embedding weights, behind a generic catalog API.
- ``audiocpp_cli`` runtime discovery and download from ``0xShug0/audio.cpp`` releases.
- ``whisper-cli`` and ``ffmpeg``/``ffprobe`` runtimes; macOS ``whisper-cli`` and
  every ffmpeg pack come from the project-published ``media_runtime_manifests.json``.
"""

from __future__ import annotations

import contextlib
import functools
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import time
import uuid
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from vienetts_app.core.managed_install import check_download_url, free_space_bytes, safe_remove
from vienetts_app.core.processes import PROBE_TIMEOUT_S, find_executable, run_cli

CHUNK_BYTES = 256 * 1024
REPLACE_LOCK_ATTEMPTS = 5
REPLACE_LOCK_DELAY_S = 0.2
# A managed runtime/model download must not start when the volume cannot hold
# the artifact plus extraction headroom.
INSTALL_SPACE_HEADROOM = 64 * 1024 * 1024
RUNTIME_META_FILENAME = "runtime.json"


class AiModelInstallError(RuntimeError):
    """Downloading or installing an ASR/Diarization model or runtime failed."""


class AiModelDownloadCancelled(AiModelInstallError):
    """``cancel_check`` stopped a download or install; nothing was installed."""


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
        size_bytes=77_691_713,
        sha256="be07e048e1e599ad46341c8d2a135645097a538221678b7acdd1b1919c6e1b21",
    ),
    "base": DownloadableModelSpec(
        id="base",
        label="Whisper Base (142 MB — Cân bằng)",
        filename="ggml-base.bin",
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-base.bin",
        size_mb=142.0,
        size_bytes=147_951_465,
        sha256="60ed5bc3dd14eea856493d334349b405782ddcaf0028d4b5df4088345fba2efe",
    ),
    "small": DownloadableModelSpec(
        id="small",
        label="Whisper Small (466 MB — Độ chính xác cao)",
        filename="ggml-small.bin",
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin",
        size_mb=466.0,
        size_bytes=487_601_967,
        sha256="1be3a9b2063867b937e64e2ec7483364a79917e157fa98c5d94b5c1fffea987b",
    ),
    "medium": DownloadableModelSpec(
        id="medium",
        label="Whisper Medium Q5_0 (514 MB — Đa ngôn ngữ chuẩn)",
        filename="ggml-medium-q5_0.bin",
        url="https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-medium-q5_0.bin",
        size_mb=514.0,
        size_bytes=539_212_467,
        sha256="19fea4b380c3a618ec4723c3eef2eb785ffba0d0538cf43f8f235e7b3b34220f",
    ),
    "large-v3-turbo": DownloadableModelSpec(
        id="large-v3-turbo",
        label="Whisper Large-v3-Turbo Q5_0 (547 MB — Tốt nhất, mặc định)",
        filename="ggml-large-v3-turbo-q5_0.bin",
        url=(
            "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-large-v3-turbo-q5_0.bin"
        ),
        size_mb=547.0,
        size_bytes=574_041_195,
        sha256="394221709cd5ad1f40c46e6031ca61bce88931e6e088c188294c6d5a55ffa7e2",
    ),
}

AUDIOCPP_RELEASE_TAG = "v0.9.2"
WHISPER_CPP_RELEASE_TAG = "b5454"


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
    cancel_check: Callable[[], bool] | None = None,
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

    ``cancel_check`` is polled before every chunk and raises
    :class:`AiModelDownloadCancelled`. The part file is always removed on
    failure or cancel, so a retry starts clean.
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
                    if cancel_check is not None and cancel_check():
                        raise AiModelDownloadCancelled(f"Đã huỷ tải {target.name}.")
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


def _scaled_progress(
    progress_cb: Callable[[float], None] | None, start: float, span: float
) -> Callable[[float], None] | None:
    """Map one download's 0..1 progress onto ``[start, start + span]``."""
    if progress_cb is None:
        return None
    return lambda p: progress_cb(min(1.0, start + float(p) * span))


def _runtime_space_needed(archive_bytes: int) -> int:
    """Free space a runtime pack needs: the archive, its extracted tree, headroom."""
    return archive_bytes * 3 + INSTALL_SPACE_HEADROOM if archive_bytes > 0 else 0


def _archive_suffix(url: str, archive: str = "") -> str:
    """Suffix of a staged archive: the manifest's ``archive`` kind, else the URL's."""
    lowered = url.lower()
    kind = archive or next(
        (k for k in ("zip", "tar.xz", "tar.gz") if lowered.endswith(f".{k}")), "tar.gz"
    )
    return f".{kind}"


def _host_arch() -> str:
    """``x64`` / ``arm64`` for the running interpreter, "" for anything else."""
    machine = platform.machine().lower()
    if machine in ("x86_64", "amd64", "x64"):
        return "x64"
    if machine in ("arm64", "aarch64"):
        return "arm64"
    return ""


def _promote_runtime_dir(staging: Path, rt_dir: Path, backup: Path, label: str) -> None:
    """Swap a probed ``staging`` tree in as ``rt_dir``, restoring the old one on failure."""
    if rt_dir.exists():
        os.replace(rt_dir, backup)
    try:
        os.replace(staging, rt_dir)
    except OSError as exc:
        if backup.exists():
            with contextlib.suppress(OSError):
                os.replace(backup, rt_dir)
        raise AiModelInstallError(f"Không thể kích hoạt runtime {label} mới: {exc}") from exc
    if backup.exists():
        safe_remove(backup)


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
    if cand.is_file() and cand.suffix.lower() in (".bin", ".gguf"):
        return cand
    if data_dir is None:
        return None
    base = asr_models_dir(data_dir)
    spec = WHISPER_GGUF_MODELS.get(raw)
    if spec is not None:
        p = base / spec.filename
        if p.is_file() and _managed_model_size_ok(p, spec):
            return p
        return None
    by_filename = {s.filename: s for s in WHISPER_GGUF_MODELS.values()}
    for name in (raw, f"ggml-{raw}.bin", f"{raw}.bin"):
        p = base / name
        if not p.is_file() or p.suffix.lower() not in (".bin", ".gguf"):
            continue
        matched_spec = by_filename.get(p.name)
        if matched_spec is not None:
            if _managed_model_size_ok(p, matched_spec):
                return p
        elif p.stat().st_size > 0:
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
    spec = WHISPER_GGUF_MODELS.get(key)
    if spec is None:
        raise AiModelInstallError(f"Mô hình ASR {key!r} không có trong danh mục của ứng dụng.")
    url = url_override.strip() or spec.url
    target = asr_models_dir(data_dir) / spec.filename
    return _stream_download(
        url,
        target,
        progress_cb=progress_cb,
        expected_size=0 if url_override.strip() else spec.size_bytes,
        expected_sha256="" if url_override.strip() else spec.sha256,
        min_free_bytes=0 if url_override.strip() else spec.size_bytes + INSTALL_SPACE_HEADROOM,
    )


def remove_asr_model(model_id: str, data_dir: Path) -> bool:
    """Remove an installed Whisper model, refusing paths outside ``models/whisper``."""
    path = resolve_asr_model_path(model_id, data_dir)
    if path is None:
        return False
    base = asr_models_dir(data_dir).resolve()
    try:
        resolved = path.resolve()
    except OSError:
        return False
    if not resolved.is_relative_to(base) or resolved.suffix.lower() not in (".bin", ".gguf"):
        return False
    with contextlib.suppress(OSError):
        resolved.unlink(missing_ok=True)
        return not resolved.exists()
    return False


# ── Optional audio models (Silero VAD, speaker embedding) ────────────────────

WHISPER_VAD_MODEL_ID = "whisper-vad-silero-v6.2.0"
SILERO_VAD_MODEL_ID = "silero-vad"
SPEAKER_EMBEDDING_MODEL_ID = "speaker-embedding"


@dataclass(frozen=True)
class AudioModelSpec:
    """One optional model file the ASR/diarization pipeline uses when installed.

    ``subdir`` is the folder under ``<data_dir>/models``: the whisper.cpp VAD
    model sits beside the Whisper weights (``whisper-cli -vm``). ``license`` is
    the SPDX id of the weights — spec D2 admits only Apache-2.0 or MIT — and
    ``source`` the upstream project. URLs are pinned to an immutable revision.
    """

    id: str
    label: str
    filename: str
    url: str
    size_bytes: int
    sha256: str
    license: str
    source: str
    subdir: str
    format: str  # "ggml" | "onnx"


AUDIO_MODELS: dict[str, AudioModelSpec] = {
    WHISPER_VAD_MODEL_ID: AudioModelSpec(
        id=WHISPER_VAD_MODEL_ID,
        label="Silero VAD v6.2.0 cho whisper.cpp (0,8 MB — bỏ qua đoạn không có lời)",
        filename="ggml-silero-v6.2.0.bin",
        url=(
            "https://huggingface.co/ggml-org/whisper-vad/resolve/"
            "9ffd54a1e1ee413ddf265af9913beaf518d1639b/ggml-silero-v6.2.0.bin"
        ),
        size_bytes=885_098,
        sha256="2aa269b785eeb53a82983a20501ddf7c1d9c48e33ab63a41391ac6c9f7fb6987",
        license="MIT",
        source="https://github.com/snakers4/silero-vad",
        subdir="whisper",
        format="ggml",
    ),
    SILERO_VAD_MODEL_ID: AudioModelSpec(
        id=SILERO_VAD_MODEL_ID,
        label="Silero VAD v6.2 ONNX (2,2 MB — ranh giới lời nói chính xác hơn)",
        filename="silero_vad.onnx",
        url=(
            "https://raw.githubusercontent.com/snakers4/silero-vad/"
            "be95df9152c0d7618fa1edfeb296fc3dae32376f/src/silero_vad/data/silero_vad.onnx"
        ),
        size_bytes=2_327_524,
        sha256="1a153a22f4509e292a94e67d6f9b85e8deb25b4988682b7e174c65279d8788e3",
        license="MIT",
        source="https://github.com/snakers4/silero-vad",
        subdir="vad",
        format="onnx",
    ),
    SPEAKER_EMBEDDING_MODEL_ID: AudioModelSpec(
        id=SPEAKER_EMBEDDING_MODEL_ID,
        label="3D-Speaker CAM++ ONNX (28 MB — phân biệt người nói chính xác hơn)",
        filename="3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx",
        url=(
            "https://huggingface.co/csukuangfj/speaker-embedding-models/resolve/"
            "0743f301363dec56491a490f6d6cbc9d67f9a3bf/"
            "3dspeaker_speech_campplus_sv_en_voxceleb_16k.onnx"
        ),
        size_bytes=29_596_978,
        sha256="357a834f702b80161e5b981182c038e18553c1f2ca752ed6cec2052365d4129b",
        license="Apache-2.0",
        source="https://www.modelscope.cn/models/iic/speech_campplus_sv_en_voxceleb_16k",
        subdir="speaker",
        format="onnx",
    ),
}


@dataclass(frozen=True)
class AudioModelStatus:
    """Install state of one :data:`AUDIO_MODELS` entry."""

    spec: AudioModelSpec
    path: Path | None

    @property
    def installed(self) -> bool:
        return self.path is not None


def audio_model_catalog() -> tuple[AudioModelSpec, ...]:
    """The optional audio models, in display order."""
    return tuple(AUDIO_MODELS.values())


def _audio_model_spec(model_id: str) -> AudioModelSpec:
    spec = AUDIO_MODELS.get((model_id or "").strip())
    if spec is None:
        raise AiModelInstallError(f"Mô hình {model_id!r} không có trong danh mục của ứng dụng.")
    return spec


def _audio_model_target(spec: AudioModelSpec, data_dir: Path) -> Path:
    return Path(data_dir) / "models" / spec.subdir / spec.filename


def audio_model_path(model_id: str, data_dir: Path | None) -> Path | None:
    """The installed file of ``model_id``; None when missing, unknown or truncated.

    Like the GGUF catalog, a size mismatch reads as "not installed" (the
    SHA-256 was verified when the file was downloaded).
    """
    spec = AUDIO_MODELS.get((model_id or "").strip())
    if spec is None or data_dir is None:
        return None
    path = _audio_model_target(spec, data_dir)
    if path.is_file() and _managed_model_size_ok(path, spec):
        return path
    return None


def audio_model_status(model_id: str, data_dir: Path | None) -> AudioModelStatus:
    spec = _audio_model_spec(model_id)
    return AudioModelStatus(spec=spec, path=audio_model_path(spec.id, data_dir))


def download_audio_model(
    model_id: str,
    data_dir: Path,
    *,
    progress_cb: Callable[[float], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> Path:
    """Download one optional audio model, verified against its size and SHA-256."""
    spec = _audio_model_spec(model_id)
    return _stream_download(
        spec.url,
        _audio_model_target(spec, data_dir),
        progress_cb=progress_cb,
        expected_size=spec.size_bytes,
        expected_sha256=spec.sha256,
        min_free_bytes=spec.size_bytes + INSTALL_SPACE_HEADROOM,
        cancel_check=cancel_check,
    )


def remove_audio_model(model_id: str, data_dir: Path) -> bool:
    """Remove an installed optional model, refusing anything outside its model dir.

    The guard resolves against ``<data_dir>/models``, so a model file — or a
    model subdir — symlinked out of the app's data is never followed.
    """
    spec = AUDIO_MODELS.get((model_id or "").strip())
    if spec is None:
        return False
    target = _audio_model_target(spec, data_dir)
    if not target.is_file():
        return False
    try:
        resolved = target.resolve()
        base = (Path(data_dir) / "models").resolve() / spec.subdir
    except OSError:
        return False
    if not resolved.is_relative_to(base) or resolved.suffix != target.suffix:
        return False
    with contextlib.suppress(OSError):
        resolved.unlink(missing_ok=True)
        return not resolved.exists()
    return False


# ── Media runtime packs (project-published whisper-cli / ffmpeg) ─────────────

#: Rendered by ``scripts/render_media_runtime_manifests.py`` from locked packs;
#: listed as frozen data in ``packaging/vienetts-app.spec``.
MEDIA_RUNTIME_MANIFEST_PATH = Path(__file__).with_name("media_runtime_manifests.json")
MEDIA_RUNTIME_SCHEMA = 1
WHISPER_CPP_RUNTIME = "whisper_cpp"
FFMPEG_RUNTIME = "ffmpeg"
WHISPER_CPP_CELLS = ("macos-arm64-metal", "macos-x64-cpu")
FFMPEG_CELLS = ("windows-x64", "linux-x64", "linux-arm64", "macos-arm64", "macos-x64")
MEDIA_PACK_ARCHIVES = ("zip", "tar.gz", "tar.xz")
#: Direct-serving artifact hosts (gh-pages ``packs/`` is the publish target).
MEDIA_PACK_DOWNLOAD_HOSTS = frozenset(
    {"nguyensitrung.github.io", "github.com", "objects.githubusercontent.com"}
)
_MEDIA_RUNTIME_CELLS: dict[str, tuple[str, ...]] = {
    WHISPER_CPP_RUNTIME: WHISPER_CPP_CELLS,
    FFMPEG_RUNTIME: FFMPEG_CELLS,
}
_SHA256_HEX = re.compile(r"[0-9a-f]{64}")


@dataclass(frozen=True)
class MediaRuntimeCell:
    """One platform cell of a managed runtime pack.

    Only a ``published`` cell carries a download recipe (``url`` + ``size_bytes``
    + ``sha256``); an unpublished one is a truthful "not available yet".
    """

    runtime: str
    cell: str
    published: bool
    url: str
    size_bytes: int
    sha256: str
    archive: str
    license: str
    source: str
    version: str = ""


@dataclass(frozen=True)
class MediaRuntimeManifest:
    runtimes: Mapping[str, Mapping[str, MediaRuntimeCell]]

    def cells(self, runtime: str) -> Mapping[str, MediaRuntimeCell]:
        return self.runtimes.get(runtime, {})

    def cell(self, runtime: str, cell: str) -> MediaRuntimeCell | None:
        return self.cells(runtime).get(cell)


def _parse_media_cell(runtime: str, cell_id: str, raw: object) -> MediaRuntimeCell:
    where = f"{runtime}/{cell_id}"
    if not isinstance(raw, Mapping):
        raise ValueError(f"{where}: a cell must be an object")
    published, size = raw.get("published"), raw.get("size")
    text = {k: raw.get(k) for k in ("url", "sha256", "archive", "license", "source")}
    version = raw.get("version", "")
    if not isinstance(published, bool):
        raise ValueError(f"{where}: published must be a boolean")
    if isinstance(size, bool) or not isinstance(size, int) or size < 0:
        raise ValueError(f"{where}: size must be a non-negative integer")
    if not all(isinstance(v, str) for v in (*text.values(), version)):
        raise ValueError(f"{where}: url/sha256/archive/license/source/version must be strings")
    url, sha256, archive = text["url"], text["sha256"], text["archive"]
    if archive not in MEDIA_PACK_ARCHIVES:
        raise ValueError(f"{where}: unsupported archive {archive!r}")
    if not text["license"].strip() or not text["source"].strip():
        raise ValueError(f"{where}: license and source are required")
    if published:
        check_download_url(url, allowed_hosts=MEDIA_PACK_DOWNLOAD_HOSTS)
        if not url.endswith(f".{archive}"):
            raise ValueError(f"{where}: the URL is not a .{archive} pack: {url}")
        if size <= 0 or not _SHA256_HEX.fullmatch(sha256):
            raise ValueError(f"{where}: a published cell needs its size and sha256")
    elif url:
        raise ValueError(f"{where}: an unpublished cell must not carry a download URL")
    return MediaRuntimeCell(
        runtime=runtime,
        cell=cell_id,
        published=published,
        url=url,
        size_bytes=size,
        sha256=sha256,
        archive=archive,
        license=text["license"],
        source=text["source"],
        version=version,
    )


def parse_media_runtime_manifest(data: object) -> MediaRuntimeManifest:
    """Validate a ``{schema: 1, runtimes: {<runtime>: {cells: {...}}}}`` document.

    Any violation — unknown schema, runtime or cell, a published cell without a
    pinned HTTPS pack on an allowed host, an unpublished cell with a URL —
    raises ``ValueError``. Runtimes and cells may be absent (= unpublished).
    """
    if not isinstance(data, Mapping):
        raise ValueError("the media runtime manifest must be an object")
    schema = data.get("schema")
    if isinstance(schema, bool) or schema != MEDIA_RUNTIME_SCHEMA:
        raise ValueError(f"unsupported media runtime manifest schema: {schema!r}")
    runtimes = data.get("runtimes")
    if not isinstance(runtimes, Mapping):
        raise ValueError("the media runtime manifest has no runtimes object")
    parsed: dict[str, dict[str, MediaRuntimeCell]] = {}
    for runtime, body in runtimes.items():
        known = _MEDIA_RUNTIME_CELLS.get(runtime)
        if known is None:
            raise ValueError(f"unknown media runtime: {runtime!r}")
        cells = body.get("cells") if isinstance(body, Mapping) else None
        if not isinstance(cells, Mapping):
            raise ValueError(f"{runtime}: missing cells object")
        parsed[runtime] = {}
        for cell_id, raw in cells.items():
            if cell_id not in known:
                raise ValueError(f"{runtime}: unknown cell {cell_id!r}")
            parsed[runtime][cell_id] = _parse_media_cell(runtime, cell_id, raw)
    return MediaRuntimeManifest(parsed)


def load_media_runtime_manifest(path: Path | None = None) -> MediaRuntimeManifest:
    """The shipped manifest (or ``path``). Missing or invalid data publishes nothing."""
    source = MEDIA_RUNTIME_MANIFEST_PATH if path is None else Path(path)
    try:
        return parse_media_runtime_manifest(json.loads(source.read_text(encoding="utf-8")))
    except (OSError, UnicodeDecodeError, ValueError):
        return MediaRuntimeManifest({})


# ── audio.cpp Native Runtime (audiocpp_cli) ──────────────────────────────────


def audiocpp_runtime_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "runtimes" / "audiocpp"


def resolve_audiocpp_cli(data_dir: Path | None = None) -> Path | None:
    """Managed runtime first, then ``PATH``, then the well-known POSIX dirs."""
    if data_dir is not None:
        rt_dir = audiocpp_runtime_dir(data_dir)
        if rt_dir.is_dir():
            found = _locate_cli(rt_dir)
            if found is not None:
                return found
    return find_executable("audiocpp_cli")


def is_audiocpp_installed(data_dir: Path | None = None) -> bool:
    return resolve_audiocpp_cli(data_dir) is not None


@dataclass(frozen=True)
class ReleaseAsset:
    """One upstream release asset, pinned by size and SHA-256."""

    filename: str
    size_bytes: int = 0
    sha256: str = ""


@dataclass(frozen=True)
class AudiocppRuntimeVariant:
    """One downloadable ``audiocpp_cli`` build.

    The variant is picked from the ``diarization_device`` setting. Upstream
    ships CPU/Vulkan builds for Windows and Linux x64, Metal for macOS, CUDA
    CLI zips for Windows only and nothing for Linux arm64. ``companions`` are
    archives extracted into the same runtime dir: a Windows CUDA zip carries
    ``ggml-cuda.dll`` but not the cudart/cuBLAS DLLs it loads, which upstream
    publishes as a separate ``cudart`` zip.
    """

    id: str
    label: str
    filename: str
    size_bytes: int = 0
    sha256: str = ""
    companions: tuple[ReleaseAsset, ...] = ()


#: ``audiocpp_unavailable_reason()`` when upstream ships no build for the host.
AUDIOCPP_UNSUPPORTED = "unsupported"

#: Size and SHA-256 of every audio.cpp asset the app may download, from
#: ``gh release view v0.9.2 -R 0xShug0/audio.cpp --json assets``.
_AUDIOCPP_ASSET_PINS: dict[str, tuple[int, str]] = {
    "audio-v0.9.2-bin-windows-x64-cpu-portable.zip": (
        28_114_705,
        "4bea04239d753d2d0a294580ad4af2a0fa81b0de244a1b9146650c7f9fa4e84d",
    ),
    "audio-v0.9.2-bin-windows-x64-vulkan-portable.zip": (
        62_556_276,
        "4d11a82c560f7a1dc0934e6fef847d6e0b779cf79854aa7d90d8b28e4ad04d93",
    ),
    "audio-v0.9.2-bin-windows-x64-cuda12.4.zip": (
        463_532_839,
        "099bd43b74a5175fd9e03ac95c9fabe8b97718826d547890a03950483e81b168",
    ),
    "audio-v0.9.2-cudart-windows-x64-cuda12.4.zip": (
        607_273_675,
        "50e278d213daa38e782a10b501b764c5c0738381898dacee7243101b890eeb0d",
    ),
    "audio-v0.9.2-bin-windows-x64-cuda13.3.zip": (
        275_142_402,
        "ddf7474656e4c47a1bc5b5a53a268709d968d87132367d646baae5a77c34391f",
    ),
    "audio-v0.9.2-cudart-windows-x64-cuda13.3.zip": (
        575_457_454,
        "f3bb616e43f4b8aec4be7f2c2b323aec381a1993bac382401a011c121c126e59",
    ),
    "audio-v0.9.2-bin-macos-arm64-metal.tar.gz": (
        31_972_675,
        "827239e1d5824f8294cce461661dc7f20f933869bd333dfc60ba3c089f29faab",
    ),
    "audio-v0.9.2-bin-macos-x64-metal.tar.gz": (
        33_862_576,
        "b199add8ab8d53f81769367b9869a021eae838fa6104c9b05a012ec7899774d8",
    ),
    "audio-v0.9.2-bin-ubuntu-x64-cpu-portable.tar.gz": (
        59_857_917,
        "72bfb07f46bb8f4cc0217a604619dbb78cfc388dfa4e639feb412ca7e2410980",
    ),
    "audio-v0.9.2-bin-ubuntu-x64-vulkan-portable.tar.gz": (
        73_616_441,
        "37294a0d9d52383475a47df95dc725c83faee0dbfae00088f1509ed4d7f3619d",
    ),
}

#: Windows CUDA builds and the oldest driver CUDA version each runs on. The
#: cudart companion bundles the CUDA runtime, so minor-version compatibility
#: applies (the reasoning behind ``detector.REQUIRED_CUDA``).
_AUDIOCPP_CUDA_BUILDS: tuple[tuple[str, str, tuple[int, int]], ...] = (
    ("cuda12.4", "NVIDIA CUDA 12.4 (GPU NVIDIA — tải ~1,1 GB)", (12, 0)),
    ("cuda13.3", "NVIDIA CUDA 13.3 (driver NVIDIA R580+ — tải ~850 MB)", (13, 0)),
)


def _audiocpp_asset(kind: str, platform_id: str) -> ReleaseAsset:
    ext = ".zip" if platform_id.startswith("windows-") else ".tar.gz"
    filename = f"audio-{AUDIOCPP_RELEASE_TAG}-{kind}-{platform_id}{ext}"
    size, sha256 = _AUDIOCPP_ASSET_PINS.get(filename, (0, ""))
    return ReleaseAsset(filename, size, sha256)


def _audiocpp_variant(
    variant_id: str, label: str, platform_id: str, *, cudart: str = ""
) -> AudiocppRuntimeVariant:
    asset = _audiocpp_asset("bin", platform_id)
    return AudiocppRuntimeVariant(
        variant_id,
        label,
        asset.filename,
        asset.size_bytes,
        asset.sha256,
        (_audiocpp_asset("cudart", cudart),) if cudart else (),
    )


@functools.cache
def _nvidia_driver_cuda_version() -> str | None:
    """The NVIDIA driver's maximum CUDA version, or None without a driver.

    Reuses the hardware detector's probe (NVML, then ``nvidia-smi``) once per
    process: the variant list feeds QML property reads on the GUI thread.
    """
    from vienetts_app.core.detector import probe_cuda_driver

    probe = probe_cuda_driver()
    return probe.cuda_version if probe.available else None


def _cuda_at_least(version: str | None, required: tuple[int, int]) -> bool:
    try:
        parts = tuple(int(p) for p in (version or "").split(".")[:2])
    except ValueError:
        return False
    return bool(parts) and parts >= required


def available_audiocpp_variants() -> dict[str, AudiocppRuntimeVariant]:
    if sys.platform == "win32":
        variants = {
            "cpu": _audiocpp_variant("cpu", "CPU (tương thích rộng)", "windows-x64-cpu-portable"),
            "vulkan": _audiocpp_variant(
                "vulkan",
                "Vulkan GPU (nhanh hơn trên GPU rời/tích hợp)",
                "windows-x64-vulkan-portable",
            ),
        }
        driver = _nvidia_driver_cuda_version()
        for variant_id, label, floor in _AUDIOCPP_CUDA_BUILDS:
            if _cuda_at_least(driver, floor):
                cell = f"windows-x64-{variant_id}"
                variants[variant_id] = _audiocpp_variant(variant_id, label, cell, cudart=cell)
        return variants
    if sys.platform == "darwin":
        arch = "arm64" if _host_arch() == "arm64" else "x64"
        return {
            "metal": _audiocpp_variant(
                "metal", "Metal GPU (Apple Silicon/Intel)", f"macos-{arch}-metal"
            )
        }
    if _host_arch() != "x64":
        # No Linux arm64 build upstream; the x64 one would only fail to exec.
        return {}
    return {
        "cpu": _audiocpp_variant("cpu", "CPU (tương thích rộng)", "ubuntu-x64-cpu-portable"),
        "vulkan": _audiocpp_variant(
            "vulkan",
            "Vulkan GPU (nhanh hơn trên GPU rời/tích hợp)",
            "ubuntu-x64-vulkan-portable",
        ),
    }


def audiocpp_unavailable_reason() -> str:
    """:data:`AUDIOCPP_UNSUPPORTED` when upstream ships no build for this host, else ""."""
    return "" if available_audiocpp_variants() else AUDIOCPP_UNSUPPORTED


def default_audiocpp_variant() -> str:
    return "metal" if sys.platform == "darwin" else "cpu"


def resolve_audiocpp_variant(requested: str = "") -> AudiocppRuntimeVariant:
    variants = available_audiocpp_variants()
    if not variants:
        raise AiModelInstallError(
            f"audio.cpp không phát hành bản dựng cho {sys.platform}/{platform.machine()} "
            f"({AUDIOCPP_UNSUPPORTED}) — hãy dùng phân tách người nói âm học (acoustic)."
        )
    key = (requested or "").strip().lower()
    if not key or key == "auto":
        key = default_audiocpp_variant()
    elif key == "cuda":
        # The newest CUDA build the installed driver can run.
        key = next(
            (vid for vid, _label, _floor in reversed(_AUDIOCPP_CUDA_BUILDS) if vid in variants),
            key,
        )
    variant = variants.get(key)
    if variant is None:
        offered = ", ".join(sorted(variants))
        raise AiModelInstallError(
            f"Không có bản runtime audio.cpp cho lựa chọn {requested!r} trên hệ điều hành này. "
            f"Các bản khả dụng: {offered}."
        )
    return variant


def _audiocpp_release_url(filename: str) -> str:
    base = f"https://github.com/0xShug0/audio.cpp/releases/download/{AUDIOCPP_RELEASE_TAG}"
    return f"{base}/{filename}"


def audiocpp_archive_url(variant: AudiocppRuntimeVariant) -> str:
    return _audiocpp_release_url(variant.filename)


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
    names = ("audiocpp_cli.exe", "audiocpp_cli") if os.name == "nt" else ("audiocpp_cli",)
    for exe in names:
        for cand in (root / "bin" / exe, root / exe):
            if cand.is_file():
                return cand
    for exe in names:
        found = next((p for p in root.rglob(exe) if p.is_file()), None)
        if found is not None:
            return found
    return None


def cli_command(cli: Path, *args: str) -> list[str]:
    """Build the argv for ``cli``, handling shebang test stubs on Windows."""
    if os.name == "nt":
        with contextlib.suppress(OSError):
            with cli.open("rb") as fh:
                first_line = fh.readline(256)
            if first_line.startswith(b"#!") and b"python" in first_line.lower():
                py_exe = getattr(sys, "_base_executable", None) or sys.executable
                return [py_exe, str(cli), *args]
    return [str(cli), *args]


def _probe_runtime(cli: Path, runtime_label: str = "audio.cpp") -> None:
    """Run ``<cli> --help`` with ``cwd=cli.parent`` so a broken archive is rejected.

    A partial extraction, a missing shared library, or a GPU build on a machine
    without the GPU loader all produce a binary that exists but cannot run.
    Running in ``cli.parent`` lets ``RUNPATH=$ORIGIN`` and ``ggml`` backend
    discovery locate sibling shared libraries.
    """
    try:
        proc = run_cli(cli_command(cli, "--help"), cwd=cli.parent, timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AiModelInstallError(
            f"Runtime {runtime_label} không chạy được trên máy này ({exc}). "
            "Hãy thử bản CPU nếu đang dùng bản GPU."
        ) from exc
    if proc.returncode not in (0, 1):
        detail = (proc.stderr or proc.stdout or "").strip()
        raise AiModelInstallError(
            f"Runtime {runtime_label} tải về nhưng không khởi động được"
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
    non-runnable archive therefore never destroys a working install. Catalog
    archives (and their companions) are verified against their pinned size
    and SHA-256; a ``url_override`` artifact is fetched alone and unverified.
    """
    runtime_variant = resolve_audiocpp_variant(variant)
    override = url_override.strip()
    if override:
        downloads = [(override, 0, "")]
    else:
        downloads = [
            (_audiocpp_release_url(a.filename), a.size_bytes, a.sha256)
            for a in (runtime_variant, *runtime_variant.companions)
        ]
    total_bytes = sum(size for _url, size, _sha in downloads)
    rt_dir = audiocpp_runtime_dir(data_dir)
    root = rt_dir.parent
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".audiocpp_staging_{uuid.uuid4().hex}"
    backup = rt_dir.with_name(f".audiocpp_old_{uuid.uuid4().hex}")
    staging.mkdir(parents=True, exist_ok=False)
    try:
        start = 0.0
        for index, (url, size, sha256) in enumerate(downloads):
            span = size / total_bytes if total_bytes else 1.0 / len(downloads)
            archive_path = staging / f"runtime{index}{_archive_suffix(url)}"
            _stream_download(
                url,
                archive_path,
                progress_cb=_scaled_progress(progress_cb, start, span),
                expected_size=size,
                expected_sha256=sha256,
                min_free_bytes=_runtime_space_needed(size),
            )
            _extract_archive(archive_path, staging)
            with contextlib.suppress(OSError):
                archive_path.unlink(missing_ok=True)
            start += span
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
        _promote_runtime_dir(staging, rt_dir, backup, "audio.cpp")
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


# ── whisper.cpp Native Runtime (whisper-cli) ─────────────────────────────────


def whisper_runtime_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "runtimes" / "whisper"


def _locate_whisper_cli(root: Path) -> Path | None:
    names = (
        ("whisper-cli.exe", "main.exe", "whisper-cli", "whisper-cpp")
        if os.name == "nt"
        else ("whisper-cli", "whisper-cpp")
    )
    for name in names:
        for cand in (root / "bin" / name, root / "Release" / name, root / name):
            if cand.is_file():
                return cand
    for name in names:
        found = next((p for p in root.rglob(name) if p.is_file()), None)
        if found is not None:
            return found
    return None


def resolve_whisper_cpp_cli(data_dir: Path | None = None) -> Path | None:
    """Managed runtime first, then ``PATH``, then the well-known POSIX dirs.

    The well-known dirs are where Homebrew's ``whisper-cpp`` lands, which a
    Finder-launched macOS app does not have on its ``PATH``.
    """
    if data_dir is not None:
        rt_dir = whisper_runtime_dir(data_dir)
        if rt_dir.is_dir():
            found = _locate_whisper_cli(rt_dir)
            if found is not None:
                return found
    names = (
        ("whisper-cli.exe", "main.exe", "whisper-cli", "whisper-cpp")
        if os.name == "nt"
        else ("whisper-cli", "whisper-cpp")
    )
    for name in names:
        found = find_executable(name)
        if found is not None:
            return found
    return None


def is_whisper_cpp_installed(data_dir: Path | None = None) -> bool:
    return resolve_whisper_cpp_cli(data_dir) is not None


@dataclass(frozen=True)
class WhisperCppRuntimeVariant:
    """One downloadable ``whisper.cpp`` CLI build.

    Windows/Linux builds are upstream release assets (``filename``). Upstream
    ships no macOS CLI, so macOS builds are project-published packs named by
    their media-runtime manifest ``cell`` and pinned by ``url``/``size_bytes``/
    ``sha256``/``archive``. A cell that is not published yet has no URL and
    carries ``unavailable_reason`` (Homebrew guidance) instead.
    """

    id: str
    label: str
    filename: str
    cell: str = ""
    url: str = ""
    size_bytes: int = 0
    sha256: str = ""
    archive: str = ""
    unavailable_reason: str = ""


WHISPER_CPP_HOMEBREW_HINT = (
    "Bản whisper-cli dựng sẵn cho macOS chưa được phát hành — hãy cài `whisper-cpp` "
    "bằng Homebrew (`brew install whisper-cpp`); ứng dụng sẽ tự tìm thấy whisper-cli."
)


def _macos_whisper_cpp_variant(
    variant_id: str, label: str, cell_id: str
) -> WhisperCppRuntimeVariant:
    cell = load_media_runtime_manifest().cell(WHISPER_CPP_RUNTIME, cell_id)
    if cell is None or not cell.published:
        return WhisperCppRuntimeVariant(
            variant_id, label, "", cell=cell_id, unavailable_reason=WHISPER_CPP_HOMEBREW_HINT
        )
    return WhisperCppRuntimeVariant(
        variant_id,
        label,
        cell.url.rsplit("/", 1)[-1],
        cell=cell_id,
        url=cell.url,
        size_bytes=cell.size_bytes,
        sha256=cell.sha256,
        archive=cell.archive,
    )


def available_whisper_cpp_variants() -> dict[str, WhisperCppRuntimeVariant]:
    machine = platform.machine().lower()
    is_arm = "arm" in machine or "aarch64" in machine
    if sys.platform == "darwin":
        # Never an upstream ``ubuntu`` tarball: that binary cannot exec here.
        if _host_arch() == "arm64":
            # One Metal build per arch; it also serves ``cpu`` (``-ng``).
            cell = "macos-arm64-metal"
            return {
                "cpu": _macos_whisper_cpp_variant("cpu", "CPU (bản dựng Metal, tắt GPU)", cell),
                "metal": _macos_whisper_cpp_variant("metal", "Metal GPU (Apple Silicon)", cell),
            }
        return {"cpu": _macos_whisper_cpp_variant("cpu", "CPU (Mac Intel)", "macos-x64-cpu")}
    if sys.platform == "win32":
        if is_arm:
            return {
                "cpu": WhisperCppRuntimeVariant(
                    "cpu",
                    "CPU ARM64 (tương thích rộng)",
                    "whisper-bin-win-cpu-arm64.zip",
                ),
                "cuda": WhisperCppRuntimeVariant(
                    "cuda",
                    "NVIDIA CUDA (GPU NVIDIA)",
                    "whisper-bin-win-cuda-13.4-arm64.zip",
                ),
            }
        return {
            "cpu": WhisperCppRuntimeVariant(
                "cpu",
                "CPU (tương thích rộng)",
                "whisper-bin-x64.zip",
            ),
            "cuda": WhisperCppRuntimeVariant(
                "cuda",
                "NVIDIA CUDA 12.4 (GPU NVIDIA)",
                "whisper-bin-win-cuda-12.4.0-x64.zip",
            ),
        }
    arch = "arm64" if is_arm else "x64"
    return {
        "cpu": WhisperCppRuntimeVariant(
            "cpu",
            "CPU (tương thích rộng)",
            f"whisper-bin-ubuntu-{arch}.tar.gz",
        ),
    }


def default_whisper_cpp_variant() -> str:
    return "metal" if sys.platform == "darwin" and _host_arch() == "arm64" else "cpu"


def resolve_whisper_cpp_variant(requested: str = "") -> WhisperCppRuntimeVariant:
    variants = available_whisper_cpp_variants()
    key = (requested or "").strip().lower()
    if not key or key in ("auto", "metal"):
        key = default_whisper_cpp_variant()
    variant = variants.get(key)
    if variant is None:
        offered = ", ".join(sorted(variants))
        raise AiModelInstallError(
            f"Không có bản runtime whisper.cpp cho lựa chọn {requested!r} trên hệ điều hành này. "
            f"Các bản khả dụng: {offered}."
        )
    return variant


def whisper_cpp_archive_url(variant: WhisperCppRuntimeVariant) -> str:
    """The pack URL: the manifest cell's, else the upstream release asset ("" if none)."""
    if variant.url or variant.cell:
        return variant.url
    base = f"https://github.com/ggml-org/whisper.cpp/releases/download/{WHISPER_CPP_RELEASE_TAG}"
    return f"{base}/{variant.filename}"


def installed_whisper_cpp_variant(data_dir: Path) -> str:
    """Variant id recorded by the last successful install, or ''."""
    try:
        meta_path = whisper_runtime_dir(data_dir) / RUNTIME_META_FILENAME
        meta = json.loads(meta_path.read_text("utf-8"))
    except (OSError, ValueError):
        return ""
    variant = meta.get("variant") if isinstance(meta, dict) else None
    return variant if isinstance(variant, str) else ""


def download_whisper_cpp_runtime(
    data_dir: Path,
    *,
    progress_cb: Callable[[float], None] | None = None,
    url_override: str = "",
    variant: str = "",
) -> Path:
    """Download, extract, validate and atomically install ``whisper-cli``.

    An unpublished macOS cell raises its Homebrew guidance before anything is
    fetched; a published one is verified against its pinned size and SHA-256.
    """
    runtime_variant = resolve_whisper_cpp_variant(variant)
    override = url_override.strip()
    if not override and runtime_variant.unavailable_reason:
        raise AiModelInstallError(runtime_variant.unavailable_reason)
    url = override or whisper_cpp_archive_url(runtime_variant)
    rt_dir = whisper_runtime_dir(data_dir)
    root = rt_dir.parent
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".whisper_staging_{uuid.uuid4().hex}"
    backup = rt_dir.with_name(f".whisper_old_{uuid.uuid4().hex}")
    staging.mkdir(parents=True, exist_ok=False)
    archive_path = (
        staging / f"runtime{_archive_suffix(url, '' if override else runtime_variant.archive)}"
    )
    size = 0 if override else runtime_variant.size_bytes
    try:
        _stream_download(
            url,
            archive_path,
            progress_cb=progress_cb,
            expected_size=size,
            expected_sha256="" if override else runtime_variant.sha256,
            min_free_bytes=_runtime_space_needed(size),
        )
        _extract_archive(archive_path, staging)
        with contextlib.suppress(OSError):
            archive_path.unlink(missing_ok=True)
        cli = _locate_whisper_cli(staging)
        if cli is None:
            raise AiModelInstallError("Không tìm thấy thực thi whisper-cli trong gói tải về.")
        if os.name != "nt":
            with contextlib.suppress(OSError):
                cli.chmod(cli.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        _probe_runtime(cli, "whisper.cpp")
        meta = {"variant": runtime_variant.id, "tag": WHISPER_CPP_RELEASE_TAG}
        if runtime_variant.cell:
            meta["cell"] = runtime_variant.cell
        (staging / RUNTIME_META_FILENAME).write_text(json.dumps(meta), encoding="utf-8")
        _promote_runtime_dir(staging, rt_dir, backup, "whisper.cpp")
    finally:
        with contextlib.suppress(OSError):
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    installed = resolve_whisper_cpp_cli(data_dir)
    if installed is None:
        raise AiModelInstallError("Không tìm thấy thực thi whisper-cli sau khi cài đặt.")
    return installed


def remove_whisper_cpp_runtime(data_dir: Path) -> bool:
    rt_dir = whisper_runtime_dir(data_dir)
    if not rt_dir.is_dir():
        return False
    safe_remove(rt_dir)
    return not rt_dir.exists()


# ── Managed ffmpeg runtime (ffmpeg + ffprobe) ────────────────────────────────

#: What dubbing needs from ffmpeg: H.264/AAC and VP9 encoders for the MP4 and
#: WebM exports, and libass's ``subtitles`` filter for burn-in.
FFMPEG_REQUIRED_ENCODERS = ("libx264", "aac", "libvpx-vp9")
FFMPEG_REQUIRED_FILTERS = ("subtitles",)


@dataclass(frozen=True)
class FfmpegRuntimeStatus:
    """What the Settings card needs to show about ffmpeg.

    ``ffmpeg``/``ffprobe`` are what ``video_dub`` runs (managed runtime → PATH
    → well-known dirs) and ``managed`` says whether the app-installed pack is
    present. ``downloadable`` is whether this host's ``cell`` is published;
    when it is not, ``note`` is the install guidance to show instead.
    ``version`` is the managed pack's ffmpeg version ("" without one).
    """

    cell: str
    downloadable: bool
    managed: bool
    ffmpeg: Path | None
    ffprobe: Path | None
    version: str
    note: str


def ffmpeg_runtime_dir(data_dir: Path) -> Path:
    return Path(data_dir) / "runtimes" / "ffmpeg"


def ffmpeg_runtime_bin_dir(data_dir: Path) -> Path:
    """Where the managed ``ffmpeg``/``ffprobe`` live (``video_dub`` looks here first)."""
    return ffmpeg_runtime_dir(data_dir) / "bin"


def ffmpeg_host_cell() -> str:
    """The ffmpeg manifest cell of this host, or "" when no pack can exist."""
    if sys.platform == "win32":
        os_key = "windows"
    elif sys.platform == "darwin":
        os_key = "macos"
    elif sys.platform.startswith("linux"):
        os_key = "linux"
    else:
        return ""
    cell = f"{os_key}-{_host_arch()}"
    return cell if cell in FFMPEG_CELLS else ""


def _ffmpeg_install_hint() -> str:
    if sys.platform == "darwin":
        return "hãy cài ffmpeg bằng Homebrew (`brew install ffmpeg`)"
    if sys.platform == "win32":
        return "hãy cài ffmpeg (ví dụ `winget install Gyan.FFmpeg`) rồi mở lại ứng dụng"
    return "hãy cài ffmpeg bằng trình quản lý gói (ví dụ `sudo apt install ffmpeg`)"


def _published_ffmpeg_cell() -> tuple[MediaRuntimeCell | None, str]:
    """This host's published pack, or ``(None, guidance)``."""
    cell_id = ffmpeg_host_cell()
    if not cell_id:
        return None, (
            f"Chưa có gói ffmpeg dựng sẵn cho {sys.platform}/{platform.machine()} — "
            f"{_ffmpeg_install_hint()}."
        )
    cell = load_media_runtime_manifest().cell(FFMPEG_RUNTIME, cell_id)
    if cell is None or not cell.published:
        return None, (
            f"Gói ffmpeg dựng sẵn cho {cell_id} chưa được phát hành — {_ffmpeg_install_hint()}."
        )
    return cell, ""


def _locate_ffmpeg_pair(bin_dir: Path) -> tuple[Path, Path] | None:
    found: list[Path] = []
    for tool in ("ffmpeg", "ffprobe"):
        names = (f"{tool}.exe", tool) if os.name == "nt" else (tool,)
        path = next((bin_dir / n for n in names if (bin_dir / n).is_file()), None)
        if path is None:
            return None
        found.append(path)
    return found[0], found[1]


def _installed_ffmpeg_version(data_dir: Path) -> str:
    try:
        meta = json.loads((ffmpeg_runtime_dir(data_dir) / RUNTIME_META_FILENAME).read_text("utf-8"))
    except (OSError, ValueError):
        return ""
    version = meta.get("ffmpeg_version") if isinstance(meta, dict) else None
    return version if isinstance(version, str) else ""


def ffmpeg_runtime_status(data_dir: Path | None) -> FfmpegRuntimeStatus:
    cell, note = _published_ffmpeg_cell()
    managed_dirs = [] if data_dir is None else [ffmpeg_runtime_bin_dir(data_dir)]
    managed = (
        data_dir is not None and _locate_ffmpeg_pair(ffmpeg_runtime_bin_dir(data_dir)) is not None
    )
    return FfmpegRuntimeStatus(
        cell=ffmpeg_host_cell(),
        downloadable=cell is not None,
        managed=managed,
        ffmpeg=find_executable("ffmpeg", managed_dirs=managed_dirs),
        ffprobe=find_executable("ffprobe", managed_dirs=managed_dirs),
        version=_installed_ffmpeg_version(data_dir) if managed and data_dir else "",
        note=note,
    )


def _run_ffmpeg_probe(exe: Path, *args: str) -> str:
    try:
        proc = run_cli(cli_command(exe, *args), cwd=exe.parent, timeout=PROBE_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError) as exc:
        raise AiModelInstallError(f"Gói ffmpeg không chạy được trên máy này ({exc}).") from exc
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise AiModelInstallError(
            f"Gói ffmpeg tải về không chạy được ({exe.name} {' '.join(args)})"
            + (f": {detail[:300]}" if detail else ".")
        )
    return proc.stdout or ""


def _listed_names(listing: str) -> set[str]:
    """The name column of ``ffmpeg -encoders`` / ``-filters`` (legend lines skipped)."""
    names: set[str] = set()
    for line in listing.splitlines():
        parts = line.split()
        if len(parts) >= 2 and parts[1] != "=":
            names.add(parts[1])
    return names


def _probe_ffmpeg_pack(ffmpeg: Path, ffprobe: Path) -> str:
    """Run a staged pack and return its version; reject a pack dubbing cannot use."""
    match = re.search(r"ffmpeg version (\S+)", _run_ffmpeg_probe(ffmpeg, "-version"))
    if match is None:
        raise AiModelInstallError("Gói tải về không chứa ffmpeg hợp lệ.")
    if "ffprobe version" not in _run_ffmpeg_probe(ffprobe, "-version"):
        raise AiModelInstallError("Gói tải về không chứa ffprobe hợp lệ.")
    encoders = _listed_names(_run_ffmpeg_probe(ffmpeg, "-hide_banner", "-encoders"))
    filters = _listed_names(_run_ffmpeg_probe(ffmpeg, "-hide_banner", "-filters"))
    missing = [f"bộ mã hoá {name}" for name in FFMPEG_REQUIRED_ENCODERS if name not in encoders]
    missing += [
        f"bộ lọc {name} (libass)" for name in FFMPEG_REQUIRED_FILTERS if name not in filters
    ]
    if missing:
        raise AiModelInstallError(
            "Gói ffmpeg tải về thiếu thành phần cần cho lồng tiếng và ghép phụ đề: "
            + ", ".join(missing)
            + "."
        )
    return match.group(1)


def install_ffmpeg_runtime(
    data_dir: Path,
    *,
    progress_cb: Callable[[float], None] | None = None,
    cancel_check: Callable[[], bool] | None = None,
) -> Path:
    """Download, verify, probe and atomically install the managed ffmpeg pack.

    The pack (``bin/ffmpeg`` + ``bin/ffprobe``, ``.exe`` on Windows, plus
    ``LICENSE``/``SOURCE.txt``) is this host's published manifest cell, pinned
    by size and SHA-256. It is staged beside the live runtime and must pass
    the ``-version``/``-encoders``/``-filters`` probe before the swap, so any
    failure leaves the previous runtime in place. Returns the managed ffmpeg.
    """
    cell, reason = _published_ffmpeg_cell()
    if cell is None:
        raise AiModelInstallError(reason)
    rt_dir = ffmpeg_runtime_dir(data_dir)
    root = rt_dir.parent
    root.mkdir(parents=True, exist_ok=True)
    staging = root / f".ffmpeg_staging_{uuid.uuid4().hex}"
    backup = root / f".ffmpeg_old_{uuid.uuid4().hex}"
    staging.mkdir(parents=True, exist_ok=False)
    archive_path = staging / f"pack{_archive_suffix(cell.url, cell.archive)}"
    try:
        _stream_download(
            cell.url,
            archive_path,
            progress_cb=progress_cb,
            expected_size=cell.size_bytes,
            expected_sha256=cell.sha256,
            min_free_bytes=_runtime_space_needed(cell.size_bytes),
            cancel_check=cancel_check,
        )
        _extract_archive(archive_path, staging)
        with contextlib.suppress(OSError):
            archive_path.unlink(missing_ok=True)
        pair = _locate_ffmpeg_pair(staging / "bin")
        if pair is None:
            raise AiModelInstallError("Gói ffmpeg tải về thiếu bin/ffmpeg hoặc bin/ffprobe.")
        if os.name != "nt":
            for exe in pair:
                with contextlib.suppress(OSError):
                    exe.chmod(exe.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        version = _probe_ffmpeg_pack(*pair)
        if cancel_check is not None and cancel_check():
            raise AiModelDownloadCancelled("Đã huỷ cài đặt ffmpeg.")
        meta = {
            "runtime": FFMPEG_RUNTIME,
            "cell": cell.cell,
            "version": cell.version,
            "ffmpeg_version": version,
            "sha256": cell.sha256,
        }
        (staging / RUNTIME_META_FILENAME).write_text(json.dumps(meta), encoding="utf-8")
        _promote_runtime_dir(staging, rt_dir, backup, "ffmpeg")
    finally:
        with contextlib.suppress(OSError):
            if staging.exists():
                shutil.rmtree(staging, ignore_errors=True)

    installed = _locate_ffmpeg_pair(ffmpeg_runtime_bin_dir(data_dir))
    if installed is None:
        raise AiModelInstallError("Không tìm thấy ffmpeg sau khi cài đặt.")
    return installed[0]


def remove_ffmpeg_runtime(data_dir: Path) -> bool:
    """Remove the managed ffmpeg pack, refusing a runtime dir that leads elsewhere.

    A symlinked (or junctioned) ``runtimes/ffmpeg`` is never followed: the
    resolved dir must stay inside the resolved ``runtimes`` dir.
    """
    rt_dir = ffmpeg_runtime_dir(data_dir)
    if rt_dir.is_symlink() or not rt_dir.is_dir():
        return False
    try:
        resolved = rt_dir.resolve()
        root = rt_dir.parent.resolve()
    except OSError:
        return False
    if resolved == root or not resolved.is_relative_to(root):
        return False
    safe_remove(rt_dir)
    return not rt_dir.exists()
