"""Auditoría (TASK-08): eventos append-only (moderator/admin).

- Muestra lo que devuelve GET /api/audit/events con filtros reales.
- Append-only: la UI no ofrece editar ni borrar eventos (no existe tal
  operación en el backend).
- UI-REDESIGN-03: resultado como píldora semántica (ok/denied), IDs
  abreviados (`short_ref`; el backend no se modifica), paginación
  client-side (20 por página) y estado vacío consistente.
"""

import streamlit as st

from ada_study.ui import auth
from ada_study.ui.api_client import ApiError
from ada_study.ui.components import (
    empty_state,
    page_header,
    semantic_pill,
    short_ref,
)

EVENTS_PER_PAGE = 20

# Resultado del evento -> (etiqueta, tono de pastilla). "ok" y "denied" son
# los valores reales que escribe el backend (identity_service); cualquier
# otro valor desconocido se muestra neutral, sin inventar semántica.
RESULT_TONES: dict[str, tuple[str, str]] = {
    "ok": ("Exitoso", "ok"),
    "denied": ("Denegado", "danger"),
}


def describe_audit_error(exc: ApiError) -> str:
    """Mensaje controlado para fallos al cargar eventos (pura, testeable)."""
    if exc.error_code == "AUTH_FORBIDDEN":
        return "Tu rol no tiene acceso a la auditoría."
    return f"No se pudieron cargar los eventos ({exc.error_code})."


def _render_filters(client) -> list:
    """Filtros + carga de eventos reales (GET /api/audit/events)."""
    col1, col2, col3 = st.columns(3)
    with col1:
        entity_type = st.text_input("Tipo de entidad (opcional)", key="audit_entity_type")
    with col2:
        entity_id = st.text_input("ID de entidad (opcional)", key="audit_entity_id")
    with col3:
        limit = st.number_input("Límite", min_value=1, max_value=500, value=100)

    try:
        return client.audit_events(
            entity_type=entity_type or None,
            entity_id=entity_id or None,
            limit=int(limit),
        )
    except ApiError as exc:
        st.error(describe_audit_error(exc))
        return []


def _render_event(event) -> None:
    """Fila de evento legible: acción legible + pastilla + IDs abreviados."""
    with st.container(border=True):
        col1, col2 = st.columns([3, 1])
        with col1:
            st.markdown(f"**{event.action}**")
            st.caption(
                f"Entidad: {event.entity_type} / {short_ref(event.entity_id)} · "
                f"Actor: {short_ref(event.actor_id)}"
            )
        with col2:
            label, tone = RESULT_TONES.get(event.result, (event.result, "neutral"))
            semantic_pill(label, tone, technical=True)
        st.caption(f"Fecha: {event.created_at}")
        if event.reason:
            st.caption(f"Motivo: {event.reason}")


def _pagination(total: int) -> int:
    """Paginación client-side (el backend no expone cursor de auditoría)."""
    page = st.session_state.get("ada_audit_page", 0)
    max_page = max((len(total) - 1) // EVENTS_PER_PAGE, 0)
    page = min(page, max_page)
    prev_col, info_col, next_col = st.columns([1, 3, 1])
    with prev_col:
        if st.button("← Anterior", disabled=page <= 0, key="audit_prev_page"):
            st.session_state["ada_audit_page"] = page - 1
            st.rerun()
    with info_col:
        first = page * EVENTS_PER_PAGE + 1
        last = min(first + EVENTS_PER_PAGE - 1, len(total))
        st.caption(f"Eventos {first}–{last} de {len(total)}")
    with next_col:
        if st.button("Siguiente →", disabled=page >= max_page, key="audit_next"):
            st.session_state["ada_audit_page"] = page + 1
            st.rerun()
    return page


def render_audit() -> None:
    """Eventos de auditoría reales (moderator/admin)."""
    client = auth.get_client()
    if client is None:
        st.error("Sesión no encontrada. Volvé a iniciar sesión.")
        return

    events = _render_filters(client)
    page_header("REGISTRO DE ACTIVIDAD", "Auditoría")
    if not events:
        empty_state(
            "Sin eventos para el filtro indicado",
            "La auditoría registra cada acción del sistema (append-only): "
            "ajustá los filtros para ver más historia.",
        )
        return

    page = _pagination(events)
    start = page * EVENTS_PER_PAGE
    for event in events[start : start + EVENTS_PER_PAGE]:
        _render_event(event)
