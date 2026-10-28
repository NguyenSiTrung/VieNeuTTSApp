"""core/subtitle_project.py + audio.StreamingWavWriter: streamed SRT renders.

A subtitle track is built cue by cue and written straight to disk, so these
tests pin three things: the writer never buffers the whole track, the workspace
round-trips its inputs and stats, and a cached render is reused exactly when the
fingerprint still matches.
"""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from vienetts_app.core.align import FitPolicy
from vienetts_app.core.audio import StreamingWavWriter, read_wav
from vienetts_app.core.subtitle_project import (
    PROJECT_VERSION,
    SubtitleProjectError,
    SubtitleProjectStore,
    SubtitleTrackRenderer,
    UnitClipCache,
    build_project,
    export_srt_file,
    project_id_for,
    render_fingerprint,
    voice_file_stamp,
)
from vienetts_app.core.subtitles import parse_cues
from vienetts_app.core.timeline import SegmentSpan, Timeline

# 1 kHz keeps one millisecond equal to one frame, so expectations read as ms.
SR = 1_000

SAMPLE_SRT = (
    "1\n00:00:00,000 --> 00:00:02,000\nHello world.\n\n"
    "2\n00:00:02,500 --> 00:00:04,000\nSecond cue here.\n\n"
    "3\n00:00:05,000 --> 00:00:06,500\nThird cue.\n"
)


def clip(ms: int, value: float = 0.5) -> np.ndarray:
    return np.full(ms, value, dtype=np.float32)


def make_store(tmp_path: Path) -> SubtitleProjectStore:
    return SubtitleProjectStore(tmp_path / "subtitles")


def make_project(
    tmp_path: Path,
    *,
    policy: FitPolicy | None = None,
    sample_rate: int = SR,
    context: Any = None,
):
    return build_project(
        "/media/movie.srt",
        parse_cues(SAMPLE_SRT),
        policy or FitPolicy.dub(rate_cap=1.5),
        sample_rate=sample_rate,
        voice_key="voice-a",
        context=context,
    )


def render_context(profile: str = "vieneu", **overrides: Any) -> Any:
    """One engine identity for provenance tests (never a real model)."""
    from vienetts_app.core.synthesis_context import context_for

    kwargs: dict[str, Any] = {"language": "vi" if profile == "vieneu" else "zh"}
    kwargs.update(overrides)
    return context_for(profile, **kwargs)


def render_three(store: SubtitleProjectStore, project) -> object:
    with SubtitleTrackRenderer(store, project) as renderer:
        renderer.add_clip(0, clip(1_500))
        renderer.add_clip(1, clip(2_000))  # 2000 ms into a 1500 ms window
        renderer.add_silent_cue(2)
        return renderer.finish()


def spy_commit(store: SubtitleProjectStore, monkeypatch, *, fail: str = "") -> list[str]:
    """Record the store's commit steps in call order; ``fail`` makes one crash."""
    calls: list[str] = []

    def spied(name: str) -> Any:
        real = getattr(store, name)

        def step(*args: Any, **kwargs: Any) -> Any:
            calls.append(name)
            if name == fail:
                raise SubtitleProjectError(f"simulated crash in {name}")
            return real(*args, **kwargs)

        return step

    for name in ("save_timeline", "save", "promote_track"):
        monkeypatch.setattr(store, name, spied(name))
    return calls


# ── StreamingWavWriter ───────────────────────────────────────────────────────


def test_streaming_writer_contract(tmp_path) -> None:
    path = tmp_path / "track.wav"
    writer = StreamingWavWriter(path, SR).open()
    assert writer.write(clip(500, 1.0)) == 500
    assert writer.write_silence(1_000) == 1_000
    assert writer.write(clip(500, -1.0)) == 500
    assert writer.frames == 2_000
    writer.close()

    data, rate = read_wav(path)
    assert rate == SR
    assert data.size == 2_000
    np.testing.assert_allclose(data[:500], 1.0, atol=1e-3)  # PCM_16 quantizes
    np.testing.assert_allclose(data[500:1_500], 0.0, atol=1e-3)
    np.testing.assert_allclose(data[1_500:], -1.0, atol=1e-3)

    # More than one bounded silence chunk: the writer must not allocate it whole.
    frames = StreamingWavWriter.SILENCE_CHUNK_FRAMES * 2 + 7
    writer = StreamingWavWriter(tmp_path / "gap.wav", SR).open()
    assert writer.write_silence(frames) == frames
    writer.close()
    data, _ = read_wav(tmp_path / "gap.wav")
    assert data.size == frames
    assert not data.any()

    writer = StreamingWavWriter(tmp_path / "t.wav", SR)
    writer.open()
    writer.open()  # idempotent
    writer.write(clip(10))
    writer.close()
    writer.close()  # idempotent
    assert writer.closed is True
    with pytest.raises(ValueError):
        writer.open()
    # Empty blocks are a no-op and never open the file.
    fresh = StreamingWavWriter(tmp_path / "empty.wav", SR)
    assert fresh.write(np.zeros(0, dtype=np.float32)) == 0
    assert fresh.frames == 0


def test_writer_abort_and_flush_failures(tmp_path, monkeypatch) -> None:
    with pytest.raises(ValueError):
        StreamingWavWriter(tmp_path / "t.wav", 0)

    path = tmp_path / "t.wav"
    writer = StreamingWavWriter(path, SR).open()
    writer.write(clip(100))
    writer.abort()
    assert not path.exists()

    # A failed close must not strand the partial file: the unlink still runs.
    path = tmp_path / "t.wav"
    writer = StreamingWavWriter(path, SR).open()
    writer.write(clip(100))
    monkeypatch.setattr(
        writer, "close", lambda: (_ for _ in ()).throw(OSError("simulated close failure"))
    )
    with pytest.raises(OSError, match="simulated close failure"):
        writer.abort()
    assert not path.exists()
    assert writer.closed is True
    assert writer._file is None

    monkeypatch.undo()

    path = tmp_path / "t.wav"
    writer = StreamingWavWriter(path, SR).open()
    writer.write(clip(100))
    monkeypatch.setattr(writer._file, "flush", lambda: (_ for _ in ()).throw(OSError("flush boom")))
    with pytest.raises(OSError, match="flush boom"):
        writer.abort()
    assert not path.exists()
    assert writer.closed is True
    assert writer._file is None

    monkeypatch.undo()

    path = tmp_path / "t.wav"
    writer = StreamingWavWriter(path, SR).open()
    writer.write(clip(100))
    soundfile_obj = writer._file
    monkeypatch.setattr(
        soundfile_obj, "flush", lambda: (_ for _ in ()).throw(OSError("flush boom"))
    )
    with pytest.raises(OSError, match="flush boom"):
        writer.close()
    assert writer.closed is True
    assert writer._file is None
    assert getattr(soundfile_obj, "_file", None) is None


def test_writer_exit_contract(tmp_path, monkeypatch) -> None:
    # An abort() cleanup failure must never mask the error that raised it.
    path = tmp_path / "t.wav"
    writer = StreamingWavWriter(path, SR).open()
    writer.write(clip(100))
    monkeypatch.setattr(
        writer, "abort", lambda: (_ for _ in ()).throw(OSError("simulated abort failure"))
    )
    with pytest.raises(RuntimeError, match="original"), writer:
        raise RuntimeError("original")

    monkeypatch.undo()

    # A failed flush/close on a NORMAL exit still fails the operation.
    path = tmp_path / "t.wav"
    writer = StreamingWavWriter(path, SR).open()
    writer.write(clip(100))
    monkeypatch.setattr(
        writer, "close", lambda: (_ for _ in ()).throw(OSError("simulated close failure"))
    )
    with pytest.raises(OSError, match="simulated close failure"), writer:
        pass

    monkeypatch.undo()

    good = tmp_path / "good.wav"
    with StreamingWavWriter(good, SR) as writer:
        writer.write(clip(100))
    assert good.exists()

    bad = tmp_path / "bad.wav"
    with pytest.raises(RuntimeError), StreamingWavWriter(bad, SR) as writer:
        writer.write(clip(100))
        raise RuntimeError("boom")
    assert not bad.exists()


# ── fingerprint + id ─────────────────────────────────────────────────────────


def test_identity_and_id() -> None:
    cues = parse_cues(SAMPLE_SRT)
    base = render_fingerprint(cues, FitPolicy.dub(), SR, "v1")
    assert base == "050f95d486dc7faf27b71ac36a83ccb38c69feacb07c385394f312632b5521a1"
    assert base == render_fingerprint(cues, FitPolicy.dub(), SR, "v1")
    assert base != render_fingerprint(cues, FitPolicy.dub(rate_cap=1.4), SR, "v1")
    assert base != render_fingerprint(cues, FitPolicy.transcript(), SR, "v1")
    assert base != render_fingerprint(cues, FitPolicy.dub(), SR, "v2")
    assert base != render_fingerprint(cues, FitPolicy.dub(), 8_000, "v1")
    assert base != render_fingerprint(cues[:1], FitPolicy.dub(), SR, "v1")

    cues = parse_cues(SAMPLE_SRT)
    assert project_id_for("/a/movie.srt", cues) == project_id_for("/b/movie.srt", cues)
    assert project_id_for("/a/movie.srt", cues) != project_id_for("/a/other.srt", cues)


# ── build_project ────────────────────────────────────────────────────────────


def test_build_project(tmp_path) -> None:
    with pytest.raises(SubtitleProjectError):
        build_project("/media/movie.srt", [], FitPolicy.dub())

    project = make_project(tmp_path)
    assert project.id == project_id_for("/media/movie.srt", project.cues)
    assert project.title == "movie"
    assert project.cue_count == 3
    assert project.rendered is False
    assert project.created_at
    assert project.fingerprint == render_fingerprint(
        project.cues, project.policy, project.sample_rate, project.voice_key
    )


# ── store ────────────────────────────────────────────────────────────────────


def test_store_io(tmp_path) -> None:
    store = make_store(tmp_path)
    project = make_project(tmp_path)
    store.save(project)
    assert "llmGlossary" not in json.loads(store.project_path(project.id).read_text())
    with_glossary = replace(project, llm_glossary="x" * 5000)
    store.save(with_glossary)
    loaded = store.require(project.id)
    assert loaded.llm_glossary == "x" * 4000
    assert loaded.fingerprint == project.fingerprint
    store.save(project)

    loaded = store.load(project.id)
    assert loaded == project
    assert store.require(project.id) == project

    tmp_path = tmp_path / "case-b"
    tmp_path.mkdir(parents=True, exist_ok=True)

    store = make_store(tmp_path)
    project = make_project(tmp_path)
    result = render_three(store, project)

    loaded = store.require(project.id)
    assert loaded.rendered is True
    assert loaded.total_ms == result.total_ms
    assert loaded.stats == result.stats
    assert loaded.cues == project.cues
    assert loaded.policy == project.policy
    # The retimed SRT persists, so export works from a cached render.
    assert loaded.adjusted
    assert [c.start_ms for c in loaded.adjusted] == [0, 2_500, 5_000]


def test_store_load_guards(tmp_path) -> None:
    store = make_store(tmp_path)
    assert store.load("missing") is None

    project = make_project(tmp_path)
    store.save(project)

    # Corrupt JSON.
    store.project_path(project.id).write_text("{not json", encoding="utf-8")
    assert store.load(project.id) is None

    # Wrong version.
    store.project_path(project.id).write_text(
        json.dumps({"version": PROJECT_VERSION + 1}), encoding="utf-8"
    )
    assert store.load(project.id) is None

    # A cue that violates the model (end < start) is not silently repaired.
    store.save(project)  # restore a valid payload first
    payload = json.loads(store.project_path(project.id).read_text(encoding="utf-8"))
    payload["cues"][0]["endMs"] = -1
    store.project_path(project.id).write_text(json.dumps(payload), encoding="utf-8")
    assert store.load(project.id) is None

    tmp_path = tmp_path / "case-b"
    tmp_path.mkdir(parents=True, exist_ok=True)

    store = make_store(tmp_path)
    with pytest.raises(SubtitleProjectError):
        store.require("nope")

    tmp_path = tmp_path / "case-b"
    tmp_path.mkdir(parents=True, exist_ok=True)

    store = make_store(tmp_path)
    older = build_project(
        "/media/older.srt",
        parse_cues(SAMPLE_SRT),
        FitPolicy.dub(),
        created_at="2024-01-01T00:00:00+00:00",
    )
    newer = build_project(
        "/media/newer.srt",
        parse_cues(SAMPLE_SRT.replace("Hello", "Hi")),
        FitPolicy.dub(),
        created_at="2025-01-01T00:00:00+00:00",
    )
    store.save(older)
    store.save(newer)
    (store.root / "junk").mkdir(parents=True)  # no project.json

    assert [p.id for p in store.list_projects()] == [newer.id, older.id]


def test_store_remove_and_render_tracking(tmp_path) -> None:
    store = make_store(tmp_path)
    store.remove("missing")  # no raise
    project = make_project(tmp_path)
    store.save(project)
    store.remove(project.id)
    assert not store.project_dir(project.id).exists()

    tmp_path = tmp_path / "case-b"
    tmp_path.mkdir(parents=True, exist_ok=True)

    store = make_store(tmp_path)
    project = make_project(tmp_path)
    assert store.needs_render(project) is True  # no track yet
    render_three(store, project)
    assert store.needs_render(project) is False  # cached and matching

    changed_policy = build_project(
        project.source_path, project.cues, FitPolicy.dub(rate_cap=1.2), sample_rate=SR
    )
    assert store.needs_render(changed_policy) is True


def test_store_security(tmp_path) -> None:
    store = make_store(tmp_path)
    store.root.mkdir(parents=True)
    assert store.load("../outside") is None
    assert store.load("not-a-project-id") is None
    assert store.load("") is None

    tmp_path = tmp_path / "case-b"
    tmp_path.mkdir(parents=True, exist_ok=True)

    store = make_store(tmp_path)
    project = make_project(tmp_path)
    store.save(project)
    payload = json.loads(store.project_path(project.id).read_text(encoding="utf-8"))
    payload["version"] = "abc"
    store.project_path(project.id).write_text(json.dumps(payload), encoding="utf-8")
    assert store.load(project.id) is None

    # A payload claiming another identity (or a traversal path) must never
    # be handed back under the requested id.
    tmp_path = tmp_path / "case-b"
    tmp_path.mkdir(parents=True, exist_ok=True)

    store = make_store(tmp_path)
    project = make_project(tmp_path)
    store.save(project)
    payload = json.loads(store.project_path(project.id).read_text(encoding="utf-8"))
    payload["id"] = "../outside"
    store.project_path(project.id).write_text(json.dumps(payload), encoding="utf-8")
    assert store.load(project.id) is None

    tmp_path = tmp_path / "case-b"
    tmp_path.mkdir(parents=True, exist_ok=True)

    store = make_store(tmp_path)
    store.root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x", encoding="utf-8")
    store.remove("../outside")  # invalid id → no-op, not root/../outside
    assert (outside / "keep.txt").is_file()


def test_cached_render_contract(tmp_path, monkeypatch) -> None:
    store = make_store(tmp_path)
    project = make_project(tmp_path)
    assert store.cached_render(project) is None  # nothing stored
    store.save(project)
    assert store.cached_render(project) is None  # saved but not rendered
    render_three(store, project)
    cached = store.cached_render(project)
    assert cached is not None
    assert cached.rendered is True
    assert cached.fingerprint == project.fingerprint
    assert cached.adjusted
    assert cached.total_ms > 0

    tmp_path = tmp_path / "case-b"
    tmp_path.mkdir(parents=True, exist_ok=True)

    store = make_store(tmp_path)
    project = make_project(tmp_path)
    render_three(store, project)

    changed = build_project(
        project.source_path, project.cues, FitPolicy.dub(rate_cap=1.2), sample_rate=SR
    )
    assert store.cached_render(changed) is None  # fingerprint moved

    # A rendered project.json without its timeline is not a reusable render.
    store.timeline_path(project.id).unlink()
    assert store.cached_render(project) is None
    assert store.needs_render(project) is True

    # A parseable timeline that does not line up with the stored cues (empty,
    # or a different segment count) would karaoke-highlight the wrong spans —
    # it is not a reusable render.
    tmp_path = tmp_path / "case-b"
    tmp_path.mkdir(parents=True, exist_ok=True)

    store = make_store(tmp_path)
    project = make_project(tmp_path)
    render_id = render_three(store, project).render_id

    store.save_timeline(project.id, Timeline(()), render_id=render_id)
    assert store.cached_render(project) is None
    assert store.needs_render(project) is True

    store.save_timeline(
        project.id,
        Timeline((SegmentSpan(0, 5, 0, 1_000),)),  # 1 span for 3 cues
        render_id=render_id,
    )
    assert store.cached_render(project) is None
    assert store.needs_render(project) is True

    # FR-5.7: finish commits timeline → project.json → track.wav, all three
    # stamped with one render_id.
    tmp_path = tmp_path / "case-c"
    tmp_path.mkdir(parents=True, exist_ok=True)

    store = make_store(tmp_path)
    project = make_project(tmp_path)
    calls = spy_commit(store, monkeypatch)
    result = render_three(store, project)
    assert calls == ["save_timeline", "save", "promote_track"]
    fresh = make_store(tmp_path)
    timeline_json = json.loads(fresh.timeline_path(project.id).read_text(encoding="utf-8"))
    assert result.render_id
    assert timeline_json["renderId"] == result.render_id
    assert fresh.require(project.id).render_id == result.render_id
    assert fresh.track_render_id(project.id) == result.render_id
    assert fresh.cached_render(project) is not None
    track = fresh.wav_path(project.id).read_bytes()

    # A crash right after the timeline write (project.json and track.wav are
    # still the previous render's), or after the project.json write, leaves a
    # mixed state that is refused, never half-adopted. track.wav is untouched
    # and no part file lingers.
    for crash_in in ("save", "promote_track"):
        crashing = make_store(tmp_path)
        spy_commit(crashing, monkeypatch, fail=crash_in)
        with pytest.raises(SubtitleProjectError, match=crash_in):
            render_three(crashing, project)
        fresh = make_store(tmp_path)
        assert fresh.cached_render(project) is None
        assert fresh.load_timeline(project.id) is None  # not this track's timeline
        assert fresh.wav_path(project.id).read_bytes() == track
        assert not list(fresh.project_dir(project.id).glob("*.part.wav"))

    # Legacy: a render committed before render_id existed carries no id
    # anywhere. All three agree (blank), so it stays reusable on its
    # fingerprint alone (backward-compat NFR)...
    store = make_store(tmp_path)
    render_three(store, project)
    for path in (store.project_path(project.id), store.timeline_path(project.id)):
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload.pop("renderId")
        path.write_text(json.dumps(payload), encoding="utf-8")
    store.render_stamp_path(project.id).unlink()
    legacy = store.cached_render(project)
    assert legacy is not None and legacy.render_id == ""
    # ...while a legacy track under a stamped timeline is a crash mid-commit.
    legacy_timeline = store.load_timeline(project.id)
    assert legacy_timeline is not None
    store.save_timeline(project.id, legacy_timeline, render_id="f" * 32)
    assert store.cached_render(project) is None


def test_track_parts_and_timeline(tmp_path) -> None:
    store = make_store(tmp_path)
    project = make_project(tmp_path)
    first = store.prepare_track_part(project.id)
    second = store.prepare_track_part(project.id)
    assert first != second
    assert first.parent == store.project_dir(project.id)
    assert first.suffix == ".wav"

    first.write_bytes(b"part")
    promoted = store.promote_track(project.id, first)
    assert promoted == store.wav_path(project.id)
    assert promoted.read_bytes() == b"part"
    assert not first.exists()

    tmp_path = tmp_path / "case-b"
    tmp_path.mkdir(parents=True, exist_ok=True)

    store = make_store(tmp_path)
    project = make_project(tmp_path)

    store.save(project)
    store.save_timeline(project.id, Timeline(()))
    assert store.load_timeline(project.id) is None  # no track.wav yet
    render_three(store, project)
    assert store.load_timeline(project.id) is not None


# ── renderer ─────────────────────────────────────────────────────────────────


def test_renderer_placement(tmp_path) -> None:
    store = make_store(tmp_path)
    project = make_project(tmp_path)
    result = render_three(store, project)

    data, rate = read_wav(result.wav_path)
    assert rate == SR
    assert data.size == result.total_ms == 5_000
    # Cue 0: 1500 ms of audio at 0, then silence until cue 1 at 2500.
    assert np.count_nonzero(data[0:1_500]) == 1_500
    assert not data[1_500:2_500].any()
    # Cue 1: 2000 ms spoken in a 1500 ms window (2500..4000) followed by a 1000 ms gap
    # before Cue 2 (5000..6500) -> compressed at COMFORT_RATE (1.15x -> 1739 ms)
    # and borrows 239 ms from the silent gap.
    assert np.count_nonzero(np.abs(data[2_500:4_239]) > 1e-3) >= 1_725
    assert not data[4_239:5_000].any()
    # Cue 2 was silent: its slot is silence, not missing time.
    assert not data[5_000:].any()

    tmp_path = tmp_path / "case-b"
    tmp_path.mkdir(parents=True, exist_ok=True)

    store = make_store(tmp_path)
    project = make_project(tmp_path)
    result = render_three(store, project)
    second = result.fits[1]
    assert second.compressed is True
    assert second.rate == pytest.approx(1.15)
    assert second.duration_ms == 1_739
    assert second.overflow_ms == 0
    tmp_path = tmp_path / "case-b"
    tmp_path.mkdir(parents=True, exist_ok=True)

    store = make_store(tmp_path)
    project = make_project(tmp_path)
    result = render_three(store, project)

    timeline = store.load_timeline(project.id)
    assert timeline == result.timeline
    assert [span.start_ms for span in timeline.segments] == [0, 2_500, 5_000]
    assert timeline.approximate is False
    assert store.require(project.id).stats == result.stats


def test_renderer_failures(tmp_path) -> None:
    store = make_store(tmp_path)
    project = make_project(tmp_path)
    with pytest.raises(SubtitleProjectError), SubtitleTrackRenderer(store, project) as renderer:
        renderer.add_clip(1, clip(100))

    tmp_path = tmp_path / "case-b"
    tmp_path.mkdir(parents=True, exist_ok=True)

    store = make_store(tmp_path)
    project = make_project(tmp_path)
    with pytest.raises(SubtitleProjectError), SubtitleTrackRenderer(store, project) as renderer:
        renderer.finish()
    assert store.has_track(project.id) is False
    assert not list(store.project_dir(project.id).glob("*.part.wav"))

    tmp_path = tmp_path / "case-b"
    tmp_path.mkdir(parents=True, exist_ok=True)

    store = make_store(tmp_path)
    project = make_project(tmp_path)
    first = render_three(store, project)
    original = first.wav_path.read_bytes()

    with pytest.raises(RuntimeError), SubtitleTrackRenderer(store, project) as renderer:
        renderer.add_clip(0, clip(1_500))
        raise RuntimeError("synthesis failed")
    assert first.wav_path.read_bytes() == original
    assert not list(store.project_dir(project.id).glob("*.part.wav"))


def test_renderer_abort_and_rerender(tmp_path) -> None:
    store = make_store(tmp_path)
    project = make_project(tmp_path)
    render_three(store, project)

    # A new policy is a new fingerprint, so a fresh render must replace the WAV.
    changed = build_project(
        project.source_path, project.cues, FitPolicy.transcript(), sample_rate=SR
    )
    assert store.needs_render(changed) is True
    with SubtitleTrackRenderer(store, changed) as renderer:
        for index, text_len in enumerate(cue_lengths(changed)):
            renderer.add_clip(index, clip(text_len))
        result = renderer.finish()
    assert store.require(changed.id).fingerprint == changed.fingerprint
    assert store.load_timeline(changed.id) == result.timeline

    tmp_path = tmp_path / "case-b"
    tmp_path.mkdir(parents=True, exist_ok=True)

    store = make_store(tmp_path)
    project = make_project(tmp_path)
    renderer = SubtitleTrackRenderer(store, project)
    renderer.add_clip(0, clip(100))
    renderer.abort()
    assert store.has_track(project.id) is False

    # A late abort() — e.g. a failure path that still holds the renderer —
    # must not touch the promoted track.
    tmp_path = tmp_path / "case-b"
    tmp_path.mkdir(parents=True, exist_ok=True)

    store = make_store(tmp_path)
    project = make_project(tmp_path)
    renderer = SubtitleTrackRenderer(store, project).open()
    renderer.add_clip(0, clip(1_500))
    renderer.add_clip(1, clip(2_000))
    renderer.add_silent_cue(2)
    result = renderer.finish()
    renderer.abort()
    assert result.wav_path.is_file()
    assert store.has_track(project.id) is True


def test_renderer_exit_preserves_the_block_exception(tmp_path, monkeypatch):
    # An abort() cleanup failure must never mask the error that raised it.
    store = make_store(tmp_path)
    project = make_project(tmp_path)
    renderer = SubtitleTrackRenderer(store, project).open()
    monkeypatch.setattr(
        renderer, "abort", lambda: (_ for _ in ()).throw(OSError("simulated abort failure"))
    )
    with pytest.raises(RuntimeError, match="original"), renderer:
        raise RuntimeError("original")


def test_save_timeline_creates_the_project_dir(tmp_path):
    # Self-defense: persisting a timeline must not depend on a prior save()
    # or track preparation having created the directory.
    store = make_store(tmp_path)
    project = make_project(tmp_path)
    target = store.save_timeline(project.id, Timeline(()))
    assert target.is_file()
    assert store.load_timeline(project.id) is None  # still gated on the track


def cue_lengths(project) -> list[int]:
    """Deterministic per-cue clip lengths for a re-render test."""
    return [max(1, len(cue.text) * 100) for cue in project.cues]


# ── per-unit clip cache (FR-5.1) ─────────────────────────────────────────────


def test_unit_clip_cache_key(tmp_path) -> None:
    base: dict[str, Any] = {
        "voice_id": "Binh",
        "voice_stamp": "1024:1700000000000000000",
        "profile": "vieneu",
        "model_revision": "rev1",
        "speed": 1.0,
        "context_fingerprint": "ctx-a",
    }
    key = UnitClipCache.key("Xin chào.", **base)
    # Pinned: the key must survive a restart (no per-process hash salt). A
    # recipe change must bump UNIT_CACHE_VERSION, which moves this digest.
    assert key == "e8accb60d4df5141823edb1bf2f2769e737d7f7e7bc131304769c08cbbccb4f0"
    assert UnitClipCache.key("Xin chào.", **{**base, "speed": 1}) == key
    variants = {
        UnitClipCache.key("Xin chào!", **base),
        UnitClipCache.key("Xin chào.", **{**base, "voice_id": "Ngoc"}),
        UnitClipCache.key("Xin chào.", **{**base, "voice_stamp": "1024:1700000000000000001"}),
        UnitClipCache.key("Xin chào.", **{**base, "profile": "qwen_custom_0_6b"}),
        UnitClipCache.key("Xin chào.", **{**base, "model_revision": "rev2"}),
        UnitClipCache.key("Xin chào.", **{**base, "speed": 1.1}),
        UnitClipCache.key("Xin chào.", **{**base, "context_fingerprint": "ctx-b"}),
    }
    assert len(variants) == 7 and key not in variants

    # The voice stamp moves when the reference file is replaced; a missing
    # file stamps blank rather than raising.
    voice = tmp_path / "binh.wav"
    voice.write_bytes(b"a" * 10)
    stamp = voice_file_stamp(voice)
    assert stamp and voice_file_stamp(voice) == stamp
    voice.write_bytes(b"b" * 12)
    assert voice_file_stamp(voice) not in ("", stamp)
    assert voice_file_stamp(tmp_path / "missing.wav") == ""


def test_unit_clip_cache_storage_and_lru(tmp_path) -> None:
    store = make_store(tmp_path)
    project = make_project(tmp_path)
    cache = store.unit_cache(project.id)
    units = store.project_dir(project.id) / "units"
    key = UnitClipCache.key("Hello world.", voice_id="voice-a")
    audio = np.linspace(-0.5, 0.5, 700, dtype=np.float32)
    assert cache.get(key, SR) is None  # miss
    path = cache.put(key, audio, SR)
    assert path == units / f"{key}.wav"
    # A fresh instance (the next app run) reads the exact clip back.
    np.testing.assert_array_equal(store.unit_cache(project.id).get(key, SR), audio)
    assert cache.get(key, SR * 2) is None  # another output rate is a miss
    assert cache.put(UnitClipCache.key("", voice_id="v"), np.zeros(0, np.float32), SR) is None
    bad = UnitClipCache.key("corrupt", voice_id="v")
    (units / f"{bad}.wav").write_bytes(b"not a wav")
    assert cache.get(bad, SR) is None
    assert not (units / f"{bad}.wav").exists()  # dropped, not re-read forever
    assert cache.get("../project", SR) is None  # never a path outside units/

    # LRU under the byte cap: the oldest ACCESS goes first, not the oldest put.
    size = path.stat().st_size
    lru_dir = tmp_path / "lru"
    lru = UnitClipCache(lru_dir, cap_bytes=3 * size)
    keys = [UnitClipCache.key(f"unit {i}", voice_id="v") for i in range(4)]
    for k in keys[:3]:
        lru.put(k, audio, SR)
    assert lru.get(keys[0], SR) is not None  # keys[1] is now the oldest access
    lru.put(keys[3], audio, SR)
    assert {p.stem for p in lru_dir.glob("*.wav")} == {keys[0], keys[2], keys[3]}
    assert sum(p.stat().st_size for p in lru_dir.glob("*.wav")) <= 3 * size
    # A restart rebuilds the access order from disk: keys[2] goes next.
    UnitClipCache(lru_dir, cap_bytes=3 * size).put(keys[1], audio, SR)
    assert {p.stem for p in lru_dir.glob("*.wav")} == {keys[0], keys[3], keys[1]}

    # The cache lives inside the workspace: removing the project drops it too.
    store.remove(project.id)
    assert not units.exists()


# ── engine identity (Phase 5 Task 5.3) ───────────────────────────────────────


def test_fingerprint_provenance() -> None:
    # Every project written before engine provenance existed hashed this exact
    # payload. The "context" key must stay ABSENT (not null) when no identity is
    # known, or an upgrade would invalidate every stored track.
    cues = parse_cues(SAMPLE_SRT)
    assert (
        render_fingerprint(cues, FitPolicy.dub(rate_cap=1.5), SR, "voice-a")
        == "0c8ef8dacbaac2774b16e4f3b3bfc6eb611acf7af93252706cb59710631ddd90"
    )

    cues = parse_cues(SAMPLE_SRT)
    policy = FitPolicy.dub(rate_cap=1.5)
    plain = render_fingerprint(cues, policy, SR, "voice-a")
    vieNeu = render_fingerprint(cues, policy, SR, "voice-a", render_context())
    assert vieNeu != plain
    assert vieNeu == render_fingerprint(cues, policy, SR, "voice-a", render_context())
    assert vieNeu != render_fingerprint(cues, policy, SR, "voice-a", render_context(language="en"))
    assert vieNeu != render_fingerprint(
        cues, policy, SR, "voice-a", render_context(voice_id="Adam")
    )
    qwen = render_fingerprint(
        cues, policy, SR, "voice-a", render_context("qwen_custom_0_6b", voice_id="Vivian")
    )
    assert qwen not in (plain, vieNeu)

    # FR-5.7: a normalized project (identity + per-speaker voices) keeps the
    # digest it always had, and speakers no cue uses or whitespace-padded
    # names hash exactly like the normalized map.
    from vienetts_app.core.subtitles import Cue
    from vienetts_app.core.synthesis_context import SynthesisContext

    spk_cues = (
        Cue(1, 0, 2_000, "Xin chào.", speaker="SPEAKER_01"),
        Cue(
            2,
            2_500,
            4_000,
            "Chào bạn.",
            speaker="SPEAKER_02",
            speech_start_ms=2_600,
            speech_end_ms=3_900,
        ),
    )
    spk_policy = FitPolicy.dub(rate_cap=1.4, offset_ms=-120, merge_sentences=True)
    identity = SynthesisContext(profile="vieneu", model_revision="rev1", language="vi")
    normalized = {"SPEAKER_01": "Binh", "SPEAKER_02": "Ngoc"}
    messy = {" SPEAKER_01 ": " Binh", "SPEAKER_02": "Ngoc\t", "SPEAKER_09": "Mai", "x": " "}
    digest = render_fingerprint(spk_cues, spk_policy, 48_000, "Binh", identity, normalized)
    assert digest == "a3737dbe03d7af3771d2ad83d6618c15b06a887e19f0d2c176d9d81662df1101"
    assert render_fingerprint(spk_cues, spk_policy, 48_000, "Binh", identity, messy) == digest


def test_identity_storage(tmp_path) -> None:
    store = make_store(tmp_path)
    context = render_context()
    project = make_project(tmp_path, context=context)
    store.save(project)
    loaded = store.require(project.id)
    assert loaded == project
    assert loaded.context == context

    tmp_path = tmp_path / "case-b"
    tmp_path.mkdir(parents=True, exist_ok=True)

    store = make_store(tmp_path)
    project = make_project(tmp_path, context=render_context())
    store.save(project)
    path = store.project_path(project.id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["context"] = {"profile": "qwen_9b_future", "modelRevision": "x"}
    path.write_text(json.dumps(payload), encoding="utf-8")

    loaded = store.require(project.id)
    assert loaded.context is None  # fail-soft: no identity, not a failed load
    assert loaded.fingerprint == project.fingerprint


def test_cache_keying(tmp_path) -> None:
    store = make_store(tmp_path)
    project = make_project(tmp_path, context=render_context())
    render_three(store, project)
    assert store.cached_render(project) is not None

    # Same cues, another engine: the track is not reusable...
    other = make_project(tmp_path, context=render_context("qwen_custom_0_6b", voice_id="Vivian"))
    assert other.id == project.id  # same content → same workspace
    assert store.cached_render(other) is None
    assert store.needs_render(other) is True
    # ...and the identity that produced it still finds it.
    assert store.cached_render(project) is not None

    tmp_path = tmp_path / "case-b"
    tmp_path.mkdir(parents=True, exist_ok=True)

    store = make_store(tmp_path)
    legacy = make_project(tmp_path)  # no identity: the pre-5.3 project
    render_three(store, legacy)
    assert store.require(legacy.id).context is None

    # VieNeu is the engine the pre-multi-engine app used: its render stands.
    assert store.cached_render(make_project(tmp_path, context=render_context())) is not None
    # A Qwen profile must re-render audio it cannot vouch for.
    qwen = make_project(tmp_path, context=render_context("qwen_custom_0_6b", voice_id="Vivian"))
    assert store.cached_render(qwen) is None
    # A legacy render whose INPUTS moved is not reusable either.
    changed = make_project(tmp_path, policy=FitPolicy.dub(rate_cap=1.2), context=render_context())
    assert store.cached_render(changed) is None


def test_render_identity_and_gguf(tmp_path) -> None:
    store = make_store(tmp_path)
    context = render_context()
    result = render_three(store, make_project(tmp_path, context=context))
    assert store.require(result.project_id).context == context

    from vienetts_app.core import qwen_variants as qv

    tmp_path = tmp_path / "case-b"
    tmp_path.mkdir(parents=True, exist_ok=True)

    store = make_store(tmp_path)
    q8 = render_context(
        "qwen_custom_0_6b",
        voice_id="Vivian",
        variant=qv.variant_for("qwen_custom_0_6b", model_format="gguf", quantization="Q8_0"),
        resolved_device="cpu",
        model_identity="sha256:m8",
        tokenizer_identity="sha256:t8",
    )
    project = make_project(tmp_path, context=q8)
    store.save(project)
    loaded = store.require(project.id)
    assert loaded.context == q8
    assert loaded.context.quantization == "Q8_0"

    render_three(store, project)
    assert store.cached_render(project) is not None
    # Q4_K_M is a different model artifact: the Q8_0 track cannot serve it.
    q4 = make_project(
        tmp_path,
        context=render_context(
            "qwen_custom_0_6b",
            voice_id="Vivian",
            variant=qv.variant_for("qwen_custom_0_6b", model_format="gguf", quantization="Q4_K_M"),
        ),
    )
    assert q4.id == project.id  # same inputs → same workspace
    assert store.cached_render(q4) is None
    # ...and neither can an official full-weight render identity.
    official = make_project(tmp_path, context=render_context("qwen_custom_0_6b", voice_id="Vivian"))
    assert store.cached_render(official) is None


# ── export_srt_file ──────────────────────────────────────────────────────────


def test_export_srt(tmp_path, monkeypatch) -> None:
    cues = parse_cues(SAMPLE_SRT)
    target = export_srt_file(tmp_path / "nested" / "out.srt", cues)
    assert target == tmp_path / "nested" / "out.srt"
    assert parse_cues(target.read_text(encoding="utf-8")) == cues
    assert not list(target.parent.glob("*.tmp"))

    monkeypatch.undo()

    tmp_path = tmp_path / "case-b"
    tmp_path.mkdir(parents=True, exist_ok=True)

    cues = parse_cues(SAMPLE_SRT)
    target = tmp_path / "out.srt"

    def boom(_src, _dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("vienetts_app.core.subtitle_project.os.replace", boom)
    with pytest.raises(SubtitleProjectError):
        export_srt_file(target, cues)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []


def test_splice_studio_window_into_subtitle_updates_track_and_shifts_downstream(
    tmp_path, monkeypatch
) -> None:
    from vienetts_app.core.align import DEFAULT_SAMPLE_RATE, frames_for_ms
    from vienetts_app.core.audio import read_wav_range
    from vienetts_app.core.studio import (
        GainOp,
        commit_ops,
        load_project_from_subtitle,
        refit_subtitle_clips,
    )
    from vienetts_app.core.subtitle_project import splice_studio_window_into_subtitle
    from vienetts_app.core.subtitles import Cue

    store = make_store(tmp_path)
    cues = tuple(
        Cue(
            index=i + 1,
            start_ms=i * 1000 + 100,
            end_ms=i * 1000 + 600,
            text=f"Dòng {i + 1}",
        )
        for i in range(60)
    )
    project = build_project(
        tmp_path / "sixty.srt",
        cues,
        FitPolicy.dub(rate_cap=1.5),
        voice_key="Binh",
        context=render_context(),
    )
    store.save(project)
    with SubtitleTrackRenderer(store, project) as renderer:
        for i in range(60):
            tone = np.full(int(0.4 * DEFAULT_SAMPLE_RATE), 0.2, dtype=np.float32)
            renderer.add_clip(i, tone)
        orig_res = renderer.finish()

    orig_proj = store.require(project.id)
    orig_tl = store.load_timeline(project.id)
    assert orig_tl is not None
    orig_cue55_start = orig_tl.segments[55].start_ms
    orig_cue55_audio, _ = read_wav_range(
        store.wav_path(project.id),
        frames_for_ms(orig_tl.segments[55].start_ms),
        frames_for_ms(orig_tl.segments[55].end_ms),
    )

    # Load window 0 (cues 0..50), lengthen cue 0 by +400 ms at custom 1.0x speed
    # (100..500 becomes 100..1100, overflowing the 600 ms window and pushing cue 1
    # from 1100..1500 to 1100..1500, and cue 49 by +0 or if we lengthen cue 49 by +500 ms!)
    win0 = load_project_from_subtitle(store, project.id, start_cue=0)
    long_last = np.full(int(0.9 * DEFAULT_SAMPLE_RATE), 0.25, dtype=np.float32)
    win0 = refit_subtitle_clips(
        win0,
        win0.clips[-1].id,
        long_last,
        new_text="Dòng 50 mới",
        custom_rate=1.0,
    )
    win0 = commit_ops(win0, (GainOp(db=6.0),))

    # FR-5.7: the save-back commits in the same order as a render, under a
    # fresh render_id shared by all three files.
    calls = spy_commit(store, monkeypatch)
    updated = splice_studio_window_into_subtitle(store, win0)
    assert calls == ["save_timeline", "save", "promote_track"]
    assert updated.render_id and updated.render_id != orig_proj.render_id
    assert store.track_render_id(project.id) == updated.render_id
    assert store.cached_render(updated) is not None
    new_tl = store.load_timeline(project.id)
    assert new_tl is not None
    # Cue 49 grew from 400 ms (49100..49500) to 900 ms (49100..50000) -> +500 ms delta.
    assert updated.total_ms == orig_res.total_ms + 500
    assert updated.adjusted[49].text == "Dòng 50 mới"
    assert updated.cues[49].text == "Dòng 50 mới"
    assert new_tl.segments[49].end_ms == 50_000
    # Downstream cue 55 (in window 2) shifted by +500 ms and its audio survived intact.
    assert new_tl.segments[55].start_ms == orig_cue55_start + 500
    assert updated.adjusted[55].start_ms == orig_proj.adjusted[55].start_ms + 500
    shifted_cue55_audio, _ = read_wav_range(
        store.wav_path(project.id),
        frames_for_ms(new_tl.segments[55].start_ms),
        frames_for_ms(new_tl.segments[55].end_ms),
    )
    assert np.allclose(shifted_cue55_audio, orig_cue55_audio, atol=1e-4)

    # Window 0 cue 0 audio in track.wav reflects the +6 dB gain op (~0.4 amplitude).
    cue0_audio, _ = read_wav_range(
        store.wav_path(project.id),
        frames_for_ms(new_tl.segments[0].start_ms),
        frames_for_ms(new_tl.segments[0].end_ms),
    )
    assert float(np.max(np.abs(cue0_audio))) == pytest.approx(
        0.2 * (10.0 ** (6.0 / 20.0)), abs=1e-2
    )

    # A save-back that crashes right after its timeline write leaves the mixed
    # state refused; track.wav is untouched and no part file lingers.
    spliced_track = store.wav_path(project.id).read_bytes()
    crashing = make_store(tmp_path)
    spy_commit(crashing, monkeypatch, fail="save")
    with pytest.raises(SubtitleProjectError, match="save"):
        splice_studio_window_into_subtitle(crashing, win0)
    fresh = make_store(tmp_path)
    assert fresh.cached_render(updated) is None
    assert fresh.wav_path(project.id).read_bytes() == spliced_track
    assert not list(fresh.project_dir(project.id).glob("*.part.wav"))


def test_video_and_speaker_fields_round_trip(tmp_path: Path) -> None:
    from vienetts_app.core.audio import DEFAULT_SAMPLE_RATE
    from vienetts_app.core.studio import load_project_from_subtitle
    from vienetts_app.core.subtitle_project import (
        SubtitleTrackRenderer,
        render_fingerprint,
        splice_studio_window_into_subtitle,
    )
    from vienetts_app.core.subtitles import Cue

    store = make_store(tmp_path)
    cues_no_spk = (
        Cue(1, 0, 1000, "Câu một"),
        Cue(2, 1200, 2200, "Câu hai"),
    )
    cues_with_spk = (
        Cue(1, 0, 1000, "Câu một", speaker="SPEAKER_01"),
        Cue(2, 1200, 2200, "Câu hai", speaker="SPEAKER_02"),
    )
    fp_base = render_fingerprint(cues_no_spk, FitPolicy.dub(), DEFAULT_SAMPLE_RATE, "Binh")
    fp_spk = render_fingerprint(
        cues_with_spk,
        FitPolicy.dub(),
        DEFAULT_SAMPLE_RATE,
        "Binh",
        speaker_voices={"SPEAKER_01": "Binh", "SPEAKER_02": "Tuyen"},
    )
    assert fp_base != fp_spk

    project = build_project(
        tmp_path / "dub.srt",
        cues_with_spk,
        FitPolicy.dub(),
        voice_key="Binh",
        video_path=str(tmp_path / "movie.mp4"),
        speaker_voices={"SPEAKER_01": "Binh", "SPEAKER_02": "Tuyen"},
        video_mix_mode="duck",
        video_bg_gain_db=-14.0,
        embed_subtitles=False,
        subtitle_embed_mode="burn",
    )
    store.save(project)
    loaded = store.require(project.id)
    assert loaded.video_path == str(tmp_path / "movie.mp4")
    assert loaded.speaker_voices == {"SPEAKER_01": "Binh", "SPEAKER_02": "Tuyen"}
    assert loaded.video_mix_mode == "duck"
    assert loaded.video_bg_gain_db == pytest.approx(-14.0)
    assert loaded.embed_subtitles is False
    assert loaded.subtitle_embed_mode == "burn"
    assert [c.speaker for c in loaded.cues] == ["SPEAKER_01", "SPEAKER_02"]

    with SubtitleTrackRenderer(store, loaded) as renderer:
        for i in range(2):
            renderer.add_clip(i, np.full(int(0.5 * DEFAULT_SAMPLE_RATE), 0.1, dtype=np.float32))
        renderer.finish()

    rendered = store.require(project.id)
    assert [c.speaker for c in rendered.adjusted] == ["SPEAKER_01", "SPEAKER_02"]
    studio_proj = load_project_from_subtitle(store, project.id)
    assert [clip.speaker for clip in studio_proj.clips] == ["SPEAKER_01", "SPEAKER_02"]
    spliced = splice_studio_window_into_subtitle(store, studio_proj)
    assert spliced.video_path == str(tmp_path / "movie.mp4")
    assert spliced.speaker_voices == {"SPEAKER_01": "Binh", "SPEAKER_02": "Tuyen"}
    assert [c.speaker for c in spliced.adjusted] == ["SPEAKER_01", "SPEAKER_02"]

    # FR-5.7: an unused speaker and padded names do not move the fingerprint,
    # so save → reload → rebuild keeps the render reusable.
    store = make_store(tmp_path / "case-b")
    messy = {" SPEAKER_01 ": "Binh ", "SPEAKER_02": " Tuyen", "SPEAKER_09": "Mai"}
    project = build_project(
        tmp_path / "dub.srt", cues_with_spk, FitPolicy.dub(), voice_key="Binh", speaker_voices=messy
    )
    assert project.fingerprint == fp_spk
    with SubtitleTrackRenderer(store, project) as renderer:
        for i in range(2):
            renderer.add_clip(i, np.full(int(0.5 * DEFAULT_SAMPLE_RATE), 0.1, dtype=np.float32))
        renderer.finish()
    reloaded = store.require(project.id)
    rebuilt = build_project(
        tmp_path / "dub.srt",
        cues_with_spk,
        FitPolicy.dub(),
        voice_key="Binh",
        speaker_voices=reloaded.speaker_voices,
    )
    assert rebuilt.fingerprint == reloaded.fingerprint == fp_spk
    assert store.cached_render(rebuilt) is not None
    # A track rendered before FR-5.7 hashed the raw map, unused speaker
    # included; it stays reusable rather than forcing a full re-render...
    path = store.project_path(project.id)
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["fingerprint"] = "07da9a0c84c2b1d4e2412575445c64d28fce219000bc282bb18b7f97fad34576"
    path.write_text(json.dumps(payload), encoding="utf-8")
    assert store.cached_render(rebuilt) is not None
    # ...but a voice change for a speaker that does speak still re-renders.
    revoiced = build_project(
        tmp_path / "dub.srt",
        cues_with_spk,
        FitPolicy.dub(),
        voice_key="Binh",
        speaker_voices={**reloaded.speaker_voices, "SPEAKER_02": "Ngoc"},
    )
    assert store.cached_render(revoiced) is None


def test_speech_bounds_sparse_json_and_fingerprint(tmp_path: Path) -> None:
    import hashlib

    from vienetts_app.core.subtitle_project import (
        _cue_from_json,
        _cue_to_json,
        _policy_to_json,
    )
    from vienetts_app.core.subtitles import Cue

    plain_cues = (
        Cue(1, 0, 2_000, "Hello world."),
        Cue(2, 2_500, 4_000, "Second cue here."),
    )
    policy = FitPolicy.dub(rate_cap=1.5)

    # Sparse serialization omits speechStartMs/speechEndMs when None.
    serialized_plain = _cue_to_json(plain_cues[0])
    assert "speechStartMs" not in serialized_plain
    assert "speechEndMs" not in serialized_plain

    # Byte-identical golden fingerprint when speech bounds are None.
    expected_payload = {
        "version": PROJECT_VERSION,
        "sampleRate": SR,
        "voice": "voice-a",
        "policy": _policy_to_json(policy),
        "cues": [
            {"index": 1, "startMs": 0, "endMs": 2_000, "text": "Hello world."},
            {"index": 2, "startMs": 2_500, "endMs": 4_000, "text": "Second cue here."},
        ],
    }
    golden_fp = hashlib.sha256(
        json.dumps(
            expected_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        ).encode("utf-8")
    ).hexdigest()
    fp_none = render_fingerprint(plain_cues, policy, SR, "voice-a")
    assert fp_none == golden_fp

    # Setting speech_start_ms or speech_end_ms changes render_fingerprint.
    cues_start_set = (
        Cue(1, 0, 2_000, "Hello world.", speech_start_ms=150),
        Cue(2, 2_500, 4_000, "Second cue here."),
    )
    cues_end_set = (
        Cue(1, 0, 2_000, "Hello world."),
        Cue(2, 2_500, 4_000, "Second cue here.", speech_end_ms=3_800),
    )
    cues_both_set = (
        Cue(1, 0, 2_000, "Hello world.", speech_start_ms=150, speech_end_ms=1_850),
        Cue(2, 2_500, 4_000, "Second cue here.", speech_end_ms=3_800),
    )
    fp_start = render_fingerprint(cues_start_set, policy, SR, "voice-a")
    fp_end = render_fingerprint(cues_end_set, policy, SR, "voice-a")
    fp_both = render_fingerprint(cues_both_set, policy, SR, "voice-a")
    assert len({fp_none, fp_start, fp_end, fp_both}) == 4

    # Round-trip through SubtitleProjectStore preserves sparse JSON and exact fields.
    store = make_store(tmp_path)
    mixed_cues = (
        Cue(1, 0, 2_000, "No bounds"),
        Cue(2, 2_500, 4_000, "Start bound only", speech_start_ms=2_650),
        Cue(3, 4_500, 6_000, "End bound only", speech_end_ms=5_750),
        Cue(4, 6_500, 8_000, "Both bounds", speech_start_ms=6_600, speech_end_ms=7_850),
    )
    project = build_project(
        tmp_path / "speech_bounds.srt",
        mixed_cues,
        policy,
        sample_rate=SR,
        voice_key="voice-a",
    )
    store.save(project)
    raw_json = json.loads(store.project_path(project.id).read_text(encoding="utf-8"))
    raw_cues = raw_json["cues"]
    assert "speechStartMs" not in raw_cues[0] and "speechEndMs" not in raw_cues[0]
    assert raw_cues[1]["speechStartMs"] == 2_650 and "speechEndMs" not in raw_cues[1]
    assert "speechStartMs" not in raw_cues[2] and raw_cues[2]["speechEndMs"] == 5_750
    assert raw_cues[3]["speechStartMs"] == 6_600 and raw_cues[3]["speechEndMs"] == 7_850

    loaded = store.require(project.id)
    assert loaded.cues == mixed_cues
    assert loaded.fingerprint == project.fingerprint

    # Invalid speechStartMs / speechEndMs in JSON are rejected by _cue_from_json and store.load.
    base_entry = {"index": 1, "startMs": 1_000, "endMs": 2_000, "text": "Hello"}
    for bad_patch in (
        {"speechStartMs": True},
        {"speechEndMs": False},
        {"speechStartMs": "1200"},
        {"speechEndMs": 1_500.5},
        {"speechStartMs": 999},
        {"speechStartMs": 2_001},
        {"speechEndMs": 999},
        {"speechEndMs": 2_001},
        {"speechStartMs": 1_600, "speechEndMs": 1_400},
    ):
        assert _cue_from_json({**base_entry, **bad_patch}) is None

    corrupt_payload = dict(raw_json)
    corrupt_cues = [dict(c) for c in raw_cues]
    corrupt_cues[0]["speechStartMs"] = 2_500  # > endMs (2000)
    corrupt_payload["cues"] = corrupt_cues
    store.project_path(project.id).write_text(json.dumps(corrupt_payload), encoding="utf-8")
    assert store.load(project.id) is None


def test_renderer_trims_vocoder_silence_and_stretches_merged_units_uniformly(
    tmp_path: Path,
) -> None:
    from vienetts_app.core.subtitles import Cue

    store = make_store(tmp_path)
    # 1. AC-1 in SubtitleTrackRenderer: 1000 ms speech + 300 ms edge silence in a 1100 ms cue
    cues_trim = (Cue(1, 0, 1_100, "One second speech"),)
    proj_trim = build_project(
        tmp_path / "trim.srt",
        cues_trim,
        FitPolicy.dub(rate_cap=1.5),
        sample_rate=SR,
        voice_key="voice-a",
    )
    store.save(proj_trim)
    padded_clip = np.concatenate(
        [
            np.zeros(150, dtype=np.float32),
            np.full(1_000, 0.4, dtype=np.float32),
            np.zeros(150, dtype=np.float32),
        ]
    )
    with SubtitleTrackRenderer(store, proj_trim) as renderer:
        fit0 = renderer.add_clip(0, padded_clip)
        res_trim = renderer.finish()
    assert fit0.rate == 1.0
    assert fit0.compressed is False
    assert res_trim.stats.compressed == 0

    # 2. AC-3: Uniform unit rate across a multi-cue merged sentence unit
    cues_unit = (
        Cue(1, 0, 1_000, "Mệnh đề đầu tiên,"),
        Cue(2, 1_000, 2_000, "mệnh đề tiếp theo"),
        Cue(3, 2_000, 3_000, "và kết thúc câu."),
    )
    proj_unit = build_project(
        tmp_path / "unit.srt",
        cues_unit,
        FitPolicy.dub(rate_cap=1.5, merge_sentences=True),
        sample_rate=SR,
        voice_key="voice-a",
    )
    store.save(proj_unit)
    # 3450 ms speech for a 3000 ms merged unit window -> uniform rate = 1.15x across all 3 cues
    unit_audio = np.full(3_450, 0.35, dtype=np.float32)
    with SubtitleTrackRenderer(store, proj_unit) as renderer:
        unit_fits = renderer.add_unit_clip((0, 1, 2), unit_audio)
        res_unit = renderer.finish()
    assert len(unit_fits) == 3
    assert unit_fits[0].rate == pytest.approx(1.15)
    assert unit_fits[1].rate == pytest.approx(unit_fits[0].rate)
    assert unit_fits[2].rate == pytest.approx(unit_fits[0].rate)
    assert res_unit.fits[0].rate == res_unit.fits[1].rate == res_unit.fits[2].rate

    # 3. FR-5.2: only the first piece anchors to its cue; later pieces follow
    # contiguously (the unit gains no silence its synthesis didn't have) in both
    # modes, and both split edges ramp over 5 ms.
    cues_pair = (Cue(1, 0, 3_000, "Hello there my"), Cue(2, 3_100, 5_000, "good friend."))
    for policy in (FitPolicy.dub(rate_cap=1.5, merge_sentences=True), FitPolicy.transcript()):
        proj_pair = build_project(
            tmp_path / f"pair-{policy.mode}.srt", cues_pair, policy, sample_rate=SR
        )
        with SubtitleTrackRenderer(store, proj_pair) as renderer:
            head, tail = renderer.add_unit_clip((0, 1), clip(1_500, 0.5))
            res_pair = renderer.finish()
        assert head.start_ms == 0
        assert tail.start_ms == head.end_ms
        assert abs(head.end_ms - 656) <= 150  # the weighted split, within the snap window
        assert res_pair.total_ms == 1_500
        data, _ = read_wav(res_pair.wav_path)
        cut = head.end_ms
        assert np.all(np.diff(data[cut - 5 : cut]) < 0) and abs(data[cut - 1]) < 1e-3
        assert np.all(np.diff(data[cut : cut + 5]) > 0) and abs(data[cut]) < 1e-3
        assert data[cut - 6] == pytest.approx(0.5, abs=1e-3)
        assert data[cut + 5] == pytest.approx(0.5, abs=1e-3)

    # A 100 ms pause 80 ms past the weighted split is where the cut lands.
    paused = clip(1_500, 0.5)
    paused[686:786] = 0.0
    proj_pause = build_project(
        tmp_path / "pause.srt", cues_pair, FitPolicy.dub(merge_sentences=True), sample_rate=SR
    )
    with SubtitleTrackRenderer(store, proj_pause) as renderer:
        head, tail = renderer.add_unit_clip((0, 1), paused)
        renderer.finish()
    assert 686 <= head.end_ms <= 786
    assert tail.start_ms == head.end_ms
