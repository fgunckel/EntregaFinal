"""RAG-QUALITY-02: diagnóstico read-only de falsa suficiencia.

SELECT-only contra PostgreSQL; reconstruye BM25 en memoria y aplica el filtro
de pertinencia del researcher SIN llamar a ningún LLM y sin escribir datos.
"""

import asyncio

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ada_study.config import get_settings
from ada_study.graph.researcher import (
    _normalize,
    _term_matches,
    build_retrieval_query,
)
from ada_study.index.bm25_index import BM25Index, significant_terms, tokenize

QUERY = (
    "Explicame la fotosíntesis usando exclusivamente el material de "
    "Historia de 5.º grado."
)


async def load_chunks(engine):
    """Chunks reales de las versiones activas available de ambos documentos."""
    sql = text(
        """
        SELECT c.chunk_id, c.version_id, c.page, c.title, c.text,
               d.document_id, d.filename, d.editorial_status
        FROM document_chunks c
        JOIN document_versions v ON v.version_id = c.version_id
        JOIN documents d ON d.document_id = v.document_id
        WHERE d.document_id IN ('doc_e156f7ba6e3f', 'doc_b9d9f675e861')
          AND v.status = 'available'
          AND c.version_id = d.active_version_id
        ORDER BY d.document_id, c.page, c.chunk_id
        """
    )
    async with engine.connect() as conn:
        rows = (await conn.execute(sql)).fetchall()
    return rows


def diagnose(query, rows, label):
    """Reproduce BM25 + filtro del researcher e informa por fragmento."""
    index = BM25Index.rebuild(rows)
    terms = significant_terms(query)
    results = index.search(query, top_k=5)
    print(f"\n=== {label} ===")
    print(f"Consulta: {query!r}")
    print(f"Términos significativos ({len(terms)}): {terms}")
    target = terms
    required = min(2, len(target))
    print(f"Umbral aplicado: >= {required} coincidencias de {len(target)} términos")
    if not results:
        print("BM25: sin resultados.")
        return []
    for rank, (chunk_id, score) in enumerate(results, 1):
        row = next(r for r in rows if r.chunk_id == chunk_id)
        chunk_terms = frozenset(_normalize(t) for t in tokenize(f"{row.title} {row.text}"))
        matched = [t for t in target if _term_matches(t, chunk_terms)]
        verdict = "ACEPTADO" if len(matched) >= required else "RECHAZADO"
        print(
            f"  #{rank} score={score:.4f} [{row.filename} p.{row.page}] "
            f"{row.title[:60]!r} -> {verdict} ({len(matched)} matches: {matched})"
        )
        print(f"      texto: {row.text[:120]!r}")
    return results


async def main():
    engine = create_async_engine(get_settings().database_url)
    rows = await load_chunks(engine)
    print(f"Chunks cargados: {len(rows)}")
    docs = {}
    for r in rows:
        docs.setdefault((r.document_id, r.filename, r.editorial_status), 0)
        docs[(r.document_id, r.filename, r.editorial_status)] += 1
    for k, v in docs.items():
        print(f"  {k}: {v} chunks")

    diagnose(QUERY, rows, "Caso B: fotosíntesis (Historia + Matemática del dueño)")
    diagnose(
        "Explicame la Revolución de Mayo usando el material de Historia de 5.º grado.",
        rows,
        "Caso A: Revolución de Mayo (Historia publicada)",
    )
    # Caso D: seguimiento contextual con historial previo (tema ADA previo).
    history = [
        {"role": "student", "content": QUERY},
        {"role": "ada", "content": "Respuesta previa", "topic": "la Revolución de Mayo"},
    ]
    rq = build_retrieval_query("Dame otro ejemplo", history)
    print(f"\nCaso D: consulta contextual 'Dame otro ejemplo' -> retrieval_query: {rq!r}")
    diagnose(rq, rows, "Caso D: seguimiento contextual con tema previo ajeno")

    # Caso C: un documento que SÍ explica fotosíntesis debe aceptarse.
    from types import SimpleNamespace

    fotos_rows = list(rows) + [
        SimpleNamespace(
            chunk_id="chunk_synthetic",
            version_id="ver_synthetic",
            page=1,
            title="Ciencias Naturales 5.º grado — La fotosíntesis",
            filename="ciencias_fotosintesis.pdf",
            editorial_status="published",
            text=(
                "La fotosíntesis es el proceso por el cual las plantas "
                "convierten la luz solar, el agua y el dióxido de carbono en "
                "glucosa y oxígeno. Ocurre en las hojas, en los cloroplastos."
            ),
        )
    ]
    diagnose(QUERY, fotos_rows, "Caso C: con documento que explica fotosíntesis")

    # Experimento de umbral: ¿subir required arregla el caso B?
    print("\n=== Experimento de umbral (Caso B) ===")
    index = BM25Index.rebuild(rows)
    terms = significant_terms(QUERY)
    results = index.search(QUERY, top_k=5)
    for req in (2, 3, 4, 5):
        accepted = 0
        for chunk_id, _ in results:
            row = next(r for r in rows if r.chunk_id == chunk_id)
            chunk_terms = frozenset(
                _normalize(t) for t in tokenize(f"{row.title} {row.text}")
            )
            n = sum(1 for t in terms if _term_matches(t, chunk_terms))
            if n >= req:
                accepted += 1
        print(f"  required={req}: {accepted}/5 fragmentos aceptados")

    # Ruta conversacional REAL: el tutor declara topic = evidence[0].title
    # (título del chunk = nombre de archivo). Simular el turno observado
    # ('Turnos anteriores (2)') con ese topic previo.
    prev_topic = "Historia_5to_Grado_Guia_Completa.pdf"
    history = [
        {"role": "student", "content": QUERY},
        {"role": "ada", "content": "Respuesta previa", "topic": prev_topic},
    ]
    rq = build_retrieval_query(QUERY, history)
    print(f"\n=== Ruta conversacional (topic previo = {prev_topic!r}) ===")
    print(f"retrieval_query: {rq!r}")
    from ada_study.graph.researcher import _SHORT_QUERY_WORDS

    low = f" {QUERY.lower()} "
    contextual = len(QUERY.split()) <= _SHORT_QUERY_WORDS or any(
        m in low
        for m in (
            "ese", "esa", "eso", "otro", "otra", "mismo", "misma",
            "anterior", "explicame", "explicámelo", "más", "practicar",
            "ejercicio", "ejemplo",
        )
    )
    topic_terms = significant_terms(prev_topic) if contextual else []
    print(f"contextual={contextual} (marker 'explicame' en la consulta)")
    print(f"target_terms = SOLO términos del topic previo: {topic_terms}")
    required = min(2, len(topic_terms))
    print(f"Umbral: >= {required} de {len(topic_terms)}")
    for chunk_id, score in index.search(rq, top_k=5):
        row = next(r for r in rows if r.chunk_id == chunk_id)
        chunk_terms = frozenset(
            _normalize(t) for t in tokenize(f"{row.title} {row.text}")
        )
        matched = [t for t in topic_terms if _term_matches(t, chunk_terms)]
        verdict = "ACEPTADO" if len(matched) >= required else "RECHAZADO"
        print(
            f"  score={score:.4f} [{row.filename} p.{row.page}] -> {verdict} "
            f"({len(matched)}: {matched})"
        )

    await engine.dispose()


async def list_conversations(engine):
    """SELECT-only: hilos de conversación existentes (sin modificar nada)."""
    try:
        async with engine.connect() as conn:
            rows = (
                await conn.execute(
                    text(
                        "SELECT thread_id, created_at FROM conversations "
                        "ORDER BY created_at DESC LIMIT 10"
                    )
                )
            ).fetchall()
            print(f"\nConversaciones registradas (últimas {len(rows)}):")
            for r in rows:
                print(f"  {r.thread_id} @ {r.created_at}")
    except Exception as exc:  # noqa: BLE001
        print(f"\n(conversaciones no legibles: {exc})")


async def main2():
    engine = create_async_engine(get_settings().database_url)
    await list_conversations(engine)
    await engine.dispose()


asyncio.run(main())
