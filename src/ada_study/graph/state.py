"""Estado compartido del grafo pedagógico (G2, ADR-0007).

Mecánica idéntica a M7 (`src/ada/state.py`): cada nodo actualiza solo los
campos que posee, devolviendo updates parciales. Cambian los contratos del
dominio (study.py) y los campos de dominio; la mecánica de contribuciones,
invalidación y HITL queda intacta.
"""

from typing import Literal, NotRequired

from langgraph.graph import MessagesState
from typing_extensions import TypedDict

AgentName = Literal[
    "researcher",
    "tutor",
    "validator",
    "synthesizer",
    "limited_finish",
    "END",
]


class AgentContribution(TypedDict):
    agent: str
    artifact_type: str
    summary: str
    step: int
    decision: NotRequired[str]
    reason: NotRequired[str]
    attempt: NotRequired[int]
    errors: NotRequired[list[str]]


class AgentState(MessagesState):
    """Estado compartido del grafo de estudio (misma mecánica que M7)."""

    # Solicitud del estudiante y contexto de estudio (reemplaza system_profile)
    user_request: str
    study_context: NotRequired[dict | None]

    # Artefactos: cada especialista posee solo el suyo
    research_artifact: NotRequired[dict | None]
    study_response: NotRequired[dict | None]
    validation_result: NotRequired[dict | None]
    contributions: NotRequired[list[AgentContribution]]
    # Errores de validación pendientes, dirigidos al especialista responsable
    validation_errors: NotRequired[list[str]]

    next_agent: AgentName
    routing_reason: str
    steps: int
    retries: int
    task_completed: bool

    # --- Human-in-the-loop (conservado de M7) ---
    hitl_required: NotRequired[bool]
    hitl_approved: NotRequired[bool]
    hitl_rejected: NotRequired[bool]
    hitl_reason: NotRequired[str]
    estimated_cost_usd: NotRequired[float]
    pending_tools: NotRequired[list[dict]]
