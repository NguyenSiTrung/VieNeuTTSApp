"""Unit tests for cross-platform path normalization and filename sanitization."""

from pathlib import Path

from vienetts_app.core.paths import (
    is_empty_path,
    normalize_local_path,
    path_to_file_url,
    sanitize_filename,
)


class TestNormalizeLocalPath:
    def test_handles_none_and_empty(self) -> None:
        assert is_empty_path(normalize_local_path(None))
        assert is_empty_path(normalize_local_path(""))
        assert is_empty_path(normalize_local_path("   \n\t"))

    def test_handles_quoted_paths(self) -> None:
        assert normalize_local_path('"C:\\Users\\test\\file.wav"') == Path(
            "C:\\Users\\test\\file.wav"
        )
        assert normalize_local_path("'C:/Users/test/file.wav'") == Path("C:/Users/test/file.wav")

    def test_handles_windows_file_url(self) -> None:
        # Standard QUrl string on Windows: file:///C:/Users/...
        assert normalize_local_path("file:///C:/Users/test/audio.wav") == Path(
            "C:/Users/test/audio.wav"
        )

    def test_handles_windows_file_url_with_encoded_spaces(self) -> None:
        # QUrl string with percent encoding
        assert normalize_local_path("file:///C:/Users/User%20Name/my%20file.wav") == Path(
            "C:/Users/User Name/my file.wav"
        )

    def test_handles_unix_file_url(self) -> None:
        assert normalize_local_path("file:///home/user/test.wav") == Path("/home/user/test.wav")

    def test_handles_unc_file_url(self) -> None:
        assert normalize_local_path("file:////server/share/file.wav") == Path(
            r"\\server\share\file.wav"
        )

    def test_handles_windows_extended_prefix(self) -> None:
        assert normalize_local_path(r"\\?\C:\Users\test\file.wav") == Path(
            r"C:\Users\test\file.wav"
        )
        assert normalize_local_path(r"\\?\UNC\server\share\file.wav") == Path(
            r"\\server\share\file.wav"
        )

    def test_handles_stray_leading_slash_before_drive(self) -> None:
        assert normalize_local_path("/C:/Users/test/file.wav") == Path("C:/Users/test/file.wav")
        assert normalize_local_path("/D:\\Users\\test\\file.wav") == Path(
            "D:\\Users\\test\\file.wav"
        )


class TestIsEmptyPath:
    def test_empty_conditions(self) -> None:
        assert is_empty_path(None) is True
        assert is_empty_path("") is True
        assert is_empty_path("   ") is True
        assert is_empty_path(Path("")) is True
        assert is_empty_path(Path(".")) is True

    def test_non_empty_conditions(self) -> None:
        assert is_empty_path("out.wav") is False
        assert is_empty_path(Path("out.wav")) is False
        assert is_empty_path("C:/Users") is False


class TestSanitizeFilename:
    def test_strips_forbidden_characters(self) -> None:
        raw = 'Chapter 1: "Introduction"? <Part |>'
        assert sanitize_filename(raw) == "Chapter 1 Introduction Part"

    def test_removes_trailing_dots_and_whitespace(self) -> None:
        assert sanitize_filename("Chapter 1...   ") == "Chapter 1"

    def test_protects_windows_reserved_device_names(self) -> None:
        # Vietnamese words like "Con" (child), "Aux", "Nul", "Prn"
        assert sanitize_filename("CON") == "_CON"
        assert sanitize_filename("con.wav") == "_con.wav"
        assert sanitize_filename("aux.txt") == "_aux.txt"
        assert sanitize_filename("NUL") == "_NUL"
        assert sanitize_filename("COM1") == "_COM1"

    def test_preserves_non_reserved_vietnamese_titles(self) -> None:
        assert sanitize_filename("Chương 1 - Lời mở đầu") == "Chương 1 - Lời mở đầu"
        # "Con cá vàng" begins with "Con", but stem is "Con cá vàng", not "CON"
        assert sanitize_filename("Con cá vàng") == "Con cá vàng"

    def test_fallback_on_all_forbidden(self) -> None:
        assert sanitize_filename(':::???***"""', fallback="chapter-1") == "chapter-1"

    def test_bounds_max_length(self) -> None:
        long_title = "A" * 100
        assert len(sanitize_filename(long_title, max_len=50)) == 50


class TestPathToFileUrl:
    def test_handles_none_and_empty(self) -> None:
        assert path_to_file_url(None) == ""
        assert path_to_file_url("") == ""
        assert path_to_file_url("   \n\t") == ""

    def test_handles_windows_paths_with_backslashes_and_forward_slashes(self) -> None:
        assert path_to_file_url(r"C:\Users\test\Music") == "file:///C:/Users/test/Music"
        assert path_to_file_url("C:/Users/test/Music") == "file:///C:/Users/test/Music"
        assert path_to_file_url(r"d:\audio\exports") == "file:///d:/audio/exports"

    def test_handles_spaces_and_unicode_characters(self) -> None:
        # Should be valid for QUrl consumption
        url = path_to_file_url(r"C:\Users\Trung\Nhạc Việt\Tệp 01.wav")
        assert url.startswith("file:///C:/Users/Trung/")
        assert "Tệp 01.wav" in url or "T%E1%BB%87p%2001.wav" in url

    def test_handles_unc_network_shares(self) -> None:
        assert path_to_file_url(r"\\server\share\music") == "file://server/share/music"
        assert path_to_file_url("//server/share/music") == "file://server/share/music"

    def test_handles_unix_absolute_paths(self) -> None:
        assert path_to_file_url("/home/user/Music") == "file:///home/user/Music"

    def test_preserves_valid_file_url(self) -> None:
        assert path_to_file_url("file:///C:/Users/test/Music") == "file:///C:/Users/test/Music"
        assert path_to_file_url("file:///home/user/Music") == "file:///home/user/Music"
