"""Synthesizer y limited_finish del grafo pedagógico (G2, ADR-0007).

Mecánica conservada de M7: el synthesizer solo corre con validación vigente
aprobada (protección determinística en el enrutador); limited_finish es la
salida segura sin fabricar respuesta. Cambio de dominio: el informe final es
la `StudyResponse` validada.
"""

from ada_study.contracts.study import StudyResponse


def synthesizer_node(state: dict) -> dict:
    """Publica la StudyResponse validada (mecánica M7: owns final_report)."""
    validation = state.get("validation_result") or {}
    approved = bool(validation.get("is_valid"))

    if not approved:
        return {
            "study_response": StudyResponse(
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
            ).model_dump(),
            "task_completed": True,
        }

    response = StudyResponse.model_validate(state["study_response"])
    return {
        "study_response": response.model_dump(),
        "task_completed": True,
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
    }
