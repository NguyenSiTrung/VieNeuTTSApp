"""Incremental validated WAV artifacts (Phase 3 Task 1).

The worker writes one chunk at a time and never holds duration-sized audio;
only a closed and structurally validated file is promoted to its final name.
Failure injection uses fake writer handles — the long-artifact bounds are
proven through counters, never multi-gigabyte fixtures.
"""

from pathlib import Path

import numpy as np
import pytest
import soundfile as sf

from vienetts_app.core.artifacts import (
    ArtifactWriteError,
    IncrementalArtifactWriter,
    InteractiveArtifactStore,
    validate_wav_artifact,
)


class _FakeHandle:
    """SoundFile duck-type recording writes and injected failures."""

    def __init__(
        self,
        *,
        fail_after_writes: int | None = None,
        fail_on_close: bool = False,
    ) -> None:
        self.writes: list[tuple[tuple[int, ...], str, bool]] = []
        self.closed = False
        self._remaining = fail_after_writes
        self._fail_on_close = fail_on_close

    def write(self, chunk: np.ndarray) -> None:
        if self._remaining is not None:
            if self._remaining <= 0:
                raise OSError("injected write failure")
            self._remaining -= 1
        self.writes.append(
            (tuple(chunk.shape), str(chunk.dtype), bool(chunk.flags["C_CONTIGUOUS"]))
        )

    def close(self) -> None:
        self.closed = True
        if self._fail_on_close:
            raise OSError("injected close failure")


class _FakeWriterFactory:
    def __init__(
        self,
        *,
        fail_after_writes: int | None = None,
        fail_on_close: bool = False,
    ) -> None:
        self.handles: list[_FakeHandle] = []
        self.paths: list[Path] = []
        self._fail_after_writes = fail_after_writes
        self._fail_on_close = fail_on_close

    def __call__(self, path: Path, sample_rate: int) -> _FakeHandle:
        handle = _FakeHandle(
            fail_after_writes=self._fail_after_writes,
            fail_on_close=self._fail_on_close,
        )
        self.handles.append(handle)
        self.paths.append(Path(path))
        return handle


def _chunk_lengths(factory: _FakeWriterFactory) -> list[int]:
    return [shape[0] for handle in factory.handles for shape, _, _ in handle.writes]


def test_writer_promotes_only_after_close_and_validation(tmp_path: Path) -> None:
    destination = tmp_path / "jobs" / "abc.wav"
    writer = IncrementalArtifactWriter("abc", destination)
    assert not destination.exists()
    writer.append(np.full(480, 0.25, dtype=np.float32))
    writer.append(np.full(960, -0.5, dtype=np.float32))
    assert not destination.exists()

    artifact = writer.finalize()

    assert artifact.path == destination
    assert not writer.part_path.exists()
    assert destination.exists()
    assert (artifact.samples, artifact.sample_rate, artifact.duration_ms) == (1440, 48_000, 30)
    assert artifact.job_id == "abc"
    assert validate_wav_artifact(destination) == (1440, 48_000)


def test_append_normalizes_non_contiguous_float64_slice(tmp_path: Path) -> None:
    factory = _FakeWriterFactory()
    writer = IncrementalArtifactWriter("abc", tmp_path / "abc.wav", writer_factory=factory)
    source = np.arange(960, dtype=np.float64).reshape(480, 2)[:, 1]  # non-contiguous view

    written = writer.append(source)

    assert written == 480
    assert writer.samples_written == 480
    (handle,) = factory.handles
    assert handle.writes == [((480,), "float32", True)]
    writer.finalize()


def test_nan_and_inf_chunks_raise_without_promoting(tmp_path: Path) -> None:
    for bad in (
        np.full(16, np.nan, dtype=np.float32),
        np.full(16, np.inf, dtype=np.float32),
        np.array([0.0, float("-inf")], dtype=np.float64),
    ):
        destination = tmp_path / f"bad-{len(bad)}.wav"
        writer = IncrementalArtifactWriter("abc", destination)
        with pytest.raises(ArtifactWriteError, match="non-finite"):
            writer.append(bad)
        writer.abort()
        assert not destination.exists()


def test_non_mono_chunk_raises_without_promoting(tmp_path: Path) -> None:
    writer = IncrementalArtifactWriter("abc", tmp_path / "abc.wav")
    with pytest.raises(ArtifactWriteError, match="mono"):
        writer.append(np.zeros((16, 2), dtype=np.float32))
    writer.abort()
    assert not (tmp_path / "abc.wav").exists()


def test_finalize_with_zero_samples_raises(tmp_path: Path) -> None:
    writer = IncrementalArtifactWriter("abc", tmp_path / "abc.wav")
    with pytest.raises(ArtifactWriteError, match="no samples"):
        writer.finalize()
    assert not (tmp_path / "abc.wav").exists()


def test_write_failure_deletes_partial_and_never_promotes(tmp_path: Path) -> None:
    writer = IncrementalArtifactWriter(
        "abc",
        tmp_path / "abc.wav",
        writer_factory=_FakeWriterFactory(fail_after_writes=1),
    )
    writer.append(np.ones(10, dtype=np.float32))

    with pytest.raises(ArtifactWriteError, match="write"):
        writer.append(np.ones(10, dtype=np.float32))

    writer.abort()
    assert not writer.part_path.exists()
    assert not (tmp_path / "abc.wav").exists()


def test_preexisting_malformed_part_is_swept_for_fresh_job(tmp_path: Path) -> None:
    stale = tmp_path / "abc.part.wav"
    stale.write_bytes(b"not a wav file")
    writer = IncrementalArtifactWriter("abc", tmp_path / "abc.wav")

    # Fresh job owns the path: stale bytes are gone, replaced by a real WAV.
    assert stale.read_bytes()[:4] == b"RIFF"
    writer.append(np.ones(48, dtype=np.float32))
    artifact = writer.finalize()
    assert artifact.samples == 48


def test_close_failure_leaves_no_final_wav(tmp_path: Path) -> None:
    destination = tmp_path / "abc.wav"
    writer = IncrementalArtifactWriter(
        "abc", destination, writer_factory=_FakeWriterFactory(fail_on_close=True)
    )
    writer.append(np.ones(10, dtype=np.float32))
    with pytest.raises(ArtifactWriteError, match="close"):
        writer.finalize()

    assert not destination.exists()
    assert not writer.part_path.exists()


def test_post_close_validation_failure_leaves_no_final_wav(tmp_path: Path) -> None:
    destination = tmp_path / "abc.wav"
    writer = IncrementalArtifactWriter("abc", destination, validate=lambda _path: (0, 48_000))
    writer.append(np.ones(10, dtype=np.float32))

    with pytest.raises(ArtifactWriteError, match="mismatch"):
        writer.finalize()

    assert not destination.exists()
    assert not writer.part_path.exists()


def test_abort_is_idempotent_and_removes_everything(tmp_path: Path) -> None:
    destination = tmp_path / "abc.wav"
    writer = IncrementalArtifactWriter("abc", destination)
    writer.append(np.ones(48, dtype=np.float32))

    writer.abort()
    writer.abort()

    assert not writer.part_path.exists()
    assert not destination.exists()


def test_recording_fake_sees_original_chunk_lengths(tmp_path: Path) -> None:
    factory = _FakeWriterFactory()
    writer = IncrementalArtifactWriter("abc", tmp_path / "abc.wav", writer_factory=factory)
    writer.append(np.full(480, 0.25, dtype=np.float32))
    writer.append(np.full(960, -0.5, dtype=np.float32))
    writer.finalize()

    assert _chunk_lengths(factory) == [480, 960]


def test_append_after_finalize_raises(tmp_path: Path) -> None:
    writer = IncrementalArtifactWriter("abc", tmp_path / "abc.wav")
    writer.append(np.ones(48, dtype=np.float32))
    writer.finalize()

    with pytest.raises(ArtifactWriteError, match="closed"):
        writer.append(np.ones(48, dtype=np.float32))


def _write_wav(path: Path, frames: int, channels: int = 1, rate: int = 48_000) -> None:
    data = np.zeros((frames, channels) if channels > 1 else frames, dtype=np.float32)
    sf.write(str(path), data, rate, subtype="FLOAT", format="WAV")


def test_validate_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(ArtifactWriteError, match="missing|unreadable|not found"):
        validate_wav_artifact(tmp_path / "nope.wav")


def test_validate_rejects_zero_frame_file(tmp_path: Path) -> None:
    path = tmp_path / "empty.wav"
    _write_wav(path, 0)
    with pytest.raises(ArtifactWriteError, match="zero|empty|no frames"):
        validate_wav_artifact(path)


def test_validate_rejects_multichannel_file(tmp_path: Path) -> None:
    path = tmp_path / "stereo.wav"
    _write_wav(path, 480, channels=2)
    with pytest.raises(ArtifactWriteError, match="mono|channel"):
        validate_wav_artifact(path)


def test_validate_rejects_wrong_sample_rate(tmp_path: Path) -> None:
    path = tmp_path / "rate.wav"
    _write_wav(path, 480, rate=44_100)
    with pytest.raises(ArtifactWriteError, match="48|sample rate"):
        validate_wav_artifact(path)


def test_validate_rejects_unreadable_file(tmp_path: Path) -> None:
    path = tmp_path / "garbage.wav"
    path.write_bytes(bytes(range(256)))
    with pytest.raises(ArtifactWriteError, match="unreadable|not a|valid"):
        validate_wav_artifact(path)


def test_validate_rejects_truncated_file(tmp_path: Path) -> None:
    path = tmp_path / "cut.wav"
    _write_wav(path, 4800)
    with open(path, "r+b") as handle:
        handle.truncate(path.stat().st_size // 2)
    with pytest.raises(ArtifactWriteError, match="truncat|short|size"):
        validate_wav_artifact(path)


class TestInteractiveArtifactStore:
    def test_allocate_uses_job_scoped_path(self, tmp_path: Path) -> None:
        store = InteractiveArtifactStore(tmp_path)

        allocated = store.allocate("a" * 32)

        assert allocated == tmp_path / "artifacts" / "interactive" / ("a" * 32 + ".wav")

    @pytest.mark.parametrize("bad_id", ["a/b", "a\\b", "..", "", "a.wav/b"])
    def test_allocate_rejects_path_separators(self, tmp_path: Path, bad_id: str) -> None:
        store = InteractiveArtifactStore(tmp_path)
        with pytest.raises(ValueError, match="job"):
            store.allocate(bad_id)

    def test_protected_artifact_survives_cleanup(self, tmp_path: Path) -> None:
        from vienetts_app.core.artifacts import SynthesisArtifact

        store = InteractiveArtifactStore(tmp_path)
        path = store.allocate("job-1")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"RIFF....")
        artifact = SynthesisArtifact(
            job_id="job-1", path=path, sample_rate=48_000, samples=1, duration_ms=0
        )

        store.protect(artifact)
        assert store.remove_if_unprotected(artifact) is False
        assert path.exists()

        store.release(artifact)
        assert store.remove_if_unprotected(artifact) is True
        assert not path.exists()
        assert store.remove_if_unprotected(artifact) is False

    def test_cleanup_removes_only_orphaned_parts(self, tmp_path: Path) -> None:
        store = InteractiveArtifactStore(tmp_path)
        interactive = tmp_path / "artifacts" / "interactive"
        interactive.mkdir(parents=True)
        (interactive / "a.part.wav").write_bytes(b"x")
        (interactive / "b.part.wav").write_bytes(b"y")
        kept = interactive / "c.wav"
        kept.write_bytes(b"z")

        assert store.cleanup_orphaned_parts() == 2
        assert kept.exists()
        assert store.cleanup_orphaned_parts() == 0

    def test_double_release_is_safe(self, tmp_path: Path) -> None:
        from vienetts_app.core.artifacts import SynthesisArtifact

        store = InteractiveArtifactStore(tmp_path)
        artifact = SynthesisArtifact(
            job_id="job-1",
            path=tmp_path / "missing.wav",
            sample_rate=48_000,
            samples=1,
            duration_ms=0,
        )
        store.release(artifact)  # never protected: must not go negative
        assert store.remove_if_unprotected(artifact) is False

    def test_remove_if_unprotected_handles_oserror(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from vienetts_app.core.artifacts import SynthesisArtifact

        store = InteractiveArtifactStore(tmp_path)
        path = store.allocate("job-locked")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"RIFF....")
        artifact = SynthesisArtifact(
            job_id="job-locked", path=path, sample_rate=48_000, samples=1, duration_ms=0
        )

        def fake_unlink(self_path: Path) -> None:
            raise PermissionError("[WinError 32] File locked")
        monkeypatch.setattr(Path, "unlink", fake_unlink)
        # Must not raise PermissionError; should return False so retry can happen later
        assert store.remove_if_unprotected(artifact) is False
        assert path.exists()
