"""RAG-QUALITY-05 (diagnóstico §3): reproducción de la consulta fallida.

Carga el corpus visible COMPLETO (Historia + Matemática, como la prueba
funcional), reconstruye BM25 y ejecuta el pipeline actual del researcher con
la consulta exacta del caso, mostrando términos objetivo, clasificación
temático/genérico, coincidencias por fragmento y veredicto.
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
    _source_restriction_terms,
    _split_thematic_terms,
    _term_matches,
    build_retrieval_query,
)
from ada_study.index.bm25_index import BM25Index, significant_terms, tokenize
from ada_study.index.retriever import HybridRetriever, ViewerContext

QUERY = (
    "Explicame la fotosíntesis utilizando exclusivamente el material de "
    "Historia y Matemática que tenés disponible."
)


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
                        "c.text, c.title, c.source, d.document_id, d.filename, d.uploaded_by, "
                        "d.editorial_status, d.active_version_id "
                        "FROM document_chunks c JOIN documents d ON d.document_id = c.document_id "
                        "WHERE d.editorial_status IN ('published', 'published_pending_review') "
                        "AND c.version_id = d.active_version_id"
                    )
                )
            )
            .mappings()
            .all()
        )
    await engine.dispose()
    if not rows:
        print("SIN CORPUS EN BD: usar fallback sintético")
        return

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
    print(f"=== CORPUS: {len(chunks)} chunks de {len({c.document_id for c in chunks})} documentos ===")

    index = BM25Index.rebuild(chunks)
    n = index.chunk_count()
    viewer = ViewerContext(user_id=owner, role="student")

    class MemCatalog:
        async def get_document(self, document_id):
            row = next(r for r in rows if r["document_id"] == document_id)
            return SimpleNamespace(
                uploaded_by=row["uploaded_by"],
                active_version_id=row["active_version_id"],
                editorial_status=row["editorial_status"],
            )

        async def get_version(self, version_id):
            return SimpleNamespace(status="available")

        async def list_chunks(self, version_id):
            return [c for c in chunks if c.version_id == version_id]

    retriever = HybridRetriever(
        catalog=MemCatalog(), bm25_index=index, vector_search=None, top_k=5, viewer=viewer
    )
    retriever.load_chunks(chunks)

    retrieval_query = build_retrieval_query(QUERY, [])
    results = await retriever.search(retrieval_query, top_k=5)
    print(f"\n=== BM25 devolvió {len(results)} fragmentos ===")

    target_terms = [t for t in significant_terms(retrieval_query) if not _is_discourse_marker(t)]
    restriction = _source_restriction_terms(QUERY)
    print(f"\ntérminos objetivo (sin marcadores): {target_terms}")
    print(f"restricción de fuentes detectada: {sorted(restriction)}")
    print("\ndf por término (sobre la consulta completa):")
    for term in set(tokenize(retrieval_query)):
        df = index.document_frequency(term)
        print(f"  df({term!r}) = {df}/{n} = {df / n:.2f}")
    thematic, generic = _split_thematic_terms(
        [t for t in target_terms if _normalize(t) not in restriction],
        index.document_frequency,
        n,
    )
    print(f"\ngenéricos (df>=3 y ratio>=0.5): {generic}")
    print(f"temáticos (tras restricción): {thematic}")
    evidence_terms = thematic or [t for t in target_terms if _normalize(t) not in restriction] or target_terms
    required = min(2, len(evidence_terms))
    print(f"umbral exigido: {required} coincidencias sobre {evidence_terms}")

    accepted = 0
    for r in results:
        chunk_terms = frozenset(_normalize(tok) for tok in tokenize(f"{r.title} {r.text}"))
        matched = [t for t in evidence_terms if _term_matches(t, chunk_terms)]
        ok = len(matched) >= required
        accepted += ok
        print(f"\n  [{r.chunk_id[:12]}] p.{r.page} {'ACEPTADO' if ok else 'RECHAZADO'}")
        print(f"    coincidencias: {matched}")
        print(f"    texto: {r.text[:180]!r}")
    print(
        f"\n=== VEREDICTO: {accepted} aceptados -> "
        f"{'complete (LLM invocado)' if accepted else 'insufficient_evidence (sin LLM)'} ==="
    )


asyncio.run(main())
