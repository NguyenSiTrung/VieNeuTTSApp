"""Backend-neutral streaming contract + VieNeu adapter (Phase 1 Task 3)."""

from collections.abc import Iterator
from typing import Any

import numpy as np
import pytest

from vienetts_app.core.backends import BackendCapabilityError, get_capabilities
from vienetts_app.core.engine import TTSEngine
from vienetts_app.core.tts_backend import (
    TtsBackend,
    VieneuBackend,
    assert_backend_contract,
    create_backend,
    register_backend,
)


def silent(n: int, rate: int = 48000) -> np.ndarray:
    del rate
    return np.zeros(n, dtype=np.float32)


class FakeVieneu:
    instances: list["FakeVieneu"] = []

    def __init__(self, **kwargs: Any) -> None:
        self.init_kwargs = kwargs
        self.backend = "onnx"
        self.sample_rate = 48_000
        self.closed = False
        FakeVieneu.instances.append(self)

    def infer_stream(self, text, voice=None, **kw) -> Iterator[np.ndarray]:
        yield silent(15360)
        yield silent(23040)

    def close(self) -> None:
        self.closed = True


@pytest.fixture(autouse=True)
def _reset_instances():
    FakeVieneu.instances = []
    yield
    FakeVieneu.instances = []


class FakeQwenBackend:
    """Minimal second implementation proving the contract is backend-neutral."""

    engine_id = "qwen_customvoice"
    native_sample_rate = 24000

    def __init__(self) -> None:
        self._open = False
        self.closed_count = 0
        self.seen: list[dict[str, Any]] = []

    def initialize(self) -> None:
        self._open = True

    @property
    def is_initialized(self) -> bool:
        return self._open

    def synthesize_stream(
        self,
        text: str,
        *,
        voice: str | None = None,
        language: str | None = None,
        instruction: str | None = None,
        temperature: float | None = None,
    ) -> Iterator[np.ndarray]:
        del temperature
        self.seen.append(
            {"text": text, "voice": voice, "language": language, "instruction": instruction}
        )
        yield np.zeros(12000, dtype=np.float32)
        yield np.zeros(12000, dtype=np.float32)

    def close(self) -> None:
        self.closed_count += 1
        self._open = False


class TestContractConformance:
    def test_fake_qwen_backend_satisfies_contract(self) -> None:
        assert_backend_contract(FakeQwenBackend())

    def test_native_rate_matches_capabilities(self) -> None:
        backend = FakeQwenBackend()
        assert backend.native_sample_rate == get_capabilities("qwen_customvoice").native_sample_rate

    def test_contract_rejects_bad_chunks(self) -> None:
        class StereoBackend(FakeQwenBackend):
            def synthesize_stream(self, text: str, **kw: Any) -> Iterator[np.ndarray]:
                yield np.zeros((12000, 2), dtype=np.float32)

        with pytest.raises(AssertionError, match="mono"):
            assert_backend_contract(StereoBackend())


class TestVieneuBackend:
    def _backend(self) -> VieneuBackend:
        engine = TTSEngine(factory=lambda **kw: FakeVieneu(**kw))
        return VieneuBackend(engine)

    def test_identity_and_rate(self) -> None:
        backend = self._backend()
        assert backend.engine_id == "vieneu"
        assert backend.native_sample_rate == 48000
        assert isinstance(backend, TtsBackend)

    def test_satisfies_contract(self) -> None:
        assert_backend_contract(self._backend())

    def test_stream_matches_engine_chunked_path(self) -> None:
        backend = self._backend()
        backend.initialize()
        got = np.concatenate(list(backend.synthesize_stream("Xin chào", voice="Adam")))
        engine = TTSEngine(factory=lambda **kw: FakeVieneu(**kw))
        want = np.concatenate(list(engine.infer_stream_chunked("Xin chào", voice="Adam")))
        assert got.shape == want.shape
        assert got.dtype == np.float32

    def test_close_releases_engine(self) -> None:
        backend = self._backend()
        backend.initialize()
        assert backend.is_initialized
        backend.close()
        assert not backend.is_initialized
        backend.close()  # idempotent


class TestFactory:
    def test_create_vieneu(self) -> None:
        backend = create_backend("vieneu", engine_factory=lambda **kw: FakeVieneu(**kw))
        assert isinstance(backend, VieneuBackend)

    def test_create_unregistered_qwen_is_actionable(self) -> None:
        with pytest.raises(BackendCapabilityError, match="no backend registered"):
            create_backend("qwen_customvoice")

    def test_create_unknown_engine(self) -> None:
        with pytest.raises(BackendCapabilityError, match="unknown engine"):
            create_backend("bogus")  # type: ignore[arg-type]

    def test_register_and_create_custom(self) -> None:
        register_backend("qwen_customvoice", lambda **kw: FakeQwenBackend())
        try:
            assert isinstance(create_backend("qwen_customvoice"), FakeQwenBackend)
        finally:
            register_backend("qwen_customvoice", None)
