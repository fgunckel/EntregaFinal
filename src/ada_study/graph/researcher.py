"""Nodo researcher del grafo pedagógico (G2, ADR-0007).

Mecánica conservada de M7: el researcher posee solo `research_artifact`; al
regenerar limpia los errores pendientes e invalida resultados posteriores.
Cambios de dominio: contratos de study.py; el texto recuperado se trata como
dato no confiable (Spec 06 §94) y la insuficiencia se reconoce explícitamente
(Spec 00/03/04).
"""

from ada_study.contracts.study import SourceReference


def make_researcher_node(retriever):
    """Crea el nodo researcher sobre el retriever híbrido (F1).

    El retriever ya aplica los filtros de visibilidad (F1): solo fragmentos
    de documentos publicados con versión activa `available` llegan aquí.
    """

    async def researcher_node(state: dict) -> dict:
        pending_errors = state.get("validation_errors") or []
        query = state.get("user_request", "")
        results = await retriever.search(query, top_k=5)
        evidence = [_chunk_to_reference(result) for result in results]

        gaps = []
        if not evidence:
            gaps = [
                "No se recuperó evidencia del corpus para esta consulta. "
                "No se inventarán citas ni ejercicios."
            ]
        elif len(evidence) < 2:
            gaps.append(
                "Evidencia recuperada insuficiente para una respuesta completa."
            )

        summary = f"{len(evidence)} evidencias recuperadas."
        if pending_errors:
            summary = f"Reintento tras validación: {summary}"

        return {
            "research_artifact": {
                "query": query,
                "evidence": [ref.model_dump() for ref in evidence],
                "evidence_gaps": gaps,
                "confidence_score": round(min(1.0, len(evidence) / 5.0), 6),
            },
            "validation_errors": [],
            # Invalidación de resultados previos (mecánica M7 intacta)
            "validation_result": None,
            "study_response": None,
            "contributions": [
                *state.get("contributions", []),
                {
                    "agent": "researcher",
                    "artifact_type": "research",
                    "summary": summary,
                    "step": state.get("steps", 0),
                    "attempt": state.get("retries", 0),
                    "errors": pending_errors,
                },
            ],
        }

    return researcher_node


def _chunk_to_reference(result) -> SourceReference:
    """Deriva SourceReference desde un RetrievedChunk (procedencia completa)."""
    methods = [
        part for part in result.retriever.split("+") if part in {"bm25", "vector"}
    ]
    return SourceReference(
        document_id=result.document_id,
        version_id=result.version_id,
        chunk_id=result.chunk_id,
        title=result.title,
        page=result.page,
        excerpt=result.text[:280],
        content_hash=result.content_hash,
        retrieval_method=methods or ["bm25"],
    )
