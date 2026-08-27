"""Document import parsers (FR-3.3): ``.txt``/``.md``/``.docx``/``.pdf`` → plain text.

``SUPPORTED_EXTENSIONS`` is the single source of truth for QML file dialogs
(name filters) later; keep it in sync with the reader dispatch below.
"""

from __future__ import annotations

from pathlib import Path

from docx import Document
from pypdf import PdfReader

SUPPORTED_EXTENSIONS: tuple[str, ...] = (".txt", ".md", ".docx", ".pdf")


class DocumentImportError(RuntimeError):
    """A document exists but cannot be imported (unsupported/corrupt/unreadable).

    Deliberately NOT named ``ImportError`` — that would shadow the builtin.
    Messages are user-facing and actionable; the original library exception
    is always chained as ``__cause__`` for logs.
    """


def import_document(path: str | Path) -> str:
    """Extract plain text from a supported document; returns ``""`` if empty.

    Raises:
        FileNotFoundError: the path does not exist or is a directory.
        DocumentImportError: unsupported extension, or a corrupt/unreadable
            file (original exception chained).
    """
    path = Path(path)
    if not path.is_file():
        # Covers missing files, directories, and broken symlinks alike.
        raise FileNotFoundError(f"no such document file: {path}")
    ext = path.suffix.lower()
    if ext not in SUPPORTED_EXTENSIONS:
        raise DocumentImportError(
            f"Unsupported document type '{ext or path.name}'. "
            f"Supported types: {', '.join(SUPPORTED_EXTENSIONS)}. "
            "Please choose another file or convert it first."
        )
    if ext in (".txt", ".md"):
        return _read_plain_text(path)
    if ext == ".docx":
        return _read_docx(path)
    return _read_pdf(path)


def _read_plain_text(path: Path) -> str:
    """UTF-8 text; markdown is returned verbatim (no stripping)."""
    try:
        return path.read_text(encoding="utf-8")
    except UnicodeDecodeError as err:
        raise DocumentImportError(
            f"Could not decode '{path.name}' as UTF-8 text. "
            "Re-save the file with UTF-8 encoding and try again."
        ) from err


def _read_docx(path: Path) -> str:
    """python-docx; paragraph texts joined with newlines (predictable shape)."""
    try:
        document = Document(str(path))
    except Exception as err:
        raise DocumentImportError(
            f"Could not open '{path.name}': it is not a valid Word (.docx) file. "
            "Re-save it from Word/Google Docs as .docx and try again."
        ) from err
    return "\n".join(paragraph.text for paragraph in document.paragraphs)


def _read_pdf(path: Path) -> str:
    """pypdf; per-page text joined with a blank line so pages stay separable."""
    try:
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as err:
        raise DocumentImportError(
            f"Could not read '{path.name}': it is not a valid PDF or uses an unsupported "
            "text encoding (scanned/image-only PDFs have no extractable text)."
        ) from err
    return "\n\n".join(pages)
