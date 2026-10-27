"""Pruebas de extracción pypdf y chunking determinista (TASK-03)."""

import pytest

from ada_study.ingestion.extraction import ScannedPdfError, chunk_pages, extract_pages


def _pdf_with_text(text: str) -> bytes:
    """Genera un PDF 1.4 mínimo con texto seleccionable y xref válida."""
    text_bytes = text.encode("latin-1", "replace")
    stream = b"BT /F1 12 Tf 72 720 Td (" + text_bytes + b") Tj ET\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >> stream\n" + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size " + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n" + str(xref_pos).encode() + b"\n%%EOF\n"
    )
    return bytes(out)


def test_extract_pages_returns_page_text():
    content = _pdf_with_text("hola mundo esto es texto suficiente")
    pages = extract_pages(content)
    assert len(pages) == 1
    assert pages[0][0] == 1
    assert "hola" in pages[0][1]


def test_extract_pages_rejects_empty_content():
    with pytest.raises(ValueError):
        extract_pages(b"")


def test_extract_pages_rejects_scanned_pdf():
    # PDF válido pero sin texto extraíble (páginas en blanco): escaneado
    blank = _pdf_with_text("")
    with pytest.raises(ScannedPdfError):
        extract_pages(blank)


def test_chunk_pages_provenance_is_complete():
    pages = [(1, "texto de la pagina uno"), (2, "texto de la pagina dos")]
    chunks = chunk_pages(
        pages,
        document_id="doc_1",
        version_id="ver_1",
        title="apuntes.pdf",
        source="apuntes.pdf",
    )
    assert len(chunks) == 2
    first = chunks[0]
    assert first.document_id == "doc_1"
    assert first.version_id == "ver_1"
    assert first.page == 1
    assert first.chunk_index == 0
    assert first.title == "apuntes.pdf"
    assert first.source == "apuntes.pdf"
    assert len(first.chunk_id) == 64
    assert len(first.content_hash) == 64


def test_chunk_pages_ids_are_deterministic():
    pages = [(1, "texto estable")]
    kwargs = {"document_id": "doc_1", "version_id": "ver_1", "title": "t", "source": "s"}
    first = chunk_pages(pages, **kwargs)
    second = chunk_pages(pages, **kwargs)
    assert [c.chunk_id for c in first] == [c.chunk_id for c in second]


def test_chunk_pages_splits_long_text():
    long_text = "palabra " * 400  # ~3200 chars
    chunks = chunk_pages(
        [(1, long_text)],
        document_id="doc_1",
        version_id="ver_1",
        title="t",
        source="s",
        max_chars=1200,
    )
    assert len(chunks) > 1
    assert all(len(c.text) <= 1200 for c in chunks)
    # chunk_index se numera dentro de la página
    assert [c.chunk_index for c in chunks] == list(range(len(chunks)))


def test_chunk_pages_skips_empty_pages():
    chunks = chunk_pages(
        [(1, ""), (2, "contenido real")],
        document_id="d",
        version_id="v",
        title="t",
        source="s",
    )
    assert len(chunks) == 1
    assert chunks[0].page == 2
