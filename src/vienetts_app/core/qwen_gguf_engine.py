"""Parent-side adapter for the managed qwentts.cpp GGUF host (Task 4.2).

Same framed protocol and lifecycle as :class:`QwenEngine` — the transport,
reader threads, cancellation escalation, and reaping are inherited verbatim.
What differs is *which* child runs and *what* the load frame carries:

- the child is ``vienetts-qwen-gguf-host`` (no torch, no site-packages
  runtime), spawned with ``cwd`` inside the verified runtime pack because
  ggml discovers its backend modules relative to the process working
  directory (the Phase 1 probe established exe-dir + cwd as the search path);
- the load frame carries ``format="gguf"`` plus ``quantization``, the native
  device id (``cpu``/``cuda``/``metal`` — never PyTorch's ``mps``), and the
  verified ``runtimeDir``/``talkerPath``/``codecPath`` from the Phase 3
  installers — never the official ``modelDir``/``dtype``/``attention``
  fields.

Nothing here imports the native library: the binding lives in
``workers/qwen_gguf_abi.py`` and runs only inside the child.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path

from vienetts_app.core.qwen_engine import (
    RSS_RECYCLE_GROWTH_BYTES,
    QwenEngine,
    QwenEngineError,
    _profile_identity,
    host_environment,
    is_frozen,
)
from vienetts_app.core.qwen_protocol import Frame
from vienetts_app.core.qwen_variants import (
    DEFAULT_GGUF_QUANTIZATION,
    ENGINE_QWENTTS_CPP,
    GGUF_DEVICES,
    GGUF_QUANTIZATIONS,
    MODEL_FORMAT_GGUF,
)

GGUF_HOST_MODULE = "vienetts_app.workers.qwen_gguf_host"

#: The flag a frozen build re-dispatches ITSELF with to become the GGUF host —
#: the native analogue of ``qwen_engine.HOST_FLAG``.
GGUF_HOST_FLAG = "--qwen-gguf-host"


def gguf_host_command() -> list[str]:
    """The shell-free command that starts the GGUF host.

    A source checkout runs ``-m vienetts_app.workers.qwen_gguf_host``; a frozen
    build re-dispatches itself with :data:`GGUF_HOST_FLAG` (the pack carries no
    Python, so the host module always comes from the app, not the runtime).
    """
    if is_frozen():
        return [sys.executable, GGUF_HOST_FLAG]
    return [sys.executable, "-m", GGUF_HOST_MODULE]


class QwenGgufEngine(QwenEngine):
    """Owns one ``vienetts-qwen-gguf-host`` subprocess for one Qwen profile.

    ``runtime_dir`` is the verified pack directory — it holds ``libqwen`` and
    the ggml backend modules and doubles as the child's working directory.
    ``talker_path``/``codec_path`` are the verified GGUF pair from the managed
    model install; ``device`` and ``quantization`` use the native vocabulary
    (``cpu``/``cuda``/``metal``, ``Q8_0``/``Q4_K_M``) — app-level names like
    ``mps`` are translated by the caller, never accepted here.
    """

    engine_id = ENGINE_QWENTTS_CPP

    def __init__(
        self,
        profile: str,
        *,
        runtime_dir: Path | str | None,
        talker_path: Path | str,
        codec_path: Path | str,
        quantization: str = DEFAULT_GGUF_QUANTIZATION,
        device: str = "cpu",
        command: Sequence[str] | None = None,
        environment: Mapping[str, str] | None = None,
        handshake_timeout: float = 10.0,
        load_timeout: float = 300.0,
        frame_timeout: float = 60.0,
        cancel_timeout: float = 5.0,
        cancel_grace_timeout: float = 300.0,
        kill_timeout: float = 5.0,
        shutdown_timeout: float = 5.0,
        logger: logging.Logger | None = None,
        footprint: Callable[[int], int | None] | None = None,
        rss_growth_recycle_bytes: int = RSS_RECYCLE_GROWTH_BYTES,
    ) -> None:
        capabilities, profile_key = _profile_identity(profile)
        device = str(device)
        if device not in GGUF_DEVICES:
            raise QwenEngineError(
                f"unsupported device {device!r} for the GGUF runtime — the native host "
                f"speaks {', '.join(GGUF_DEVICES)} (the app maps mps to metal itself)"
            )
        quantization = str(quantization or DEFAULT_GGUF_QUANTIZATION)
        if quantization not in GGUF_QUANTIZATIONS:
            raise QwenEngineError(
                f"unknown GGUF quantization {quantization!r} — the managed models pin "
                f"exactly {', '.join(GGUF_QUANTIZATIONS)}"
            )
        if runtime_dir is None or not str(runtime_dir).strip():
            raise QwenEngineError(
                "the GGUF engine needs the verified runtime pack directory — "
                "install the managed qwentts.cpp runtime first"
            )
        if not str(talker_path).strip():
            raise QwenEngineError(
                "the GGUF engine needs the verified talker GGUF path — "
                "install the managed model first"
            )
        if not str(codec_path).strip():
            raise QwenEngineError(
                "the GGUF engine needs the verified codec GGUF path — "
                "install the managed model first"
            )
        self._init_transport(
            profile=profile,
            profile_key=profile_key,
            label=capabilities.label,
            device=device,
            runtime_dir=Path(str(runtime_dir)),
            command=command,
            environment=environment,
            engine_note=f"{device}/{quantization}",
            handshake_timeout=handshake_timeout,
            load_timeout=load_timeout,
            frame_timeout=frame_timeout,
            cancel_timeout=cancel_timeout,
            cancel_grace_timeout=cancel_grace_timeout,
            kill_timeout=kill_timeout,
            shutdown_timeout=shutdown_timeout,
            logger=logger,
            footprint=footprint,
            rss_growth_recycle_bytes=rss_growth_recycle_bytes,
        )
        self._talker_path = Path(str(talker_path))
        self._codec_path = Path(str(codec_path))
        self._quantization = quantization

    @property
    def quantization(self) -> str:
        return self._quantization

    # -- host-kind hooks ---------------------------------------------------- #

    def _host_command(self) -> list[str]:
        return gguf_host_command()

    def _host_environment(self) -> dict[str, str]:
        # The same sanitized offline environment; the pack is native-only (no
        # site-packages), so nothing of it goes on PYTHONPATH — the spawn cwd
        # is what carries backend discovery.
        return host_environment(None, self._environment)

    def _spawn_cwd(self) -> str | None:
        # ggml's backend_init searches the executable dir and the process cwd
        # for its backend modules — the pack dir is the only place they are
        # guaranteed to sit (Phase 1 probe).
        return str(self._runtime_dir) if self._runtime_dir is not None else None

    def _load_frame(self) -> Frame:
        return Frame(
            type="load",
            fields={
                "profile": self._profile_key,
                "format": MODEL_FORMAT_GGUF,
                "quantization": self._quantization,
                "device": self._device,
                "runtimeDir": str(self._runtime_dir),
                "talkerPath": str(self._talker_path),
                "codecPath": str(self._codec_path),
            },
        )


__all__ = [
    "GGUF_HOST_FLAG",
    "GGUF_HOST_MODULE",
    "QwenGgufEngine",
    "gguf_host_command",
]
