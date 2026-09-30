"""Tiny PDFs built in memory, so tests need no binary fixtures."""
import io

from PIL import Image


def text_pdf(*pages: str) -> bytes:
    """A PDF with one page per string, each carrying a real text layer."""
    font = 3 + 2 * len(pages)
    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = {}

    def obj(number, body):
        offsets[number] = out.tell()
        out.write(f"{number} 0 obj\n".encode() + body + b"\nendobj\n")

    kids = " ".join(f"{3 + 2 * i} 0 R" for i in range(len(pages)))
    obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    obj(2, f"<< /Type /Pages /Kids [{kids}] /Count {len(pages)} >>".encode())
    for i, text in enumerate(pages):
        page, contents = 3 + 2 * i, 4 + 2 * i
        stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
        obj(page, (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {contents} 0 R "
            f"/Resources << /Font << /F1 {font} 0 R >> >> >>"
        ).encode())
        obj(contents, f"<< /Length {len(stream)} >>\nstream\n".encode() + stream + b"\nendstream")
    obj(font, b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")

    xref = out.tell()
    out.write(f"xref\n0 {font + 1}\n0000000000 65535 f \n".encode())
    for number in range(1, font + 1):
        out.write(f"{offsets[number]:010d} 00000 n \n".encode())
    out.write(f"trailer\n<< /Size {font + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def scanned_pdf() -> bytes:
    """A single image-only page, like a scanned proof of delivery."""
    buffer = io.BytesIO()
    Image.new("RGB", (400, 560), "white").save(buffer, "PDF")
    return buffer.getvalue()
