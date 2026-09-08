"""Qwen3-TTS backends behind the streaming contract (Phase 3 Task 2).

Wraps the official ``qwen-tts`` runtime (``Qwen3TTSModel``) without importing
it at module load: model construction goes through an injectable factory so
tests run torch-free. Whole-utterance ``generate_*`` calls are sliced into
bounded chunks for progress/cancel granularity — the blocking generate call
itself cannot be interrupted mid-call (same posture as the worker's plain
``infer``), but segments stay ≤512 chars so no call runs away.

Official signatures (HF model cards Qwen/Qwen3-TTS-12Hz-0.6B-{CustomVoice,Base}):
- ``generate_custom_voice(text, language, speaker, instruct) -> (wavs, sr)``
- ``generate_voice_clone(text, language, ref_audio, ref_text) -> (wavs, sr)``
with ``language`` as a full English name ("English", "Chinese", ...).
"""

from __future__ import annotations

import gc
from collections.abc import Callable, Iterator
from typing import Any

import numpy as np

from vienetts_app.core.backends import (
    QWEN_BASE,
    QWEN_CUSTOMVOICE,
    BackendCapabilityError,
    EngineId,
    get_capabilities,
)
from vienetts_app.core.qwen_runtime import (
    QWEN_BASE_REPO,
    QWEN_CUSTOMVOICE_REPO,
    QWEN_NATIVE_RATE,
    QwenRuntimeError,
    describe_qwen_device,
    require_qwen,
)
from vienetts_app.core.tts_backend import TtsBackend

# App language code → Qwen full language name (covers all 10 supported codes).
LANGUAGE_NAMES = {
    "en": "English",
    "zh": "Chinese",
    "ja": "Japanese",
    "ko": "Korean",
    "de": "German",
    "fr": "French",
    "ru": "Russian",
    "es": "Spanish",
    "it": "Italian",
    "pt": "Portuguese",
}

# Output slice per yielded chunk (0.5 s at the native rate): progress and
# cancel boundaries without duration-sized intermediate arrays.
_STREAM_SLICE = 12000


def qwen_language_name(code: str | None) -> str:
    """Map an app language code to the Qwen full language name."""
    try:
        return LANGUAGE_NAMES[code or ""]
    except KeyError:
        raise BackendCapabilityError(
            f"Qwen does not support language {code!r}; supported: {sorted(LANGUAGE_NAMES)}"
        ) from None


class QwenBackend(TtsBackend):
    """One loaded Qwen checkpoint (CustomVoice or Base) as a TtsBackend."""

    def __init__(
        self,
        engine_id: EngineId,
        model: Any,
        *,
        ref_audio: str | None = None,
        ref_text: str | None = None,
    ) -> None:
        get_capabilities(engine_id)
        self.engine_id: str = engine_id
        self.native_sample_rate: int = QWEN_NATIVE_RATE
        self._model = model
        self._ref_audio = ref_audio
        self._ref_text = ref_text
        self._open = True

    @classmethod
    def custom_voice(cls, model: Any) -> QwenBackend:
        return cls(QWEN_CUSTOMVOICE, model)

    @classmethod
    def base(
        cls, model: Any, *, ref_audio: str | None = None, ref_text: str | None = None
    ) -> QwenBackend:
        return cls(QWEN_BASE, model, ref_audio=ref_audio, ref_text=ref_text)

    def initialize(self) -> None:
        if self._model is None:
            raise BackendCapabilityError(f"{self.engine_id} backend is closed")

    @property
    def is_initialized(self) -> bool:
        return self._model is not None

    def synthesize_stream(
        self,
        text: str,
        *,
        voice: str | None = None,
        language: str | None = None,
        instruction: str | None = None,
        temperature: float | None = None,
    ) -> Iterator[np.ndarray]:
        del temperature  # qwen-tts exposes no temperature knob on these entries
        if self._model is None:
            raise BackendCapabilityError(f"{self.engine_id} backend is closed")
        name = qwen_language_name(language or "en")
        if self.engine_id == QWEN_CUSTOMVOICE:
            speaker = _require_speaker(voice)
            wavs, sr = self._model.generate_custom_voice(
                text=text,
                language=name,
                speaker=speaker,
                instruct=instruction or "",
            )
        else:
            if not self._ref_audio:
                raise BackendCapabilityError(
                    "Qwen3-TTS Base needs an enrolled reference voice — "
                    "clone a voice from a reference clip first"
                )
            wavs, sr = self._model.generate_voice_clone(
                text=text,
                language=name,
                ref_audio=self._ref_audio,
                ref_text=self._ref_text or "",
            )
        if int(sr) != self.native_sample_rate:
            raise BackendCapabilityError(
                f"Qwen residual sample rate drift: model returned {sr} Hz, "
                f"expected {self.native_sample_rate} Hz — update QWEN_NATIVE_RATE"
            )
        audio = np.ascontiguousarray(wavs[0], dtype=np.float32)
        if audio.ndim != 1 or audio.size == 0 or not np.all(np.isfinite(audio)):
            raise BackendCapabilityError("Qwen model returned empty/invalid audio")
        for start in range(0, audio.size, _STREAM_SLICE):
            yield audio[start : start + _STREAM_SLICE].copy()

    def close(self) -> None:
        """Drop the model reference; idempotent, runs on the owner thread."""
        self._model = None
        self._open = False
        gc.collect()
        try:
            import torch  # noqa: PLC0415 - optional; CUDA cache release only

            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except Exception:  # noqa: BLE001 - torch stub/build quirks must degrade, not crash
            pass


def _require_speaker(voice: str | None) -> str:
    """Validate a CustomVoice fixed speaker against the documented catalog."""
    from vienetts_app.core.voices import QWEN_SPEAKERS  # noqa: PLC0415 - lazy, one direction

    names = [speaker.name for speaker in QWEN_SPEAKERS]
    if voice is None or not voice.strip():
        raise BackendCapabilityError(
            "Qwen3-TTS CustomVoice needs an explicit fixed speaker — "
            f"choose one of: {', '.join(names)}"
        )
    if voice not in names:
        raise BackendCapabilityError(
            f"unknown Qwen speaker {voice!r} — available: {', '.join(names)}"
        )
    return voice


def _default_dtype(device: str) -> Any:
    import torch  # noqa: PLC0415 - optional runtime, lazy by design

    return torch.bfloat16 if device == "cuda" else torch.float32


def load_qwen_model(
    engine_id: EngineId,
    *,
    ref_audio: str | None = None,
    ref_text: str | None = None,
    model_factory: Callable[..., Any] | None = None,
    device_fn: Callable[[], tuple[str, str]] | None = None,
    dtype_fn: Callable[[str], Any] | None = None,
    qwen_import: Callable[[], Any] | None = None,
) -> QwenBackend:
    """Load a Qwen checkpoint through the official runtime and wrap it.

    CUDA when available, explicit CPU fallback otherwise. All seams are
    injectable so tests never touch torch/HF (``model_factory`` replaces
    ``from_pretrained``; ``device_fn``/``dtype_fn``/``qwen_import`` replace
    environment probes).
    """
    caps = get_capabilities(engine_id)
    if engine_id not in (QWEN_CUSTOMVOICE, QWEN_BASE):
        raise BackendCapabilityError(f"{caps.label} is not a Qwen profile")
    qwen = None
    if model_factory is None:
        # Production path: the real runtime must be present.
        qwen = require_qwen(import_fn=(lambda name: qwen_import()) if qwen_import else None)
    device, _detail = (device_fn or describe_qwen_device)()
    repo = QWEN_CUSTOMVOICE_REPO if engine_id == QWEN_CUSTOMVOICE else QWEN_BASE_REPO
    if model_factory is None:

        def model_factory(repo: str, device_map: str, dtype: Any) -> Any:
            return qwen.Qwen3TTSModel.from_pretrained(repo, device_map=device_map, dtype=dtype)

        try:
            dtype: Any = (dtype_fn or _default_dtype)(device)
        except ImportError as exc:
            from vienetts_app.core.qwen_runtime import _missing_message

            raise QwenRuntimeError(_missing_message("torch")) from exc
    else:
        dtype = dtype_fn(device) if dtype_fn is not None else None

    model = model_factory(
        repo,
        device_map="cuda:0" if device == "cuda" else "cpu",
        dtype=dtype,
    )
    if engine_id == QWEN_CUSTOMVOICE:
        return QwenBackend.custom_voice(model)
    return QwenBackend.base(model, ref_audio=ref_audio, ref_text=ref_text)


def _default_customvoice_factory(**kwargs: Any) -> QwenBackend:
    return load_qwen_model(QWEN_CUSTOMVOICE, model_factory=kwargs.get("qwen_model_factory"))


def _default_base_factory(**kwargs: Any) -> QwenBackend:
    return load_qwen_model(
        QWEN_BASE,
        ref_audio=kwargs.get("ref_audio"),
        ref_text=kwargs.get("ref_text"),
        model_factory=kwargs.get("qwen_model_factory"),
    )


def register_default_qwen_backends() -> None:
    """Register the production Qwen loaders (idempotent, app bootstrap only)."""
    from vienetts_app.core.tts_backend import register_backend  # noqa: PLC0415 - late bind

    register_backend(QWEN_CUSTOMVOICE, _default_customvoice_factory)
    register_backend(QWEN_BASE, _default_base_factory)
