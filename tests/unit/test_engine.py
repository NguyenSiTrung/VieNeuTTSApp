"""TTSEngine: lazy single-instance ownership, SDK wrappers, error propagation."""

from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest

from vienetts_app.core.engine import TTSEngine, TTSEngineError


def silent(samples: int = 48_000) -> np.ndarray:
    return np.zeros(samples, dtype=np.float32)


class FakeVieneu:
    """Fake of the confirmed SDK surface (docs/spike-report.md §0)."""

    instances: list["FakeVieneu"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.backend = "onnx"
        self.sample_rate = 48_000
        self.closed = False
        self.calls: list[tuple[str, dict[str, Any]]] = []
        FakeVieneu.instances.append(self)

    def infer(
        self,
        text,
        voice=None,
        ref_audio=None,
        temperature=None,
        top_k=None,
        show_progress=True,
        **kw,
    ) -> np.ndarray:
        self.calls.append(
            (
                "infer",
                {
                    "text": text,
                    "voice": voice,
                    "ref_audio": ref_audio,
                    "temperature": temperature,
                    "show_progress": show_progress,
                },
            )
        )
        return silent(2400)

    def infer_stream(self, text, voice=None, **kw) -> Iterator[np.ndarray]:
        self.calls.append(("infer_stream", {"text": text, "voice": voice}))
        yield silent(15360)
        yield silent(23040)

    def infer_batch(self, texts, voice=None, **kw) -> list[np.ndarray]:
        self.calls.append(("infer_batch", {"texts": list(texts), "voice": voice}))
        return [silent(1000) for _ in texts]

    def list_preset_voices(self) -> list[tuple[str, str]]:
        return [("Label — Nam · Bắc", "Adam")]

    def add_voice(self, name, ref_audio, *, denoise=True, save=False, **kw) -> str:
        self.calls.append(("add_voice", {"name": name, "denoise": denoise, "save": save}))
        return name

    def remove_voice(self, name, *, save=False, **kw) -> None:
        self.calls.append(("remove_voice", {"name": name, "save": save}))

    def denoise(self, ref_audio, out_path=None, max_seconds=None):
        self.calls.append(("denoise", {"ref_audio": ref_audio}))
        return silent(44100), 44_100

    def save(self, audio, output_path) -> None:
        self.calls.append(("save", {"samples": len(audio), "path": str(output_path)}))

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_instances():
    FakeVieneu.instances = []
    yield
    FakeVieneu.instances = []


def make_engine(**kwargs: Any) -> TTSEngine:
    return TTSEngine(factory=lambda **kw: FakeVieneu(**kw), **kwargs)


class TestLazyInit:
    def test_factory_not_called_until_first_request(self) -> None:
        engine = make_engine()
        assert engine.is_initialized is False
        assert FakeVieneu.instances == []
        engine.infer("Xin chào")
        assert engine.is_initialized is True
        assert len(FakeVieneu.instances) == 1

    def test_initialized_once_across_many_requests(self) -> None:
        engine = make_engine()
        engine.infer("a")
        engine.infer_batch(["a", "b"])
        list(engine.infer_stream("c"))
        assert len(FakeVieneu.instances) == 1

    def test_init_kwargs_forwarded(self) -> None:
        engine = make_engine(backend="onnx", precision="int8")
        engine.infer("hi")
        assert FakeVieneu.instances[0].init_kwargs == {"backend": "onnx", "precision": "int8"}

    def test_sample_rate_available_after_init(self) -> None:
        engine = make_engine()
        with pytest.raises(TTSEngineError, match="not initialized"):
            _ = engine.sample_rate
        engine.infer("hi")
        assert engine.sample_rate == 48_000

    def test_close_resets_lazy_state(self) -> None:
        engine = make_engine()
        engine.infer("hi")
        first = FakeVieneu.instances[0]
        engine.close()
        assert first.closed is True
        assert engine.is_initialized is False
        engine.infer("hi again")
        assert len(FakeVieneu.instances) == 2


class TestWrappers:
    def test_infer_passes_voice_and_progress_off(self) -> None:
        engine = make_engine()
        audio = engine.infer("Xin chào", voice="Adam")
        assert audio.dtype == np.float32 and len(audio) == 2400
        op, kwargs = FakeVieneu.instances[0].calls[0]
        assert op == "infer"
        assert kwargs["voice"] == "Adam"
        assert kwargs["show_progress"] is False

    def test_infer_forwards_optional_sampling_params(self) -> None:
        engine = make_engine()
        engine.infer("hi", temperature=0.7, top_k=25)
        kwargs = FakeVieneu.instances[0].calls[0][1]
        assert kwargs["temperature"] == 0.7

    def test_infer_omits_none_temperature(self) -> None:
        engine = make_engine()
        engine.infer("hi")
        assert FakeVieneu.instances[0].calls[0][1]["temperature"] is None

    def test_infer_stream_yields_chunks(self) -> None:
        engine = make_engine()
        chunks = list(engine.infer_stream("text", voice="Adam"))
        assert [len(c) for c in chunks] == [15360, 23040]
        assert all(c.dtype == np.float32 for c in chunks)

    def test_infer_batch_returns_list(self) -> None:
        engine = make_engine()
        wavs = engine.infer_batch(["a", "b", "c"], voice="Adam")
        assert len(wavs) == 3

    def test_add_voice_round_trip(self) -> None:
        engine = make_engine()
        assert engine.add_voice("my", "/tmp/ref.wav", denoise=False, save=True) == "my"
        kwargs = FakeVieneu.instances[0].calls[0][1]
        assert kwargs == {"name": "my", "denoise": False, "save": True}

    def test_list_voices_delegates(self) -> None:
        engine = make_engine()
        assert engine.list_voices() == [("Label — Nam · Bắc", "Adam")]

    def test_denoise_returns_tuple(self) -> None:
        engine = make_engine()
        wav, sr = engine.denoise("/tmp/clip.wav")
        assert sr == 44_100 and wav.dtype == np.float32

    def test_save_delegates(self, tmp_path) -> None:
        engine = make_engine()
        engine.save(silent(100), tmp_path / "o.wav")
        kwargs = FakeVieneu.instances[0].calls[0][1]
        assert kwargs["samples"] == 100
        assert kwargs["path"].endswith("o.wav")


class TestErrorPropagation:
    def test_torch_missing_becomes_actionable_error(self) -> None:
        def factory(**kw: Any):
            raise ModuleNotFoundError("No module named 'torch'")

        engine = TTSEngine(factory=factory, backend="torch")
        with pytest.raises(TTSEngineError, match="onnx|gpu"):
            engine.infer("hi")

    def test_sdk_errors_wrapped_with_cause(self) -> None:
        class Boom(FakeVieneu):
            def infer(self, text, **kw):
                raise ValueError("Voice 'Nope' not found. Available: ['Adam']")

        engine = TTSEngine(factory=lambda **kw: Boom(**kw))
        with pytest.raises(TTSEngineError, match="Nope") as excinfo:
            engine.infer("hi", voice="Nope")
        assert isinstance(excinfo.value.__cause__, ValueError)

    def test_backend_attribute_after_init(self) -> None:
        engine = make_engine()
        engine.infer("hi")
        assert engine.backend == "onnx"
