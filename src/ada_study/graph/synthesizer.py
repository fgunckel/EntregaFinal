"""Synthesizer y limited_finish del grafo pedagógico (G2, ADR-0007).

Mecánica conservada de M7: el synthesizer solo corre con validación vigente
aprobada (protección determinística en el enrutador); limited_finish es la
salida segura sin fabricar respuesta. Cambio de dominio: el informe final es
la `StudyResponse` validada.

CHECKPOINTER-03 (dimensión A): el synthesizer es el punto de CONSOLIDACIÓN
del historial pedagógico: agrega UNA entrada `ada` por turno. limited_finish
NO registra nada: en una ejecución fallida no se inventa una respuesta final
ficticia (el mensaje del alumno queda registrado por el intake del runtime,
con guarda anti-duplicado para reintentos).
"""

from ada_study.contracts.study import StudyResponse
from ada_study.graph.history import append_ada_message
from ada_study.observability.trace import new_event


def _consolidate(state: dict, response: StudyResponse) -> dict:
    """Devuelve el update con la entrada `ada` agregada UNA sola vez."""
    return {
        "conversation_history": append_ada_message(
            list(state.get("conversation_history") or []),
            response.explanation,
            response.topic,
        ),
    }


def _publication_event(response: StudyResponse, approved: bool) -> dict:
    """Evento de publicación (AGENT-TRACE-01 §3.6): decisión y resultado.

    Registra el resultado recibido del validator, la decisión de publicación
    y el estado final de la respuesta (con cantidad de referencias).
    """
    return new_event(
        "synthesizer",
        "decision",
        (
            f"Validación aprobada; respuesta publicada (status={response.status})."
            if approved
            else "Validación no aprobada; se publica salida 'limited' no definitiva."
        ),
        details={
            "validation_approved": approved,
            "published_status": response.status,
            "references_count": len(response.references),
            "history_consolidated": True,
        },
    )


def synthesizer_node(state: dict) -> dict:
    """Publica la StudyResponse validada (mecánica M7: owns final_report)."""
    validation = state.get("validation_result") or {}
    approved = bool(validation.get("is_valid"))

    if not approved:
        response = StudyResponse(
            status="limited",
            topic="general",
            explanation=(
                "Los artefactos no fueron aprobados por el validador. "
                "No se emite respuesta definitiva."
            ),
            references=[],
            evidence_gaps=[],
            limitations=[
                *(validation.get("errors", []) or []),
                "Respuesta no definitiva: artefactos no aprobados.",
            ],
        )
        return {
            "study_response": response.model_dump(),
            "task_completed": True,
            "trace_events": [_publication_event(response, approved=False)],
            **_consolidate(state, response),
        }

    response = StudyResponse.model_validate(state["study_response"])
    return {
        "study_response": response.model_dump(),
        "task_completed": True,
        "trace_events": [_publication_event(response, approved=True)],
        **_consolidate(state, response),
        "contributions": [
            *state.get("contributions", []),
            {
                "agent": "synthesizer",
                "artifact_type": "study_response",
                "summary": "Respuesta de estudio publicada.",
                "step": state.get("steps", 0),
            },
        ],
    }


def limited_finish_node(state: dict) -> dict:
    """Salida segura sin fabricar respuesta (mecánica M7 intacta)."""
    return {
        "study_response": StudyResponse(
            status="limited",
            topic="general",
            explanation=(
                "El flujo terminó antes de completar la respuesta: límite de "
                "pasos o error no reparable. Los materiales no alcanzan para "
                "una respuesta fundamentada."
            ),
            references=[],
            evidence_gaps=["Resultado limitado: el flujo no completó la validación."],
            limitations=[
                "Resultado limitado: el flujo no completó investigación y síntesis validadas."
            ],
        ).model_dump(),
        "task_completed": True,
        # Advertencia trazable (AGENT-TRACE-01): salida segura sin respuesta
        # fundamentada; la causa quedó en los eventos previos del turno.
        "trace_events": [
            new_event(
                "limited_finish",
                "warning",
                "Salida segura sin respuesta fundamentada (límite de pasos o "
                "reparaciones agotadas).",
                status="warning",
            )
        ],
    }
