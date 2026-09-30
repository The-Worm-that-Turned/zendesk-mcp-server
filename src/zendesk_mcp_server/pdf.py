"""
Turn a PDF attachment into something a model can read.

Most PDFs on tickets (order confirmations, invoices) carry a text layer, which
is extracted per page. Pages without one (scanned PODs, photos saved as PDF)
are rendered to PNG instead, so they can still be read visually.
"""
import io
from dataclasses import dataclass

import pypdfium2 as pdfium

MAX_PAGES = 20
# A page with less text than this is treated as scanned and rendered.
MIN_TEXT_CHARS = 20
# Long edge of rendered pages. Larger images are downscaled by the model anyway.
RENDER_LONG_EDGE_PX = 1568


@dataclass
class PdfPage:
    number: int
    text: str
    png: bytes | None = None


@dataclass
class PdfContent:
    page_count: int
    pages: list[PdfPage]

    @property
    def truncated(self) -> bool:
        return self.page_count > len(self.pages)


def read_pdf(data: bytes, render_pages: bool = False) -> PdfContent:
    """
    Extract each page's text, rendering pages that have none (or every page
    when ``render_pages`` is set). Only the first MAX_PAGES pages are read.
    """
    try:
        document = pdfium.PdfDocument(data)
    except pdfium.PdfiumError as e:
        raise ValueError(f"Could not read PDF (it may be damaged or password-protected): {e}")

    try:
        pages = []
        for index in range(min(len(document), MAX_PAGES)):
            page = document[index]
            text = page.get_textpage().get_text_bounded().strip()
            png = None
            if render_pages or len(text) < MIN_TEXT_CHARS:
                png = _render(page)
            pages.append(PdfPage(number=index + 1, text=text, png=png))
        return PdfContent(page_count=len(document), pages=pages)
    finally:
        document.close()


def _render(page) -> bytes:
    width, height = page.get_size()
    scale = RENDER_LONG_EDGE_PX / max(width, height)
    image = page.render(scale=scale).to_pil()
    buffer = io.BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()
