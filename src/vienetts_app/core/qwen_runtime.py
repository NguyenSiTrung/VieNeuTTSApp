"""Optional Qwen runtime boundary (Phase 3 Task 1).

``qwen-tts`` + ``torch`` are OPTIONAL: the default VieNeu install stays
torch-free. Nothing here imports either package at module load — every
access goes through an injectable import hook (production passes nothing;
tests inject fakes), so ``import vienetts_app.core.qwen_runtime`` never
pays the torch import cost and never fails when torch is absent.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

QWEN_PACKAGE = "qwen-tts"
QWEN_IMPORT_NAME = "qwen_tts"
QWEN_CUSTOMVOICE_REPO = "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
QWEN_BASE_REPO = "Qwen/Qwen3-TTS-12Hz-0.6B-Base"

# Native output rate of the 0.6B checkpoints (artifact normalization
# upsamples to the 48 kHz app contract). Pinned from the official model
# cards + epic measurement; the Phase 5 real-model smoke re-verifies it
# against the runtime-reported `sr` and fails loudly on drift.
QWEN_NATIVE_RATE = 24000

# 0.6B needs ~4 GB VRAM on CUDA (official sizing); CPU fallback is
# functional but has NO real-time promise — say so instead of implying it.
QWEN_VRAM_NOTE = "the 0.6B models need ~4 GB of VRAM on CUDA"


class QwenRuntimeError(RuntimeError):
    """Qwen runtime/model unavailable; message names the fix."""


def _default_import(name: str) -> Any:
    import importlib

    return importlib.import_module(name)


def _missing_message(what: str) -> str:
    return (
        f"Qwen TTS needs the optional runtime ({what} is not installed). "
        f"Install it with `pip install vienetts-app[qwen]` "
        f"({QWEN_PACKAGE} plus PyTorch), then install a Qwen model pack. "
        f"CPU fallback works without CUDA but is slow — {QWEN_VRAM_NOTE} "
        "for real-time synthesis."
    )


def require_qwen(
    import_fn: Callable[[str], Any] | None = None,
) -> Any:
    """Import ``qwen_tts`` lazily or raise an actionable error."""
    import_fn = import_fn or _default_import
    try:
        return import_fn(QWEN_IMPORT_NAME)
    except ImportError as exc:
        raise QwenRuntimeError(_missing_message(QWEN_PACKAGE)) from exc


def describe_qwen_device(
    torch_import: Callable[[], Any] | None = None,
) -> tuple[str, str]:
    """Return ``(device, detail)`` without importing torch at module load.

    ``"cuda"`` only when torch imports AND reports CUDA available;
    otherwise ``"cpu"`` with the reason (missing torch vs no CUDA).
    """
    try:
        torch = torch_import() if torch_import is not None else _default_import("torch")
    except ImportError:
        return (
            "cpu",
            "torch is not installed — Qwen runs on CPU only after `pip install vienetts-app[qwen]`",
        )
    try:
        if torch.cuda.is_available():
            return "cuda", f"CUDA available (torch {torch.__version__})"
    except Exception:  # noqa: BLE001 - torch build quirks must degrade, not crash
        pass
    return "cpu", "CUDA not available — Qwen CPU fallback will be slow"


def _repo_snapshot_present(hub_dir: str | object, repo: str) -> bool:
    """True when the HF hub cache holds any snapshot of ``repo``."""
    from pathlib import Path  # noqa: PLC0415 - stdlib, deferred like the rest

    cached = Path(str(hub_dir)) / ("models--" + repo.replace("/", "--"))
    snapshots = cached / "snapshots"
    try:
        return snapshots.is_dir() and any(snapshots.iterdir())
    except OSError:
        return False


def _default_hub_dir() -> str:
    import os  # noqa: PLC0415 - env lookup only
    from pathlib import Path  # noqa: PLC0415 - stdlib, deferred like the rest

    root = os.environ.get("HF_HOME") or os.environ.get("HF_HUB_CACHE")
    if root:
        return str(Path(root) / "hub")
    return str(Path.home() / ".cache" / "huggingface" / "hub")


def qwen_install_status(
    *,
    hub_dir: str | object | None = None,
    import_fn: Callable[[str], Any] | None = None,
    torch_import: Callable[[], Any] | None = None,
) -> dict[str, object]:
    """Probe the optional Qwen pack: runtime, torch, device, cached models.

    Never raises for missing pieces — every absence is data the settings UI
    renders as install guidance. ``ready`` means a Qwen job can run (runtime
    + torch + at least one cached checkpoint).
    """
    try:
        (import_fn or _default_import)(QWEN_IMPORT_NAME)
        runtime = True
    except ImportError:
        runtime = False
    device, detail = describe_qwen_device(torch_import=torch_import)
    try:
        (torch_import or (lambda: _default_import("torch")))()
        torch_present = True
    except ImportError:
        torch_present = False
    hub = hub_dir if hub_dir is not None else _default_hub_dir()
    models = {
        "customvoice": _repo_snapshot_present(hub, QWEN_CUSTOMVOICE_REPO),
        "base": _repo_snapshot_present(hub, QWEN_BASE_REPO),
    }
    return {
        "runtime": runtime,
        "torch": torch_present,
        "device": device,
        "detail": detail,
        "models": models,
        "ready": bool(runtime and torch_present and any(models.values())),
    }
