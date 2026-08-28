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
import re
from collections.abc import Callable, Iterator, Sequence
from pathlib import Path
from typing import Any

import numpy as np

logger = logging.getLogger(__name__)

# Defensive import: huggingface_hub ships with vieneu today, but the
# classification must degrade gracefully if the dependency tree changes.
try:
    from huggingface_hub.errors import EntryNotFoundError as _HubEntryNotFound
    from huggingface_hub.errors import LocalEntryNotFoundError as _HubLocalEntryNotFound
    from huggingface_hub.errors import OfflineModeIsEnabled as _HubOfflineMode

    # Exception types whose meaning is "the weights are not available locally":
    # verified in huggingface_hub 1.28.0 (see ModelsMissingError docstring).
    _HUB_WEIGHT_ERRORS: tuple[type[BaseException], ...] = (
        _HubEntryNotFound,
        _HubLocalEntryNotFound,
        _HubOfflineMode,
    )
except ImportError:  # pragma: no cover - only on a stripped install
    _HUB_WEIGHT_ERRORS = ()

VOICES_FILENAME = "voices.json"


class TTSEngineError(RuntimeError):
    """Engine operation failed; message is user-actionable."""


class ModelsMissingError(TTSEngineError):
    """Lazy init failed because the model WEIGHTS ARE ABSENT, not broken.

    Raised by ``_ensure()`` when the ``Vieneu(...)`` factory fails with a
    shape that means "weights not downloaded / not on disk" rather than a
    generic engine fault. Shapes (verified against vieneu 3.3.0 +
    huggingface_hub 1.28.0, live repro with empty HF_HOME + HF_HUB_OFFLINE=1):

    - ``huggingface_hub.errors.LocalEntryNotFoundError`` — the ONNX engine's
      per-artifact ``hf_hub_download`` calls (``OnnxV3LiteEngine._fetch``)
      fail this way when the HF cache is missing and offline mode is on; its
      MRO is LocalEntryNotFoundError → FileNotFoundError → OSError.
    - bare ``FileNotFoundError`` — local-dir weight reads (config/graphs) on a
      path that was never fetched.
    - ``OfflineModeIsEnabled`` / other hub entry errors — defensive: hub may
      surface these directly from other code paths.

    STRING SEAM (FR-4.6c): the worker's ``error`` signal carries only
    ``str(exc)``, so every message STARTS WITH the stable prefix constant
    ``MODELS_MISSING_MARKER`` ("Model weights are missing"). Downstream UI
    must detect this case via :func:`is_models_missing` on the plain message
    and route to the "models missing" screen instead of a generic dialog.
    The message names the concrete fetch command (``python scripts/fetch_models.py``,
    confirmed against that script) and the HF_HOME/HF_HUB_OFFLINE envs because
    the SDK genuinely resolves weights through huggingface_hub cache lookups
    (docs/spike-report.md §6 strategy B).
    """


MODELS_MISSING_MARKER = "Model weights are missing"
FETCH_MODELS_COMMAND = "python scripts/fetch_models.py"


def is_models_missing(message: str) -> bool:
    """True if ``message`` came from a :class:`ModelsMissingError`.

    Detection seam for the worker → UI error path, which carries only plain
    strings: every ModelsMissingError message starts with the marker constant.
    Anything else (including empty/generic text) is False.
    """
    return bool(message) and message.startswith(MODELS_MISSING_MARKER)


def _is_weights_missing_exception(exc: BaseException) -> bool:
    """Classify a factory exception as "model weights absent" vs generic fault.

    FileNotFoundError covers both the bare local-dir read failures AND
    ``LocalEntryNotFoundError`` (a FileNotFoundError subclass); the hub entry/
    offline classes cover shapes that are NOT FileNotFoundError subclasses.
    Deliberately narrow: PermissionError or connection errors alone stay
    generic TTSEngineError.
    """
    return isinstance(exc, (FileNotFoundError, *_HUB_WEIGHT_ERRORS))


def _models_missing_message(exc: BaseException) -> str:
    return (
        f"{MODELS_MISSING_MARKER}: the TTS model files were not found in the local "
        f"Hugging Face cache ({exc}). Fetch the offline bundle once with "
        f"`{FETCH_MODELS_COMMAND}` (run from the project root); to launch fully "
        f"offline, point HF_HOME at the bundled cache and set HF_HUB_OFFLINE=1."
    )


# App-level segment cap for long-text STREAMING synthesis (FR-4.6d).
#
# Why 512: the SDK's own AR chunking inside ``infer_stream`` is capped at
# max_chars=256 (vieneu/v3turbo.py infer_stream signature +
# normalize_to_chunks_v3 in vieneu_utils/phonemize_text.py), so any app
# segment ≥256 chars adds no extra prefill work per character — the model
# workload per infer_stream call is set by the SDK's 256-char chunks either
# way. Doubling it to 512 halves the number of app-level dispatches while
# keeping the largest single infer_stream workload bounded at ~2 SDK chunks,
# so ONNX Runtime's arena grows with SEGMENT size, not document size (spike
# §18 measured a ~2.5 GB plateau when one infer_stream call covers a whole
# document; budget < 2 GB).
DEFAULT_MAX_CHARS = 512

# Sentence-terminal punctuation that closes a segment unit: ASCII .!?,
# Unicode … (U+2026) and fullwidth ！？。; optional trailing closing
# quotes/brackets stay attached to the sentence; the match ends at the
# following whitespace (or end of text). Comma/semicolon are deliberately
# NOT boundaries (they do not reliably end an intonation unit); newlines are
# folded into the same terminator's trailing whitespace.
_SENTENCE_END_RE = re.compile(r"[.!?…！？。]+[\"'”’)\]]*(?:\s+|$)")


def _split_into_sentence_units(cleaned: str) -> list[str]:
    """Cut ``cleaned`` into sentence/paragraph units, keeping punctuation.

    A unit is everything up to (and including) a run of terminal punctuation
    plus its trailing whitespace/newlines. Trailing whitespace of each unit
    and the final tail are stripped; empty units are dropped.
    """
    units: list[str] = []
    start = 0
    for match in _SENTENCE_END_RE.finditer(cleaned):
        end = match.end()
        unit = cleaned[start:end].strip()
        if unit:
            units.append(unit)
        start = end
    tail = cleaned[start:].strip()
    if tail:
        units.append(tail)
    return units


def split_text_for_streaming(text: str, max_chars: int = DEFAULT_MAX_CHARS) -> list[str]:
    """Split ``text`` into segments of ≤ ``max_chars`` at natural boundaries.

    Pure function used by chunked stream dispatch so ONE ``infer_stream``
    call never sees more than ``max_chars`` characters: ONNX Runtime's CPU
    arena grows with the largest single workload and never shrinks (spike
    §18, bead VieNeuTTSApp-u5c), so bounding segments bounds RSS for
    arbitrarily long documents.

    Rules:
    - Text is first cut into units at sentence terminators (``.!?!…`` etc.,
      optionally followed by closing quotes/brackets) and newlines; the
      terminal punctuation stays attached to its sentence. Sentences are
      NEVER broken mid-sentence while they fit within ``max_chars``;
      consecutive units are greedily packed into one segment until adding
      the next would exceed the cap.
    - A single unit longer than ``max_chars`` (a runaway run without
      terminal punctuation) is hard-split AT the cap, preferring the last
      space inside the window so words stay whole where possible; only a
      word longer than ``max_chars`` itself is split mid-word.
    - Empty and whitespace-only segments are dropped.
    - Deterministic; unicode/diacritics safe (pure str slicing, no NFC/NFD
      normalization that could decompose Vietnamese combining marks).

    Returns ``[text]`` (stripped) when it already fits, so short texts keep
    byte-identical downstream behavior to today's non-chunked path.
    """
    if max_chars < 1:
        raise ValueError(f"max_chars must be >= 1, got {max_chars}")
    cleaned = (text or "").strip()
    if not cleaned:
        return []

    units = _split_into_sentence_units(cleaned)

    segments: list[str] = []
    current = ""
    for unit in units:
        if len(unit) > max_chars:
            if current:
                segments.append(current)
                current = ""
            remaining = unit
            while len(remaining) > max_chars:
                cut = remaining.rfind(" ", 0, max_chars + 1)
                if cut <= 0:
                    cut = max_chars
                segments.append(remaining[:cut].strip())
                remaining = remaining[cut:].strip()
            current = remaining
        elif not current:
            current = unit
        elif len(current) + 1 + len(unit) <= max_chars:
            current = f"{current} {unit}"
        else:
            segments.append(current)
            current = unit
    if current:
        segments.append(current)
    return segments


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
        threads: int | None = None,
        max_batch_size: int | None = None,
    ) -> None:
        if threads is not None and (
            not isinstance(threads, int) or isinstance(threads, bool) or threads < 0
        ):
            raise ValueError("threads must be a non-negative integer or None")
        if max_batch_size is not None and (
            not isinstance(max_batch_size, int)
            or isinstance(max_batch_size, bool)
            or max_batch_size < 1
        ):
            raise ValueError("max_batch_size must be a positive integer or None")
        self._factory = factory or _default_factory
        self._init_kwargs: dict[str, Any] = {"backend": backend, "precision": precision}
        if threads is not None:
            self._init_kwargs["threads"] = threads
        if max_batch_size is not None:
            self._init_kwargs["max_batch_size"] = max_batch_size
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

    def initialize(self) -> None:
        """Load the configured VieNeu engine without running synthesis."""
        self._ensure()

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
                if _is_weights_missing_exception(exc):
                    # Weights absent (missing/offline HF cache, FR-4.6c) — a
                    # distinct actionable case, not a generic engine fault.
                    raise ModelsMissingError(_models_missing_message(exc)) from exc
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

    def infer_stream_chunked(
        self,
        text: str,
        voice: str | None = None,
        temperature: float | None = None,
        max_chars: int | None = None,
    ) -> Iterator[np.ndarray]:
        """Stream ``text`` as ONE continuous chunk stream, synthesized per
        :func:`split_text_for_streaming` segment (FR-4.6d).

        Each segment is dispatched to ``tts.infer_stream`` separately and its
        chunks yielded straight through (no accumulation beyond one chunk),
        so the largest single SDK workload is bounded by the segment cap
        instead of document length — keeping ONNX Runtime's arena plateau in
        check for long documents (bead VieNeuTTSApp-u5c, §18 budget < 2 GB).

        Short texts (a single segment) behave byte-identically to
        :meth:`infer_stream`; error wrapping and lazy init match too. The
        audio concatenation is seamless: the SDK's own internal chunk joins
        are raw sample concatenation with no injected silence, so app-level
        segment boundaries add no clicks either.
        """
        tts = self._ensure()
        limit = DEFAULT_MAX_CHARS if max_chars is None else max_chars
        try:
            for segment in split_text_for_streaming(text, max_chars=limit):
                yield from tts.infer_stream(segment, voice=voice, temperature=temperature)
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
