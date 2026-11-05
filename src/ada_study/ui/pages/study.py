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
from ada_study.ui.components import answer_block, empty_state, page_header

# Límite contractual del endpoint (StudyRequest.max_length en routes_study).
MAX_REQUEST_CHARS = 2000

# Bandera de sesión para evitar envíos duplicados durante una petición.
IN_FLIGHT_KEY = "ada_study_in_flight"

# Conversación multi-turno (CHECKPOINTER-03): el identificador vive en
# `st.session_state` y se envía en cada petición posterior mientras dure la
# conversación. "Nueva conversación" simplemente lo descarta. FIX-TC06 §4.5:
# además se refleja en `st.query_params` para que un F5 del navegador — que
# reinicia `st.session_state` — NO inicie una conversación nueva: el
# identificador vuelve del parámetro de URL y el backend verifica su
# titularidad (404 → se descarta). "Nueva conversación" limpia el parámetro.
CONVERSATION_KEY = "ada_study_conversation_id"
TURNS_KEY = "ada_study_turns"
CONVERSATION_PARAM = "conversacion"

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


def _render_turns(turns) -> None:
    """Historial de la conversación como chat (UI-REDESIGN-03).

    Reemplaza al expander «Turnos anteriores»: cada turno se ve como en una
    conversación real (pregunta del usuario → respuesta de ADA en su bloque
    de lectura). Lo que se ve es EXACTAMENTE lo que devolvió el endpoint en
    su momento; sin listados ni administración. Cada turno conserva SU
    PROPIA traza (AGENT-TRACE-02 §5): viaja dentro del objeto `StudyResponse`
    guardado, nunca se reutiliza el último trace.
    """
    for turn in turns:
        with st.chat_message("user"):
            st.markdown(turn["pregunta"])
        with st.chat_message("assistant"):
            answer_block(turn["respuesta"])


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


def resolve_conversation_id(session_state, query_params) -> str | None:
    """Conversación activa: sesión primero, query params como respaldo (§4.5).

    Pura y testeable. Sobrevive a un F5 del navegador (nueva sesión de
    Streamlit): el identificador vuelve del parámetro `?conversacion=…`.
    La titularidad SIEMPRE la verifica el backend; un id ajeno/expirado
    devuelve 404 y el llamador descarta ambos (sesión y parámetro).
    """
    conversation_id = session_state.get(CONVERSATION_KEY)
    if conversation_id:
        return conversation_id
    restored = query_params.get(CONVERSATION_PARAM)
    if not restored:
        return None
    session_state[CONVERSATION_KEY] = restored
    return restored


def _conversation_actions() -> None:
    """Acción del encabezado: iniciar una conversación nueva (CHECKPOINTER-03)."""
    if st.button("Nueva conversación", key="ada_new_conversation"):
        st.session_state.pop(CONVERSATION_KEY, None)
        st.session_state.pop(TURNS_KEY, None)
        if CONVERSATION_PARAM in st.query_params:
            del st.query_params[CONVERSATION_PARAM]
        st.rerun()


def _handle_submission(client, question: str, conversation_id: str | None) -> None:
    """Envío de la consulta y render del turno nuevo (flujo existente).

    Conserva: validación previa, guard de doble envío, mapeo de errores,
    limpieza de sesión/conversación y registro de turno solo con
    `conversation_id` real (RAG-QUALITY-03).
    """
    error = validation_error(question)
    if error:
        st.error(error)
        return
    question = question.strip()
    st.session_state[IN_FLIGHT_KEY] = True
    try:
        response = client.study(question, conversation_id)
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
            if CONVERSATION_PARAM in st.query_params:
                del st.query_params[CONVERSATION_PARAM]
        return
    finally:
        # Limpia la bandera en todo camino (st.rerun lanza una excepción
        # que igual pasa por acá).
        st.session_state[IN_FLIGHT_KEY] = False

    # Continuidad conversacional (RAG-QUALITY-03): el turno SOLO se registra
    # cuando el backend devuelve conversation_id (conversaciones habilitadas);
    # el modo sin conversaciones no acumula historial ficticio.
    register_turn(st.session_state, question, response)
    # FIX-TC06 §4.5: el identificador real también viaja en la URL para que
    # un F5 o una reconexión recuperen la MISMA conversación (el backend
    # verifica titularidad; nunca se confía en el parámetro sin validar).
    if response.conversation_id:
        st.query_params[CONVERSATION_PARAM] = response.conversation_id

    # El turno recién creado se muestra sin esperar un rerun: la conversación
    # se ve crecer inmediatamente (comportamiento de chat estándar).
    with st.chat_message("user"):
        st.markdown(question)
    with st.chat_message("assistant"):
        answer_block(response)


def render_study() -> None:
    """Chat de estudio: pregunta libre, respuesta con evidencia real."""
    client = auth.get_client()
    if client is None:
        st.error("Sesión no encontrada. Volvé a iniciar sesión.")
        return

    # Conversación en curso (CHECKPOINTER-03): mismo conversation_id en cada
    # petición mientras dure la conversación; ADA recuerda los turnos previos.
    # FIX-TC06 §4.5: un F5 (nueva sesión) restaura el identificador desde el
    # parámetro de URL en lugar de iniciar una conversación nueva.
    conversation_id = resolve_conversation_id(st.session_state, st.query_params)

    # st.chat_input se declara primero y se ancla al pie de la página;
    # `disabled` durante la petición previene el doble envío (mismo guard
    # que tenía el botón «Consultar»).
    in_flight = st.session_state.get(IN_FLIGHT_KEY, False)
    question = st.chat_input(
        f"Preguntale algo a ADA (hasta {MAX_REQUEST_CHARS} caracteres)…",
        key="ada_study_request",
        max_chars=MAX_REQUEST_CHARS,
        disabled=in_flight,
    )

    if conversation_id:
        page_header("CONVERSACIÓN ACTIVA", "Estudiar", actions=_conversation_actions)
        st.caption("ADA recuerda tus preguntas anteriores en esta conversación.")
    else:
        page_header("ESTUDIO", "Estudiar")
        st.caption(
            "Tu pregunta se responde con la evidencia visible que procesa el "
            "backend (toda tu biblioteca con acceso), no un documento puntual."
        )

    turns = st.session_state.get(TURNS_KEY) or []
    if turns:
        _render_turns(turns)
    else:
        empty_state(
            "Empezá una conversación",
            "Preguntale algo sobre tu material: ADA responde con evidencia "
            "de tu biblioteca y muestra de dónde salió cada dato.",
        )

    if not question:
        return

    _handle_submission(client, question, conversation_id)

