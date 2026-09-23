"""Managed Qwen3-TTS GGUF model installation (track qwen_gguf_engine_20260923).

One install root holds every GGUF variant side by side plus one shared tree:

    <root>/base-Q8_0/qwen-talker-0.6b-base-Q8_0.gguf      variant talker
    <root>/customvoice-Q8_0/…
    <root>/shared/qwen-tokenizer-12hz-Q8_0.gguf          codec shared per quant

Both profiles that quantize to Q8_0 pin the *same* tokenizer bytes, so it is
downloaded once and deleted only when no installed variant references it. The
install path is pure file plumbing — no torch, no PyTorch runtime, no native
loads — and a user-supplied source clone is only ever copied from, never
consumed by cleanup.
"""

from __future__ import annotations

import contextlib
import json
import os
import shutil
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from vienetts_app.core.managed_install import (
    DownloadCancelled,
    InstallPromotionError,
    file_matches,
    free_space_bytes,
    promoted_install,
    safe_remove,
)
from vienetts_app.core.qwen_gguf_model_manifest import (
    FORMAT_VERSION,
    MANIFEST,
    PROFILE_KEY_FOR,
    QwenGgufFile,
    QwenGgufModelManifest,
    metadata_matches,
)
from vienetts_app.core.qwen_variants import MODEL_FORMAT_GGUF, QwenVariant

SHARED_DIR_NAME = "shared"

#: Manifest profile key -> EngineId (inverse of PROFILE_KEY_FOR).
_ENGINE_ID_FOR = {profile: engine for engine, profile in PROFILE_KEY_FOR.items()}


@dataclass(frozen=True)
class QwenGgufModelLocation:
    root: Path
    talker_path: Path
    tokenizer_path: Path
    format_version: str
    variant_key: str
    model_identity: str
    revision: str


@dataclass(frozen=True)
class QwenGgufModelStatus:
    state: str
    variant_key: str = ""
    installed_bytes: int = 0
    required_bytes: int = 0
    progress: float = 0.0
    error: str = ""
    location: QwenGgufModelLocation | None = None

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    @property
    def talker_path(self) -> Path | None:
        return self.location.talker_path if self.location is not None else None

    @property
    def tokenizer_path(self) -> Path | None:
        return self.location.tokenizer_path if self.location is not None else None

    @property
    def model_identity(self) -> str:
        return self.location.model_identity if self.location is not None else ""


class ManagedQwenGgufModelError(RuntimeError):
    """The managed GGUF model install is unavailable, corrupt, or incomplete."""


class QwenGgufModelManager:
    """Owns one variant's install inside the shared GGUF model root.

    The downloader seam takes ``(url, target)`` — the pinned HF resolve URL
    and the staging path — so tests inject plain file copies while the
    default wraps ``hf_hub_download`` (resume, redirects, partials). Nothing
    here imports torch or the PyTorch runtime.
    """

    def __init__(
        self,
        root: Path,
        variant: QwenVariant,
        manifest: QwenGgufModelManifest | None = None,
        downloader: Callable[[str, Path], None] | None = None,
        disk_usage: Callable[[Path], object] = shutil.disk_usage,
    ) -> None:
        if variant.model_format != MODEL_FORMAT_GGUF:
            raise ManagedQwenGgufModelError(
                f"{variant.profile} is not a gguf variant — only GGUF model pairs install here"
            )
        resolved_manifest = manifest if manifest is not None else MANIFEST
        if resolved_manifest is None:
            raise ManagedQwenGgufModelError("no verified GGUF model manifest")
        profile_key = PROFILE_KEY_FOR.get(str(variant.profile), "")
        recipe = resolved_manifest.recipe_for(profile_key, variant.quantization)
        if recipe is None:
            raise ManagedQwenGgufModelError(
                f"no verified GGUF manifest entry for {profile_key}-{variant.quantization}"
            )
        self.root = Path(root)
        self.variant = variant
        self.recipe = recipe
        self.manifest = resolved_manifest
        self._downloader = downloader
        self._disk_usage = disk_usage

    # --- layout -------------------------------------------------------------

    def _active_dir(self) -> Path:
        return self.root / self.recipe.key

    def _shared_dir(self) -> Path:
        return self.root / SHARED_DIR_NAME

    def _staging_dir(self) -> Path:
        return self.root / ".staging" / self.recipe.key

    def _shared_staging_dir(self) -> Path:
        return self.root / ".staging" / SHARED_DIR_NAME

    def _previous_dir(self) -> Path:
        return self.root / f"{self.recipe.key}.previous"

    def _location(self, active: Path) -> QwenGgufModelLocation:
        return QwenGgufModelLocation(
            root=active,
            talker_path=active / self.recipe.talker.path,
            tokenizer_path=self._shared_dir() / self.recipe.tokenizer.path,
            format_version=FORMAT_VERSION,
            variant_key=self.recipe.key,
            model_identity=self.recipe.model_identity,
            revision=self.recipe.revision,
        )

    def _status(
        self,
        state: str,
        *,
        installed_bytes: int = 0,
        error: str = "",
        location: QwenGgufModelLocation | None = None,
    ) -> QwenGgufModelStatus:
        total = self.recipe.total_bytes
        return QwenGgufModelStatus(
            state=state,
            variant_key=self.recipe.key,
            installed_bytes=installed_bytes,
            required_bytes=self.recipe.required_free_bytes,
            progress=installed_bytes / total if total else 1.0,
            error=error,
            location=location,
        )

    def _metadata(self) -> dict[str, object]:
        return {
            "format": FORMAT_VERSION,
            "variant": self.recipe.key,
            "repo": self.recipe.repo,
            "revision": self.recipe.revision,
            "talker": self.recipe.talker.sha256,
            "tokenizer": self.recipe.tokenizer.sha256,
        }

    def _url_for(self, record: QwenGgufFile) -> str:
        return (
            f"https://huggingface.co/{self.recipe.repo}"
            f"/resolve/{self.recipe.revision}/{record.path}"
        )

    # --- status -------------------------------------------------------------

    def status(self) -> QwenGgufModelStatus:
        """Report ready only when both files verify against the lock.

        Digest checks only — the GGUF header was already parsed at install
        time, and status stays cheap enough for the GUI thread.
        """
        active = self._active_dir()
        if active.is_symlink():
            return self._status("failed", error="variant directory must not be a symlink")
        if not active.is_dir():
            return self._status("unavailable")
        install_path = active / "install.json"
        if not install_path.is_file():
            return self._status("failed", error="install metadata is missing")
        try:
            metadata = json.loads(install_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            return self._status("failed", error="install metadata is corrupt")
        if metadata != self._metadata():
            return self._status("failed", error="install metadata does not match the manifest")
        talker = active / self.recipe.talker.path
        if talker.is_symlink() or not file_matches(
            talker, self.recipe.talker.size_bytes, self.recipe.talker.sha256
        ):
            return self._status("failed", error="talker does not match the manifest")
        tokenizer = self._shared_dir() / self.recipe.tokenizer.path
        if tokenizer.is_symlink() or not file_matches(
            tokenizer, self.recipe.tokenizer.size_bytes, self.recipe.tokenizer.sha256
        ):
            return self._status("failed", error="tokenizer does not match the manifest")
        return self._status(
            "ready",
            installed_bytes=self.recipe.total_bytes,
            location=self._location(active),
        )

    inspect = status

    # --- staging ------------------------------------------------------------

    def _install_guard(self) -> QwenGgufModelStatus | None:
        try:
            self.root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            return self._status("failed", error=f"could not create model root: {exc}")
        free = free_space_bytes(self.root, self._disk_usage)
        if free is not None and free < self.recipe.required_free_bytes:
            return self._status(
                "failed",
                error=(
                    f"insufficient disk space: need "
                    f"{self.recipe.required_free_bytes} bytes, free {free}"
                ),
            )
        root = self.root.resolve()
        for directory in (self._staging_dir(), self._shared_staging_dir()):
            if directory.is_symlink():
                return self._status("failed", error="staging directory must not be a symlink")
            try:
                directory.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                return self._status("failed", error=f"staging setup failed: {exc}")
            if (
                directory.is_symlink()
                or not directory.is_dir()
                or not directory.resolve().is_relative_to(root)
            ):
                return self._status("failed", error="staging directory is unsafe")
        return None

    def _verified(self, record: QwenGgufFile, path: Path) -> bool:
        """Size+SHA and the declared GGUF metadata — both, never just bytes."""
        if path.is_symlink() or not file_matches(path, record.size_bytes, record.sha256):
            return False
        return not metadata_matches(record, path)

    def _default_download(self, url: str, target: Path) -> None:
        """HF downloads reuse the hub seam: resume, redirects, *.incomplete."""
        from huggingface_hub import hf_hub_download  # noqa: PLC0415 - lazy import

        hf_hub_download(
            repo_id=self.recipe.repo,
            filename=url.rsplit("/", 1)[-1],
            revision=self.recipe.revision,
            local_dir=str(target.parent),
            local_dir_use_symlinks=False,
        )

    def _download_into(
        self,
        record: QwenGgufFile,
        destination: Path,
        downloader: Callable[[str, Path], None],
    ) -> str:
        """Fetch one file into ``destination`` and verify it; "" on success."""
        destination.parent.mkdir(parents=True, exist_ok=True)
        try:
            downloader(self._url_for(record), destination)
        except DownloadCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - the seam reports anything
            return f"download failed: {exc}"
        if not file_matches(destination, record.size_bytes, record.sha256):
            return f"checksum mismatch: {record.path}"
        metadata_error = metadata_matches(record, destination)
        if metadata_error:
            with contextlib.suppress(OSError):
                destination.unlink(missing_ok=True)
            return metadata_error
        return ""

    def _stage_files(
        self,
        cancelled: Callable[[], bool],
        on_progress: Callable[[QwenGgufModelStatus], None],
    ) -> tuple[int, QwenGgufModelStatus | None]:
        """Stage the talker + shared tokenizer, reusing anything that verifies."""
        downloader = self._downloader if self._downloader is not None else self._default_download
        verified_bytes = 0
        plan = (
            (self.recipe.talker, self._staging_dir() / self.recipe.talker.path),
            (
                self.recipe.tokenizer,
                self._shared_staging_dir() / self.recipe.tokenizer.path,
            ),
        )
        for record, destination in plan:
            if cancelled():
                return verified_bytes, self._status("unavailable", installed_bytes=verified_bytes)
            is_shared = destination.parent == self._shared_staging_dir()
            if is_shared and file_matches(
                self._shared_dir() / record.path,
                record.size_bytes,
                record.sha256,
            ):
                # Already promoted for a sibling variant: one download serves
                # every profile sharing the quantization.
                verified_bytes += record.size_bytes
                on_progress(self._status("downloading", installed_bytes=verified_bytes))
                continue
            if not self._verified(record, destination):
                try:
                    error = self._download_into(record, destination, downloader)
                except DownloadCancelled:
                    return verified_bytes, self._status(
                        "unavailable", installed_bytes=verified_bytes
                    )
                if error:
                    return verified_bytes, self._status(
                        "failed", installed_bytes=verified_bytes, error=error
                    )
            verified_bytes += record.size_bytes
            on_progress(self._status("downloading", installed_bytes=verified_bytes))
        return verified_bytes, None

    # --- promote ------------------------------------------------------------

    def _promote_shared(self) -> str:
        record = self.recipe.tokenizer
        staged = self._shared_staging_dir() / record.path
        target = self._shared_dir() / record.path
        if file_matches(target, record.size_bytes, record.sha256):
            return ""
        if not self._verified(record, staged):
            return f"shared file is missing or corrupt: {record.path}"
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            os.replace(staged, target)
        except OSError as exc:
            return f"could not promote shared file {record.path}: {exc}"
        return ""

    def _promote_staging(self) -> QwenGgufModelStatus:
        staging = self._staging_dir()
        active = self._active_dir()
        previous = self._previous_dir()
        shared_error = self._promote_shared()
        if shared_error:
            return self._status("failed", error=shared_error)
        try:
            if not active.exists() and previous.exists():
                os.replace(previous, active)
            with promoted_install(staging, active, previous):
                status = self.status()
                if not status.ready:
                    raise InstallPromotionError(status.error or "promotion failed")
        except InstallPromotionError as exc:
            return self._status("failed", error=str(exc))
        except OSError as exc:
            return self._status("failed", error=f"promotion failed: {exc}")
        return status

    def _finish_staging(self, verified_bytes: int) -> QwenGgufModelStatus:
        try:
            (self._staging_dir() / "install.json").write_text(
                json.dumps(self._metadata(), sort_keys=True),
                encoding="utf-8",
            )
        except OSError as exc:
            return self._status(
                "failed",
                installed_bytes=verified_bytes,
                error=f"could not write metadata: {exc}",
            )
        return self._promote_staging()

    # --- public API ---------------------------------------------------------

    def install(
        self,
        cancelled: Callable[[], bool] = lambda: False,
        on_progress: Callable[[QwenGgufModelStatus], None] = lambda _status: None,
    ) -> QwenGgufModelStatus:
        """Download, verify (digest + GGUF metadata), and promote one variant."""
        existing = self.status()
        if existing.ready:
            return existing
        guard = self._install_guard()
        if guard is not None:
            return guard
        verified_bytes, failure = self._stage_files(cancelled, on_progress)
        if failure is not None:
            return failure
        if cancelled():
            return self._status("unavailable", installed_bytes=verified_bytes)
        on_progress(self._status("validating", installed_bytes=verified_bytes))
        return self._finish_staging(verified_bytes)

    def install_offline(
        self,
        source: Path,
        cancelled: Callable[[], bool] = lambda: False,
        on_progress: Callable[[QwenGgufModelStatus], None] = lambda _status: None,
    ) -> QwenGgufModelStatus:
        """Install from a directory laid out like the model root — copy only.

        The source is never modified or consumed: a user's clone of the GGUF
        repo stays outside managed cleanup. Only declared paths are read; an
        unexpected file rejects the pack instead of being silently ignored.
        """
        existing = self.status()
        if existing.ready:
            return existing
        src = Path(source)
        if src.is_symlink() or not src.is_dir():
            return self._status("failed", error=f"offline pack is not a directory: {src}")
        allowed = {
            f"{self.recipe.key}/{self.recipe.talker.path}",
            f"{self.recipe.key}/install.json",
            f"{SHARED_DIR_NAME}/{self.recipe.tokenizer.path}",
        }
        for path in sorted(src.rglob("*")):
            if not path.is_file():
                continue
            relative = path.relative_to(src).as_posix()
            if relative not in allowed:
                return self._status(
                    "failed",
                    error=f"offline pack contains unexpected path: {relative}",
                )
        pair = (
            (self.recipe.talker, src / self.recipe.key / self.recipe.talker.path),
            (
                self.recipe.tokenizer,
                src / SHARED_DIR_NAME / self.recipe.tokenizer.path,
            ),
        )
        for record, candidate in pair:
            if not candidate.is_file() or candidate.is_symlink():
                return self._status("failed", error=f"offline pack is missing {record.path}")
            if not self._verified(record, candidate):
                return self._status(
                    "failed",
                    error=(f"offline pack file does not match the manifest: {record.path}"),
                )
        if cancelled():
            return self._status("unavailable")
        guard = self._install_guard()
        if guard is not None:
            return guard
        try:
            for record, candidate in pair:
                destination = (
                    self._staging_dir() / record.path
                    if record is self.recipe.talker
                    else self._shared_staging_dir() / record.path
                )
                destination.parent.mkdir(parents=True, exist_ok=True)
                if not file_matches(destination, record.size_bytes, record.sha256):
                    shutil.copyfile(candidate, destination)
            (self._staging_dir() / "install.json").write_text(
                json.dumps(self._metadata(), sort_keys=True),
                encoding="utf-8",
            )
        except OSError as exc:
            safe_remove(self._staging_dir())
            return self._status("failed", error=f"could not stage offline pack: {exc}")
        on_progress(self._status("validating", installed_bytes=self.recipe.total_bytes))
        return self._promote_staging()

    def repair(
        self,
        cancelled: Callable[[], bool] = lambda: False,
        on_progress: Callable[[QwenGgufModelStatus], None] = lambda _status: None,
    ) -> QwenGgufModelStatus:
        """Re-install a failed variant, keeping every file that still verifies."""
        existing = self.status()
        if existing.ready:
            return existing
        for record, staged in (
            (self.recipe.talker, self._staging_dir() / self.recipe.talker.path),
            (
                self.recipe.tokenizer,
                self._shared_staging_dir() / self.recipe.tokenizer.path,
            ),
        ):
            if staged.exists() and not self._verified(record, staged):
                staged.unlink(missing_ok=True)
        return self.install(cancelled, on_progress)

    def remove(self, *, in_use: bool = False, drop_shared: bool = False) -> QwenGgufModelStatus:
        """Remove this variant; a shared codec survives while referenced.

        ``drop_shared`` removes only the tokenizer files no remaining install
        references — a codec another variant still needs is never deleted.
        """
        if in_use:
            return self._status("failed", error="model is in use; restart the app before removal")
        active = self._active_dir()
        if not active.exists():
            return self._status("unavailable")
        safe_remove(active)
        if drop_shared:
            for path in unreferenced_shared_files(self.root, self.manifest):
                safe_remove(path)
        return self._status("unavailable")

    def cancel_staging(self) -> None:
        """Discard this variant's staged files; shared staging is left alone."""
        safe_remove(self._staging_dir())


def _variant_references(root: Path, manifest: QwenGgufModelManifest, quantization: str) -> bool:
    """True while any installed variant of ``quantization`` needs the codec."""
    for key in manifest.talkers:
        if not key.endswith(f"-{quantization}"):
            continue
        install = root / key / "install.json"
        if not install.is_file():
            continue
        try:
            metadata = json.loads(install.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            continue
        recipe = manifest.recipe_for(key.rpartition("-")[0], quantization)
        if recipe is not None and metadata.get("tokenizer") == recipe.tokenizer.sha256:
            return True
    return False


def unreferenced_shared_files(
    root: Path, manifest: QwenGgufModelManifest | None = None
) -> tuple[Path, ...]:
    """Shared codec files no installed variant references."""
    manifest = manifest if manifest is not None else MANIFEST
    if manifest is None:
        return ()
    shared = Path(root) / SHARED_DIR_NAME
    stray: list[Path] = []
    for quantization, record in manifest.tokenizers.items():
        candidate = shared / record.path
        if candidate.exists() and not _variant_references(Path(root), manifest, quantization):
            stray.append(candidate)
    return tuple(stray)


def installed_variants(
    root: Path, manifest: QwenGgufModelManifest | None = None
) -> tuple[str, ...]:
    """Variant keys with a ready install under ``root``."""
    manifest = manifest if manifest is not None else MANIFEST
    if manifest is None:
        return ()
    from vienetts_app.core.qwen_variants import variant_for  # noqa: PLC0415

    ready: list[str] = []
    for key in manifest.talkers:
        profile_key, _, quant = key.rpartition("-")
        engine = _ENGINE_ID_FOR.get(profile_key)
        if engine is None:
            continue
        variant = variant_for(engine, model_format="gguf", quantization=quant)
        if QwenGgufModelManager(root, variant, manifest).status().ready:
            ready.append(key)
    return tuple(ready)
