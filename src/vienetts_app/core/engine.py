"""TTSEngine: owns the single lazily-initialized Vieneu instance (§5, NFR-2).

Wraps the confirmed SDK contract (docs/spike-report.md §0). The factory is
injectable so unit tests run against a fake; production uses the real
``vieneu.Vieneu``. Not thread-safe by design — exactly one worker thread owns
an engine (plan §4).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)


class TTSEngineError(RuntimeError):
    """Engine operation failed; message is user-actionable."""


def _default_factory(**kwargs: Any) -> Any:
    from vieneu import Vieneu  # deferred: importing vieneu is not free

    return Vieneu(**kwargs)


class TTSEngine:
    """Thin wrapper enforcing single-instance ownership + actionable errors."""

    def __init__(
        self,
        backend: str = "auto",
        precision: str = "int8",
        factory: Callable[..., Any] | None = None,
    ) -> None:
        self._factory = factory or _default_factory
        self._init_kwargs: dict[str, Any] = {"backend": backend, "precision": precision}
        self._tts: Any = None

    # ── lifecycle ───────────────────────────────────────────────────────────

    @property
    def is_initialized(self) -> bool:
        return self._tts is not None

    @property
    def sample_rate(self) -> int:
        if self._tts is None:
            raise TTSEngineError("engine is not initialized; run a request first")
        return int(self._tts.sample_rate)

    @property
    def backend(self) -> str:
        self._ensure()
        return str(self._tts.backend)

    def close(self) -> None:
        if self._tts is not None:
            try:
                self._tts.close()
            except Exception:  # noqa: BLE001 - shutdown must not raise
                logger.exception("error closing Vieneu instance")
            finally:
                self._tts = None

    def _ensure(self) -> Any:
        if self._tts is None:
            try:
                self._tts = self._factory(**self._init_kwargs)
            except ModuleNotFoundError as exc:
                if "torch" in str(exc):
                    raise TTSEngineError(
                        "The torch/CUDA stack is not installed. Install the GPU extra "
                        "(pip install 'vienetts-app[gpu]') or switch the backend to onnx "
                        "in Settings."
                    ) from exc
                raise TTSEngineError(f"Engine initialization failed: {exc}") from exc
            except Exception as exc:
                raise TTSEngineError(f"Engine initialization failed: {exc}") from exc
            logger.info("Vieneu initialized with %s", self._init_kwargs)
        return self._tts

    def _run(self, op: str, fn: Callable[[Any], Any]) -> Any:
        tts = self._ensure()
        try:
            return fn(tts)
        except TTSEngineError:
            raise
        except Exception as exc:
            raise TTSEngineError(f"{op} failed: {exc}") from exc

    # ── synthesis (confirmed contract) ──────────────────────────────────────

    def infer(
        self,
        text: str,
        voice: str | None = None,
        ref_audio: str | None = None,
        temperature: float | None = None,
        top_k: int | None = None,
    ) -> np.ndarray:
        return self._run(
            "infer",
            lambda tts: tts.infer(
                text,
                voice=voice,
                ref_audio=ref_audio,
                temperature=temperature,
                top_k=top_k,
                show_progress=False,
            ),
        )

    def infer_stream(
        self, text: str, voice: str | None = None, temperature: float | None = None
    ) -> Iterator[np.ndarray]:
        tts = self._ensure()
        try:
            yield from tts.infer_stream(text, voice=voice, temperature=temperature)
        except Exception as exc:
            raise TTSEngineError(f"infer_stream failed: {exc}") from exc

    def infer_batch(self, texts: Sequence[str], voice: str | None = None) -> list[np.ndarray]:
        return self._run("infer_batch", lambda tts: tts.infer_batch(list(texts), voice=voice))

    # ── voices / cleanup / export ───────────────────────────────────────────

    def list_voices(self) -> list[tuple[str, str]]:
        return self._run("list_voices", lambda tts: list(tts.list_preset_voices()))

    def add_voice(
        self, name: str, ref_clip: str | Path, *, denoise: bool = True, save: bool = False
    ) -> str:
        return self._run(
            "add_voice", lambda tts: tts.add_voice(name, str(ref_clip), denoise=denoise, save=save)
        )

    def remove_voice(self, name: str, *, save: bool = False) -> None:
        self._run("remove_voice", lambda tts: tts.remove_voice(name, save=save))

    def denoise(
        self, clip: str | Path, out_path: str | Path | None = None, max_seconds: float | None = None
    ) -> tuple[np.ndarray, int]:
        return self._run(
            "denoise",
            lambda tts: tts.denoise(
                str(clip),
                out_path=None if out_path is None else str(out_path),
                max_seconds=max_seconds,
            ),
        )

    def save(self, audio: np.ndarray, path: str | Path) -> None:
        self._run("save", lambda tts: tts.save(audio, str(path)))
