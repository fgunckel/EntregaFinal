"""AGENT-TRACE-01: verificación en vivo de la trazabilidad real.

Ejecuta `run_study()` COMPLETO (retrieval BM25 real del corpus de PostgreSQL,
solo SELECT + grafo real) con el chat model REAL de Groq desde `.env`.

Valida por caso:
- trace_id único, estado y duración total.
- Nodos efectivamente ejecutados con duraciones medidas.
- Decisiones del supervisor (próximo nodo + motivo).
- Researcher: fragmentos BM25 y aceptadas/descartadas con motivo.
- Tutor: modo usado y llamada(s) real(es) a Groq con tokens cuando el
  proveedor los reporta; 0 llamadas cuando no hay evidencia.
- Phoenix: spans reales compartiendo `trace_id` (exportador en memoria).

Uso: LIVE_CASE=1|2|3 para ejecutar un solo caso.
"""

import asyncio
import json
import os
import re
import sys
from types import SimpleNamespace

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src")))

from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ada_study.config import Settings
from ada_study.contracts.documents import ChunkRecord
from ada_study.index.retriever import ViewerContext
from ada_study.llm import build_chat_model
from ada_study.observability.phoenix import init_phoenix
from ada_study.study import run_study

CALLS: list[str] = []


async def build_repository():
    """Repositorio real de solo lectura sobre PostgreSQL (CorpusMem)."""
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
    docs_by_id = {d["document_id"]: d for d in docs}

    class CorpusRepository:
        async def list_documents(self):
            return [SimpleNamespace(**d) for d in docs]

        async def list_versions(self, document_id):
            return [
                SimpleNamespace(**v)
                for v in vers
                if v["version_id"] == docs_by_id[document_id]["active_version_id"]
            ]

        async def list_chunks(self, version_id):
            return by_version.get(version_id, [])

        async def get_document(self, document_id):
            d = docs_by_id[document_id]
            return SimpleNamespace(
                uploaded_by=d["uploaded_by"],
                active_version_id=d["active_version_id"],
                editorial_status=d["editorial_status"],
            )

        async def get_version(self, version_id):
            return SimpleNamespace(status=vers_by_id[version_id]["status"])

    print(f"corpus cargado: {len(chunks)} chunks de {len(docs)} documentos")
    return CorpusRepository(), next(
        d["uploaded_by"] for d in docs if "prueba" in d["filename"]
    )


def build_counting_model():
    """Cliente Groq real con contador de llamadas (sin envolver la clave)."""
    model = build_chat_model(Settings())  # fail-fast si la config es inválida
    assert model is not None, "el modo LLM no quedó activo"
    original = model._agenerate

    async def counting(messages, stop=None, run_manager=None, **kwargs):
        CALLS.append(str(messages[-1].content) if messages else "")
        return await original(messages, stop=stop, run_manager=run_manager, **kwargs)

    model._agenerate = counting
    return model


async def run_case(label, query, repository, owner_id, tutor_model, expect_calls: int):
    CALLS.clear()
    viewer = ViewerContext(user_id=owner_id, role="student")
    response = await run_study(
        repository, query, viewer, chat_model=tutor_model
    )
    trace = response.trace
    assert trace is not None, "run_study no adjuntó la traza"
    print("=" * 72)
    print(f"CASO: {label}")
    print(f"consulta: {query!r}")
    print(f"llamadas al modelo (Groq): {len(CALLS)} (esperadas: {expect_calls})")
    print(
        f"TRACE: id={trace.trace_id[:8]}... status={trace.status} "
        f"duración={trace.duration_ms:.0f}ms nodos={trace.nodes_executed}"
    )
    for event in trace.events:
        extra = ""
        if event.node == "tutor" and event.event_type in {"result", "error"}:
            d = event.details
            extra = (
                f" provider={d.get('provider')} model={d.get('model')} "
                f"tokens(in/out)={d.get('input_tokens')}/{d.get('output_tokens')}"
            )
        if event.node == "researcher" and event.event_type == "decision":
            extra = f" bm25={event.details['retrieved_chunks']} chunks"
        if event.node == "researcher" and event.event_type == "result":
            extra = (
                f" aceptadas={len(event.details['accepted'])} "
                f"descartadas={len(event.details['rejected'])}"
            )
        print(
            f"  [{event.node:12s}] {event.event_type:10s} ({event.status:7s}) "
            f"{event.summary[:70]}{extra}"
        )
    rechazadas = [
        ev
        for ev in trace.events
        if ev.node == "researcher" and ev.event_type == "result" and ev.details["rejected"]
    ]
    for ev in rechazadas:
        for item in ev.details["rejected"]:
            print(f"    DESCARTADA [{item['chunk_id'][:14]}...] {item['reason']}")
    print(f"STATUS FINAL: {response.status} | referencias: {len(response.references)}")
    if CALLS:
        veredicto = "OK" if len(CALLS) == expect_calls else "FALLO"
        print(f"llamadas registradas == {expect_calls}: {veredicto}")
    assert len(CALLS) == expect_calls, "número de llamadas a Groq inesperado"
    print(f"EXPLICACIÓN ({len(response.explanation)} chars): {response.explanation[:200]}...")
    print()
    return trace


async def main() -> None:
    settings = Settings()
    print(
        f"config: provider={settings.ada_llm_provider} model={settings.ada_llm_model} "
        f"phoenix={settings.ada_phoenix_enabled}"
    )
    tutor = build_counting_model()
    repository, owner_id = await build_repository()

    # Smoke Phoenix: spans reales comparten el trace_id (exportador en memoria).
    exporter = InMemorySpanExporter()
    init_phoenix(settings, span_exporter=exporter)

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
    only = os.environ.get("LIVE_CASE")
    if only:
        cases = [c for c in cases if c[0].startswith(f"{only}:")]
    traces = []
    for label, query, expected in cases:
        traces.append(
            await run_case(label, query, repository, owner_id, tutor, expected)
        )

    # Correlación Phoenix: spans con ada_trace_id == trace_id estructurado.
    spans = exporter.get_finished_spans()
    blob = json.dumps([dict(s.attributes or {}) for s in spans], default=str)
    print("=" * 72)
    print(f"PHOENIX: {len(spans)} spans reales capturados (exportador en memoria)")
    for t in traces:
        assert t.trace_id in blob, f"span sin ada_trace_id para {t.trace_id[:8]}"
    print("correlación ada_trace_id en spans: OK")
    nodos = {span.name for span in spans}
    print(f"nombres de span (muestra): {sorted(nodos)[:10]}")


asyncio.run(main())

