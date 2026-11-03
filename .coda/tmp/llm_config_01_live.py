"""LLM-CONFIG-01: verificación en vivo de la activación de Groq.

Ejecuta el grafo REAL con el corpus real de PostgreSQL (solo SELECT) y el
chat model REAL construido por `build_chat_model()` desde `.env`
(ADA_LLM_PROVIDER=groq). Cuenta las llamadas al proveedor envolviendo
`_agenerate` (sin envolver la clave ni loguear contenido sensible).

Casos:
1. Consulta temática con evidencia  -> 1 llamada, explicación pedagógica.
2. Consulta histórica problemática  -> 1 llamada, estado contractual correcto.
3. Consulta sin evidencia           -> 0 llamadas, insufficient_evidence.

Además: smoke test de `create_app` (config validada y cliente construido una
vez por proceso, sin errores al arrancar).
"""

import asyncio
import os
import re
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ada_study.config import Settings
from ada_study.contracts.documents import ChunkRecord
from ada_study.graph import compile_graph
from ada_study.index.bm25_index import BM25Index
from ada_study.index.retriever import HybridRetriever, ViewerContext
from ada_study.llm import build_chat_model
from ada_study.study.runtime import STUDY_TOP_K

CALLS: list[str] = []


async def load_corpus():
    url = re.search(r"^DATABASE_URL=(.+)$", open(".env", encoding="utf-8").read(), re.M)
    url = url.group(1).strip().strip('"').strip("'")
    if url.startswith("postgresql://"):
        url = url.replace("postgresql://", "postgresql+asyncpg://", 1)
    engine = create_async_engine(url)
    async with engine.connect() as conn:
        docs = (
            (
                await conn.execute(
                    text(
                        "SELECT document_id, filename, active_version_id, editorial_status, "
                        "uploaded_by FROM documents"
                    )
                )
            )
            .mappings()
            .all()
        )
        vers = (
            (await conn.execute(text("SELECT version_id, status FROM document_versions")))
            .mappings()
            .all()
        )
        chunks = []
        for d in docs:
            if d["editorial_status"] not in {"published", "published_pending_review"}:
                continue
            rows = (
                (
                    await conn.execute(
                        text(
                            "SELECT version_id, chunk_id, page, chunk_index, content_hash, "
                            "text, title, source FROM document_chunks WHERE version_id = :vid"
                        ),
                        {"vid": d["active_version_id"]},
                    )
                )
                .mappings()
                .all()
            )
            for r in rows:
                chunks.append(ChunkRecord(document_id=d["document_id"], **r))
    await engine.dispose()
    vers_by_id = {v["version_id"]: v for v in vers}
    by_version = {}
    for c in chunks:
        by_version.setdefault(c.version_id, []).append(c)

    class MemCatalog:
        async def get_document(self, document_id):
            d = next(x for x in docs if x["document_id"] == document_id)
            return SimpleNamespace(
                uploaded_by=d["uploaded_by"],
                active_version_id=d["active_version_id"],
                editorial_status=d["editorial_status"],
            )

        async def get_version(self, version_id):
            return SimpleNamespace(status=vers_by_id[version_id]["status"])

        async def list_chunks(self, version_id):
            return by_version.get(version_id, [])

    index = BM25Index.rebuild(chunks)
    owner = next(d["uploaded_by"] for d in docs if "prueba" in d["filename"])
    retriever = HybridRetriever(
        catalog=MemCatalog(),
        bm25_index=index,
        vector_search=None,
        top_k=STUDY_TOP_K,
        viewer=ViewerContext(user_id=owner, role="student"),
    )
    retriever.load_chunks(chunks)
    return retriever


def build_counting_model():
    """Cliente Groq real con contador de llamadas (sin envolver la clave)."""
    settings = Settings()
    model = build_chat_model(settings)  # fail-fast si la config es inválida
    assert model is not None, "build_chat_model devolvió None: el modo LLM no quedó activo"
    original = model._agenerate

    async def counting(messages, stop=None, run_manager=None, **kwargs):
        CALLS.append(str(messages[-1].content) if messages else "")
        return await original(messages, stop=stop, run_manager=run_manager, **kwargs)

    model._agenerate = counting
    return model


async def run_case(label, query, tutor_model, expect_calls: int):
    CALLS.clear()
    retriever = await load_corpus()
    graph = compile_graph(chat_model=None, retriever=retriever, tutor_model=tutor_model)
    initial = {
        "user_request": query,
        "study_context": None,
        "conversation_history": [],
        "research_artifact": None,
        "study_response": None,
        "validation_result": None,
        "validation_errors": [],
        "contributions": [],
        "steps": 0,
        "retries": 0,
        "task_completed": False,
    }
    result = await graph.ainvoke(initial, config={"recursion_limit": 25})
    response = result["study_response"]
    research = result.get("research_artifact") or {}
    print("=" * 72)
    print(f"CASO: {label}")
    print(f"consulta: {query!r}")
    print(f"llamadas al modelo (Groq): {len(CALLS)} (esperadas: {expect_calls})")
    print(f"evidencia del researcher: {len(research.get('evidence', []))} fragmentos")
    for ref in research.get("evidence", []):
        print(f"  - [{ref['chunk_id'][:12]}...] p.{ref['page']} '{ref['title']}'")
    print(f"evidence_gaps: {research.get('evidence_gaps', [])}")
    print(f"STATUS FINAL: {response['status']}")
    print(f"validación: is_valid={result['validation_result']['is_valid']}")
    print(f"limitations: {response['limitations']}")
    print(f"referencias publicadas: {len(response['references'])}")
    for ref in response["references"]:
        print(f"  - [{ref['chunk_id'][:12]}...] p.{ref['page']} '{ref['title']}'")
    print(f"EXPLICACIÓN FINAL ({len(response['explanation'])} chars):")
    print("---")
    print(response["explanation"])
    print("---")
    print()


async def main() -> None:
    # 0. Smoke test de arranque: config validada + cliente construido 1 vez.
    settings = Settings()
    print("=" * 72)
    provider = settings.ada_llm_provider
    model_name = settings.ada_llm_model
    print(f"SMOKE TEST create_app: provider={provider} model={model_name}")
    model = build_chat_model(settings)
    print(f"chat model construido: {type(model).__module__}.{type(model).__name__}")
    print(f"  max_tokens: {model.max_tokens}  temperature: {model.temperature}")
    print()

    tutor = build_counting_model()
    only = os.environ.get("LIVE_CASE")
    cases = [
        (
            "1: consulta temática CON evidencia (Groq real)",
            "¿Qué es una fracción y cómo se suman dos fracciones?",
            1,
        ),
        (
            "2: consulta histórica problemática (Groq real)",
            "información de matemática que tengas",
            1,
        ),
        (
            "3: consulta SIN evidencia (Groq NO debe invocarse)",
            "zzz qqq xyzzy tema inexistente qwerty",
            0,
        ),
    ]
    if only:
        cases = [c for c in cases if c[0].startswith(f"{only}:")]
    for label, query, expected in cases:
        await run_case(label, query, tutor, expect_calls=expected)


asyncio.run(main())
