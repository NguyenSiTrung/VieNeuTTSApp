"""Unit tests for Windows platform helpers (AppUserModelID, dark titlebar)."""

from __future__ import annotations

import ctypes
import sys
from unittest.mock import patch

from vienetts_app.ui import windows
from vienetts_app.ui.windows import (
    DWMWA_USE_IMMERSIVE_DARK_MODE,
    apply_dark_titlebars,
    apply_immersive_dark_mode,
    setup_windows_app,
)


class _FakeShell32:
    def __init__(self) -> None:
        self.seen: dict[str, str] = {}

    def SetCurrentProcessExplicitAppUserModelID(self, app_id: object) -> None:
        self.seen["app_id"] = getattr(app_id, "value", str(app_id))


class _FakeDwmApi:
    def __init__(self, result: int = 0) -> None:
        self.result = result
        self.fail_attributes: set[int] = set()
        self.calls: list[dict[str, int]] = []

    @staticmethod
    def _num(arg: object) -> int:
        # Production wraps HWND/attribute in ctypes instances (pointer-sized
        # on Win64); unwrap rather than int(), which mis-parses them.
        simple = ctypes._SimpleCData  # type: ignore[attr-defined]
        return int(arg.value) if isinstance(arg, simple) else int(arg)

    def DwmSetWindowAttribute(
        self, hwnd: object, attribute: object, value: object, size: object
    ) -> int:
        attribute_num = self._num(attribute)
        self.calls.append(
            {
                "hwnd": self._num(hwnd),
                "attribute": attribute_num,
                "value": ctypes.cast(value, ctypes.POINTER(ctypes.c_int)).contents.value,
                "size": self._num(size),
            }
        )
        return 1 if attribute_num in self.fail_attributes else self.result


class _FakeWindll:
    def __init__(self, shell32: object, dwmapi: object) -> None:
        self.shell32 = shell32
        self.dwmapi = dwmapi


class _FakeWindow:
    def winId(self) -> int:
        return 0x1234


class TestWindowsSetup:
    def test_returns_false_off_windows(self) -> None:
        with patch.object(sys, "platform", "darwin"):
            assert setup_windows_app() is False
            assert apply_immersive_dark_mode(_FakeWindow(), True) is False
        with patch.object(sys, "platform", "linux"):
            assert setup_windows_app() is False
            assert apply_dark_titlebars(True) == 0

    def test_dark_mode_rejects_none_window(self) -> None:
        with patch.object(sys, "platform", "win32"):
            # No ctypes.windll on POSIX — must fail closed, never raise.
            assert apply_immersive_dark_mode(None, True) is False

    def test_app_model_id_applied(self) -> None:
        shell32 = _FakeShell32()
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(
                windows.ctypes, "windll", _FakeWindll(shell32, _FakeDwmApi()), create=True
            ),
        ):
            assert setup_windows_app("VieNeuTTS.VieNeuTTS.VieNeuTTS") is True
        assert shell32.seen == {"app_id": "VieNeuTTS.VieNeuTTS.VieNeuTTS"}

    def test_app_model_id_failure_is_swallowed(self) -> None:
        class _ExplodingShell32:
            def SetCurrentProcessExplicitAppUserModelID(self, app_id: object) -> None:
                raise OSError("shell32 unavailable")

        with (
            patch.object(sys, "platform", "win32"),
            patch.object(
                windows.ctypes,
                "windll",
                _FakeWindll(_ExplodingShell32(), _FakeDwmApi()),
                create=True,
            ),
        ):
            assert setup_windows_app() is False

    def test_immersive_dark_mode_sets_attribute(self) -> None:
        dwm = _FakeDwmApi(result=0)
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(windows.ctypes, "windll", _FakeWindll(_FakeShell32(), dwm), create=True),
        ):
            assert apply_immersive_dark_mode(_FakeWindow(), True) is True
        assert dwm.calls == [
            {
                "hwnd": 0x1234,
                "attribute": DWMWA_USE_IMMERSIVE_DARK_MODE,
                "value": 1,
                "size": 4,
            }
        ]

    def test_immersive_dark_mode_falls_back_to_legacy_attribute(self) -> None:
        dwm = _FakeDwmApi()
        dwm.fail_attributes = {20}  # pre-20H1: attribute 20 → E_INVALIDARG
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(windows.ctypes, "windll", _FakeWindll(_FakeShell32(), dwm), create=True),
        ):
            assert apply_immersive_dark_mode(_FakeWindow(), False) is True
        assert [c["attribute"] for c in dwm.calls] == [20, 19]
        assert dwm.calls[-1]["value"] == 0

    def test_immersive_dark_mode_reports_failure(self) -> None:
        dwm = _FakeDwmApi(result=1)  # not S_OK on either attribute
        with (
            patch.object(sys, "platform", "win32"),
            patch.object(windows.ctypes, "windll", _FakeWindll(_FakeShell32(), dwm), create=True),
        ):
            assert apply_immersive_dark_mode(_FakeWindow(), True) is False
        assert len(dwm.calls) == 2
