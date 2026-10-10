"""Unit tests for macOS platform customization helper (process name, menu title, dock icon)."""

from __future__ import annotations

import sys
from unittest.mock import patch

from vienetts_app.ui.macos import setup_macos_app


class TestMacOSSetup:
    def test_returns_false_on_non_darwin_platform(self) -> None:
        with patch.object(sys, "platform", "linux"):
            assert setup_macos_app() is False

        with patch.object(sys, "platform", "win32"):
            assert setup_macos_app() is False

    def test_macos_setup_handles_library_failure_gracefully(self) -> None:
        with (
            patch("ctypes.util.find_library", return_value=None),
            patch.object(sys, "platform", "darwin"),
        ):
            assert setup_macos_app() is False

    def test_macos_setup_handles_exception_gracefully(self) -> None:
        with (
            patch("ctypes.cdll.LoadLibrary", side_effect=RuntimeError("dlopen failed")),
            patch.object(sys, "platform", "darwin"),
        ):
            assert setup_macos_app() is False
