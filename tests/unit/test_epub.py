"""EPUB reader (audiobook track FR-A1): .epub → EpubBook chapters."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from vienetts_app.core.epub import (
    DRM_MESSAGE,
    EpubBook,
    EpubChapter,
    import_epub,
)
from vienetts_app.core.importers import DocumentImportError

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"
SAMPLE_EPUB = FIXTURES / "sample.epub"

CONTAINER_XML = (
    '<?xml version="1.0"?>\n'
    '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container">'
    '<rootfiles><rootfile full-path="{opf}" '
    'media-type="application/oebps-package+xml"/></rootfiles></container>'
)


def minimal_opf(chapter_hrefs: list[str], *, title: str = "Book", author: str = "A") -> str:
    manifest = "\n".join(
        f'<item id="c{i}" href="{href}" media-type="application/xhtml+xml"/>'
        for i, href in enumerate(chapter_hrefs)
    )
    spine = "\n".join(f'<itemref idref="c{i}"/>' for i in range(len(chapter_hrefs)))
    return (
        '<?xml version="1.0"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"<dc:title>{title}</dc:title><dc:creator>{author}</dc:creator>"
        "</metadata>"
        f"<manifest>{manifest}</manifest><spine>{spine}</spine></package>"
    )


def chapter_xhtml(title: str | None, paragraphs: list[str]) -> str:
    heading = f"<h1>{title}</h1>" if title else ""
    body = "".join(f"<p>{p}</p>" for p in paragraphs)
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<html xmlns="http://www.w3.org/1999/xhtml"><body>{heading}{body}'
        "</body></html>"
    )


def write_epub(
    path: Path,
    files: dict[str, str | bytes],
    *,
    opf_path: str = "content.opf",
    with_container: bool = True,
    mimetype: bool = True,
) -> Path:
    """Zip ``files`` ({zip-name: text-or-bytes}) into an .epub at ``path``."""
    with zipfile.ZipFile(path, "w") as zf:
        if mimetype:
            info = zipfile.ZipInfo("mimetype")
            info.compress_type = zipfile.ZIP_STORED
            zf.writestr(info, "application/epub+zip")
        if with_container:
            zf.writestr("META-INF/container.xml", CONTAINER_XML.format(opf=opf_path))
        for name, payload in files.items():
            zf.writestr(name, payload)
    return path


class TestModels:
    def test_chapter_fields_and_defaults(self) -> None:
        chapter = EpubChapter(index=0, title="Chương một", text="Nội dung.")
        assert chapter.index == 0
        assert chapter.title == "Chương một"
        assert chapter.text == "Nội dung."

    def test_chapter_rejects_blank_text(self) -> None:
        with pytest.raises(ValueError):
            EpubChapter(index=0, title="t", text="   ")

    def test_chapter_rejects_blank_title(self) -> None:
        with pytest.raises(ValueError):
            EpubChapter(index=0, title="  ", text="text")

    def test_chapter_rejects_negative_index(self) -> None:
        with pytest.raises(ValueError):
            EpubChapter(index=-1, title="t", text="text")

    def test_book_holds_chapters(self) -> None:
        book = EpubBook(
            title="Sách",
            author="Tác giả",
            chapters=[EpubChapter(0, "Chương 1", "a."), EpubChapter(1, "Chương 2", "b.")],
            source_path="/tmp/x.epub",
            content_hash="a" * 64,
        )
        assert len(book.chapters) == 2
        assert book.chapters[1].index == 1

    def test_book_requires_hash_shape(self) -> None:
        with pytest.raises(ValueError):
            EpubBook(
                title="t",
                author="",
                chapters=[EpubChapter(0, "c", "x")],
                source_path="p",
                content_hash="nothex",
            )

    def test_book_requires_at_least_one_chapter(self) -> None:
        with pytest.raises(ValueError):
            EpubBook(title="t", author="", chapters=[], source_path="p", content_hash="b" * 64)


class TestSampleFixture:
    """The committed sample.epub exercises the full happy path."""

    def test_metadata_and_chapter_count(self) -> None:
        book = import_epub(SAMPLE_EPUB)
        assert book.title == "Sách thử nghiệm"
        assert book.author == "Tác Giả A"
        assert len(book.chapters) == 3  # cover + nav excluded

    def test_chapter_titles_in_spine_order(self) -> None:
        titles = [c.title for c in import_epub(SAMPLE_EPUB).chapters]
        assert titles == ["Chương một", "Chương hai", "Chương 3"]

    def test_chapter_texts_join_paragraphs_with_blank_line(self) -> None:
        texts = [c.text for c in import_epub(SAMPLE_EPUB).chapters]
        assert texts[0] == "First paragraph of chapter one.\n\nSecond paragraph of chapter one."
        assert "Đoạn văn tiếng Việt đầu tiên của chương hai." in texts[1]
        assert "—" in texts[1]  # diacritics/punctuation preserved verbatim

    def test_heading_text_not_duplicated_into_body(self) -> None:
        # The h1 becomes the title; it is not spoken again at paragraph level.
        assert "Chương một" not in import_epub(SAMPLE_EPUB).chapters[0].text

    def test_content_hash_is_sha256_of_file(self) -> None:
        digest = hashlib.sha256(SAMPLE_EPUB.read_bytes()).hexdigest()
        assert import_epub(SAMPLE_EPUB).content_hash == digest
        assert len(digest) == 64

    def test_source_path_recorded(self) -> None:
        assert import_epub(SAMPLE_EPUB).source_path == str(SAMPLE_EPUB)

    def test_chapter_indexes_are_contiguous(self) -> None:
        assert [c.index for c in import_epub(SAMPLE_EPUB).chapters] == [0, 1, 2]

    def test_accepts_str_path(self) -> None:
        assert import_epub(str(SAMPLE_EPUB)).title == "Sách thử nghiệm"


class TestVariantBooks:
    def test_fallback_title_without_heading(self, tmp_path: Path) -> None:
        path = write_epub(
            tmp_path / "noheading.epub",
            {
                "content.opf": minimal_opf(["a.xhtml"]),
                "a.xhtml": chapter_xhtml(None, ["One paragraph only."]),
            },
        )
        book = import_epub(path)
        assert book.chapters[0].title == "Chương 1"
        assert book.chapters[0].text == "One paragraph only."

    def test_title_falls_back_to_filename(self, tmp_path: Path) -> None:
        path = write_epub(
            tmp_path / "Tiểu thuyết.epub",
            {
                "content.opf": minimal_opf(["a.xhtml"], title="", author="X"),
                "a.xhtml": chapter_xhtml("C", ["t."]),
            },
        )
        assert import_epub(path).title == "Tiểu thuyết"

    def test_author_missing_is_empty_string(self, tmp_path: Path) -> None:
        path = write_epub(
            tmp_path / "noauthor.epub",
            {
                "content.opf": minimal_opf(["a.xhtml"], title="T", author=""),
                "a.xhtml": chapter_xhtml("C", ["t."]),
            },
        )
        assert import_epub(path).author == ""

    def test_percent_encoded_hrefs_resolve(self, tmp_path: Path) -> None:
        opf = minimal_opf(["ch%20one.xhtml"])
        path = write_epub(
            tmp_path / "encoded.epub",
            {
                "content.opf": opf,
                "ch one.xhtml": chapter_xhtml("C", ["encoded href body."]),
            },
        )
        assert import_epub(path).chapters[0].text == "encoded href body."

    def test_opf_in_subdirectory_resolves_relative_hrefs(self, tmp_path: Path) -> None:
        path = write_epub(
            tmp_path / "subdir.epub",
            {
                "OEBPS/content.opf": minimal_opf(["text/a.xhtml"]),
                "OEBPS/text/a.xhtml": chapter_xhtml("C", ["nested body."]),
            },
            opf_path="OEBPS/content.opf",
        )
        assert import_epub(path).chapters[0].text == "nested body."

    def test_malformed_xhtml_falls_back_to_html_extraction(self, tmp_path: Path) -> None:
        malformed = (
            '<?xml version="1.0" encoding="UTF-8"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            "<h1>Chương hỏng</h1><p>Đoạn chưa đóng thẻ"
        )  # unclosed p and body — invalid XML, parseable as HTML
        path = write_epub(
            tmp_path / "malformed.epub",
            {"content.opf": minimal_opf(["a.xhtml"]), "a.xhtml": malformed},
        )
        chapter = import_epub(path).chapters[0]
        assert chapter.title == "Chương hỏng"
        assert "Đoạn chưa đóng thẻ" in chapter.text

    def test_inline_and_block_whitespace_normalized(self, tmp_path: Path) -> None:
        xhtml = (
            '<?xml version="1.0"?>\n'
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            "<p>Word   one\n\ttwo</p><p></p><div>   </div><p>Three</p>"
            "</body></html>"
        )
        path = write_epub(
            tmp_path / "ws.epub",
            {"content.opf": minimal_opf(["a.xhtml"]), "a.xhtml": xhtml},
        )
        assert import_epub(path).chapters[0].text == "Word one two\n\nThree"

    def test_br_becomes_newline(self, tmp_path: Path) -> None:
        xhtml = (
            '<?xml version="1.0"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            "<p>Line one<br/>Line two</p></body></html>"
        )
        path = write_epub(
            tmp_path / "br.epub",
            {"content.opf": minimal_opf(["a.xhtml"]), "a.xhtml": xhtml},
        )
        assert import_epub(path).chapters[0].text == "Line one\nLine two"

    def test_textless_spine_docs_are_skipped(self, tmp_path: Path) -> None:
        cover = (
            '<?xml version="1.0"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<div><img src="c.png" alt="cover"/></div></body></html>'
        )
        opf = (
            '<?xml version="1.0"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>B</dc:title></metadata><manifest>"
            '<item id="c0" href="cover.xhtml" media-type="application/xhtml+xml"/>'
            '<item id="c1" href="a.xhtml" media-type="application/xhtml+xml"/>'
            "</manifest><spine>"
            '<itemref idref="c0"/><itemref idref="c1"/></spine></package>'
        )
        path = write_epub(
            tmp_path / "cover.epub",
            {
                "content.opf": opf,
                "cover.xhtml": cover,
                "a.xhtml": chapter_xhtml("C", ["real chapter."]),
            },
        )
        book = import_epub(path)
        assert len(book.chapters) == 1
        assert book.chapters[0].text == "real chapter."

    def test_non_html_manifest_items_ignored(self, tmp_path: Path) -> None:
        # spine references a css item: not a document, must not crash or count
        opf = (
            '<?xml version="1.0"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="3.0">'
            '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
            "<dc:title>B</dc:title></metadata><manifest>"
            '<item id="s" href="style.css" media-type="text/css"/>'
            '<item id="a" href="a.xhtml" media-type="application/xhtml+xml"/>'
            "</manifest><spine>"
            '<itemref idref="s"/><itemref idref="a"/></spine></package>'
        )
        path = write_epub(
            tmp_path / "css.epub",
            {
                "content.opf": opf,
                "style.css": "p{}",
                "a.xhtml": chapter_xhtml("C", ["body."]),
            },
        )
        assert len(import_epub(path).chapters) == 1


class TestErrors:
    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            import_epub(tmp_path / "nope.epub")

    def test_directory(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            import_epub(tmp_path)

    def test_wrong_extension(self, tmp_path: Path) -> None:
        plain = tmp_path / "book.txt"
        plain.write_text("x", encoding="utf-8")
        with pytest.raises(DocumentImportError, match="epub"):
            import_epub(plain)

    def test_not_a_zip(self, tmp_path: Path) -> None:
        bogus = tmp_path / "fake.epub"
        bogus.write_bytes(b"PK-fake-not-a-zip" * 10)
        with pytest.raises(DocumentImportError, match="not a valid EPUB"):
            import_epub(bogus)

    def test_drm_encrypted(self, tmp_path: Path) -> None:
        path = write_epub(
            tmp_path / "drm.epub",
            {
                "META-INF/encryption.xml": "<encryption/>",
                "content.opf": minimal_opf(["a.xhtml"]),
                "a.xhtml": chapter_xhtml("C", ["t."]),
            },
        )
        with pytest.raises(DocumentImportError) as excinfo:
            import_epub(path)
        assert str(excinfo.value) == DRM_MESSAGE

    def test_missing_container(self, tmp_path: Path) -> None:
        path = write_epub(
            tmp_path / "nocontainer.epub",
            {"content.opf": minimal_opf(["a.xhtml"]), "a.xhtml": chapter_xhtml("C", ["t."])},
            with_container=False,
        )
        with pytest.raises(DocumentImportError, match="container"):
            import_epub(path)

    def test_corrupt_container_xml(self, tmp_path: Path) -> None:
        with zipfile.ZipFile(tmp_path / "badcontainer.epub", "w") as zf:
            zf.writestr("META-INF/container.xml", "<container><rootfiles>")
        with pytest.raises(DocumentImportError, match="container"):
            import_epub(tmp_path / "badcontainer.epub")

    def test_opf_missing_from_zip(self, tmp_path: Path) -> None:
        path = write_epub(tmp_path / "noopf.epub", {"a.xhtml": chapter_xhtml("C", ["t."])})
        with pytest.raises(DocumentImportError, match="content\\.opf|OPF"):
            import_epub(path)

    def test_corrupt_opf(self, tmp_path: Path) -> None:
        path = write_epub(
            tmp_path / "badopf.epub",
            {"content.opf": "<package><manifest>"},
        )
        with pytest.raises(DocumentImportError, match="OPF"):
            import_epub(path)

    def test_book_with_no_text_chapters(self, tmp_path: Path) -> None:
        cover_only = (
            '<?xml version="1.0"?>'
            '<html xmlns="http://www.w3.org/1999/xhtml"><body>'
            '<div><img src="c.png" alt="cover"/></div></body></html>'
        )
        path = write_epub(
            tmp_path / "empty.epub",
            {
                "content.opf": minimal_opf(["cover.xhtml"]),
                "cover.xhtml": cover_only,
            },
        )
        with pytest.raises(DocumentImportError, match="no readable text"):
            import_epub(path)
