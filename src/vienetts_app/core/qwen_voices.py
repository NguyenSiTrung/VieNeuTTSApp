"""Qwen Base reference-audio cloning adapter (Phase 4 Task 3).

VieNeu voices live in ``voices.json``; Qwen Base reference voices live
alongside them under an engine-isolated subdirectory so the two registries
never mix. Each enrolled voice keeps its reference clip, the transcript the
official Qwen3-TTS API requires (``ref_text``), and a sidecar recording the
engine and consent decision (FR-3.6 consent gate is enforced at the door).
"""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path

import numpy as np

from vienetts_app.core.paths import sanitize_filename

logger = logging.getLogger(__name__)


class QwenVoiceError(ValueError):
    """Actionable cloning-store failure (missing/invalid reference, no consent)."""


QWEN_REF_MIN_SECONDS = 3.0  # official Qwen3-TTS cloning floor
QWEN_REF_MAX_SECONDS = 8.0  # keeps enrolled clips preview-light
QWEN_BASE_VOICES_SUBDIR = "qwen_base"
_REF_AUDIO_FILENAME = "reference"
_REF_TEXT_FILENAME = "ref_text.txt"
_META_FILENAME = "meta.json"


def _engine_root(voices_dir: str | Path, engine: str) -> Path:
    return Path(voices_dir) / engine


def _voice_dir(voices_dir: str | Path, name: str, engine: str) -> Path:
    return _engine_root(voices_dir, engine) / sanitize_filename(name.strip())


def _decode_clip(path: Path) -> tuple[np.ndarray, int]:
    try:
        import soundfile as sf  # noqa: PLC0415 - optional hobby dep, lazy like audio._sf
    except ImportError as exc:
        raise QwenVoiceError(
            "cannot read the reference clip — audio support is unavailable"
        ) from exc
    try:
        audio, rate = sf.read(str(path), always_2d=False)
    except Exception as exc:
        raise QwenVoiceError(
            f"cannot decode {path.name} as audio — pick a WAV/MP3/FLAC clip"
        ) from exc
    mono = np.asarray(audio, dtype=np.float64)
    if mono.size == 0:
        raise QwenVoiceError(f"{path.name} is empty — pick a 3-8 s voice clip")
    if mono.ndim == 2:
        mono = mono.mean(axis=1)
    samples = np.ascontiguousarray(mono, dtype=np.float32)
    if not np.all(np.isfinite(samples)):
        raise QwenVoiceError(f"{path.name} has invalid samples — pick another clip")
    return samples, int(rate)


def validate_reference_clip(path: str | Path) -> tuple[np.ndarray, int]:
    """Decode a Qwen reference clip and enforce the 3-8 s enrollment window."""
    clip = Path(path)
    if not clip.is_file():
        raise QwenVoiceError(f"reference clip {clip} does not exist — pick a file first")
    audio, rate = _decode_clip(clip)
    seconds = len(audio) / rate if rate > 0 else 0.0
    if seconds < QWEN_REF_MIN_SECONDS:
        raise QwenVoiceError(
            f"reference clip is {seconds:.1f} s — record at least "
            f"{QWEN_REF_MIN_SECONDS:.0f} s for a stable clone"
        )
    if seconds > QWEN_REF_MAX_SECONDS:
        raise QwenVoiceError(
            f"reference clip is {seconds:.1f} s — trim it under "
            f"{QWEN_REF_MAX_SECONDS:.0f} s before enrolling"
        )
    return audio, rate


def save_reference_voice(
    voices_dir: str | Path,
    name: str,
    clip_path: str | Path,
    ref_text: str,
    *,
    engine: str = "qwen_base",
    consent: bool = False,
) -> Path:
    """Enroll a Qwen Base reference voice (audio + transcript + consent sidecar)."""
    clean_name = (name or "").strip()
    if not clean_name:
        raise QwenVoiceError("voice name must not be blank")
    if not consent:
        raise QwenVoiceError("voice cloning needs consent — acknowledge the consent prompt first")
    transcript = (ref_text or "").strip()
    if not transcript:
        raise QwenVoiceError(
            "Qwen Base needs the reference transcript (ref_text) — "
            "type what the clip says so the clone can match it"
        )
    validate_reference_clip(clip_path)
    dest = _voice_dir(voices_dir, clean_name, engine)
    dest.mkdir(parents=True, exist_ok=True)
    try:
        target = dest / (_REF_AUDIO_FILENAME + Path(clip_path).suffix.lower())
        tmp_clip = dest / (target.name + ".part")
        shutil.copyfile(clip_path, tmp_clip)
        tmp_clip.replace(target)
        (dest / _REF_TEXT_FILENAME).write_text(transcript + "\n", encoding="utf-8")
        meta = {
            "engine": engine,
            "name": clean_name,
            "consent": True,
            "ref_text_chars": len(transcript),
        }
        tmp_meta = dest / (_META_FILENAME + ".part")
        tmp_meta.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_meta.replace(dest / _META_FILENAME)
    except QwenVoiceError:
        raise
    except Exception as exc:
        raise QwenVoiceError(f"could not save reference voice {clean_name!r}: {exc}") from exc
    logger.info("enrolled Qwen reference voice %r", clean_name)
    return target


def _read_store(name: str, dest: Path) -> tuple[Path, str]:
    try:
        meta = json.loads((dest / _META_FILENAME).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise QwenVoiceError(f"reference voice {name!r} is incomplete — enroll it again") from exc
    if not isinstance(meta, dict) or meta.get("name") != name:
        raise QwenVoiceError(f"reference voice {name!r} is corrupt — enroll it again")
    clips = [p for p in dest.glob(_REF_AUDIO_FILENAME + ".*") if p.is_file()]
    transcript_file = dest / _REF_TEXT_FILENAME
    if not clips or not transcript_file.is_file():
        raise QwenVoiceError(
            f"reference voice {name!r} is missing its audio or transcript — enroll it again"
        )
    try:
        transcript = transcript_file.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise QwenVoiceError(f"reference voice {name!r} is unreadable — enroll it again") from exc
    if not transcript:
        raise QwenVoiceError(f"reference voice {name!r} lost its transcript — enroll it again")
    return clips[0], transcript


def load_reference_voice(
    voices_dir: str | Path, name: str, engine: str = "qwen_base"
) -> tuple[Path, str]:
    """Return ``(clip path, transcript)`` for an enrolled Qwen reference voice."""
    clean_name = (name or "").strip()
    dest = _voice_dir(voices_dir, clean_name or " ", engine)
    if not dest.is_dir():
        raise QwenVoiceError(
            f"no Qwen reference voice named {clean_name!r} — enroll it from a 3-8 s clip first"
        )
    return _read_store(clean_name, dest)


def list_reference_voices(voices_dir: str | Path, engine: str = "qwen_base") -> list[str]:
    """Names enrolled under one engine's isolated store (sorted, VieNeu excluded)."""
    root = _engine_root(voices_dir, engine)
    if not root.is_dir():
        return []
    names: list[str] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            meta = json.loads((child / _META_FILENAME).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(meta, dict) and meta.get("engine") == engine and meta.get("name"):
            names.append(str(meta["name"]))
    return sorted(names)


def remove_reference_voice(voices_dir: str | Path, name: str, engine: str = "qwen_base") -> None:
    """Drop an enrolled Qwen reference voice (audio + transcript + sidecar)."""
    clean_name = (name or "").strip()
    dest = _voice_dir(voices_dir, clean_name or " ", engine)
    if not dest.is_dir():
        raise QwenVoiceError(f"no Qwen reference voice named {clean_name!r} — nothing to remove")
    shutil.rmtree(dest, ignore_errors=False)
    logger.info("removed Qwen reference voice %r", clean_name)
