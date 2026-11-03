"""RAG-QUALITY-04: diagnóstico READ-ONLY de generación (problema B).

Ejecuta el grafo REAL con el corpus real de PostgreSQL (solo SELECT) y un
FakeChatModel (sin llamadas a Groq ni a ningún proveedor). No escribe nada:
toda la persistencia es en memoria.
"""

import asyncio
import os
import re
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ada_study.contracts.documents import ChunkRecord
from ada_study.graph import compile_graph
from ada_study.index.bm25_index import BM25Index
from ada_study.index.retriever import HybridRetriever, ViewerContext
from ada_study.study.runtime import STUDY_TOP_K


class FakeChatModel(BaseChatModel):
    """Modelo falso: registra prompts, cuenta llamadas, respuesta fija."""

    response: str = "Explicación generada por el modelo falso (FakeChatModel)."
    calls: list = []

    @property
    def _llm_type(self) -> str:
        return "fake-chat"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls.append(str(messages[-1].content) if messages else "")
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=self.response))])


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
    docs_by_id = {d["document_id"]: d for d in docs}
    vers_by_id = {v["version_id"]: v for v in vers}
    by_version = {}
    for c in chunks:
        by_version.setdefault(c.version_id, []).append(c)

    class MemCatalog:
        async def get_document(self, document_id):
            d = docs_by_id[document_id]
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


async def run_case(label, query, tutor_model):
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
    modo = "SÍ (FakeChatModel)" if tutor_model is not None else "None (PRODUCCIÓN ACTUAL)"
    print(f"tutor_model inyectado: {modo}")
    print(f"llamadas al modelo del tutor: {len(tutor_model.calls) if tutor_model else 0}")
    print(f"evidencia del researcher: {len(research.get('evidence', []))} fragmentos")
    for ref in research.get("evidence", []):
        print(f"  - [{ref['chunk_id'][:12]}...] p.{ref['page']} '{ref['title']}'")
    print(f"evidence_gaps: {research.get('evidence_gaps', [])}")
    print(f"STATUS FINAL: {response['status']}")
    print(f"validación: is_valid={result['validation_result']['is_valid']}")
    print(f"topic: {response['topic']}")
    print(f"limitations: {response['limitations']}")
    print(f"referencias publicadas: {len(response['references'])}")
    for ref in response["references"]:
        print(f"  - [{ref['chunk_id'][:12]}...] p.{ref['page']} '{ref['title']}'")
    print(f"EXPLICACIÓN FINAL:\n---\n{response['explanation']}\n---")
    if tutor_model and tutor_model.calls:
        print("PROMPT RECIBIDO POR EL MODELO (primeros 900 chars):")
        print("---")
        print(tutor_model.calls[0][:900])
        print("---")
    print()


async def main() -> None:
    fake = FakeChatModel()
    await run_case("A: consulta general con modelo INYECTADO (diseño TASK-11)", "Matemática", fake)
    fake2 = FakeChatModel()
    await run_case(
        "B: consulta problemática con modelo INYECTADO",
        "información de matemática que tengas",
        fake2,
    )
    await run_case(
        "C: consulta general en MODO PRODUCCIÓN ACTUAL (tutor_model=None)",
        "Matemática",
        None,
    )
    await run_case(
        "D: consulta específica en MODO PRODUCCIÓN ACTUAL",
        "¿Qué es una fracción?",
        None,
    )


asyncio.run(main())
