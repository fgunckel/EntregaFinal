"""Supervisor con LLM del grafo pedagógico (G2, mecánica M7).

Mecánica conservada de M7 (`src/ada/agents/supervisor.py`): decisión
estructurada con salida validada por contrato. Cambio de dominio: los agentes
disponibles son researcher/tutor/validator/synthesizer/limited_finish/END.
"""

import json

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ada_study.graph.state import AgentName


class RoutingDecision(BaseModel):
    """Decisión estructurada del supervisor (contrato del dominio pedagógico)."""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    next_agent: AgentName
    reason: str = Field(min_length=1)
    task_completed: bool = False


def route_next_agent(state: dict, chat_model, response_holder: dict) -> RoutingDecision:
    """Decide el siguiente agente con salida estructurada (mecánica M7).

    El chat model (real o fake) recibe el estado y devuelve un dict con
    `next_agent`, `reason` y `task_completed`; la salida se valida contra
    `RoutingDecision`. En fallo de validación, enruta a validator de forma
    segura (nunca fabrica una ruta).
    """
    prompt = (
        "Sos el supervisor de un grafo de estudio pedagógico. Agentes "
        "disponibles: researcher (recuperar evidencia), tutor (generar "
        "respuesta), validator (validar), synthesizer (publicar), "
        "limited_finish (salida segura), END (terminar). Estado: "
        f"research={bool(state.get('research_artifact'))}, "
        f"response={bool(state.get('study_response'))}, "
        f"validation={bool((state.get('validation_result') or {}).get('is_valid'))}. "
        "Respondé SOLO con JSON: {\"next_agent\": ..., \"reason\": ..., "
        "\"task_completed\": ...}."
    )
    raw = chat_model.invoke(prompt)
    message = getattr(raw, "content", None) or str(raw)
    if response_holder is not None:
        response_holder["message"] = message

    try:
        data = json.loads(message)
        return RoutingDecision.model_validate(data)
    except (json.JSONDecodeError, ValidationError):
        # Salida no estructurada: ruta segura (mecánica M7: nunca fabricar).
        return RoutingDecision(
            next_agent="validator",
            reason="Salida del modelo no estructurada; validar estado.",
        )
