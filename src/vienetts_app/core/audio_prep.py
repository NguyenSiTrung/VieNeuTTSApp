"""Shared 16 kHz mono WAV preparation for ASR and diarization.

Whisper and the diarization models both take 16 kHz mono input. ASR used to
write ``input_16k.wav`` (whole-file linear interpolation: aliased, and the full
track as float32 in RAM) while diarization wrote ``source_wav_16k.wav`` through
ffmpeg, so every WAV input was converted twice. :func:`ensure_source_16k` now
produces ONE ``source_16k.wav`` plus a JSON sidecar per media cache dir:

* soundfile-readable lossless audio is read in bounded blocks, downmixed and
  resampled by ``soxr.ResampleStream`` (anti-aliased, ``VHQ``), then streamed to
  disk as PCM_16 — memory stays flat whatever the input length;
* video and lossy audio go through ffmpeg (:func:`extract_video_audio`), which
  keeps its gapless/encoder-delay handling;
* the WAV is promoted atomically (``.part.wav`` → ``os.replace``) and is reused
  while its sidecar still names the same source path, size and mtime;
* legacy ``source_wav_16k.wav`` / ``input_16k.wav`` files whose sidecar matches
  are renamed into place instead of reconverted. A rename keeps size and
  mtime, so ASR/diarization cache keys written against the legacy name stay
  valid through :func:`cache_key_paths`.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import threading
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from vienetts_app.core.audio import StreamingWavWriter
from vienetts_app.core.video_dub import VideoDubCancelled, VideoDubError, extract_video_audio

SAMPLE_RATE = 16_000
SOURCE_16K_FILENAME = "source_16k.wav"
#: Pre-unification per-tool names, in adoption order: diarization's ffmpeg
#: output (anti-aliased) before ASR's linear-interpolation output.
LEGACY_16K_FILENAMES = ("source_wav_16k.wav", "input_16k.wav")
#: Bump when the conversion changes so older outputs reconvert. Sidecars from
#: before versioning carry no ``format`` and are adopted as-is.
SIDECAR_FORMAT = 1
#: Frames per read — 64 Ki frames is 512 KiB of float32 stereo.
_BLOCK_FRAMES = 1 << 16
#: Containers libsndfile decodes sample-exactly. Lossy formats (MP3, Ogg)
#: stay on ffmpeg, which trims encoder delay/padding.
_SOUNDFILE_FORMATS = frozenset({"WAV", "WAVEX", "RF64", "W64", "AIFF", "CAF", "FLAC"})
_REPLACE_ATTEMPTS = 5
_REPLACE_DELAY_S = 0.1

logger = logging.getLogger(__name__)

_LOCKS_GUARD = threading.Lock()
_LOCKS: dict[str, threading.Lock] = {}


class AudioPrepError(RuntimeError):
    """The 16 kHz WAV could not be produced; the message is user-facing."""


class AudioPrepCancelled(AudioPrepError):
    """``cancel_check`` stopped the conversion; no partial file is left behind."""


def is_16k_mono_wav(path: Path) -> bool:
    """True when ``path`` is readable audio at 16 kHz with one channel."""
    try:
        import soundfile as sf

        info = sf.info(str(path))
    except Exception:  # noqa: BLE001 - unreadable means "needs conversion"
        return False
    return int(info.samplerate) == SAMPLE_RATE and int(info.channels) == 1


def cache_key_paths(wav_path: Path) -> tuple[Path, ...]:
    """``wav_path`` first, then the legacy names an adopted ``source_16k.wav`` had.

    Cache keys embed the WAV path plus its size and mtime; a key written
    against ``input_16k.wav`` still matches once that file is renamed here.
    """
    if wav_path.name != SOURCE_16K_FILENAME:
        return (wav_path,)
    return (wav_path, *(wav_path.with_name(name) for name in LEGACY_16K_FILENAMES))


def ensure_source_16k(
    media_path: str | Path,
    cache_dir: str | Path,
    *,
    data_dir: Path | None = None,
    cancel_check: Callable[[], bool] | None = None,
    progress_cb: Callable[[float], None] | None = None,
) -> Path:
    """Return the 16 kHz mono WAV for ``media_path``, converting at most once.

    A ``.wav`` already at 16 kHz mono is returned as-is (nothing is written
    beside the user's file). Otherwise the result is ``cache_dir /
    source_16k.wav``. ``progress_cb`` receives the conversion fraction (0..1)
    only when a conversion runs. Raises :class:`AudioPrepCancelled` when
    ``cancel_check`` turns true and :class:`AudioPrepError` on failure.
    """
    src = Path(media_path)
    if src.suffix.lower() == ".wav" and is_16k_mono_wav(src):
        return src
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    target = cache / SOURCE_16K_FILENAME
    with _lock_for(target):
        identity = _source_identity(src)
        if _reusable(target, identity, allow_unversioned_mtime=True):
            return target
        if identity is not None:
            for name in LEGACY_16K_FILENAMES:
                legacy = cache / name
                if _reusable(legacy, identity) and _adopt(legacy, target, identity):
                    _drop_legacy(cache)
                    return target
        _convert(src, target, data_dir=data_dir, cancel_check=cancel_check, progress_cb=progress_cb)
        if identity is not None:
            _write_sidecar(target, {**identity, "format": SIDECAR_FORMAT})
        _drop_legacy(cache)
        return target


def _lock_for(target: Path) -> threading.Lock:
    """One lock per target, so concurrent ASR + diarization convert only once."""
    key = os.path.normcase(str(target.resolve()))
    with _LOCKS_GUARD:
        return _LOCKS.setdefault(key, threading.Lock())


def _sidecar_path(wav: Path) -> Path:
    return wav.with_name(wav.name + ".json")


def _source_identity(src: Path) -> dict[str, Any] | None:
    try:
        stat = src.stat()
    except OSError:
        return None
    return {"source": str(src.resolve()), "size": stat.st_size, "mtime_ns": stat.st_mtime_ns}


def _reusable(
    wav: Path, identity: dict[str, Any] | None, *, allow_unversioned_mtime: bool = False
) -> bool:
    """True when ``wav`` is a 16 kHz mono WAV whose sidecar matches ``identity``.

    ``allow_unversioned_mtime`` keeps pre-sidecar ``source_16k.wav`` caches
    working: with no sidecar at all, a WAV at least as new as its source is
    trusted, as both old helpers did.
    """
    try:
        if not wav.is_file() or wav.stat().st_size <= 0 or not is_16k_mono_wav(wav):
            return False
    except OSError:
        return False
    if identity is None:
        return False
    try:
        stored = json.loads(_sidecar_path(wav).read_text(encoding="utf-8"))
    except FileNotFoundError:
        if not allow_unversioned_mtime:
            return False
        try:
            return wav.stat().st_mtime_ns >= int(identity["mtime_ns"])
        except OSError:
            return False
    except (OSError, ValueError):
        return False
    if not isinstance(stored, dict):
        return False
    version = stored.get("format")  # absent: written before versioning, still valid
    if version is not None and version != SIDECAR_FORMAT:
        return False
    return all(stored.get(key) == value for key, value in identity.items())


def _adopt(legacy: Path, target: Path, identity: dict[str, Any]) -> bool:
    """Rename a matching legacy WAV (and its sidecar) to the shared name.

    Returns False when the WAV cannot be moved (e.g. a Windows reader holds
    it); the caller then converts afresh instead.
    """
    target_sidecar = _sidecar_path(target)
    with contextlib.suppress(OSError):
        target_sidecar.unlink(missing_ok=True)
    try:
        _replace(legacy, target)
    except OSError:
        return False
    try:
        _replace(_sidecar_path(legacy), target_sidecar)
    except OSError:
        _write_sidecar(target, identity)  # unversioned, like the content it describes
    return True


def _drop_legacy(cache: Path) -> None:
    """Delete superseded per-tool copies once ``source_16k.wav`` is current."""
    for name in LEGACY_16K_FILENAMES:
        legacy = cache / name
        for path in (legacy, _sidecar_path(legacy)):
            with contextlib.suppress(OSError):
                path.unlink(missing_ok=True)


def _convert(
    src: Path,
    target: Path,
    *,
    data_dir: Path | None,
    cancel_check: Callable[[], bool] | None,
    progress_cb: Callable[[float], None] | None,
) -> None:
    if cancel_check is not None and cancel_check():
        raise AudioPrepCancelled("Đã hủy chuẩn bị âm thanh 16 kHz.")
    # A stale sidecar must never describe the WAV that replaces it.
    with contextlib.suppress(OSError):
        _sidecar_path(target).unlink(missing_ok=True)
    if progress_cb is not None:
        progress_cb(0.0)
    if _soundfile_format(src) in _SOUNDFILE_FORMATS:
        part = target.with_name(f".{target.stem}.{uuid.uuid4().hex}.part.wav")
        try:
            _resample_to_16k(src, part, cancel_check=cancel_check, progress_cb=progress_cb)
            _replace(part, target)
            return
        except AudioPrepCancelled:
            raise
        except Exception as exc:  # noqa: BLE001 - undecodable data: let ffmpeg try
            logger.warning("16 kHz conversion of %s failed, trying ffmpeg: %s", src.name, exc)
        finally:
            with contextlib.suppress(OSError):
                part.unlink(missing_ok=True)
    try:
        extract_video_audio(
            src,
            target,
            sample_rate=SAMPLE_RATE,
            mono=True,
            data_dir=data_dir,
            cancel_check=cancel_check,
        )
    except VideoDubCancelled as exc:
        raise AudioPrepCancelled("Đã hủy chuẩn bị âm thanh 16 kHz.") from exc
    except VideoDubError as exc:
        raise AudioPrepError(str(exc)) from exc
    if progress_cb is not None:
        progress_cb(1.0)


def _soundfile_format(src: Path) -> str:
    try:
        import soundfile as sf

        return str(sf.info(str(src)).format)
    except Exception:  # noqa: BLE001 - video containers etc. are not libsndfile input
        return ""


def _resample_to_16k(
    src: Path,
    part: Path,
    *,
    cancel_check: Callable[[], bool] | None = None,
    progress_cb: Callable[[float], None] | None = None,
) -> None:
    """Stream ``src`` into a 16 kHz mono PCM_16 WAV at ``part``, block by block.

    Memory is one input block plus soxr's filter state, whatever the length.
    The streamed output is sample-aligned with a one-shot resample (soxr
    compensates its filter delay) and the flush emits the exact tail.
    """
    import soundfile as sf

    with sf.SoundFile(str(src)) as reader, StreamingWavWriter(part, SAMPLE_RATE) as writer:
        in_rate = int(reader.samplerate)
        total = max(0, int(reader.frames))
        stream = None
        if in_rate != SAMPLE_RATE:
            import soxr

            stream = soxr.ResampleStream(in_rate, SAMPLE_RATE, 1, dtype="float32", quality="VHQ")
        done = 0
        reported = 0.0
        for block in reader.blocks(blocksize=_BLOCK_FRAMES, dtype="float32", always_2d=True):
            if cancel_check is not None and cancel_check():
                raise AudioPrepCancelled("Đã hủy chuẩn bị âm thanh 16 kHz.")
            if block.shape[1] == 1:
                mono = np.ascontiguousarray(block[:, 0])
            else:
                mono = block.mean(axis=1, dtype=np.float32)
            _write_clipped(writer, mono if stream is None else stream.resample_chunk(mono))
            done += block.shape[0]
            if progress_cb is not None and total > 0 and done / total - reported >= 0.01:
                reported = min(1.0, done / total)
                progress_cb(reported)
        if stream is not None:
            _write_clipped(writer, stream.resample_chunk(np.zeros(0, dtype=np.float32), last=True))
    if progress_cb is not None and reported < 1.0:
        progress_cb(1.0)


def _write_clipped(writer: StreamingWavWriter, samples: np.ndarray) -> None:
    # The resampler can overshoot full scale on clipped masters; PCM_16 must not wrap.
    if samples.size:
        writer.write(np.clip(samples, -1.0, 1.0))


def _replace(part: Path, target: Path) -> None:
    """``os.replace`` with a short retry for Windows readers holding ``target``."""
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(part, target)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_DELAY_S)


def _write_sidecar(wav: Path, payload: dict[str, Any]) -> None:
    sidecar = _sidecar_path(wav)
    tmp = sidecar.with_name(f".{sidecar.name}.{uuid.uuid4().hex}.part")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        _replace(tmp, sidecar)
    except OSError:
        # The WAV is still valid; without a sidecar it is only reused by mtime.
        with contextlib.suppress(OSError):
            tmp.unlink(missing_ok=True)
