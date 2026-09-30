import pytest

from pdf_samples import scanned_pdf, text_pdf
from zendesk_mcp_server import pdf
from zendesk_mcp_server.pdf import read_pdf

PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


def test_text_pages_are_extracted_without_rendering():
    content = read_pdf(text_pdf("SO 695667 Ship to Elstead Garden Centre", "Line 1 Tall Planter Grey x 2"))

    assert content.page_count == 2
    assert [p.text for p in content.pages] == [
        "SO 695667 Ship to Elstead Garden Centre",
        "Line 1 Tall Planter Grey x 2",
    ]
    assert all(p.png is None for p in content.pages)
    assert not content.truncated


def test_pages_without_text_are_rendered():
    content = read_pdf(scanned_pdf())

    page = content.pages[0]
    assert page.text == ""
    assert page.png.startswith(PNG_MAGIC)


def test_render_pages_renders_text_pages_too():
    content = read_pdf(text_pdf("SO 695667 Ship to Elstead Garden Centre"), render_pages=True)

    page = content.pages[0]
    assert page.text.startswith("SO 695667")
    assert page.png.startswith(PNG_MAGIC)


def test_only_the_first_pages_are_read(monkeypatch):
    monkeypatch.setattr(pdf, "MAX_PAGES", 2)

    content = read_pdf(text_pdf(*[f"Page number {n} of the order" for n in range(1, 6)]))

    assert content.page_count == 5
    assert [p.number for p in content.pages] == [1, 2]
    assert content.truncated


def test_unreadable_pdf_raises_a_clear_error():
    with pytest.raises(ValueError, match="Could not read PDF"):
        read_pdf(b"%PDF-1.4\nnot really a pdf")
