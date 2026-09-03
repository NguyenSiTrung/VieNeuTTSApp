"""Incremental validated WAV artifacts (Phase 3 Task 1).

Normal synthesis never holds duration-sized audio: the worker appends one
validated chunk at a time to a private ``<stem>.part.wav`` and only a closed,
structurally validated file is promoted to its final name with ``os.replace``
(same directory, same volume, Windows-safe ordering). Only handles plus an
integer sample counter are retained — chunks are never aggregated or emitted.
"""

import contextlib
import os
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import numpy as np

from vienetts_app.core.audio import DEFAULT_SAMPLE_RATE


def _sf() -> Any:
    """soundfile, imported on first use (never startup work)."""
    import soundfile as sf

    return sf


class ArtifactWriteError(RuntimeError):
    """Chunk validation, incremental write, or promotion failed."""


@dataclass(frozen=True)
class SynthesisArtifact:
    """Validated synthesis result. Carries file metadata, never PCM."""

    job_id: str
    path: Path
    sample_rate: int
    samples: int
    duration_ms: int
    timeline_path: Path | None = None
    envelope_path: Path | None = None


def _normalize_chunk(value: object) -> np.ndarray:
    samples = np.asarray(value)
    if samples.ndim != 1:
        raise ArtifactWriteError("audio chunk must be mono")
    if samples.size == 0:
        return np.empty(0, dtype=np.float32)
    if not np.isfinite(samples).all():
        raise ArtifactWriteError("audio chunk contains non-finite samples")
    return np.ascontiguousarray(samples, dtype=np.float32)


def _default_writer_factory(path: Path, sample_rate: int) -> Any:
    return _sf().SoundFile(
        str(path),
        mode="w",
        samplerate=sample_rate,
        channels=1,
        subtype="FLOAT",
        format="WAV",
    )


def _wav_declared_total_bytes(path: Path) -> int | None:
    """Total file bytes the RIFF header declares, or None if unparseable.

    A truncated file keeps its original header while losing tail bytes, so a
    short actual size against the declared size proves truncation without
    decoding any PCM. Returns None for non-RIFF layouts (libsndfile already
    validated the audible shape); those skip this check.
    """
    try:
        with open(path, "rb") as handle:
            header = handle.read(12)
            if len(header) < 12 or header[0:4] != b"RIFF" or header[8:12] != b"WAVE":
                return None
            riff_size = int.from_bytes(header[4:8], "little")
            return 8 + riff_size
    except OSError:
        return None


def validate_wav_artifact(path: str | Path) -> tuple[int, int]:
    """Return ``(frames, sample_rate)`` from SoundFile metadata only.

    Rejects a missing, zero-frame, multichannel, non-48-kHz, unreadable, or
    truncated file. Never decodes PCM; messages carry no user text or path.
    """
    candidate = Path(path)
    if not candidate.is_file():
        raise ArtifactWriteError("artifact file is missing")
    try:
        info = _sf().info(str(candidate))
    except Exception as exc:  # noqa: BLE001 - libsndfile error taxonomy varies
        raise ArtifactWriteError("artifact file is unreadable") from exc
    if info.channels != 1:
        raise ArtifactWriteError("artifact must be mono")
    if info.samplerate != DEFAULT_SAMPLE_RATE:
        raise ArtifactWriteError("artifact sample rate must be 48000 Hz")
    if info.frames <= 0:
        raise ArtifactWriteError("artifact has no frames")
    # libsndfile clamps the reported frame count to the bytes actually
    # present, so truncation is proven against the header's declared total.
    declared = _wav_declared_total_bytes(candidate)
    if declared is not None:
        try:
            actual = candidate.stat().st_size
        except OSError as exc:
            raise ArtifactWriteError("artifact file is unreadable") from exc
        if actual < declared:
            raise ArtifactWriteError("artifact file is truncated")
    return int(info.frames), int(info.samplerate)


class IncrementalArtifactWriter:
    """Append-once validated WAV writer with atomic promotion."""

    def __init__(
        self,
        job_id: str,
        destination: Path,
        *,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        writer_factory: Callable[[Path, int], Any] | None = None,
        validate: Callable[[Path], tuple[int, int]] | None = None,
    ) -> None:
        self._job_id = job_id
        self._destination = Path(destination)
        self._sample_rate = sample_rate
        # An injected writer double produces no file: close is still honored
        # (failure injection), but validation and promotion are file
        # operations and apply only to the real factory. Production never
        # injects a factory, so real files are always validated + promoted.
        self._validate = validate or validate_wav_artifact
        self._has_file = writer_factory is None
        self._part_path = self._destination.parent / (self._destination.stem + ".part.wav")
        self._destination.parent.mkdir(parents=True, exist_ok=True)
        # A fresh job owns its part path; stale bytes from an aborted run
        # must never be appended to.
        with contextlib.suppress(OSError):
            self._part_path.unlink(missing_ok=True)
        factory = writer_factory or _default_writer_factory
        self._handle: Any | None = factory(self._part_path, sample_rate)
        self._samples_written = 0

    @property
    def part_path(self) -> Path:
        return self._part_path

    @property
    def samples_written(self) -> int:
        return self._samples_written

    def append(self, samples: object) -> int:
        if self._handle is None:
            raise ArtifactWriteError("artifact writer is closed")
        chunk = _normalize_chunk(samples)
        try:
            self._handle.write(chunk)
        except Exception as exc:
            raise ArtifactWriteError("failed to write audio chunk") from exc
        self._samples_written += int(chunk.size)
        return int(chunk.size)

    def finalize(self) -> SynthesisArtifact:
        if self._samples_written == 0:
            self._discard()
            raise ArtifactWriteError("artifact has no samples")
        handle, self._handle = self._handle, None
        try:
            if handle is not None:
                try:
                    handle.close()
                except Exception as exc:
                    raise ArtifactWriteError("failed to close artifact file") from exc
            if self._has_file:
                frames, _rate = self._validate(self._part_path)
                if frames != self._samples_written:
                    raise ArtifactWriteError("artifact frame count mismatch")
                os.replace(self._part_path, self._destination)
        except ArtifactWriteError:
            self._discard()
            raise
        except Exception as exc:  # noqa: BLE001 - replace failure modes
            self._discard()
            raise ArtifactWriteError("failed to finalize artifact") from exc
        return SynthesisArtifact(
            job_id=self._job_id,
            path=self._destination,
            sample_rate=self._sample_rate,
            samples=self._samples_written,
            duration_ms=int(self._samples_written * 1000 / self._sample_rate),
        )

    def abort(self) -> None:
        """Idempotent: close and remove every trace of the partial artifact."""
        self._discard()

    def _discard(self) -> None:
        handle, self._handle = self._handle, None
        if handle is not None:
            with contextlib.suppress(Exception):
                handle.close()
        with contextlib.suppress(OSError):
            self._part_path.unlink(missing_ok=True)


class InteractiveArtifactStore:
    """Job-scoped artifact paths with playback-release protection.

    ``root`` is the application data directory; artifacts live under
    ``artifacts/interactive/<job_id>.wav``, outside user export directories.
    The protection count keeps the current artifact alive while a file player
    holds it (Windows cannot delete an open file); cleanup only removes
    unprotected files after playback releases them.
    """

    def __init__(self, root: Path) -> None:
        self._directory = Path(root) / "artifacts" / "interactive"
        self._protected: dict[str, int] = {}

    @property
    def directory(self) -> Path:
        return self._directory

    def allocate(self, job_id: str) -> Path:
        if (
            not job_id
            or job_id in (".", "..")
            or "/" in job_id
            or "\\" in job_id
            or Path(job_id).name != job_id
        ):
            raise ValueError(f"invalid job id for artifact allocation: {job_id!r}")
        return self._directory / f"{job_id}.wav"

    def protect(self, artifact: SynthesisArtifact) -> None:
        key = str(artifact.path)
        self._protected[key] = self._protected.get(key, 0) + 1

    def release(self, artifact: SynthesisArtifact) -> None:
        key = str(artifact.path)
        self._protected[key] = max(0, self._protected.get(key, 0) - 1)

    def remove_if_unprotected(self, artifact: SynthesisArtifact) -> bool:
        """Unlink the artifact unless protected or already gone."""
        if self._protected.get(str(artifact.path), 0) > 0:
            return False
        path = Path(artifact.path)
        if not path.exists():
            return False
        path.unlink()
        return True

    def cleanup_orphaned_parts(self) -> int:
        """Remove abandoned ``*.part.wav`` files; never touches finals."""
        if not self._directory.is_dir():
            return 0
        removed = 0
        for part in sorted(self._directory.glob("*.part.wav")):
            with contextlib.suppress(OSError):
                part.unlink(missing_ok=True)
                removed += 1
        return removed
