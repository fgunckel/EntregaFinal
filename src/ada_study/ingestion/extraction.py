"""Extracción de texto con pypdf y chunking determinista (specs/02 §25-26).

- `extract_pages`: extrae texto por página; detecta PDFs escaneados (sin texto
  extraíble) y lanza `ScannedPdfError` para no publicarlos (decisión aprobada
  en TASK-02: pypdf detecta escaneados y no se publican).
- `chunk_pages`: divide el texto en fragmentos deterministas con procedencia
  completa: `document_id`, `version_id`, `chunk_id`, `page`, `chunk_index`,
  `content_hash`, título y fuente (specs/02 §18).
"""

import hashlib
from io import BytesIO

from pypdf import PdfReader

from ada_study.contracts.documents import ChunkRecord

# Umbral de texto extraíble por página para considerar que el PDF tiene texto
# seleccionable (specs/02 §57: si no se extrae texto suficiente, failed).
_MIN_CHARS_PER_PAGE = 20


class ScannedPdfError(Exception):
    """PDF sin texto extraíble (escaneado); no se publica (decisión TASK-02)."""

    def __init__(self) -> None:
        super().__init__("El PDF no tiene texto extraíble (probablemente escaneado)")


def extract_pages(content: bytes) -> list[tuple[int, str]]:
    """Extrae el texto de cada página; devuelve [(page, text)].

    Lanza `ScannedPdfError` si ninguna página tiene texto suficiente
    (PDF escaneado) y `ValueError` si el contenido no es un PDF válido.
    """
    if not content:
        raise ValueError("El contenido del PDF está vacío")
    try:
        reader = PdfReader(BytesIO(content))
        pages: list[tuple[int, str]] = []
        for page_number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
            pages.append((page_number, text.strip()))
    except ScannedPdfError:
        raise
    except Exception as exc:
        raise ValueError(f"El contenido no es un PDF válido: {exc}") from exc
    if not pages:
        raise ScannedPdfError()
    if all(len(text) < _MIN_CHARS_PER_PAGE for _, text in pages):
        raise ScannedPdfError()
    return pages


def chunk_pages(
    pages: list[tuple[int, str]],
    *,
    document_id: str,
    version_id: str,
    title: str,
    source: str,
    max_chars: int = 1200,
) -> list[ChunkRecord]:
    """Divide el texto de las páginas en chunks deterministas.

    - Un chunk no cruza páginas (procedencia de página exacta, specs/02 §26).
    - `chunk_index` se numera por página (0-based dentro de la página).
    - `chunk_id` es estable: derivado de la procedencia (document_id,
      version_id, page, chunk_index) — repetir la extracción produce los
      mismos IDs (idempotencia, specs/02 §29).
    - `content_hash` es la huella SHA-256 del texto del fragmento.
    """
    chunks: list[ChunkRecord] = []
    for page, text in pages:
        if not text:
            continue
        pieces = _split_text(text, max_chars)
        for chunk_index, piece in enumerate(pieces):
            chunk_id = hashlib.sha256(
                f"{document_id}:{version_id}:{page}:{chunk_index}".encode()
            ).hexdigest()
            chunks.append(
                ChunkRecord(
                    chunk_id=chunk_id,
                    document_id=document_id,
                    version_id=version_id,
                    page=page,
                    chunk_index=chunk_index,
                    content_hash=hashlib.sha256(piece.encode("utf-8")).hexdigest(),
                    text=piece,
                    title=title,
                    source=source,
                )
            )
    return chunks


def _split_text(text: str, max_chars: int) -> list[str]:
    """Divide el texto en piezas de a lo sumo `max_chars`, por párrafos."""
    if len(text) <= max_chars:
        return [text]
    paragraphs = text.split("\n")
    pieces: list[str] = []
    current: list[str] = []
    current_len = 0
    for paragraph in paragraphs:
        # Un párrafo más largo que max_chars se corta por longitud fija.
        while len(paragraph) > max_chars:
            if current:
                pieces.append("\n".join(current))
                current = []
                current_len = 0
            pieces.append(paragraph[:max_chars])
            paragraph = paragraph[max_chars:]
        if current_len + len(paragraph) + 1 > max_chars and current:
            pieces.append("\n".join(current))
            current = []
            current_len = 0
        current.append(paragraph)
        current_len += len(paragraph) + 1
    if current:
        pieces.append("\n".join(current))
    return pieces
