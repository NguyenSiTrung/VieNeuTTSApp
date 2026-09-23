"""ctypes binding for the managed qwentts.cpp shared library (``libqwen``).

``NativeQwenSession`` is the *internal* binding the GGUF host drives: explicit
``load`` / streaming ``synthesize`` / ``extract_voice_ref`` / cancellation /
``close`` operations, each mapping onto the audited ``qt_*`` C ABI. The module
imports cleanly without the native library — the ``.so``/``.dylib``/``.dll``
is only touched when a session is constructed with ``lib=None``.

ABI transcription source: ``src/qwen.h`` at qwentts.cpp commit
``0cbde9b5d21aa2142efa5d62e14b97f197ac6f6d`` (``QT_ABI_VERSION 5``), verified
end-to-end by ``scripts/spike/qwen_gguf_probe.py`` against the real library.
Do not extend this binding by guesswork — re-transcribe from the pinned header.

Ownership rules the binding enforces:

* ``qt_synthesize``'s ``on_chunk`` buffer is *borrowed* — the native side reuses
  it after the callback returns — so the PCM is copied out before returning.
* Every ``CFUNCTYPE`` instance is retained for the duration of the native call
  (or the session's lifetime for the log hook): native code calls them
  asynchronously, and a garbage-collected callback is a crash.
* ``qt_audio_free`` runs after every ``qt_synthesize``; ``qt_voice_ref_free``
  releases extracted references; ``qt_free`` releases the context — including
  partial-initialisation and error paths.
* Exceptions must never cross a ctypes callback boundary; callback failures are
  captured and re-raised on the calling thread after the native call returns.
"""

from __future__ import annotations

import contextlib
import ctypes
import os
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

# Transcribed from src/qwen.h @ 0cbde9b5d21aa2142efa5d62e14b97f197ac6f6d.
QT_ABI_VERSION = 5
QT_ABI_MIN_VERSION = 5

QT_STATUS_OK = 0
QT_STATUS_INVALID_PARAMS = -1
QT_STATUS_MODE_INVALID = -2
QT_STATUS_GENERATE_FAILED = -3
QT_STATUS_OOM = -4
QT_STATUS_CANCELLED = -5
QT_STATUS_NAMES = {
    QT_STATUS_OK: "ok",
    QT_STATUS_INVALID_PARAMS: "invalid_params",
    QT_STATUS_MODE_INVALID: "mode_invalid",
    QT_STATUS_GENERATE_FAILED: "generate_failed",
    QT_STATUS_OOM: "oom",
    QT_STATUS_CANCELLED: "cancelled",
}

#: Native decode rate — every qwentts.cpp talker emits 24 kHz mono float32.
NATIVE_SAMPLE_RATE = 24_000

REQUIRED_SYMBOLS = (
    "qt_version",
    "qt_last_error",
    "qt_init_default_params",
    "qt_init",
    "qt_free",
    "qt_audio_free",
    "qt_extract_voice_ref",
    "qt_voice_ref_free",
    "qt_log_set",
    "qt_tts_default_params",
    "qt_synthesize",
    "qt_num_codebooks",
    "qt_duration_sec_to_tokens",
    "qt_n_speakers",
    "qt_speaker_name",
    "qt_n_languages",
    "qt_language_name",
    "qt_model_type",
)


class NativeAbiError(RuntimeError):
    """Binding-level failure: missing symbols, no build identity, wrong call order."""


class NativeInitError(NativeAbiError):
    """``qt_init`` returned NULL; carries the upstream diagnostic."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail or "qt_init failed")
        self.status = status


class NativeCallError(NativeAbiError):
    """A ``qt_*`` entry returned a negative ``qt_status``."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail or QT_STATUS_NAMES.get(status, f"status {status}"))
        self.status = status


class NativeCancelled(NativeCallError):
    """The job was interrupted through the pinned cancel mechanism."""

    def __init__(self, detail: str = "cancelled") -> None:
        super().__init__(QT_STATUS_CANCELLED, detail or "cancelled")


@dataclass(frozen=True)
class VoiceRefData:
    """A copied-out voice reference — safe to reuse across synthesis calls.

    ``codes`` is the ``(num_codebooks, ref_T)`` int32 codec matrix; when it is
    non-empty the reference may still be conditioned on its transcript
    (``ref_text_usable``), which is how ``--ref-rvq`` + ``--ref-text`` pair up
    upstream.
    """

    spk_emb: np.ndarray  # float32, (ref_spk_dim,)
    codes: np.ndarray  # int32, (num_codebooks, ref_T)
    ref_text_usable: bool


# ── ctypes declarations, one-to-one with the pinned header ──────────────────

_QtCancelCb = ctypes.CFUNCTYPE(ctypes.c_bool, ctypes.c_void_p)
_QtChunkCb = ctypes.CFUNCTYPE(
    ctypes.c_bool, ctypes.POINTER(ctypes.c_float), ctypes.c_int, ctypes.c_void_p
)
_QtLogCb = ctypes.CFUNCTYPE(None, ctypes.c_int, ctypes.c_char_p, ctypes.c_void_p)


class _QtAudio(ctypes.Structure):
    _fields_ = [
        ("samples", ctypes.POINTER(ctypes.c_float)),
        ("n_samples", ctypes.c_int),
        ("sample_rate", ctypes.c_int),
        ("channels", ctypes.c_int),
    ]


class _QtInitParams(ctypes.Structure):
    _fields_ = [
        ("abi_version", ctypes.c_int),
        ("talker_path", ctypes.c_char_p),
        ("codec_path", ctypes.c_char_p),
        ("use_fa", ctypes.c_bool),
        ("clamp_fp16", ctypes.c_bool),
        ("max_batch", ctypes.c_int),
        ("codec_chunk_sec", ctypes.c_float),
    ]


class _QtVoiceRef(ctypes.Structure):
    _fields_ = [
        ("ref_spk_emb", ctypes.POINTER(ctypes.c_float)),
        ("ref_spk_dim", ctypes.c_int),
        ("ref_codes", ctypes.POINTER(ctypes.c_int32)),
        ("ref_T", ctypes.c_int),
        ("num_codebooks", ctypes.c_int),
    ]


class _QtTtsParams(ctypes.Structure):
    _fields_ = [
        ("abi_version", ctypes.c_int),
        ("text", ctypes.c_char_p),
        ("lang", ctypes.c_char_p),
        ("instruct", ctypes.c_char_p),
        ("speaker", ctypes.c_char_p),
        ("ref_audio_24k", ctypes.POINTER(ctypes.c_float)),
        ("ref_n_samples", ctypes.c_int),
        ("ref_text", ctypes.c_char_p),
        ("seed", ctypes.c_int64),
        ("max_new_tokens", ctypes.c_int),
        ("temperature", ctypes.c_float),
        ("top_k", ctypes.c_int),
        ("top_p", ctypes.c_float),
        ("repetition_penalty", ctypes.c_float),
        ("subtalker_temperature", ctypes.c_float),
        ("subtalker_top_k", ctypes.c_int),
        ("subtalker_top_p", ctypes.c_float),
        ("dump_dir", ctypes.c_char_p),
        ("cancel", ctypes.c_void_p),
        ("cancel_user_data", ctypes.c_void_p),
        ("on_chunk", ctypes.c_void_p),
        ("on_chunk_user_data", ctypes.c_void_p),
        ("ref_spk_emb", ctypes.POINTER(ctypes.c_float)),
        ("ref_spk_dim", ctypes.c_int),
        ("ref_codes", ctypes.POINTER(ctypes.c_int32)),
        ("ref_T", ctypes.c_int),
    ]


def shared_lib_name() -> str:
    """The library filename inside a managed runtime pack for this platform."""
    if sys.platform == "win32":
        return "qwen.dll"
    if sys.platform == "darwin":
        return "libqwen.dylib"
    return "libqwen.so"


def _bind_signatures(lib: Any) -> None:
    """Pin ``argtypes``/``restype`` on a freshly ``CDLL``-ed library.

    Injected fakes are plain Python objects whose methods reject attribute
    assignment, so this runs only for libraries this module loaded itself.
    """
    lib.qt_version.restype = ctypes.c_char_p
    lib.qt_last_error.restype = ctypes.c_char_p
    lib.qt_init_default_params.argtypes = [ctypes.POINTER(_QtInitParams)]
    lib.qt_init.argtypes = [ctypes.POINTER(_QtInitParams)]
    lib.qt_init.restype = ctypes.c_void_p
    lib.qt_free.argtypes = [ctypes.c_void_p]
    lib.qt_audio_free.argtypes = [ctypes.POINTER(_QtAudio)]
    lib.qt_extract_voice_ref.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_float),
        ctypes.c_int,
        ctypes.POINTER(_QtVoiceRef),
    ]
    lib.qt_voice_ref_free.argtypes = [ctypes.POINTER(_QtVoiceRef)]
    lib.qt_log_set.argtypes = [_QtLogCb, ctypes.c_void_p]
    lib.qt_tts_default_params.argtypes = [ctypes.POINTER(_QtTtsParams)]
    lib.qt_synthesize.argtypes = [
        ctypes.c_void_p,
        ctypes.POINTER(_QtTtsParams),
        ctypes.POINTER(_QtAudio),
    ]
    lib.qt_num_codebooks.argtypes = [ctypes.c_void_p]
    lib.qt_duration_sec_to_tokens.argtypes = [ctypes.c_void_p, ctypes.c_double]
    lib.qt_n_speakers.argtypes = [ctypes.c_void_p]
    lib.qt_speaker_name.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.qt_speaker_name.restype = ctypes.c_char_p
    lib.qt_n_languages.argtypes = [ctypes.c_void_p]
    lib.qt_language_name.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.qt_language_name.restype = ctypes.c_char_p
    lib.qt_model_type.argtypes = [ctypes.c_void_p]
    lib.qt_model_type.restype = ctypes.c_char_p


def _decode(raw: Any) -> str:
    """``c_char_p`` result → text, tolerating injected fakes and NULL."""
    if raw is None:
        return ""
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


class NativeQwenSession:
    """One ``qt_init`` context with explicit load/stream/ref/cancel/close ops.

    ``lib`` is the injectable seam: production passes nothing and the pinned,
    verified ``libqwen`` is ``CDLL``-ed from ``library_path``; tests inject a
    Python object with the same ``qt_*`` surface.
    """

    def __init__(self, library_path: str, *, lib: Any = None) -> None:
        self._path = str(library_path)
        created = lib is None
        if created:
            try:
                lib = ctypes.CDLL(self._path)
            except OSError as exc:
                raise NativeAbiError(f"cannot load {self._path}: {exc}") from exc
        missing = [name for name in REQUIRED_SYMBOLS if not hasattr(lib, name)]
        if missing:
            raise NativeAbiError(
                f"{self._path} does not export the pinned ABI — missing: {', '.join(missing)}"
            )
        if created:
            _bind_signatures(lib)
        self._lib = lib
        self._ctx: int | None = None
        self._model_type = ""
        self._speakers: tuple[str, ...] = ()
        self._languages: tuple[str, ...] = ()
        self._num_codebooks = 0
        self._library_version = ""
        self._log_cb: _QtLogCb | None = None

    # -- introspection ------------------------------------------------------ #

    @property
    def loaded(self) -> bool:
        return self._ctx is not None

    @property
    def library_version(self) -> str:
        return self._library_version

    @property
    def model_type(self) -> str:
        """The checkpoint's own type string: ``base`` | ``custom_voice`` | ..."""
        return self._model_type

    @property
    def speakers(self) -> tuple[str, ...]:
        """Model-reported speaker names (GGUF metadata, native spelling)."""
        return self._speakers

    @property
    def languages(self) -> tuple[str, ...]:
        """Model-reported language names (GGUF metadata, native spelling)."""
        return self._languages

    @property
    def num_codebooks(self) -> int:
        return self._num_codebooks

    # ``describe_capabilities`` in qwen_host reads these same-named SDK-style
    # getters so both hosts share one capability-mapping code path.
    def get_supported_speakers(self) -> list[str]:
        return list(self.speakers)

    def get_supported_languages(self) -> list[str]:
        return list(self.languages)

    # -- lifecycle ---------------------------------------------------------- #

    def load(
        self,
        talker_path: str,
        codec_path: str,
        *,
        use_fa: bool = True,
        clamp_fp16: bool = False,
        max_batch: int = 0,
        codec_chunk_sec: float = 0.0,
    ) -> None:
        """Open the talker+codec pair; enumerates model-reported capabilities."""
        if self._ctx is not None:
            raise NativeAbiError("session already holds a context — close() before reloading")
        version = _decode(self._lib.qt_version())
        if not version:
            raise NativeAbiError(f"{self._path} returned no qt_version build identity")
        params = _QtInitParams()
        self._lib.qt_init_default_params(ctypes.pointer(params))
        params.abi_version = QT_ABI_VERSION
        params.talker_path = os.fsencode(talker_path)
        params.codec_path = os.fsencode(codec_path)
        params.use_fa = bool(use_fa)
        params.clamp_fp16 = bool(clamp_fp16)
        if max_batch > 0:
            params.max_batch = int(max_batch)
        if codec_chunk_sec > 0:
            params.codec_chunk_sec = float(codec_chunk_sec)
        ctx = self._lib.qt_init(ctypes.pointer(params))
        if not ctx:
            raise NativeInitError(-1, self._last_error() or "qt_init returned NULL")
        self._ctx = ctx
        try:
            self._model_type = _decode(self._lib.qt_model_type(ctx))
            self._speakers = tuple(
                _decode(self._lib.qt_speaker_name(ctx, i))
                for i in range(int(self._lib.qt_n_speakers(ctx)))
            )
            self._languages = tuple(
                _decode(self._lib.qt_language_name(ctx, i))
                for i in range(int(self._lib.qt_n_languages(ctx)))
            )
            self._num_codebooks = int(self._lib.qt_num_codebooks(ctx))
        except Exception:
            self._release_ctx()
            raise
        self._library_version = version

    def close(self) -> None:
        """Free the native context and detach the log hook; idempotent."""
        self._release_ctx()
        if self._log_cb is not None:
            # Detach so the library can't call into a torn-down session.
            with contextlib.suppress(Exception):
                self._lib.qt_log_set(None, None)  # type: ignore[arg-type]
            self._log_cb = None

    def __enter__(self) -> NativeQwenSession:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _release_ctx(self) -> None:
        ctx, self._ctx = self._ctx, None
        self._model_type = ""
        self._speakers = ()
        self._languages = ()
        self._num_codebooks = 0
        if ctx is not None:
            self._lib.qt_free(ctx)

    # -- logging ------------------------------------------------------------ #

    def set_log_callback(self, sink: Callable[[str], None]) -> None:
        """Route native log lines to ``sink``; the callback is session-retained."""

        def _relay(_level: int, msg: bytes | None, _ud: Any) -> None:
            with contextlib.suppress(Exception):  # a log sink must not break the lib
                sink(_decode(msg))

        self._log_cb = _QtLogCb(_relay)
        self._lib.qt_log_set(self._log_cb, None)

    # -- synthesis ------------------------------------------------------------ #

    def synthesize(
        self,
        *,
        text: str,
        lang: str,
        speaker: str | None = None,
        instruct: str | None = None,
        ref: VoiceRefData | None = None,
        ref_audio_24k: np.ndarray | None = None,
        ref_text: str | None = None,
        seed: int | None = None,
        max_new_tokens: int | None = None,
        on_chunk: Callable[[np.ndarray], bool | None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> None:
        """One streaming synthesis; chunks arrive copied, on the native thread.

        ``on_chunk`` receives a fresh float32 array per call — the native buffer
        is copied before the callback returns. Returning ``False`` stops the
        stream; any other value (including ``None``) continues. ``cancelled``
        is polled by the native side (~83 ms granularity upstream). Callback
        exceptions are captured and re-raised here once ``qt_synthesize``
        returns, because an exception cannot propagate out of a ctypes
        callback.
        """
        ctx = self._require_loaded()
        params = _QtTtsParams()
        self._lib.qt_tts_default_params(ctypes.pointer(params))
        params.abi_version = QT_ABI_VERSION
        params.text = str(text).encode("utf-8")
        params.lang = lang.encode("utf-8") if lang else None
        params.instruct = instruct.encode("utf-8") if instruct else None
        params.speaker = speaker.encode("utf-8") if speaker else None
        keep: list[Any] = [params]
        if ref_audio_24k is not None:
            pcm = np.ascontiguousarray(ref_audio_24k, dtype=np.float32)
            if pcm.ndim != 1 or pcm.size == 0:
                raise NativeCallError(
                    QT_STATUS_INVALID_PARAMS, "reference audio must be non-empty mono float32"
                )
            keep.append(pcm)
            params.ref_audio_24k = pcm.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            params.ref_n_samples = int(pcm.size)
        if ref is not None:
            emb = np.ascontiguousarray(ref.spk_emb, dtype=np.float32)
            codes = np.ascontiguousarray(ref.codes, dtype=np.int32)
            keep += [emb, codes]
            params.ref_spk_emb = emb.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            params.ref_spk_dim = int(emb.size)
            params.ref_codes = codes.ctypes.data_as(ctypes.POINTER(ctypes.c_int32))
            params.ref_T = int(codes.shape[1] if codes.ndim == 2 else codes.size)
        params.ref_text = ref_text.encode("utf-8") if ref_text else None
        if seed is not None:
            params.seed = int(seed)
        if max_new_tokens:
            params.max_new_tokens = int(max_new_tokens)

        callback_errors: list[BaseException] = []

        def _cancel_relay(_ud: Any) -> bool:
            try:
                return bool(cancelled()) if cancelled is not None else False
            except BaseException as exc:  # noqa: BLE001 — cannot cross ctypes
                callback_errors.append(exc)
                return True  # stop the native call; the error is raised below

        cancel_cb = _QtCancelCb(_cancel_relay)
        keep.append(cancel_cb)
        params.cancel = ctypes.cast(cancel_cb, ctypes.c_void_p)

        def _chunk_relay(samples: Any, n_samples: int, _ud: Any) -> bool:
            try:
                chunk = np.ctypeslib.as_array(samples, shape=(int(n_samples),)).copy()
                verdict = on_chunk(chunk) if on_chunk is not None else True
                return verdict is not False
            except BaseException as exc:  # noqa: BLE001 — cannot cross ctypes
                callback_errors.append(exc)
                return False  # stop the native call; the error is raised below

        chunk_cb = _QtChunkCb(_chunk_relay)
        keep.append(chunk_cb)
        params.on_chunk = ctypes.cast(chunk_cb, ctypes.c_void_p)

        out = _QtAudio()
        try:
            status = int(self._lib.qt_synthesize(ctx, ctypes.pointer(params), ctypes.pointer(out)))
        finally:
            # qt_audio_free is NULL-safe and idempotent; upstream frees the
            # buffered output on every path, including early validation exits.
            self._lib.qt_audio_free(ctypes.pointer(out))
        if callback_errors:
            raise callback_errors[0]
        if status == QT_STATUS_CANCELLED:
            raise NativeCancelled(self._last_error() or "cancelled")
        if status != QT_STATUS_OK:
            raise NativeCallError(status, self._last_error())

    # -- voice reference extraction ------------------------------------------ #

    def extract_voice_ref(self, pcm_24k: np.ndarray) -> VoiceRefData:
        """Pre-encode a 24 kHz reference clip into reusable speaker latents.

        The returned data is a copy — the native buffers are released via
        ``qt_voice_ref_free`` before this method returns.
        """
        ctx = self._require_loaded()
        pcm = np.ascontiguousarray(pcm_24k, dtype=np.float32)
        if pcm.ndim != 1 or pcm.size == 0:
            raise NativeCallError(
                QT_STATUS_INVALID_PARAMS, "reference audio must be non-empty mono float32"
            )
        ref = _QtVoiceRef()
        status = int(
            self._lib.qt_extract_voice_ref(
                ctx,
                pcm.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
                int(pcm.size),
                ctypes.pointer(ref),
            )
        )
        if status != QT_STATUS_OK:
            raise NativeCallError(status, self._last_error())
        try:
            if not ref.ref_spk_emb or int(ref.ref_spk_dim) <= 0:
                raise NativeCallError(
                    QT_STATUS_GENERATE_FAILED,
                    "qt_extract_voice_ref returned no speaker embedding",
                )
            emb = np.ctypeslib.as_array(ref.ref_spk_emb, shape=(int(ref.ref_spk_dim),)).copy()
            count = int(ref.num_codebooks) * int(ref.ref_T)
            if count > 0:
                if not ref.ref_codes:
                    raise NativeCallError(
                        QT_STATUS_GENERATE_FAILED,
                        "qt_extract_voice_ref reported codes but returned a NULL buffer",
                    )
                codes = np.ctypeslib.as_array(ref.ref_codes, shape=(count,)).copy()
                codes = codes.reshape(int(ref.num_codebooks), int(ref.ref_T))
            else:
                codes = np.zeros((0, 0), dtype=np.int32)
        finally:
            self._lib.qt_voice_ref_free(ctypes.pointer(ref))
        return VoiceRefData(spk_emb=emb, codes=codes, ref_text_usable=bool(codes.size))

    def duration_sec_to_tokens(self, seconds: float) -> int:
        """Native seconds→token estimate (progress/reporting only)."""
        return int(self._lib.qt_duration_sec_to_tokens(self._require_loaded(), float(seconds)))

    # -- internals ----------------------------------------------------------- #

    def _require_loaded(self) -> int:
        if self._ctx is None:
            raise NativeAbiError("the native session is not loaded or is closed")
        return self._ctx

    def _last_error(self) -> str:
        try:
            return _decode(self._lib.qt_last_error())
        except Exception:  # noqa: BLE001 — never mask the original failure
            return ""


def default_library_path(runtime_dir: str | Path) -> str:
    """The verified library inside one managed runtime pack directory."""
    return str(Path(runtime_dir) / shared_lib_name())
