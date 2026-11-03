"""Moderación (TASK-08): cola real + decisiones con motivo obligatorio.

- Solo visible para moderator/admin (el backend igual aplica la frontera:
  la UI solo oculta lo que el rol no puede hacer).
- Motivo obligatorio en reject/withdraw (Spec 06 §102): la UI lo exige
  antes de llamar; el backend lo revalida.
"""

import streamlit as st

from ada_study.ui import auth
from ada_study.ui.api_client import ApiError


def describe_queue_error(exc: ApiError) -> str:
    """Mensaje controlado para fallos al cargar la cola (pura, testeable).

    Muestra el código estable (ADR-0005) sin exponer el detalle interno
    del backend (patrón de `study.describe_error`).
    """
    if exc.error_code == "AUTH_FORBIDDEN":
        return "Tu rol no tiene acceso a la cola de moderación."
    return f"No se pudo cargar la cola ({exc.error_code})."


def render_moderation() -> None:
    """Cola de moderación con decisiones reales (moderator/admin)."""
    st.title("Moderación")
    client = auth.get_client()
    if client is None:
        st.error("Sesión no encontrada. Volvé a iniciar sesión.")
        return

    try:
        queue = client.moderation_queue()
    except ApiError as exc:
        st.error(describe_queue_error(exc))
        return

    if not queue:
        st.info("No hay documentos pendientes de decisión.")
        return

    for document in queue:
        with st.container(border=True):
            st.markdown(f"**{document.filename}** — {document.editorial_status}")
            if document.subject:
                st.caption(f"Materia: {document.subject}")
            st.caption(f"Autor: {document.uploaded_by}")

            col1, col2, col3 = st.columns(3)
            with col1:
                if st.button("✅ Aprobar", key=f"approve_{document.document_id}"):
                    _decide(client, document.document_id, "approve", None)
            with col2:
                reason_reject = st.text_input(
                    "Motivo del rechazo", key=f"reject_reason_{document.document_id}"
                )
                if st.button("❌ Rechazar", key=f"reject_{document.document_id}"):
                    if not reason_reject:
                        st.warning("El motivo es obligatorio para rechazar.")
                    else:
                        _decide(client, document.document_id, "reject", reason_reject)
            with col3:
                reason_withdraw = st.text_input(
                    "Motivo del retiro", key=f"withdraw_reason_{document.document_id}"
                )
                if st.button("⚪ Retirar", key=f"withdraw_{document.document_id}"):
                    if not reason_withdraw:
                        st.warning("El motivo es obligatorio para retirar.")
                    else:
                        _decide(client, document.document_id, "withdraw", reason_withdraw)


def _decide(client, document_id: str, action: str, reason: str | None) -> None:
    """Decisión real de moderación (el backend audita y aplica reglas)."""
    try:
        updated = client.decide(document_id, action, reason)
    except ApiError as exc:
        if exc.error_code == "MOD_REASON_REQUIRED":
            st.warning("El motivo es obligatorio para esta acción.")
        elif exc.error_code == "MOD_INVALID_TRANSITION":
            st.error("La transición editorial no es válida desde el estado actual.")
        else:
            st.error(f"Decisión rechazada ({exc.error_code}).")
        return
    st.success(f"Decisión aplicada: {updated['editorial_status']}.")
    st.rerun()
