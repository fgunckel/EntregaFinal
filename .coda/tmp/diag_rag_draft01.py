"""AUDIT RAG-DRAFT-01 — diagnóstico READ-ONLY (solo SELECTs, sin LLM, sin escrituras).

Simula EXACTAMENTE el flujo de estudio contra la base real:
1. Verifica chunks de Historia (doc_e156f7ba6e3f / ver_a096f844b901).
2. Reconstruye la colección visible de _collect_visible_chunks para el admin.
3. BM25 bajo nivel: (a) índice real del runtime (solo chunks visibles),
   (b) índice hipotético con TODOS los chunks (capa 1) + _is_visible real
   (capa 2) para mostrar motivos de exclusión.
Nunca imprime credenciales.
"""

import asyncio
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import asyncpg

from ada_study.contracts.documents import ChunkRecord, DocumentRecord, DocumentVersion
from ada_study.index.bm25_index import BM25Index
from ada_study.index.retriever import HybridRetriever, ViewerContext

QUERY = (
    "Según el documento Historia_5to_Grado_Guia_Completa.pdf, "
    "¿cuáles fueron las causas de la Revolución de Mayo?"
)
HISTORIA_DOC = "doc_e156f7ba6e3f"
HISTORIA_VER = "ver_a096f844b901"
ADMIN = "usr_86123da82670"


def _env_url() -> str:
    env = Path(".env").read_text(encoding="utf-8")
    raw = re.search(r"^DATABASE_URL=(.+)$", env, re.MULTILINE).group(1).strip()
    return raw.replace("postgresql+asyncpg://", "postgresql://")


class _MiniCatalog:
    """Vista de solo lectura del catálogo (mismo Protocol que CatalogView)."""

    def __init__(self, documents, versions):
        self._docs = {d.document_id: d for d in documents}
        self._vers = {v.version_id: v for v in versions}

    async def get_document(self, document_id):
        return self._docs[document_id]

    async def get_version(self, version_id):
        return self._vers[version_id]

    async def list_chunks(self, version_id):
        return []  # no se usa en el diagnóstico


def _iso(value):
    return value.isoformat() if value is not None else None


async def _load(conn):
    doc_rows = await conn.fetch("SELECT * FROM documents ORDER BY created_at")
    ver_rows = await conn.fetch("SELECT * FROM document_versions ORDER BY created_at")
    chunk_rows = await conn.fetch(
        "SELECT * FROM document_chunks ORDER BY version_id, page, chunk_index"
    )
    outbox = await conn.fetch(
        "SELECT version_id, destination, operation, status FROM pending_index_ops "
        "ORDER BY version_id, destination"
    )
    documents = [
        DocumentRecord(
            document_id=r["document_id"], filename=r["filename"], subject=r["subject"],
            active_version_id=r["active_version_id"],
            editorial_status=r["editorial_status"], review_status=r["review_status"],
            uploaded_by=r["uploaded_by"], created_at=_iso(r["created_at"]),
            updated_at=_iso(r["updated_at"]), deleted_at=_iso(r["deleted_at"]),
        )
        for r in doc_rows
    ]
    versions = [
        DocumentVersion(
            version_id=r["version_id"], document_id=r["document_id"],
            version_number=r["version_number"], sha256=r["sha256"],
            storage_key=r["storage_key"], content_type=r["content_type"],
            size_bytes=r["size_bytes"], status=r["status"],
            created_at=_iso(r["created_at"]), page_count=r["page_count"],
            error_code=r["error_code"],
        )
        for r in ver_rows
    ]
    chunks = [
        ChunkRecord(
            chunk_id=r["chunk_id"], document_id=r["document_id"],
            version_id=r["version_id"], page=r["page"], chunk_index=r["chunk_index"],
            content_hash=r["content_hash"], text=r["text"], title=r["title"],
            source=r["source"],
        )
        for r in chunk_rows
    ]
    return documents, versions, chunks, outbox


async def main() -> None:
    conn = await asyncpg.connect(_env_url())
    try:
        documents, versions, chunks, outbox = await _load(conn)
    finally:
        await conn.close()

    # --- 1. Estado en base (hechos) -----------------------------------------
    print("=== 1. ESTADO EN BASE (hechos) ===")
    for d in documents:
        print(
            f"doc={d.document_id} file={d.filename!r} status={d.editorial_status} "
            f"active={d.active_version_id} by={d.uploaded_by} deleted={d.deleted_at is not None}"
        )
    hist = next(d for d in documents if d.document_id == HISTORIA_DOC)
    hist_chunks = [c for c in chunks if c.document_id == HISTORIA_DOC]
    hist_active = [c for c in hist_chunks if c.version_id == hist.active_version_id]
    hist_status = next(v.status for v in versions if v.version_id == hist.active_version_id)
    print(
        f"Historia: chunks totales={len(hist_chunks)}, en version activa "
        f"({hist.active_version_id})={len(hist_active)}, version_status={hist_status}"
    )
    for term in ("revolución", "revolucion", "mayo", "causas"):
        n = sum(1 for c in hist_active if term in c.text.lower())
        print(f"  chunks activos con termino {term!r}: {n}")
    print("outbox:")
    for o in outbox:
        print(f"  ver={o['version_id']} {o['destination']}/{o['operation']} -> {o['status']}")



    # --- 2. Colección visible (capa 0 del runtime) ---------------------------
    print(f"\n=== 2. _collect_visible_chunks para admin {ADMIN} ===")
    visible: list = []
    for d in documents:
        if d.deleted_at is not None:
            print(f"  EXCLUIDO (deleted): {d.document_id}")
            continue
        if d.editorial_status in {"published", "published_pending_review"}:
            pass
        elif d.editorial_status == "pending_review" and d.uploaded_by == ADMIN:
            pass
        else:
            print(
                f"  EXCLUIDO (status={d.editorial_status}; el rol NO omite el filtro): "
                f"{d.document_id} {d.filename!r}"
            )
            continue
        for v in versions:
            if v.document_id == d.document_id and v.version_id == d.active_version_id \
                    and v.status == "available":
                n = sum(1 for c in chunks if c.version_id == v.version_id)
                print(f"  INCLUIDO: {d.document_id} {d.filename!r} ({n} chunks)")
                visible.extend(c for c in chunks if c.version_id == v.version_id)
    print(f"total chunks que alimentan BM25 en run_study: {len(visible)}")
    print(f"¿Historia entre ellos? {any(c.document_id == HISTORIA_DOC for c in visible)}")

    # --- 3a. BM25 con el índice REAL del runtime (solo chunks visibles) ------
    print("\n=== 3a. BM25 con indice REAL del runtime (solo chunks visibles) ===")
    catalog = _MiniCatalog(documents, versions)
    index_real = BM25Index.rebuild(visible)
    retriever = HybridRetriever(
        catalog=catalog, bm25_index=index_real, vector_search=None, top_k=5,
        viewer=ViewerContext(user_id=ADMIN, role="admin"),
    )
    retriever.load_chunks(visible)
    for rank, res in enumerate(await retriever.search(QUERY, top_k=5), 1):
        print(
            f"  #{rank} doc={res.document_id} page={res.page} score={res.score:.6f} "
            f"via={res.retriever} title={res.title!r}"
        )
        print(f"      text={res.text[:90]!r}")

    # --- 3b. Índice hipotético con TODOS los chunks + _is_visible real -------
    print("\n=== 3b. Indice hipotetico con TODOS los chunks (capa 1) + _is_visible (capa 2) ===")
    index_full = BM25Index.rebuild(chunks)
    print(f"chunks indexados en el indice hipotetico: {index_full.chunk_count()}")
    raw = index_full.search(QUERY, top_k=10)
    retriever_full = HybridRetriever(
        catalog=catalog, bm25_index=index_full, vector_search=None, top_k=5,
        viewer=ViewerContext(user_id=ADMIN, role="admin"),
    )
    retriever_full.load_chunks(chunks)
    doc_by_id = {d.document_id: d for d in documents}
    for pos, (chunk_id, score) in enumerate(raw, 1):
        chunk = retriever_full._chunk_by_id[chunk_id]
        visible_ok = await retriever_full._is_visible(chunk)
        doc = doc_by_id[chunk.document_id]
        motivo = "VISIBLE" if visible_ok else (
            f"EXCLUIDO (editorial_status={doc.editorial_status}; "
            f"propietario={doc.uploaded_by == ADMIN})"
        )
        print(
            f"  BM25 #{pos} score={score:.4f} doc={chunk.document_id} ({doc.filename!r}) "
            f"page={chunk.page} -> {motivo}"
        )
    ranked = index_full.search(QUERY, top_k=len(chunks))
    by_id = {c.chunk_id: c for c in chunks}
    hist_ranked = [
        (p, round(s, 4)) for p, (cid, s) in enumerate(ranked, 1)
        if by_id[cid].document_id == HISTORIA_DOC
    ]
    print(
        f"Historia en el ranking BM25 completo: {len(hist_ranked)} chunks; "
        f"posiciones/scores: {hist_ranked[:6]}"
    )

    # --- 4. Viewer estudiante ajeno -------------------------------------------
    print("\n=== 4. Viewer estudiante ajeno (usr_otro, student) ===")
    retriever_stu = HybridRetriever(
        catalog=catalog, bm25_index=index_real, vector_search=None, top_k=5,
        viewer=ViewerContext(user_id="usr_otro", role="student"),
    )
    retriever_stu.load_chunks(visible)
    res_stu = await retriever_stu.search(QUERY, top_k=5)
    print(f"resultados para estudiante ajeno: {len(res_stu)} (prueba.pdf excluido: no es autor)")
    for rank, res in enumerate(res_stu, 1):
        print(f"  #{rank} doc={res.document_id} page={res.page} score={res.score:.6f}")


asyncio.run(main())


