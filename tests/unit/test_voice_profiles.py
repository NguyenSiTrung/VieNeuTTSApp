"""CloneStore: profile-scoped clone persistence (Task 4.1).

The store is exercised with real WAV files on disk (mono, stereo, short, long,
non-WAV bytes) and with an injected clock, so every validation, dedup, restart
and atomic-failure path is deterministic and needs no engine or model.
"""

from __future__ import annotations

import json
import os
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path

import numpy as np
import pytest

from vienetts_app.core.audio import read_wav, write_wav_file
from vienetts_app.core.engine_profiles import QWEN_BASE, QWEN_CUSTOM, VIENEU
from vienetts_app.core.voice_profiles import (
    INDEX_FILENAME,
    MAX_NAME_CHARS,
    MAX_TRANSCRIPT_CHARS,
    CloneProfile,
    CloneStore,
    CloneStoreError,
)

_EPOCH = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)


def make_clip(
    path: Path, *, seconds: float = 3.0, sample_rate: int = 24_000, tone: float = 0.2
) -> Path:
    """A real mono float WAV of ``seconds`` at ``sample_rate``."""
    samples = int(seconds * sample_rate)
    audio = np.full(samples, tone, dtype=np.float32)
    return write_wav_file(audio, path, sample_rate)


@pytest.fixture
def store(tmp_path: Path) -> CloneStore:
    return CloneStore(tmp_path / "clones", now=lambda: _EPOCH)


@pytest.fixture
def clip(tmp_path: Path) -> Path:
    return make_clip(tmp_path / "ref.wav")


def enroll(store: CloneStore, clip: Path, **overrides) -> CloneProfile:
    kwargs: dict = {
        "name": "Ngọc Anh",
        "profile": QWEN_BASE,
        "reference_clip": clip,
        "transcript": "Xin chào, đây là giọng đọc mẫu.",
        "consent": True,
    }
    kwargs.update(overrides)
    return store.enroll(**kwargs)


# ── enrollment and persistence ────────────────────────────────────────────


def test_enroll_copies_the_reference_and_indexes_metadata(store, clip: Path) -> None:
    clone = enroll(store, clip)

    assert clone.clone_id
    assert clone.profile == QWEN_BASE
    assert clone.name == "Ngọc Anh"
    assert clone.transcript == "Xin chào, đây là giọng đọc mẫu."
    assert clone.sample_rate == 24_000
    assert clone.duration_seconds == pytest.approx(3.0)
    assert clone.created_at == "2026-09-21T12:00:00Z"
    assert clone.updated_at == clone.created_at
    assert len(clone.content_hash) == 64

    # The stored clip is an app-owned copy, mono float WAV, same rate.
    assert clone.reference_path.is_file()
    assert clone.reference_path != clip
    assert clone.reference_path.parent == store.root / "references"
    audio, rate = read_wav(clone.reference_path)
    assert rate == 24_000
    assert audio.ndim == 1
    assert audio.size == int(3.0 * 24_000)

    index = json.loads((store.root / INDEX_FILENAME).read_text(encoding="utf-8"))
    assert index["schema"] == 1
    (entry,) = index["clones"]
    assert entry["cloneId"] == clone.clone_id
    assert entry["reference"].startswith("references/")
    assert entry["reference"].endswith(".wav")
    assert entry["contentHash"] == clone.content_hash


def test_enrolled_clones_survive_a_restart(store, clip: Path) -> None:
    clone = enroll(store, clip)

    reopened = CloneStore(store.root, now=lambda: _EPOCH)
    (listed,) = reopened.list()

    assert listed.clone_id == clone.clone_id
    assert listed.name == clone.name
    assert listed.transcript == clone.transcript
    assert listed.reference_path == clone.reference_path
    assert listed.content_hash == clone.content_hash
    assert reopened.get(clone.clone_id) == clone
    assert reopened.prompt_for(clone.clone_id).transcript == clone.transcript
    assert reopened.prompt_for(clone.clone_id).reference_path == str(clone.reference_path)


def test_list_filters_by_profile_and_sorts_by_name(store, tmp_path: Path) -> None:
    zoe = enroll(store, make_clip(tmp_path / "a.wav", tone=0.1), name="Zoe")
    an = enroll(store, make_clip(tmp_path / "b.wav", tone=0.2), name="An")
    binh = enroll(store, make_clip(tmp_path / "c.wav", tone=0.3), name="Binh")

    assert [clone.name for clone in store.list()] == ["An", "Binh", "Zoe"]
    assert [clone.clone_id for clone in store.list(profile=QWEN_BASE)] == [
        an.clone_id,
        binh.clone_id,
        zoe.clone_id,
    ]
    assert store.list(profile=VIENEU) == ()
    assert store.list(profile=QWEN_CUSTOM) == ()


def test_the_same_name_is_free_on_another_profile(store, tmp_path: Path) -> None:
    base = enroll(store, make_clip(tmp_path / "a.wav", tone=0.1), name="Shared")
    vieneu = enroll(
        store,
        make_clip(tmp_path / "b.wav", tone=0.2),
        name="Shared",
        profile=VIENEU,
        transcript="",
    )

    assert base.name == vieneu.name == "Shared"
    assert base.clone_id != vieneu.clone_id
    assert base.reference_path != vieneu.reference_path


def test_enrolling_the_same_clip_twice_is_idempotent(store, clip: Path) -> None:
    first = enroll(store, clip)
    second = enroll(store, clip, name="Other name")

    assert second == first
    assert len(store.list()) == 1
    assert len(list((store.root / "references").glob("*.wav"))) == 1


def test_the_same_clip_with_another_transcript_is_a_new_clone(store, clip: Path) -> None:
    first = enroll(store, clip, name="Một", transcript="Một")
    second = enroll(store, clip, name="Hai", transcript="Hai")

    assert second.clone_id != first.clone_id
    assert second.reference_path != first.reference_path
    assert len(store.list()) == 2
    # Each clone owns its own copy: removing one must not break the other.
    assert store.remove(first.clone_id) is True
    assert second.reference_path.is_file()
    assert [clone.clone_id for clone in store.list()] == [second.clone_id]


def test_removal_drops_the_entry_and_the_reference_copy(store, clip: Path) -> None:
    clone = enroll(store, clip)

    assert store.remove(clone.clone_id) is True
    assert store.remove(clone.clone_id) is False
    assert store.list() == ()
    assert not clone.reference_path.exists()
    assert json.loads((store.root / INDEX_FILENAME).read_text(encoding="utf-8"))["clones"] == []
    assert CloneStore(store.root).list() == ()


def test_removal_tolerates_a_missing_reference_file(store, clip: Path) -> None:
    clone = enroll(store, clip)
    clone.reference_path.unlink()

    assert store.remove(clone.clone_id) is True
    assert store.list() == ()


# ── capability-driven validation ──────────────────────────────────────────


def test_customvoice_cannot_enroll(store, clip: Path) -> None:
    with pytest.raises(CloneStoreError, match="fixed speakers and cannot enroll clones"):
        enroll(store, clip, profile=QWEN_CUSTOM)


def test_unknown_profile_is_rejected(store, clip: Path) -> None:
    with pytest.raises(CloneStoreError, match="unknown engine profile"):
        enroll(store, clip, profile="qwen_omni")  # type: ignore[arg-type]


def test_base_requires_a_transcript(store, clip: Path) -> None:
    with pytest.raises(CloneStoreError, match="needs the reference transcript"):
        enroll(store, clip, transcript="   ")


def test_vieneu_does_not_require_a_transcript(store, clip: Path) -> None:
    clone = enroll(store, clip, profile=VIENEU, transcript="")

    assert clone.transcript == ""
    assert clone.profile == VIENEU


def test_consent_is_required(store, clip: Path) -> None:
    with pytest.raises(CloneStoreError, match="explicit consent acknowledgement"):
        enroll(store, clip, consent=False)


@pytest.mark.parametrize(
    ("name", "message"),
    [
        pytest.param("", "needs a display name", id="empty"),
        pytest.param("   ", "needs a display name", id="blank"),
        pytest.param("x" * (MAX_NAME_CHARS + 1), "limited to 60 characters", id="too-long"),
        pytest.param("bad\nname", "control characters", id="control-char"),
    ],
)
def test_name_validation(store, clip: Path, name: str, message: str) -> None:
    with pytest.raises(CloneStoreError, match=message):
        enroll(store, clip, name=name)


def test_transcript_length_is_bounded(store, clip: Path) -> None:
    with pytest.raises(CloneStoreError, match=f"limited to {MAX_TRANSCRIPT_CHARS} characters"):
        enroll(store, clip, transcript="x" * (MAX_TRANSCRIPT_CHARS + 1))


def test_a_name_collision_inside_a_profile_is_rejected(store, tmp_path: Path) -> None:
    enroll(store, make_clip(tmp_path / "a.wav", tone=0.1), name="Trùng")

    with pytest.raises(CloneStoreError, match="already exists"):
        enroll(store, make_clip(tmp_path / "b.wav", tone=0.2), name="trùng")

    assert len(store.list()) == 1
    assert len(list((store.root / "references").glob("*.wav"))) == 1


# ── reference clip validation ─────────────────────────────────────────────


def test_missing_reference_clip_is_rejected(store, tmp_path: Path) -> None:
    with pytest.raises(CloneStoreError, match="does not exist"):
        enroll(store, tmp_path / "absent.wav")


def test_non_wav_reference_is_rejected(store, tmp_path: Path) -> None:
    mp3 = tmp_path / "ref.mp3"
    mp3.write_bytes(b"ID3\x04\x00\x00\x00\x00\x00\x00")

    with pytest.raises(CloneStoreError, match="must be a WAV file"):
        enroll(store, mp3)


def test_a_wav_that_is_not_readable_audio_is_rejected(store, tmp_path: Path) -> None:
    fake = tmp_path / "ref.wav"
    fake.write_bytes(b"not really a RIFF file")

    with pytest.raises(CloneStoreError, match="not a readable WAV file"):
        enroll(store, fake)


@pytest.mark.parametrize(
    ("seconds", "message"),
    [
        pytest.param(0.5, "use at least 1s", id="too-short"),
        pytest.param(31.0, "use at most 30s", id="too-long"),
    ],
)
def test_reference_duration_is_bounded(store, tmp_path: Path, seconds: float, message: str) -> None:
    with pytest.raises(CloneStoreError, match=message):
        enroll(store, make_clip(tmp_path / "ref.wav", seconds=seconds))


def test_a_stereo_reference_is_downmixed(store, tmp_path: Path) -> None:
    import soundfile as sf

    path = tmp_path / "stereo.wav"
    stereo = np.stack(
        [np.full(48_000, 0.5, dtype=np.float32), np.full(48_000, 0.25, dtype=np.float32)],
        axis=1,
    )
    sf.write(str(path), stereo, 24_000, subtype="FLOAT")

    clone = enroll(store, path)
    audio, rate = read_wav(clone.reference_path)

    assert rate == 24_000
    assert audio.ndim == 1
    assert float(audio.mean()) == pytest.approx(0.375, abs=1e-6)


def test_the_content_hash_tracks_the_source_bytes(store, tmp_path: Path) -> None:
    first = enroll(store, make_clip(tmp_path / "a.wav", tone=0.1), name="One")
    second = enroll(store, make_clip(tmp_path / "b.wav", tone=0.1), name="Two")

    # Same tone, same length, same rate -> identical bytes -> dedup, not a copy.
    assert second == first
    assert len(list((store.root / "references").glob("*.wav"))) == 1


# ── lookups ───────────────────────────────────────────────────────────────


def test_get_reports_the_enrolled_names(store, clip: Path) -> None:
    enroll(store, clip, name="Ngọc Anh")

    with pytest.raises(CloneStoreError, match="enrolled clones: Ngọc Anh"):
        store.get("nope")


def test_prompt_for_reports_a_missing_reference(store, clip: Path) -> None:
    clone = enroll(store, clip)
    clone.reference_path.unlink()

    prompt = None
    with pytest.raises(CloneStoreError, match="re-enroll it"):
        prompt = store.prompt_for(clone.clone_id)
    assert prompt is None
    # The metadata stays listed: the clone is broken, not forgotten.
    assert store.list() == (clone,)


# ── corruption and atomic failures ────────────────────────────────────────


def test_a_corrupt_index_is_quarantined_not_overwritten(store, clip: Path) -> None:
    enroll(store, clip)
    store.root.joinpath(INDEX_FILENAME).write_text("{not json", encoding="utf-8")

    reopened = CloneStore(store.root, now=lambda: _EPOCH)

    assert reopened.list() == ()
    quarantined = store.root / (INDEX_FILENAME + ".corrupt")
    assert quarantined.read_text(encoding="utf-8") == "{not json"
    # The reference audio is untouched, and enrolling again works.
    assert len(list((store.root / "references").glob("*.wav"))) == 1
    assert enroll(reopened, clip, name="Mới").name == "Mới"


def test_unreadable_entries_are_dropped_without_touching_audio(store, clip: Path) -> None:
    clone = enroll(store, clip)
    index_path = store.root / INDEX_FILENAME
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    payload["clones"].extend(
        [
            {"name": "no id"},
            "not an object",
            {**payload["clones"][0], "profile": "qwen_omni"},
            {**payload["clones"][0], "profile": QWEN_CUSTOM},
            {**payload["clones"][0], "sampleRate": 0},
            {**payload["clones"][0], "reference": ""},
            {**payload["clones"][0], "createdAt": ""},
        ]
    )
    index_path.write_text(json.dumps(payload), encoding="utf-8")

    reopened = CloneStore(store.root)

    assert [entry.clone_id for entry in reopened.list()] == [clone.clone_id]
    assert clone.reference_path.is_file()


def test_an_absolute_or_escaping_reference_stays_inside_the_store(store, clip: Path) -> None:
    clone = enroll(store, clip)
    index_path = store.root / INDEX_FILENAME
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    payload["clones"][0]["reference"] = "../../../etc/passwd"
    index_path.write_text(json.dumps(payload), encoding="utf-8")

    reopened = CloneStore(store.root)
    (entry,) = reopened.list()

    assert entry.reference_path == (store.root / "references" / "passwd").resolve()
    assert entry.clone_id == clone.clone_id


def test_a_failed_index_write_leaves_no_reference_copy(store, clip: Path, monkeypatch) -> None:
    real_replace = os.replace

    def flaky(source, destination, *args, **kwargs):
        if Path(destination).name == INDEX_FILENAME:
            raise OSError("disk full")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky)

    with pytest.raises(OSError, match="disk full"):
        enroll(store, clip)

    assert store.list() == ()
    assert (
        not (store.root / "references").exists()
        or list((store.root / "references").iterdir()) == []
    )


def test_a_failed_index_write_on_removal_keeps_the_clone(store, clip: Path, monkeypatch) -> None:
    clone = enroll(store, clip)
    index_path = store.root / INDEX_FILENAME
    before = index_path.read_text(encoding="utf-8")

    def boom(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)

    with pytest.raises(OSError, match="disk full"):
        store.remove(clone.clone_id)

    assert index_path.read_text(encoding="utf-8") == before
    assert clone.reference_path.is_file()
    assert [entry.clone_id for entry in CloneStore(store.root).list()] == [clone.clone_id]


def test_an_unwritable_store_root_reports_the_failure(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where the store directory should be", encoding="utf-8")
    store = CloneStore(blocked)
    clip = make_clip(tmp_path / "ref.wav")

    with pytest.raises(OSError):
        enroll(store, clip)
    assert store.list() == ()


# ── defensive paths (index tampering, IO failures, retries) ───────────────


def test_an_absolute_reference_path_is_rebased_into_the_store(store, clip: Path) -> None:
    clone = enroll(store, clip)
    index_path = store.root / INDEX_FILENAME
    payload = json.loads(index_path.read_text(encoding="utf-8"))
    payload["clones"][0]["reference"] = "/etc/passwd"
    index_path.write_text(json.dumps(payload), encoding="utf-8")

    (entry,) = CloneStore(store.root).list()

    assert entry.reference_path == (store.root / "references" / "passwd").resolve()
    assert entry.clone_id == clone.clone_id


def test_to_json_falls_back_to_an_absolute_reference_outside_the_root(
    store, clip: Path, tmp_path: Path
) -> None:
    clone = enroll(store, clip)
    foreign = replace(clone, reference_path=tmp_path / "elsewhere" / "ref.wav")

    entry = foreign.to_json(root=store.root)

    assert entry["reference"] == str(tmp_path / "elsewhere" / "ref.wav")


def test_a_naive_clock_is_treated_as_utc(tmp_path: Path, clip: Path) -> None:
    store = CloneStore(tmp_path / "clones", now=lambda: datetime(2026, 9, 21, 12, 0, 0))

    assert enroll(store, clip).created_at == "2026-09-21T12:00:00Z"


def test_an_index_without_a_clone_list_is_quarantined(store, clip: Path) -> None:
    store.root.mkdir(parents=True, exist_ok=True)
    (store.root / INDEX_FILENAME).write_text('{"clones": {"not": "a list"}}', encoding="utf-8")

    reopened = CloneStore(store.root)

    assert reopened.list() == ()
    assert (store.root / (INDEX_FILENAME + ".corrupt")).is_file()


def test_a_failed_reference_copy_is_reported_and_changes_nothing(
    store, clip: Path, monkeypatch
) -> None:
    def boom(*_args, **_kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr("vienetts_app.core.voice_profiles.write_wav_file", boom)

    with pytest.raises(CloneStoreError, match="could not store the reference clip"):
        enroll(store, clip)

    assert store.list() == ()
    assert list((store.root / "references").iterdir()) == []


def test_an_unreadable_reference_clip_is_reported(store, clip: Path, monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise OSError("permission denied")

    monkeypatch.setattr(Path, "read_bytes", boom)

    with pytest.raises(CloneStoreError, match="could not read the reference clip"):
        enroll(store, clip)


def test_a_reference_clip_without_audio_is_rejected(store, tmp_path: Path) -> None:
    import soundfile as sf

    empty = tmp_path / "empty.wav"
    with sf.SoundFile(str(empty), "w", samplerate=24_000, channels=1, subtype="FLOAT"):
        pass  # a valid 0-frame WAV

    with pytest.raises(CloneStoreError, match="has no audio"):
        enroll(store, empty)


def test_removal_survives_a_reference_that_cannot_be_deleted(
    store, clip: Path, monkeypatch
) -> None:
    clone = enroll(store, clip)
    real_unlink = Path.unlink

    def flaky(self, *args, **kwargs):
        if self == clone.reference_path:
            raise OSError("file is in use")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky)

    assert store.remove(clone.clone_id) is True
    assert store.list() == ()
    assert clone.reference_path.is_file()  # left as an orphan, not resurrected


def test_a_transient_sharing_violation_is_retried(store, clip: Path, monkeypatch) -> None:
    real_replace = os.replace
    calls: list[str] = []

    def flaky(source, destination, *args, **kwargs):
        calls.append(Path(destination).name)
        if len(calls) == 1:
            raise PermissionError("sharing violation")
        return real_replace(source, destination, *args, **kwargs)

    monkeypatch.setattr(os, "replace", flaky)

    clone = enroll(store, clip)

    assert clone.reference_path.is_file()
    assert store.list() == (clone,)
    assert len(calls) == 3  # one retry for the reference, then the index


def test_a_persistent_sharing_violation_is_reported(store, clip: Path, monkeypatch) -> None:
    attempts: list[str] = []

    def always_busy(*_args, **_kwargs):
        attempts.append("replace")
        raise PermissionError("file is locked by another process")

    monkeypatch.setattr(os, "replace", always_busy)

    with pytest.raises(CloneStoreError, match="could not store the reference clip"):
        enroll(store, clip)

    assert len(attempts) == 4  # every retry was used before giving up
    assert store.list() == ()
    assert list((store.root / "references").iterdir()) == []
