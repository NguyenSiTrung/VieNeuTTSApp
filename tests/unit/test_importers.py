"""Document import parsers (FR-3.3): txt/md/docx/pdf → plain text."""

from pathlib import Path

import pytest
from docx import Document

from vienetts_app.core.importers import (
    IMPORT_CHAR_LIMIT,
    SUPPORTED_EXTENSIONS,
    DocumentImportError,
    import_document,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

SAMPLE_TXT = "Hello from the text fixture.\nXin chào thế giới.\nThird line for joining checks.\n"
SAMPLE_MD = "# Heading\n\n- item one\n- item two\n\nParagraph with **bold** text returned as-is.\n"
SAMPLE_DOCX = (
    "Word fixture paragraph one.\nĐây là đoạn văn tiếng Việt.\nWord fixture paragraph three."
)
SAMPLE_PDF = "PDF fixture page one.\n\nPDF fixture page two."

SAMPLE_SRT = (
    "1\n"
    "00:00:00,000 --> 00:00:02,000\n"
    "Hello from the subtitle fixture.\n"
    "\n"
    "2\n"
    "00:00:02,500 --> 00:00:04,000\n"
    "Xin chào <i>thế giới</i>.\n"
    "Dòng thứ hai.\n"
    "\n"
    "3\n"
    "00:00:05,000 --> 00:00:06,500\n"
    "Third cue with <b>bold</b> and {\\an8}pos.\n"
)
EXPECTED_CLEAN = (
    "Hello from the subtitle fixture.\n"
    "Xin chào thế giới. Dòng thứ hai.\n"
    "Third cue with bold and pos."
)


class TestSupportedExtensions:
    def test_supported_extensions_contents(self) -> None:
        assert isinstance(SUPPORTED_EXTENSIONS, tuple)
        assert SUPPORTED_EXTENSIONS == (".txt", ".md", ".docx", ".pdf", ".srt")
        assert ".srt" in SUPPORTED_EXTENSIONS


class TestHappyPaths:
    @pytest.mark.parametrize(
        ("filename", "expected"),
        [
            ("sample.txt", SAMPLE_TXT),
            ("sample.md", SAMPLE_MD),
            ("sample.docx", SAMPLE_DOCX),
            ("sample.pdf", SAMPLE_PDF),
        ],
    )
    def test_extracts_exact_content(self, filename: str, expected: str) -> None:
        assert import_document(FIXTURES / filename) == expected

    def test_accepts_str_path(self) -> None:
        assert import_document(str(FIXTURES / "sample.txt")) == SAMPLE_TXT


class TestSrtImport:
    def _write(self, tmp_path: Path, name: str = "sample.srt") -> Path:
        p = tmp_path / name
        p.write_text(SAMPLE_SRT, encoding="utf-8")
        return p

    def test_clean_is_default(self, tmp_path: Path) -> None:
        assert import_document(self._write(tmp_path)) == EXPECTED_CLEAN

    def test_keep_raw_returns_verbatim(self, tmp_path: Path) -> None:
        p = self._write(tmp_path)
        assert import_document(p, keep_srt_raw=True) == SAMPLE_SRT

    def test_malformed_without_timestamps_refuses(self, tmp_path: Path) -> None:
        bad = tmp_path / "bad.srt"
        bad.write_text("just some text\nno timecodes here\n", encoding="utf-8")
        with pytest.raises(DocumentImportError):
            import_document(bad)

    def test_bom_handled(self, tmp_path: Path) -> None:
        p = tmp_path / "bom.srt"
        p.write_bytes(b"\xef\xbb\xbf" + SAMPLE_SRT.encode("utf-8"))
        assert import_document(p) == EXPECTED_CLEAN


class TestCaseInsensitiveExtension:
    def test_case_insensitive_extension(self, tmp_path: Path) -> None:
        # Extension casing is ignored, including .SRT handling of subtitles.
        for filename, content, expected in [
            ("SAMPLE.TXT", SAMPLE_TXT, SAMPLE_TXT),
            ("Readme.Md", SAMPLE_MD, SAMPLE_MD),
            ("SAMPLE.SRT", SAMPLE_SRT, EXPECTED_CLEAN),
        ]:
            p = tmp_path / filename
            p.write_text(content, encoding="utf-8")
            assert import_document(p) == expected


class TestEmptyDocuments:
    def test_empty_txt_returns_empty_string(self, tmp_path: Path) -> None:
        empty = tmp_path / "empty.txt"
        empty.write_text("", encoding="utf-8")
        assert import_document(empty) == ""


class TestErrors:
    def test_unsupported_extension(self, tmp_path: Path) -> None:
        bad = tmp_path / "note.xyz"
        bad.write_text("content", encoding="utf-8")
        with pytest.raises(DocumentImportError) as excinfo:
            import_document(bad)
        message = str(excinfo.value)
        assert ".xyz" in message
        for ext in SUPPORTED_EXTENSIONS:
            assert ext in message

    def test_missing_file_raises_native_filenotfound(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            import_document(tmp_path / "missing.txt")

    def test_directory_as_path(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            import_document(tmp_path / "somedir")

    def test_corrupt_input_chains_cause(self, tmp_path: Path) -> None:
        for filename, data, match in [
            ("corrupt.docx", b"this is not a zip", "corrupt.docx"),
            ("corrupt.pdf", b"not a pdf at all", "corrupt.pdf"),
            ("binary.txt", b"\xff\xfe\xfa not utf-8 \x00\x81", None),
        ]:
            (tmp_path / filename).write_bytes(data)
            with pytest.raises(DocumentImportError, match=match) as excinfo:
                import_document(tmp_path / filename)
            assert excinfo.value.__cause__ is not None


class TestImportCharLimit:
    """FR-4.6b: over-long documents are REFUSED, never truncated."""

    def test_limit_is_200k_chars(self) -> None:
        assert IMPORT_CHAR_LIMIT == 200_000

    def test_oversize_txt_refuses(self, tmp_path: Path) -> None:
        big = tmp_path / "big.txt"
        big.write_text("x" * (IMPORT_CHAR_LIMIT + 1), encoding="utf-8")
        with pytest.raises(DocumentImportError) as excinfo:
            import_document(big)
        message = str(excinfo.value)
        assert "big.txt" in message
        assert "200,000" in message  # the limit itself is named
        assert "200,001" in message  # the actual size is named
        lowered = message.lower()
        assert "too large" in lowered
        assert "split" in lowered  # actionable: what the user should do
        assert "smaller" in lowered
        assert excinfo.value.__cause__ is None  # policy error, not a library failure

    def test_exactly_at_limit_passes(self, tmp_path: Path) -> None:
        # Boundary semantics: exactly IMPORT_CHAR_LIMIT chars is importable.
        text = "a" * IMPORT_CHAR_LIMIT
        edge = tmp_path / "edge.txt"
        edge.write_text(text, encoding="utf-8")
        assert import_document(edge) == text

    def test_oversize_docx_refuses_after_extraction(self, tmp_path: Path) -> None:
        # Binary formats are capped on the EXTRACTED text, not file size.
        document = Document()
        document.add_paragraph("y" * (IMPORT_CHAR_LIMIT + 5))
        big = tmp_path / "big.docx"
        document.save(str(big))
        with pytest.raises(DocumentImportError, match="big.docx") as excinfo:
            import_document(big)
        message = str(excinfo.value)
        assert "200,005" in message  # extracted text length (single paragraph)
        assert "200,000" in message
        assert excinfo.value.__cause__ is None


class TestWindowsCompatibility:
    def test_utf8_with_bom_strips_bom_cleanly(self, tmp_path: Path) -> None:
        file = tmp_path / "bom.txt"
        file.write_bytes(b"\xef\xbb\xbfXin ch\xc3\xa0o Vi\xe1\xbb\x87t Nam")
        assert import_document(file) == "Xin chào Việt Nam"
        assert not import_document(file).startswith("\ufeff")

    def test_import_from_path_forms(self, tmp_path: Path) -> None:
        for spec, content in [
            ("file://{}", "Hello from URL"),
            ('"{}"', "Hello from quoted path"),
        ]:
            file = tmp_path / "hello.txt"
            file.write_text(content, encoding="utf-8")
            assert import_document(spec.format(file.resolve())) == content
