"""Unit tests for cross-platform path normalization and filename sanitization."""

import ctypes
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from vienetts_app.core import paths as paths_module
from vienetts_app.core.paths import (
    is_empty_path,
    normalize_local_path,
    path_to_file_url,
    sanitize_filename,
    to_extended,
    to_short_path,
)


class TestNormalizeLocalPath:
    def test_normalize_local_path(self) -> None:
        # None / empty / whitespace resolve to the empty path.
        assert is_empty_path(normalize_local_path(None))
        assert is_empty_path(normalize_local_path(""))
        assert is_empty_path(normalize_local_path("   \n\t"))
        # Surrounding quotes (e.g. from Windows Explorer).
        assert normalize_local_path('"C:\\Users\\test\\file.wav"') == Path(
            "C:\\Users\\test\\file.wav"
        )
        assert normalize_local_path("'C:/Users/test/file.wav'") == Path("C:/Users/test/file.wav")
        # Standard QUrl string on Windows: file:///C:/Users/...
        assert normalize_local_path("file:///C:/Users/test/audio.wav") == Path(
            "C:/Users/test/audio.wav"
        )
        # QUrl string with percent encoding
        assert normalize_local_path("file:///C:/Users/User%20Name/my%20file.wav") == Path(
            "C:/Users/User Name/my file.wav"
        )
        assert normalize_local_path("file:///home/user/test.wav") == Path("/home/user/test.wav")
        # Authority-bearing file URLs: a remote host is a UNC share on
        # Windows and a //host/share network path on POSIX — never a
        # Windows \\host\share literal on macOS/Linux.
        for url in ("file:////server/share/file.wav", "file://server/share/file.wav"):
            expected = (
                Path(r"\\server\share\file.wav")
                if os.name == "nt"
                else Path("//server/share/file.wav")
            )
            assert normalize_local_path(url) == expected
        # The localhost authority names this machine: the URL is a plain
        # absolute local path (drag sources on macOS emit this form).
        assert normalize_local_path("file://localhost/Users/x/a.wav") == Path("/Users/x/a.wav")
        # A drive URL is a path, not an authority.
        assert normalize_local_path("file://C:/Users/test/a.wav") == Path("C:/Users/test/a.wav")
        # Windows extended-length prefixes.
        assert normalize_local_path(r"\\?\C:\Users\test\file.wav") == Path(
            r"C:\Users\test\file.wav"
        )
        assert normalize_local_path(r"\\?\UNC\server\share\file.wav") == Path(
            r"\\server\share\file.wav"
        )
        # Stray leading slash before a Windows drive letter.
        assert normalize_local_path("/C:/Users/test/file.wav") == Path("C:/Users/test/file.wav")
        assert normalize_local_path("/D:\\Users\\test\\file.wav") == Path(
            "D:\\Users\\test\\file.wav"
        )


class TestIsEmptyPath:
    def test_is_empty_path_truth_table(self) -> None:
        assert is_empty_path(None) is True
        assert is_empty_path("") is True
        assert is_empty_path("   ") is True
        assert is_empty_path(Path("")) is True
        assert is_empty_path(Path(".")) is True
        assert is_empty_path("out.wav") is False
        assert is_empty_path(Path("out.wav")) is False
        assert is_empty_path("C:/Users") is False


class TestSanitizeFilename:
    def test_sanitize_filename_rules(self) -> None:
        raw = 'Chapter 1: "Introduction"? <Part |>'
        assert sanitize_filename(raw) == "Chapter 1 Introduction Part"
        assert sanitize_filename("Chapter 1...   ") == "Chapter 1"
        # Windows reserved device names; Vietnamese words like "Con" (child),
        # "Aux", "Nul", "Prn" collide with them.
        assert sanitize_filename("CON") == "_CON"
        assert sanitize_filename("con.wav") == "_con.wav"
        assert sanitize_filename("aux.txt") == "_aux.txt"
        assert sanitize_filename("NUL") == "_NUL"
        assert sanitize_filename("COM1") == "_COM1"
        # Non-reserved Vietnamese titles are preserved.
        assert sanitize_filename("Chương 1 - Lời mở đầu") == "Chương 1 - Lời mở đầu"
        # "Con cá vàng" begins with "Con", but stem is "Con cá vàng", not "CON"
        assert sanitize_filename("Con cá vàng") == "Con cá vàng"
        assert sanitize_filename(':::???***"""', fallback="chapter-1") == "chapter-1"
        long_title = "A" * 100
        assert len(sanitize_filename(long_title, max_len=50)) == 50
        # Trailing spaces/dots before the extension dot must not bypass
        # Windows reserved-device detection (e.g. "CON .txt" -> "_CON.txt").
        assert sanitize_filename("CON .txt") == "_CON.txt"
        assert sanitize_filename("aux   .wav") == "_aux.wav"
        assert sanitize_filename("NUL . .tar.gz").startswith("_")
        assert sanitize_filename("report   .txt") == "report.txt"
        # UTF-8 byte length cap: multi-byte Vietnamese/emoji names must stay
        # under OS NAME_MAX without splitting codepoints, preserving extension.
        viet_with_ext = "ệ" * 100 + ".wav"
        capped_ext = sanitize_filename(viet_with_ext, max_len=255)
        assert capped_ext.endswith(".wav")
        assert len(capped_ext.encode("utf-8")) <= 255
        emoji_name = "🎵" * 80
        capped_emoji = sanitize_filename(emoji_name)
        assert len(capped_emoji.encode("utf-8")) <= 200
        reserved_long = "CON." + "a" * 255
        capped_reserved = sanitize_filename(reserved_long, max_len=255)
        assert capped_reserved.startswith("_CON")
        assert len(capped_reserved.encode("utf-8")) <= 255


class TestToExtended:
    def test_to_extended_normalizes_segments_and_relative_paths(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(paths_module.os, "name", "nt")
        assert to_extended(r"C:/Users/test/./sub/../file.wav") == Path(
            r"\\?\C:\Users\test\file.wav"
        )
        assert to_extended(r"\\server\share\dir\..\file.wav") == Path(
            r"\\?\UNC\server\share\file.wav"
        )
        seen_rel: list[str] = []

        def fake_abspath(p: str) -> str:
            seen_rel.append(p)
            return r"C:\Work\dir\..\file.wav"

        monkeypatch.setattr(paths_module.os.path, "abspath", fake_abspath)
        assert to_extended("relative/./file.wav") == Path(r"\\?\C:\Work\file.wav")
        assert seen_rel == [r"relative\file.wav"]


class TestToShortPath:
    def test_to_short_path(self, tmp_path: Path) -> None:
        import os

        # Empty input stays empty on every platform.
        assert is_empty_path(to_short_path(None))
        assert is_empty_path(to_short_path(""))
        if os.name != "nt":
            # POSIX: always a passthrough.
            assert to_short_path(tmp_path / "đĩa mềm") == tmp_path / "đĩa mềm"
            return
        # Windows: 8.3 conversion is per-component — components that have an
        # alias come back spelled, ones that do not keep their long name, so
        # the result can be unchanged, fully ASCII, or partially spelled.
        # Whatever it returns must name the same file.
        non_ascii = tmp_path / "Nhạc Việt" / "Tệp 01.wav"
        non_ascii.parent.mkdir(parents=True)
        non_ascii.write_bytes(b"x")
        spelled = to_short_path(non_ascii)
        assert spelled.exists()
        assert os.path.samefile(spelled, non_ascii)

    def test_to_short_path_uses_to_extended_and_local_typed_windll(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setattr(paths_module.os, "name", "nt")
        calls: list[tuple[str, int]] = []
        dll_args: list[tuple[str, bool]] = []

        class FakeGetShort:
            argtypes = None
            restype = None

            def __call__(self, source: str, buf: object, size: int) -> int:
                calls.append((source, size))
                short = r"\\?\C:\Users\NGUYEN~1\file.wav"
                buf.value = short  # type: ignore[attr-defined]
                return len(short)

        fake_fn = FakeGetShort()

        def fake_windll(name: str, *, use_last_error: bool = False) -> SimpleNamespace:
            dll_args.append((name, use_last_error))
            return SimpleNamespace(GetShortPathNameW=fake_fn)

        monkeypatch.setattr(ctypes, "WinDLL", fake_windll, raising=False)
        monkeypatch.delattr(ctypes, "windll", raising=False)

        result = to_short_path(r"C:/Users/Nguyễn/./sub/../file.wav")
        assert dll_args == [("kernel32", True)]
        assert fake_fn.argtypes is not None and len(fake_fn.argtypes) == 3
        assert fake_fn.restype is not None
        assert calls and calls[0][0] == r"\\?\C:\Users\Nguyễn\file.wav"
        assert result == Path(r"C:\Users\NGUYEN~1\file.wav")


class TestPathToFileUrl:
    def test_path_to_file_url(self) -> None:
        assert path_to_file_url(None) == ""
        assert path_to_file_url("") == ""
        assert path_to_file_url("   \n\t") == ""
        assert path_to_file_url(r"C:\Users\test\Music") == "file:///C:/Users/test/Music"
        assert path_to_file_url("C:/Users/test/Music") == "file:///C:/Users/test/Music"
        assert path_to_file_url(r"d:\audio\exports") == "file:///d:/audio/exports"
        # Spaces and Unicode should be valid for QUrl consumption.
        url = path_to_file_url(r"C:\Users\Trung\Nhạc Việt\Tệp 01.wav")
        assert url.startswith("file:///C:/Users/Trung/")
        assert "Tệp 01.wav" in url or "T%E1%BB%87p%2001.wav" in url
        # Characters that are structural in a URL are percent-encoded so
        # QML dialogs never see a truncated or re-parsed path.
        url = path_to_file_url("/Users/x/Nhạc Việt/Tệp #1?.wav")
        assert url == "file:///Users/x/Nh%E1%BA%A1c%20Vi%E1%BB%87t/T%E1%BB%87p%20%231%3F.wav"
        assert "#" not in url and "?" not in url
        # A literal '%' in the name encodes as %25 (no half-encoded output).
        assert path_to_file_url("/tmp/100%/a.wav") == "file:///tmp/100%25/a.wav"
        assert path_to_file_url(r"\\server\share\music") == "file://server/share/music"
        assert path_to_file_url("//server/share/music") == "file://server/share/music"
        assert path_to_file_url("/home/user/Music") == "file:///home/user/Music"
        assert path_to_file_url("file:///C:/Users/test/Music") == "file:///C:/Users/test/Music"
        assert path_to_file_url("file:///home/user/Music") == "file:///home/user/Music"
        # Two-slash Windows drive URLs keep the drive colon and existing percent escapes.
        assert (
            path_to_file_url("file://C:/Users/User%20Name/a.wav")
            == "file:///C:/Users/User%20Name/a.wav"
        )
        assert (
            path_to_file_url(r"file://C:\Users\test\my file.wav")
            == "file:///C:/Users/test/my%20file.wav"
        )
