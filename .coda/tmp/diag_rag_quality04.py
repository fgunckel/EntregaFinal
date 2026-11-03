"""RAG-QUALITY-04: diagnóstico READ-ONLY de recuperación (problema A).

Solo SELECT sobre PostgreSQL + reconstrucción EN MEMORIA del índice BM25
(idéntica a `study/runtime.py`). No escribe en la base, no toca índices,
no llama a LLM y no imprime secretos (solo nombres de provider/model).
"""

import asyncio
import os
import re
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from types import SimpleNamespace

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ada_study.contracts.documents import ChunkRecord
from ada_study.graph.researcher import (
    _is_discourse_marker,
    _normalize,
    _split_thematic_terms,
    _term_matches,
    build_retrieval_query,
)
from ada_study.index.bm25_index import BM25Index, significant_terms, tokenize
from ada_study.index.retriever import HybridRetriever, ViewerContext

MAIN_QUERY = "información de matemática que tengas"
CONTROL_QUERIES = ["Matemática", "¿Qué información contiene el material de Matemática?"]


def effective_llm_config() -> None:
    env = {}
    if os.path.exists(".env"):
        for line in open(".env", encoding="utf-8"):
            m = re.match(r"^([A-Z_]+)=(.*)$", line.strip())
            if m:
                env[m.group(1)] = m.group(2).strip().strip('"').strip("'")
    print("=== CONFIG LLM EFECTIVA (sin secretos) ===")
    print(f"ADA_LLM_PROVIDER = {env.get('ADA_LLM_PROVIDER', '<no definido>')}")
    print(f"ADA_LLM_MODEL    = {env.get('ADA_LLM_MODEL', '<no definido>')}")
    print(f"ADA_LLM_API_KEY presente: {bool(env.get('ADA_LLM_API_KEY'))}")
    print(f"GROQ_API_KEY presente:    {bool(env.get('GROQ_API_KEY'))}")
    print(f"ADA_CONVERSATIONS_ENABLED = {env.get('ADA_CONVERSATIONS_ENABLED', '<no definido>')}")
    print()


async def main() -> None:
    effective_llm_config()
    url = re.search(r"^DATABASE_URL=(.+)$", open(".env", encoding="utf-8").read(), re.M)
    url = url.group(1).strip().strip('"').strip("'")
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url)

    async with engine.connect() as conn:
        print("=== DOCUMENTOS (todos) ===")
        docs = (
            (
                await conn.execute(
                    text(
                        "SELECT document_id, filename, subject, active_version_id, "
                        "editorial_status, review_status, uploaded_by, created_at, "
                        "updated_at FROM documents ORDER BY created_at"
                    )
                )
            )
            .mappings()
            .all()
        )
        for d in docs:
            print(
                f"- {d['filename']!r} | id={d['document_id']} | estado={d['editorial_status']} "
                f"| review={d['review_status']} | owner={d['uploaded_by']} "
                f"| activa={d['active_version_id']}"
            )
        print()

        target = [d for d in docs if "prueba" in d["filename"].lower()]
        if not target:
            print("NO HAY documento 'prueba.pdf'")
            return
        doc = target[0]
        active_vid = doc["active_version_id"]

        print(f"=== VERSIONES DE {doc['filename']!r} ===")
        all_vers = (
            (
                await conn.execute(
                    text(
                        "SELECT version_id, version_number, status, document_id, created_at "
                        "FROM document_versions ORDER BY created_at"
                    )
                )
            )
            .mappings()
            .all()
        )
        vers = [v for v in all_vers if v["document_id"] == doc["document_id"]]
        for v in vers:
            print(
                f"- v{v['version_number']} id={v['version_id']} estado={v['status']}"
                f"{'  <-- ACTIVA' if v['version_id'] == active_vid else ''}"
            )
        print()

        print("=== CHUNKS POR VERSIÓN ===")
        chunk_counts = (
            (
                await conn.execute(
                    text(
                        "SELECT version_id, count(*) AS n FROM document_chunks "
                        "WHERE version_id IN (SELECT version_id FROM document_versions "
                        "WHERE document_id = :did) GROUP BY version_id"
                    ),
                    {"did": doc["document_id"]},
                )
            )
            .mappings()
            .all()
        )
        for c in chunk_counts:
            print(f"- versión {c['version_id']}: {c['n']} chunks")
        print()

        print("=== OUTBOX (pending_index_ops) DE LA VERSIÓN ACTIVA ===")
        ops = (
            (
                await conn.execute(
                    text(
                        "SELECT destination, operation, status, attempts, "
                        "last_error_code FROM pending_index_ops WHERE version_id = :vid "
                        "ORDER BY created_at"
                    ),
                    {"vid": active_vid},
                )
            )
            .mappings()
            .all()
        )
        if not ops:
            print("- (sin operaciones registradas)")
        for o in ops:
            print(
                f"- {o['destination']}/{o['operation']} estado={o['status']} "
                f"intentos={o['attempts']} error={o['last_error_code']}"
            )
        print()

        # Cargar TODOS los chunks visibles como hace el runtime (en memoria).
        print("=== CORPUS VISIBLE PARA STUDY (réplica de _collect_visible_chunks) ===")
        chunks: list[ChunkRecord] = []
        per_doc = {}
        for d in docs:
            if d["editorial_status"] not in {"published", "published_pending_review"}:
                per_doc[d["filename"]] = (
                    f"EXCLUIDO del RAG compartido (estado={d['editorial_status']})"
                )
                continue
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT version_id, chunk_id, page, chunk_index, content_hash, "
                            "text, title, source FROM document_chunks WHERE version_id = :vid "
                            "ORDER BY page, chunk_index"
                        ),
                        {"vid": d["active_version_id"]},
                    )
                )
                .mappings()
                .all()
            )
            vstatus = (
                await conn.execute(
                    text("SELECT status FROM document_versions WHERE version_id = :vid"),
                    {"vid": d["active_version_id"]},
                )
            ).scalar()
            if vstatus != "available":
                per_doc[d["filename"]] = f"EXCLUIDO (versión={vstatus})"
                continue
            for r in rows:
                chunks.append(
                    ChunkRecord(
                        document_id=d["document_id"],
                        version_id=r["version_id"],
                        chunk_id=r["chunk_id"],
                        page=r["page"],
                        chunk_index=r["chunk_index"],
                        content_hash=r["content_hash"],
                        text=r["text"],
                        title=r["title"],
                        source=r["source"],
                    )
                )
            per_doc[d["filename"]] = f"incluido ({len(rows)} chunks)"
        for name, note in per_doc.items():
            print(f"- {name}: {note}")
        print(f"TOTAL chunks en BM25 (rebuild en memoria): {len(chunks)}")
        print()

        index = BM25Index.rebuild(chunks)
        print(f"BM25Index.chunk_count() = {index.chunk_count()}")
        print()

        print("=== CONTENIDO DE LOS CHUNKS DE prueba.pdf ===")
        for c in chunks:
            if c.version_id == active_vid:
                preview = " ".join(c.text.split())
                print(f"[{c.chunk_id}] p.{c.page} '{c.title}': {preview[:200]}")
        print()

        docs_by_id = {d["document_id"]: d for d in docs}
        vers_by_id = {v["version_id"]: v for v in all_vers}
        chunks_by_version = {}
        for c in chunks:
            chunks_by_version.setdefault(c.version_id, []).append(c)

        class MemCatalog:
            async def get_document(self, document_id):
                return SimpleNamespace(
                    uploaded_by=docs_by_id[document_id]["uploaded_by"],
                    active_version_id=docs_by_id[document_id]["active_version_id"],
                    editorial_status=docs_by_id[document_id]["editorial_status"],
                )

            async def get_version(self, version_id):
                return SimpleNamespace(status=vers_by_id[version_id]["status"])

            async def list_chunks(self, version_id):
                return chunks_by_version.get(version_id, [])

        viewer = ViewerContext(user_id=doc["uploaded_by"] or "viewer", role="student")
        retriever = HybridRetriever(
            catalog=MemCatalog(), bm25_index=index, vector_search=None, top_k=5, viewer=viewer
        )
        retriever.load_chunks(chunks)

        # Consulta de control 3: término más frecuente NO genérico de prueba.pdf
        term_freq: dict[str, int] = {}
        for t in significant_terms(" ".join(c.text for c in chunks if c.version_id == active_vid)):
            term_freq[t] = term_freq.get(t, 0) + 1
        topic_term = None
        for t, _f in sorted(term_freq.items(), key=lambda kv: -kv[1]):
            if not _is_discourse_marker(t) and index.document_frequency(t) < len(chunks) * 0.5:
                topic_term = t
                break
        print(f"=== CONTROL 3: término más frecuente no genérico = {topic_term!r} ===")
        print(f"df({topic_term!r}) = {index.document_frequency(topic_term)}/{len(chunks)}")
        print()

        queries = [MAIN_QUERY, *CONTROL_QUERIES, f"¿Qué es {topic_term}?"]
        for query in queries:
            print("=" * 70)
            print(f"CONSULTA: {query!r}")
            rq = build_retrieval_query(query, [])
            print(f"retrieval_query: {rq!r}")
            results = await retriever.search(rq, top_k=5)
            print(f"BM25+RRF devolvió {len(results)} resultados visibles:")
            for r in results:
                print(f"  rank={r.rank} score={r.score:.4f} [{r.chunk_id}] p.{r.page} '{r.title}'")

            target_terms = [t for t in significant_terms(rq) if not _is_discourse_marker(t)]
            print(f"términos significativos: {significant_terms(rq)}")
            print(f"términos objetivo (sin marcadores): {target_terms}")
            thematic, generic = _split_thematic_terms(
                target_terms, index.document_frequency, index.chunk_count()
            )
            print(f"genéricos por frecuencia (df>=3 y df/N>=0.5): {generic}")
            for t in target_terms:
                print(f"  df({t!r}) = {index.document_frequency(t)}/{index.chunk_count()}")
            print(f"términos temáticos: {thematic}")
            evidence_terms = thematic or target_terms
            required = min(2, len(evidence_terms))
            print(f"umbral por chunk: {required} coincidencias sobre {evidence_terms}")
            print("análisis por chunk:")
            accepted_ids = []
            for r in results:
                chunk_terms = frozenset(_normalize(tok) for tok in tokenize(f"{r.title} {r.text}"))
                matched = [t for t in evidence_terms if _term_matches(t, chunk_terms)]
                verdict = "ACEPTADO" if len(matched) >= required else "RECHAZADO"
                if verdict == "ACEPTADO":
                    accepted_ids.append(r.chunk_id)
                print(
                    f"  [{r.chunk_id}] p.{r.page} {verdict}: {matched} ({len(matched)}/{required})"
                )
            print(f"EVIDENCIA AL TUTOR: {len(accepted_ids)} -> {accepted_ids}")
            print()

    await engine.dispose()


asyncio.run(main())
