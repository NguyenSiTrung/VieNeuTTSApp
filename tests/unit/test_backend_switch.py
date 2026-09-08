"""Backend-switch lifecycle on the worker (Phase 3 Task 4)."""

import time
from pathlib import Path
from typing import Any

import numpy as np
import pytest

pytest.importorskip("PySide6")

from PySide6.QtCore import QCoreApplication  # noqa: E402
from tests.unit.test_engine import FakeVieneu  # noqa: E402

from vienetts_app.core.backends import BackendCapabilityError  # noqa: E402
from vienetts_app.core.engine import TTSEngine  # noqa: E402
from vienetts_app.core.jobs import JobTerminal, SynthesisJob  # noqa: E402
from vienetts_app.core.models import TTSRequest  # noqa: E402
from vienetts_app.core.tts_backend import TtsBackend, register_backend  # noqa: E402
from vienetts_app.workers.inference_worker import InferenceWorker  # noqa: E402


def sine24k(n: int) -> np.ndarray:
    t = np.arange(n, dtype=np.float64) / 24000.0
    return (0.4 * np.sin(2.0 * np.pi * 440.0 * t)).astype(np.float32)


class FakeQwenBackend(TtsBackend):
    events: list[tuple[str, str]] = []
    loads = 0

    def __init__(self, engine_id: str = "qwen_customvoice") -> None:
        self.engine_id = engine_id
        self.native_sample_rate = 24000
        self._open = False
        type(self).loads += 1

    def initialize(self) -> None:
        self._open = True
        type(self).events.append(("init", self.engine_id))

    @property
    def is_initialized(self) -> bool:
        return self._open

    def synthesize_stream(self, text, **kw):
        assert text.strip()
        yield sine24k(12000)
        yield sine24k(12000)

    def close(self) -> None:
        self._open = False
        type(self).events.append(("close", self.engine_id))


def wait_until(cond, timeout: float = 10.0) -> bool:
    app = QCoreApplication.instance()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if cond():
            return True
        if app is not None:
            app.processEvents()
        time.sleep(0.01)
    return False


def make_job(job_id: str, engine: str, language: str, path: Path) -> SynthesisJob:
    return SynthesisJob(
        id=job_id,
        owner="text",  # type: ignore[arg-type]
        kind="interactive",  # type: ignore[arg-type]
        priority=0,
        request=TTSRequest(text="hello world", engine=engine, language=language),  # type: ignore[arg-type]
        artifact_path=path,
        live_transport=None,
    )


@pytest.fixture
def rig(qcoreapp, tmp_path: Path):
    FakeVieneu.instances = []
    FakeQwenBackend.events = []
    FakeQwenBackend.loads = 0
    engine = TTSEngine(factory=lambda **kw: FakeVieneu(**kw))
    worker = InferenceWorker(engine)
    terminals: list[JobTerminal] = []
    worker.terminal.connect(terminals.append)
    worker.start()
    yield worker, terminals, tmp_path
    worker.stop()
    register_backend("qwen_customvoice", None)
    register_backend("qwen_base", None)


def run_job(rig, job_id: str, engine: str, language: str, name: str) -> JobTerminal:
    worker, terminals, tmp_path = rig
    job = make_job(job_id, engine, language, tmp_path / name)
    assert worker.submit(job) is True
    assert wait_until(lambda: any(t.job_id == job_id for t in terminals))
    (terminal,) = [t for t in terminals if t.job_id == job_id]
    return terminal


class TestBackendSwitch:
    def test_switch_closes_vieneu_before_loading_qwen(self, rig) -> None:
        worker, _terminals, _tmp = rig
        assert run_job(rig, "a" * 32, "vieneu", "vi", "a.wav").state == "completed"
        assert FakeVieneu.instances and not FakeVieneu.instances[0].closed

        def factory(**kw: Any) -> FakeQwenBackend:
            assert FakeVieneu.instances[0].closed, "VieNeu must close before Qwen loads"
            return FakeQwenBackend("qwen_customvoice")

        register_backend("qwen_customvoice", factory)
        worker.request_backend_switch("qwen_customvoice")
        terminal = run_job(rig, "b" * 32, "qwen_customvoice", "en", "b.wav")
        assert terminal.state == "completed"
        assert abs(terminal.value.samples - 48000) <= 1
        assert ("init", "qwen_customvoice") in FakeQwenBackend.events

    def test_consecutive_qwen_jobs_share_one_backend(self, rig) -> None:
        register_backend("qwen_customvoice", lambda **kw: FakeQwenBackend())
        worker, _t, _tmp = rig
        worker.request_backend_switch("qwen_customvoice")
        assert run_job(rig, "a" * 32, "qwen_customvoice", "en", "a.wav").state == "completed"
        assert run_job(rig, "b" * 32, "qwen_customvoice", "en", "b.wav").state == "completed"
        assert FakeQwenBackend.loads == 1

    def test_failed_switch_keeps_worker_usable(self, rig) -> None:
        from vienetts_app.core.engine import TTSEngineError

        def boom(**kw: Any) -> FakeQwenBackend:
            raise TTSEngineError("weights missing")

        register_backend("qwen_customvoice", boom)
        worker, _t, _tmp = rig
        worker.request_backend_switch("qwen_customvoice")
        terminal = run_job(rig, "a" * 32, "qwen_customvoice", "en", "a.wav")
        assert terminal.state == "failed"
        assert "weights missing" in (terminal.error or "")
        # Worker survives: VieNeu still works afterwards.
        assert run_job(rig, "b" * 32, "vieneu", "vi", "b.wav").state == "completed"

    def test_inflight_job_finishes_on_old_backend(self, rig) -> None:
        register_backend("qwen_customvoice", lambda **kw: FakeQwenBackend())
        worker, terminals, _tmp = rig
        worker.request_backend_switch("qwen_customvoice")
        assert run_job(rig, "a" * 32, "qwen_customvoice", "en", "a.wav").state == "completed"
        # Switch back: the next boundary closes Qwen and re-inits VieNeu.
        worker.request_backend_switch("vieneu")
        assert worker.pending_engine == "vieneu"
        assert run_job(rig, "b" * 32, "vieneu", "vi", "b.wav").state == "completed"
        assert ("close", "qwen_customvoice") in FakeQwenBackend.events
        assert worker.current_engine == "vieneu"

    def test_unknown_switch_rejected_immediately(self, rig) -> None:
        worker, _t, _tmp = rig
        with pytest.raises(BackendCapabilityError, match="unknown engine"):
            worker.request_backend_switch("bogus")  # type: ignore[arg-type]

    def test_same_engine_switch_is_noop(self, rig) -> None:
        register_backend("qwen_customvoice", lambda **kw: FakeQwenBackend())
        worker, _t, _tmp = rig
        worker.request_backend_switch("vieneu")
        assert run_job(rig, "a" * 32, "vieneu", "vi", "a.wav").state == "completed"
        assert FakeQwenBackend.loads == 0
        assert worker.current_engine == "vieneu"
