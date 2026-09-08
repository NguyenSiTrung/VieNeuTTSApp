"""Qwen model profiles and loaders (Phase 3 Task 2)."""

import numpy as np
import pytest

from vienetts_app.core.backends import BackendCapabilityError, get_capabilities
from vienetts_app.core.qwen_backend import (
    LANGUAGE_NAMES,
    QwenBackend,
    load_qwen_model,
    qwen_language_name,
)
from vienetts_app.core.qwen_runtime import (
    QWEN_BASE_REPO,
    QWEN_CUSTOMVOICE_REPO,
    QwenRuntimeError,
)
from vienetts_app.core.tts_backend import assert_backend_contract


def tone(n: int, freq: float = 440.0) -> np.ndarray:
    t = np.arange(n, dtype=np.float64) / 24000.0
    return (0.4 * np.sin(2.0 * np.pi * freq * t)).astype(np.float32)


class FakeQwenModel:
    """Duck type of qwen_tts.Qwen3TTSModel (official card signatures)."""

    def __init__(self, sr: int = 24000) -> None:
        self.sr = sr
        self.custom_calls: list[dict] = []
        self.clone_calls: list[dict] = []

    def generate_custom_voice(self, text, language, speaker, instruct):
        self.custom_calls.append(
            {"text": text, "language": language, "speaker": speaker, "instruct": instruct}
        )
        return [tone(48000)], self.sr

    def generate_voice_clone(self, text, language, ref_audio, ref_text):
        self.clone_calls.append(
            {"text": text, "language": language, "ref_audio": ref_audio, "ref_text": ref_text}
        )
        return [tone(24000)], self.sr


class TestLanguageNames:
    def test_covers_all_capability_languages(self) -> None:
        for engine in ("qwen_customvoice", "qwen_base"):
            for code in get_capabilities(engine).languages:  # type: ignore[arg-type]
                assert qwen_language_name(code) in LANGUAGE_NAMES.values()

    def test_english_chinese_korean(self) -> None:
        assert qwen_language_name("en") == "English"
        assert qwen_language_name("zh") == "Chinese"
        assert qwen_language_name("ko") == "Korean"

    def test_unknown_code_rejected(self) -> None:
        with pytest.raises(BackendCapabilityError, match="language"):
            qwen_language_name("vi")
        with pytest.raises(BackendCapabilityError, match="language"):
            qwen_language_name("xx")


class TestCustomVoiceBackend:
    def test_identity_and_contract(self) -> None:
        backend = QwenBackend.custom_voice(FakeQwenModel())
        assert backend.engine_id == "qwen_customvoice"
        assert backend.native_sample_rate == 24000
        assert_backend_contract(backend, voice="Ryan", language="en")

    def test_forwards_speaker_instruction_language(self) -> None:
        model = FakeQwenModel()
        backend = QwenBackend.custom_voice(model)
        out = np.concatenate(
            list(backend.synthesize_stream("hi", voice="Ryan", language="en", instruction="calm"))
        )
        assert len(out) == 48000
        assert out.dtype == np.float32
        (call,) = model.custom_calls
        assert call == {
            "text": "hi",
            "language": "English",
            "speaker": "Ryan",
            "instruct": "calm",
        }

    def test_chunks_bounded_for_progress(self) -> None:
        backend = QwenBackend.custom_voice(FakeQwenModel())
        chunks = list(backend.synthesize_stream("hi", voice="Ryan", language="en"))
        assert len(chunks) > 1
        assert all(len(c) <= 12000 for c in chunks)

    def test_empty_instruction_ok(self) -> None:
        model = FakeQwenModel()
        backend = QwenBackend.custom_voice(model)
        list(backend.synthesize_stream("hi", voice="Ryan", language="en"))
        assert model.custom_calls[0]["instruct"] == ""

    def test_sr_drift_raises_actionably(self) -> None:
        backend = QwenBackend.custom_voice(FakeQwenModel(sr=16000))
        with pytest.raises(BackendCapabilityError, match="sample rate"):
            list(backend.synthesize_stream("hi", voice="Ryan", language="en"))

    def test_missing_speaker_rejected_with_choices(self) -> None:
        backend = QwenBackend.custom_voice(FakeQwenModel())
        with pytest.raises(BackendCapabilityError, match="Ryan"):
            list(backend.synthesize_stream("hi", language="en"))

    def test_unknown_speaker_rejected_with_choices(self) -> None:
        backend = QwenBackend.custom_voice(FakeQwenModel())
        with pytest.raises(BackendCapabilityError, match="Vivian"):
            list(backend.synthesize_stream("hi", voice="Nobody", language="en"))

    @pytest.mark.parametrize(
        ("code", "name", "text"),
        [
            ("en", "English", "hello world"),
            ("zh", "Chinese", "你好世界"),
            ("ko", "Korean", "안녕하세요"),
        ],
    )
    def test_en_zh_ko_forwarding(self, code: str, name: str, text: str) -> None:
        model = FakeQwenModel()
        backend = QwenBackend.custom_voice(model)
        out = np.concatenate(list(backend.synthesize_stream(text, voice="Sohee", language=code)))
        assert out.dtype == np.float32 and out.size > 0
        assert model.custom_calls[0]["language"] == name
        assert model.custom_calls[0]["text"] == text

    def test_close_idempotent(self) -> None:
        backend = QwenBackend.custom_voice(FakeQwenModel())
        backend.close()
        backend.close()


class TestBaseBackend:
    def test_identity_and_contract(self) -> None:
        backend = QwenBackend.base(FakeQwenModel(), ref_audio="/r.wav", ref_text="hello")
        assert backend.engine_id == "qwen_base"
        assert_backend_contract(backend, language="en")

    def test_missing_reference_rejected(self) -> None:
        backend = QwenBackend.base(FakeQwenModel())
        with pytest.raises(BackendCapabilityError, match="[Rr]eference"):
            list(backend.synthesize_stream("hi", language="en"))

    def test_forwards_reference(self) -> None:
        model = FakeQwenModel()
        backend = QwenBackend.base(model, ref_audio="/r.wav", ref_text="hello there")
        out = np.concatenate(list(backend.synthesize_stream("hi", language="en")))
        assert len(out) == 24000
        (call,) = model.clone_calls
        assert call["ref_audio"] == "/r.wav"
        assert call["ref_text"] == "hello there"
        assert call["language"] == "English"


class TestLoader:
    def test_load_customvoice_uses_repo_and_cuda(self) -> None:
        seen: dict = {}

        def factory(repo, device_map, dtype):
            seen.update(repo=repo, device_map=device_map, dtype=dtype)
            return FakeQwenModel()

        backend = load_qwen_model(
            "qwen_customvoice",
            model_factory=factory,
            device_fn=lambda: ("cuda", "x"),
            dtype_fn=lambda device: f"dtype-{device}",
        )
        assert isinstance(backend, QwenBackend)
        assert seen["repo"] == QWEN_CUSTOMVOICE_REPO
        assert seen["device_map"] == "cuda:0"
        assert seen["dtype"] == "dtype-cuda"

    def test_load_base_registers_and_routes(self) -> None:
        from vienetts_app.core.tts_backend import create_backend, register_backend

        register_backend(
            "qwen_base",
            lambda **kw: load_qwen_model(
                "qwen_base", model_factory=lambda repo, **fkw: FakeQwenModel(), **kw
            ),
        )
        try:
            backend = create_backend("qwen_base", ref_audio="/r.wav", ref_text="hi")
            assert isinstance(backend, QwenBackend)
            assert backend.engine_id == "qwen_base"
        finally:
            register_backend("qwen_base", None)

    def test_missing_runtime_is_actionable(self) -> None:
        with pytest.raises(QwenRuntimeError, match="qwen-tts"):
            load_qwen_model(
                "qwen_customvoice",
                model_factory=None,
                qwen_import=lambda: (_ for _ in ()).throw(ImportError("nope")),
            )

    def test_base_repo_used(self) -> None:
        seen: dict = {}

        def factory(repo, **kw):
            seen["repo"] = repo
            return FakeQwenModel()

        load_qwen_model("qwen_base", model_factory=factory)
        assert seen["repo"] == QWEN_BASE_REPO
