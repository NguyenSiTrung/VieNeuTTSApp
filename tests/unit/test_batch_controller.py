"""BatchFileController (bead VieNeuTTSApp-qef): Paragraph-tab multi-file queue.

Fakes sit at the app seam (submit_stream_for_listener/cancel_job/settings
reads) and at the importer — the same posture as the audiobook suite, but
listener events are driven directly (the contract AppController calls).
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QObject, Signal

from vienetts_app.core.artifacts import SynthesisArtifact
from vienetts_app.ui.batch_controller import BatchFileController
from vienetts_app.ui.bg_ops import run_sync
from vienetts_app.ui.controller import GENERATE_CHAR_LIMIT


class FakeApp(QObject):
    """Narrow app seam: only what BatchFileController touches."""

    def __init__(self, output_dir: str = "") -> None:
        super().__init__()
        self.defaultVoice = "Minh Đức"
        self.outputDir = output_dir
        self._srt_keep = False
        self.submissions: list[dict] = []
        self.cancelled: list[str] = []
        self._next = 0

    @property
    def srtKeepTimestamps(self) -> bool:
        return self._srt_keep

    def submit_stream_for_listener(self, text, voice, listener, *, kind="requested_chapter"):
        self._next += 1
        job_id = f"job-{self._next}"
        self.submissions.append(
            {"job_id": job_id, "text": text, "voice": voice, "listener": listener, "kind": kind}
        )
        return job_id

    def cancel_job(self, job_id: str) -> bool:
        self.cancelled.append(job_id)
        return True


class FakePlayer:
    """Duck-typed PlaybackController: exactly what the batch controller calls."""

    stateChanged = Signal()

    def __init__(self) -> None:
        self.calls: list[tuple] = []
        self._released = None

    def play(self, path, on_released=None):
        self.calls.append(("play", path))
        self._released = on_released

    def stop(self):
        self.calls.append(("stop",))

    def finish(self) -> None:
        if self._released is not None:
            cb, self._released = self._released, None
            cb()


class Harness:
    def __init__(self, tmp_path: Path, *, texts: dict[str, str] | None = None) -> None:
        self.tmp = tmp_path
        self.app = FakeApp(output_dir=str(tmp_path / "out"))
        self.player = FakePlayer()
        texts = {} if texts is None else texts
        self.imports: list[dict] = []

        def importer(path: str, *, keep_srt_raw: bool = False) -> str:
            self.imports.append({"path": path, "keep_srt_raw": keep_srt_raw})
            if path in texts:
                return texts[path]
            if Path(path).is_file():
                return Path(path).read_text(encoding="utf-8")
            raise RuntimeError(f"boom: {path}")

        self.bc = BatchFileController(
            self.app,
            data_dir=tmp_path,
            player_factory=lambda: self.player,
            importer=importer,
            bg_runner=run_sync,
        )


@pytest.fixture()
def harness(qcoreapp, tmp_path: Path) -> Harness:
    return Harness(tmp_path)


def txt(tmp_path: Path, name: str, content: str = "Xin chào") -> Path:
    p = tmp_path / name
    p.write_text(content, encoding="utf-8")
    return p


class TestAddFiles:
    def test_add_pending_items_in_order(self, harness: Harness, tmp_path: Path) -> None:
        a = txt(tmp_path, "a.txt", "nội dung a")
        b = txt(tmp_path, "b.md", "nội dung b")
        harness.bc.addFiles([str(a), str(b)])
        items = harness.bc.items
        assert [i["fileName"] for i in items] == ["a.txt", "b.md"]
        assert all(i["status"] == "pending" for i in items)
        assert all(i["error"] == "" for i in items)

    def test_unsupported_extension_rejected_with_error(self, harness, tmp_path):
        bad = tmp_path / "photo.png"
        bad.write_bytes(b"\x89PNG")
        harness.bc.addFiles([str(bad)])
        assert harness.bc.items == []
        assert "photo.png" in harness.bc.errorText or ".png" in harness.bc.errorText

    def test_parse_failure_marks_item_failed(self, harness, tmp_path):
        missing = tmp_path / "missing.pdf"
        harness.bc.addFiles([str(missing)])
        (item,) = harness.bc.items
        assert item["status"] == "failed"
        assert item["error"] != ""

    def test_srt_flag_passed_to_importer(self, harness, tmp_path):
        srt = txt(tmp_path, "s.srt", "1\n00:00:01,000 --> 00:00:02,000\nChào")
        harness.app._srt_keep = True
        harness.bc.addFiles([str(srt)])
        assert harness.imports[0]["keep_srt_raw"] is True

    def test_empty_text_import_fails_item(self, harness, tmp_path):
        blank = txt(tmp_path, "empty.txt", "   ")
        harness.bc.addFiles([str(blank)])
        (item,) = harness.bc.items
        assert item["status"] == "failed"


class TestItemManagement:
    def test_remove_pending_item(self, harness, tmp_path):
        a = txt(tmp_path, "a.txt")
        b = txt(tmp_path, "b.txt")
        harness.bc.addFiles([str(a), str(b)])
        harness.bc.removeItem(0)
        assert [i["fileName"] for i in harness.bc.items] == ["b.txt"]

    def test_remove_out_of_range_is_noop(self, harness, tmp_path):
        harness.bc.removeItem(5)
        assert harness.bc.items == []

    def test_clear_finished_keeps_unfinished(self, harness, tmp_path):
        a = txt(tmp_path, "a.txt")
        b = tmp_path / "broken.pdf"  # never written: import fails → finished
        harness.bc.addFiles([str(a), str(b)])
        harness.bc.clearFinished()
        assert [i["fileName"] for i in harness.bc.items] == ["a.txt"]


def progress_event(job_id: str, done: int, total: int):
    return SimpleNamespace(job_id=job_id, done=done, total=total, stage="synthesizing")


def terminal_event(job_id: str, state: str, value=None, error: str = ""):
    return SimpleNamespace(job_id=job_id, owner="paragraph", state=state, value=value, error=error)


class TestRunLoop:
    def test_run_all_submits_pending_in_order(self, harness, tmp_path):
        for name in ("a.txt", "b.txt"):
            harness.bc.addFiles([str(txt(tmp_path, name, f"nội dung {name}"))])
        harness.bc.runAll()
        # Strictly sequential: only the FIRST item is submitted so far.
        assert [s["text"] for s in harness.app.submissions] == ["nội dung a.txt"]
        assert harness.bc.running is True
        assert harness.bc.runAllTotal == 2 and harness.bc.runAllDone == 0
        assert harness.bc.items[0]["status"] == "rendering"
        assert harness.bc.items[1]["status"] == "pending"

    def test_progress_maps_to_current_item(self, harness, tmp_path):
        harness.bc.addFiles([str(txt(tmp_path, "a.txt", "nội dung"))])
        harness.bc.runAll()
        job_id = harness.app.submissions[0]["job_id"]
        harness.bc.on_synthesis_progress(progress_event(job_id, 1, 4))
        assert harness.bc.progress == pytest.approx(0.25)
        assert harness.bc.items[0]["progress"] == pytest.approx(0.25)

    def test_foreign_events_ignored(self, harness, tmp_path):
        harness.bc.addFiles([str(txt(tmp_path, "a.txt", "nội dung"))])
        harness.bc.runAll()
        harness.bc.on_synthesis_progress(progress_event("job-999", 3, 4))
        assert harness.bc.progress == 0.0

    def test_chunk_events_are_noop(self, harness, tmp_path):
        harness.bc.addFiles([str(txt(tmp_path, "a.txt", "nội dung"))])
        harness.bc.runAll()
        harness.bc.on_synthesis_chunk(SimpleNamespace(job_id="job-1", sample_count=480, peak=0.1))

    def test_oversize_item_fails_and_run_continues(self, harness, tmp_path):
        big = txt(tmp_path, "big.txt", "x" * (GENERATE_CHAR_LIMIT + 1))
        ok = txt(tmp_path, "ok.txt", "ngắn")
        harness.bc.addFiles([str(big), str(ok)])
        harness.bc.runAll()
        assert harness.bc.items[0]["status"] == "failed"
        assert "quá dài" in harness.bc.items[0]["error"]
        # the run skipped straight to the valid item
        assert harness.app.submissions[0]["text"] == "ngắn"
        assert harness.bc.items[1]["status"] == "rendering"

    def test_failed_terminal_marks_item_and_continues(self, harness, tmp_path):
        harness.bc.addFiles(
            [
                str(txt(tmp_path, "a.txt", "thứ nhất")),
                str(txt(tmp_path, "b.txt", "thứ hai")),
            ]
        )
        harness.bc.runAll()
        harness.bc.on_synthesis_terminal(terminal_event("job-1", "failed", error="engine boom"))
        assert harness.bc.items[0]["status"] == "failed"
        assert harness.bc.items[0]["error"] == "engine boom"
        assert harness.bc.items[1]["status"] == "rendering"

    def test_cancel_returns_item_to_pending_and_halts(self, harness, tmp_path):
        harness.bc.addFiles(
            [
                str(txt(tmp_path, "a.txt", "thứ nhất")),
                str(txt(tmp_path, "b.txt", "thứ hai")),
            ]
        )
        harness.bc.runAll()
        harness.bc.cancel()
        assert harness.app.cancelled == ["job-1"]
        assert harness.bc.running is False
        assert harness.bc.items[0]["status"] == "pending"
        assert harness.bc.items[1]["status"] == "pending"
        # the late cancelled terminal is dropped, not re-processed
        harness.bc.on_synthesis_terminal(terminal_event("job-1", "cancelled"))
        assert harness.bc.items[0]["status"] == "pending"
        assert len(harness.app.submissions) == 1

    def test_submission_failure_fails_item(self, harness, tmp_path, monkeypatch):
        harness.app.submit_stream_for_listener = lambda *a, **k: None
        harness.bc.addFiles([str(txt(tmp_path, "a.txt", "nội dung"))])
        harness.bc.runAll()
        assert harness.bc.items[0]["status"] == "failed"
        assert harness.bc.running is False

    def test_voice_resolution_prefers_render_voice(self, harness, tmp_path):
        harness.bc.renderVoice = "Giọng đặc biệt"
        harness.bc.addFiles([str(txt(tmp_path, "a.txt", "nội dung"))])
        harness.bc.runAll()
        assert harness.app.submissions[0]["voice"] == "Giọng đặc biệt"
        assert harness.app.submissions[0]["kind"] == "bulk"


def make_artifact(tmp_path: Path, job_id: str, samples: int = 480) -> SynthesisArtifact:
    import numpy as np
    import soundfile as sf

    path = tmp_path / "artifacts" / "interactive" / f"{job_id}.wav"
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = np.zeros(samples, dtype=np.float32)
    sf.write(path, payload, 48_000, subtype="FLOAT", format="WAV")
    return SynthesisArtifact(
        job_id=job_id,
        path=path,
        sample_rate=48_000,
        samples=samples,
        duration_ms=int(samples * 1000 / 48_000),
    )


class TestCompletionExport:
    def test_completed_item_saves_named_wav_and_advances(self, harness, tmp_path):
        a = txt(tmp_path, "báo cáo.txt", "thứ nhất")
        b = txt(tmp_path, "cuốn.md", "thứ hai")
        harness.bc.addFiles([str(a), str(b)])
        harness.bc.runAll()
        art = make_artifact(tmp_path, "job-1")
        payload = art.path.read_bytes()
        harness.bc.on_synthesis_terminal(terminal_event("job-1", "completed", value=art))
        out = tmp_path / "out"
        assert harness.bc.items[0]["status"] == "ready"
        assert harness.bc.items[0]["wavPath"] == str(out / "báo cáo.wav")
        assert (out / "báo cáo.wav").read_bytes() == payload
        assert not art.path.exists()  # interactive artifact cleaned up
        assert harness.bc.runAllDone == 1 and harness.bc.runAllTotal == 2
        assert harness.bc.items[1]["status"] == "rendering"  # auto-advanced

    def test_same_stem_gets_collision_suffix(self, harness, tmp_path):
        one = txt(tmp_path, "a.txt", "thứ nhất")
        (tmp_path / "sub").mkdir()
        two = tmp_path / "sub" / "a.txt"
        two.write_text("thứ hai", encoding="utf-8")
        harness.bc.addFiles([str(one), str(two)])
        harness.bc.runAll()
        art1 = make_artifact(tmp_path, "job-1")
        harness.bc.on_synthesis_terminal(terminal_event("job-1", "completed", value=art1))
        art2 = make_artifact(tmp_path, "job-2")
        harness.bc.on_synthesis_terminal(terminal_event("job-2", "completed", value=art2))
        wavs = sorted(p.name for p in (tmp_path / "out").glob("*.wav"))
        assert wavs == ["a.wav", "a_2.wav"]

    def test_invalid_artifact_fails_item_and_continues(self, harness, tmp_path):
        a = txt(tmp_path, "a.txt", "thứ nhất")
        b = txt(tmp_path, "b.txt", "thứ hai")
        harness.bc.addFiles([str(a), str(b)])
        harness.bc.runAll()
        harness.bc.on_synthesis_terminal(terminal_event("job-1", "completed", value="nonsense"))
        assert harness.bc.items[0]["status"] == "failed"
        assert harness.bc.items[1]["status"] == "rendering"

    def test_mismatched_job_id_artifact_fails_item(self, harness, tmp_path):
        harness.bc.addFiles([str(txt(tmp_path, "a.txt", "thứ nhất"))])
        harness.bc.runAll()
        art = make_artifact(tmp_path, "job-other")
        harness.bc.on_synthesis_terminal(terminal_event("job-1", "completed", value=art))
        assert harness.bc.items[0]["status"] == "failed"

    def test_export_copy_failure_fails_item_keeps_artifact(self, harness, tmp_path, monkeypatch):
        harness.bc.addFiles([str(txt(tmp_path, "a.txt", "thứ nhất"))])
        harness.bc.runAll()
        art = make_artifact(tmp_path, "job-1")

        def boom(src, dst):
            raise OSError("disk full")

        monkeypatch.setattr("vienetts_app.ui.batch_controller.shutil.copyfile", boom)
        harness.bc.on_synthesis_terminal(terminal_event("job-1", "completed", value=art))
        assert harness.bc.items[0]["status"] == "failed"
        assert "disk full" in harness.bc.items[0]["error"]
        assert art.path.exists()  # kept for manual recovery

    def test_foreign_completed_terminal_releases_artifact(self, harness, tmp_path):
        art = make_artifact(tmp_path, "job-foreign")
        harness.bc.on_synthesis_terminal(terminal_event("job-foreign", "completed", value=art))
        assert not art.path.exists()

    def test_run_completes_and_totals_freeze(self, harness, tmp_path):
        harness.bc.addFiles([str(txt(tmp_path, "a.txt", "thứ nhất"))])
        harness.bc.runAll()
        art = make_artifact(tmp_path, "job-1")
        harness.bc.on_synthesis_terminal(terminal_event("job-1", "completed", value=art))
        assert harness.bc.running is False
        assert harness.bc.runAllDone == 1 and harness.bc.runAllTotal == 1


def ready_item(harness: Harness, tmp_path: Path, name: str = "a.txt") -> None:
    harness.bc.addFiles([str(txt(tmp_path, name, "thứ nhất"))])
    harness.bc.runAll()
    art = make_artifact(tmp_path, "job-1")
    harness.bc.on_synthesis_terminal(terminal_event("job-1", "completed", value=art))


class TestPlaybackAndReveal:
    def test_play_ready_item_uses_player(self, harness, tmp_path):
        ready_item(harness, tmp_path)
        wav = harness.bc.items[0]["wavPath"]
        harness.bc.playItem(0)
        assert harness.player.calls == [("play", wav)]
        assert harness.bc.playingIndex == 0

    def test_play_toggle_stops(self, harness, tmp_path):
        ready_item(harness, tmp_path)
        harness.bc.playItem(0)
        harness.bc.playItem(0)  # same row again = stop
        assert ("stop",) in harness.player.calls
        assert harness.bc.playingIndex == -1

    def test_released_resets_playing_index(self, harness, tmp_path):
        ready_item(harness, tmp_path)
        harness.bc.playItem(0)
        harness.player.finish()  # playback ran to the end
        assert harness.bc.playingIndex == -1

    def test_play_non_ready_item_is_noop(self, harness, tmp_path):
        harness.bc.addFiles([str(txt(tmp_path, "a.txt", "thứ nhất"))])
        harness.bc.playItem(0)
        assert harness.player.calls == []

    def test_show_in_folder_reveals_parent(self, harness, tmp_path):
        revealed: list[Path] = []
        harness.bc._reveal_fn = lambda p: revealed.append(p) or True
        ready_item(harness, tmp_path)
        assert harness.bc.showInFolder(0) is True
        assert revealed == [tmp_path / "out"]

    def test_show_in_folder_failure_sets_error(self, harness, tmp_path):
        harness.bc._reveal_fn = lambda p: False
        ready_item(harness, tmp_path)
        assert harness.bc.showInFolder(0) is False
        assert harness.bc.errorText != ""
