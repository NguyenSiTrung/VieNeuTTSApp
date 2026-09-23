#!/usr/bin/env python
"""Qwen3-TTS GGUF / qwentts.cpp native compatibility probe (track Task 1.1).

Emits exactly ONE JSON verdict describing what the pinned shared library and
GGUF pair actually do on this host: build identity (``qt_version``), ABI
version acceptance, resolved GGML backend, model-reported type/languages/
speakers/codebooks, streaming chunk cadence (TTFA + chunk count), reference
extraction for Base, cooperative cancellation, audio validity, and clean
teardown.

Run (Linux CPU example, after building ``-DQWEN_SHARED=ON`` and downloading
the pinned pair):

    .venv/bin/python scripts/spike/qwen_gguf_probe.py \
        --profile customvoice --quantization Q8_0 \
        --library /path/to/libqwen.so \
        --talker /models/qwen-talker-0.6b-customvoice-Q8_0.gguf \
        --tokenizer /models/qwen-tokenizer-12hz-Q8_0.gguf \
        --speaker ryan --check-cancel --cell linux-x64-cpu \
        --json-out docs/performance/evidence/qwen-gguf-linux-x64-cpu-customvoice-Q8_0.json

The ABI object is injectable (``abi_factory``) so unit tests exercise every
verdict without a native library. Diagnostics go to stderr; stdout carries the
single JSON object and nothing else.

ABI transcription source: ``src/qwen.h`` at qwentts.cpp commit
0cbde9b5d21aa2142efa5d62e14b97f197ac6f6d (QT_ABI_VERSION 5). Do not extend
this binding by guesswork — re-transcribe from the pinned header.
"""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import json
import math
import os
import platform
import struct
import sys
import threading
import time
import wave
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

SCHEMA_VERSION = 1

# Transcribed from src/qwen.h @ 0cbde9b5d21aa2142efa5d62e14b97f197ac6f6d.
QT_ABI_VERSION = 5
QT_ABI_MIN_VERSION = 5
QT_STATUS_NAMES = {
    0: "ok",
    -1: "invalid_params",
    -2: "mode_invalid",
    -3: "generate_failed",
    -4: "oom",
    -5: "cancelled",
}

PROFILES = ("base", "customvoice")
QUANTIZATIONS = ("Q8_0", "Q4_K_M")
EXPECTED_MODEL_TYPE = {"base": "base", "customvoice": "custom_voice"}
DEVICES = ("auto", "cpu", "cuda", "metal")
# GGML_BACKEND device names the native loader accepts (backend_init in
# src/backend.h forces by name, or auto-picks when unset). ggml names Metal
# devices MTL<N> — "Metal" itself is not a selectable device name.
DEVICE_ENV = {"cpu": "CPU", "cuda": "CUDA0", "metal": "MTL0"}
CELL_KEYS = (
    "windows-x64-cpu",
    "windows-x64-cuda",
    "linux-x64-cpu",
    "linux-x64-cuda",
    "macos-arm64-cpu",
    "macos-arm64-metal",
)

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

DEFAULT_TEXT = "Xin chào, đây là bài kiểm tra tương thích của Qwen GGUF."
DEFAULT_LANGUAGE = "auto"
DEFAULT_CANCEL_AFTER_MS = 250
NATIVE_RATE = 24000

VERDICT_PASS = "pass"
VERDICT_USAGE_ERROR = "usage_error"
VERDICT_MISSING_BACKEND = "missing_backend"
VERDICT_ABI_MISMATCH = "abi_mismatch"
VERDICT_INVALID_SPEAKER = "invalid_speaker"
VERDICT_INVALID_LANGUAGE = "invalid_language"
VERDICT_MODE_MISMATCH = "mode_mismatch"
VERDICT_BAD_MODEL = "bad_model"
VERDICT_BAD_AUDIO = "bad_audio"
VERDICT_CANCELLED = "cancelled"
VERDICT_OOM = "oom"
VERDICT_FAILED = "failed"


class ProbeUsageError(ValueError):
    """The probe was invoked with an unusable combination; message names the fix."""


class MissingBackendError(RuntimeError):
    """The shared library or its compute backend cannot be loaded."""


class AbiMismatchError(RuntimeError):
    """The loaded library does not match the pinned public ABI."""


class NativeInitError(RuntimeError):
    """qt_init returned NULL; carries the upstream diagnostic."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail or "qt_init failed")
        self.status = status


class NativeCallError(RuntimeError):
    """A qt_* entry returned a negative qt_status."""

    def __init__(self, status: int, detail: str) -> None:
        super().__init__(detail or QT_STATUS_NAMES.get(status, f"status {status}"))
        self.status = status


@dataclass(frozen=True)
class ProbeRequest:
    """One validated probe run."""

    profile: str
    quantization: str
    talker: str
    tokenizer: str
    library: str
    device: str = "auto"
    text: str = DEFAULT_TEXT
    language: str = DEFAULT_LANGUAGE
    speaker: str = ""
    ref_audio: str = ""
    ref_text: str = ""
    instruct: str = ""
    use_fa: bool = True
    clamp_fp16: bool = False
    max_batch: int = 1
    codec_chunk_sec: float = 0.0
    seed: int = -1
    max_new_tokens: int = 0  # 0 keeps the upstream default (2048)
    check_cancel: bool = False
    cancel_after_ms: int = DEFAULT_CANCEL_AFTER_MS
    hash_models: bool = True
    cell: str = ""
    json_out: str = ""
    engine: str = field(default="qwentts_cpp", init=False)


def _shared_lib_name() -> str:
    if sys.platform == "win32":
        return "qwen.dll"
    if sys.platform == "darwin":
        return "libqwen.dylib"
    return "libqwen.so"


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="qwen_gguf_probe",
        description="Probe the pinned qwentts.cpp shared library + GGUF pair; print one JSON verdict.",
    )
    # Presence is validated in validate_request (not argparse) so every usage
    # failure still emits the single JSON verdict contract.
    parser.add_argument("--profile", default="", help=f"one of: {', '.join(PROFILES)}")
    parser.add_argument("--quantization", default="", help=f"one of: {', '.join(QUANTIZATIONS)}")
    parser.add_argument("--talker", default="", help="Talker GGUF path.")
    parser.add_argument("--tokenizer", default="", help="12 Hz tokenizer GGUF path.")
    parser.add_argument(
        "--library",
        default="",
        help="libqwen shared library path, or the directory that contains it.",
    )
    parser.add_argument("--device", default="auto", help="auto | cpu | cuda | metal")
    parser.add_argument("--text", default=DEFAULT_TEXT)
    parser.add_argument("--language", default=DEFAULT_LANGUAGE)
    parser.add_argument("--speaker", default="", help="CustomVoice named speaker (required).")
    parser.add_argument("--ref-audio", default="", help="Base reference clip (required).")
    parser.add_argument("--ref-text", default="", help="Base reference transcript (required).")
    parser.add_argument("--instruct", default="", help="Style instruction passthrough.")
    parser.add_argument("--no-use-fa", dest="use_fa", action="store_false")
    parser.add_argument("--clamp-fp16", action="store_true")
    parser.add_argument("--max-batch", type=int, default=1)
    parser.add_argument("--codec-chunk-sec", type=float, default=0.0)
    parser.add_argument("--seed", type=int, default=-1)
    parser.add_argument("--max-new-tokens", type=int, default=0)
    parser.add_argument("--check-cancel", action="store_true")
    parser.add_argument("--cancel-after-ms", type=int, default=DEFAULT_CANCEL_AFTER_MS)
    parser.add_argument("--no-hash-models", dest="hash_models", action="store_false")
    parser.add_argument("--cell", default="", help=f"Matrix cell key: {', '.join(CELL_KEYS)}")
    parser.add_argument("--json-out", default="", help="Also write the verdict JSON to this path.")
    return parser.parse_args(argv)


def validate_request(args: argparse.Namespace) -> ProbeRequest:
    """Validate CLI input before the native library is touched."""
    if args.profile not in PROFILES:
        raise ProbeUsageError(
            f"unknown profile {args.profile!r} — expected one of: {', '.join(PROFILES)}"
        )
    if args.quantization not in QUANTIZATIONS:
        raise ProbeUsageError(
            f"unknown quantization {args.quantization!r} — expected one of: {', '.join(QUANTIZATIONS)}"
        )
    if not args.talker:
        raise ProbeUsageError("--talker is required (the profile's GGUF)")
    talker = Path(args.talker)
    if not talker.is_file():
        raise ProbeUsageError(f"talker {args.talker!r} is not a file")
    if not args.tokenizer:
        raise ProbeUsageError("--tokenizer is required (the 12 Hz codec GGUF)")
    tokenizer = Path(args.tokenizer)
    if not tokenizer.is_file():
        raise ProbeUsageError(f"tokenizer {args.tokenizer!r} is not a file")
    if not args.library:
        raise ProbeUsageError("--library is required (the built libqwen shared library)")
    library = Path(args.library)
    if library.is_dir():
        library = library / _shared_lib_name()
    if not library.is_file():
        raise ProbeUsageError(f"library {library} is not a file")
    if args.device not in DEVICES:
        raise ProbeUsageError(
            f"unknown device {args.device!r} — expected one of: {', '.join(DEVICES)}"
        )
    if args.cell and args.cell not in CELL_KEYS:
        raise ProbeUsageError(
            f"unknown cell {args.cell!r} — expected one of: {', '.join(CELL_KEYS)}"
        )
    if args.profile == "customvoice":
        if not args.speaker.strip():
            raise ProbeUsageError("--speaker is required for customvoice")
        if args.ref_audio or args.ref_text:
            raise ProbeUsageError("--ref-audio/--ref-text are only valid for base")
    else:
        if args.speaker.strip():
            raise ProbeUsageError("--speaker is only valid for customvoice")
        if not args.ref_audio.strip():
            raise ProbeUsageError("--ref-audio is required for base (a 3-8 s reference clip)")
        if not Path(args.ref_audio).is_file():
            raise ProbeUsageError(f"ref-audio {args.ref_audio!r} does not exist")
        if not args.ref_text.strip():
            raise ProbeUsageError("--ref-text is required for base (the clip's transcript)")
    if args.cancel_after_ms < 0:
        raise ProbeUsageError("--cancel-after-ms must not be negative")
    if args.max_batch < 0:
        raise ProbeUsageError("--max-batch must not be negative")
    if args.codec_chunk_sec < 0:
        raise ProbeUsageError("--codec-chunk-sec must not be negative")
    return ProbeRequest(
        profile=args.profile,
        quantization=args.quantization,
        talker=str(talker),
        tokenizer=str(tokenizer),
        library=str(library),
        device=args.device,
        text=args.text,
        language=args.language,
        speaker=args.speaker.strip(),
        ref_audio=args.ref_audio,
        ref_text=args.ref_text,
        instruct=args.instruct,
        use_fa=bool(args.use_fa),
        clamp_fp16=bool(args.clamp_fp16),
        max_batch=int(args.max_batch),
        codec_chunk_sec=float(args.codec_chunk_sec),
        seed=int(args.seed),
        max_new_tokens=int(args.max_new_tokens),
        check_cancel=bool(args.check_cancel),
        cancel_after_ms=int(args.cancel_after_ms),
        hash_models=bool(args.hash_models),
        cell=args.cell,
        json_out=args.json_out,
    )


# ── ctypes binding, transcribed from the pinned src/qwen.h ──────────────────

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


class CtypesQwenAbi:
    """The probe-facing ABI surface, bound to a real libqwen via ctypes."""

    def __init__(self, library: str) -> None:
        path = Path(library)
        if path.is_dir():
            path = path / _shared_lib_name()
        try:
            self._lib = ctypes.CDLL(str(path))
        except OSError as exc:
            raise MissingBackendError(f"cannot load {path}: {exc}") from exc
        self._path = str(path)
        self._missing = [name for name in REQUIRED_SYMBOLS if not hasattr(self._lib, name)]
        if self._missing:
            return
        lib = self._lib
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
        lib.qt_n_speakers.argtypes = [ctypes.c_void_p]
        lib.qt_speaker_name.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.qt_speaker_name.restype = ctypes.c_char_p
        lib.qt_n_languages.argtypes = [ctypes.c_void_p]
        lib.qt_language_name.argtypes = [ctypes.c_void_p, ctypes.c_int]
        lib.qt_language_name.restype = ctypes.c_char_p
        lib.qt_model_type.argtypes = [ctypes.c_void_p]
        lib.qt_model_type.restype = ctypes.c_char_p
        self.log_lines: list[str] = []
        self._log_cb_ref: _QtLogCb | None = None

    def missing_symbols(self) -> list[str]:
        return list(self._missing)

    def version(self) -> str:
        raw = self._lib.qt_version()
        if not raw:
            raise AbiMismatchError("qt_version returned NULL")
        return raw.decode("utf-8", "replace")

    def last_error(self) -> str:
        raw = self._lib.qt_last_error()
        return raw.decode("utf-8", "replace") if raw else ""

    def set_log_callback(self, cb: Callable[[str], None]) -> None:
        def _relay(_level: int, msg: bytes | None, _ud) -> None:
            line = msg.decode("utf-8", "replace") if msg else ""
            self.log_lines.append(line)
            cb(line)

        self._log_cb_ref = _QtLogCb(_relay)  # retained: the lib calls it async
        self._lib.qt_log_set(self._log_cb_ref, None)

    def init(
        self,
        *,
        talker: str,
        codec: str,
        use_fa: bool,
        clamp_fp16: bool,
        max_batch: int,
        codec_chunk_sec: float,
    ) -> int:
        params = _QtInitParams()
        self._lib.qt_init_default_params(ctypes.byref(params))
        params.abi_version = QT_ABI_VERSION
        params.talker_path = os.fsencode(talker)
        params.codec_path = os.fsencode(codec)
        params.use_fa = use_fa
        params.clamp_fp16 = clamp_fp16
        params.max_batch = max_batch
        if codec_chunk_sec > 0:
            params.codec_chunk_sec = codec_chunk_sec
        ctx = self._lib.qt_init(ctypes.byref(params))
        if not ctx:
            raise NativeInitError(-1, self.last_error() or "qt_init returned NULL")
        return ctx

    def model_type_of(self, ctx: int) -> str:
        raw = self._lib.qt_model_type(ctx)
        return raw.decode("utf-8", "replace") if raw else ""

    def speakers_of(self, ctx: int) -> list[str]:
        return [
            self._lib.qt_speaker_name(ctx, i).decode("utf-8", "replace")
            for i in range(self._lib.qt_n_speakers(ctx))
        ]

    def languages_of(self, ctx: int) -> list[str]:
        return [
            self._lib.qt_language_name(ctx, i).decode("utf-8", "replace")
            for i in range(self._lib.qt_n_languages(ctx))
        ]

    def num_codebooks_of(self, ctx: int) -> int:
        return int(self._lib.qt_num_codebooks(ctx))

    def extract_voice_ref(self, ctx: int, pcm: np.ndarray) -> dict[str, int]:
        pcm = np.ascontiguousarray(pcm, dtype=np.float32)
        ref = _QtVoiceRef()
        rc = self._lib.qt_extract_voice_ref(
            ctx,
            pcm.ctypes.data_as(ctypes.POINTER(ctypes.c_float)),
            int(pcm.size),
            ctypes.byref(ref),
        )
        if rc != 0:
            raise NativeCallError(rc, self.last_error())
        try:
            return {
                "spkEmbDim": int(ref.ref_spk_dim),
                "refT": int(ref.ref_T),
                "numCodebooks": int(ref.num_codebooks),
            }
        finally:
            self._lib.qt_voice_ref_free(ctypes.byref(ref))

    def synthesize(
        self,
        ctx: int,
        *,
        text: str,
        lang: str,
        speaker: str | None = None,
        instruct: str | None = None,
        ref_audio_24k: np.ndarray | None = None,
        ref_text: str | None = None,
        ref_spk_emb: np.ndarray | None = None,
        ref_codes: np.ndarray | None = None,
        seed: int = -1,
        max_new_tokens: int = 0,
        on_chunk: Callable[[np.ndarray], bool] | None = None,
        cancel: Callable[[], bool] | None = None,
    ) -> int:
        params = _QtTtsParams()
        self._lib.qt_tts_default_params(ctypes.byref(params))
        params.abi_version = QT_ABI_VERSION
        params.text = text.encode("utf-8")
        params.lang = lang.encode("utf-8") if lang else None
        params.instruct = instruct.encode("utf-8") if instruct else None
        params.speaker = speaker.encode("utf-8") if speaker else None
        keep_alive: list[Any] = []
        if ref_audio_24k is not None:
            pcm = np.ascontiguousarray(ref_audio_24k, dtype=np.float32)
            keep_alive.append(pcm)
            params.ref_audio_24k = pcm.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            params.ref_n_samples = int(pcm.size)
        params.ref_text = ref_text.encode("utf-8") if ref_text else None
        if seed:
            params.seed = seed
        if max_new_tokens:
            params.max_new_tokens = max_new_tokens
        if ref_spk_emb is not None and ref_codes is not None:
            emb = np.ascontiguousarray(ref_spk_emb, dtype=np.float32)
            codes = np.ascontiguousarray(ref_codes, dtype=np.int32)
            keep_alive += [emb, codes]
            params.ref_spk_emb = emb.ctypes.data_as(ctypes.POINTER(ctypes.c_float))
            params.ref_spk_dim = int(emb.size)
            params.ref_codes = codes.ctypes.data_as(ctypes.POINTER(ctypes.c_int32))
            params.ref_T = int(codes.shape[1] if codes.ndim == 2 else codes.size)
        if cancel is not None:
            cancel_cb = _QtCancelCb(lambda _ud: bool(cancel()))
            keep_alive.append(cancel_cb)
            params.cancel = ctypes.cast(cancel_cb, ctypes.c_void_p)
        if on_chunk is not None:

            def _relay(samples, n_samples, _ud) -> bool:
                chunk = np.ctypeslib.as_array(samples, shape=(int(n_samples),)).copy()
                return bool(on_chunk(chunk))

            chunk_cb = _QtChunkCb(_relay)
            keep_alive.append(chunk_cb)
            params.on_chunk = ctypes.cast(chunk_cb, ctypes.c_void_p)
        out = _QtAudio()
        rc = int(self._lib.qt_synthesize(ctx, ctypes.byref(params), ctypes.byref(out)))
        if rc == 0 and on_chunk is None and out.samples and out.n_samples > 0:
            np.ctypeslib.as_array(out.samples, shape=(int(out.n_samples),)).copy()
        self._lib.qt_audio_free(ctypes.byref(out))
        return rc

    def free(self, ctx: int) -> None:
        self._lib.qt_free(ctx)


def _ctypes_abi_factory(library: str) -> CtypesQwenAbi:
    return CtypesQwenAbi(library)


# ── environment probes (all injectable) ─────────────────────────────────────


def peak_rss_mb(rss_fn: Callable[[], int]) -> float:
    raw = float(rss_fn())
    divisor = 1024.0 * 1024.0 if sys.platform == "darwin" else 1024.0
    return round(raw / divisor, 1)


def _default_rss_fn() -> int:
    if sys.platform == "win32":  # pragma: no cover - Windows-only path
        import ctypes as _ct

        class _Counters(_ct.Structure):
            _fields_ = [
                ("cb", _ct.c_ulong),
                ("PageFaultCount", _ct.c_ulong),
                ("PeakWorkingSetSize", _ct.c_size_t),
                ("WorkingSetSize", _ct.c_size_t),
                ("QuotaPeakPagedPoolUsage", _ct.c_size_t),
                ("QuotaPagedPoolUsage", _ct.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", _ct.c_size_t),
                ("QuotaNonPagedPoolUsage", _ct.c_size_t),
                ("PagefileUsage", _ct.c_size_t),
                ("PeakPagefileUsage", _ct.c_size_t),
            ]

        counters = _Counters()
        counters.cb = _ct.sizeof(counters)
        current = _ct.windll.kernel32.GetCurrentProcess()
        ok = _ct.windll.psapi.GetProcessMemoryInfo(current, _ct.byref(counters))
        return int(counters.PeakWorkingSetSize) // 1024 if ok else 0
    import resource

    return int(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss)


def _sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(lambda: handle.read(1 << 22), b""):
            digest.update(block)
    return digest.hexdigest()


# ── GGUF header inspection (local files, metadata section only) ─────────────

_GGUF_SCALAR = {
    0: ("B", 1),
    1: ("b", 1),
    2: ("H", 2),
    3: ("h", 2),
    4: ("I", 4),
    5: ("i", 4),
    6: ("f", 4),
    7: ("?", 1),
    10: ("Q", 8),
    11: ("q", 8),
    12: ("d", 8),
}


def _gguf_metadata(path: str, wanted: tuple[str, ...]) -> dict[str, Any]:
    """Read selected metadata keys from a local GGUF's header section.

    Stops at the tensor list; never touches tensor data. Returns what was
    found plus ``kvCount``/``tensorCount``/``version`` bookkeeping.
    """

    with open(path, "rb") as handle:
        head = handle.read(24)
        if len(head) < 24 or head[:4] != b"GGUF":
            raise ValueError("not a GGUF file")
        version, n_tensors, n_kv = struct.unpack("<IQQ", head[4:24])
        found: dict[str, Any] = {}
        wanted_set = set(wanted)

        def _string() -> str:
            (n,) = struct.unpack("<Q", handle.read(8))
            return handle.read(n).decode("utf-8", "replace")

        def _value(vtype: int) -> Any:
            if vtype == 8:
                return _string()
            if vtype == 9:
                (etype,) = struct.unpack("<I", handle.read(4))
                (count,) = struct.unpack("<Q", handle.read(8))
                if etype == 8:
                    return [_string() for _ in range(count)]
                fmt, size = _GGUF_SCALAR[etype]
                return list(struct.unpack(f"<{count}{fmt}", handle.read(count * size)))
            fmt, size = _GGUF_SCALAR[vtype]
            return struct.unpack(f"<{fmt}", handle.read(size))[0]

        for _ in range(n_kv):
            key = _string()
            (vtype,) = struct.unpack("<I", handle.read(4))
            value = _value(vtype)
            if key in wanted_set:
                found[key] = value
            if wanted_set <= found.keys():
                break
        return {
            "version": version,
            "tensorCount": n_tensors,
            "kvCount": n_kv,
            "values": found,
        }


# ── WAV helpers (stdlib; 24 kHz mono float32 for the reference path) ────────


def _write_wav_24k(path: Path, pcm: np.ndarray) -> None:
    pcm16 = np.clip(pcm, -1.0, 1.0)
    pcm16 = (pcm16 * 32767.0).astype("<i2")
    with wave.open(str(path), "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(NATIVE_RATE)
        wav.writeframes(pcm16.tobytes())


def _read_wav_mono(path: str) -> tuple[np.ndarray, int]:
    with wave.open(path, "rb") as wav:
        rate = wav.getframerate()
        channels = wav.getnchannels()
        width = wav.getsampwidth()
        raw = wav.readframes(wav.getnframes())
    if width == 2:
        pcm = np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    elif width == 4:
        pcm = np.frombuffer(raw, dtype="<f4").astype(np.float32)
    elif width == 1:
        pcm = (np.frombuffer(raw, dtype=np.uint8).astype(np.float32) - 128.0) / 128.0
    else:
        raise ProbeUsageError(f"ref-audio {path!r} uses an unsupported {width * 8}-bit WAV format")
    if channels > 1:
        pcm = pcm.reshape(-1, channels).mean(axis=1)
    return np.ascontiguousarray(pcm, dtype=np.float32), rate


def _resample_linear(pcm: np.ndarray, src_rate: int, dst_rate: int = NATIVE_RATE) -> np.ndarray:
    if src_rate == dst_rate or pcm.size == 0:
        return pcm.astype(np.float32, copy=False)
    duration = pcm.size / float(src_rate)
    out_n = max(1, int(round(duration * dst_rate)))
    positions = np.linspace(0.0, pcm.size - 1, num=out_n)
    return np.interp(positions, np.arange(pcm.size), pcm).astype(np.float32)


# ── probe flow ──────────────────────────────────────────────────────────────


def _base_result(request: ProbeRequest) -> dict[str, Any]:
    return {
        "schemaVersion": SCHEMA_VERSION,
        "kind": "qwen-gguf-probe",
        "verdict": VERDICT_FAILED,
        "profile": request.profile,
        "quantization": request.quantization,
        "engine": request.engine,
        "cell": request.cell or None,
        "host": {
            "platform": f"{sys.platform}-{platform.machine()}",
            "python": platform.python_version(),
        },
        "runtime": {
            "libraryPath": request.library,
            "qtVersion": None,
            "abiVersion": QT_ABI_VERSION,
            "abiMinVersion": QT_ABI_MIN_VERSION,
            "deviceEnv": None,
        },
        "model": {
            "talkerFile": Path(request.talker).name,
            "talkerBytes": os.path.getsize(request.talker)
            if os.path.exists(request.talker)
            else None,
            "tokenizerFile": Path(request.tokenizer).name,
            "tokenizerBytes": os.path.getsize(request.tokenizer)
            if os.path.exists(request.tokenizer)
            else None,
            "modelType": None,
            "gguf": None,
        },
        "device": {"requested": request.device, "resolvedBackend": None},
        "capabilities": {
            "languages": None,
            "speakers": None,
            "numCodebooks": None,
            "nativeSampleRate": NATIVE_RATE,
            "streaming": None,
        },
        "metrics": {
            "loadMs": None,
            "refExtractMs": None,
            "ttfaMs": None,
            "totalMs": None,
            "audioSeconds": None,
            "rtf": None,
            "peakRssMb": None,
        },
        "streaming": None,
        "refExtraction": None,
        "cancellation": None,
        "shutdown": {"clean": None, "detail": ""},
        "errors": [],
    }


def _fail(result: dict[str, Any], verdict: str, detail: str) -> dict[str, Any]:
    result["verdict"] = verdict
    if detail:
        result["errors"].append(detail)
    return result


def _classify_init_failure(detail: str) -> str:
    lowered = detail.lower()
    if "abi_version" in lowered or "abi version" in lowered:
        return VERDICT_ABI_MISMATCH
    if any(
        token in lowered for token in ("backend", "device", "cuda", "metal", "driver", "library")
    ):
        return VERDICT_MISSING_BACKEND
    return VERDICT_FAILED


def _check_model_metadata(request: ProbeRequest, result: dict[str, Any]) -> str | None:
    """Read GGUF headers for pairing evidence; a mismatch is a bad_model verdict."""
    report: dict[str, Any] = {"talker": {}, "tokenizer": {}}
    for label, path in (("talker", request.talker), ("tokenizer", request.tokenizer)):
        try:
            meta = _gguf_metadata(
                path, ("general.architecture", "general.name", "general.file_type")
            )
        except Exception as exc:  # noqa: BLE001 - an unreadable header is data, not a crash
            report[label] = {"parseError": str(exc)}
            continue
        values = meta["values"]
        report[label] = {
            "architecture": values.get("general.architecture"),
            "name": values.get("general.name"),
            "fileType": values.get("general.file_type"),
            "tensorCount": meta["tensorCount"],
        }
    result["model"]["gguf"] = report
    talker = report["talker"]
    tokenizer = report["tokenizer"]
    if talker.get("architecture") and talker["architecture"] != "qwen3-tts":
        return f"talker architecture {talker['architecture']!r} is not qwen3-tts"
    if tokenizer.get("architecture") and tokenizer["architecture"] != "qwen3-tts-tokenizer":
        return f"tokenizer architecture {tokenizer['architecture']!r} is not qwen3-tts-tokenizer"
    for label, entry in (("talker", talker), ("tokenizer", tokenizer)):
        file_type = entry.get("fileType")
        if file_type and file_type != request.quantization:
            return (
                f"{label} file_type {file_type!r} does not match requested "
                f"quantization {request.quantization!r}"
            )
    return None


def _collect_synth_stats() -> dict[str, Any]:
    return {
        "chunks": 0,
        "samples": 0,
        "first_chunk_samples": None,
        "last_chunk_samples": None,
        "finite": True,
        "peak": 0.0,
        "sum_sq": 0.0,
        "ttfa_ms": None,
    }


def _stream_stats_callback(stats: dict[str, Any], started: float):
    def _on_chunk(chunk: np.ndarray) -> bool:
        if stats["chunks"] == 0:
            stats["first_chunk_samples"] = int(chunk.size)
            stats["ttfa_ms"] = round((time.perf_counter() - started) * 1000.0, 3)
        stats["chunks"] += 1
        stats["samples"] += int(chunk.size)
        stats["last_chunk_samples"] = int(chunk.size)
        if not np.isfinite(chunk).all():
            stats["finite"] = False
        if chunk.size:
            stats["peak"] = max(stats["peak"], float(np.abs(chunk).max()))
            stats["sum_sq"] += float(np.square(chunk, dtype=np.float64).sum())
        return True

    return _on_chunk


def run_probe(
    request: ProbeRequest,
    *,
    abi_factory: Callable[[str], Any] | None = None,
    rss_fn: Callable[[], int] | None = None,
    environ: dict[str, str] | None = None,
    hash_fn: Callable[[str], str] | None = None,
) -> dict[str, Any]:
    """Run one probe and return the JSON-ready verdict mapping."""
    abi_factory = abi_factory or _ctypes_abi_factory
    rss_fn = rss_fn or _default_rss_fn
    hash_fn = hash_fn or _sha256_file
    environ = os.environ if environ is None else environ
    result = _base_result(request)
    result["request"] = {
        "textChars": len(request.text),
        "language": request.language,
        "speaker": request.speaker,
        "instruct": request.instruct,
        "useFa": request.use_fa,
        "clampFp16": request.clamp_fp16,
        "maxBatch": request.max_batch,
        "codecChunkSec": request.codec_chunk_sec,
        "seed": request.seed,
        "checkCancel": request.check_cancel,
    }

    if request.hash_models:
        for key, path in (
            ("talkerSha256", request.talker),
            ("tokenizerSha256", request.tokenizer),
        ):
            try:
                result["model"][key] = hash_fn(path)
            except OSError as exc:
                result["model"][key] = f"unhashable: {exc}"

    mismatch = _check_model_metadata(request, result)
    if mismatch:
        return _fail(result, VERDICT_BAD_MODEL, mismatch)

    try:
        abi = abi_factory(request.library)
    except MissingBackendError as exc:
        return _fail(result, VERDICT_MISSING_BACKEND, str(exc))
    except OSError as exc:
        return _fail(result, VERDICT_MISSING_BACKEND, str(exc))
    except Exception as exc:  # noqa: BLE001 - any loader failure is reportable
        return _fail(result, VERDICT_FAILED, f"library load raised {exc!r}")

    missing = abi.missing_symbols()
    if missing:
        return _fail(
            result,
            VERDICT_ABI_MISMATCH,
            f"library lacks required qt_* symbols: {', '.join(missing)}",
        )
    try:
        result["runtime"]["qtVersion"] = abi.version()
    except Exception as exc:  # noqa: BLE001 - version is the build identity
        return _fail(result, VERDICT_ABI_MISMATCH, f"qt_version failed: {exc}")

    abi.set_log_callback(lambda _line: None)
    previous_env = environ.get("GGML_BACKEND")
    if request.device in DEVICE_ENV:
        environ["GGML_BACKEND"] = DEVICE_ENV[request.device]
        result["runtime"]["deviceEnv"] = environ["GGML_BACKEND"]
    else:
        environ.pop("GGML_BACKEND", None)

    ctx = None
    try:
        load_started = time.perf_counter()
        try:
            ctx = abi.init(
                talker=request.talker,
                codec=request.tokenizer,
                use_fa=request.use_fa,
                clamp_fp16=request.clamp_fp16,
                max_batch=request.max_batch,
                codec_chunk_sec=request.codec_chunk_sec,
            )
        except NativeInitError as exc:
            return _fail(result, _classify_init_failure(str(exc)), str(exc))
        result["metrics"]["loadMs"] = round((time.perf_counter() - load_started) * 1000.0, 3)
        for line in getattr(abi, "log_lines", []):
            if "backend:" in line:
                result["device"]["resolvedBackend"] = (
                    line.split("backend:", 1)[1].strip().split(" ")[0]
                )

        model_type = abi.model_type_of(ctx)
        result["model"]["modelType"] = model_type
        expected = EXPECTED_MODEL_TYPE[request.profile]
        if model_type != expected:
            return _fail(
                result,
                VERDICT_MODE_MISMATCH,
                f"model_type {model_type!r} does not match profile {request.profile!r} "
                f"(expected {expected!r})",
            )

        speakers = abi.speakers_of(ctx)
        languages = abi.languages_of(ctx)
        result["capabilities"]["speakers"] = speakers
        result["capabilities"]["languages"] = languages
        result["capabilities"]["numCodebooks"] = abi.num_codebooks_of(ctx)

        if request.profile == "customvoice" and request.speaker.lower() not in speakers:
            return _fail(
                result,
                VERDICT_INVALID_SPEAKER,
                f"speaker {request.speaker!r} not in the model's table: {', '.join(speakers)}",
            )
        if request.language != "auto" and request.language.lower() not in languages:
            return _fail(
                result,
                VERDICT_INVALID_LANGUAGE,
                f"language {request.language!r} not in the model's codec table",
            )

        ref_pcm = None
        if request.profile == "base":
            pcm, rate = _read_wav_mono(request.ref_audio)
            ref_pcm = _resample_linear(pcm, rate)
            started = time.perf_counter()
            try:
                result["refExtraction"] = abi.extract_voice_ref(ctx, ref_pcm)
            except NativeCallError as exc:
                return _fail(result, VERDICT_FAILED, f"qt_extract_voice_ref failed: {exc}")
            result["metrics"]["refExtractMs"] = round((time.perf_counter() - started) * 1000.0, 3)

        stats = _collect_synth_stats()
        started = time.perf_counter()
        status = abi.synthesize(
            ctx,
            text=request.text,
            lang=request.language,
            speaker=request.speaker or None,
            instruct=request.instruct or None,
            ref_audio_24k=ref_pcm,
            ref_text=request.ref_text or None,
            seed=request.seed,
            max_new_tokens=request.max_new_tokens,
            on_chunk=_stream_stats_callback(stats, started),
        )
        result["metrics"]["totalMs"] = round((time.perf_counter() - started) * 1000.0, 3)
        result["streaming"] = {
            "chunkCount": stats["chunks"],
            "firstChunkSamples": stats["first_chunk_samples"],
            "lastChunkSamples": stats["last_chunk_samples"],
        }
        result["metrics"]["ttfaMs"] = stats["ttfa_ms"]

        if status == -5:
            return _fail(result, VERDICT_CANCELLED, "synthesis reported QT_STATUS_CANCELLED")
        if status != 0:
            verdict = VERDICT_OOM if status == -4 else VERDICT_FAILED
            detail = abi.last_error() or QT_STATUS_NAMES.get(status, f"status {status}")
            return _fail(result, verdict, f"qt_synthesize: {detail}")
        if stats["samples"] == 0:
            return _fail(result, VERDICT_BAD_AUDIO, "synthesis produced no samples")
        if not stats["finite"]:
            return _fail(result, VERDICT_BAD_AUDIO, "synthesis produced non-finite samples")
        rms = math.sqrt(stats["sum_sq"] / stats["samples"])
        if stats["peak"] == 0.0 or rms == 0.0:
            return _fail(result, VERDICT_BAD_AUDIO, "synthesis produced only silence")

        audio_seconds = stats["samples"] / float(NATIVE_RATE)
        result["metrics"]["audioSeconds"] = round(audio_seconds, 3)
        result["metrics"]["rtf"] = round(result["metrics"]["totalMs"] / 1000.0 / audio_seconds, 3)
        result["capabilities"]["streaming"] = True

        if request.check_cancel:
            result["cancellation"] = _probe_cancellation(abi, ctx, request)
        result["metrics"]["peakRssMb"] = peak_rss_mb(rss_fn)
        result["verdict"] = VERDICT_PASS
        return result
    finally:
        if ctx is not None:
            try:
                abi.free(ctx)
                result["shutdown"]["clean"] = True
                result["shutdown"]["detail"] = "qt_free returned without error"
            except Exception as exc:  # noqa: BLE001 - teardown failure is evidence
                result["shutdown"]["clean"] = False
                result["shutdown"]["detail"] = str(exc) or repr(exc)
        if request.device in DEVICE_ENV:
            if previous_env is None:
                environ.pop("GGML_BACKEND", None)
            else:
                environ["GGML_BACKEND"] = previous_env


def _probe_cancellation(abi: Any, ctx: int, request: ProbeRequest) -> dict[str, Any]:
    """Cooperative-cancel evidence: qt_cancel_cb is polled per decode step."""
    cancel_after = request.cancel_after_ms / 1000.0
    started_at = time.perf_counter()
    requested_at: list[float] = []

    def _cancel() -> bool:
        if not requested_at and time.perf_counter() - started_at >= cancel_after:
            requested_at.append(time.perf_counter())
            return True
        return False

    stats = _collect_synth_stats()
    status = abi.synthesize(
        ctx,
        text=request.text,
        lang=request.language,
        speaker=request.speaker or None,
        instruct=request.instruct or None,
        seed=request.seed,
        on_chunk=_stream_stats_callback(stats, started_at),
        cancel=_cancel,
    )
    returned_at = time.perf_counter()
    latency_ms = round((returned_at - requested_at[0]) * 1000.0, 1) if requested_at else None
    terminal = {0: "completed", -5: "cancelled"}.get(status, f"error:{status}")
    return {
        "requested": True,
        "cancelAfterMs": request.cancel_after_ms,
        "interruptible": status == -5,
        "latencyMs": latency_ms,
        "terminal": terminal,
        "chunksBeforeStop": stats["chunks"],
        "detail": abi.last_error() if status not in (0, -5) else "",
    }


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
    except SystemExit:
        raise
    try:
        request = validate_request(args)
    except ProbeUsageError as exc:
        payload = _base_result(
            ProbeRequest(
                profile=getattr(args, "profile", "") or "",
                quantization=getattr(args, "quantization", "") or "",
                talker=getattr(args, "talker", "") or "",
                tokenizer=getattr(args, "tokenizer", "") or "",
                library=getattr(args, "library", "") or "",
            )
        )
        payload["verdict"] = VERDICT_USAGE_ERROR
        payload["errors"].append(str(exc))
        print(json.dumps(payload, allow_nan=False))
        return 2

    payload = run_probe(request)
    if request.json_out:
        out_path = Path(request.json_out)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, indent=2, allow_nan=False) + "\n", "utf-8")
    print(json.dumps(payload, allow_nan=False))
    return 0 if payload["verdict"] == VERDICT_PASS else 1


if __name__ == "__main__":
    sys.exit(main())
