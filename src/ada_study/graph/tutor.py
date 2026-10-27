"""Nodo tutor del grafo pedagógico (G2, ADR-0007).

Reemplaza al `analyst` de M7: en lugar de puntuar alternativas de
arquitectura, genera la respuesta pedagógica (StudyResponse) DESDE la
evidencia recuperada por el researcher. Mecánica intacta: posee solo su
artefacto, limpia errores pendientes e invalida resultados posteriores.

Regla de insuficiencia (Spec 00/03/04): sin evidencia suficiente, la
respuesta reconoce la insuficiencia explícitamente (status=
"insufficient_evidence") y NO inventa explicación fundamentada ni citas.
"""

from ada_study.contracts.study import SourceReference, StudyResponse


def make_tutor_node(chat_model=None):
    """Crea el nodo tutor (síntesis pedagógica desde evidencia).

    Con `chat_model` real, el tutor redacta la explicación a partir de la
    evidencia delimitada como dato no confiable. Sin modelo (modo
    determinístico para pruebas), compone la respuesta desde los excerpts.
    """

    def tutor_node(state: dict) -> dict:
        pending_errors = state.get("validation_errors") or []
        evidence = [
            SourceReference.model_validate(item)
            for item in (state.get("research_artifact") or {}).get("evidence", [])
        ]
        topic = (state.get("study_context") or {}).get("topic")
        if not topic and evidence:
            topic = evidence[0].title
        topic = topic or "general"

        if evidence:
            explanation = _compose_explanation(state.get("user_request", ""), evidence)
            status = "complete"
            limitations = []
        else:
            explanation = (
                "Los materiales disponibles no alcanzan para responder esta "
                "consulta. No se genera una respuesta fundamentada ni se "
                "inventan citas."
            )
            status = "insufficient_evidence"
            limitations = list(
                (state.get("research_artifact") or {}).get("evidence_gaps", [])
            )

        summary = f"Respuesta de estudio: {status}."
        if pending_errors:
            summary = f"Reintento tras validación: {summary}"

        return {
            "study_response": StudyResponse(
                status=status,
                topic=topic,
                explanation=explanation,
                references=evidence,
                uses_web_sources=False,
                evidence_gaps=list((state.get("research_artifact") or {}).get("evidence_gaps", [])),
                limitations=limitations,
            ).model_dump(),
            "validation_errors": [],
            "validation_result": None,
            "contributions": [
                *state.get("contributions", []),
                {
                    "agent": "tutor",
                    "artifact_type": "study_response",
                    "summary": summary,
                    "step": state.get("steps", 0),
                    "attempt": state.get("retries", 0),
                    "errors": pending_errors,
                },
            ],
        }

    return tutor_node


def _compose_explanation(request: str, evidence: list[SourceReference]) -> str:
    """Compone la explicación pedagógica a partir de la evidencia.

    Determinístico para pruebas: en producción, el chat model redacta la
    explicación a partir de la evidencia delimitada como dato no confiable.
    """
    parts = [f"Consulta: {request}."]
    for ref in evidence:
        # El excerpt se cita como dato, nunca como instrucciones (Spec 06 §94)
        parts.append(f"Según '{ref.title}' (página {ref.page}): {ref.excerpt}")
    return " ".join(parts)
