"""Document import parsers (FR-3.3): ``.txt``/``.md``/``.docx``/``.pdf``/``.srt`` → plain text.

``SUPPORTED_EXTENSIONS`` is the single source of truth for QML file dialogs
(name filters) later; keep it in sync with the reader dispatch below.

Extracted text over ``IMPORT_CHAR_LIMIT`` characters is refused with an
actionable ``DocumentImportError`` (FR-4.6b) — never truncated, because
silently dropped text would yield audio for only part of the document.
"""

from __future__ import annotations

import re
from pathlib import Path

from vienetts_app.core.paths import normalize_local_path

# docx/pypdf import lazily inside their readers: both are heavyweight
# (~170 ms combined on the app's import path) and only a .docx/.pdf import
# ever needs them — .txt/.md imports and app startup stay un-penalized.
_SRT_TAG_RE = re.compile(r"<[^>]*>")
_SRT_OVERRIDE_RE = re.compile(r"\{[^}]*\}")
_SRT_WS_RE = re.compile(r"\s+")

SUPPORTED_EXTENSIONS: tuple[str, ...] = (".txt", ".md", ".docx", ".pdf", ".srt")

IMPORT_CHAR_LIMIT = 200_000


class DocumentImportError(RuntimeError):
    """A document exists but cannot be imported (unsupported/corrupt/unreadable).

    Deliberately NOT named ``ImportError`` — that would shadow the builtin.
    Messages are user-facing and actionable; the original library exception
    is always chained as ``__cause__`` for logs.
    """


def import_document(path: str | Path, *, keep_srt_raw: bool = False) -> str:
    """Extract plain text from a supported document; returns ``""`` if empty.

    Raises:
        FileNotFoundError: the path does not exist or is a directory.
        DocumentImportError: unsupported extension, a corrupt/unreadable file
            (original exception chained), or extracted text over
            ``IMPORT_CHAR_LIMIT`` characters (policy error, no chained cause).

    Oversized documents are REFUSED, never truncated: silently dropping the
    tail would produce audio for only part of the text, which is worse than
    an actionable error (FR-4.6b).
    """
    path = normalize_local_path(path)
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
        text = _read_plain_text(path)
    elif ext == ".docx":
        text = _read_docx(path)
    elif ext == ".pdf":
        text = _read_pdf(path)
    else:
        text = _read_srt(path, keep_raw=keep_srt_raw)
    if len(text) > IMPORT_CHAR_LIMIT:
        raise DocumentImportError(
            f"Document '{path.name}' is too large to import: {len(text):,} characters "
            f"(limit is {IMPORT_CHAR_LIMIT:,}). Split the document into smaller parts, "
            "or import a smaller file."
        )
    return text


def _read_plain_text(path: Path) -> str:
    """UTF-8 text (BOM-safe); markdown is returned verbatim (no stripping)."""
    try:
        return path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as err:
        raise DocumentImportError(
            f"Could not decode '{path.name}' as UTF-8 text. "
            "Re-save the file with UTF-8 encoding and try again."
        ) from err


def _read_docx(path: Path) -> str:
    """python-docx; paragraph texts joined with newlines (predictable shape)."""
    from docx import Document  # lazy: see module import-block note

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
    from pypdf import PdfReader  # lazy: see module import-block note

    try:
        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
    except Exception as err:
        raise DocumentImportError(
            f"Could not read '{path.name}': it is not a valid PDF or uses an unsupported "
            "text encoding (scanned/image-only PDFs have no extractable text)."
        ) from err
    return "\n\n".join(pages)


def _read_srt(path: Path, *, keep_raw: bool = False) -> str:
    """SubRip subtitles → spoken text (default) or verbatim source (``keep_raw``).

    Clean mode drops sequence numbers, ``-->`` timestamp lines and ``<...>`` /
    ``{...}`` styling, joins multi-line cues with a space and cues with ``"\\n"``.
    Files without any ``-->`` line are refused — importing them as prose would
    read sequence numbers aloud or silently accept a misnamed file.
    """
    try:
        raw = path.read_bytes().decode("utf-8-sig")
    except UnicodeDecodeError as err:
        raise DocumentImportError(
            f"Could not decode '{path.name}' as UTF-8 text. "
            "Re-save the file with UTF-8 encoding and try again."
        ) from err
    text = raw.replace("\r\n", "\n").replace("\r", "\n")
    if keep_raw:
        return text
    if text.strip() == "":
        return ""
    if "-->" not in text:
        raise DocumentImportError(
            f"Could not read '{path.name}': it has no subtitle timecodes "
            "('-->'). Re-save it as SubRip (.srt) and try again."
        )
    cues: list[str] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = [ln.strip() for ln in block.split("\n") if ln.strip() != ""]
        stamp = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if stamp is None:
            continue
        body = " ".join(lines[stamp + 1 :])
        body = _SRT_TAG_RE.sub("", body)
        body = _SRT_OVERRIDE_RE.sub("", body)
        body = _SRT_WS_RE.sub(" ", body).strip()
        if body:
            cues.append(body)
    return "\n".join(cues)
