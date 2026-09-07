"""Unit tests for cross-platform path normalization and filename sanitization."""

from pathlib import Path

from vienetts_app.core.paths import (
    is_empty_path,
    normalize_local_path,
    path_to_file_url,
    sanitize_filename,
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
        assert normalize_local_path("file:////server/share/file.wav") == Path(
            r"\\server\share\file.wav"
        )
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
        assert path_to_file_url(r"\\server\share\music") == "file://server/share/music"
        assert path_to_file_url("//server/share/music") == "file://server/share/music"
        assert path_to_file_url("/home/user/Music") == "file:///home/user/Music"
        assert path_to_file_url("file:///C:/Users/test/Music") == "file:///C:/Users/test/Music"
        assert path_to_file_url("file:///home/user/Music") == "file:///home/user/Music"
