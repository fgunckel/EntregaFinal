"""Estudiar (TASK-12): consulta pedagógica contra POST /api/study.

- El usuario autenticado escribe una pregunta; la respuesta real del
  endpoint se presenta con estado, referencias y limitaciones tal cual.
- Sin selección documental: la evidencia la recopila el backend sobre toda
  la biblioteca visible (no se simulan filtros por documento).
- La identidad/permisos los resuelve el backend con el token de sesión;
  la UI nunca envía roles, usuarios ni claves de proveedores (TASK-11).
- Funciones puras (`validation_error`, `describe_error`) separadas para
  poder probarlas sin Streamlit y sin dependencias nuevas.
"""

import streamlit as st

from ada_study.contracts.study import StudyResponse
from ada_study.ui import auth
from ada_study.ui.api_client import ApiError
from ada_study.ui.trace_view import render_trace

# Límite contractual del endpoint (StudyRequest.max_length en routes_study).
MAX_REQUEST_CHARS = 2000

# Bandera de sesión para evitar envíos duplicados durante una petición.
IN_FLIGHT_KEY = "ada_study_in_flight"

# Conversación multi-turno (CHECKPOINTER-03): el identificador vive solo en
# `st.session_state` y se envía en cada petición posterior mientras dure la
# conversación. "Nueva conversación" simplemente lo descarta.
CONVERSATION_KEY = "ada_study_conversation_id"
TURNS_KEY = "ada_study_turns"

# Códigos de sesión vencida/inválida (frontera del backend, ADR-0005).
SESSION_ERROR_CODES = {"AUTH_SESSION_INVALID", "AUTH_SESSION_EXPIRED"}


def validation_error(text: str | None) -> str | None:
    """Validación previa al envío (mismos límites que el backend).

    Función pura, testeable sin Streamlit. Devuelve el mensaje de error o
    None si la consulta es admisible.
    """
    if text is None or not text.strip():
        return "La consulta no puede estar vacía."
    if len(text) > MAX_REQUEST_CHARS:
        return f"La consulta no puede exceder {MAX_REQUEST_CHARS} caracteres."
    return None


def describe_error(exc: ApiError) -> str:
    """Mensaje comprensible por código estable (pura, sin secretos ni trazas)."""
    if exc.error_code in SESSION_ERROR_CODES:
        return "Tu sesión expiró o no es válida. Volvé a iniciar sesión."
    if exc.error_code == "CONVERSATION_NOT_FOUND":
        return "La conversación no está disponible. Iniciá una nueva conversación."
    if exc.error_code == "CONVERSATION_INVALID_ID":
        return "El identificador de conversación no es válido. Iniciá una conversación nueva."
    if exc.status_code == 422:
        return (
            "La consulta fue rechazada: revisá que tenga contenido y no "
            f"exceda {MAX_REQUEST_CHARS} caracteres."
        )
    if exc.status_code == 503:
        return (
            "El servicio de estudio no está disponible en este momento "
            f"({exc.error_code}). Probá de nuevo más tarde."
        )
    return f"No se pudo completar la consulta ({exc.error_code})."


def _render_response(response: StudyResponse) -> None:
    """Muestra exclusivamente lo que trae la respuesta real del endpoint."""
    labels = {
        "complete": "✅ Respuesta con evidencia suficiente",
        "insufficient_evidence": "⚠️ Evidencia insuficiente",
        "limited": "🟡 Respuesta limitada",
    }
    st.subheader(labels.get(response.status, response.status))
    st.markdown(response.explanation)

    if response.references:
        with st.expander(f"Referencias ({len(response.references)})", expanded=True):
            for reference in response.references:
                methods = " + ".join(reference.retrieval_method)
                st.markdown(
                    f"📄 **{reference.title}** — página {reference.page} "
                    f"(recuperado por {methods})"
                )
                st.caption(reference.excerpt)

    if response.status == "insufficient_evidence":
        st.warning(
            "No hay evidencia suficiente en tu biblioteca para responder "
            "con fundamento. Podés subir más material y volver a consultar."
        )
    for gap in response.evidence_gaps:
        st.caption(f"• {gap}")

    # Limitaciones: solo si la respuesta real las declara (ej. fallback
    # determinístico del tutor, TASK-11). La UI nunca las infiere.
    if response.limitations:
        st.info("Limitaciones declaradas: " + " | ".join(response.limitations))

    if response.uses_web_sources:
        st.warning("La respuesta incluye fuentes web (aviso contractual).")

    # Trazabilidad de ESTA consulta (AGENT-TRACE-02): la sección viaja dentro
    # de la propia respuesta (`response.trace`); con trace ausente se informa
    # discretamente y la respuesta sigue visible.
    render_trace(response.trace)


def _render_previous_turns() -> None:
    """Muestra los turnos anteriores de la conversación en curso (sesión).

    Mínimo necesario (CHECKPOINTER-03): lo que se ve es EXACTAMENTE lo que
    devolvió el endpoint en su momento; sin listados ni administración.
    Cada turno conserva SU PROPIA traza (AGENT-TRACE-02 §5): viene dentro del
    objeto `StudyResponse` guardado, nunca se reutiliza el último trace.
    """
    turns = st.session_state.get(TURNS_KEY) or []
    if not turns:
        return
    with st.expander(f"Turnos anteriores ({len(turns)})", expanded=False):
        for turn in turns:
            st.markdown(f"**Vos:** {turn['pregunta']}")
            response = turn["respuesta"]
            st.markdown(f"**ADA:** {response.explanation}")
            render_trace(response.trace)
            st.divider()


def register_turn(session_state, question: str, response: StudyResponse) -> None:
    """Registra el turno SOLO si el backend devolvió `conversation_id`.

    RAG-QUALITY-03: en modo efímero (conversaciones deshabilitadas) el backend
    no devuelve `conversation_id`; acumular turnos igual crearía un historial
    ficticio ("Turnos anteriores") que sugiere una memoria que no existe.
    Con conversación persistida el comportamiento es el de siempre.
    """
    if not response.conversation_id:
        return
    session_state[CONVERSATION_KEY] = response.conversation_id
    turns = session_state.setdefault(TURNS_KEY, [])
    turns.append({"pregunta": question, "respuesta": response})


def render_study() -> None:
    """Formulario de consulta y presentación de la respuesta real."""
    st.title("Estudiar")
    st.caption(
        "Tu pregunta se responde con la evidencia visible que procesa el "
        "backend (toda tu biblioteca con acceso), no un documento puntual."
    )
    client = auth.get_client()
    if client is None:
        st.error("Sesión no encontrada. Volvé a iniciar sesión.")
        return

    # Conversación en curso (CHECKPOINTER-03): mismo conversation_id en cada
    # petición mientras dure la conversación; ADA recuerda los turnos previos.
    conversation_id = st.session_state.get(CONVERSATION_KEY)
    if conversation_id:
        col_title, col_new = st.columns([3, 1])
        with col_new:
            if st.button("Nueva conversación"):
                st.session_state.pop(CONVERSATION_KEY, None)
                st.session_state.pop(TURNS_KEY, None)
                st.rerun()
        st.caption("Conversación activa: ADA recuerda tus preguntas anteriores.")

    _render_previous_turns()

    in_flight = st.session_state.get(IN_FLIGHT_KEY, False)
    question = st.text_area(
        "Tu pregunta",
        value="",
        max_chars=MAX_REQUEST_CHARS,
        disabled=in_flight,
        key="ada_study_request",
        help=f"Entre 1 y {MAX_REQUEST_CHARS} caracteres.",
    )
    st.caption(f"{len(question)}/{MAX_REQUEST_CHARS} caracteres")

    if not st.button("Consultar", type="primary", disabled=in_flight):
        return

    error = validation_error(question)
    if error:
        st.error(error)
        return
    st.session_state[IN_FLIGHT_KEY] = True
    try:
        response = client.study(question.strip(), conversation_id)
    except ApiError as exc:
        st.error(describe_error(exc))
        if exc.error_code in SESSION_ERROR_CODES:
            # Flujo existente de sesión vencida: descartar token y volver al
            # login (el backend es la frontera de autenticación, TASK-08).
            auth.clear_session()
            st.rerun()
        if exc.error_code in {"CONVERSATION_NOT_FOUND", "CONVERSATION_INVALID_ID"}:
            # Conversación vencida/inválida: descartarla para permitir iniciar
            # una nueva en el próximo envío (el histórico vive en el backend).
            st.session_state.pop(CONVERSATION_KEY, None)
        return
    finally:
        # Limpia la bandera en todo camino (st.rerun lanza una excepción
        # que igual pasa por acá).
        st.session_state[IN_FLIGHT_KEY] = False

    # Continuidad conversacional (RAG-QUALITY-03): el turno SOLO se registra
    # cuando el backend devuelve conversation_id (conversaciones habilitadas);
    # el modo sin conversaciones no acumula historial ficticio.
    register_turn(st.session_state, question.strip(), response)

    st.divider()
    _render_response(response)

