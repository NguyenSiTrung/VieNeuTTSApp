"""AppController: QML-facing app state — catalog, synthesis, voice ops, settings.

Every dependency is injectable (data_dir, engine/worker factories, catalog and
saved-names functions); construction must NOT create the worker or initialize
the engine (NFR-3.1). Fakes stand in for both — no vieneu model, no QThread.
"""

import json
import re
import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication, QObject, Signal  # noqa: E402

from vienetts_app.core.models import TTSRequest, VoiceOp  # noqa: E402
from vienetts_app.ui.controller import AppController  # noqa: E402


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


@pytest.fixture()
def qcoreapp():
    app = QCoreApplication.instance() or QCoreApplication([])
    yield app


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
    """Same signal surface as InferenceWorker; records submissions."""

    progress = Signal(object)
    chunk_ready = Signal(object)
    done = Signal(object)
    error = Signal(str)
    voice_op_done = Signal(object)

    def __init__(self, engine: Any) -> None:
        super().__init__()
        self.engine = engine
        self.submitted: list[Any] = []
        self.cancelled = 0
        self.stopped = False
        self.started = False

    def start(self) -> None:
        self.started = True

    def submit(self, item: Any) -> None:
        self.submitted.append(item)

    def cancel(self) -> None:
        self.cancelled += 1

    def stop(self) -> None:
        self.stopped = True


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
    def __init__(self, tmp_path: Path, catalog=None, saved=None) -> None:
        self.tmp_path = tmp_path
        self.engines: list[FakeEngine] = []
        self.workers: list[FakeWorker] = []
        self.catalog_calls = 0
        self.saved_calls = 0
        self._saved_stub = saved if saved is not None else (lambda voices_dir: [])

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

        self.engine_factory = engine_factory
        self.controller = AppController(
            data_dir=tmp_path,
            engine_factory=engine_factory,
            worker_factory=worker_factory,
            catalog=catalog_fn,
            saved_names=saved_fn,
        )

    @property
    def worker(self) -> FakeWorker:
        return self.workers[-1]


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
        assert isinstance(request, TTSRequest)
        assert request.text == "Xin chào"
        assert request.voice == "Minh Đức"
        assert request.mode == "infer"
        assert harness.controller.busy is True

    def test_generate_uses_settings_temperature(self, qcoreapp, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        h.controller.temperature = 0.9
        h.controller.generate("hi", "")
        (request,) = h.worker.submitted
        assert request.temperature == pytest.approx(0.9)
        assert request.voice is None  # blank voice → SDK default

    def test_generate_ignores_blank_text(self, harness: Harness) -> None:
        harness.controller.generate("   ", "Adam")
        assert harness.workers == []
        assert harness.controller.busy is False

    def test_done_holds_audio_and_clears_busy(self, harness: Harness) -> None:
        harness.controller.generate("hi", "")
        audio = np.full(48_000, 0.2, dtype=np.float32)
        harness.worker.done.emit(audio)
        assert harness.controller.hasAudio is True
        assert harness.controller.busy is False
        assert harness.controller.progress == pytest.approx(1.0)
        assert harness.controller.errorText == ""

    def test_progress_updates_fraction(self, harness: Harness) -> None:
        from vienetts_app.core.models import TTSProgress

        harness.controller.generate("hi", "")
        harness.worker.progress.emit(TTSProgress(done=0, total=1, stage="synthesizing"))
        assert harness.controller.progress == pytest.approx(0.0)
        harness.worker.progress.emit(TTSProgress(done=1, total=1, stage="synthesizing"))
        assert harness.controller.progress == pytest.approx(1.0)

    def test_error_surfaces_and_clears_busy(self, harness: Harness) -> None:
        harness.controller.generate("hi", "")
        harness.worker.error.emit("Voice 'X' not found")
        assert harness.controller.errorText == "Voice 'X' not found"
        assert harness.controller.busy is False

    def test_cancelled_resets_busy_silently(self, harness: Harness) -> None:
        fired: list[bool] = []
        harness.controller.cancelled.connect(lambda: fired.append(True))
        harness.controller.generate("hi", "")
        harness.worker.error.emit("Cancelled by user")
        assert harness.controller.busy is False
        assert harness.controller.errorText == ""  # not a scary error
        assert fired == [True]  # transient notification instead

    def test_cancel_calls_worker_cancel(self, harness: Harness) -> None:
        harness.controller.generate("hi", "")
        harness.controller.cancel()
        assert harness.worker.cancelled == 1


class TestExport:
    def test_export_with_explicit_path(self, harness: Harness, tmp_path: Path) -> None:
        from vienetts_app.core.audio import read_wav

        harness.controller.generate("hi", "")
        harness.worker.done.emit(np.full(24_000, 0.3, dtype=np.float32))
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
        h.worker.done.emit(np.full(1000, 0.1, dtype=np.float32))
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


class TestImportDocument:
    def test_import_txt_returns_text(self, qcoreapp, tmp_path: Path) -> None:
        doc = tmp_path / "note.txt"
        doc.write_text("Xin chào\nthế giới", encoding="utf-8")
        h = Harness(tmp_path)
        assert h.controller.importDocument(str(doc)) == "Xin chào\nthế giới"
        assert h.controller.errorText == ""

    def test_import_missing_file_error_and_empty(self, qcoreapp, tmp_path: Path) -> None:
        h = Harness(tmp_path)
        result = h.controller.importDocument(str(tmp_path / "nope.txt"))
        assert result == ""
        assert h.controller.errorText != ""

    def test_import_unsupported_extension(self, qcoreapp, tmp_path: Path) -> None:
        doc = tmp_path / "bad.xyz"
        doc.write_text("data", encoding="utf-8")
        h = Harness(tmp_path)
        assert h.controller.importDocument(str(doc)) == ""
        assert ".xyz" in h.controller.errorText

    def test_import_corrupt_docx_no_crash(self, qcoreapp, tmp_path: Path) -> None:
        doc = tmp_path / "fake.docx"
        doc.write_bytes(b"not a zip")
        h = Harness(tmp_path)
        assert h.controller.importDocument(str(doc)) == ""
        assert h.controller.errorText != ""


class TestVoiceOps:
    def test_add_voice_submits_voiceop(self, harness: Harness) -> None:
        harness.controller.addVoice("MyVoice", "/ref.wav", False)
        (op,) = harness.worker.submitted
        assert isinstance(op, VoiceOp)
        assert (op.op, op.name, op.clip_path, op.denoise) == ("add", "MyVoice", "/ref.wav", False)
        assert harness.controller.busy is True

    def test_add_done_refreshes_voices_and_clears_busy(self, qcoreapp, tmp_path: Path) -> None:
        saved: list[str] = []

        h = Harness(tmp_path, saved=lambda vd: list(saved))
        before = h.saved_calls
        h.controller.addVoice("Fresh", "/r.wav", True)
        saved.append("Fresh")
        h.worker.voice_op_done.emit({"op": "add", "name": "Fresh"})
        assert h.saved_calls == before + 1  # the refresh rebuild
        cloned = h.controller.voices[-1]
        assert cloned["label"] == "Đã sao chép"
        assert [v["id"] for v in cloned["voices"]] == ["Fresh"]
        assert h.controller.busy is False

    def test_remove_voice_submits_and_refreshes(self, qcoreapp, tmp_path: Path) -> None:
        h = Harness(tmp_path, saved=lambda vd: ["Doomed"])
        h.controller.removeVoice("Doomed")
        (op,) = h.worker.submitted
        assert (op.op, op.name) == ("remove", "Doomed")
        h.worker.voice_op_done.emit({"op": "remove", "name": "Doomed"})
        # catalog rebuilt (cloned group still last, now sourced from the stub)
        assert h.controller.voices[-1]["label"] == "Đã sao chép"

    def test_denoise_preview_written_at_native_rate(self, harness: Harness, tmp_path: Path) -> None:
        from vienetts_app.core.audio import read_wav

        harness.controller.denoisePreview("/clip.wav")
        (op,) = harness.worker.submitted
        assert (op.op, op.clip_path) == ("denoise", "/clip.wav")
        harness.worker.voice_op_done.emit(
            {
                "op": "denoise",
                "audio": np.full(44_100, 0.25, dtype=np.float32),
                "sample_rate": 44_100,
            }
        )
        preview = Path(harness.controller.previewPath)
        assert preview == tmp_path / "preview.wav"
        _data, sr = read_wav(preview)
        assert sr == 44_100  # denoise output is NOT 48 kHz

    def test_voice_op_error_surfaces(self, harness: Harness) -> None:
        harness.controller.addVoice("X", "/r.wav", True)
        harness.worker.error.emit("add failed")
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


def test_controller_is_qobject_subclass() -> None:
    assert issubclass(AppController, QObject)


def test_worker_thread_safety_smoke(qcoreapp, tmp_path: Path) -> None:
    # The real worker path: fake engine + REAL InferenceWorker thread; ensure
    # the controller's signal wiring works across threads (queued connections).
    from vienetts_app.workers.inference_worker import InferenceWorker

    h = Harness(tmp_path)
    real_worker = InferenceWorker(h.engines[0] if h.engines else FakeEngine())
    h.controller._worker = real_worker
    h.controller._engine = real_worker.engine
    h.controller._connect_worker(real_worker)
    real_worker.start()
    try:
        h.controller.generate("threaded", "")
        assert wait_until(lambda: h.controller.hasAudio)
        assert h.controller.busy is False
    finally:
        real_worker.stop()
