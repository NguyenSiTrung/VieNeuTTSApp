"""AppController: QML-facing app state — catalog, synthesis, voice ops, settings.

Every dependency is injectable (data_dir, engine/worker factories, catalog,
saved-names functions, stream-playback factory); construction must NOT create
the worker or initialize the engine (NFR-3.1). Fakes stand in for both — no
vieneu model, no QThread. Streaming tests run the REAL StreamPlaybackController
against FakeSinks (fake at the audio seam, per project testing pattern).
"""

import json
import re
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QObject, QStandardPaths, Qt, Signal  # noqa: E402

from vienetts_app.core.engine import (  # noqa: E402
    FETCH_MODELS_COMMAND,
    MODELS_MISSING_MARKER,
    ModelsMissingError,
)
from vienetts_app.core.jobs import JobChunk, JobProgress, JobTerminal, SynthesisJob
from vienetts_app.core.models import TTSRequest, VoiceOp, WarmupOp  # noqa: E402
from vienetts_app.core.performance import PerformanceRecorder  # noqa: E402
from vienetts_app.ui.bg_ops import run_sync  # noqa: E402
from vienetts_app.ui.controller import GENERATE_CHAR_LIMIT, AppController  # noqa: E402
from vienetts_app.ui.stream_playback import StreamPlaybackController  # noqa: E402
from vienetts_app.workers.inference_worker import CANCELLED_MESSAGE  # noqa: E402

# The REAL message shape the engine raises at its lazy-init site
# (core.engine._models_missing_message): marker prefix + fetch hint. The
# worker error signal carries only this plain string across the thread.
MODELS_MISSING_MESSAGE = (
    f"{MODELS_MISSING_MARKER}: the TTS model files were not found in the local "
    f"Hugging Face cache (missing). Fetch the offline bundle once with "
    f"`{FETCH_MODELS_COMMAND}`."
)


def wait_until(cond, timeout: float = 5.0, interval: float = 0.01) -> bool:
    app = QCoreApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        if app is not None:
            app.processEvents()
        time.sleep(interval)
    return False


class FakeEngine:
    """Stands in for TTSEngine — records kwargs and calls, returns silence."""

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.backend = kwargs.get("backend", "auto")
        self.sample_rate = 48_000
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def infer(self, text, voice=None, temperature=None, **kw) -> np.ndarray:
        self.calls.append(("infer", {"text": text, "voice": voice, "temperature": temperature}))
        return np.zeros(48_000, dtype=np.float32)

    def add_voice(self, name, ref_clip, *, denoise=True, save=False) -> str:
        self.calls.append(("add_voice", {"name": name, "denoise": denoise}))
        return name

    def remove_voice(self, name, *, save=False) -> None:
        self.calls.append(("remove_voice", {"name": name}))

    def denoise(self, clip_path, out_path=None, max_seconds=None):
        self.calls.append(("denoise", {"clip_path": str(clip_path)}))
        return np.full(44_100, 0.5, dtype=np.float32), 44_100

    def persist_voices(self) -> Path:
        self.calls.append(("persist_voices", {}))
        return Path("/fake/voices.json")

    def close(self) -> None:
        self.closed = True


class FakeWorker(QObject):
    """Tagged signal surface of InferenceWorker; records submissions."""

    progress = Signal(object)  # JobProgress
    chunk_ready = Signal(object)  # JobChunk
    terminal = Signal(object)  # JobTerminal

    def __init__(self, engine: Any) -> None:
        super().__init__()
        self.engine = engine
        self.submitted: list[Any] = []
        self.cancelled_job_ids: list[str] = []
        self.cancelled_owners: list[str] = []
        self.stopped = False
        self.started = False

    def start(self) -> None:
        self.started = True

    def submit(self, job: Any) -> bool:
        self.submitted.append(job)
        return True

    def cancel_job(self, job_id: str) -> bool:
        self.cancelled_job_ids.append(job_id)
        return True

    def cancel_owner(self, owner: str) -> int:
        self.cancelled_owners.append(owner)
        return 0

    def stop(self) -> None:
        self.stopped = True

    # -- tagged-emit conveniences (the worker tags by submitted job) --------
    def progress_last(self, done: int, total: int, stage: str = "synthesizing") -> None:
        job = self.submitted[-1]
        self.progress.emit(JobProgress(job.id, done=done, total=total, stage=stage))

    def chunk_last(self, samples: Any) -> None:
        job = self.submitted[-1]
        self.chunk_ready.emit(JobChunk(job.id, samples))

    def complete_last(self, value: Any, owner: str = "text") -> None:
        job = self.submitted[-1]
        self.terminal.emit(
            JobTerminal(job_id=job.id, owner=owner, state="completed", value=value)  # type: ignore[arg-type]
        )

    def fail_last(self, message: str, owner: str = "text") -> None:
        job = self.submitted[-1]
        if message == CANCELLED_MESSAGE:
            self.terminal.emit(
                JobTerminal(job_id=job.id, owner=owner, state="cancelled")  # type: ignore[arg-type]
            )
        else:
            self.terminal.emit(
                JobTerminal(job_id=job.id, owner=owner, state="failed", error=message)  # type: ignore[arg-type]
            )


class FakeSink:
    """QAudioSink duck-type per StreamPlaybackController's fake-sink contract.

    Records the call sequence; keeps a reference to the io device handed to
    start() so tests can drain buffered bytes. ``state()`` returns plain
    enum-name strings (name-mapped controller, not import-coupled).
    """

    def __init__(self) -> None:
        self.calls: list[str] = []
        self.device: Any | None = None
        self._state = "StoppedState"

    def start(self, device) -> None:
        self.calls.append("start")
        self.device = device
        self._state = "ActiveState"

    def stop(self) -> None:
        self.calls.append("stop")
        self._state = "StoppedState"

    def state(self) -> str:
        return self._state


class FailingSinkFactory:
    """Sink factory that always raises — simulates no audio backend."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, _fmt) -> FakeSink:
        self.calls += 1
        msg = "no audio device"
        raise RuntimeError(msg)


def make_stream_playback(sink: FakeSink) -> StreamPlaybackController:
    """Real StreamPlaybackController wired to one shared FakeSink."""

    def sink_factory(_fmt):
        return sink

    return StreamPlaybackController(sink_factory=sink_factory)


class FakeFilePlayback(QObject):
    """PlaybackController stand-in for the temp-file replay path.

    Plain attributes suffice — AppController only getattrs play/stop/
    finished/errorText off the attached player — except the SIGNALS it
    connects to, which must be real: finished/errorTextChanged (replay
    lifecycle) and positionChanged/durationChanged (playhead feed).
    """

    finished = Signal()
    errorTextChanged = Signal()
    positionChanged = Signal(int)
    durationChanged = Signal(int)

    def __init__(self) -> None:
        super().__init__()
        self.played: list[str] = []
        self.stops = 0
        self.errorText = ""

    def play(self, path) -> None:
        self.played.append(str(path))

    def stop(self) -> None:
        self.stops += 1


def fake_catalog() -> list[dict[str, str]]:
    return [
        {
            "name": "Minh Đức",
            "description": "Nam · Bắc · Phong cách tin tức",
            "gender": "male",
            "style": "tin_tuc",
        },
        {
            "name": "Hà Vy",
            "description": "Nữ · Trung · Phong cách tự nhiên",
            "gender": "female",
            "style": "tu_nhien",
        },
        {
            "name": "Thái Sơn",
            "description": "Nam · Nam · Phong cách kể chuyện",
            "gender": "male",
            "style": "doc_truyen",
        },
        {"name": "Weird", "description": "no region tokens here", "gender": "", "style": ""},
    ]


class Harness:
    def __init__(
        self,
        tmp_path: Path,
        catalog=None,
        saved=None,
        performance_recorder: PerformanceRecorder | None = None,
    ) -> None:
        self.tmp_path = tmp_path
        self.engines: list[FakeEngine] = []
        self.workers: list[FakeWorker] = []
        self.catalog_calls = 0
        self.saved_calls = 0
        self._saved_stub = saved if saved is not None else (lambda voices_dir: [])
        # Streaming seam: each controller gets its own real StreamPlayback-
        # Controller backed by a shared FakeSink (plus a failing variant).
        self.sink = FakeSink()
        self.failing_sink_factory: FailingSinkFactory | None = None

        def engine_factory(**kwargs: Any) -> FakeEngine:
            engine = FakeEngine(**kwargs)
            self.engines.append(engine)
            return engine

        def worker_factory(engine: Any) -> FakeWorker:
            worker = FakeWorker(engine)
            self.workers.append(worker)
            return worker

        def catalog_fn() -> list[dict[str, str]]:
            self.catalog_calls += 1
            return catalog if catalog is not None else fake_catalog()

        def saved_fn(voices_dir: Any) -> list[str]:
            self.saved_calls += 1
            return self._saved_stub(voices_dir)

        controller_kwargs: dict[str, Any] = {}

        def stream_playback_factory():
            if self.failing_sink_factory is not None:
                factory = self.failing_sink_factory
                return StreamPlaybackController(sink_factory=factory)
            return make_stream_playback(self.sink)

        controller_kwargs["stream_playback_factory"] = stream_playback_factory

        self.engine_factory = engine_factory
        self.controller = AppController(
            data_dir=tmp_path,
            engine_factory=engine_factory,
            worker_factory=worker_factory,
            catalog=catalog_fn,
            saved_names=saved_fn,
            performance_recorder=performance_recorder,
            # Import/export complete inline (the real thread-pool path is
            # covered by TestBgOpsAsync below).
            bg_runner=run_sync,
            **controller_kwargs,
        )

    @property
    def worker(self) -> FakeWorker:
        return self.workers[-1]

    @property
    def stream_player(self) -> StreamPlaybackController:
        player = self.controller._stream_playback
        assert isinstance(player, StreamPlaybackController)
        return player


@pytest.fixture()
def harness(qcoreapp, tmp_path: Path) -> Harness:
    return Harness(tmp_path)


class TestConstruction:
    def test_no_worker_or_engine_created(self, harness: Harness) -> None:
        # NFR-3.1: startup stays model-free.
        assert harness.engines == []
        assert harness.workers == []
        assert harness.controller.busy is False
        assert harness.controller.hasAudio is False
        assert harness.controller.needsRestart is False
        assert harness.controller.errorText == ""

    def test_consent_defaults_false_without_file(self, harness: Harness) -> None:
        assert harness.controller.consentGiven is False

    def test_settings_loaded_from_data_dir(self, qcoreapp, tmp_path: Path) -> None:
        (tmp_path / "settings.json").write_text(
            json.dumps({"temperature": 1.1, "theme": "dark"}), encoding="utf-8"
        )
        controller = AppController(
            data_dir=tmp_path,
            engine_factory=lambda **kw: FakeEngine(**kw),
            worker_factory=lambda engine: FakeWorker(engine),
            catalog=lambda: [],
            saved_names=lambda vd: [],
        )
        assert controller.temperature == pytest.approx(1.1)
        assert controller.theme == "dark"


class TestVoiceCatalog:
    def test_grouping_by_region_with_fallback_group(self, harness: Harness) -> None:
        voices = harness.controller.voices
        labels = [g["label"] for g in voices]
        # Fixed order: Bắc, Trung, Nam, Khác(fallback), then cloned.
        assert labels == ["Bắc", "Trung", "Nam", "Khác"]

    def test_entries_carry_id_and_display_label(self, harness: Harness) -> None:
        voices = harness.controller.voices
        bac = voices[0]["voices"]
        assert bac[0]["id"] == "Minh Đức"
        assert "Minh Đức" in bac[0]["label"]
        assert "tin tức" in bac[0]["label"]

    def test_unparseable_description_lands_in_khac(self, harness: Harness) -> None:
        khac = harness.controller.voices[-1]["voices"]
        assert [v["id"] for v in khac] == ["Weird"]

    def test_cloned_group_appended(self, qcoreapp, tmp_path: Path) -> None:
        h = Harness(tmp_path, saved=lambda vd: ["MyClone", "Other"])
        voices = h.controller.voices
        cloned = voices[-1]
        assert cloned["label"] == "Đã sao chép"
        assert [v["id"] for v in cloned["voices"]] == ["MyClone", "Other"]

    def test_refresh_voices_rebuilds(self, harness: Harness) -> None:
        before = harness.catalog_calls
        harness.controller.refreshVoices()
        assert harness.catalog_calls == before + 1

    def test_empty_catalog_yields_only_cloned(self, qcoreapp, tmp_path: Path) -> None:
        h = Harness(tmp_path, catalog=[], saved=lambda vd: ["Solo"])
        voices = h.controller.voices
        assert [g["label"] for g in voices] == ["Đã sao chép"]
        assert voices[0]["voices"][0]["id"] == "Solo"


class TestGenerate:
    def test_generate_submits_request_and_sets_busy(self, harness: Harness) -> None:
        harness.controller.generate("Xin chào", "Minh Đức")
        assert len(harness.workers) == 1  # lazily created
        assert harness.worker.started is True
        (request,) = harness.worker.submitted
        assert isinstance(request.request, TTSRequest)
        assert request.request.text == "Xin chào"
        assert request.request.voice == "Minh Đức"
        assert request.request.mode == "infer"
        assert harness.controller.busy is True

    def test_generate_uses_settings_temperature(self, qcoreapp, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.controller.temperature = 0.9
        h.controller.generate("hi", "")
        (request,) = h.worker.submitted
        assert request.request.temperature == pytest.approx(0.9)
        assert request.request.voice is None  # blank voice → SDK default

    def test_generate_ignores_blank_text(self, harness: Harness) -> None:
        harness.controller.generate("   ", "Adam")
        assert harness.workers == []
        assert harness.controller.busy is False

    def test_prewarm_creates_worker_and_submits_silent_warmup(self, harness: Harness) -> None:
        harness.controller.prewarm_engine()
        assert len(harness.workers) == 1  # lazily created, engine NOT loaded
        (op,) = harness.worker.submitted
        assert isinstance(op, WarmupOp)
        assert harness.controller.busy is False  # prewarm never blocks the UI
        assert harness.controller.errorText == ""

        # Already-initialized engine → no second warmup op.
        harness.engines[0].is_initialized = True
        harness.controller.prewarm_engine()
        assert len(harness.worker.submitted) == 1

    def test_generate_rejects_oversize_text_without_submitting(self, harness: Harness) -> None:
        # OOM guard: the worker retains the finished audio in RAM, so a
        # document-scale paste must be refused before any job starts.
        oversize = "a" * (GENERATE_CHAR_LIMIT + 1)
        harness.controller.generate(oversize, "Adam")
        assert harness.workers == []
        assert harness.controller.busy is False
        assert "quá dài" in harness.controller.errorText
        assert f"{GENERATE_CHAR_LIMIT:,}" in harness.controller.errorText

        harness.controller.generateStream(oversize, "Adam")
        assert harness.workers == []
        assert harness.controller.busy is False
        assert harness.controller.streamActive is False

    def test_generate_accepts_text_at_the_limit(self, harness: Harness) -> None:
        harness.controller.generate("a" * GENERATE_CHAR_LIMIT, "Adam")
        (request,) = harness.worker.submitted
        assert request.request.mode == "infer"

    def test_done_holds_audio_and_clears_busy(self, harness: Harness) -> None:
        harness.controller.generate("hi", "")
        audio = np.full(48_000, 0.2, dtype=np.float32)
        harness.worker.complete_last(audio)
        assert harness.controller.hasAudio is True
        assert harness.controller.busy is False
        assert harness.controller.progress == pytest.approx(1.0)
        assert harness.controller.errorText == ""

    def test_progress_updates_fraction(self, harness: Harness) -> None:
        harness.controller.generate("hi", "")
        harness.worker.progress_last(0, 1, "synthesizing")
        assert harness.controller.progress == pytest.approx(0.0)
        harness.worker.progress_last(1, 1, "synthesizing")
        assert harness.controller.progress == pytest.approx(1.0)

    def test_first_progress_flips_foreground_to_generating(self, harness: Harness) -> None:
        harness.controller.generate("hi", "")
        assert harness.controller.foregroundJobState == "queued"
        harness.worker.progress_last(1, 4)
        assert harness.controller.foregroundJobState == "generating"

    def test_late_progress_never_clobbers_cancel_requested(self, harness: Harness) -> None:
        harness.controller.generate("hi", "")
        harness.controller.cancel()
        assert harness.controller.foregroundJobState == "cancel_requested"
        # Emitted before the cancel, delivered after: must not revive.
        harness.worker.progress_last(1, 4)
        assert harness.controller.foregroundJobState == "cancel_requested"

    def test_error_surfaces_and_clears_busy(self, harness: Harness) -> None:
        harness.controller.generate("hi", "")
        harness.worker.fail_last("Voice 'X' not found")
        assert harness.controller.errorText == "Voice 'X' not found"
        assert harness.controller.busy is False

    def test_cancelled_resets_busy_silently(self, harness: Harness) -> None:
        fired: list[bool] = []
        harness.controller.cancelled.connect(lambda: fired.append(True))
        harness.controller.generate("hi", "")
        harness.worker.fail_last("Cancelled by user")
        assert harness.controller.busy is False
        assert harness.controller.errorText == ""  # not a scary error
        assert fired == [True]  # transient notification instead

    def test_cancel_calls_worker_cancel(self, harness: Harness) -> None:
        harness.controller.generate("hi", "")
        job = harness.worker.submitted[-1]
        harness.controller.cancel()
        assert harness.worker.cancelled_job_ids == [job.id]


class TestExport:
    def test_export_with_explicit_path(self, harness: Harness, tmp_path: Path) -> None:
        from vienetts_app.core.audio import read_wav

        harness.controller.generate("hi", "")
        harness.worker.complete_last(np.full(24_000, 0.3, dtype=np.float32))
        target = tmp_path / "out" / "clip.wav"
        assert harness.controller.exportWav(str(target)) is True
        assert harness.controller.lastExportPath == str(target)
        data, sr = read_wav(target)
        assert sr == 48_000  # synthesis audio is 48 kHz
        assert data.dtype == np.float32 and len(data) == 24_000

    def test_export_empty_path_uses_settings_output_dir(self, qcoreapp, tmp_path: Path) -> None:
        from vienetts_app.core.audio import read_wav

        out_dir = tmp_path / "exports"
        (tmp_path / "settings.json").write_text(
            json.dumps({"output_dir": str(out_dir)}), encoding="utf-8"
        )
        h = Harness(tmp_path)
        h.controller.generate("hi", "")
        h.worker.complete_last(np.full(1000, 0.1, dtype=np.float32))
        assert h.controller.exportWav("") is True
        path = Path(h.controller.lastExportPath)
        assert path.parent == out_dir
        assert re.fullmatch(r"vienetts_\d{8}_\d{6}\.wav", path.name)
        _data, sr = read_wav(path)
        assert sr == 48_000

    def test_export_without_audio_sets_error(self, harness: Harness, tmp_path: Path) -> None:
        assert harness.controller.exportWav(str(tmp_path / "x.wav")) is False
        assert harness.controller.errorText != ""
        assert not (tmp_path / "x.wav").exists()


class TestDefaultExportPath:
    def test_uses_standard_music_location(
        self, qcoreapp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            QStandardPaths,
            "writableLocation",
            staticmethod(lambda _loc: "/xdg/music"),
        )
        controller = AppController(data_dir=tmp_path, bg_runner=run_sync)
        path = controller._default_export_path()
        assert path.parent == Path("/xdg/music/VieNeuTTS")
        assert re.fullmatch(r"vienetts_\d{8}_\d{6}\.wav", path.name)

    def test_falls_back_to_home_music_when_location_empty(
        self, qcoreapp, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(
            QStandardPaths,
            "writableLocation",
            staticmethod(lambda _loc: ""),
        )
        controller = AppController(data_dir=tmp_path, bg_runner=run_sync)
        path = controller._default_export_path()
        assert path.parent == Path.home() / "Music" / "VieNeuTTS"


class TestImportDocument:
    def _import_and_collect(self, h: "Harness", path: str) -> dict[str, str]:
        got: dict[str, str] = {}
        h.controller.documentImported.connect(
            lambda p, t: got.update(path=p, text=t), Qt.DirectConnection
        )
        assert h.controller.importDocument(path) is True
        return got

    def test_import_txt_returns_text(self, qcoreapp, tmp_path: Path) -> None:
        doc = tmp_path / "note.txt"
        doc.write_text("Xin chào\nthế giới", encoding="utf-8")
        h = Harness(tmp_path)
        got = self._import_and_collect(h, str(doc))
        assert got["text"] == "Xin chào\nthế giới"
        assert h.controller.errorText == ""
        assert h.controller.importing is False

    def test_import_missing_file_error_and_empty(self, qcoreapp, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        got = self._import_and_collect(h, str(tmp_path / "nope.txt"))
        assert got["text"] == ""
        assert h.controller.errorText != ""

    def test_import_unsupported_extension(self, qcoreapp, tmp_path: Path) -> None:
        doc = tmp_path / "bad.xyz"
        doc.write_text("data", encoding="utf-8")
        h = Harness(tmp_path)
        got = self._import_and_collect(h, str(doc))
        assert got["text"] == ""
        assert ".xyz" in h.controller.errorText

    def test_import_corrupt_docx_no_crash(self, qcoreapp, tmp_path: Path) -> None:
        doc = tmp_path / "fake.docx"
        doc.write_bytes(b"not a zip")
        h = Harness(tmp_path)
        got = self._import_and_collect(h, str(doc))
        assert got["text"] == ""
        assert h.controller.errorText != ""


class TestVoiceOps:
    def test_add_voice_submits_voiceop(self, harness: Harness) -> None:
        harness.controller.addVoice("MyVoice", "/ref.wav", False)
        (job,) = harness.worker.submitted
        assert isinstance(job, SynthesisJob)
        op = job.request
        assert isinstance(op, VoiceOp)
        assert (op.op, op.name, op.clip_path, op.denoise) == ("add", "MyVoice", "/ref.wav", False)
        assert harness.controller.busy is True

    def test_add_done_refreshes_voices_and_clears_busy(self, qcoreapp, tmp_path: Path) -> None:
        saved: list[str] = []

        h = Harness(tmp_path, saved=lambda vd: list(saved))
        before = h.saved_calls
        h.controller.addVoice("Fresh", "/r.wav", True)
        saved.append("Fresh")
        h.worker.complete_last({"op": "add", "name": "Fresh"}, "cloning")
        assert h.saved_calls == before + 1  # the refresh rebuild
        cloned = h.controller.voices[-1]
        assert cloned["label"] == "Đã sao chép"
        assert [v["id"] for v in cloned["voices"]] == ["Fresh"]
        assert h.controller.busy is False

    def test_remove_voice_submits_and_refreshes(self, qcoreapp, tmp_path: Path) -> None:
        h = Harness(tmp_path, saved=lambda vd: ["Doomed"])
        h.controller.removeVoice("Doomed")
        (job,) = h.worker.submitted
        op = job.request
        assert (op.op, op.name) == ("remove", "Doomed")
        h.worker.complete_last({"op": "remove", "name": "Doomed"}, "cloning")
        # catalog rebuilt (cloned group still last, now sourced from the stub)
        assert h.controller.voices[-1]["label"] == "Đã sao chép"

    def test_denoise_preview_written_at_native_rate(self, harness: Harness, tmp_path: Path) -> None:
        from vienetts_app.core.audio import read_wav

        harness.controller.denoisePreview("/clip.wav")
        (job,) = harness.worker.submitted
        op = job.request
        assert (op.op, op.clip_path) == ("denoise", "/clip.wav")
        harness.worker.complete_last(
            {
                "op": "denoise",
                "audio": np.full(44_100, 0.25, dtype=np.float32),
                "sample_rate": 44_100,
            },
            "cloning",
        )
        preview = Path(harness.controller.previewPath)
        assert preview == tmp_path / "preview.wav"
        _data, sr = read_wav(preview)
        assert sr == 44_100  # denoise output is NOT 48 kHz

    def test_voice_op_error_surfaces(self, harness: Harness) -> None:
        harness.controller.addVoice("X", "/r.wav", True)
        harness.worker.fail_last("add failed")
        assert harness.controller.errorText == "add failed"
        assert harness.controller.busy is False


class TestSettingsSeam:
    def test_invalid_backend_ignored_with_error(self, harness: Harness) -> None:
        harness.controller.backend = "gpu"
        assert harness.controller.backend == "auto"  # unchanged
        assert "backend" in harness.controller.errorText

    def test_invalid_temperature_ignored(self, harness: Harness) -> None:
        harness.controller.temperature = 99.0
        assert harness.controller.temperature == pytest.approx(0.4)
        assert "temperature" in harness.controller.errorText

    def test_valid_theme_applies_and_persists(self, harness: Harness) -> None:
        harness.controller.theme = "dark"
        assert harness.controller.theme == "dark"
        data = json.loads((harness.tmp_path / "settings.json").read_text(encoding="utf-8"))
        assert data["theme"] == "dark"

    def test_valid_language_applies_and_persists(self, harness: Harness) -> None:
        harness.controller.language = "en"
        assert harness.controller.language == "en"
        data = json.loads((harness.tmp_path / "settings.json").read_text(encoding="utf-8"))
        assert data["language"] == "en"

    def test_invalid_language_ignored_with_error(self, harness: Harness) -> None:
        harness.controller.language = "fr"
        assert harness.controller.language == "system"
        assert "language" in harness.controller.errorText

    def test_valid_model_repo_applies_and_persists(self, harness: Harness) -> None:
        harness.controller.modelRepo = "someone/vieneu-tts-custom"
        assert harness.controller.modelRepo == "someone/vieneu-tts-custom"
        data = json.loads((harness.tmp_path / "settings.json").read_text(encoding="utf-8"))
        assert data["model_repo"] == "someone/vieneu-tts-custom"

    def test_blank_model_repo_resets_to_default(self, harness: Harness) -> None:
        harness.controller.modelRepo = "someone/vieneu-tts-custom"
        harness.controller.modelRepo = "   "  # blank → back to official default
        assert harness.controller.modelRepo == ""
        data = json.loads((harness.tmp_path / "settings.json").read_text(encoding="utf-8"))
        assert data["model_repo"] == ""

    def test_invalid_model_repo_ignored_with_error(self, harness: Harness) -> None:
        harness.controller.modelRepo = "no-slash"
        assert harness.controller.modelRepo == ""  # unchanged
        assert "model_repo" in harness.controller.errorText

    def test_applied_language_pinned_at_construction(self, qcoreapp, tmp_path: Path) -> None:
        (tmp_path / "settings.json").write_text(json.dumps({"language": "en"}), encoding="utf-8")
        harness = Harness(tmp_path)
        assert harness.controller.appliedLanguage == "en"
        harness.controller.language = "vi"
        # The startup value stays frozen — the LIVE swap is the bootstrap's
        # job (translator + retranslate); this property only pins what the
        # UI started with.
        assert harness.controller.appliedLanguage == "en"

    def test_valid_temperature_persists(self, harness: Harness) -> None:
        harness.controller.temperature = 1.2
        assert harness.controller.temperature == pytest.approx(1.2)
        data = json.loads((harness.tmp_path / "settings.json").read_text(encoding="utf-8"))
        assert data["temperature"] == pytest.approx(1.2)

    def test_default_voice_persists(self, harness: Harness) -> None:
        harness.controller.defaultVoice = "Minh Đức"
        assert harness.controller.defaultVoice == "Minh Đức"
        data = json.loads((harness.tmp_path / "settings.json").read_text(encoding="utf-8"))
        assert data["default_voice"] == "Minh Đức"

    def test_output_dir_persists(self, harness: Harness) -> None:
        harness.controller.outputDir = "/tmp/xyz"
        assert harness.controller.outputDir == "/tmp/xyz"


class TestNeedsRestart:
    def test_change_before_init_no_restart_flag(self, harness: Harness) -> None:
        harness.controller.backend = "onnx"
        assert harness.controller.needsRestart is False
        assert harness.controller.backend == "onnx"

    def test_change_after_init_sets_flag(self, harness: Harness) -> None:
        harness.controller.generate("hi", "")
        assert len(harness.engines) == 1
        harness.controller.backend = "torch"
        assert harness.controller.needsRestart is True

    def test_precision_change_after_init_sets_flag(self, harness: Harness) -> None:
        harness.controller.generate("hi", "")
        harness.controller.precision = "fp32"
        assert harness.controller.needsRestart is True

    def test_model_repo_change_after_init_sets_flag(self, harness: Harness) -> None:
        harness.controller.generate("hi", "")
        harness.controller.modelRepo = "someone/vieneu-tts-custom"
        assert harness.controller.needsRestart is True

    def test_invalid_change_after_init_no_flag(self, harness: Harness) -> None:
        harness.controller.generate("hi", "")
        harness.controller.backend = "quantum"
        assert harness.controller.needsRestart is False

    def test_engine_uses_current_settings(self, qcoreapp, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.controller.backend = "onnx"
        h.controller.precision = "fp32"
        h.controller.generate("hi", "")
        kwargs = h.engines[0].init_kwargs
        assert kwargs["backend"] == "onnx"
        assert kwargs["precision"] == "fp32"
        assert Path(kwargs["voices_dir"]) == tmp_path / "voices"

    def test_engine_uses_model_repo_setting(self, qcoreapp, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.controller.modelRepo = "someone/vieneu-tts-custom"
        h.controller.generate("hi", "")
        assert h.engines[0].init_kwargs["model_repo"] == "someone/vieneu-tts-custom"
        # Default (unset) still passes an explicit empty override = SDK default.
        second = tmp_path / "second"
        second.mkdir()
        h2 = Harness(second)
        h2.controller.generate("hi", "")
        assert h2.engines[0].init_kwargs.get("model_repo", "") == ""


class TestConsent:
    def test_acknowledge_persists_and_round_trips(self, qcoreapp, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        assert h.controller.consentGiven is False
        h.controller.acknowledgeConsent()
        assert h.controller.consentGiven is True
        data = json.loads((tmp_path / "cloning_consent.json").read_text(encoding="utf-8"))
        assert data == {"consent": True}
        # A fresh controller in the same data dir sees it.
        h2 = Harness(tmp_path)
        assert h2.controller.consentGiven is True

    def test_corrupt_consent_file_defaults_false(self, qcoreapp, tmp_path: Path) -> None:
        (tmp_path / "cloning_consent.json").write_text("not json", encoding="utf-8")
        h = Harness(tmp_path)
        assert h.controller.consentGiven is False


class TestLifecycle:
    def test_shutdown_stops_worker_and_closes_engine(self, harness: Harness) -> None:
        harness.controller.generate("hi", "")
        harness.controller.shutdown()
        assert harness.worker.stopped is True
        assert harness.engines[0].closed is True

    def test_shutdown_without_worker_is_safe(self, harness: Harness) -> None:
        harness.controller.shutdown()  # must not raise
        assert harness.controller.busy is False

    def test_shutdown_resets_needs_restart(self, harness: Harness) -> None:
        harness.controller.generate("hi", "")
        harness.controller.backend = "torch"
        assert harness.controller.needsRestart is True
        harness.controller.shutdown()
        assert harness.controller.needsRestart is False
        # Next engine uses the NEW backend.
        harness.controller.generate("again", "")
        assert harness.engines[1].init_kwargs["backend"] == "torch"

    def test_shutdown_defers_engine_close_while_worker_thread_alive(self, harness: Harness) -> None:
        # Regression: a worker stuck inside a non-cancellable SDK call used to
        # get its engine closed (and the running QThread dropped) — a native
        # crash at quit. The pair must be RETIRED instead, and a later
        # shutdown() finishes the teardown once the thread has exited.
        harness.controller.generate("hi", "")
        harness.worker.isRunning = lambda: True  # type: ignore[method-assign]
        harness.controller.shutdown()
        assert harness.worker.stopped is True
        assert harness.engines[0].closed is False  # deferred, not closed
        assert harness.controller._retired_workers == [(harness.worker, harness.engines[0])]

        harness.worker.isRunning = lambda: False  # type: ignore[method-assign]
        harness.controller.shutdown()  # retry path closes it
        assert harness.engines[0].closed is True
        assert harness.controller._retired_workers == []

    def test_setting_persist_failure_surfaces_error_not_traceback(
        self, harness: Harness, monkeypatch
    ) -> None:
        # Regression: _set_setting caught only ValueError; a disk-full/
        # read-only OSError raised straight through the Qt slot.
        import vienetts_app.ui.controller as controller_module

        def boom(_settings, _data_dir):
            raise OSError("disk full")

        monkeypatch.setattr(controller_module, "save_settings", boom)
        harness.controller.theme = "dark"  # must not raise
        assert "Could not save settings" in harness.controller.errorText
        assert "disk full" in harness.controller.errorText
        assert harness.controller.theme == "dark"  # applies live regardless


def test_controller_is_qobject_subclass() -> None:
    assert issubclass(AppController, QObject)


class TestStreaming:
    """FR-4.2: generateStream drives chunk playback through the real
    StreamPlaybackController (fake sink at the audio seam)."""

    def test_generate_stream_submits_stream_mode_and_raises_active(self, harness: Harness) -> None:
        harness.controller.generateStream("Xin chào", "Minh Đức")
        (request,) = harness.worker.submitted
        assert isinstance(request.request, TTSRequest)
        assert request.request.mode == "stream"
        assert request.request.text == "Xin chào"
        assert request.request.voice == "Minh Đức"
        assert harness.controller.busy is True
        assert harness.controller.streamActive is True

    def test_generate_stream_starts_content_safe_trace(
        self,
        qcoreapp,
        tmp_path: Path,
    ) -> None:
        recorder = PerformanceRecorder(enabled=True)
        harness = Harness(tmp_path, performance_recorder=recorder)
        harness.controller.generateStream("private words", "Minh Đức")

        (request,) = harness.worker.submitted
        assert request.id
        trace = recorder.snapshot(request.id)[0]
        assert trace["tags"] == {
            "char_count": 13,
            "mode": "stream",
            "streaming": True,
        }
        serialized = json.dumps(trace, ensure_ascii=False)
        assert "private words" not in serialized
        assert "Minh Đức" not in serialized

    def test_stream_trace_marks_controller_boundaries_and_completion(
        self,
        qcoreapp,
        tmp_path: Path,
    ) -> None:
        recorder = PerformanceRecorder(enabled=True)
        harness = Harness(tmp_path, performance_recorder=recorder)
        harness.controller.generateStream("hello", "")
        (request,) = harness.worker.submitted

        harness.worker.chunk_last(np.zeros(4, dtype=np.float32))
        harness.worker.complete_last(np.zeros(4, dtype=np.float32))

        (trace,) = recorder.snapshot(request.id)
        names = [event["name"] for event in trace["events"]]
        assert names.index("submitted") < names.index("controller_first_chunk")
        assert names.index("controller_first_chunk") < names.index("controller_done")
        assert trace["outcome"] == "completed"

    def test_sequential_submissions_receive_unique_job_ids(
        self,
        qcoreapp,
        tmp_path: Path,
    ) -> None:
        recorder = PerformanceRecorder(enabled=True)
        harness = Harness(tmp_path, performance_recorder=recorder)
        harness.controller.generate("first", "")
        harness.worker.complete_last(np.zeros(4, dtype=np.float32))
        harness.controller.generate("second", "")

        first, second = harness.worker.submitted
        assert first.id != second.id

    def test_cancel_and_error_finish_trace_with_distinct_outcomes(
        self,
        qcoreapp,
        tmp_path: Path,
    ) -> None:
        recorder = PerformanceRecorder(enabled=True)
        harness = Harness(tmp_path, performance_recorder=recorder)
        harness.controller.generateStream("cancel me", "")
        (cancel_request,) = harness.worker.submitted
        harness.controller.cancel()
        harness.worker.fail_last(CANCELLED_MESSAGE)
        (cancel_trace,) = recorder.snapshot(cancel_request.id)
        cancel_names = [event["name"] for event in cancel_trace["events"]]
        assert "submitted" in cancel_names
        assert "cancel_requested" in cancel_names
        assert cancel_trace["outcome"] == "cancelled"

        harness.controller.generate("fail me", "")
        fail_request = harness.worker.submitted[-1]
        harness.worker.fail_last("engine failed")
        (fail_trace,) = recorder.snapshot(fail_request.id)
        assert [event["name"] for event in fail_trace["events"]] == [
            "submitted",
            "controller_error",
        ]
        assert fail_trace["outcome"] == "failed"

    def test_generate_stream_uses_settings_temperature(self, harness: Harness) -> None:
        harness.controller.temperature = 0.9
        harness.controller.generateStream("hi", "")
        (request,) = harness.worker.submitted
        assert request.request.mode == "stream"
        assert request.request.temperature == pytest.approx(0.9)

    def test_chunk_ready_feeds_sink_bytes_in_order(self, harness: Harness) -> None:
        harness.controller.generateStream("hi", "")
        chunks = [
            np.arange(15_360, dtype=np.float32),
            np.full(1537, 0.5, dtype=np.float32),  # variable sizes...
            np.array([1.5], dtype=np.float32),  # ...incl. tiny tail chunk
        ]
        for chunk in chunks:
            harness.worker.chunk_last(chunk)
            assert harness.controller.streamLevel >= 0.0  # envelope per chunk
        device = harness.sink.device
        expected = b"".join(np.asarray(c, dtype="<f4").tobytes() for c in chunks)
        assert device.readData(len(expected) + 8) == expected

    def test_levels_surface_as_stream_level(self, harness: Harness) -> None:
        harness.controller.generateStream("hi", "")
        harness.worker.chunk_last(np.zeros(16, dtype=np.float32))
        assert harness.controller.streamLevel == pytest.approx(0.0)
        harness.worker.chunk_last(np.full(16, 0.5, dtype=np.float32))
        assert harness.controller.streamLevel == pytest.approx(0.5)

    def test_chunks_before_stream_are_ignored(self, qcoreapp, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        # Plain infer request: stream session never opened...
        h.controller.generate("plain", "")
        h.worker.chunk_ready.emit(np.ones(8, dtype=np.float32))  # ...so no feed
        assert h.sink.calls == []
        assert h.controller.streamLevel == pytest.approx(0.0)
        assert h.controller.streamActive is False

    def test_done_retains_audio_resets_active_export_works(
        self, harness: Harness, tmp_path: Path
    ) -> None:
        from vienetts_app.core.audio import read_wav

        harness.controller.generateStream("hi", "")
        full = np.full(48_000, 0.25, dtype=np.float32)
        harness.worker.chunk_last(np.full(24_000, 0.5, dtype=np.float32))
        harness.worker.complete_last(full)
        assert harness.controller.hasAudio is True
        assert harness.controller.busy is False
        # Done does NOT kill the meter instantly: 0.5 s of audio is still
        # buffered in the sink — streamActive holds until it drains (rqy),
        # then flips without any further event.
        assert harness.controller.streamActive is True
        assert wait_until(lambda: harness.controller.streamActive is False, timeout=3.0)
        target = tmp_path / "stream.wav"
        assert harness.controller.exportWav(str(target)) is True
        data, sr = read_wav(target)
        assert sr == 48_000 and len(data) == 48_000
        assert data.dtype == np.float32 and np.allclose(data[:4], [0.25] * 4)
        # Done lets the sink DRAIN: buffered bytes stay, sink keeps running.
        assert harness.sink.calls[-1] != "stop"

    def test_done_with_empty_buffer_flips_stream_active_immediately(self, harness: Harness) -> None:
        harness.controller.generateStream("hi", "")
        harness.worker.complete_last(np.zeros(8, dtype=np.float32))  # no chunk fed
        assert harness.controller.streamActive is False

    def test_drain_window_never_leaks_into_a_new_session(self, harness: Harness) -> None:
        # Session A done → drain window armed; a new synthesis must not have
        # its meter killed by A's stale timer.
        harness.controller.generateStream("first", "")
        harness.worker.chunk_last(np.full(24_000, 0.5, dtype=np.float32))
        harness.worker.complete_last(np.zeros(8, dtype=np.float32))
        assert harness.controller.streamActive is True  # draining
        harness.controller.generateStream("second", "")
        assert harness.controller.streamActive is True  # new session started
        assert harness.controller.busy is True
        # Long after A's window would have fired, B's meter is still live.
        time.sleep(0.9)
        app = QCoreApplication.instance()
        if app is not None:
            app.processEvents()
        assert harness.controller.streamActive is True

    def test_slot_cancel_stops_sink_immediately(self, harness: Harness) -> None:
        harness.controller.generateStream("hi", "")
        harness.worker.chunk_last(np.ones(64, dtype=np.float32))
        assert harness.controller.streamActive is True
        job = harness.worker.submitted[-1]
        harness.controller.cancel()
        assert harness.worker.cancelled_job_ids == [job.id]
        assert harness.controller.streamActive is False
        assert harness.sink.calls[-1] == "stop"  # FR-4.2: playback halts NOW
        assert harness.sink.device.readData(4096) == b""  # buffer dropped

    def test_cancelled_message_path_resets_without_error_text(self, harness: Harness) -> None:
        fired: list[bool] = []
        harness.controller.cancelled.connect(lambda: fired.append(True))
        harness.controller.generateStream("hi", "")
        harness.worker.chunk_last(np.ones(32, dtype=np.float32))
        harness.worker.fail_last("Cancelled by user")
        assert harness.controller.streamActive is False
        assert harness.controller.busy is False
        assert harness.controller.errorText == ""
        assert fired == [True]
        assert harness.sink.calls[-1] == "stop"

    def test_real_error_path_also_stops_playback(self, harness: Harness) -> None:
        harness.controller.generateStream("hi", "")
        harness.worker.chunk_last(np.ones(32, dtype=np.float32))
        harness.worker.fail_last("Voice 'X' not found")
        assert harness.controller.streamActive is False
        assert harness.controller.errorText == "Voice 'X' not found"
        assert harness.sink.calls[-1] == "stop"

    def test_blank_text_is_noop(self, harness: Harness) -> None:
        harness.controller.generateStream("   ", "Adam")
        assert harness.workers == []  # never built
        assert harness.controller.busy is False
        assert harness.controller.streamActive is False
        assert harness.sink.calls == []

    def test_new_generate_stops_previous_sink_session(self, harness: Harness) -> None:
        harness.controller.generateStream("first", "")
        harness.worker.chunk_last(np.ones(16, dtype=np.float32))
        harness.controller.generate("second", "")  # plain infer request
        assert harness.sink.calls[-1] == "stop"
        assert harness.controller.streamActive is False
        assert harness.worker.submitted[1].request.mode == "infer"

    def test_new_generate_stream_restarts_previous_sink_session(self, harness: Harness) -> None:
        harness.controller.generateStream("first", "")
        harness.worker.chunk_last(np.ones(16, dtype=np.float32))
        harness.controller.generateStream("second", "")
        # Previous session torn down (stop) before the fresh start().
        assert harness.sink.calls == ["start", "stop", "start"]
        assert harness.controller.streamActive is True
        assert harness.worker.submitted[1].request.mode == "stream"
        # Second session's chunks can still reach the SAME ring-buffer io.
        harness.worker.chunk_last(np.full(4, -0.75, dtype=np.float32))
        raw = harness.sink.device.readData(4096)
        assert np.frombuffer(raw[:4], dtype="<f4")[0] == pytest.approx(-0.75)

    def test_sink_construction_failure_surfaces_error_synthesis_completes(
        self, harness: Harness
    ) -> None:
        harness.failing_sink_factory = FailingSinkFactory()
        harness.controller.generateStream("hi", "")
        assert "không phát được âm thanh" in harness.controller.errorText
        # Synthesis still completes despite missing audio.
        audio = np.full(1000, 0.3, dtype=np.float32)
        harness.worker.complete_last(audio)
        assert harness.controller.hasAudio is True
        assert harness.controller.busy is False
        assert harness.controller.streamActive is False
        assert harness.failing_sink_factory.calls >= 1

    def test_stream_player_built_lazily(self, harness: Harness) -> None:
        assert harness.controller._stream_playback is None
        harness.controller.generateStream("hi", "")
        assert isinstance(harness.controller._stream_playback, StreamPlaybackController)
        assert harness.controller.streamActive is True


class TestReplay:
    """Phát without export: RAM replay for small audio, self-cleaning temp
    WAV for large; replayActive drives the Phát/Dừng toggle."""

    @staticmethod
    def finish_generation(harness: Harness) -> np.ndarray:
        harness.controller.generateStream("hi", "")
        audio = np.linspace(-0.5, 0.5, 960, dtype=np.float32)  # 20 ms
        harness.worker.complete_last(audio)
        return audio

    @staticmethod
    def force_temp_file_path(monkeypatch, harness: Harness) -> None:
        import vienetts_app.ui.controller as controller_module

        monkeypatch.setattr(controller_module, "REPLAY_MEMORY_LIMIT_BYTES", 64)
        assert harness.controller._audio.nbytes > 64  # 960 f32 samples = 3840 B

    def test_replay_without_audio_sets_error(self, harness: Harness) -> None:
        harness.controller.replay()
        assert "Chưa có gì để phát" in harness.controller.errorText
        assert harness.controller.replayActive is False

    def test_small_audio_replays_from_memory(self, harness: Harness) -> None:
        self.finish_generation(harness)
        harness.controller.replay()
        assert harness.controller.replayActive is True
        assert harness.controller.streamActive is True
        assert harness.sink.calls[-1] == "start"
        # Drain timer (20 ms + margin) ends the session by itself.
        assert wait_until(lambda: not harness.controller.replayActive)
        assert harness.controller.streamActive is False

    def test_replay_sink_failure_surfaces_error(self, harness: Harness) -> None:
        harness.failing_sink_factory = FailingSinkFactory()
        self.finish_generation(harness)
        harness.controller.replay()
        assert harness.controller.replayActive is False
        assert harness.sink.calls == []  # never started

    def test_large_audio_replays_via_temp_file_and_cleans_up(
        self, harness: Harness, monkeypatch, tmp_path: Path
    ) -> None:
        self.finish_generation(harness)
        self.force_temp_file_path(monkeypatch, harness)
        playback = FakeFilePlayback()
        harness.controller.attach_file_playback(playback)
        harness.controller.replay()
        assert harness.controller.replayActive is True
        assert harness.sink.calls == ["start"]  # generation only — RAM path untouched
        assert len(playback.played) == 1
        temp = Path(playback.played[0])
        assert temp.is_file()
        assert temp.parent != tmp_path  # system temp, never the user's folders
        playback.finished.emit()  # EndOfMedia
        assert harness.controller.replayActive is False
        assert not temp.exists()

    def test_stop_replay_ends_file_replay_and_deletes_temp(
        self, harness: Harness, monkeypatch
    ) -> None:
        self.finish_generation(harness)
        self.force_temp_file_path(monkeypatch, harness)
        playback = FakeFilePlayback()
        harness.controller.attach_file_playback(playback)
        harness.controller.replay()
        temp = Path(playback.played[0])
        harness.controller.stopReplay()
        assert harness.controller.replayActive is False
        assert playback.stops == 1
        assert not temp.exists()

    def test_stop_replay_ends_memory_replay(self, harness: Harness) -> None:
        self.finish_generation(harness)
        harness.controller.replay()
        harness.controller.stopReplay()
        assert harness.controller.replayActive is False
        assert harness.controller.streamActive is False
        assert harness.sink.calls[-1] == "stop"

    def test_replay_replaces_previous_file_replay(self, harness: Harness, monkeypatch) -> None:
        self.finish_generation(harness)
        self.force_temp_file_path(monkeypatch, harness)
        playback = FakeFilePlayback()
        harness.controller.attach_file_playback(playback)
        harness.controller.replay()
        first = Path(playback.played[0])
        harness.controller.replay()
        second = Path(playback.played[1])
        assert not first.exists()  # replaced replay removes its temp WAV
        assert second.is_file()
        assert playback.stops == 1  # the old file replay was stopped

    def test_new_generation_stops_replay_and_clears_temp(
        self, harness: Harness, monkeypatch
    ) -> None:
        self.finish_generation(harness)
        self.force_temp_file_path(monkeypatch, harness)
        playback = FakeFilePlayback()
        harness.controller.attach_file_playback(playback)
        harness.controller.replay()
        temp = Path(playback.played[0])
        harness.controller.generateStream("again", "")
        assert harness.controller.replayActive is False
        assert harness.controller.hasAudio is False
        assert not temp.exists()

    def test_file_finished_without_replay_is_ignored(self, harness: Harness) -> None:
        playback = FakeFilePlayback()
        harness.controller.attach_file_playback(playback)
        playback.finished.emit()  # e.g. a Cloning-tab preview reaching its end
        assert harness.controller.replayActive is False  # no crash, no state change

    def test_replay_without_file_player_surfaces_error(self, harness: Harness, monkeypatch) -> None:
        self.finish_generation(harness)
        self.force_temp_file_path(monkeypatch, harness)
        harness.controller.replay()  # no attach_file_playback
        assert harness.controller.replayActive is False
        assert "không phát được âm thanh" in harness.controller.errorText

    def test_file_error_mid_replay_ends_replay(self, harness: Harness, monkeypatch) -> None:
        # Regression: only `finished` was wired, so a decode/backend error
        # during the temp-WAV replay left replayActive stuck on "Dừng".
        self.finish_generation(harness)
        self.force_temp_file_path(monkeypatch, harness)
        playback = FakeFilePlayback()
        harness.controller.attach_file_playback(playback)
        harness.controller.replay()
        temp = Path(playback.played[0])
        assert harness.controller.replayActive is True
        playback.errorTextChanged.emit()  # backend error mid-play
        assert harness.controller.replayActive is False
        assert playback.stops == 1
        assert not temp.exists()

    def test_file_error_without_replay_is_ignored(self, harness: Harness, monkeypatch) -> None:
        # Errors from exported-file/preview playback on the shared player
        # must not manufacture a replay stop.
        self.finish_generation(harness)
        playback = FakeFilePlayback()
        harness.controller.attach_file_playback(playback)
        playback.errorTextChanged.emit()
        assert harness.controller.replayActive is False
        assert playback.stops == 0

    def test_shutdown_stops_replay_and_removes_temp(self, harness: Harness, monkeypatch) -> None:
        self.finish_generation(harness)
        self.force_temp_file_path(monkeypatch, harness)
        playback = FakeFilePlayback()
        harness.controller.attach_file_playback(playback)
        harness.controller.replay()
        temp = Path(playback.played[0])
        harness.controller.shutdown()
        assert harness.controller.replayActive is False
        assert playback.stops == 1
        assert not temp.exists()


class TestWaveformVisualization:
    """PlaybackWaveform feed: envelope overview + live replay playhead."""

    def test_initial_state_is_empty_and_parked(self, harness: Harness) -> None:
        assert harness.controller.waveformEnvelope == []
        assert harness.controller.replayPosition == 0.0
        assert harness.controller.replayDurationMs == 0

    def test_envelope_computed_on_done(self, harness: Harness) -> None:
        harness.controller.generateStream("hi", "")
        # Loud first half, silent second half → descending-then-zero buckets.
        audio = np.concatenate(
            [
                np.full(2_400, 0.5, dtype=np.float32),
                np.zeros(2_400, dtype=np.float32),
            ]
        )
        harness.worker.complete_last(audio)
        envelope = harness.controller.waveformEnvelope
        assert len(envelope) <= 160
        assert max(envelope) == pytest.approx(1.0)  # normalized to its peak
        assert min(envelope) == pytest.approx(0.0)
        assert envelope[0] == pytest.approx(1.0)
        assert envelope[-1] == pytest.approx(0.0)

    def test_constant_audio_fills_envelope(self, harness: Harness) -> None:
        harness.controller.generateStream("hi", "")
        harness.worker.complete_last(np.full(4_800, 0.25, dtype=np.float32))
        assert all(v == pytest.approx(1.0) for v in harness.controller.waveformEnvelope)

    def test_envelope_cleared_when_new_generation_starts(self, harness: Harness) -> None:
        TestReplay.finish_generation(harness)
        assert harness.controller.waveformEnvelope != []
        harness.controller.generateStream("again", "")
        assert harness.controller.waveformEnvelope == []
        assert harness.controller.hasAudio is False

    def test_memory_replay_advances_playhead(self, harness: Harness) -> None:
        harness.controller.generateStream("hi", "")
        # 0.5 s of audio: the 80 ms position timer ticks well inside the
        # 0.5 s + margin drain window.
        harness.worker.complete_last(np.zeros(24_000, dtype=np.float32))
        harness.controller.replay()
        assert harness.controller.replayDurationMs == 500
        assert wait_until(lambda: 0.0 < harness.controller.replayPosition < 1.0), (
            f"position={harness.controller.replayPosition}"
        )
        assert wait_until(lambda: not harness.controller.replayActive)
        assert harness.controller.replayPosition == 0.0  # parked after end

    def test_stop_replay_parks_playhead(self, harness: Harness) -> None:
        harness.controller.generateStream("hi", "")
        harness.worker.complete_last(np.zeros(24_000, dtype=np.float32))
        harness.controller.replay()
        assert wait_until(lambda: harness.controller.replayPosition > 0.0)
        harness.controller.stopReplay()
        assert harness.controller.replayPosition == 0.0
        assert not harness.controller._replay_pos_timer.isActive()

    def test_file_replay_position_mirrors_player(self, harness: Harness, monkeypatch) -> None:
        TestReplay.finish_generation(harness)
        TestReplay.force_temp_file_path(monkeypatch, harness)
        playback = FakeFilePlayback()
        harness.controller.attach_file_playback(playback)
        harness.controller.replay()
        assert harness.controller.replayDurationMs == 0  # player hasn't loaded yet
        playback.durationChanged.emit(60_000)
        assert harness.controller.replayDurationMs == 60_000
        playback.positionChanged.emit(30_000)
        assert harness.controller.replayPosition == pytest.approx(0.5)
        playback.finished.emit()
        assert harness.controller.replayActive is False
        assert harness.controller.replayPosition == 0.0

    def test_file_position_ignored_for_foreign_playback(self, harness: Harness) -> None:
        # Export/preview playback rides the same player without a replay:
        # its position/duration must never move OUR playhead.
        TestReplay.finish_generation(harness)
        playback = FakeFilePlayback()
        harness.controller.attach_file_playback(playback)
        playback.durationChanged.emit(90_000)
        playback.positionChanged.emit(45_000)
        assert harness.controller.replayPosition == 0.0
        assert harness.controller.replayDurationMs == 0


class TestModelsMissingFlag:
    """FR-4.6c: modelsMissing mirrors is_models_missing on the LAST error.

    Lifecycle contract: True only while the most recent error routed through
    _set_error carries the engine's marker prefix; cleared by any successful
    op start (_set_error("")); CANCELLED_MESSAGE never touches it (the cancel
    path bypasses _set_error entirely).
    """

    def test_false_initially(self, harness: Harness) -> None:
        assert harness.controller.modelsMissing is False

    def test_marker_error_through_real_error_path_sets_flag(self, harness: Harness) -> None:
        harness.controller.generate("hi", "")
        # Exact engine raise-site shape, travelling as a PLAIN STRING exactly
        # like the real InferenceWorker error signal delivers it.
        harness.worker.fail_last(str(ModelsMissingError(MODELS_MISSING_MESSAGE)))
        assert harness.controller.modelsMissing is True
        assert harness.controller.errorText.startswith(MODELS_MISSING_MARKER)
        assert FETCH_MODELS_COMMAND in harness.controller.errorText
        assert harness.controller.busy is False

    def test_generic_error_keeps_flag_false(self, harness: Harness) -> None:
        harness.controller.generate("hi", "")
        harness.worker.fail_last("Voice 'X' not found")
        assert harness.controller.modelsMissing is False

    def test_next_submit_clears_flag(self, harness: Harness) -> None:
        harness.controller.generate("hi", "")
        harness.worker.fail_last(str(ModelsMissingError(MODELS_MISSING_MESSAGE)))
        assert harness.controller.modelsMissing is True
        harness.controller.generate("again", "")
        # Re-evaluated on op start: generating again clears immediately.
        assert harness.controller.modelsMissing is False

    def test_flag_rearms_on_second_marker_error(self, harness: Harness) -> None:
        marker_message = str(ModelsMissingError(MODELS_MISSING_MESSAGE))
        harness.controller.generate("hi", "")
        harness.worker.fail_last(marker_message)
        harness.controller.generate("again", "")
        assert harness.controller.modelsMissing is False
        harness.worker.fail_last(marker_message)
        assert harness.controller.modelsMissing is True

    def test_cancelled_message_does_not_set_flag(self, harness: Harness) -> None:
        fired: list[bool] = []
        harness.controller.cancelled.connect(lambda: fired.append(True))
        harness.controller.generate("hi", "")
        harness.worker.fail_last("Cancelled by user")
        assert harness.controller.modelsMissing is False
        assert harness.controller.errorText == ""  # silent-reset policy intact
        assert fired == [True]

    def test_voice_op_error_with_marker_sets_flag(self, harness: Harness) -> None:
        harness.controller.addVoice("X", "/r.wav", True)
        harness.worker.fail_last(MODELS_MISSING_MESSAGE)
        assert harness.controller.modelsMissing is True


class TestAudioAvailability:
    """FR-4.6a: injectable probe, LAZY first-read evaluation, explicit refresh."""

    @staticmethod
    def make_controller(qcoreapp: Any, tmp_path: Path, probe: Any) -> AppController:
        del qcoreapp
        return AppController(
            data_dir=tmp_path,
            catalog=lambda: [],
            saved_names=lambda vd: [],
            audio_probe=probe,
        )

    def test_lazy_first_read_and_caching(self, qcoreapp: Any, tmp_path: Path) -> None:
        calls: list[int] = []

        def probe() -> bool:
            calls.append(1)
            return False

        controller = self.make_controller(qcoreapp, tmp_path, probe)
        assert calls == []  # LAZY: __init__ never touches the audio stack
        assert controller.audioAvailable is False
        assert len(calls) == 1
        assert controller.audioAvailable is False  # cached — no second probe
        assert len(calls) == 1

    def test_true_provider_case(self, qcoreapp: Any, tmp_path: Path) -> None:
        calls: list[int] = []

        def probe() -> bool:
            calls.append(1)
            return True

        controller = self.make_controller(qcoreapp, tmp_path, probe)
        assert controller.audioAvailable is True
        assert len(calls) == 1

    def test_refresh_reprobes_and_notifies_unconditionally(
        self, qcoreapp: Any, tmp_path: Path
    ) -> None:
        state = {"available": True}
        calls: list[int] = []

        def probe() -> bool:
            calls.append(1)
            return state["available"]

        controller = self.make_controller(qcoreapp, tmp_path, probe)
        assert controller.audioAvailable is True
        notified: list[bool] = []
        controller.audioAvailableChanged.connect(lambda: notified.append(True))

        state["available"] = False  # hot-unplug
        controller.refreshAudioAvailability()
        assert len(calls) == 2  # fresh probe, cache invalidated
        assert controller.audioAvailable is False
        assert notified == [True]

        state["available"] = True  # hot-plug back
        controller.refreshAudioAvailability()
        assert controller.audioAvailable is True
        assert len(calls) == 3
        # Unchanged result STILL notifies: documented refresh semantics.
        assert notified == [True, True]

    def test_broken_probe_treated_as_unavailable(self, qcoreapp: Any, tmp_path: Path) -> None:
        def probe() -> bool:
            raise RuntimeError("device service gone")

        controller = self.make_controller(qcoreapp, tmp_path, probe)
        assert controller.audioAvailable is False  # never crashes the UI


def test_worker_thread_safety_smoke(qcoreapp, tmp_path: Path) -> None:
    # The real worker path: fake engine + REAL InferenceWorker thread; ensure
    # the controller's signal wiring works across threads (queued connections).
    from vienetts_app.workers.inference_worker import InferenceWorker

    class StreamFakeEngine(FakeEngine):
        """Adds infer_stream so the threaded smoke covers the stream path."""

        def infer_stream(self, text, voice=None, **kw):
            yield np.full(2000, 0.1, dtype=np.float32)
            yield np.full(1200, -0.2, dtype=np.float32)  # variable-size chunk

    engine = StreamFakeEngine()
    real_worker = InferenceWorker(engine)
    h = Harness(tmp_path)
    h.controller._worker = real_worker
    h.controller._engine = real_worker.engine
    h.controller._connect_worker(real_worker)
    real_worker.start()
    try:
        h.controller.generateStream("threaded", "")
        ok = wait_until(lambda: h.controller.hasAudio and not h.controller.streamActive)
        assert ok, f"hasAudio={h.controller.hasAudio} stream={h.controller.streamActive}"
        assert h.controller.busy is False
        # Cross-thread chunks reached the ring buffer IN ORDER: queued signals
        # preserve emission order, so both chunk_ready events landed before
        # done ended the session.
        assert wait_until(lambda: h.sink.device is not None, timeout=2.0)
        raw = b""
        while True:
            more = h.sink.device.readData(65_536)
            if not more:
                break
            raw += more
        total = np.concatenate([np.full(2000, 0.1), np.full(1200, -0.2)]).astype(np.float32)
        got = np.frombuffer(raw, dtype="<f4")
        assert got.size == total.size
        np.testing.assert_allclose(got, total, atol=1e-7)
    finally:
        real_worker.stop()


class TestBgOpsAsync:
    """Production wiring: import/export run on the global thread pool."""

    def test_import_runs_on_pool_and_lands_via_queued_signal(
        self, qcoreapp, tmp_path: Path
    ) -> None:
        controller = AppController(data_dir=tmp_path)  # real bg_runner
        doc = tmp_path / "note.txt"
        doc.write_text("nhập bất đồng bộ", encoding="utf-8")
        got: dict[str, str] = {}
        controller.documentImported.connect(lambda p, t: got.update(path=p, text=t))
        assert controller.importDocument(str(doc)) is True
        assert controller.importing is True  # busy until delivery lands
        assert wait_until(lambda: bool(got), timeout=5.0)
        qcoreapp.processEvents()
        assert wait_until(lambda: controller.importing is False, timeout=5.0)
        qcoreapp.processEvents()
        assert got["text"] == "nhập bất đồng bộ"

    def test_export_runs_on_pool_and_lands_via_queued_signal(
        self, qcoreapp, tmp_path: Path
    ) -> None:
        from vienetts_app.core.audio import read_wav

        controller = AppController(data_dir=tmp_path)
        target = tmp_path / "async.wav"
        done: list[bool] = []
        controller.exportFinished.connect(lambda _p, ok: done.append(ok))
        # No audio yet → fast-fail, nothing queued.
        assert controller.exportWav(str(target)) is False
        # Hand it audio directly (generate path is covered elsewhere).
        controller._audio = np.full(4_800, 0.2, dtype=np.float32)
        assert controller.exportWav(str(target)) is True
        assert controller.exporting is True
        assert wait_until(lambda: bool(done), timeout=5.0)
        qcoreapp.processEvents()
        assert done == [True]
        assert wait_until(lambda: controller.exporting is False, timeout=5.0)
        data, sr = read_wav(target)
        assert sr == 48_000 and len(data) == 4_800


class _FakeModelLocation:
    def __init__(self, root: Path) -> None:
        from vienetts_app.core.model_manager import ManagedModelLocation

        self._inner = ManagedModelLocation(
            root=root,
            backbone_dir=root / "backbone",
            onnx_dir=root / "backbone" / "onnx_int8",
            codec_dir=root / "codec",
            format_version="official-v1",
            revision="rev",
        )

    def __getattr__(self, name: str):
        return getattr(self._inner, name)


def _model_status(state="unavailable", progress=0.0, error="", location=None):
    from vienetts_app.core.model_manager import ModelStatus

    total = 10
    installed = total if state == "ready" else 0
    if state == "ready":
        progress = 1.0
    return ModelStatus(
        state=state,
        installed_bytes=installed,
        required_bytes=total,
        progress=progress,
        error=error,
        location=location,
    )


class _FakeModelManager:
    """Injectable ModelManager double: inspect/install without Hub or disk."""

    def __init__(self, status=None) -> None:
        from pathlib import Path as _Path

        self.root = _Path("/fake/models")
        self.status = status or _model_status()
        self.inspect_calls = 0
        self.install_calls = 0
        self.cancel_calls = 0
        self.queued: list = []
        self.seen_cancel = None

    def queue_statuses(self, *statuses) -> None:
        self.queued.extend(statuses)

    def inspect(self):
        self.inspect_calls += 1
        if self.queued:
            self.status = self.queued.pop(0)
        return self.status

    def install(self, cancelled=lambda: False, on_progress=lambda _s: None):
        self.install_calls += 1
        self.seen_cancel = cancelled
        for queued_status in list(self.queued):
            self.queued.remove(queued_status)
            self.status = queued_status
            on_progress(queued_status)
            if cancelled():
                break
        return self.status

    def cancel_staging(self) -> None:
        self.cancel_calls += 1


def _harness_with_model_manager(tmp_path: Path, manager: _FakeModelManager) -> Harness:
    harness = Harness(tmp_path)
    # Swap in the fake manager post-construction (construction must not scan).
    harness.controller._model_manager = manager  # noqa: SLF001
    harness.model_manager = manager  # type: ignore[attr-defined]
    return harness


class TestModelSetup:
    def test_initial_state_is_checking_not_ready(self, qcoreapp, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        assert harness.controller.modelState == "checking"
        assert harness.controller.modelReady is False
        assert harness.engines == []

    def test_download_model_updates_state_without_initializing_engine(
        self, qcoreapp, tmp_path: Path
    ) -> None:
        manager = _FakeModelManager()
        manager.queue_statuses(
            _model_status("unavailable"),
            _model_status("downloading", progress=0.5),
            _model_status("ready", location=_FakeModelLocation(tmp_path)._inner),
        )
        harness = _harness_with_model_manager(tmp_path, manager)

        harness.controller.downloadOfficialModel()

        assert harness.controller.modelState == "ready"
        assert harness.controller.modelProgress == 1.0
        assert harness.engines == []

    def test_retry_refreshes_state_instead_of_dismissing_it(self, qcoreapp, tmp_path: Path) -> None:
        manager = _FakeModelManager(
            status=_model_status("unavailable", error="Network unavailable")
        )
        harness = _harness_with_model_manager(tmp_path, manager)

        harness.controller.refreshModelState()

        assert manager.inspect_calls == 1
        assert harness.controller.modelState == "unavailable"

    def test_custom_repo_refuses_download_with_explanation(self, qcoreapp, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        manager = _FakeModelManager()
        harness.controller._model_manager = manager  # noqa: SLF001
        harness.controller.modelRepo = "someone/custom"

        harness.controller.downloadOfficialModel()

        assert manager.install_calls == 0
        assert "advanced" in harness.controller.modelError.lower()

    def test_model_dir_points_at_versioned_install(self, qcoreapp, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        model_dir = Path(harness.controller.modelDir)
        assert model_dir.parent == (tmp_path / "models").resolve()
        assert model_dir.name == "official-v1"

    def test_copy_model_dir_returns_path_headless_safe(self, qcoreapp, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        assert harness.controller.copyModelDir() == harness.controller.modelDir

    def test_import_offline_pack_empty_path_explains_layout(self, qcoreapp, tmp_path: Path) -> None:
        harness = Harness(tmp_path)
        manager = _FakeModelManager()
        harness.controller._model_manager = manager  # noqa: SLF001

        harness.controller.importOfflinePack("")

        assert "backbone" in harness.controller.modelError
        assert manager.install_calls == 0

    def test_import_offline_pack_missing_dir_fails_without_crash(
        self, qcoreapp, tmp_path: Path
    ) -> None:
        harness = Harness(tmp_path)

        harness.controller.importOfflinePack("file://" + str(tmp_path / "no-pack"))

        assert harness.controller.modelState == "failed"
        assert harness.controller.modelError != ""

    def test_import_offline_pack_delegates_to_manager(
        self, qcoreapp, tmp_path: Path
    ) -> None:
        harness = Harness(tmp_path)
        manager = _FakeModelManager()
        calls: list[Path] = []

        def fake_install_offline_pack(source: Path):
            calls.append(Path(source))
            return _model_status("ready")

        manager.install_offline_pack = fake_install_offline_pack  # type: ignore[attr-defined]
        harness.controller._model_manager = manager  # noqa: SLF001

        harness.controller.importOfflinePack(str(tmp_path / "pack"))

        assert [str(p) for p in calls] == [str(tmp_path / "pack")]
        assert harness.controller.modelState == "ready"


def samples(count: int) -> np.ndarray:
    return np.zeros(count, dtype=np.float32)


def completed(job_id: str, owner: str, audio: np.ndarray) -> JobTerminal:
    return JobTerminal(job_id=job_id, owner=owner, state="completed", value=audio)  # type: ignore[arg-type]


def failed_terminal(job_id: str, owner: str, message: str) -> JobTerminal:
    return JobTerminal(job_id=job_id, owner=owner, state="failed", error=message)  # type: ignore[arg-type]


def cancelled_terminal(job_id: str, owner: str) -> JobTerminal:
    return JobTerminal(job_id=job_id, owner=owner, state="cancelled")  # type: ignore[arg-type]


class RecordingJobListener:
    """Audiobook-style listener: records tagged events by job ID."""

    def __init__(self) -> None:
        self.progress: list[Any] = []
        self.chunks: list[Any] = []
        self.terminals: list[JobTerminal] = []

    def on_synthesis_progress(self, event: Any) -> None:
        self.progress.append(event)

    def on_synthesis_chunk(self, event: Any) -> None:
        self.chunks.append(event)

    def on_synthesis_terminal(self, event: JobTerminal) -> None:
        self.terminals.append(event)


class TestJobIdentityRouting:
    """Phase 2 Task 3 RED: controller routes tagged events by job ID."""

    def test_controller_discards_stale_terminal_for_a_previous_job(self, harness: Harness) -> None:
        harness.controller.generateStream("first", "")
        first = harness.worker.submitted[-1]
        harness.worker.terminal.emit(completed(first.id, "text", samples(4)))

        harness.controller.generateStream("second", "")
        second = harness.worker.submitted[-1]
        harness.worker.terminal.emit(completed(first.id, "text", samples(8)))

        assert harness.controller.foregroundJobId == second.id
        assert harness.controller.busy is True
        assert harness.controller.hasAudio is False

    def test_cancel_targets_only_the_foreground_job(self, harness: Harness) -> None:
        harness.controller.generate("one", "")
        first = harness.worker.submitted[-1]
        harness.controller.cancel()

        assert harness.worker.cancelled_job_ids == [first.id]

    def test_text_terminal_never_invokes_audiobook_listener(self, harness: Harness) -> None:
        listener = RecordingJobListener()
        listener_job_id = harness.controller.submit_stream_for_listener(
            "chapter", "", listener, kind="requested_chapter"
        )
        assert listener_job_id

        harness.controller.generate("text job", "")
        text_job = harness.worker.submitted[-1]
        harness.worker.terminal.emit(completed(text_job.id, "text", samples(4)))

        assert listener.terminals == []
        assert harness.controller.hasAudio is True

    def test_audiobook_submit_leaves_text_busy_false(self, harness: Harness) -> None:
        listener = RecordingJobListener()
        job_id = harness.controller.submit_stream_for_listener("chapter text", "Adam", listener)

        assert job_id
        assert harness.controller.busy is False
        assert harness.controller.foregroundJobId == ""

    def test_terminalizing_one_audiobook_job_removes_only_its_listener(
        self, harness: Harness
    ) -> None:
        first_listener = RecordingJobListener()
        second_listener = RecordingJobListener()
        first_id = harness.controller.submit_stream_for_listener(
            "first", "", first_listener, kind="requested_chapter"
        )
        second_id = harness.controller.submit_stream_for_listener(
            "second", "", second_listener, kind="requested_chapter"
        )

        harness.worker.terminal.emit(completed(first_id, "audiobook", samples(4)))

        assert [t.job_id for t in first_listener.terminals] == [first_id]
        assert second_listener.terminals == []
        harness.worker.terminal.emit(completed(second_id, "audiobook", samples(4)))
        assert [t.job_id for t in second_listener.terminals] == [second_id]

    def test_foreground_state_tracks_lifecycle(self, harness: Harness) -> None:
        harness.controller.generate("hi", "")
        job = harness.worker.submitted[-1]

        assert harness.controller.foregroundJobId == job.id
        assert harness.controller.foregroundJobState == "queued"

        harness.worker.terminal.emit(completed(job.id, "text", samples(8)))

        assert harness.controller.foregroundJobState == "completed"
        assert harness.controller.busy is False
        assert harness.controller.hasAudio is True

    def test_connect_worker_uses_tagged_signals_only(self, harness: Harness) -> None:
        # The fake exposes ONLY the tagged surface: _connect_worker touching
        # a legacy done/error/voice_op_done signal would raise AttributeError.
        worker = harness.controller._ensure_worker()  # noqa: SLF001
        assert worker.started is True
        assert not hasattr(worker, "done")
