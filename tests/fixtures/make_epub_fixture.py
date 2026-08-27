"""Deterministic generator for ``sample.epub`` (audiobook import fixture).

Run manually to (re)generate the committed binary; tests read the binary and
never execute this script::

    .venv/bin/python tests/fixtures/make_epub_fixture.py

Structure (EPUB 3 flavor, spec-valid enough for real readers):

- ``mimetype`` first entry, STORED (uncompressed) per OCFS.
- ``META-INF/container.xml`` → ``OEBPS/content.opf``.
- OPF metadata: dc:title "Sách thử nghiệm", dc:creator "Tác Giả A".
- Spine order: cover.xhtml (image-only page), nav.xhtml (EPUB3 nav,
  ``properties="nav"``), then three content chapters:
  ch1 (h1 + two paragraphs), ch2 (h1 + Vietnamese paragraphs), ch3 (no
  heading → parser falls back to a generated chapter title).
- cover.xhtml references images/cover.png so it is a real image-only page.
"""

from __future__ import annotations

import zipfile
from pathlib import Path

CONTAINER_XML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">\n'
    "  <rootfiles>\n"
    '    <rootfile full-path="OEBPS/content.opf" '
    'media-type="application/oebps-package+xml"/>\n'
    "  </rootfiles>\n"
    "</container>\n"
)

COVER_XHTML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml">\n'
    "  <head><title>Cover</title></head>\n"
    "  <body>\n"
    '    <div class="cover"><img src="images/cover.png" alt="Bìa"/></div>\n'
    "  </body>\n"
    "</html>\n"
)

NAV_XHTML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">\n'
    "  <head><title>Navigation</title></head>\n"
    "  <body>\n"
    '    <nav epub:type="toc">\n'
    "      <h1>Mục lục</h1>\n"
    "      <ol>\n"
    '        <li><a href="ch1.xhtml">Chương một</a></li>\n'
    '        <li><a href="ch2.xhtml">Chương hai</a></li>\n'
    '        <li><a href="ch3.xhtml">Chương ba</a></li>\n'
    "      </ol>\n"
    "    </nav>\n"
    "  </body>\n"
    "</html>\n"
)

CH1_XHTML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml">\n'
    "  <head><title>Chapter One</title></head>\n"
    "  <body>\n"
    "    <h1>Chương một</h1>\n"
    "    <p>First paragraph of chapter one.</p>\n"
    "    <p>Second paragraph of chapter one.</p>\n"
    "  </body>\n"
    "</html>\n"
)

CH2_XHTML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml">\n'
    "  <head><title>Chapter Two</title></head>\n"
    "  <body>\n"
    "    <h1>Chương hai</h1>\n"
    "    <p>Đoạn văn tiếng Việt đầu tiên của chương hai.</p>\n"
    "    <p>Đoạn văn thứ hai — có dấu gạch ngang và dấu câu!</p>\n"
    "  </body>\n"
    "</html>\n"
)

CH3_XHTML = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<html xmlns="http://www.w3.org/1999/xhtml">\n'
    "  <head><title>Chapter Three</title></head>\n"
    "  <body>\n"
    "    <p>A chapter without any heading element at all.</p>\n"
    "    <p>The parser must generate a fallback title.</p>\n"
    "  </body>\n"
    "</html>\n"
)

CONTENT_OPF = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" '
    'unique-identifier="book-id">\n'
    '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
    '    <dc:identifier id="book-id">urn:uuid:fixture-epub-sample</dc:identifier>\n'
    "    <dc:title>Sách thử nghiệm</dc:title>\n"
    "    <dc:creator>Tác Giả A</dc:creator>\n"
    "    <dc:language>vi</dc:language>\n"
    "  </metadata>\n"
    "  <manifest>\n"
    '    <item id="css" href="style.css" media-type="text/css"/>\n'
    '    <item id="cover-image" href="images/cover.png" '
    'media-type="image/png"/>\n'
    '    <item id="cover" href="cover.xhtml" media-type="application/xhtml+xml"/>\n'
    '    <item id="nav" href="nav.xhtml" media-type="application/xhtml+xml" '
    'properties="nav"/>\n'
    '    <item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/>\n'
    '    <item id="ch2" href="ch2.xhtml" media-type="application/xhtml+xml"/>\n'
    '    <item id="ch3" href="ch3.xhtml" media-type="application/xhtml+xml"/>\n'
    "  </manifest>\n"
    "  <spine>\n"
    '    <itemref idref="cover"/>\n'
    '    <itemref idref="nav"/>\n'
    '    <itemref idref="ch1"/>\n'
    '    <itemref idref="ch2"/>\n'
    '    <itemref idref="ch3"/>\n'
    "  </spine>\n"
    "</package>\n"
)

# 1×1 transparent PNG (smallest valid fixture image).
COVER_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010806000000"
    "1f15c4890000000d49444154789c626001000000ffff030000060005"
    "57bfabd40000000049454e44ae426082"
)


def build_epub() -> bytes:
    """Assemble the fixture EPUB as raw bytes."""
    import io

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as zf:
        # EPUB spec: mimetype must be the FIRST entry and uncompressed.
        info = zipfile.ZipInfo("mimetype")
        info.compress_type = zipfile.ZIP_STORED
        zf.writestr(info, "application/epub+zip")
        zf.writestr("META-INF/container.xml", CONTAINER_XML)
        zf.writestr("OEBPS/content.opf", CONTENT_OPF)
        zf.writestr("OEBPS/style.css", "body { margin: 0; }\n")
        zf.writestr("OEBPS/cover.xhtml", COVER_XHTML)
        zf.writestr("OEBPS/nav.xhtml", NAV_XHTML)
        zf.writestr("OEBPS/ch1.xhtml", CH1_XHTML)
        zf.writestr("OEBPS/ch2.xhtml", CH2_XHTML)
        zf.writestr("OEBPS/ch3.xhtml", CH3_XHTML)
        zf.writestr("OEBPS/images/cover.png", COVER_PNG)
    return buffer.getvalue()


if __name__ == "__main__":
    target = Path(__file__).with_name("sample.epub")
    target.write_bytes(build_epub())
    print(f"wrote {target} ({target.stat().st_size} bytes)")
