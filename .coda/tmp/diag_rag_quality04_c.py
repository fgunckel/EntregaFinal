"""RAG-QUALITY-04 (hipótesis histórica): pertinencia con corpus = solo prueba.pdf.

Verifica si, con N=22, 'matemática' cae bajo la regla de genéricos
(df>=3 y df/N>=0.5) y la consulta 'información de matemática que tengas'
queda sin fragmentos aceptados (insufficient_evidence).
"""

import asyncio
import os
import re
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ada_study.contracts.documents import ChunkRecord
from ada_study.graph.researcher import (
    _is_discourse_marker,
    _normalize,
    _split_thematic_terms,
    _term_matches,
)
from ada_study.index.bm25_index import BM25Index, significant_terms, tokenize
from ada_study.index.retriever import HybridRetriever, ViewerContext

QUERY = "información de matemática que tengas"


async def main() -> None:
    url = re.search(r"^DATABASE_URL=(.+)$", open(".env", encoding="utf-8").read(), re.M)
    url = url.group(1).strip().strip('"').strip("'")
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        rows = (
            (
                await conn.execute(
                    text(
                        "SELECT c.version_id, c.chunk_id, c.page, c.chunk_index, c.content_hash, "
                        "c.text, c.title, c.source, d.document_id, d.uploaded_by "
                        "FROM document_chunks c JOIN documents d ON d.document_id = c.document_id "
                        "WHERE c.version_id = (SELECT active_version_id FROM documents "
                        "WHERE filename = 'prueba.pdf')"
                    )
                )
            )
            .mappings()
            .all()
        )
        owner = rows[0]["uploaded_by"]
        chunks = [
            ChunkRecord(
                document_id=r["document_id"],
                version_id=r["version_id"],
                chunk_id=r["chunk_id"],
                page=r["page"],
                chunk_index=r["chunk_index"],
                content_hash=r["content_hash"],
                text=r["text"],
                title=r["title"],
                source=r["source"],
            )
            for r in rows
        ]
    await engine.dispose()

    index = BM25Index.rebuild(chunks)
    n = index.chunk_count()
    print(f"N (solo prueba.pdf) = {n}")

    viewer = ViewerContext(user_id=owner, role="student")

    class MemCatalog:
        async def get_document(self, document_id):
            return SimpleNamespace(
                uploaded_by=owner,
                active_version_id=chunks[0].version_id,
                editorial_status="published",
            )

        async def get_version(self, version_id):
            return SimpleNamespace(status="available")

        async def list_chunks(self, version_id):
            return chunks

    retriever = HybridRetriever(
        catalog=MemCatalog(), bm25_index=index, vector_search=None, top_k=5, viewer=viewer
    )
    retriever.load_chunks(chunks)

    results = await retriever.search(QUERY, top_k=5)
    print(f"BM25 devolvió {len(results)} resultados:")
    for r in results:
        print(f"  [{r.chunk_id[:12]}] p.{r.page}")

    target_terms = [t for t in significant_terms(QUERY) if not _is_discourse_marker(t)]
    thematic, generic = _split_thematic_terms(
        target_terms, index.document_frequency, index.chunk_count()
    )
    print(f"términos objetivo: {target_terms}")
    print(f"genéricos con N=22: {generic}")
    for t in target_terms:
        df = index.document_frequency(t)
        print(f"  df({t!r}) = {df}/{n} = {df / n:.2f}")
    print(f"temáticos: {thematic}")
    evidence_terms = thematic or target_terms
    required = min(2, len(evidence_terms))
    print(f"umbral: {required} sobre {evidence_terms}")
    accepted = 0
    for r in results:
        chunk_terms = frozenset(_normalize(tok) for tok in tokenize(f"{r.title} {r.text}"))
        matched = [t for t in evidence_terms if _term_matches(t, chunk_terms)]
        ok = len(matched) >= required
        accepted += ok
        print(f"  [{r.chunk_id[:12]}] p.{r.page} {'ACEPTADO' if ok else 'RECHAZADO'}: {matched}")
    print(
        f"EVIDENCIA: {accepted} fragmentos -> "
        f"{'insufficient_evidence' if accepted == 0 else 'complete'}"
    )


asyncio.run(main())
