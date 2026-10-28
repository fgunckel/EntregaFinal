"""Auditoría (TASK-08): eventos append-only (moderator/admin).

- Muestra lo que devuelve GET /api/audit/events con filtros reales.
- Append-only: la UI no ofrece editar ni borrar eventos (no existe tal
  operación en el backend).
"""

import streamlit as st

from ada_study.ui import auth
from ada_study.ui.api_client import ApiError


def render_audit() -> None:
    """Eventos de auditoría reales (moderator/admin)."""
    st.title("Auditoría")
    client = auth.get_client()
    if client is None:
        st.error("Sesión no encontrada. Volvé a iniciar sesión.")
        return

    col1, col2, col3 = st.columns(3)
    with col1:
        entity_type = st.text_input("Tipo de entidad (opcional)", key="audit_entity_type")
    with col2:
        entity_id = st.text_input("ID de entidad (opcional)", key="audit_entity_id")
    with col3:
        limit = st.number_input("Límite", min_value=1, max_value=500, value=100)

    try:
        events = client.audit_events(
            entity_type=entity_type or None,
            entity_id=entity_id or None,
            limit=int(limit),
        )
    except ApiError as exc:
        if exc.error_code == "AUTH_FORBIDDEN":
            st.error("Tu rol no tiene acceso a la auditoría.")
        else:
            st.error(f"No se pudieron cargar los eventos ({exc.error_code}).")
        return

    if not events:
        st.info("No hay eventos de auditoría para el filtro indicado.")
        return

    for event in events:
        with st.container(border=True):
            st.markdown(f"`{event.action}` — {event.entity_type}/{event.entity_id}")
            st.caption(
                f"Actor: {event.actor_id or '—'} · Resultado: {event.result} · "
                f"Fecha: {event.created_at}"
            )
            if event.reason:
                st.caption(f"Motivo: {event.reason}")
