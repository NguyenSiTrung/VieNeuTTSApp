"""EPUB reader (audiobook FR-A1): ``.epub`` → :class:`EpubBook` chapters.

Pure stdlib — ``zipfile`` + ``xml.etree.ElementTree`` with an
``html.parser`` fallback for spine documents that are not well-formed XML
(common in the wild). No new dependencies (project precedent: pypdf over
PyMuPDF at Phase 0; ``ebooklib`` would pull in ``lxml``).

An EPUB is a ZIP: ``META-INF/container.xml`` names the OPF package file;
the OPF carries ``metadata`` (dc:title/dc:creator), ``manifest`` (id →
href/media-type/properties) and ``spine`` (reading order). Chapters are the
spine's XHTML documents in order, EXCLUDING:

- EPUB3 navigation documents (manifest ``properties="nav"``),
- documents whose extracted text is empty/whitespace (cover galleries),
- manifest items that are not XHTML/HTML documents (spines occasionally
  list stylesheets).

Chapter titles: the first ``h1``–``h6`` in the document (whitespace-joined),
else the localized fallback ``"Chương N"`` (1-based chapter number — the
app's language is Vietnamese). The heading is consumed as the title and NOT
repeated in the spoken text.

Text shape: block texts (``p``, headings, list items, table cells, …) are
joined with ``\\n\\n`` so paragraph boundaries survive into segmentation;
runs of whitespace inside a block collapse to single spaces; ``<br/>``
becomes a newline. Vietnamese diacritics pass through verbatim (pure str
slicing, no Unicode normalization — project pattern).

DRM: the presence of ``META-INF/encryption.xml`` means the content is
encrypted (Adobe/Kindle DRM schemes all register there); such books fail
fast with the exact ``DRM_MESSAGE`` instead of yielding ciphertext noise.

Errors mirror :mod:`vienetts_app.core.importers`: actionable
``DocumentImportError`` messages with the original exception chained, and
``FileNotFoundError`` for missing paths.
"""

from __future__ import annotations

import hashlib
import re
import zipfile
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from xml.etree import ElementTree

from vienetts_app.core.importers import DocumentImportError

CONTAINER_PATH = "META-INF/container.xml"
ENCRYPTION_PATH = "META-INF/encryption.xml"

_OCF_NS = "{urn:oasis:names:tc:opendocument:xmlns:container}"
_OPF_NS = "{http://www.idpf.org/2007/opf}"
_DC_NS = "{http://purl.org/dc/elements/1.1/}"
_XHTML_NS = "{http://www.w3.org/1999/xhtml}"

_EPUB_SUFFIX = ".epub"

# Document media types that may contain readable text (EPUB2 uses
# application/xhtml+xml; EPUB3 allows text/html for legacy content).
_DOCUMENT_MEDIA_TYPES = frozenset({"application/xhtml+xml", "text/html"})

# Localized fallback chapter title: "Chương 1", "Chương 2", …
FALLBACK_CHAPTER_TITLE = "Chương {number}"

DRM_MESSAGE = (
    "This EPUB is DRM-protected (encrypted), so its text cannot be read. "
    "Use a DRM-free copy of the book."
)

# HTML elements that force a line break (newline) without ending the block.
_BREAK_TAGS = frozenset({"br"})

# HTML block-level elements: each contributes one text block joined to its
# neighbours with a blank line. Everything else (span/em/strong/a/…) flows
# inline inside its block.
_BLOCK_TAGS = frozenset(
    {
        "p",
        "div",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "blockquote",
        "pre",
        "td",
        "th",
        "figcaption",
        "section",
        "article",
        "header",
        "footer",
        "aside",
        "dt",
        "dd",
    }
)

# Headings, in the order h1..h6 — the first present becomes the title.
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")

# Content whose text is never spoken.
_SKIPPED_TAGS = frozenset({"script", "style", "head", "title", "svg"})

_WHITESPACE_RE = re.compile(r"\s+")


@dataclass(frozen=True)
class EpubChapter:
    """One readable chapter: contiguous ``index``, non-blank title and text."""

    index: int
    title: str
    text: str

    def __post_init__(self) -> None:
        if not isinstance(self.index, int) or isinstance(self.index, bool):
            raise TypeError("index must be an int")
        if self.index < 0:
            raise ValueError(f"index must be >= 0, got {self.index}")
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("title must be a non-blank string")
        if not isinstance(self.text, str) or not self.text.strip():
            raise ValueError("text must be a non-blank string")
        self.__dict__["title"] = self.title.strip()
        self.__dict__["text"] = self.text.strip()


@dataclass(frozen=True)
class EpubBook:
    """Parsed EPUB: metadata + ordered chapters + content identity."""

    title: str
    author: str
    chapters: list[EpubChapter]
    source_path: str
    content_hash: str  # sha256 hex of the source file bytes

    def __post_init__(self) -> None:
        if not isinstance(self.title, str) or not self.title.strip():
            raise ValueError("title must be a non-blank string")
        if not isinstance(self.author, str):
            raise ValueError("author must be a string (may be empty)")
        if not self.chapters:
            raise ValueError("an EpubBook requires at least one chapter")
        if not isinstance(self.source_path, str) or not self.source_path.strip():
            raise ValueError("source_path must be a non-empty string")
        if not re.fullmatch(r"[0-9a-f]{64}", self.content_hash):
            raise ValueError("content_hash must be a 64-char lowercase sha256 hex")
        expected = list(range(len(self.chapters)))
        if [c.index for c in self.chapters] != expected:
            raise ValueError(
                f"chapter indexes must be contiguous 0..n-1, got {[c.index for c in self.chapters]}"
            )


def _hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def _local_name(tag: str) -> str:
    """Strip an XML namespace: ``{ns}tag`` → ``tag``."""
    return tag.rsplit("}", 1)[-1]


def _collapse_block_text(text: str) -> str:
    """Normalize one block's inline text: collapse space/tab runs to single
    spaces, keep ONE ``\\n`` per line break (``<br/>``/nested blocks), strip
    the edges. Source-text whitespace is already collapsed to spaces by the
    extractors, so newlines here are always explicit breaks.
    """
    text = re.sub(r"[^\S\n]+", " ", text)  # space/tab runs → single space
    text = re.sub(r" ?\n ?", "\n", text)  # spaces hugging a newline
    return re.sub(r"\n{2,}", "\n", text).strip()  # newline runs → one


def _norm_source_text(text: str) -> str:
    """Source text-node whitespace (indentation, wrapped lines) → spaces."""
    return _WHITESPACE_RE.sub(" ", text)


class _TextExtractor(HTMLParser):
    """Block-aware text extraction for the lenient HTML fallback path.

    Collects block texts in document order and remembers the FIRST heading
    (h1–h6) anywhere in the document as the chapter title — the heading is
    consumed as the title and NOT repeated in the spoken text (same rule as
    the ElementTree path).
    """

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.blocks: list[str] = []
        self.heading: str | None = None
        self._current: list[str] = []
        self._skip_depth = 0
        self._in_heading: str | None = None
        self._heading_parts: list[str] = []
        self._heading_done = False

    # -- block plumbing ------------------------------------------------------

    def _flush(self) -> None:
        text = _collapse_block_text("".join(self._current))
        if text:
            self.blocks.append(text)
        self._current = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:  # noqa: ARG002
        if tag in _SKIPPED_TAGS:
            self._skip_depth += 1
            return
        if self._skip_depth:
            return
        if tag in _BREAK_TAGS:
            self._current.append("\n")
        elif tag in _BLOCK_TAGS:
            self._flush()
            if tag in _HEADING_TAGS and not self._heading_done and self._in_heading is None:
                self._in_heading = tag
                self._heading_parts = []

    def handle_endtag(self, tag: str) -> None:
        if tag in _SKIPPED_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if self._skip_depth:
            return
        if tag in _BLOCK_TAGS:
            if self._in_heading == tag:
                self._close_heading()
            self._flush()

    def handle_data(self, data: str) -> None:
        if self._skip_depth or not data:
            return
        normalized = _norm_source_text(data)
        if not normalized.strip():
            return
        if self._in_heading is not None:
            self._heading_parts.append(normalized)
            return  # heading text is consumed as the title, not the body
        self._current.append(normalized)

    def _close_heading(self) -> None:
        heading = _collapse_block_text("".join(self._heading_parts))
        if heading:
            self.heading = heading
        self._heading_done = True
        self._in_heading = None
        self._heading_parts = []

    # -- result --------------------------------------------------------------

    def result(self) -> tuple[str | None, str]:
        if self._in_heading is not None:  # never closed (malformed doc)
            self._close_heading()
        self._flush()
        return self.heading, "\n\n".join(self.blocks)


def _extract_html_fallback(raw: bytes) -> tuple[str | None, str]:
    """Extract (heading, text) from possibly-malformed HTML via html.parser."""
    extractor = _TextExtractor()
    try:
        extractor.feed(raw.decode("utf-8", errors="replace"))
        extractor.close()
    except Exception:  # noqa: BLE001 - a broken doc still yields whatever parsed
        pass
    return extractor.result()


def _extract_xhtml(raw: bytes) -> tuple[str | None, str]:
    """Extract (first-heading, block-joined text) from an XHTML document.

    ElementTree first; malformed XML falls back to the lenient HTML parser
    (FR-A1 robustness — never crash, never silently drop a chapter).

    Rules (mirrored by the fallback extractor):
    - The FIRST h1–h6 anywhere in the document becomes the chapter title and
      is not re-spoken; later headings stay in the text as line breaks.
    - Each block element contributes one text block; blocks join with a
      blank line; ``<br/>`` and nested blocks become single newlines inside
      a block; script/style/svg/head content is dropped.
    """
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return _extract_html_fallback(raw)

    heading_el = next((el for el in root.iter() if _local_name(el.tag) in _HEADING_TAGS), None)
    heading = None if heading_el is None else _collapse_block_text(" ".join(heading_el.itertext()))

    blocks: list[str] = []

    def _inline_text(element: Any) -> str:
        """All descendant text of a block as one normalized string."""
        parts: list[str] = []

        def collect(node: Any) -> None:
            if node.text:
                parts.append(_norm_source_text(node.text))
            for child in node:
                tag = _local_name(child.tag)
                if tag in _SKIPPED_TAGS or child is heading_el:
                    if child.tail:
                        parts.append(_norm_source_text(child.tail))
                    continue
                if tag in _BREAK_TAGS or tag in _BLOCK_TAGS:
                    parts.append("\n")
                collect(child)
                if tag in _BLOCK_TAGS:
                    parts.append("\n")
                if child.tail:
                    parts.append(_norm_source_text(child.tail))

        collect(element)
        return _collapse_block_text("".join(parts))

    def _emit(text: str) -> None:
        collapsed = _collapse_block_text(text)
        if collapsed:
            blocks.append(collapsed)

    def walk(node: Any) -> None:
        # Stray text directly inside a non-block wrapper (body/span/…) still
        # becomes a block — never lose readable text.
        if node is not heading_el and node.text and node.text.strip():
            _emit(_norm_source_text(node.text))
        for child in node:
            if child is heading_el:
                if child.tail and child.tail.strip():
                    _emit(_norm_source_text(child.tail))
                continue
            tag = _local_name(child.tag)
            if tag in _SKIPPED_TAGS:
                continue
            if tag in _BLOCK_TAGS or tag in _HEADING_TAGS:
                text = _inline_text(child)
                if text:
                    blocks.append(text)
            else:
                walk(child)
            if child.tail and child.tail.strip():
                _emit(_norm_source_text(child.tail))

    walk(root)
    return heading, "\n\n".join(blocks)


def _normalize_zip_name(name: str) -> str:
    """OPF-relative href → zip entry name: percent-decode, forward slashes."""
    decoded = unquote(name).replace("\\", "/")
    while decoded.startswith("./"):
        decoded = decoded[2:]
    return decoded.lstrip("/")


def _resolve_href(opf_dir: str, href: str) -> str:
    href = _normalize_zip_name(href)
    if not opf_dir:
        return href
    return f"{opf_dir}/{href}"


def _find_opf_path(zf: zipfile.ZipFile, path: Path) -> str:
    """container.xml → the OPF zip path (first rootfile)."""
    try:
        container_raw = zf.read(CONTAINER_PATH)
    except KeyError as exc:
        raise DocumentImportError(
            f"Could not read '{path.name}': the EPUB is missing its "
            "META-INF/container.xml (the file may be corrupt or not an EPUB)."
        ) from exc
    try:
        root = ElementTree.fromstring(container_raw)
    except ElementTree.ParseError as exc:
        raise DocumentImportError(
            f"Could not read '{path.name}': META-INF/container.xml is corrupt."
        ) from exc
    for rootfile in root.iter(f"{_OCF_NS}rootfile"):
        full_path = rootfile.get("full-path", "").strip()
        if full_path:
            return _normalize_zip_name(full_path)
    raise DocumentImportError(
        f"Could not read '{path.name}': container.xml names no package (OPF) file."
    )


def _parse_opf(
    zf: zipfile.ZipFile, opf_zip_path: str, path: Path
) -> tuple[str, str, list[dict[str, str]]]:
    """OPF → (title, author, ordered spine manifest entries).

    Spine entries carry ``href``/``media_type``/``properties`` of their
    manifest item; entries whose idref has no manifest item are skipped.
    """
    try:
        opf_raw = zf.read(opf_zip_path)
    except KeyError as exc:
        raise DocumentImportError(
            f"Could not read '{path.name}': the OPF package file "
            f"'{opf_zip_path}' is missing from the EPUB."
        ) from exc
    try:
        package = ElementTree.fromstring(opf_raw)
    except ElementTree.ParseError as exc:
        raise DocumentImportError(
            f"Could not read '{path.name}': the OPF package file is corrupt."
        ) from exc

    title = ""
    author = ""
    metadata = package.find(f"{_OPF_NS}metadata")
    if metadata is not None:
        for element in metadata:
            if element.tag == f"{_DC_NS}title":
                title = (element.text or "").strip()
            elif element.tag == f"{_DC_NS}creator":
                author = (element.text or "").strip()

    manifest: dict[str, dict[str, str]] = {}
    manifest_el = package.find(f"{_OPF_NS}manifest")
    if manifest_el is not None:
        for item in manifest_el:
            if _local_name(item.tag) != "item":
                continue
            item_id = item.get("id", "")
            if not item_id:
                continue
            manifest[item_id] = {
                "href": item.get("href", ""),
                "media_type": item.get("media-type", ""),
                "properties": item.get("properties", "") or "",
            }

    spine_entries: list[dict[str, str]] = []
    spine_el = package.find(f"{_OPF_NS}spine")
    if spine_el is not None:
        for itemref in spine_el:
            if _local_name(itemref.tag) != "itemref":
                continue
            item = manifest.get(itemref.get("idref", ""))
            if item is not None:
                spine_entries.append(item)
    return title, author, spine_entries


def import_epub(path: str | Path) -> EpubBook:
    """Parse an EPUB into an :class:`EpubBook` (FR-A1).

    Raises ``FileNotFoundError`` for missing paths and
    :class:`DocumentImportError` (actionable message, cause chained) for
    every other failure: wrong extension, non-zip, DRM, corrupt structure,
    or a book with no readable text chapters.
    """
    path = Path(path)
    if not path.is_file():
        raise FileNotFoundError(f"no such EPUB file: {path}")
    if path.suffix.lower() != _EPUB_SUFFIX:
        raise DocumentImportError(
            f"Unsupported audiobook type '{path.suffix or path.name}'. "
            "Audiobooks must be .epub files."
        )
    content_hash = _hash_file(path)
    try:
        with zipfile.ZipFile(path) as zf:
            names = set(zf.namelist())
            if ENCRYPTION_PATH in names:
                raise DocumentImportError(DRM_MESSAGE)
            opf_zip_path = _find_opf_path(zf, path)
            title, author, spine_entries = _parse_opf(zf, opf_zip_path, path)
            opf_dir = opf_zip_path.rsplit("/", 1)[0] if "/" in opf_zip_path else ""
            chapters = _extract_chapters(zf, names, opf_dir, spine_entries)
    except DocumentImportError:
        raise
    except zipfile.BadZipFile as exc:
        raise DocumentImportError(
            f"Could not open '{path.name}': it is not a valid EPUB "
            "(the file is not a readable ZIP archive)."
        ) from exc
    except OSError as exc:
        raise DocumentImportError(f"Could not read '{path.name}': {exc}.") from exc

    if not chapters:
        raise DocumentImportError(
            f"'{path.name}' has no readable text chapters (image-only or "
            "empty documents). It cannot be turned into an audiobook."
        )
    return EpubBook(
        title=title or path.stem,
        author=author,
        chapters=chapters,
        source_path=str(path),
        content_hash=content_hash,
    )


def _extract_chapters(
    zf: zipfile.ZipFile,
    names: set[str],
    opf_dir: str,
    spine_entries: list[dict[str, str]],
) -> list[EpubChapter]:
    chapters: list[EpubChapter] = []
    for entry in spine_entries:
        if entry["media_type"] not in _DOCUMENT_MEDIA_TYPES:
            continue
        if "nav" in entry["properties"].split():
            continue  # EPUB3 navigation document — a table of contents, not a chapter
        zip_name = _resolve_href(opf_dir, entry["href"])
        if zip_name not in names:
            continue  # broken manifest reference: skip rather than fail the book
        heading, text = _extract_xhtml(zf.read(zip_name))
        if not text or not text.strip():
            continue  # cover/gallery page with no text
        chapters.append(
            EpubChapter(
                index=len(chapters),
                title=heading or FALLBACK_CHAPTER_TITLE.format(number=len(chapters) + 1),
                text=text,
            )
        )
    return chapters
