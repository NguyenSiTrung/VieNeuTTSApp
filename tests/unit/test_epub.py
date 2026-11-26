"""EPUB reader (audiobook track FR-A1): .epub → EpubBook chapters."""

from __future__ import annotations

import hashlib
import zipfile
from pathlib import Path

import pytest

from vienetts_app.core.epub import (
    DRM_MESSAGE,
    MAX_COVER_BYTES,
    EpubBook,
    EpubChapter,
    EpubMetadata,
    extract_epub_metadata,
    import_epub,
    sniff_image_ext,
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
    def test_model_contracts(self) -> None:
        EpubChapter(index=0, title="Chương một", text="Nội dung.")
        with pytest.raises(ValueError):
            EpubChapter(index=0, title="t", text="   ")
        with pytest.raises(ValueError):
            EpubChapter(index=0, title="  ", text="text")
        with pytest.raises(ValueError):
            EpubChapter(index=-1, title="t", text="text")

        book = EpubBook(
            title="Sách",
            author="Tác giả",
            chapters=[EpubChapter(0, "Chương 1", "a."), EpubChapter(1, "Chương 2", "b.")],
            source_path="/tmp/x.epub",
            content_hash="a" * 64,
        )
        assert book.authors == ()
        assert book.language == ""
        assert book.isbn == ""
        assert book.year is None
        assert book.description == ""
        assert book.cover_bytes is None
        assert book.cover_ext == ""

        meta = EpubMetadata(title="Sách")
        assert meta.author == ""
        assert meta.authors == ()
        assert meta.language == ""
        assert meta.isbn == ""
        assert meta.year is None
        assert meta.description == ""
        assert meta.cover_bytes is None
        assert meta.cover_ext == ""
        assert MAX_COVER_BYTES == 32 * 1024 * 1024

        with pytest.raises(ValueError):
            EpubBook(
                title="t",
                author="",
                chapters=[EpubChapter(0, "c", "x")],
                source_path="p",
                content_hash="nothex",
            )
        with pytest.raises(ValueError):
            EpubBook(title="t", author="", chapters=[], source_path="p", content_hash="b" * 64)

    def test_sniff_image_ext(self) -> None:
        assert sniff_image_ext(b"\xff\xd8\xff\xe0\x00\x10JFIF") == "jpg"
        assert sniff_image_ext(b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR") == "png"
        assert sniff_image_ext(b"GIF89a\x01\x00") == "gif"
        assert sniff_image_ext(b"GIF87a\x01\x00") == "gif"
        assert sniff_image_ext(b"RIFF\x24\x00\x00\x00WEBPVP8 ") == "webp"

        # Truncated or unrecognized signatures
        assert sniff_image_ext(b"") == ""
        assert sniff_image_ext(b"\xff\xd8") == ""
        assert sniff_image_ext(b"\x89PNG\r\n") == ""
        assert sniff_image_ext(b"GIF8") == ""
        assert sniff_image_ext(b"RIFF\x24\x00\x00\x00WAVEfmt ") == ""
        assert sniff_image_ext(b"RIFF\x24\x00\x00\x00WEB") == ""
        assert sniff_image_ext(b"<svg xmlns='http://www.w3.org/2000/svg'/>") == ""


class TestSampleFixture:
    """The committed sample.epub exercises the full happy path."""

    def test_sample_parsing(self) -> None:
        book = import_epub(SAMPLE_EPUB)
        assert book.title == "Sách thử nghiệm"
        assert book.author == "Tác Giả A"
        assert book.authors == ("Tác Giả A",)
        assert book.language == "vi"
        assert book.isbn == ""
        assert book.year is None
        assert book.description == ""
        assert book.cover_bytes is None
        assert book.cover_ext == ""
        assert len(book.chapters) == 3  # cover + nav excluded
        assert [c.title for c in book.chapters] == ["Chương một", "Chương hai", "Chương 3"]
        assert book.chapters[0].text == (
            "First paragraph of chapter one.\n\nSecond paragraph of chapter one."
        )
        assert "Đoạn văn tiếng Việt đầu tiên của chương hai." in book.chapters[1].text
        assert "—" in book.chapters[1].text  # diacritics/punctuation preserved verbatim
        assert "Chương một" not in book.chapters[0].text  # h1 heading not duplicated in body
        digest = hashlib.sha256(SAMPLE_EPUB.read_bytes()).hexdigest()
        assert book.content_hash == digest
        assert book.source_path == str(SAMPLE_EPUB)
        assert [c.index for c in book.chapters] == [0, 1, 2]

        assert import_epub(str(SAMPLE_EPUB)).title == "Sách thử nghiệm"


class TestVariantBooks:
    def test_title_and_author_fallbacks(self, tmp_path: Path) -> None:
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

        path = write_epub(
            tmp_path / "Tiểu thuyết.epub",
            {
                "content.opf": minimal_opf(["a.xhtml"], title="", author="X"),
                "a.xhtml": chapter_xhtml("C", ["t."]),
            },
        )
        assert import_epub(path).title == "Tiểu thuyết"

        path = write_epub(
            tmp_path / "noauthor.epub",
            {
                "content.opf": minimal_opf(["a.xhtml"], title="T", author=""),
                "a.xhtml": chapter_xhtml("C", ["t."]),
            },
        )
        assert import_epub(path).author == ""

    def test_href_resolution(self, tmp_path: Path) -> None:
        opf = minimal_opf(["ch%20one.xhtml"])
        path = write_epub(
            tmp_path / "encoded.epub",
            {
                "content.opf": opf,
                "ch one.xhtml": chapter_xhtml("C", ["encoded href body."]),
            },
        )
        assert import_epub(path).chapters[0].text == "encoded href body."

        path = write_epub(
            tmp_path / "subdir.epub",
            {
                "OEBPS/content.opf": minimal_opf(["text/a.xhtml"]),
                "OEBPS/text/a.xhtml": chapter_xhtml("C", ["nested body."]),
            },
            opf_path="OEBPS/content.opf",
        )
        assert import_epub(path).chapters[0].text == "nested body."

    def test_text_extraction(self, tmp_path: Path) -> None:
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
    def test_error_cases(self, tmp_path: Path) -> None:
        with pytest.raises(FileNotFoundError):
            import_epub(tmp_path / "nope.epub")
        with pytest.raises(FileNotFoundError):
            import_epub(tmp_path)

        plain = tmp_path / "book.txt"
        plain.write_text("x", encoding="utf-8")
        with pytest.raises(DocumentImportError, match="epub"):
            import_epub(plain)

        bogus = tmp_path / "fake.epub"
        bogus.write_bytes(b"PK-fake-not-a-zip" * 10)
        with pytest.raises(DocumentImportError, match="not a valid EPUB"):
            import_epub(bogus)

        path = write_epub(
            tmp_path / "nocontainer.epub",
            {"content.opf": minimal_opf(["a.xhtml"]), "a.xhtml": chapter_xhtml("C", ["t."])},
            with_container=False,
        )
        with pytest.raises(DocumentImportError, match="container"):
            import_epub(path)

        with zipfile.ZipFile(tmp_path / "badcontainer.epub", "w") as zf:
            zf.writestr("META-INF/container.xml", "<container><rootfiles>")
        with pytest.raises(DocumentImportError, match="container"):
            import_epub(tmp_path / "badcontainer.epub")

        path = write_epub(tmp_path / "noopf.epub", {"a.xhtml": chapter_xhtml("C", ["t."])})
        with pytest.raises(DocumentImportError, match="content\\.opf|OPF"):
            import_epub(path)

        path = write_epub(
            tmp_path / "badopf.epub",
            {"content.opf": "<package><manifest>"},
        )
        with pytest.raises(DocumentImportError, match="OPF"):
            import_epub(path)

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


_JPEG_BYTES = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
_PNG_BYTES = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01"
_GIF_BYTES = b"GIF89a\x01\x00\x01\x00\x80\x00\x00\x00\x00\x00\xff\xff\xff"
_WEBP_BYTES = b"RIFF\x1a\x00\x00\x00WEBPVP8 \x0e\x00\x00\x00\x00\x00"


def custom_opf(
    metadata_xml: str,
    manifest_xml: str = '<item id="c0" href="a.xhtml" media-type="application/xhtml+xml"/>',
    spine_xml: str = '<itemref idref="c0"/>',
    *,
    version: str = "3.0",
) -> str:
    return (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        f'<package xmlns="http://www.idpf.org/2007/opf" '
        f'xmlns:opf="http://www.idpf.org/2007/opf" version="{version}">'
        '<metadata xmlns:dc="http://purl.org/dc/elements/1.1/">'
        f"{metadata_xml}"
        "</metadata>"
        f"<manifest>{manifest_xml}</manifest>"
        f"<spine>{spine_xml}</spine></package>"
    )


def _corrupt_zip_entry_crc(zip_path: Path, entry_name: str) -> None:
    """Flip a payload byte of ``entry_name`` in a ZIP so reading it fails CRC check."""
    data = bytearray(zip_path.read_bytes())
    with zipfile.ZipFile(zip_path, "r") as zf:
        info = zf.getinfo(entry_name)
        header_offset = info.header_offset
    name_len = int.from_bytes(data[header_offset + 26 : header_offset + 28], "little")
    extra_len = int.from_bytes(data[header_offset + 28 : header_offset + 30], "little")
    payload_offset = header_offset + 30 + name_len + extra_len
    data[payload_offset] ^= 0xFF
    zip_path.write_bytes(bytes(data))


class TestExtendedMetadataAndCover:
    def test_title_first_non_empty_and_multi_author_roles(self, tmp_path: Path) -> None:
        opf = custom_opf(
            """
            <dc:title>   </dc:title>
            <dc:title>Tựa chính</dc:title>
            <dc:title>Tựa phụ không ghi đè</dc:title>
            <meta refines="#c-pre" property="role">aut</meta>
            <dc:creator id="c-pre">  Nguyễn   Nhật Ánh  </dc:creator>
            <dc:creator opf:role="AUT">Tác giả Hai</dc:creator>
            <dc:creator role="aut">Tác giả Ba</dc:creator>
            <dc:creator>Tác giả Không Vai Trò</dc:creator>
            <dc:creator opf:role="">Tác giả Rỗng Vai Trò</dc:creator>
            <dc:creator opf:role="trl">Dịch giả Bị Lọc</dc:creator>
            <dc:creator opf:role="ill">Họa sĩ Bị Lọc</dc:creator>
            <dc:creator id="c-edt">Biên tập Bị Lọc</dc:creator>
            <meta refines="#c-edt" property="role">edt</meta>
            <dc:creator id="c-multi">Vừa Vẽ Vừa Viết</dc:creator>
            <meta refines="#c-multi" property="role">ill</meta>
            <meta refines="#c-multi" property="role">AUT</meta>
            <dc:creator>Nguyễn Nhật Ánh</dc:creator>
            <dc:creator>   </dc:creator>
            """
        )
        path = write_epub(
            tmp_path / "authors.epub",
            {"content.opf": opf, "a.xhtml": chapter_xhtml("Ch1", ["Body."])},
        )
        book = import_epub(path)
        assert book.title == "Tựa chính"
        assert book.authors == (
            "Nguyễn Nhật Ánh",
            "Tác giả Hai",
            "Tác giả Ba",
            "Tác giả Không Vai Trò",
            "Tác giả Rỗng Vai Trò",
            "Vừa Vẽ Vừa Viết",
        )
        assert book.author == "; ".join(book.authors)

        # Only non-aut creators -> empty authors/author
        opf_no_aut = custom_opf(
            """
            <dc:title>Only Translator</dc:title>
            <dc:creator opf:role="trl">Dịch giả</dc:creator>
            <dc:creator id="c1">Họa sĩ</dc:creator>
            <meta refines="#c1" property="role">ill</meta>
            """
        )
        path2 = write_epub(
            tmp_path / "no_aut.epub",
            {"content.opf": opf_no_aut, "a.xhtml": chapter_xhtml("Ch1", ["Body."])},
        )
        book2 = import_epub(path2)
        assert book2.authors == ()
        assert book2.author == ""

    def test_language_normalization(self, tmp_path: Path) -> None:
        cases = [
            ("<dc:language>  </dc:language><dc:language>VI</dc:language>", "vi"),
            ("<dc:language>en</dc:language>", "en"),
            ("<dc:language>en_US</dc:language>", "en-US"),
            ("<dc:language>en-us</dc:language>", "en-US"),
            ("<dc:language>zh_hans_cn</dc:language>", "zh-Hans-CN"),
            ("<dc:language>zh-HANT-tw</dc:language>", "zh-Hant-TW"),
            ("<dc:language>es_419</dc:language>", "es-419"),
            ("<dc:language>en-US-x-Custom</dc:language>", "en-US-x-custom"),
            ("<dc:language>123</dc:language>", ""),
            ("<dc:language>x</dc:language>", ""),
            ("<dc:language>en/US</dc:language>", ""),
            ("<dc:language>   </dc:language>", ""),
            ("", ""),
        ]
        for idx, (lang_xml, expected) in enumerate(cases):
            opf = custom_opf(f"<dc:title>L{idx}</dc:title>{lang_xml}")
            path = write_epub(
                tmp_path / f"lang_{idx}.epub",
                {"content.opf": opf, "a.xhtml": chapter_xhtml("C", ["Body."])},
            )
            assert extract_epub_metadata(path).language == expected, f"failed case {lang_xml!r}"

    def test_isbn_extraction(self, tmp_path: Path) -> None:
        cases = [
            (
                '<dc:identifier opf:scheme="ISBN">978-0-13-110362-7</dc:identifier>',
                "9780131103627",
            ),
            (
                '<dc:identifier scheme="isbn">0-201-53082-x</dc:identifier>',
                "020153082X",
            ),
            (
                "<dc:identifier>urn:isbn:978 604 1 12345 6</dc:identifier>",
                "9786041123456",
            ),
            (
                "<dc:identifier>ISBN: 0-306-40615-2</dc:identifier>",
                "0306406152",
            ),
            (
                '<dc:identifier id="i1">978-3-16-148410-0</dc:identifier>'
                '<meta refines="#i1" property="identifier-type" scheme="onix:codelist5">15</meta>',
                "9783161484100",
            ),
            (
                '<dc:identifier id="i2">0-306-40615-x</dc:identifier>'
                '<meta refines="#i2" property="identifier-type">02</meta>',
                "030640615X",
            ),
            (
                '<dc:identifier id="i3">9780131103627</dc:identifier>'
                '<meta refines="#i3" property="identifier-type">ISBN</meta>',
                "9780131103627",
            ),
            # Invalid recognized ISBN skipped; falls through to valid second ISBN
            (
                '<dc:identifier opf:scheme="ISBN">not-valid</dc:identifier>'
                "<dc:identifier>urn:isbn:978-0-13-110362-X</dc:identifier>"
                "<dc:identifier>urn:isbn:978-0-13-110362-7</dc:identifier>",
                "9780131103627",
            ),
            # Unrecognized identifiers (UUID or bare 13 digits without scheme/prefix/refines) -> ""
            (
                "<dc:identifier>urn:uuid:12345678-1234-1234-1234-123456789012</dc:identifier>"
                "<dc:identifier>9780131103627</dc:identifier>",
                "",
            ),
        ]
        for idx, (id_xml, expected) in enumerate(cases):
            opf = custom_opf(f"<dc:title>I{idx}</dc:title>{id_xml}")
            path = write_epub(
                tmp_path / f"isbn_{idx}.epub",
                {"content.opf": opf, "a.xhtml": chapter_xhtml("C", ["Body."])},
            )
            assert extract_epub_metadata(path).isbn == expected, f"failed case {id_xml!r}"

    def test_year_and_description_extraction(self, tmp_path: Path) -> None:
        # Prefers publication over unset over other
        opf_pub = custom_opf(
            """
            <dc:title>Dates</dc:title>
            <dc:date opf:event="modification">2025-01-01</dc:date>
            <dc:date>2021-06-10</dc:date>
            <dc:date opf:event="PUBLICATION">2019-11-20T00:00:00Z</dc:date>
            <dc:description>   &lt;p&gt;&lt;/p&gt;  </dc:description>
            <dc:description>
                &lt;p&gt;Tóm tắt &lt;em&gt;cuốn sách&lt;/em&gt; &amp;amp; ghi chú.&lt;/p&gt;
                &lt;p&gt;Dòng&amp;nbsp;hai&lt;br/&gt;Dòng ba &amp;mdash; hết.&lt;/p&gt;
                &lt;script&gt;ignored()&lt;/script&gt;
            </dc:description>
            <dc:description>Second description ignored</dc:description>
            """
        )
        path = write_epub(
            tmp_path / "pub.epub",
            {"content.opf": opf_pub, "a.xhtml": chapter_xhtml("C", ["Body."])},
        )
        book = import_epub(path)
        assert book.year == 2019
        assert book.description == "Tóm tắt cuốn sách & ghi chú. Dòng hai Dòng ba — hết."

        # Unset preferred over modification; invalid publication date falls through
        opf_unset = custom_opf(
            """
            <dc:title>Unset</dc:title>
            <dc:date opf:event="publication">unknown</dc:date>
            <dc:date opf:event="creation">2010</dc:date>
            <dc:date>2018-03-04</dc:date>
            """
        )
        path_unset = write_epub(
            tmp_path / "unset.epub",
            {"content.opf": opf_unset, "a.xhtml": chapter_xhtml("C", ["Body."])},
        )
        assert extract_epub_metadata(path_unset).year == 2018

        # Falls back to other event when neither publication nor unset exists; ignores out-of-range
        opf_other = custom_opf(
            """
            <dc:title>Other</dc:title>
            <dc:date opf:event="creation">0999-01-01</dc:date>
            <dc:date opf:event="creation">3000-01-01</dc:date>
            <dc:date opf:event="creation">12345</dc:date>
            <dc:date opf:event="modification">1998-07-01</dc:date>
            """
        )
        path_other = write_epub(
            tmp_path / "other.epub",
            {"content.opf": opf_other, "a.xhtml": chapter_xhtml("C", ["Body."])},
        )
        assert extract_epub_metadata(path_other).year == 1998

        # No valid year at all -> None; nested XML inside dc:description still extracts cleanly
        opf_none = custom_opf(
            "<dc:title>NoYear</dc:title><dc:date>999</dc:date>"
            "<dc:description><p>Một</p><p>Hai</p></dc:description>"
        )
        path_none = write_epub(
            tmp_path / "noyear.epub",
            {"content.opf": opf_none, "a.xhtml": chapter_xhtml("C", ["Body."])},
        )
        meta_none = extract_epub_metadata(path_none)
        assert meta_none.year is None
        assert meta_none.description == "Một Hai"

    def test_cover_extraction_epub3_epub2_and_magic_sniffing(self, tmp_path: Path) -> None:
        # EPUB 3 cover-image (multi-token properties, mismatched extension sniffed by magic bytes)
        for fmt_name, payload, expected_ext in [
            ("jpg", _JPEG_BYTES, "jpg"),
            ("png", _PNG_BYTES, "png"),
            ("gif", _GIF_BYTES, "gif"),
            ("webp", _WEBP_BYTES, "webp"),
        ]:
            opf = custom_opf(
                "<dc:title>Cover3</dc:title>",
                manifest_xml=(
                    '<item id="cov" href="images/cover.bin" media-type="image/jpeg" '
                    'properties="svg cover-image"/>'
                    '<item id="c0" href="text/a.xhtml" media-type="application/xhtml+xml"/>'
                ),
            )
            path = write_epub(
                tmp_path / f"cov3_{fmt_name}.epub",
                {
                    "OEBPS/content.opf": opf,
                    "OEBPS/images/cover.bin": payload,
                    "OEBPS/text/a.xhtml": chapter_xhtml("C", ["Body."]),
                },
                opf_path="OEBPS/content.opf",
            )
            book = import_epub(path)
            assert book.cover_bytes == payload
            assert book.cover_ext == expected_ext

        # EPUB 2 <meta name="cover" content="<id>"/> (case-insensitive value) with relative path
        opf2 = custom_opf(
            '<dc:title>Cover2</dc:title><meta name="Cover" content="img-cover"/>',
            manifest_xml=(
                '<item id="img-cover" href="../images/my%20cover.png" media-type="image/png"/>'
                '<item id="c0" href="a.xhtml" media-type="application/xhtml+xml"/>'
            ),
            version="2.0",
        )
        path2 = write_epub(
            tmp_path / "cov2.epub",
            {
                "OEBPS/meta/content.opf": opf2,
                "OEBPS/images/my cover.png": _PNG_BYTES,
                "OEBPS/meta/a.xhtml": chapter_xhtml("C", ["Body."]),
            },
            opf_path="OEBPS/meta/content.opf",
        )
        book2 = import_epub(path2)
        assert book2.cover_bytes == _PNG_BYTES
        assert book2.cover_ext == "png"

        # EPUB 3 takes precedence over EPUB 2 when both are declared
        opf_both = custom_opf(
            '<dc:title>Both</dc:title><meta name="cover" content="cov2"/>',
            manifest_xml=(
                '<item id="cov2" href="c2.jpg" media-type="image/jpeg"/>'
                '<item id="cov3" href="c3.png" media-type="image/png" properties="cover-image"/>'
                '<item id="c0" href="a.xhtml" media-type="application/xhtml+xml"/>'
            ),
        )
        path_both = write_epub(
            tmp_path / "cov_both.epub",
            {
                "content.opf": opf_both,
                "c2.jpg": _JPEG_BYTES,
                "c3.png": _PNG_BYTES,
                "a.xhtml": chapter_xhtml("C", ["Body."]),
            },
        )
        meta_both = extract_epub_metadata(path_both)
        assert meta_both.cover_bytes == _PNG_BYTES
        assert meta_both.cover_ext == "png"

    def test_cover_edge_cases_silently_ignored(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Remote URIs (http, https, data, ftp) -> ignored silently
        for idx, remote_href in enumerate(
            [
                "https://example.com/cover.jpg",
                "http://example.com/cover.png",
                "data:image/png;base64,iVBORw0KGgo=",
                "ftp://example.com/cover.jpg",
            ]
        ):
            opf = custom_opf(
                f"<dc:title>Remote{idx}</dc:title>",
                manifest_xml=(
                    f'<item id="cov" href="{remote_href}" media-type="image/png" '
                    'properties="cover-image"/>'
                    '<item id="c0" href="a.xhtml" media-type="application/xhtml+xml"/>'
                ),
            )
            path = write_epub(
                tmp_path / f"remote_{idx}.epub",
                {"content.opf": opf, "a.xhtml": chapter_xhtml("C", ["Body."])},
            )
            book = import_epub(path)
            assert book.cover_bytes is None
            assert book.cover_ext == ""

        # Missing zip entry and unrecognized magic bytes -> ignored silently
        for idx, extra_files in enumerate(
            [
                {},  # cover entry missing from zip
                {"cover.svg": b"<svg xmlns='http://www.w3.org/2000/svg'/>"},
            ]
        ):
            opf = custom_opf(
                f"<dc:title>Bad{idx}</dc:title>",
                manifest_xml=(
                    '<item id="cov" href="cover.svg" media-type="image/svg+xml" '
                    'properties="cover-image"/>'
                    '<item id="c0" href="a.xhtml" media-type="application/xhtml+xml"/>'
                ),
            )
            files: dict[str, str | bytes] = {
                "content.opf": opf,
                "a.xhtml": chapter_xhtml("C", ["Body."]),
                **extra_files,
            }
            path = write_epub(tmp_path / f"bad_cov_{idx}.epub", files)
            book = import_epub(path)
            assert book.cover_bytes is None
            assert book.cover_ext == ""

        # Oversize cover (> MAX_COVER_BYTES) -> rejected via zip file_size before reading
        oversize_path = tmp_path / "oversize_cover.epub"
        opf_over = custom_opf(
            "<dc:title>BigCover</dc:title>",
            manifest_xml=(
                '<item id="cov" href="big.jpg" media-type="image/jpeg" properties="cover-image"/>'
                '<item id="c0" href="a.xhtml" media-type="application/xhtml+xml"/>'
            ),
        )
        with zipfile.ZipFile(oversize_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            zf.writestr("META-INF/container.xml", CONTAINER_XML.format(opf="content.opf"))
            zf.writestr("content.opf", opf_over)
            zf.writestr("a.xhtml", chapter_xhtml("C", ["Body."]))
            zf.writestr("big.jpg", _JPEG_BYTES + b"\x00" * (MAX_COVER_BYTES + 1 - len(_JPEG_BYTES)))
        opened_names: list[str] = []
        orig_open = zipfile.ZipFile.open

        def _spy_open(self: zipfile.ZipFile, name: str | zipfile.ZipInfo, *args, **kwargs):
            entry = name.filename if isinstance(name, zipfile.ZipInfo) else name
            opened_names.append(entry)
            return orig_open(self, name, *args, **kwargs)

        monkeypatch.setattr(zipfile.ZipFile, "open", _spy_open)
        book_over = import_epub(oversize_path)
        monkeypatch.undo()
        assert book_over.cover_bytes is None
        assert book_over.cover_ext == ""
        assert "big.jpg" not in opened_names

        # Corrupt ZIP entry for cover (bad CRC) -> ignored silently, book still imports
        corrupt_path = write_epub(
            tmp_path / "corrupt_cover.epub",
            {
                "content.opf": custom_opf(
                    "<dc:title>CorruptCover</dc:title>",
                    manifest_xml=(
                        '<item id="cov" href="cover.png" media-type="image/png" '
                        'properties="cover-image"/>'
                        '<item id="c0" href="a.xhtml" media-type="application/xhtml+xml"/>'
                    ),
                ),
                "cover.png": _PNG_BYTES + b"\x00" * 64,
                "a.xhtml": chapter_xhtml("C", ["Body."]),
            },
        )
        _corrupt_zip_entry_crc(corrupt_path, "cover.png")
        book_corrupt = import_epub(corrupt_path)
        assert book_corrupt.title == "CorruptCover"
        assert book_corrupt.cover_bytes is None
        assert book_corrupt.cover_ext == ""

    def test_extract_epub_metadata_skips_chapters_and_validates_errors(
        self, tmp_path: Path
    ) -> None:
        # Chapter XHTML has corrupt ZIP CRC and missing second chapter — extract_epub_metadata
        # MUST succeed without reading or parsing any chapter XHTML.
        opf = custom_opf(
            """
            <dc:title></dc:title>
            <dc:creator>Tác Giả</dc:creator>
            <dc:language>vi_VN</dc:language>
            <dc:identifier>urn:isbn:978-604-1-12345-6</dc:identifier>
            <dc:date opf:event="publication">2023-08-15</dc:date>
            <dc:description>&lt;p&gt;Mô tả ngắn.&lt;/p&gt;</dc:description>
            """,
            manifest_xml=(
                '<item id="cov" href="cover.webp" media-type="image/webp" '
                'properties="cover-image"/>'
                '<item id="c0" href="corrupt.xhtml" media-type="application/xhtml+xml"/>'
                '<item id="c1" href="missing.xhtml" media-type="application/xhtml+xml"/>'
            ),
            spine_xml='<itemref idref="c0"/><itemref idref="c1"/>',
        )
        path = write_epub(
            tmp_path / "Sách Cũ.epub",
            {
                "content.opf": opf,
                "cover.webp": _WEBP_BYTES,
                "corrupt.xhtml": b"x" * 128,
            },
        )
        _corrupt_zip_entry_crc(path, "corrupt.xhtml")

        meta = extract_epub_metadata(path)
        assert meta == EpubMetadata(
            title="Sách Cũ",  # fallback to path.stem
            author="Tác Giả",
            authors=("Tác Giả",),
            language="vi-VN",
            isbn="9786041123456",
            year=2023,
            description="Mô tả ngắn.",
            cover_bytes=_WEBP_BYTES,
            cover_ext="webp",
        )

        # Validation errors match import_epub
        with pytest.raises(FileNotFoundError):
            extract_epub_metadata(tmp_path / "missing.epub")
        with pytest.raises(FileNotFoundError):
            extract_epub_metadata(tmp_path)

        txt = tmp_path / "not_epub.txt"
        txt.write_text("hi", encoding="utf-8")
        with pytest.raises(DocumentImportError, match="epub"):
            extract_epub_metadata(txt)

        bad_zip = tmp_path / "bad.epub"
        bad_zip.write_bytes(b"not a zip")
        with pytest.raises(DocumentImportError, match="not a valid EPUB"):
            extract_epub_metadata(bad_zip)

        drm_path = write_epub(
            tmp_path / "drm_meta.epub",
            {
                "META-INF/encryption.xml": "<encryption/>",
                "content.opf": minimal_opf(["a.xhtml"]),
            },
        )
        with pytest.raises(DocumentImportError) as excinfo:
            extract_epub_metadata(drm_path)
        assert str(excinfo.value) == DRM_MESSAGE

        no_container = write_epub(
            tmp_path / "nocontainer_meta.epub",
            {"content.opf": minimal_opf(["a.xhtml"])},
            with_container=False,
        )
        with pytest.raises(DocumentImportError, match="container"):
            extract_epub_metadata(no_container)

        bad_opf = write_epub(tmp_path / "badopf_meta.epub", {"content.opf": "<package>"})
        with pytest.raises(DocumentImportError, match="OPF"):
            extract_epub_metadata(bad_opf)
