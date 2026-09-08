"""Qwen model install/readiness state (Phase 3 Task 3).

Separate from the VieNeu pinned-manifest installer: Qwen checkpoints are
full Hugging Face repos (safetensors + configs, ~1–2 GB per 0.6B profile),
resolved through the HF cache. ``snapshot_download(..., local_files_only=True)``
is the readiness probe (offline-safe); the same call without the flag
downloads with resume. Nothing here imports huggingface_hub at module load.

Cancellation is best-effort: honored before the blocking download starts
(the single HF call cannot be interrupted mid-flight); the result is still
usable on retry since the HF cache resumes.
"""

from __future__ import annotations

import shutil
import threading
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from vienetts_app.core.backends import (
    QWEN_BASE,
    QWEN_CUSTOMVOICE,
    EngineId,
    get_capabilities,
)
from vienetts_app.core.qwen_runtime import QWEN_BASE_REPO, QWEN_CUSTOMVOICE_REPO

QwenModelState = Literal["unknown", "missing", "downloading", "ready", "error", "cancelled"]

_ENGINE_REPOS = {QWEN_CUSTOMVOICE: QWEN_CUSTOMVOICE_REPO, QWEN_BASE: QWEN_BASE_REPO}


@dataclass(frozen=True)
class QwenModelStatus:
    """Install/readiness snapshot for one Qwen profile."""

    engine: str
    state: QwenModelState
    local_dir: str | None = None
    installed_bytes: int = 0
    required_bytes: int | None = None
    progress: float | None = None  # 0..1 while downloading; None = indeterminate
    error: str = ""


def _repo_for(engine: EngineId) -> str:
    caps = get_capabilities(engine)  # raises BackendCapabilityError on unknown engine
    if engine not in _ENGINE_REPOS:
        raise ValueError(f"{caps.label} is not a Qwen model profile")
    return _ENGINE_REPOS[engine]


QWEN_RUNTIME_COMMAND = 'pip install "vienetts-app[qwen]"'


def wizard_required_engines(need_cloning: bool) -> list[str]:
    """Engines the setup wizard must fetch for the Yes/No answers.

    CustomVoice covers multilingual TTS; Base adds reference-audio cloning.
    """
    if need_cloning:
        return [QWEN_CUSTOMVOICE, QWEN_BASE]
    return [QWEN_CUSTOMVOICE]


def wizard_fetch_command(engines: list[str]) -> str:
    """Copyable fetch command for exactly the needed checkpoints."""
    repos = [_ENGINE_REPOS[e] for e in engines if e in _ENGINE_REPOS]
    if not repos:
        return "python scripts/fetch_qwen_models.py"
    parts = " ".join(f'--repo "{repo}"' for repo in repos)
    return f"python scripts/fetch_qwen_models.py {parts}"


def _default_snapshot(*args: Any, **kwargs: Any) -> str:
    from huggingface_hub import snapshot_download  # noqa: PLC0415 - lazy, see module doc

    return str(snapshot_download(*args, **kwargs))


def _local_only_missing(exc: BaseException) -> bool:
    try:
        from huggingface_hub.utils import LocalEntryNotFoundError  # noqa: PLC0415 - lazy
    except ImportError:
        return isinstance(exc, FileNotFoundError)

    return isinstance(exc, (LocalEntryNotFoundError, FileNotFoundError))


def qwen_model_status(
    engine: EngineId,
    *,
    snapshot_fn: Callable[..., str] | None = None,
) -> QwenModelStatus:
    """Probe the HF cache: ``ready`` with the local dir, ``missing``, or ``error``."""
    repo = _repo_for(engine)
    snapshot_fn = snapshot_fn or _default_snapshot
    try:
        local_dir = snapshot_fn(repo, local_files_only=True)
    except Exception as exc:  # noqa: BLE001 - probe must degrade, not crash
        if _local_only_missing(exc):
            return QwenModelStatus(engine=engine, state="missing")
        return QwenModelStatus(engine=engine, state="error", error=str(exc) or repr(exc))
    return QwenModelStatus(engine=engine, state="ready", local_dir=str(local_dir))


class _ProgressTqdm:
    """tqdm duck type aggregating per-file bars into one progress callback."""

    def __init__(
        self,
        total: float | None = None,
        progress_cb: Callable[[int, int | None], None] | None = None,
        **kwargs: Any,
    ) -> None:
        del kwargs
        self._total = int(total or 0)
        self._done = 0
        self._cb = progress_cb

    def update(self, n: int = 1) -> None:
        self._done += int(n)
        if self._cb is not None:
            total = self._total or None
            self._cb(min(self._done, self._total) if total else self._done, total)

    def close(self) -> None:
        pass

    def __enter__(self) -> _ProgressTqdm:
        return self

    def __exit__(self, *exc: Any) -> bool:
        self.close()
        return False
        pass


_QWEN_TQDM_LOCK = threading.Lock()


def _tqdm_factory(
    progress_cb: Callable[[int, int | None], None] | None,
    tqdm_class: type | None,
) -> Callable[..., Any]:
    """Build the ``tqdm_class`` hook for ``snapshot_download``.

    huggingface_hub fans file downloads out via ``tqdm.contrib.concurrent``
    ``thread_map``, which calls ``get_lock``/``set_lock`` on the class itself
    and uses the bar as a context manager — before constructing anything. A
    bare closure dies with ``'function' object has no attribute 'get_lock'``
    (seen live 2026-09-08), so the factory carries that protocol.
    """
    bar_class = tqdm_class or _ProgressTqdm

    def make_bar(total: float | None = None, **kwargs: Any) -> Any:
        try:
            return bar_class(total=total, progress_cb=progress_cb, **kwargs)
        except TypeError:
            # Foreign tqdm classes (e.g. the real tqdm): adapt via subclass.
            class _Adapter(bar_class):  # type: ignore[valid-type, misc]
                def update(inner_self, n: int = 1) -> None:  # noqa: N805
                    super().update(n)
                    if progress_cb is not None:
                        total = getattr(inner_self, "total", None)
                        done = getattr(inner_self, "n", 0)
                        progress_cb(done, total)

            return _Adapter(total=total, **kwargs)

    def _get_lock() -> Any:
        return getattr(make_bar, "_lock", None) or _QWEN_TQDM_LOCK

    def _set_lock(lock: Any) -> None:
        make_bar._lock = lock  # type: ignore[attr-defined]

    make_bar.get_lock = _get_lock  # type: ignore[attr-defined]
    make_bar.set_lock = _set_lock  # type: ignore[attr-defined]
    return make_bar


def ensure_qwen_model(
    engine: EngineId,
    *,
    snapshot_fn: Callable[..., str] | None = None,
    tqdm_class: type | None = None,
    progress_cb: Callable[[int, int | None], None] | None = None,
    cancelled: Callable[[], bool] | None = None,
    size_probe: Callable[[str], int | None] | None = None,
    free_space_fn: Callable[[str], int] | None = None,
) -> QwenModelStatus:
    """Download a Qwen profile on demand; return its terminal status.

    Fast paths first: already cached → ``ready``; cancelled → ``cancelled``
    without touching the network. Disk-space preflight runs when a size
    probe yields a requirement (skipped offline/unknown — documented, not
    silent: the returned status keeps ``required_bytes=None``). Failures
    return ``error`` with the message; retry by calling again.
    """
    repo = _repo_for(engine)
    snapshot_fn = snapshot_fn or _default_snapshot
    if cancelled is not None and cancelled():
        return QwenModelStatus(engine=engine, state="cancelled")
    try:
        local_dir = snapshot_fn(repo, local_files_only=True)
        return QwenModelStatus(engine=engine, state="ready", local_dir=str(local_dir))
    except Exception as exc:  # noqa: BLE001 - absent cache is the normal path
        if not _local_only_missing(exc):
            return QwenModelStatus(engine=engine, state="error", error=str(exc) or repr(exc))
    required = size_probe(repo) if size_probe is not None else None
    if required is not None:
        free = (free_space_fn or _free_space)("<qwen-cache>")
        if free < required:
            return QwenModelStatus(
                engine=engine,
                state="error",
                required_bytes=required,
                error=(f"not enough disk space for {repo}: need {_gb(required)}, {_gb(free)} free"),
            )
    make_bar = _tqdm_factory(progress_cb, tqdm_class)

    try:
        local_dir = snapshot_fn(repo, tqdm_class=make_bar)
    except Exception as exc:  # noqa: BLE001 - network/disk failure → error state
        return QwenModelStatus(engine=engine, state="error", error=str(exc) or repr(exc))
    return QwenModelStatus(engine=engine, state="ready", local_dir=str(local_dir))


def _free_space(_path: str) -> int:
    from pathlib import Path  # noqa: PLC0415 - trivial stdlib, keep module head lean

    return shutil.disk_usage(Path.home()).free


def _gb(n: int) -> str:
    return f"{n / 1e9:.1f} GB"
