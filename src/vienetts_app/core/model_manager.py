"""Staging-only managed model installer (Phase 1 Task 2).

Filesystem-only inspection; network + full-file verification run through the
injected downloader seam. No Qt, no top-level Hub import.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import shutil
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from vienetts_app.core.official_model_manifest import (
    OFFICIAL_MODEL_MANIFEST,
    OfficialModelManifest,
)

logger = logging.getLogger(__name__)

ModelInstallState = Literal["unavailable", "downloading", "validating", "ready", "failed", "custom"]


@dataclass(frozen=True)
class ManagedModelLocation:
    root: Path
    backbone_dir: Path
    onnx_dir: Path
    codec_dir: Path
    format_version: str
    revision: str


@dataclass(frozen=True)
class ModelStatus:
    state: ModelInstallState
    installed_bytes: int = 0
    required_bytes: int = 0
    progress: float = 0.0
    error: str = ""
    location: ManagedModelLocation | None = None


def _sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


class ModelManager:
    """Owns a versioned, app-data-resident official model install."""

    def __init__(
        self,
        root: Path,
        manifest: OfficialModelManifest = OFFICIAL_MODEL_MANIFEST,
        downloader: Callable[..., Path] | None = None,
        disk_usage: Callable[[Path], object] = shutil.disk_usage,
    ) -> None:
        self.root = Path(root)
        self._manifest = manifest
        self._downloader = downloader
        self._disk_usage = disk_usage

    @property
    def manifest(self) -> OfficialModelManifest:
        return self._manifest

    @property
    def model_dir(self) -> Path:
        """Active install dir the app scans (offline-pack destination)."""
        return self._active_dir()

    def _active_dir(self) -> Path:
        return self.root / self._manifest.format_version

    def _staging_dir(self) -> Path:
        return self.root / ".staging" / self._manifest.format_version

    def _previous_dir(self) -> Path:
        return self.root / (self._manifest.format_version + ".previous")

    def _repo_id(self, repo_key: str) -> str:
        if repo_key == "backbone":
            return self._manifest.backbone_repo
        return self._manifest.codec_repo

    def _revision(self, repo_key: str) -> str:
        if repo_key == "backbone":
            return self._manifest.backbone_revision
        return self._manifest.codec_revision

    def _location_for(self, active: Path) -> ManagedModelLocation:
        backbone_dir = active / "backbone"
        return ManagedModelLocation(
            root=active,
            backbone_dir=backbone_dir,
            onnx_dir=backbone_dir / "onnx_int8",
            codec_dir=active / "codec",
            format_version=self._manifest.format_version,
            revision=self._manifest.backbone_revision,
        )

    def _staging_path(self, repo_key: str, relative_path: str) -> Path:
        repo_dir = "backbone" if repo_key == "backbone" else "codec"
        return self._staging_dir() / repo_dir / relative_path

    def _active_path(self, repo_key: str, relative_path: str) -> Path:
        repo_dir = "backbone" if repo_key == "backbone" else "codec"
        return self._active_dir() / repo_dir / relative_path

    def _file_validates(self, path: Path, size_bytes: int, sha256: str) -> bool:
        try:
            if not path.is_file():
                return False
            if path.stat().st_size != size_bytes:
                return False
        except OSError:
            return False
        try:
            return _sha256_of(path) == sha256
        except OSError:
            return False

    def inspect(self) -> ModelStatus:
        manifest = self._manifest
        required = manifest.total_bytes
        active = self._active_dir()
        if not active.is_dir():
            return ModelStatus("unavailable", 0, required, 0.0, "", None)
        install_path = active / "install.json"
        if not install_path.is_file():
            return ModelStatus("unavailable", 0, required, 0.0, "", None)
        try:
            meta = json.loads(install_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            return ModelStatus("failed", 0, required, 0.0, "install metadata is corrupt", None)
        if not isinstance(meta, dict):
            return ModelStatus("failed", 0, required, 0.0, "install metadata is corrupt", None)
        if meta.get("format") != manifest.format_version:
            if meta.get("format") == "custom":
                return ModelStatus("custom", 0, required, 0.0, "", None)
            return ModelStatus("failed", 0, required, 0.0, "install format mismatch", None)
        verified = 0
        verified_bytes = 0
        for item in manifest.files:
            if self._file_validates(
                self._active_path(item.repo_key, item.relative_path),
                item.size_bytes,
                item.sha256,
            ):
                verified += 1
                verified_bytes += item.size_bytes
        if verified == len(manifest.files):
            return ModelStatus(
                "ready",
                manifest.total_bytes,
                required,
                1.0,
                "",
                self._location_for(active),
            )
        return ModelStatus(
            "failed",
            verified_bytes,
            required,
            verified / len(manifest.files) if manifest.files else 0.0,
            "active install is incomplete or corrupt",
            None,
        )

    def _clean_staging_caches(self) -> None:
        staging = self._staging_dir()
        for repo_key in ("backbone", "codec"):
            cache_dir = staging / repo_key / ".cache"
            if cache_dir.exists():
                shutil.rmtree(cache_dir, ignore_errors=True)

    def cancel_staging(self) -> None:
        shutil.rmtree(self._staging_dir(), ignore_errors=True)

    def _clean_invalid_staging_files(self) -> None:
        self._clean_staging_caches()
        for item in self._manifest.files:
            path = self._staging_path(item.repo_key, item.relative_path)
            if path.exists() and not self._file_validates(path, item.size_bytes, item.sha256):
                try:
                    path.unlink()
                except OSError:
                    logger.warning("could not clean invalid staging file")

    def _write_install_json(self, staging: Path) -> None:
        manifest = self._manifest
        payload = {
            "format": manifest.format_version,
            "backbone_repo": manifest.backbone_repo,
            "backbone_revision": manifest.backbone_revision,
            "codec_repo": manifest.codec_repo,
            "codec_revision": manifest.codec_revision,
            "files": {
                f"{item.repo_key}/{item.relative_path}": {
                    "size_bytes": item.size_bytes,
                    "sha256": item.sha256,
                }
                for item in manifest.files
            },
            "validated_at": time.time(),
        }
        (staging / "install.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")

    def _promote_staging(self) -> ModelStatus:
        manifest = self._manifest
        required = manifest.total_bytes
        staging = self._staging_dir()
        active = self._active_dir()
        previous = self._previous_dir()
        if previous.exists():
            shutil.rmtree(previous, ignore_errors=True)
        had_previous = False
        if active.exists():
            if self.inspect().state == "ready":
                try:
                    os.replace(active, previous)
                except OSError as exc:
                    return ModelStatus("failed", 0, required, 0.0, f"promotion failed: {exc}", None)
                had_previous = True
            else:
                shutil.rmtree(active, ignore_errors=True)
        try:
            os.replace(staging, active)
        except OSError as exc:
            if had_previous and not active.exists():
                with contextlib.suppress(OSError):
                    os.replace(previous, active)
            return ModelStatus("failed", 0, required, 0.0, f"promotion failed: {exc}", None)
        status = self.inspect()
        if status.state == "ready":
            if had_previous:
                shutil.rmtree(previous, ignore_errors=True)
            return status
        if had_previous:
            shutil.rmtree(active, ignore_errors=True)
            with contextlib.suppress(OSError):
                os.replace(previous, active)
        return ModelStatus("failed", 0, required, 0.0, status.error or "promotion failed", None)

    def _default_downloader(self) -> Callable[..., Path]:
        from huggingface_hub import hf_hub_download  # lazy: never on inspect path

        return hf_hub_download  # type: ignore[return-value]

    def install(
        self,
        cancelled: Callable[[], bool] = lambda: False,
        on_progress: Callable[[ModelStatus], None] = lambda _status: None,
    ) -> ModelStatus:
        manifest = self._manifest
        required = manifest.total_bytes
        self.root.mkdir(parents=True, exist_ok=True)

        def failed(message: str) -> ModelStatus:
            return ModelStatus("failed", 0, required, 0.0, message, None)

        try:
            usage = self._disk_usage(self.root)
            free = int(usage.free)  # type: ignore[attr-defined]
        except (OSError, TypeError, ValueError, AttributeError):
            free = manifest.required_free_bytes
        if free < manifest.required_free_bytes:
            return failed(f"insufficient disk space: need {manifest.required_free_bytes} bytes")

        staging = self._staging_dir()
        (staging / "backbone").mkdir(parents=True, exist_ok=True)
        (staging / "codec").mkdir(parents=True, exist_ok=True)
        downloader = (
            self._downloader if self._downloader is not None else self._default_downloader()
        )

        verified = 0
        verified_bytes = 0
        total = len(manifest.files)

        def progress_status(state: ModelInstallState) -> ModelStatus:
            return ModelStatus(
                state,
                verified_bytes,
                required,
                verified / total if total else 1.0,
                "",
                None,
            )

        for item in manifest.files:
            if cancelled():
                self._clean_invalid_staging_files()
                return ModelStatus(
                    "unavailable",
                    verified_bytes,
                    required,
                    verified / total if total else 0.0,
                    "",
                    None,
                )
            target = self._staging_path(item.repo_key, item.relative_path)
            if self._file_validates(target, item.size_bytes, item.sha256):
                verified += 1
                verified_bytes += item.size_bytes
                on_progress(progress_status("downloading"))
                continue
            staging_repo_root = self._staging_dir() / (
                "backbone" if item.repo_key == "backbone" else "codec"
            )
            staging_repo_root.mkdir(parents=True, exist_ok=True)
            target.parent.mkdir(parents=True, exist_ok=True)
            local_dir_str = str(staging_repo_root)
            if os.name == "nt":
                resolved_local = str(staging_repo_root.resolve())
                if not resolved_local.startswith("\\\\?\\") and resolved_local[1:3] == ":\\":
                    local_dir_str = "\\\\?\\" + resolved_local
            try:
                result = downloader(
                    repo_id=self._repo_id(item.repo_key),
                    filename=item.relative_path,
                    revision=self._revision(item.repo_key),
                    local_dir=local_dir_str,
                    local_dir_use_symlinks=False,
                )
            except Exception as exc:
                self._clean_invalid_staging_files()
                return failed(f"download failed: {exc}")
            raw_candidate = Path(str(result)) if result is not None else target
            candidate = raw_candidate
            if os.name == "nt" and str(raw_candidate).startswith("\\\\?\\"):
                stripped = Path(str(raw_candidate)[4:])
                if stripped.is_file() and not candidate.is_file():
                    candidate = stripped
            if not candidate.is_file():
                candidate = target
            if candidate != target:
                with contextlib.suppress(OSError):
                    if candidate.resolve() == target.resolve():
                        candidate = target
            # landed elsewhere, co-locate it for the layout validator.
            if candidate != target and candidate.is_file():
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    if not target.is_file():
                        shutil.copyfile(candidate, target)
                        candidate = target
                except OSError:
                    pass
            else:
                candidate = target
            if not self._file_validates(candidate, item.size_bytes, item.sha256):
                with contextlib.suppress(OSError):
                    candidate.unlink(missing_ok=True)
                if candidate != target:
                    with contextlib.suppress(OSError):
                        target.unlink(missing_ok=True)
                return failed(f"checksum mismatch for {item.repo_key}/{item.relative_path}")
            # Ensure the validated bytes live at the canonical staging path.
            if candidate != target:
                try:
                    target.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(candidate, target)
                except OSError as exc:
                    return failed(f"could not stage verified file: {exc}")
            verified += 1
            verified_bytes += item.size_bytes
            on_progress(progress_status("downloading"))

        if cancelled():
            self._clean_invalid_staging_files()
            return ModelStatus(
                "unavailable",
                verified_bytes,
                required,
                verified / total if total else 0.0,
                "",
                None,
            )
        self._clean_staging_caches()
        on_progress(ModelStatus("validating", verified_bytes, required, 1.0, "", None))
        try:
            self._write_install_json(staging)
        except OSError as exc:
            return failed(f"could not write install metadata: {exc}")
        return self._promote_staging()

    def install_offline_pack(self, source: Path) -> ModelStatus:
        manifest = self._manifest
        required = manifest.total_bytes

        def failed(message: str) -> ModelStatus:
            return ModelStatus("failed", 0, required, 0.0, message, None)

        src = Path(source)
        if not src.is_dir():
            return failed("offline pack must be a directory")
        allowed = {(item.repo_key, item.relative_path) for item in manifest.files}
        # Reject any file outside the allowlist; never create install.json.
        for path in sorted(src.rglob("*")):
            if not path.is_file() or path.name == "install.json":
                continue
            try:
                rel = path.relative_to(src)
            except ValueError:
                return failed("offline pack contains an unexpected path")
            parts = rel.parts
            if len(parts) < 2 or parts[0] not in ("backbone", "codec"):
                return failed(f"offline pack contains unexpected path: {rel}")
            if (parts[0], "/".join(parts[1:])) not in allowed:
                return failed(f"offline pack contains unexpected path: {rel}")
        for item in manifest.files:
            repo_dir = "backbone" if item.repo_key == "backbone" else "codec"
            candidate = src / repo_dir / item.relative_path
            if not self._file_validates(candidate, item.size_bytes, item.sha256):
                return failed(
                    f"offline pack is missing or corrupt: {repo_dir}/{item.relative_path}"
                )
        self.root.mkdir(parents=True, exist_ok=True)
        try:
            usage = self._disk_usage(self.root)
            free = int(usage.free)  # type: ignore[attr-defined]
        except (OSError, TypeError, ValueError, AttributeError):
            free = manifest.required_free_bytes
        if free < manifest.required_free_bytes:
            return failed(f"insufficient disk space: need {manifest.required_free_bytes} bytes")
        staging = self._staging_dir()
        shutil.rmtree(staging, ignore_errors=True)
        try:
            for item in manifest.files:
                repo_dir = "backbone" if item.repo_key == "backbone" else "codec"
                dest = staging / repo_dir / item.relative_path
                dest.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src / repo_dir / item.relative_path, dest)
            self._write_install_json(staging)
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            return failed(f"could not stage offline pack: {exc}")
        return self._promote_staging()
