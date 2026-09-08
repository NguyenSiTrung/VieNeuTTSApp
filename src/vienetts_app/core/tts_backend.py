"""Backend-neutral streaming TTS contract (Phase 1 Task 3).

Every synthesis backend — VieNeu today, Qwen in Phase 3 — implements
:class:`TtsBackend`: chunked native-rate float32 mono synthesis plus the
worker-owned lifecycle (initialize → synthesize → close). The inference
worker keeps single-threaded ownership; only the object behind the
contract changes.

Create backends through :func:`create_backend`; new runtimes register
with :func:`register_backend` (Phase 3 registers the Qwen loaders).
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from typing import Any

import numpy as np

from vienetts_app.core.backends import (
    VIENEU,
    BackendCapabilityError,
    EngineId,
    get_capabilities,
)
from vienetts_app.core.engine import TTSEngine


class TtsBackend(ABC):
    """Streaming synthesis contract for one engine/profile."""

    engine_id: str
    native_sample_rate: int

    @abstractmethod
    def initialize(self) -> None:
        """Load the model; runs on the owning worker thread."""

    @property
    @abstractmethod
    def is_initialized(self) -> bool: ...

    @abstractmethod
    def synthesize_stream(
        self,
        text: str,
        *,
        voice: str | None = None,
        language: str | None = None,
        instruction: str | None = None,
        temperature: float | None = None,
    ) -> Iterator[np.ndarray]:
        """Yield float32 1-D mono chunks at :attr:`native_sample_rate`."""

    @abstractmethod
    def close(self) -> None:
        """Release the model; idempotent, runs on the owner thread."""


def assert_backend_contract(
    backend: TtsBackend,
    *,
    probe_text: str = "contract probe",
    voice: str | None = None,
    language: str | None = None,
) -> None:
    """Assert ``backend`` honors the streaming contract (tests + registration)."""
    caps = get_capabilities(backend.engine_id)  # type: ignore[arg-type]
    assert backend.native_sample_rate == caps.native_sample_rate, (
        f"native rate {backend.native_sample_rate} != capabilities {caps.native_sample_rate}"
    )
    chunks = list(backend.synthesize_stream(probe_text, voice=voice, language=language))
    assert len(chunks) >= 1, "synthesize_stream must yield at least one chunk"
    for chunk in chunks:
        assert isinstance(chunk, np.ndarray), f"chunk must be ndarray, got {type(chunk).__name__}"
        assert chunk.dtype == np.float32, f"chunk must be float32, got {chunk.dtype}"
        assert chunk.ndim == 1, f"chunk must be mono 1-D, got shape {chunk.shape}"
    backend.close()
    backend.close()  # close must be idempotent


class VieneuBackend(TtsBackend):
    """Adapt the existing :class:`TTSEngine` behind :class:`TtsBackend`.

    Behavior is unchanged: the bounded chunked path
    (``infer_stream_chunked``) keeps ONNX arena growth capped by segment.
    ``language``/``instruction`` are validated upstream (see
    :class:`TTSRequest`); VieNeu accepts vi/en and no instructions.
    """

    engine_id: str = VIENEU

    def __init__(self, engine: TTSEngine) -> None:
        self._engine = engine
        self.native_sample_rate: int = get_capabilities(VIENEU).native_sample_rate

    def initialize(self) -> None:
        self._engine.initialize()

    @property
    def is_initialized(self) -> bool:
        return self._engine.is_initialized

    def synthesize_stream(
        self,
        text: str,
        *,
        voice: str | None = None,
        language: str | None = None,
        instruction: str | None = None,
        temperature: float | None = None,
    ) -> Iterator[np.ndarray]:
        del language, instruction  # validated upstream; VieNeu takes voice only
        yield from self._engine.infer_stream_chunked(text, voice=voice, temperature=temperature)

    def close(self) -> None:
        self._engine.close()


_BackendFactory = Callable[..., TtsBackend]
_REGISTRY: dict[str, _BackendFactory] = {}


def _default_vieneu_factory(**kwargs: Any) -> VieneuBackend:
    engine_factory = kwargs.get("engine_factory")
    return VieneuBackend(TTSEngine(factory=engine_factory))


_REGISTRY[VIENEU] = _default_vieneu_factory


def register_backend(engine_id: EngineId, factory: _BackendFactory | None) -> None:
    """Register (or, with None, unregister) a backend factory."""
    get_capabilities(engine_id)  # reject unknown engines at registration time
    if factory is None:
        _REGISTRY.pop(engine_id, None)
    else:
        _REGISTRY[engine_id] = factory


def create_backend(engine_id: EngineId, **kwargs: Any) -> TtsBackend:
    """Build the backend for ``engine_id`` (worker-owned, single instance)."""
    caps = get_capabilities(engine_id)  # raises BackendCapabilityError on unknown engine
    try:
        factory = _REGISTRY[engine_id]
    except KeyError:
        raise BackendCapabilityError(
            f"no backend registered for {caps.label} — "
            "install the optional Qwen runtime/model pack first"
        ) from None
    return factory(**kwargs)
