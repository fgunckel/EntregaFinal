"""Vista de trazabilidad por consulta (AGENT-TRACE-02).

Consume EXCLUSIVAMENTE `StudyResponse.trace` (AGENT-TRACE-01): los estados,
decisiones y resultados mostrados provienen de los eventos reales; no hay
datos simulados ni inferencias sobre ejecuciones que no estén registradas.

Estructura (patrón de las páginas UI): funciones puras (`build_agent_views`,
`trace_notes`) separadas del render Streamlit (`render_trace`) para poder
probarlas sin runtime, más AppTest sobre el wrapper de la página Estudiar.

Reglas de estado (sin inventar):
- Un agente está "Completado" solo si su ÚLTIMA ejecución registró `node_end`
  sin estados de error/advertencia (los intentos previos quedan en detalle).
- "En ejecución": su última ejecución tiene `node_start` sin `node_end`
  (el flujo se cortó ahí).
- "Omitido": el turno TERMINÓ (trace.status == "completed") y el agente no
  tiene ningún evento: la ejecución terminó sin que interviniera.
- "Sin información disponible": no hay eventos y no puede afirmarse nada
  (traza fallida); nunca se presenta como completado ni omitido.
"""

from dataclasses import dataclass

import streamlit as st

from ada_study.contracts.trace import Trace, TraceEvent

# Orden y nombres de los cinco agentes (AGENT-TRACE-02 §4.2).
AGENT_ORDER = ("supervisor", "researcher", "tutor", "validator", "synthesizer")

AGENT_NAMES = {
    "supervisor": "Supervisor",
    "researcher": "Researcher",
    "tutor": "Tutor",
    "validator": "Validator",
    "synthesizer": "Synthesizer",
}

# Responsabilidades informativas y FIJAS (AGENT-TRACE-02 §3): describen el
# rol de cada agente, nunca el resultado de una ejecución puntual.
AGENT_ROLES = {
    "supervisor": (
        "Orquesta el flujo, decide el próximo paso y controla las condiciones "
        "de finalización o reintento."
    ),
    "researcher": (
        "Busca evidencia documental, recupera fragmentos y evalúa su pertinencia."
    ),
    "tutor": "Construye la explicación pedagógica utilizando la evidencia disponible.",
    "validator": (
        "Evalúa la respuesta y sus referencias, identifica incumplimientos y "
        "determina si requiere corrección."
    ),
    "synthesizer": "Prepara y publica la respuesta final con sus referencias.",
}

TRACE_TITLE = "¿Cómo se construyó esta respuesta?"
TRACE_SUBTITLE = "Trazabilidad de esta consulta."
TRACE_UNAVAILABLE = "La trazabilidad de esta consulta no está disponible."
DETAIL_LABEL = "Ver detalle"

# Estados visuales (AGENT-TRACE-02 §4.2), derivados SOLO de los eventos.
STATE_LABELS = {
    "completed": "✅ Completado",
    "running": "🔄 En ejecución",
    "skipped": "⏭️ Omitido",
    "warning": "⚠️ Advertencia",
    "error": "❌ Error",
    "unknown": "❔ Sin información disponible",
}

# Modo real de ejecución del tutor (evento decision.details.mode).
TUTOR_MODE_LABELS = {
    "llm": "Redacción con LLM",
    "deterministic_composition": "Composición determinística (sin LLM configurado)",
    "deterministic_fallback": "Fallback determinístico (el LLM no pudo redactar)",
    "insufficient_evidence": "Evidencia insuficiente: no se invocó al LLM",
}

# Controles del validator (nombres reales de graph/validator.py).
CHECK_LABELS = {
    "evidence_contract": "Contrato de evidencia",
    "response_contract": "Contrato de la respuesta",
    "cited_subset_of_evidence": "Citas dentro de la evidencia",
    "complete_has_references": "Respuesta completa con referencias",
    "complete_has_pertinent_evidence": "Evidencia pertinente suficiente",
    "gaps_acknowledged": "Insuficiencia reconocida",
}
CHECK_RESULT_LABELS = {"pass": "OK", "fail": "Falló", "skipped": "No aplicó"}

# Salida segura sin respuesta fundamentada (no es uno de los cinco agentes).
LIMITED_FINISH_NODE = "limited_finish"


@dataclass(frozen=True)
class AgentTraceView:
    """Vista resumida + detalle de UN agente para una consulta.

    - `state`: estado real derivado de los eventos (ver reglas del módulo).
    - `summary`: último evento con contenido (decisión/resultado/aviso/error).
    - `executions`: ejecuciones reales del nodo (reintentos incluidos).
    - `duration_ms`: suma de duraciones MEDIDAS de sus `node_end`.
    - `details`: líneas markdown del detalle (solo datos realmente presentes).
    """

    node: str
    name: str
    responsibility: str
    state: str
    summary: str | None
    duration_ms: float | None
    executions: int
    details: list[str]


def _events_for(trace: Trace, node: str) -> list[TraceEvent]:
    return [event for event in trace.events if event.node == node]


def _agent_state(events: list[TraceEvent], trace_status: str) -> str:
    """Estado del agente según su ÚLTIMA ejecución (sin inferencias).

    La última ejecución define el estado: en un reintento aprobado (p. ej.
    validator que rechaza y luego aprueba) el veredicto final es el real; los
    intentos anteriores quedan en el detalle (`Intento 1`, `Intento 2`, ...).
    """
    if not events:
        # Turno terminado sin eventos del agente: no intervino (omitido).
        # En una traza fallida la ausencia no prueba nada: sin información.
        return "skipped" if trace_status == "completed" else "unknown"
    last_start = max(
        (index for index, event in enumerate(events) if event.event_type == "node_start"),
        default=0,
    )
    last_execution = events[last_start:]
    ends = sum(1 for event in last_execution if event.event_type == "node_end")
    if not ends:
        # Ejecución cortada: quedó iniciada y sin cierre registrado.
        return "running"
    if any(event.status == "error" for event in last_execution):
        return "error"
    if any(event.status == "warning" for event in last_execution):
        return "warning"
    return "completed"


def _summary_of(events: list[TraceEvent]) -> str | None:
    """Resumen del último evento con contenido (decisión/resultado/aviso)."""
    for event in reversed(events):
        if event.event_type in {"decision", "result", "warning", "error"}:
            return event.summary
    return None


def _format_duration(duration_ms: float) -> str:
    if duration_ms >= 1000:
        return f"{duration_ms / 1000:.1f} s"
    return f"{duration_ms:.0f} ms"


# ---------------------------------------------------------------------------
# Detalle por agente: SOLO datos presentes en los eventos (AGENT-TRACE-02 §4.3)
# ---------------------------------------------------------------------------


def _supervisor_details(events: list[TraceEvent]) -> list[str]:
    lines: list[str] = []
    for event in events:
        if event.event_type != "decision":
            continue
        next_agent = event.details.get("next_agent")
        reason = event.details.get("reason")
        if next_agent:
            target = AGENT_NAMES.get(next_agent, next_agent)
            lines.append(f"- Decisión: continuar con **{target}**")
        if reason:
            lines.append(f"  - Motivo: {reason}")
        steps = event.details.get("steps")
        if steps is not None:
            lines.append(f"  - Paso del flujo: {steps}")
    return lines


def _researcher_details(events: list[TraceEvent]) -> list[str]:
    lines: list[str] = []
    for event in events:
        details = event.details
        if event.event_type == "decision":
            query = details.get("query")
            if query:
                lines.append(f"- Consulta del estudiante: «{query}»")
            if details.get("contextual_reformulation"):
                retrieval_query = details.get("retrieval_query")
                if retrieval_query:
                    lines.append(
                        f"- Consulta de búsqueda (con tema anterior): «{retrieval_query}»"
                    )
            terms = details.get("thematic_terms") or details.get("target_terms")
            if terms:
                lines.append(f"- Términos utilizados: {', '.join(terms)}")
            restricted = details.get("source_restriction_terms")
            if restricted:
                lines.append(
                    f"- Restricción de fuentes (no acredita pertinencia): "
                    f"{', '.join(restricted)}"
                )
            retrieved = details.get("retrieved_chunks")
            if retrieved is not None:
                lines.append(f"- Fragmentos recuperados por búsqueda: {retrieved}")
            for document in details.get("documents") or []:
                title = document.get("title")
                if title:
                    page = document.get("page")
                    lines.append(f"  - Recuperado: **{title}** (página {page})")
        elif event.event_type == "result":
            accepted = details.get("accepted") or []
            rejected = details.get("rejected") or []
            lines.append(f"- Fragmentos aceptados: {len(accepted)}")
            lines.append(f"- Fragmentos rechazados: {len(rejected)}")
            for item in rejected:
                title = item.get("title")
                reason = item.get("reason")
                if title and reason:
                    page = item.get("page")
                    lines.append(f"  - **{title}** (página {page}): {reason}")
            for gap in details.get("evidence_gaps") or []:
                lines.append(f"- Razón de insuficiencia: {gap}")
    return lines


def _tutor_details(events: list[TraceEvent]) -> list[str]:
    lines: list[str] = []
    for event in events:
        details = event.details
        if event.event_type == "decision":
            mode = details.get("mode")
            if mode in TUTOR_MODE_LABELS:
                lines.append(f"- Modo de ejecución: {TUTOR_MODE_LABELS[mode]}")
            evidence_count = details.get("evidence_count")
            if evidence_count is not None:
                lines.append(f"- Evidencias recibidas: {evidence_count}")
        elif event.event_type in {"result", "warning", "error"} and "provider" in details:
            # Invocación real al LLM (datos técnicos, solo en el detalle).
            lines.append(
                f"- Proveedor: {details['provider']} · Modelo: {details.get('model')}"
            )
            duration = details.get("duration_ms")
            if duration is not None:
                lines.append(f"- Duración de la generación: {_format_duration(duration)}")
            input_tokens = details.get("input_tokens")
            output_tokens = details.get("output_tokens")
            if input_tokens is not None or output_tokens is not None:
                lines.append(
                    f"- Tokens: {input_tokens} de entrada / {output_tokens} de salida"
                )
            output_chars = details.get("output_chars")
            if output_chars is not None:
                lines.append(f"- Caracteres generados: {output_chars}")
        if event.event_type == "error":
            error = f" (tipo: {event.error})" if event.error else ""
            lines.append(f"- Error en la generación{error}: {event.summary}")
        elif event.event_type == "warning" and "provider" not in details:
            lines.append(f"- Advertencia: {event.summary}")
    return lines


def _validator_details(events: list[TraceEvent]) -> list[str]:
    lines: list[str] = []
    results = [event for event in events if event.event_type == "result"]
    for attempt, event in enumerate(results, start=1):
        approved = bool(event.details.get("is_valid"))
        prefix = f"Intento {attempt}" if len(results) > 1 else "Resultado"
        verdict = "aprobada" if approved else "rechazada"
        lines.append(f"- {prefix}: validación **{verdict}**")
        for check, value in (event.details.get("checks") or {}).items():
            label = CHECK_LABELS.get(check, check)
            result = CHECK_RESULT_LABELS.get(value, value)
            lines.append(f"  - {label}: {result}")
        for message in event.details.get("errors") or []:
            lines.append(f"  - Error: {message}")
        for message in event.details.get("warnings") or []:
            lines.append(f"  - Advertencia: {message}")
        if event.details.get("repair_requested"):
            repair_agent = event.details.get("repair_agent") or "agente responsable"
            target = AGENT_NAMES.get(repair_agent, repair_agent)
            lines.append(f"  - Corrección solicitada a: **{target}**")
        retries = event.details.get("retries")
        if retries:
            lines.append(f"  - Reintentos registrados: {retries}")
    return lines


def _synthesizer_details(events: list[TraceEvent]) -> list[str]:
    lines: list[str] = []
    for event in events:
        if event.event_type != "decision":
            continue
        approved = event.details.get("validation_approved")
        if approved is not None:
            lines.append(
                "- Publicación aprobada por el validador: " + ("Sí" if approved else "No")
            )
        published = event.details.get("published_status")
        if published:
            lines.append(f"- Estado de la respuesta publicada: {published}")
        references = event.details.get("references_count")
        if references is not None:
            lines.append(f"- Referencias incluidas: {references}")
        if event.details.get("history_consolidated"):
            lines.append("- Incorporada al historial de la conversación: Sí")
    return lines


_DETAIL_BUILDERS = {
    "supervisor": _supervisor_details,
    "researcher": _researcher_details,
    "tutor": _tutor_details,
    "validator": _validator_details,
    "synthesizer": _synthesizer_details,
}


# ---------------------------------------------------------------------------
# Vista: Trace -> resumen por agente + notas del panel
# ---------------------------------------------------------------------------


def build_agent_views(trace: Trace) -> list[AgentTraceView]:
    """Secuencia vertical de los cinco agentes con su estado REAL."""
    views: list[AgentTraceView] = []
    for node in AGENT_ORDER:
        events = _events_for(trace, node)
        executions = sum(1 for event in events if event.event_type == "node_start")
        measured = [event.duration_ms for event in events if event.event_type == "node_end"]
        duration_ms = round(sum(measured), 3) if measured else None
        views.append(
            AgentTraceView(
                node=node,
                name=AGENT_NAMES[node],
                responsibility=AGENT_ROLES[node],
                state=_agent_state(events, trace.status),
                summary=_summary_of(events),
                duration_ms=duration_ms,
                executions=executions,
                details=_DETAIL_BUILDERS[node](events),
            )
        )
    return views


def trace_notes(trace: Trace) -> list[tuple[str, str]]:
    """Notas del panel: salida segura y error de ejecución (§6 B/D)."""
    notes: list[tuple[str, str]] = []
    for event in trace.events:
        if event.node == LIMITED_FINISH_NODE and event.event_type == "warning":
            notes.append(("warning", event.summary))
    if trace.status == "failed" and trace.error:
        notes.append(
            (
                "error",
                "La ejecución terminó con un error "
                f"({trace.error}); los agentes en «En ejecución» no finalizaron.",
            )
        )
    return notes


# ---------------------------------------------------------------------------
# Render Streamlit (AGENT-TRACE-02 §4/§7): desplegable, jerárquico, compacto
# ---------------------------------------------------------------------------


def render_agent_view(view: AgentTraceView) -> None:
    """Fila del agente: nombre, estado real, resumen y detalle expandido."""
    header = f"**{view.name}** · {STATE_LABELS[view.state]}"
    if view.duration_ms is not None:
        header += f" · {_format_duration(view.duration_ms)}"
    st.markdown(header)
    st.caption(view.responsibility)
    if view.summary:
        st.markdown(view.summary)
    if view.details:
        with st.expander(DETAIL_LABEL):
            for line in view.details:
                st.markdown(line)


def render_trace(trace: Trace | None) -> None:
    """Sección «¿Cómo se construyó esta respuesta?» de UNA consulta.

    - `trace is None`: respuesta visible, aviso discreto, nada inventado (§6 E).
    - Los identificadores se muestran con etiquetas distintas para que
      `trace_id` y el hilo del checkpointer (`conversation_id`) no se
      confundan entre sí ni con el UUID de conversación de la app (§5).
    """
    if trace is None:
        st.caption(TRACE_UNAVAILABLE)
        return
    with st.expander(TRACE_TITLE, expanded=False):
        st.caption(TRACE_SUBTITLE)
        for view in build_agent_views(trace):
            render_agent_view(view)
        for kind, message in trace_notes(trace):
            if kind == "error":
                st.error(message)
            else:
                st.warning(message)
        identifiers = f"ID de traza: `{trace.trace_id}`"
        if trace.conversation_id:
            identifiers += f" · Hilo del checkpointer: `{trace.conversation_id}`"
        st.caption(identifiers)
