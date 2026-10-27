"""Validator determinístico del grafo pedagógico (G2, ADR-0007).

Mecánica conservada de M7: validación determinística (no LLM) con errores
clasificados por el especialista responsable (researcher o tutor) y
presupuesto de reintentos (incrementa `retries` una vez por reparación).
Cambios de dominio: valida `StudyResponse` y `SourceReference` (study.py).
"""

from pydantic import ValidationError

from ada_study.contracts.study import SourceReference, StudyResponse

MAX_RETRIES = 2


def validator_node(state: dict) -> dict:
    """Valida los artefactos de forma determinística (mecánica M7 intacta)."""
    errors: list[str] = []
    warnings: list[str] = []
    research_errors: list[str] = []
    tutor_errors: list[str] = []

    research = state.get("research_artifact")
    response_raw = state.get("study_response")

    if not research:
        error = "Falta el artefacto de investigación; no se puede validar sin evidencia."
        errors.append(error)
        research_errors.append(error)
    if not response_raw:
        error = "Falta la respuesta de estudio; no se puede validar sin síntesis."
        errors.append(error)
        tutor_errors.append(error)

    refs: list[SourceReference] = []
    response = None
    if research and response_raw:
        try:
            refs = [
                SourceReference.model_validate(item)
                for item in research.get("evidence", [])
            ]
        except ValidationError as error:
            message = f"Violación de contrato en evidencia: {error}"
            errors.append(message)
            research_errors.append(message)

        try:
            response = StudyResponse.model_validate(state["study_response"])
        except ValidationError as error:
            message = f"Violación de contrato en StudyResponse: {error}"
            errors.append(message)
            tutor_errors.append(message)

        if response is not None:
            # Coherencia de referencias (regla M7: cited - evidence).
            evidence_ids = {ref.chunk_id for ref in refs}
            cited_ids = {ref.chunk_id for ref in response.references}
            missing = sorted(cited_ids - evidence_ids)
            if missing:
                error = f"La respuesta cita chunks desconocidos: {', '.join(missing)}."
                errors.append(error)
                tutor_errors.append(error)

            # "complete" exige al menos una referencia que fundamente
            if response.status == "complete" and not response.references:
                error = (
                    "Respuesta 'complete' sin referencias: la evidencia no "
                    "fundamenta la explicación."
                )
                errors.append(error)
                tutor_errors.append(error)

            # Insuficiencia reconocida: gaps del researcher deben reflejarse
            research_gaps = research.get("evidence_gaps", [])
            if research_gaps and response.status == "complete" and not response.evidence_gaps:
                error = (
                    "El researcher reportó gaps de evidencia pero la respuesta "
                    "no los reconoce (status='complete' sin reconocer gaps)."
                )
                errors.append(error)
                tutor_errors.append(error)

    is_valid = not errors
    retries = state.get("retries", 0)
    fixable = not is_valid and retries < MAX_RETRIES and bool(research_errors or tutor_errors)
    result = {
        "is_valid": is_valid,
        "errors": errors,
        "warnings": warnings,
        "requires_human_review": not is_valid and not fixable,
        "research_errors": research_errors,
        "tutor_errors": tutor_errors,
    }

    update = {
        "validation_result": result,
        "retries": retries,
        "contributions": [
            *state.get("contributions", []),
            {
                "agent": "validator",
                "artifact_type": "validation",
                "summary": "Válido" if is_valid else f"{len(errors)} errores de validación.",
                "step": state.get("steps", 0),
                "decision": "valid" if is_valid else "invalid",
                "reason": (
                    "Artefactos consistentes."
                    if is_valid
                    else "Errores de validación detectados."
                ),
                "attempt": retries,
                "errors": errors,
            },
        ],
    }

    if not is_valid and fixable:
        repair_errors = list(research_errors or tutor_errors)
        result["pending_repair_agent"] = "researcher" if research_errors else "tutor"
        update["validation_result"] = result
        update["retries"] = retries + 1
        update["validation_errors"] = repair_errors

    return update
