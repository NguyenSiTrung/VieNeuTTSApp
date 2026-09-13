"""core/subtitle_project.py + audio.StreamingWavWriter: streamed SRT renders.

A subtitle track is built cue by cue and written straight to disk, so these
tests pin three things: the writer never buffers the whole track, the workspace
round-trips its inputs and stats, and a cached render is reused exactly when the
fingerprint still matches.
"""

from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from vienetts_app.core.align import FitPolicy
from vienetts_app.core.audio import StreamingWavWriter, read_wav
from vienetts_app.core.subtitle_project import (
    PROJECT_VERSION,
    SubtitleProjectError,
    SubtitleProjectStore,
    SubtitleTrackRenderer,
    build_project,
    export_srt_file,
    project_id_for,
    render_fingerprint,
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


def make_project(tmp_path: Path, *, policy: FitPolicy | None = None, sample_rate: int = SR):
    return build_project(
        "/media/movie.srt",
        parse_cues(SAMPLE_SRT),
        policy or FitPolicy.dub(rate_cap=1.5),
        sample_rate=sample_rate,
        voice_key="voice-a",
    )


def render_three(store: SubtitleProjectStore, project) -> object:
    with SubtitleTrackRenderer(store, project) as renderer:
        renderer.add_clip(0, clip(1_500))
        renderer.add_clip(1, clip(2_000))  # 2000 ms into a 1500 ms window
        renderer.add_silent_cue(2)
        return renderer.finish()


# ── StreamingWavWriter ───────────────────────────────────────────────────────


def test_streaming_writer_writes_blocks_and_silence(tmp_path):
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


def test_streaming_writer_silence_spans_many_chunks(tmp_path):
    # More than one bounded silence chunk: the writer must not allocate it whole.
    frames = StreamingWavWriter.SILENCE_CHUNK_FRAMES * 2 + 7
    writer = StreamingWavWriter(tmp_path / "gap.wav", SR).open()
    assert writer.write_silence(frames) == frames
    writer.close()
    data, _ = read_wav(tmp_path / "gap.wav")
    assert data.size == frames
    assert not data.any()


def test_streaming_writer_is_idempotent_and_guards_after_close(tmp_path):
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


def test_streaming_writer_rejects_bad_rate(tmp_path):
    with pytest.raises(ValueError):
        StreamingWavWriter(tmp_path / "t.wav", 0)


def test_streaming_writer_abort_deletes_the_part_file(tmp_path):
    path = tmp_path / "t.wav"
    writer = StreamingWavWriter(path, SR).open()
    writer.write(clip(100))
    writer.abort()
    assert not path.exists()


def test_streaming_writer_abort_unlinks_even_when_close_raises(tmp_path, monkeypatch):
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


def test_streaming_writer_exit_preserves_the_block_exception(tmp_path, monkeypatch):
    # An abort() cleanup failure must never mask the error that raised it.
    path = tmp_path / "t.wav"
    writer = StreamingWavWriter(path, SR).open()
    writer.write(clip(100))
    monkeypatch.setattr(
        writer, "abort", lambda: (_ for _ in ()).throw(OSError("simulated abort failure"))
    )
    with pytest.raises(RuntimeError, match="original"), writer:
        raise RuntimeError("original")


def test_streaming_writer_exit_propagates_a_close_failure(tmp_path, monkeypatch):
    # A failed flush/close on a NORMAL exit still fails the operation.
    path = tmp_path / "t.wav"
    writer = StreamingWavWriter(path, SR).open()
    writer.write(clip(100))
    monkeypatch.setattr(
        writer, "close", lambda: (_ for _ in ()).throw(OSError("simulated close failure"))
    )
    with pytest.raises(OSError, match="simulated close failure"), writer:
        pass


def test_streaming_writer_context_manager_closes_or_aborts(tmp_path):
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


def test_fingerprint_is_stable_and_input_sensitive():
    cues = parse_cues(SAMPLE_SRT)
    base = render_fingerprint(cues, FitPolicy.dub(), SR, "v1")
    assert base == render_fingerprint(cues, FitPolicy.dub(), SR, "v1")
    assert base != render_fingerprint(cues, FitPolicy.dub(rate_cap=1.4), SR, "v1")
    assert base != render_fingerprint(cues, FitPolicy.transcript(), SR, "v1")
    assert base != render_fingerprint(cues, FitPolicy.dub(), SR, "v2")
    assert base != render_fingerprint(cues, FitPolicy.dub(), 8_000, "v1")
    assert base != render_fingerprint(cues[:1], FitPolicy.dub(), SR, "v1")


def test_project_id_follows_content_not_path():
    cues = parse_cues(SAMPLE_SRT)
    assert project_id_for("/a/movie.srt", cues) == project_id_for("/b/movie.srt", cues)
    assert project_id_for("/a/movie.srt", cues) != project_id_for("/a/other.srt", cues)


# ── build_project ────────────────────────────────────────────────────────────


def test_build_project_refuses_an_empty_cue_list(tmp_path):
    with pytest.raises(SubtitleProjectError):
        build_project("/media/movie.srt", [], FitPolicy.dub())


def test_build_project_derives_identity_and_title(tmp_path):
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


def test_store_round_trips_a_project(tmp_path):
    store = make_store(tmp_path)
    project = make_project(tmp_path)
    store.save(project)

    loaded = store.load(project.id)
    assert loaded == project
    assert store.require(project.id) == project


def test_store_save_persists_stats_and_total_after_render(tmp_path):
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


def test_store_load_degrades_on_bad_payloads(tmp_path):
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


def test_store_require_raises_for_a_missing_project(tmp_path):
    store = make_store(tmp_path)
    with pytest.raises(SubtitleProjectError):
        store.require("nope")


def test_store_list_projects_skips_junk_and_sorts_newest_first(tmp_path):
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


def test_store_remove_is_a_no_op_for_missing_dirs(tmp_path):
    store = make_store(tmp_path)
    store.remove("missing")  # no raise
    project = make_project(tmp_path)
    store.save(project)
    store.remove(project.id)
    assert not store.project_dir(project.id).exists()


def test_store_needs_render_tracks_the_fingerprint(tmp_path):
    store = make_store(tmp_path)
    project = make_project(tmp_path)
    assert store.needs_render(project) is True  # no track yet
    render_three(store, project)
    assert store.needs_render(project) is False  # cached and matching

    changed_policy = build_project(
        project.source_path, project.cues, FitPolicy.dub(rate_cap=1.2), sample_rate=SR
    )
    assert store.needs_render(changed_policy) is True


def test_store_load_rejects_ids_that_escape_the_root(tmp_path):
    store = make_store(tmp_path)
    store.root.mkdir(parents=True)
    assert store.load("../outside") is None
    assert store.load("not-a-project-id") is None
    assert store.load("") is None


def test_store_load_rejects_a_nonnumeric_version(tmp_path):
    store = make_store(tmp_path)
    project = make_project(tmp_path)
    store.save(project)
    payload = json.loads(store.project_path(project.id).read_text(encoding="utf-8"))
    payload["version"] = "abc"
    store.project_path(project.id).write_text(json.dumps(payload), encoding="utf-8")
    assert store.load(project.id) is None


def test_store_load_rejects_a_payload_id_mismatch(tmp_path):
    # A payload claiming another identity (or a traversal path) must never
    # be handed back under the requested id.
    store = make_store(tmp_path)
    project = make_project(tmp_path)
    store.save(project)
    payload = json.loads(store.project_path(project.id).read_text(encoding="utf-8"))
    payload["id"] = "../outside"
    store.project_path(project.id).write_text(json.dumps(payload), encoding="utf-8")
    assert store.load(project.id) is None


def test_store_remove_never_leaves_the_root(tmp_path):
    store = make_store(tmp_path)
    store.root.mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep.txt").write_text("x", encoding="utf-8")
    store.remove("../outside")  # invalid id → no-op, not root/../outside
    assert (outside / "keep.txt").is_file()


def test_cached_render_returns_the_stored_rendered_project(tmp_path):
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


def test_cached_render_rejects_stale_or_incomplete_state(tmp_path):
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


def test_cached_render_rejects_a_wrong_shaped_timeline(tmp_path):
    # A parseable timeline that does not line up with the stored cues (empty,
    # or a different segment count) would karaoke-highlight the wrong spans —
    # it is not a reusable render.
    store = make_store(tmp_path)
    project = make_project(tmp_path)
    render_three(store, project)

    store.save_timeline(project.id, Timeline(()))
    assert store.cached_render(project) is None
    assert store.needs_render(project) is True

    store.save_timeline(
        project.id, Timeline((SegmentSpan(0, 5, 0, 1_000),))  # 1 span for 3 cues
    )
    assert store.cached_render(project) is None
    assert store.needs_render(project) is True


def test_store_prepare_track_part_is_unique_and_promotes(tmp_path):
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


def test_store_timeline_needs_the_track_to_load(tmp_path):
    store = make_store(tmp_path)
    project = make_project(tmp_path)

    store.save(project)
    store.save_timeline(project.id, Timeline(()))
    assert store.load_timeline(project.id) is None  # no track.wav yet
    render_three(store, project)
    assert store.load_timeline(project.id) is not None


# ── renderer ─────────────────────────────────────────────────────────────────


def test_renderer_places_clips_on_the_srt_clock(tmp_path):
    store = make_store(tmp_path)
    project = make_project(tmp_path)
    result = render_three(store, project)

    data, rate = read_wav(result.wav_path)
    assert rate == SR
    assert data.size == result.total_ms == 5_000
    # Cue 0: 1500 ms of audio at 0, then silence until cue 1 at 2500.
    assert np.count_nonzero(data[0:1_500]) == 1_500
    assert not data[1_500:2_500].any()
    # Cue 1: 2000 ms spoken in a 1500 ms window -> compressed to fit 1500.
    # (WSOLA fades the buffer edges by ~2 ms, so allow a couple of silent frames.)
    assert np.count_nonzero(np.abs(data[2_500:4_000]) > 1e-3) >= 1_490
    assert not data[4_000:5_000].any()
    # Cue 2 was silent: its slot is silence, not missing time.
    assert not data[5_000:].any()


def test_renderer_compresses_an_overlong_take_to_the_cap(tmp_path):
    store = make_store(tmp_path)
    project = make_project(tmp_path)
    result = render_three(store, project)
    second = result.fits[1]
    assert second.compressed is True
    assert second.duration_ms == 1_500
    assert second.overflow_ms == 0


def test_renderer_result_matches_the_saved_timeline(tmp_path):
    store = make_store(tmp_path)
    project = make_project(tmp_path)
    result = render_three(store, project)

    timeline = store.load_timeline(project.id)
    assert timeline == result.timeline
    assert [span.start_ms for span in timeline.segments] == [0, 2_500, 5_000]
    assert timeline.approximate is False
    assert store.require(project.id).stats == result.stats


def test_renderer_rejects_out_of_order_cues(tmp_path):
    store = make_store(tmp_path)
    project = make_project(tmp_path)
    with pytest.raises(SubtitleProjectError), SubtitleTrackRenderer(store, project) as renderer:
        renderer.add_clip(1, clip(100))


def test_renderer_finish_without_audio_leaves_no_track(tmp_path):
    store = make_store(tmp_path)
    project = make_project(tmp_path)
    with pytest.raises(SubtitleProjectError), SubtitleTrackRenderer(store, project) as renderer:
        renderer.finish()
    assert store.has_track(project.id) is False
    assert not list(store.project_dir(project.id).glob("*.part.wav"))


def test_renderer_exception_discards_the_part_and_keeps_the_old_track(tmp_path):
    store = make_store(tmp_path)
    project = make_project(tmp_path)
    first = render_three(store, project)
    original = first.wav_path.read_bytes()

    with pytest.raises(RuntimeError), SubtitleTrackRenderer(store, project) as renderer:
        renderer.add_clip(0, clip(1_500))
        raise RuntimeError("synthesis failed")
    assert first.wav_path.read_bytes() == original
    assert not list(store.project_dir(project.id).glob("*.part.wav"))


def test_renderer_re_render_replaces_the_cached_track(tmp_path):
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


def test_renderer_abort_leaves_no_track(tmp_path):
    store = make_store(tmp_path)
    project = make_project(tmp_path)
    renderer = SubtitleTrackRenderer(store, project)
    renderer.add_clip(0, clip(100))
    renderer.abort()
    assert store.has_track(project.id) is False


def test_renderer_abort_after_finish_is_a_harmless_no_op(tmp_path):
    # A late abort() — e.g. a failure path that still holds the renderer —
    # must not touch the promoted track.
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


# ── export_srt_file ──────────────────────────────────────────────────────────


def test_export_srt_file_writes_atomically(tmp_path):
    cues = parse_cues(SAMPLE_SRT)
    target = export_srt_file(tmp_path / "nested" / "out.srt", cues)
    assert target == tmp_path / "nested" / "out.srt"
    assert parse_cues(target.read_text(encoding="utf-8")) == cues
    assert not list(target.parent.glob("*.tmp"))


def test_export_srt_file_cleans_its_temp_on_failure(tmp_path, monkeypatch):
    cues = parse_cues(SAMPLE_SRT)
    target = tmp_path / "out.srt"

    def boom(_src, _dst):
        raise OSError("simulated replace failure")

    monkeypatch.setattr("vienetts_app.core.subtitle_project.os.replace", boom)
    with pytest.raises(SubtitleProjectError):
        export_srt_file(target, cues)
    assert not target.exists()
    assert list(tmp_path.iterdir()) == []
