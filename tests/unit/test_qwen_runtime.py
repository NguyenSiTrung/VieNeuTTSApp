"""Optional Qwen runtime boundary (Phase 3 Task 1)."""

import pytest

from vienetts_app.core.qwen_runtime import (
    QWEN_BASE_REPO,
    QWEN_CUSTOMVOICE_REPO,
    QWEN_NATIVE_RATE,
    QWEN_PACKAGE,
    QwenRuntimeError,
    describe_qwen_device,
    require_qwen,
)


class TestConstants:
    def test_repos_and_package(self) -> None:
        assert QWEN_PACKAGE == "qwen-tts"
        assert QWEN_CUSTOMVOICE_REPO == "Qwen/Qwen3-TTS-12Hz-0.6B-CustomVoice"
        assert QWEN_BASE_REPO == "Qwen/Qwen3-TTS-12Hz-0.6B-Base"
        assert QWEN_NATIVE_RATE == 24000


class TestRequireQwen:
    def test_missing_package_raises_actionably(self) -> None:
        with pytest.raises(QwenRuntimeError, match="qwen-tts"):
            require_qwen(import_fn=lambda name: (_ for _ in ()).throw(ImportError("nope")))

    def test_message_names_install_and_vram(self) -> None:
        try:
            require_qwen(import_fn=lambda name: (_ for _ in ()).throw(ImportError("nope")))
        except QwenRuntimeError as exc:
            message = str(exc)
            assert "qwen" in message.lower()
            assert "torch" in message.lower() or "CUDA" in message
        else:  # pragma: no cover
            raise AssertionError("expected QwenRuntimeError")

    def test_injected_module_returned(self) -> None:
        sentinel = object()
        assert require_qwen(import_fn=lambda name: sentinel) is sentinel

    def test_error_is_engine_actionable(self) -> None:
        assert issubclass(QwenRuntimeError, RuntimeError)


class TestDescribeDevice:
    def test_missing_torch_reports_cpu_only(self) -> None:
        device, detail = describe_qwen_device(
            torch_import=lambda: (_ for _ in ()).throw(ImportError("no torch")),
        )
        assert device == "cpu"
        assert "torch" in detail.lower()

    def test_injected_torch_without_cuda(self) -> None:
        class Cuda:
            @staticmethod
            def is_available() -> bool:
                return False

        class Torch:
            cuda = Cuda()
            __version__ = "2.8.0"

        device, detail = describe_qwen_device(torch_import=lambda: Torch())
        assert device == "cpu"
        assert "cuda" in detail.lower()

    def test_injected_torch_with_cuda(self) -> None:
        class Cuda:
            @staticmethod
            def is_available() -> bool:
                return True

        class Torch:
            cuda = Cuda()
            __version__ = "2.8.0"

        device, _detail = describe_qwen_device(torch_import=lambda: Torch())
        assert device == "cuda"
