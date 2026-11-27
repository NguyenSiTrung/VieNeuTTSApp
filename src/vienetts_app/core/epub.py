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
import posixpath
import re
import zipfile
import zlib
from dataclasses import dataclass
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import unquote
from xml.etree import ElementTree

from vienetts_app.core.importers import DocumentImportError

CONTAINER_PATH = "META-INF/container.xml"
ENCRYPTION_PATH = "META-INF/encryption.xml"
MAX_COVER_BYTES = 32 * 1024 * 1024  # 32 MiB cap for embedded cover images

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
        "figure",
        "section",
        "article",
        "header",
        "footer",
        "aside",
        "main",
        "nav",
        "dt",
        "dd",
    }
)

# Exceptions raised by zipfile/zlib when opening or reading corrupt/unsupported archives.
_CORRUPT_ARCHIVE_ERRORS = (
    zipfile.BadZipFile,
    zipfile.LargeZipFile,
    zlib.error,
    EOFError,
    RuntimeError,
)

# Headings, in the order h1..h6 — the first present becomes the title.
_HEADING_TAGS = ("h1", "h2", "h3", "h4", "h5", "h6")

# Content whose text is never spoken.
_SKIPPED_TAGS = frozenset({"script", "style", "head", "title", "svg"})
_DESC_SKIPPED_TAGS = frozenset({"script", "style"})

_WHITESPACE_RE = re.compile(r"\s+")
_LANG_PRIMARY_RE = re.compile(r"^[A-Za-z]{2,3}$")
_LANG_SUBTAG_RE = re.compile(r"^[A-Za-z0-9]{1,8}$")
_ISBN_PREFIX_RE = re.compile(r"^(?:urn:isbn:|isbn:)", re.IGNORECASE)
_ISBN_RE = re.compile(r"^(?:[0-9]{9}[0-9X]|[0-9]{13})$")
_ISBN_TYPES = frozenset({"15", "02", "isbn"})
_YEAR_RE = re.compile(r"(?<!\d)([12]\d{3})(?!\d)")


def sniff_image_ext(data: bytes) -> str:
    """Sniff image format from magic bytes: ``jpg``, ``png``, ``gif``, ``webp``, or ``""``."""
    if data.startswith(b"\xff\xd8\xff"):
        return "jpg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if len(data) >= 6 and data.startswith(b"GIF8"):
        return "gif"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "webp"
    return ""


@dataclass(frozen=True)
class EpubMetadata:
    """OPF metadata and optional cover art extracted from an EPUB."""

    title: str
    author: str = ""
    authors: tuple[str, ...] = ()
    language: str = ""
    isbn: str = ""
    year: int | None = None
    description: str = ""
    cover_bytes: bytes | None = None
    cover_ext: str = ""


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
    authors: tuple[str, ...] = ()
    language: str = ""
    isbn: str = ""
    year: int | None = None
    description: str = ""
    cover_bytes: bytes | None = None
    cover_ext: str = ""

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
      is not re-spoken; later headings stay in the text as separate blocks.
    - Block and container elements flush inline runs on entry and exit so
      nested ``<p>``/block elements inside ``<section>``/``<div>``/``<article>``
      stay distinct paragraphs joined with a blank line; inline elements flow
      naturally; ``<br/>`` becomes a single newline; script/style/svg/head
      content is dropped.
    """
    try:
        root = ElementTree.fromstring(raw)
    except ElementTree.ParseError:
        return _extract_html_fallback(raw)

    heading_el = next(
        (el for el in root.iter() if _local_name(el.tag).lower() in _HEADING_TAGS), None
    )
    heading = None if heading_el is None else _collapse_block_text(" ".join(heading_el.itertext()))

    blocks: list[str] = []
    inline_buf: list[str] = []

    def _flush_inline() -> None:
        if inline_buf:
            collapsed = _collapse_block_text("".join(inline_buf))
            if collapsed:
                blocks.append(collapsed)
            inline_buf.clear()

    def walk(node: Any) -> None:
        if node is heading_el:
            _flush_inline()
            return
        tag = _local_name(node.tag).lower()
        if tag in _SKIPPED_TAGS:
            return
        is_block = tag in _BLOCK_TAGS or tag in _HEADING_TAGS
        if is_block:
            _flush_inline()
        if node.text:
            inline_buf.append(_norm_source_text(node.text))
        for child in node:
            child_tag = _local_name(child.tag).lower()
            if child_tag in _SKIPPED_TAGS:
                if child.tail:
                    inline_buf.append(_norm_source_text(child.tail))
                continue
            if child is heading_el:
                _flush_inline()
                if child.tail:
                    inline_buf.append(_norm_source_text(child.tail))
                continue
            if child_tag in _BREAK_TAGS:
                inline_buf.append("\n")
                if child.tail:
                    inline_buf.append(_norm_source_text(child.tail))
                continue
            walk(child)
            if child.tail:
                inline_buf.append(_norm_source_text(child.tail))
        if is_block:
            _flush_inline()

    walk(root)
    _flush_inline()
    return heading, "\n\n".join(blocks)


def _resolve_href(opf_dir: str, href: str) -> str:
    """Resolve an OPF-relative ``href`` to a normalized POSIX ZIP entry name."""
    raw = unquote(href.split("#", 1)[0]).replace("\\", "/")
    cleaned = raw.lstrip("/")
    if not cleaned:
        return ""
    joined = f"{opf_dir}/{cleaned}" if opf_dir else cleaned
    normalized = posixpath.normpath(joined)
    if normalized in ("", ".", "..") or normalized.startswith("../"):
        return ""
    return normalized.lstrip("/")


def _collapse_ws(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", text).strip()


def _normalize_language(raw: str) -> str:
    """Normalize a BCP 47 language tag; return ``""`` if empty or invalid."""
    cleaned = raw.strip().replace("_", "-")
    if not cleaned:
        return ""
    parts = cleaned.split("-")
    if not _LANG_PRIMARY_RE.fullmatch(parts[0]):
        return ""
    normalized = [parts[0].lower()]
    in_extension = False
    for part in parts[1:]:
        if not _LANG_SUBTAG_RE.fullmatch(part):
            return ""
        if in_extension:
            normalized.append(part.lower())
        elif len(part) == 1:
            in_extension = True
            normalized.append(part.lower())
        elif len(part) == 4 and part.isalpha():
            normalized.append(part.capitalize())
        elif len(part) == 2 and part.isalpha():
            normalized.append(part.upper())
        else:
            normalized.append(part.lower())
    return "-".join(normalized)


def _normalize_isbn(raw: str) -> str:
    """Strip ISBN prefix/separators and validate ISBN-10 or ISBN-13 shape."""
    stripped = _ISBN_PREFIX_RE.sub("", raw.strip())
    compact = re.sub(r"[-\s]+", "", stripped).upper()
    return compact if _ISBN_RE.fullmatch(compact) else ""


def _extract_year(raw: str) -> int | None:
    """Extract a 4-digit year in ``1000..2999`` from a ``dc:date`` string."""
    match = _YEAR_RE.search(raw)
    return int(match.group(1)) if match else None


class _PlainTextExtractor(HTMLParser):
    """Strip HTML tags to single-space-collapsed plain text."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._parts: list[str] = []
        self._skip_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:  # noqa: ARG002
        local = tag.rsplit(":", 1)[-1].lower()
        if local in _DESC_SKIPPED_TAGS:
            self._skip_depth += 1
            return
        if not self._skip_depth and (local in _BLOCK_TAGS or local in _BREAK_TAGS or local == "hr"):
            self._parts.append(" ")

    def handle_endtag(self, tag: str) -> None:
        local = tag.rsplit(":", 1)[-1].lower()
        if local in _DESC_SKIPPED_TAGS:
            if self._skip_depth:
                self._skip_depth -= 1
            return
        if not self._skip_depth and local in _BLOCK_TAGS:
            self._parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self._skip_depth and data:
            self._parts.append(data)

    def result(self) -> str:
        return _collapse_ws("".join(self._parts))


def _extract_description(element: Any) -> str:
    """Extract plain text from a ``dc:description`` element (stripping HTML if any)."""
    if len(element) == 0:
        raw = element.text or ""
    else:
        raw = (element.text or "") + "".join(
            ElementTree.tostring(child, encoding="unicode") for child in element
        )
    if not raw.strip():
        return ""
    parser = _PlainTextExtractor()
    try:
        parser.feed(raw)
        parser.close()
    except Exception:  # noqa: BLE001
        return _collapse_ws(raw)
    return parser.result()


def _is_remote_href(href: str) -> bool:
    stripped = href.strip()
    return not stripped or "://" in stripped or stripped.lower().startswith("data:")


def _extract_cover(
    zf: zipfile.ZipFile,
    names: set[str],
    opf_dir: str,
    candidate_hrefs: list[str],
) -> tuple[bytes | None, str]:
    """Locate and sniff cover image bytes; silently return ``(None, "")`` on any problem."""
    tried: set[str] = set()
    for href in candidate_hrefs:
        if _is_remote_href(href):
            continue
        zip_name = _resolve_href(opf_dir, href)
        if not zip_name or zip_name in tried or zip_name not in names:
            continue
        tried.add(zip_name)
        try:
            info = zf.getinfo(zip_name)
            if info.file_size <= 0 or info.file_size > MAX_COVER_BYTES:
                continue
            with zf.open(info, "r") as stream:
                data = stream.read(MAX_COVER_BYTES + 1)
        except (KeyError, OSError, *_CORRUPT_ARCHIVE_ERRORS):
            continue
        if not data or len(data) > MAX_COVER_BYTES:
            continue
        ext = sniff_image_ext(data)
        if ext:
            return data, ext
    return None, ""


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
            normalized = _resolve_href("", full_path)
            if normalized:
                return normalized
    raise DocumentImportError(
        f"Could not read '{path.name}': container.xml names no package (OPF) file."
    )


def _validate_epub_path(path: str | Path) -> Path:
    epub_path = Path(path)
    if not epub_path.is_file():
        raise FileNotFoundError(f"no such EPUB file: {epub_path}")
    if epub_path.suffix.lower() != _EPUB_SUFFIX:
        raise DocumentImportError(
            f"Unsupported audiobook type '{epub_path.suffix or epub_path.name}'. "
            "Audiobooks must be .epub files."
        )
    return epub_path


def _find_child(parent: Any, local_name: str) -> Any | None:
    return next((el for el in parent if _local_name(el.tag) == local_name), None)


def _iter_metadata_elements(metadata_el: Any) -> list[Any]:
    elements: list[Any] = []
    for child in metadata_el:
        if _local_name(child.tag) in ("dc-metadata", "x-metadata"):
            elements.extend(child)
        else:
            elements.append(child)
    return elements


def _load_opf_package(zf: zipfile.ZipFile, path: Path) -> tuple[set[str], str, Any]:
    names = set(zf.namelist())
    if ENCRYPTION_PATH in names:
        raise DocumentImportError(DRM_MESSAGE)
    opf_zip_path = _find_opf_path(zf, path)
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
    return names, opf_zip_path, package


def _parse_manifest(package: Any) -> tuple[dict[str, dict[str, str]], list[str]]:
    manifest: dict[str, dict[str, str]] = {}
    epub3_cover_hrefs: list[str] = []
    manifest_el = _find_child(package, "manifest")
    if manifest_el is not None:
        for item in manifest_el:
            if _local_name(item.tag) != "item":
                continue
            href = item.get("href", "")
            properties = item.get("properties", "") or ""
            if "cover-image" in properties.split() and href:
                epub3_cover_hrefs.append(href)
            item_id = item.get("id", "")
            if not item_id:
                continue
            manifest[item_id] = {
                "href": href,
                "media_type": item.get("media-type", ""),
                "properties": properties,
            }
    return manifest, epub3_cover_hrefs


def _parse_metadata(
    package: Any,
    manifest: dict[str, dict[str, str]],
    epub3_cover_hrefs: list[str],
    zf: zipfile.ZipFile,
    names: set[str],
    opf_dir: str,
    fallback_title: str,
    *,
    include_cover: bool = True,
) -> EpubMetadata:
    metadata_el = _find_child(package, "metadata")
    elements = _iter_metadata_elements(metadata_el) if metadata_el is not None else []

    refinements: dict[tuple[str, str], list[str]] = {}
    epub2_cover_ids: list[str] = []
    for el in elements:
        if _local_name(el.tag) != "meta":
            continue
        ref = (el.get("refines") or "").strip()
        prop = (el.get("property") or "").strip().lower()
        if ref.startswith("#") and prop:
            val = _collapse_ws("".join(el.itertext())).lower()
            if val:
                refinements.setdefault((ref[1:], prop), []).append(val)
        if (el.get("name") or "").strip().lower() == "cover":
            cover_id = (el.get("content") or "").strip()
            if cover_id:
                epub2_cover_ids.append(cover_id)

    title = ""
    authors_list: list[str] = []
    seen_authors: set[str] = set()
    language_raw = ""
    isbn = ""
    pub_year: int | None = None
    unset_year: int | None = None
    other_year: int | None = None
    description = ""

    for el in elements:
        tag = el.tag
        if tag == f"{_DC_NS}title":
            if not title:
                candidate = _collapse_ws("".join(el.itertext()))
                if candidate:
                    title = candidate
        elif tag == f"{_DC_NS}creator":
            name = _collapse_ws("".join(el.itertext()))
            if not name:
                continue
            roles: set[str] = set()
            for attr_role in (el.get(f"{_OPF_NS}role"), el.get("role")):
                if attr_role and attr_role.strip():
                    roles.add(attr_role.strip().lower())
            el_id = (el.get("id") or "").strip()
            if el_id:
                roles.update(refinements.get((el_id, "role"), ()))
            if roles and "aut" not in roles:
                continue
            if name not in seen_authors:
                seen_authors.add(name)
                authors_list.append(name)
        elif tag == f"{_DC_NS}language":
            if not language_raw:
                candidate = "".join(el.itertext()).strip()
                if candidate:
                    language_raw = candidate
        elif tag == f"{_DC_NS}identifier":
            if isbn:
                continue
            raw_id = "".join(el.itertext()).strip()
            if not raw_id:
                continue
            scheme = (el.get(f"{_OPF_NS}scheme") or el.get("scheme") or "").strip().lower()
            el_id = (el.get("id") or "").strip()
            id_types = refinements.get((el_id, "identifier-type"), ()) if el_id else ()
            is_isbn = (
                scheme == "isbn"
                or bool(_ISBN_PREFIX_RE.match(raw_id))
                or any(t in _ISBN_TYPES for t in id_types)
            )
            if is_isbn:
                parsed_isbn = _normalize_isbn(raw_id)
                if parsed_isbn:
                    isbn = parsed_isbn
        elif tag == f"{_DC_NS}date":
            parsed_year = _extract_year("".join(el.itertext()))
            if parsed_year is None:
                continue
            event = (el.get(f"{_OPF_NS}event") or el.get("event") or "").strip().lower()
            if event == "publication":
                if pub_year is None:
                    pub_year = parsed_year
            elif not event:
                if unset_year is None:
                    unset_year = parsed_year
            elif other_year is None:
                other_year = parsed_year
        elif tag == f"{_DC_NS}description" and not description:
            candidate = _extract_description(el)
            if candidate:
                description = candidate

    if include_cover:
        cover_candidates = list(epub3_cover_hrefs)
        for cover_id in epub2_cover_ids:
            item = manifest.get(cover_id)
            if item and item["href"]:
                cover_candidates.append(item["href"])
        cover_bytes, cover_ext = _extract_cover(zf, names, opf_dir, cover_candidates)
    else:
        cover_bytes, cover_ext = None, ""

    authors = tuple(authors_list)
    year = (
        pub_year if pub_year is not None else (unset_year if unset_year is not None else other_year)
    )
    return EpubMetadata(
        title=title or fallback_title,
        author="; ".join(authors),
        authors=authors,
        language=_normalize_language(language_raw) if language_raw else "",
        isbn=isbn,
        year=year,
        description=description,
        cover_bytes=cover_bytes,
        cover_ext=cover_ext,
    )


def _parse_spine(package: Any, manifest: dict[str, dict[str, str]]) -> list[dict[str, str]]:
    spine_entries: list[dict[str, str]] = []
    spine_el = _find_child(package, "spine")
    if spine_el is not None:
        for itemref in spine_el:
            if _local_name(itemref.tag) != "itemref":
                continue
            item = manifest.get(itemref.get("idref", ""))
            if item is not None:
                spine_entries.append(item)
    return spine_entries


def extract_epub_metadata(path: str | Path, *, include_cover: bool = True) -> EpubMetadata:
    """Extract OPF metadata and optionally cover image without reading chapter XHTML.

    Validates path, ``.epub`` suffix, ZIP structure, DRM, ``container.xml``,
    and OPF package identically to :func:`import_epub`, then returns an
    :class:`EpubMetadata` instance. When ``include_cover`` is ``False``, the
    cover image entry is not read from the archive.
    """
    epub_path = _validate_epub_path(path)
    try:
        with zipfile.ZipFile(epub_path) as zf:
            names, opf_zip_path, package = _load_opf_package(zf, epub_path)
            manifest, epub3_cover_hrefs = _parse_manifest(package)
            opf_dir = opf_zip_path.rsplit("/", 1)[0] if "/" in opf_zip_path else ""
            return _parse_metadata(
                package,
                manifest,
                epub3_cover_hrefs,
                zf,
                names,
                opf_dir,
                fallback_title=epub_path.stem,
                include_cover=include_cover,
            )
    except DocumentImportError:
        raise
    except _CORRUPT_ARCHIVE_ERRORS as exc:
        raise DocumentImportError(
            f"Could not open '{epub_path.name}': it is not a valid EPUB "
            f"(corrupt or unsupported ZIP archive: {exc})."
        ) from exc
    except OSError as exc:
        raise DocumentImportError(f"Could not read '{epub_path.name}': {exc}.") from exc


def import_epub(path: str | Path) -> EpubBook:
    """Parse an EPUB into an :class:`EpubBook` (FR-A1).

    Raises ``FileNotFoundError`` for missing paths and
    :class:`DocumentImportError` (actionable message, cause chained) for
    every other failure: wrong extension, non-zip, DRM, corrupt structure,
    or a book with no readable text chapters.
    """
    epub_path = _validate_epub_path(path)
    content_hash = _hash_file(epub_path)
    try:
        with zipfile.ZipFile(epub_path) as zf:
            names, opf_zip_path, package = _load_opf_package(zf, epub_path)
            manifest, epub3_cover_hrefs = _parse_manifest(package)
            opf_dir = opf_zip_path.rsplit("/", 1)[0] if "/" in opf_zip_path else ""
            meta = _parse_metadata(
                package,
                manifest,
                epub3_cover_hrefs,
                zf,
                names,
                opf_dir,
                fallback_title=epub_path.stem,
            )
            spine_entries = _parse_spine(package, manifest)
            chapters = _extract_chapters(zf, names, opf_dir, spine_entries)
    except DocumentImportError:
        raise
    except _CORRUPT_ARCHIVE_ERRORS as exc:
        raise DocumentImportError(
            f"Could not open '{epub_path.name}': it is not a valid EPUB "
            f"(corrupt or unsupported ZIP archive: {exc})."
        ) from exc
    except OSError as exc:
        raise DocumentImportError(f"Could not read '{epub_path.name}': {exc}.") from exc

    if not chapters:
        raise DocumentImportError(
            f"'{epub_path.name}' has no readable text chapters (image-only or "
            "empty documents). It cannot be turned into an audiobook."
        )
    return EpubBook(
        title=meta.title,
        author=meta.author,
        chapters=chapters,
        source_path=str(epub_path),
        content_hash=content_hash,
        authors=meta.authors,
        language=meta.language,
        isbn=meta.isbn,
        year=meta.year,
        description=meta.description,
        cover_bytes=meta.cover_bytes,
        cover_ext=meta.cover_ext,
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
