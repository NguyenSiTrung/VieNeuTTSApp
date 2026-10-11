"""Narrow-path bytes handed to qwen.dll (Windows ANSI-fopen contract).

The Windows build of qwentts.cpp opens model paths through the ANSI codepage,
so bytes it receives must be pure ASCII — the 8.3 spelling when the path has
one, the original spelling when it is already ASCII, and an actionable error
instead of an opaque load failure when neither is representable.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from vienetts_app.core import paths as paths_module
from vienetts_app.workers.qwen_gguf_abi import NativeAbiError, _native_path_bytes


def test_posix_paths_go_through_as_utf8(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "name", "posix")
    assert _native_path_bytes("/mô hình/model.gguf", "talker") == "/mô hình/model.gguf".encode()


@pytest.mark.skipif(os.name != "nt", reason="the ANSI contract only binds on Windows")
class TestWindowsPathBytes:
    def test_ascii_path_passes_unchanged(self) -> None:
        assert _native_path_bytes("C:\\models\\model.gguf", "talker") == b"C:\\models\\model.gguf"

    def test_an_8_3_spelling_is_used_when_ascii(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            paths_module,
            "to_short_path",
            lambda p: Path("C:\\Users\\NGUYEN~1\\model.gguf"),
        )
        assert _native_path_bytes("C:\\Users\\Nguyễn\\model.gguf", "talker") == (
            b"C:\\Users\\NGUYEN~1\\model.gguf"
        )

    def test_no_ascii_spelling_is_an_actionable_error(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(paths_module, "to_short_path", lambda p: Path(p))
        with pytest.raises(NativeAbiError, match="ASCII-only"):
            _native_path_bytes("C:\\Users\\Nguyễn\\model.gguf", "codec")

    def test_a_partial_spelling_is_not_silently_sent(self, monkeypatch: pytest.MonkeyPatch) -> None:
        # Per-component conversion can leave the unicode component in place.
        monkeypatch.setattr(
            paths_module,
            "to_short_path",
            lambda p: Path("C:\\Users\\NGUYEN~1\\Nhạc Việt\\model.gguf"),
        )
        with pytest.raises(NativeAbiError, match="ASCII-only"):
            _native_path_bytes("C:\\Users\\Nguyễn\\Nhạc Việt\\model.gguf", "codec")
