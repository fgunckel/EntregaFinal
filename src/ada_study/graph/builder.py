"""Grafo pedagógico de ADA Study (G2, ADR-0007).

Topología conservada de M7 (`src/ada/graph.py`):
    START -> supervisor -> (researcher | tutor | validator | synthesizer
    | limited_finish | END) -> supervisor -> ...

Mecánica intacta:
- Supervisor (LLM) propone ruta; los conditional edges aplican las reglas
  determinísticas: synthesizer solo con validación vigente aprobada; END
  exitoso solo con respuesta publicada y validación vigente.
- Workers vuelven al supervisor; el validator decide de forma determinística.
- limited_finish: salida segura sin fabricar respuesta.

Cambios de dominio (ADR-0007): contratos de study.py; el texto recuperado se
delimita como dato no confiable (Spec 06 §94); la insuficiencia se reconoce
explícitamente (Spec 00/03/04).
"""

from collections.abc import Callable

from langgraph.graph import END, START, StateGraph

from ada_study.graph.researcher import make_researcher_node
from ada_study.graph.state import AgentName, AgentState
from ada_study.graph.synthesizer import limited_finish_node, synthesizer_node
from ada_study.graph.tutor import make_tutor_node
from ada_study.graph.validator import validator_node
from ada_study.observability.trace import new_event, traced_node

Worker = Callable[[dict], dict]


def make_supervisor_node(chat_model) -> Worker:
    """Crea el nodo supervisor respaldado por un chat model (real o fake).

    Mecánica M7 intacta: decisión estructurada con límites determinísticos.
    El modelo real o fake debe devolver un dict con `next_agent`, `reason` y
    `task_completed` (RoutingDecision del dominio pedagógico).
    """

    def supervisor_node(state: dict) -> dict:
        steps = state.get("steps", 0)
        research = state.get("research_artifact")
        response = state.get("study_response")
        validation = state.get("validation_result") or {}

        # Enrutado determinístico mínimo (para pruebas y modo sin LLM):
        # 1. Sin evidencia -> researcher
        if not research:
            next_agent = "researcher"
            reason = "Sin evidencia recuperada; recuperar evidencia primero."
        # 2. Con evidencia y sin respuesta -> tutor
        elif not response:
            next_agent = "tutor"
            reason = "Con evidencia; generar la respuesta pedagógica."
        # 3. Con respuesta sin validar -> validator
        elif not validation:
            next_agent = "validator"
            reason = "Respuesta pendiente de validación."
        # 4. Validación vigente -> synthesizer
        elif validation.get("is_valid"):
            next_agent = "synthesizer"
            reason = "Validación vigente; publicar respuesta."
        # 5. Validación rechazada -> validator (reparación dirigida)
        else:
            next_agent = "validator"
            reason = "Validación rechazada; reparar artefactos."

        # Decisión de enrutado trazable (AGENT-TRACE-01 §3.1): próximo nodo
        # y motivo real de la decisión.
        decision_event = new_event(
            "supervisor",
            "decision",
            f"Enrutado a {next_agent}: {reason}",
            details={
                "next_agent": next_agent,
                "reason": reason,
                "steps": steps + 1,
            },
        )
        return {
            "next_agent": next_agent,
            "routing_reason": reason,
            "task_completed": False,
            "steps": steps + 1,
            "trace_events": [decision_event],
        }

    supervisor_node = _supervisor_with_model(chat_model) if chat_model else supervisor_node
    return supervisor_node


def _supervisor_with_model(chat_model):
    """Supervisor con LLM (producción): decisión estructurada (mecánica M7)."""

    def supervisor_node(state: dict) -> dict:
        from ada_study.graph.supervisor import route_next_agent

        response_holder: dict = {"message": None}
        decision = route_next_agent(state, chat_model, response_holder)
        return {
            "messages": [response_holder["message"]] if response_holder["message"] else [],
            "next_agent": decision.next_agent,
            "routing_reason": decision.reason,
            "task_completed": decision.task_completed,
            "steps": state.get("steps", 0) + 1,
            # Decisión estructurada real del modelo (AGENT-TRACE-01 §3.1):
            # próximo nodo propuesto, motivo y estado de la tarea.
            "trace_events": [
                new_event(
                    "supervisor",
                    "decision",
                    f"Enrutado a {decision.next_agent}: {decision.reason}",
                    details={
                        "next_agent": decision.next_agent,
                        "reason": decision.reason,
                        "task_completed": decision.task_completed,
                        "steps": state.get("steps", 0) + 1,
                    },
                )
            ],
        }

    return supervisor_node


def route_after_supervisor(state: dict) -> AgentName:
    """Conditional edge con protección determinística (mecánica M7 intacta).

    El supervisor (LLM) propone, pero esta arista aplica la regla
    determinística: el synthesizer solo corre con validación vigente aprobada.
    """
    proposed = state["next_agent"]
    validation = state.get("validation_result") or {}
    approved = bool(validation.get("is_valid"))

    if proposed == "synthesizer" and not approved:
        return "validator"

    if proposed == "END":
        response = state.get("study_response") or {}
        if response.get("status") == "complete" and approved:
            return END
        return "validator" if state.get("research_artifact") else "limited_finish"

    return proposed


def route_after_validator(state: dict) -> str:
    """Enrutado determinístico tras el validator (mecánica M7 intacta).

    - Válido -> synthesizer.
    - Errores reparables con presupuesto -> el especialista responsable.
    - Reparaciones agotadas -> limited_finish (salida segura).
    """
    validation = state.get("validation_result") or {}
    if validation.get("is_valid"):
        return "synthesizer"

    pending = validation.get("pending_repair_agent")
    if pending in {"researcher", "tutor"}:
        return pending

    if state.get("retries", 0) >= 2:
        return "limited_finish"

    if validation.get("research_errors"):
        return "researcher"
    if validation.get("tutor_errors"):
        return "tutor"

    return "limited_finish"


def build_graph(chat_model=None, retriever=None, tutor_model=None):
    """Construye el grafo de estudio con enrutado condicional (mecánica M7).

    `chat_model` queda reservado para el supervisor (None = enrutado
    determinístico, decisión aprobada TASK-11); `tutor_model` se inyecta
    SOLO en el tutor para redactar la explicación (None = composición
    determinística).
    """
    graph = StateGraph(AgentState)

    # Cada nodo se envuelve con `traced_node` (AGENT-TRACE-01): registra
    # inicio/fin/duración REALES de cada ejecución (incluyendo reintentos) y
    # combina los eventos de decisión que el nodo emite. La topología, la
    # mecánica M7 y la semántica de los updates quedan intactas.
    graph.add_node("supervisor", traced_node("supervisor", make_supervisor_node(chat_model)))
    graph.add_node("researcher", traced_node("researcher", make_researcher_node(retriever)))
    graph.add_node("tutor", traced_node("tutor", make_tutor_node(tutor_model)))
    graph.add_node("validator", traced_node("validator", validator_node))
    graph.add_node("synthesizer", traced_node("synthesizer", synthesizer_node))
    graph.add_node("limited_finish", traced_node("limited_finish", limited_finish_node))

    graph.add_edge(START, "supervisor")
    graph.add_conditional_edges(
        "supervisor",
        route_after_supervisor,
        {
            "researcher": "researcher",
            "tutor": "tutor",
            "validator": "validator",
            "synthesizer": "synthesizer",
            "limited_finish": "limited_finish",
            "END": END,
        },
    )

    # Workers vuelven al supervisor (mecánica M7); el validator decide solo.
    for worker in ("researcher", "tutor"):
        graph.add_edge(worker, "supervisor")

    graph.add_conditional_edges(
        "validator",
        route_after_validator,
        {
            "synthesizer": "synthesizer",
            "researcher": "researcher",
            "tutor": "tutor",
            "limited_finish": "limited_finish",
        },
    )

    graph.add_edge("synthesizer", END)
    graph.add_edge("limited_finish", END)

    return graph


def compile_graph(chat_model=None, retriever=None, tutor_model=None, checkpointer=None):
    """Construye y compila el grafo de estudio.

    `chat_model` (supervisor) permanece None en producción (enrutado
    determinístico, decisión TASK-11); `tutor_model` es el chat model del
    tutor, construido una vez por proceso; `checkpointer` (opcional,
    CHECKPOINTER-01) habilita la persistencia del estado por `thread_id`.
    """
    return build_graph(
        chat_model=chat_model, retriever=retriever, tutor_model=tutor_model
    ).compile(checkpointer=checkpointer)
