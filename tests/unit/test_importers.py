"""Document import parsers (FR-3.3): txt/md/docx/pdf → plain text."""

from pathlib import Path

import pytest

from vienetts_app.core.importers import SUPPORTED_EXTENSIONS, DocumentImportError, import_document

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

SAMPLE_TXT = "Hello from the text fixture.\nXin chào thế giới.\nThird line for joining checks.\n"
SAMPLE_MD = "# Heading\n\n- item one\n- item two\n\nParagraph with **bold** text returned as-is.\n"
SAMPLE_DOCX = (
    "Word fixture paragraph one.\nĐây là đoạn văn tiếng Việt.\nWord fixture paragraph three."
)
SAMPLE_PDF = "PDF fixture page one.\n\nPDF fixture page two."


class TestSupportedExtensions:
    def test_exact_contents(self) -> None:
        assert SUPPORTED_EXTENSIONS == (".txt", ".md", ".docx", ".pdf")

    def test_is_a_tuple_of_str(self) -> None:
        assert isinstance(SUPPORTED_EXTENSIONS, tuple)
        assert all(isinstance(ext, str) for ext in SUPPORTED_EXTENSIONS)


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

    def test_markdown_returned_as_is(self) -> None:
        # No markdown stripping: markers survive verbatim.
        assert "**bold**" in import_document(FIXTURES / "sample.md")

    def test_pdf_pages_joined_with_double_newline(self) -> None:
        result = import_document(FIXTURES / "sample.pdf")
        assert result.count("\n\n") == 1
        assert result.startswith("PDF fixture page one.")
        assert result.endswith("PDF fixture page two.")


class TestCaseInsensitiveExtension:
    def test_uppercase_txt(self, tmp_path: Path) -> None:
        upper = tmp_path / "SAMPLE.TXT"
        upper.write_text(SAMPLE_TXT, encoding="utf-8")
        assert import_document(upper) == SAMPLE_TXT

    def test_mixed_case_md(self, tmp_path: Path) -> None:
        mixed = tmp_path / "Readme.Md"
        mixed.write_text(SAMPLE_MD, encoding="utf-8")
        assert import_document(mixed) == SAMPLE_MD


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

    def test_corrupt_docx_chains_cause(self, tmp_path: Path) -> None:
        corrupt = tmp_path / "corrupt.docx"
        corrupt.write_text("this is not a zip", encoding="utf-8")
        with pytest.raises(DocumentImportError, match="corrupt.docx") as excinfo:
            import_document(corrupt)
        assert excinfo.value.__cause__ is not None

    def test_corrupt_pdf_chains_cause(self, tmp_path: Path) -> None:
        corrupt = tmp_path / "corrupt.pdf"
        corrupt.write_text("not a pdf at all", encoding="utf-8")
        with pytest.raises(DocumentImportError, match="corrupt.pdf") as excinfo:
            import_document(corrupt)
        assert excinfo.value.__cause__ is not None

    def test_non_utf8_text_chains_cause(self, tmp_path: Path) -> None:
        binary = tmp_path / "binary.txt"
        binary.write_bytes(b"\xff\xfe\xfa not utf-8 \x00\x81")
        with pytest.raises(DocumentImportError) as excinfo:
            import_document(binary)
        assert excinfo.value.__cause__ is not None
