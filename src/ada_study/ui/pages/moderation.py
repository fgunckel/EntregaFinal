"""Moderación (TASK-08): cola real + decisiones con motivo obligatorio.

- Solo visible para moderator/admin (el backend igual aplica la frontera:
  la UI solo oculta lo que el rol no puede hacer).
- Motivo obligatorio en reject/withdraw (Spec 06 §102): la UI lo exige
  antes de llamar; el backend lo revalida.
- UI-REDESIGN-02/03: estado editorial como píldora del diccionario único;
  el autor se muestra como UUID abreviado (el contrato solo trae
  `uploaded_by`, el backend NO se modifica). El formulario es CONTEXTUAL:
  un solo selector de decisión y el campo de motivo aparece solo cuando
  corresponde; rechazar/retirar exigen confirmación explícita.
"""

import logging

import streamlit as st

from ada_study.ui import auth
from ada_study.ui.api_client import ApiError
from ada_study.ui.components import (
    confirm_destructive,
    empty_state,
    error_box,
    page_header,
    short_ref,
    status_pill,
)
from ada_study.ui.theme import editorial_label

logger = logging.getLogger("ada_study.ui")

# Decisiones del selector (valor interno -> etiqueta de usuario).
DECISION_OPTIONS = [
    ("approve", "Aprobar"),
    ("reject", "Rechazar"),
    ("withdraw", "Retirar"),
]
# Decisiones que alteran/cancelan el trabajo del autor: motivo + confirmación.
DESTRUCTIVE_DECISIONS = {"reject", "withdraw"}


def describe_queue_error(exc: ApiError) -> str:
    """Mensaje controlado para fallos al cargar la cola (pura, testeable).

    Muestra el código estable (ADR-0005) sin exponer el detalle interno
    del backend (patrón de `study.describe_error`).
    """
    if exc.error_code == "AUTH_FORBIDDEN":
        return "Tu rol no tiene acceso a la cola de moderación."
    return f"No se pudo cargar la cola ({exc.error_code})."


def _decision_form(client, document) -> None:
    """Formulario contextual de decisión para un documento de la cola.

    Un SOLO selector de decisión (antes: dos inputs siempre visibles por
    documento). El motivo aparece SOLO para rechazar/retirar (Spec 06 §102)
    y esas decisiones exigen confirmación explícita; aprobar no la necesita.
    """
    decision = st.selectbox(
        "Decisión",
        DECISION_OPTIONS,
        format_func=lambda pair: pair[1],
        key=f"decision_{document.document_id}",
    )
    action, _ = decision
    reason: str | None = None
    if action in DESTRUCTIVE_DECISIONS:
        reason = st.text_input(
            "Motivo (obligatorio)",
            key=f"decision_reason_{document.document_id}",
        )
        confirmed = confirm_destructive(
            "Confirmo la decisión sobre este documento",
            key=f"ada_confirm_moderation_{document.document_id}",
        )
    else:
        confirmed = True

    if st.button(
        "Aplicar decisión",
        type="primary",
        disabled=not confirmed,
        key=f"apply_{document.document_id}",
    ):
        if action != "approve" and not (reason and reason.strip()):
            st.warning("El motivo es obligatorio para esta decisión.")
            return
        _decide(client, document.document_id, action, reason)


def render_moderation() -> None:
    """Cola de moderación con decisiones reales (moderator/admin)."""
    client = auth.get_client()
    if client is None:
        st.error("Sesión no encontrada. Volvé a iniciar sesión.")
        return

    try:
        queue = client.moderation_queue()
    except ApiError as exc:
        st.error(describe_queue_error(exc))
        return

    page_header("COLA DE DECISIÓN", "Moderación")
    if not queue:
        empty_state(
            "No hay documentos pendientes",
            "Cuando un autor envíe un borrador a revisión, va a aparecer acá "
            "para aprobarlo, rechazarlo o retirarlo.",
        )
        return

    for document in queue:
        with st.container(border=True):
            col1, col2 = st.columns([3, 1])
            with col1:
                st.markdown(f"**{document.filename}**")
                if document.subject:
                    st.caption(f"Materia: {document.subject}")
                # Autor: el contrato solo trae uploaded_by (UUID) → abreviado
                # (decisión aprobada en UI-REDESIGN-02 §1; sin cambio de backend).
                st.caption(f"Autor: {short_ref(document.uploaded_by)}")
            with col2:
                status_pill(document.editorial_status)
            _decision_form(client, document)


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
            error_box(
                "No se pudo aplicar la decisión. Intentá de nuevo en unos minutos.",
                exc=exc,
                context="moderation.decide",
            )
        return
    # Éxito en español con la etiqueta del diccionario único (sin enum crudo).
    new_status = updated["editorial_status"]
    label = editorial_label(new_status)
    if label is None:
        logger.warning("UI: estado editorial desconocido tras decisión: %r", new_status)
        st.success("Decisión aplicada.")
    else:
        st.success(f"Decisión aplicada: el documento quedó «{label}».")
    st.rerun()
