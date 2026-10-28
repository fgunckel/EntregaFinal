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

# Límite contractual del endpoint (StudyRequest.max_length en routes_study).
MAX_REQUEST_CHARS = 2000

# Bandera de sesión para evitar envíos duplicados durante una petición.
IN_FLIGHT_KEY = "ada_study_in_flight"

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
        response = client.study(question.strip())
    except ApiError as exc:
        st.error(describe_error(exc))
        if exc.error_code in SESSION_ERROR_CODES:
            # Flujo existente de sesión vencida: descartar token y volver al
            # login (el backend es la frontera de autenticación, TASK-08).
            auth.clear_session()
            st.rerun()
        return
    finally:
        # Limpia la bandera en todo camino (st.rerun lanza una excepción
        # que igual pasa por acá).
        st.session_state[IN_FLIGHT_KEY] = False
    st.divider()
    _render_response(response)

