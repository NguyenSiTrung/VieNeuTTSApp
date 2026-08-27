"""TTSEngine: owns the single lazily-initialized Vieneu instance (§5, NFR-2).

Wraps the confirmed SDK contract (docs/spike-report.md §0). The factory is
injectable so unit tests run against a fake; production uses the real
``vieneu.Vieneu``. Not thread-safe by design — exactly one worker thread owns
an engine (plan §4).

Voice persistence redirect (FR-3.4): the SDK's ``add_voice(save=True)`` writes
into site-packages; instead, cloned voices are merged back into
``tts._preset_voices`` from ``<voices_dir>/voices.json`` at init and persisted
via ``tts.save_voices(<voices_dir>/voices.json)`` (``persist_voices``).
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

VOICES_FILENAME = "voices.json"


class TTSEngineError(RuntimeError):
    """Engine operation failed; message is user-actionable."""


def _default_factory(**kwargs: Any) -> Any:
    from vieneu import Vieneu  # deferred: importing vieneu is not free

    return Vieneu(**kwargs)


def _default_asset_path() -> Path:
    from vieneu import __file__ as vieneu_file  # deferred import

    return Path(vieneu_file).parent / "assets" / "voices_v3_turbo.json"


def _read_voices_json(path: Path) -> dict[str, dict[str, Any]]:
    """Read a voices JSON file → its ``presets`` dict; {} on any problem.

    Missing file, corrupt JSON, or a non-dict payload all degrade to an empty
    dict with a logged warning — the catalog/persistence layer must never take
    the app down (FR-3.4, NFR-3 robustness).
    """
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Ignoring unreadable voices file %s (%s)", path, exc)
        return {}
    if not isinstance(data, dict):
        logger.warning("Ignoring malformed voices file %s (not a JSON object)", path)
        return {}
    presets = data.get("presets")
    if not isinstance(presets, dict):
        logger.warning("Ignoring voices file %s (missing/invalid 'presets')", path)
        return {}
    return {str(name): entry for name, entry in presets.items() if isinstance(entry, dict)}


def preset_voices(asset_path: Path | None = None) -> list[dict[str, str]]:
    """Return the SDK preset catalog WITHOUT initializing any model (FR-3.1).

    Reads the vieneu 3.3.0 asset JSON (``vieneu/assets/voices_v3_turbo.json``;
    path injectable for tests) and returns one ``{"name", "description",
    "gender", "style"}`` dict per preset, in asset order. Missing or corrupt
    asset → empty list + logged warning (never crash the UI).
    """
    path = _default_asset_path() if asset_path is None else Path(asset_path)
    presets = _read_voices_json(path)
    return [
        {
            "name": name,
            "description": str(entry.get("description") or ""),
            "gender": str(entry.get("gender") or ""),
            "style": str(entry.get("style") or ""),
        }
        for name, entry in presets.items()
    ]


def _preset_names(asset_path: Path | None = None) -> set[str]:
    return {entry["name"] for entry in preset_voices(asset_path)}


def saved_voice_names(voices_dir: str | Path, asset_path: Path | None = None) -> list[str]:
    """Names in ``<voices_dir>/voices.json`` that are NOT SDK preset names.

    Lets the UI list cloned voices WITHOUT initializing the engine (NFR-3.1);
    order is preserved. Any read problem → empty list (see _read_voices_json).
    """
    persisted = _read_voices_json(Path(voices_dir) / VOICES_FILENAME)
    sdk_names = _preset_names(asset_path)
    return [name for name in persisted if name not in sdk_names]


class TTSEngine:
    """Thin wrapper enforcing single-instance ownership + actionable errors."""

    def __init__(
        self,
        backend: str = "auto",
        precision: str = "int8",
        factory: Callable[..., Any] | None = None,
        voices_dir: str | Path | None = None,
    ) -> None:
        self._factory = factory or _default_factory
        self._init_kwargs: dict[str, Any] = {"backend": backend, "precision": precision}
        self._tts: Any = None
        self._voices_dir = None if voices_dir is None else Path(voices_dir)

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
            if self._voices_dir is not None:
                self._merge_persisted_voices(self._tts)
        return self._tts

    def _merge_persisted_voices(self, tts: Any) -> None:
        """Re-inject persisted cloned voices after (re)initialization (FR-3.4).

        PRIVATE-ATTR COUPLING: vieneu 3.3.0 (pinned) exposes the live voice
        registry as ``tts._preset_voices`` (dict name → {"description",
        "gender", "style", "speaker_emb": np.float32, "codes": np.int64}) and
        offers no public API to load voices from a custom path. We therefore
        write into that dict directly, converting the persisted JSON lists to
        the dtypes the SDK uses (missing → None). Names already registered by
        the SDK win — persisted entries never clobber live presets. A corrupt
        file is logged and skipped: engine init must never fail over it.
        """
        persisted = _read_voices_json(self._voices_dir / VOICES_FILENAME)
        if not persisted:
            return
        registry: dict[str, Any] = tts._preset_voices  # noqa: SLF001 — see docstring
        injected = 0
        for name, entry in persisted.items():
            if name in registry:
                continue
            emb = entry.get("speaker_emb")
            codes = entry.get("codes")
            registry[name] = {
                "description": str(entry.get("description") or ""),
                "gender": str(entry.get("gender") or ""),
                "style": str(entry.get("style") or ""),
                "speaker_emb": None if emb is None else np.asarray(emb, dtype=np.float32),
                "codes": None if codes is None else np.asarray(codes, dtype=np.int64),
            }
            injected += 1
        if injected:
            logger.info("Restored %d persisted voice(s) from %s", injected, self._voices_dir)

    def persist_voices(self) -> Path:
        """Write ALL current voices to ``<voices_dir>/voices.json`` (FR-3.4).

        Redirects the SDK's default (site-packages) persistence into app data.
        Requires an initialized engine (voices only exist once the SDK has
        loaded its presets) and a configured ``voices_dir``; raises
        TTSEngineError otherwise. Returns the written path.
        """
        if self._tts is None:
            raise TTSEngineError("engine is not initialized; run a request first")
        if self._voices_dir is None:
            raise TTSEngineError("persist_voices requires a configured voices_dir")
        path = self._voices_dir / VOICES_FILENAME
        try:
            self._voices_dir.mkdir(parents=True, exist_ok=True)
            self._tts.save_voices(str(path))
        except Exception as exc:
            raise TTSEngineError(f"persist_voices failed: {exc}") from exc
        return path

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
