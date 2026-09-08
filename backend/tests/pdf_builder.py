"""A minimal PDF writer used to build test documents.

The suite needs PDFs whose text is known exactly, and it needs them for made-up
subjects that appear in no starter document, so that the extractor is tested on
material it cannot have been tuned to. Writing the bytes directly keeps the test
dependencies identical to the runtime ones.
"""

from typing import List

_ESCAPE = str.maketrans({"\\": r"\\", "(": r"\(", ")": r"\)"})


def _content_stream(lines: List[str], font_size: int = 11, leading: int = 15) -> bytes:
    body = ["BT", f"/F1 {font_size} Tf", f"{leading} TL", "72 760 Td"]
    for line in lines:
        body.append(f"({line.translate(_ESCAPE)}) Tj")
        body.append("T*")
    body.append("ET")
    return "\n".join(body).encode("latin-1", "replace")


def build_pdf(pages: List[List[str]]) -> bytes:
    """Build a PDF from a list of pages, each a list of text lines."""
    objects: List[bytes] = []

    def add(body: bytes) -> int:
        objects.append(body)
        return len(objects)

    font_id = add(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
    page_ids, content_ids = [], []
    for _ in pages:
        page_ids.append(None)

    pages_id = len(objects) + len(pages) * 2 + 1

    for lines in pages:
        stream = _content_stream(lines)
        content_ids.append(add(b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)))

    for index, content_id in enumerate(content_ids):
        page_ids[index] = add(
            b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 612 792] "
            b"/Resources << /Font << /F1 %d 0 R >> >> /Contents %d 0 R >>"
            % (pages_id, font_id, content_id)
        )

    kids = b" ".join(b"%d 0 R" % pid for pid in page_ids)
    actual_pages_id = add(b"<< /Type /Pages /Kids [%s] /Count %d >>" % (kids, len(page_ids)))
    catalog_id = add(b"<< /Type /Catalog /Pages %d 0 R >>" % actual_pages_id)

    # The page objects were written referencing pages_id, which must match.
    if actual_pages_id != pages_id:
        for index, page_id in enumerate(page_ids):
            objects[page_id - 1] = objects[page_id - 1].replace(
                b"/Parent %d 0 R" % pages_id, b"/Parent %d 0 R" % actual_pages_id)

    out = bytearray(b"%PDF-1.4\n")
    offsets = [0]
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"

    xref_at = len(out)
    out += b"xref\n0 %d\n" % (len(objects) + 1)
    out += b"0000000000 65535 f \n"
    for offset in offsets[1:]:
        out += b"%010d 00000 n \n" % offset
    out += (b"trailer\n<< /Size %d /Root %d 0 R >>\nstartxref\n%d\n%%%%EOF\n"
            % (len(objects) + 1, catalog_id, xref_at))
    return bytes(out)
