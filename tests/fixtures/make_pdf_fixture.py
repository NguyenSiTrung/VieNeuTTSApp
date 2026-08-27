"""Deterministic generator for ``sample.pdf`` (two-page text fixture).

Run manually to (re)generate the committed binary; tests read the binary
and never execute this script::

    .venv/bin/python tests/fixtures/make_pdf_fixture.py

Why a script: pypdf cannot author text and hand-computed xref offsets are
brittle. This writes a minimal, spec-valid PDF with one classic content
stream per page and computes the cross-reference table programmatically.
Text is ASCII-safe on purpose (WinAnsi/Helvetica); Vietnamese coverage is
pinned by the .txt/.docx fixtures.
"""

from __future__ import annotations

from pathlib import Path

PAGE_LINES = ["PDF fixture page one.", "PDF fixture page two."]


def build_pdf(lines: list[str]) -> bytes:
    """Build a minimal N-page PDF, one single-line text Tj per page."""
    pages = len(lines)
    # Object layout: 1 Catalog, 2 Pages, 3 Font, then per page: Page + Contents.
    page_ids = [4 + 2 * i for i in range(pages)]
    content_ids = [5 + 2 * i for i in range(pages)]
    objects: list[bytes] = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Count "
        + str(pages).encode()
        + b" /Kids ["
        + b" ".join(f"{pid} 0 R".encode() for pid in page_ids)
        + b"] >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    for _pid, cid, line in zip(page_ids, content_ids, lines, strict=True):
        stream = f"BT /F1 12 Tf 72 720 Td ({line}) Tj ET".encode("ascii")
        objects.append(
            b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 3 0 R >> >> /Contents " + f"{cid} 0 R".encode() + b" >>"
        )
        objects.append(
            b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream"
        )

    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = [0]  # object numbers are 1-based; slot 0 unused
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += b"xref\n0 " + str(len(objects) + 1).encode() + b"\n"
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size "
        + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n"
        + str(xref_pos).encode()
        + b"\n%%EOF\n"
    )
    return bytes(out)


if __name__ == "__main__":
    target = Path(__file__).with_name("sample.pdf")
    target.write_bytes(build_pdf(PAGE_LINES))
    print(f"wrote {target} ({target.stat().st_size} bytes)")
