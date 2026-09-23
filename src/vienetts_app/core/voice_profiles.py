"""Profile-scoped voice-clone store (FR-4.x, Task 4.1).

One app-owned catalog of enrolled clones, keyed by engine profile so a Qwen
clone can never be selected on VieNeu and vice versa (spec: "Keep VieNeu and
Qwen clone catalogs separate"). Nothing here is engine-specific: the store
persists metadata plus a *copied*, normalized reference clip, and hands the
pair to whichever engine needs it (``prompt_for`` returns the
:class:`~vienetts_app.core.qwen_engine.ClonePrompt` the model host rebuilds).

Layout (all paths app-owned, never inside site-packages)::

    <root>/clones.json          index: {"schema": 1, "clones": [...]}
    <root>/references/<name>-<id8>.wav

Rules the store enforces so no caller can bypass them:

- ``enroll`` requires the profile's own ``clone_requirements`` (reference clip,
  transcript, consent) — Qwen3-TTS Base needs all three, VieNeu needs no
  transcript, CustomVoice cannot clone at all.
- The stored reference is always a mono float32 WAV at its original sample
  rate: the user's clip is read once, validated (codec + duration), downmixed
  and re-written, so the model host never has to cope with an arbitrary file.
- The index is written atomically (temp + ``os.replace``). A failed write
  removes the reference copy it was about to publish, and a failed *removal*
  leaves the previous index intact — there is no half-enrolled clone.
- A corrupt index is moved aside (``clones.json.corrupt``) rather than
  overwritten, so the metadata is recoverable; unreadable *entries* are dropped
  with a warning and never delete the reference audio on disk.
- Enrolling the same audio content twice for the same profile and transcript is
  idempotent: the existing clone comes back and no second file is written. The
  content hash covers the decoded mono samples plus the sample rate — never
  the raw container bytes (libsndfile stamps a wall-clock timestamp into the
  PEAK chunk of every float WAV write, so identical samples written in
  different seconds differ at the byte level).

No pickle, no torch objects, no engine handles are persisted — only JSON
scalars and a WAV file.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import time
import uuid
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from vienetts_app.core.audio import read_wav, write_wav_file
from vienetts_app.core.engine_profiles import EngineId, EngineProfileError, get_capabilities
from vienetts_app.core.paths import sanitize_filename
from vienetts_app.core.qwen_engine import ClonePrompt

logger = logging.getLogger(__name__)

INDEX_FILENAME = "clones.json"
REFERENCE_DIRNAME = "references"
SCHEMA_VERSION = 1
CORRUPT_SUFFIX = ".corrupt"

# A clone's display name is also its filename stem, so keep it short and free of
# control characters; ``sanitize_filename`` handles the OS-specific hazards.
MAX_NAME_CHARS = 60

# Reference clips: long enough to carry timbre, short enough to keep the
# enrollment prompt (and its transcript) cheap to rebuild in the model host.
MIN_REFERENCE_SECONDS = 1.0
MAX_REFERENCE_SECONDS = 30.0

# The transcript is synthesized as the clone prompt's text; one IPC frame holds
# it, exactly like a synthesis segment (core/qwen_protocol.MAX_TEXT_CHARS).
MAX_TRANSCRIPT_CHARS = 2000

# Atomic writes retry ``os.replace`` on Windows sharing violations, matching
# settings.py and the artifact writer.
_REPLACE_ATTEMPTS = 4
_REPLACE_DELAY = 0.05


class CloneStoreError(RuntimeError):
    """An enrollment/removal/load failure whose message is actionable."""


@dataclass(frozen=True)
class CloneProfile:
    """One enrolled clone: immutable metadata plus its app-owned reference copy.

    ``reference_path`` is absolute for the running process; the index stores it
    relative to the store root so the whole directory can be moved.
    """

    clone_id: str
    name: str
    profile: EngineId
    transcript: str
    reference_path: Path
    content_hash: str
    duration_seconds: float
    sample_rate: int
    created_at: str
    updated_at: str

    def to_json(self, *, root: Path) -> dict[str, Any]:
        """Index entry for this clone (reference path relative to ``root``)."""
        return {
            "cloneId": self.clone_id,
            "name": self.name,
            "profile": self.profile,
            "transcript": self.transcript,
            "reference": _relative_reference(self.reference_path, root),
            "contentHash": self.content_hash,
            "durationSeconds": self.duration_seconds,
            "sampleRate": self.sample_rate,
            "createdAt": self.created_at,
            "updatedAt": self.updated_at,
        }

    @classmethod
    def from_json(cls, entry: object, *, root: Path) -> CloneProfile | None:
        """Rebuild one entry; ``None`` when it is unusable (never raises)."""
        if not isinstance(entry, dict):
            return None
        try:
            clone_id = str(entry["cloneId"])
            name = str(entry["name"])
            profile = str(entry["profile"])
            transcript = str(entry.get("transcript", ""))
            reference = str(entry["reference"])
            content_hash = str(entry["contentHash"])
            duration = float(entry["durationSeconds"])
            sample_rate = int(entry["sampleRate"])
            created_at = str(entry["createdAt"])
            updated_at = str(entry.get("updatedAt", entry["createdAt"]))
        except (KeyError, TypeError, ValueError):
            return None
        if not clone_id or not name or not content_hash or sample_rate <= 0:
            return None
        if not _is_engine_profile(profile):
            return None
        if not created_at:
            return None
        return cls(
            clone_id=clone_id,
            name=name,
            profile=profile,
            transcript=transcript,
            reference_path=_resolve_reference(reference, root),
            content_hash=content_hash,
            duration_seconds=duration,
            sample_rate=sample_rate,
            created_at=created_at,
            updated_at=updated_at,
        )


def _is_engine_profile(value: str) -> bool:
    """True only for a known profile that can actually own clones."""
    try:
        capabilities = get_capabilities(value)  # type: ignore[arg-type] - validated here
    except EngineProfileError:
        return False
    return capabilities.supports_cloning


def _resolve_reference(reference: str, root: Path) -> Path:
    """Absolute path for a stored reference (never escapes the store root).

    Index entries are always relative; an absolute or escaping path means the
    file was hand-edited, so it is treated as a bare name inside ``references``
    rather than letting the host read an arbitrary file.
    """
    candidate = Path(reference)
    if candidate.is_absolute():
        return root / REFERENCE_DIRNAME / candidate.name
    resolved = (root / candidate).resolve()
    if not resolved.is_relative_to(root.resolve()):
        return root / REFERENCE_DIRNAME / candidate.name
    return resolved


def _relative_reference(path: Path, root: Path) -> str:
    """The index form of a reference path: relative to ``root``, POSIX separators.

    The index has to survive its store directory being moved, and copied between
    machines, so the separator is fixed instead of native; ``_resolve_reference``
    reads either form back through ``Path``.
    """
    with contextlib.suppress(ValueError):
        return path.resolve().relative_to(root.resolve()).as_posix()
    return str(path)


class CloneStore:
    """The clone catalog for one app data root.

    ``root`` is created on first write. ``now`` is injectable so tests can pin
    timestamps; it must return a timezone-aware UTC datetime.
    """

    def __init__(self, root: Path, *, now: Callable[[], datetime] | None = None) -> None:
        self._root = Path(root)
        self._index_path = self._root / INDEX_FILENAME
        self._references_dir = self._root / REFERENCE_DIRNAME
        self._now = now or (lambda: datetime.now(UTC))
        self._clones: dict[str, CloneProfile] | None = None

    # ── reads ──────────────────────────────────────────────────────────────

    @property
    def root(self) -> Path:
        return self._root

    def list(self, profile: EngineId | None = None) -> tuple[CloneProfile, ...]:
        """Enrolled clones, optionally for one profile, in stable name order."""
        clones = self._entries().values()
        selected: Iterable[CloneProfile] = (
            clones if profile is None else (c for c in clones if c.profile == profile)
        )
        return tuple(sorted(selected, key=lambda clone: (clone.name.casefold(), clone.clone_id)))

    def get(self, clone_id: str) -> CloneProfile:
        """One clone by id; unknown ids raise with the enrolled names."""
        key = str(clone_id or "").strip()
        clone = self._entries().get(key)
        if clone is None:
            known = ", ".join(sorted(clone.name for clone in self._entries().values())) or "none"
            raise CloneStoreError(f"clone {key!r} is not enrolled — enrolled clones: {known}")
        return clone

    def prompt_for(self, clone_id: str) -> ClonePrompt:
        """Reference clip + transcript for the model host, or an actionable error.

        This is the resolver ``QwenEngineProvider(clone_prompt_for=...)`` wants:
        a clone whose reference file disappeared — or no longer decodes to the
        payload it enrolled with — is reported as such instead of silently
        synthesizing with the wrong voice.
        """
        clone = self.get(clone_id)
        if not clone.reference_path.is_file():
            raise CloneStoreError(
                f"the reference clip for clone {clone.name!r} is missing "
                f"({clone.reference_path}) — re-enroll it"
            )
        try:
            audio, sample_rate = read_wav(clone.reference_path)
        except Exception as exc:  # noqa: BLE001 - soundfile error taxonomy varies
            raise CloneStoreError(
                f"the reference clip for clone {clone.name!r} is unreadable ({exc}) — re-enroll it"
            ) from exc
        if audio.ndim == 2:
            audio = audio.mean(axis=1) if audio.shape[1] > 1 else audio[:, 0]
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        if _payload_digest(audio, sample_rate) != clone.content_hash:
            raise CloneStoreError(
                f"the reference clip for clone {clone.name!r} changed since enrollment "
                f"({clone.reference_path}) — re-enroll it"
            )
        return ClonePrompt(reference_path=str(clone.reference_path), transcript=clone.transcript)

    # ── writes ─────────────────────────────────────────────────────────────

    def enroll(
        self,
        *,
        name: str,
        profile: EngineId,
        reference_clip: str | Path,
        transcript: str = "",
        consent: bool = False,
    ) -> CloneProfile:
        """Validate, copy and index one clone; returns the stored profile.

        Idempotent for the same profile + clip content + transcript (the
        existing clone is returned, no second file). A different clip under a
        name already used *by that profile* is rejected.
        """
        capabilities = self._capabilities(profile)
        clean_name = self._check_name(name)
        clean_transcript = self._check_transcript(capabilities, transcript)
        self._check_consent(capabilities, consent)
        source, content_hash, audio, sample_rate = self._read_reference(reference_clip)
        duration = len(audio) / sample_rate

        existing = self._find_by_content(profile, content_hash, clean_transcript)
        if existing is not None:
            return existing
        for clone in self._entries().values():
            if clone.profile == profile and clone.name.casefold() == clean_name.casefold():
                raise CloneStoreError(
                    f"a {capabilities.label} clone named {clean_name!r} already exists — "
                    "remove it first or enroll this clip under another name"
                )

        timestamp = self._timestamp()
        clone_id = uuid.uuid4().hex
        reference_path = self._references_dir / _reference_filename(clean_name, clone_id)
        clone = CloneProfile(
            clone_id=clone_id,
            name=clean_name,
            profile=profile,
            transcript=clean_transcript,
            reference_path=reference_path,
            content_hash=content_hash,
            duration_seconds=duration,
            sample_rate=sample_rate,
            created_at=timestamp,
            updated_at=timestamp,
        )
        self._write_reference(audio, sample_rate, reference_path, source=source)
        updated = dict(self._entries())
        updated[clone_id] = clone
        try:
            self._write_index(updated)
        except Exception:
            # Never leave a reference file behind for an unindexed clone.
            with contextlib.suppress(OSError):
                reference_path.unlink(missing_ok=True)
            raise
        self._clones = updated
        logger.info("Enrolled clone %r for %s (%s)", clean_name, profile, clone_id)
        return clone

    def remove(self, clone_id: str) -> bool:
        """Drop one clone (index first, then its reference copy).

        Returns False for an unknown id. A reference file that cannot be
        deleted is left behind as an orphan rather than failing the removal
        the index already committed.
        """
        key = str(clone_id or "").strip()
        entries = self._entries()
        clone = entries.get(key)
        if clone is None:
            return False
        updated = {cid: entry for cid, entry in entries.items() if cid != key}
        self._write_index(updated)
        self._clones = updated
        try:
            clone.reference_path.unlink(missing_ok=True)
        except OSError:
            logger.warning(
                "Could not delete reference clip %s; the clone is removed from the index",
                clone.reference_path,
            )
        return True

    # ── index handling ─────────────────────────────────────────────────────

    def _entries(self) -> dict[str, CloneProfile]:
        if self._clones is None:
            self._clones = self._load()
        return self._clones

    def _load(self) -> dict[str, CloneProfile]:
        """Read the index; a corrupt file is quarantined, never overwritten."""
        if not self._index_path.is_file():
            return {}
        try:
            raw = json.loads(self._index_path.read_text(encoding="utf-8"))
            entries = raw["clones"] if isinstance(raw, dict) else None
            if not isinstance(entries, list):
                raise TypeError(f"expected {{'clones': [...]}}, got {type(raw).__name__}")
        except (OSError, json.JSONDecodeError, TypeError, KeyError) as exc:
            logger.warning(
                "Clone index %s is unreadable (%s); moving it aside", self._index_path, exc
            )
            self._quarantine_index()
            return {}
        clones: dict[str, CloneProfile] = {}
        for entry in entries:
            clone = CloneProfile.from_json(entry, root=self._root)
            if clone is None:
                logger.warning("Dropping invalid clone entry from %s", self._index_path)
                continue
            clones[clone.clone_id] = clone
        return clones

    def _quarantine_index(self) -> None:
        target = self._index_path.with_name(self._index_path.name + CORRUPT_SUFFIX)
        with contextlib.suppress(OSError):
            os.replace(self._index_path, target)

    def _write_index(self, clones: dict[str, CloneProfile]) -> None:
        """Atomically persist ``clones`` (temp file + ``os.replace``)."""
        self._root.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {
                "schema": SCHEMA_VERSION,
                "clones": [clone.to_json(root=self._root) for clone in clones.values()],
            },
            indent=2,
            sort_keys=True,
        )
        temp = self._index_path.with_name(f".{self._index_path.name}.{uuid.uuid4().hex}.tmp")
        try:
            temp.write_text(payload, encoding="utf-8")
            _replace_with_retry(temp, self._index_path)
        except OSError:
            with contextlib.suppress(OSError):
                temp.unlink(missing_ok=True)
            raise

    def _write_reference(
        self, audio: np.ndarray, sample_rate: int, destination: Path, *, source: Path
    ) -> None:
        """Write the normalized mono reference copy (temp + atomic publish)."""
        self._references_dir.mkdir(parents=True, exist_ok=True)
        # The temp name keeps the ``.wav`` suffix so the encoder can infer the
        # container from the extension.
        temp = self._references_dir / f".{destination.stem}.{uuid.uuid4().hex}.tmp.wav"
        try:
            write_wav_file(audio, temp, sample_rate)
            _replace_with_retry(temp, destination)
        except Exception as exc:
            with contextlib.suppress(OSError):
                temp.unlink(missing_ok=True)
            raise CloneStoreError(
                f"could not store the reference clip from {source} ({exc})"
            ) from exc

    # ── validation ─────────────────────────────────────────────────────────

    def _capabilities(self, profile: EngineId) -> Any:
        try:
            capabilities = get_capabilities(profile)
        except EngineProfileError as exc:
            raise CloneStoreError(str(exc)) from exc
        if not capabilities.supports_cloning:
            raise CloneStoreError(
                f"{capabilities.label} uses fixed speakers and cannot enroll clones — "
                "switch to an engine profile that supports cloning"
            )
        return capabilities

    def _check_name(self, name: str) -> str:
        clean = str(name or "").strip()
        if not clean:
            raise CloneStoreError("a clone needs a display name")
        if len(clean) > MAX_NAME_CHARS:
            raise CloneStoreError(
                f"clone names are limited to {MAX_NAME_CHARS} characters, got {len(clean)}"
            )
        if any(ord(char) < 32 for char in clean):
            raise CloneStoreError("clone names cannot contain control characters")
        return clean

    def _check_transcript(self, capabilities: Any, transcript: str) -> str:
        clean = str(transcript or "").strip()
        if "transcript" in capabilities.clone_requirements and not clean:
            raise CloneStoreError(
                f"{capabilities.label} needs the reference transcript before it can clone a voice"
            )
        if len(clean) > MAX_TRANSCRIPT_CHARS:
            raise CloneStoreError(
                f"the reference transcript is limited to {MAX_TRANSCRIPT_CHARS} characters, "
                f"got {len(clean)}"
            )
        return clean

    def _check_consent(self, capabilities: Any, consent: bool) -> None:
        if "consent" in capabilities.clone_requirements and not consent:
            raise CloneStoreError(
                "enrolling a voice clone requires the explicit consent acknowledgement"
            )

    def _read_reference(self, reference_clip: str | Path) -> tuple[Path, str, np.ndarray, int]:
        """Read, validate and downmix a reference clip; returns (path, hash, mono, rate)."""
        source = Path(reference_clip)
        if not source.is_file():
            raise CloneStoreError(f"the reference clip {source} does not exist")
        if source.suffix.lower() != ".wav":
            raise CloneStoreError(
                f"the reference clip must be a WAV file, got {source.suffix or 'no extension'!r}"
            )
        try:
            audio, sample_rate = read_wav(source)
        except Exception as exc:  # noqa: BLE001 - soundfile error taxonomy varies
            raise CloneStoreError(
                f"the reference clip {source.name} is not a readable WAV file ({exc})"
            ) from exc
        if audio.ndim == 2:
            audio = audio.mean(axis=1) if audio.shape[1] > 1 else audio[:, 0]
        audio = np.ascontiguousarray(audio, dtype=np.float32)
        content_hash = _payload_digest(audio, sample_rate)
        if sample_rate <= 0 or audio.size == 0:
            raise CloneStoreError(f"the reference clip {source.name} has no audio")
        duration = audio.size / sample_rate
        if duration < MIN_REFERENCE_SECONDS:
            raise CloneStoreError(
                f"the reference clip is {duration:.2f}s long — use at least "
                f"{MIN_REFERENCE_SECONDS:.0f}s of clean speech"
            )
        if duration > MAX_REFERENCE_SECONDS:
            raise CloneStoreError(
                f"the reference clip is {duration:.1f}s long — use at most "
                f"{MAX_REFERENCE_SECONDS:.0f}s of clean speech"
            )
        return source, content_hash, audio, sample_rate

    def _find_by_content(
        self, profile: EngineId, content_hash: str, transcript: str
    ) -> CloneProfile | None:
        for clone in self._entries().values():
            if (
                clone.profile == profile
                and clone.content_hash == content_hash
                and clone.transcript == transcript
            ):
                return clone
        return None

    def _timestamp(self) -> str:
        moment = self._now()
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        return moment.astimezone(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _payload_digest(audio: np.ndarray, sample_rate: int) -> str:
    """The enrollment content hash: decoded mono payload plus the sample rate —
    never the raw container bytes. libsndfile stamps a wall-clock timestamp
    into the PEAK chunk of every float WAV write, so byte-identical samples
    written in different seconds differ on disk and would defeat both dedup
    and the integrity check ``prompt_for`` performs.
    """
    digest = hashlib.sha256()
    digest.update(audio.tobytes())
    digest.update(int(sample_rate).to_bytes(4, "little"))
    return digest.hexdigest()


def _reference_filename(name: str, clone_id: str) -> str:
    """Deterministic, collision-free filename: sanitized name plus a unique id."""
    stem = sanitize_filename(name, max_len=MAX_NAME_CHARS, fallback="clone")
    return f"{stem}-{clone_id[:8]}.wav"


def _replace_with_retry(temp: Path, destination: Path) -> None:
    for attempt in range(_REPLACE_ATTEMPTS):
        try:
            os.replace(temp, destination)
            return
        except PermissionError:
            if attempt == _REPLACE_ATTEMPTS - 1:
                raise
            time.sleep(_REPLACE_DELAY)


__all__ = [
    "CloneProfile",
    "ClonePrompt",
    "CloneStore",
    "CloneStoreError",
    "MAX_NAME_CHARS",
    "MAX_REFERENCE_SECONDS",
    "MAX_TRANSCRIPT_CHARS",
    "MIN_REFERENCE_SECONDS",
]
