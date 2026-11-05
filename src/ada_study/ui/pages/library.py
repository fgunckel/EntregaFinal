"""Mi biblioteca (TASK-08): catálogo real + upload + polling de ingesta.

- Lista lo que devuelve GET /api/documents (visibilidad por rol, Spec 06
  §106): publicados compartidos + propios, o todo para moderator/admin.
- Upload real: POST /api/documents (202) + seguimiento del job con el
  fragmento ``ingest_status``; la UI nunca marca un documento como
  procesado por su cuenta.
- Acciones según reglas existentes: retry solo para versiones failed,
  delete 202 con confirmación explícita y aviso de purga eventual (ADR-0004).
- UI-REDESIGN-02/03: píldoras de estado del diccionario único, tarjetas
  ``doc_card`` en grilla con filtro por estado, detalle como vista
  diferenciada (no al final de la lista) y polling no bloqueante.
"""

import streamlit as st

from ada_study.ui import auth
from ada_study.ui.api_client import ApiError
from ada_study.ui.components import (
    confirm_destructive,
    doc_card,
    empty_state,
    error_box,
    ingest_status,
    page_header,
    status_pill,
)
from ada_study.ui.theme import EDITORIAL_STATUSES, editorial_label

DOCS_PER_ROW = 3


def describe_library_error(exc: ApiError) -> str:
    """Mensaje controlado para fallos al cargar la biblioteca (pura)."""
    return f"No se pudo cargar la biblioteca ({exc.error_code})."


def can_submit_for_review(document, viewer_user_id: str | None) -> bool:
    """PUBLISH-FLOW-02: el botón corresponde SOLO al autor de un draft propio.

    Ruta de AUTORÍA (Spec 06 §46-55): un moderator/admin que ve el borrador
    de un tercero NO obtiene la acción; el backend igual revalida (403).
    La identidad del espectador viene de GET /api/me (backend), nunca del
    cliente.
    """
    return (
        document.editorial_status == "draft"
        and document.uploaded_by is not None
        and viewer_user_id is not None
        and document.uploaded_by == viewer_user_id
    )


def describe_submit_error(exc: ApiError) -> str:
    """Mensaje controlado para el envío a revisión (pura, testeable)."""
    if exc.error_code == "DEL_NOT_ALLOWED" or exc.status_code == 403:
        return "Tu cuenta no está autorizada a enviar este documento a revisión (403)."
    if exc.error_code == "DEL_NOT_FOUND" or exc.status_code == 404:
        return "El documento no existe o no es visible para tu cuenta (404)."
    if exc.status_code == 409:
        return (
            "Transición no válida o versión no disponible (409): "
            "¿el documento ya fue enviado a revisión?"
        )
    return f"No se pudo enviar a revisión ({exc.error_code})."


def _open_upload_panel() -> None:
    """Acción del encabezado: botón que despliega el panel de subida.

    ``page_header`` EJECUTA ``actions`` en cada render: el rerun solo puede
    dispararse tras el clic del botón, nunca en el cuerpo del callable.
    """
    if st.button("Subir PDF", key="ada_open_upload", use_container_width=True):
        st.session_state["ada_upload_open"] = True
        st.rerun()


def render_library() -> None:
    """Biblioteca: listado en grilla, upload y detalle como vista propia.

    El detalle (``ada_selected_document``) es una VISTA diferenciada: ocupa
    toda la página (UI-REDESIGN-03) en lugar de aparecer colgado al final de
    la lista; «Volver a la biblioteca» restaura el listado.
    """
    client = auth.get_client()
    if client is None:
        st.error("Sesión no encontrada. Volvé a iniciar sesión.")
        return

    selected = st.session_state.get("ada_selected_document")
    if selected:
        render_document_detail(client, selected)
        return

    page_header("TU MATERIAL", "Mi biblioteca", actions=_open_upload_panel)
    _render_upload(client)
    ingest_status(client)
    st.divider()
    _render_document_list(client)


def _render_upload(client) -> None:
    """Subida de PDF real (202 + seguimiento del job); rechaza no-PDF.

    El panel se despliega con «Subir PDF» del encabezado (un clic menos que
    el expander siempre colapsado) y se abre solo al pedirlo.
    """
    with st.expander(
        "Subir documento (PDF)", expanded=st.session_state.get("ada_upload_open", False)
    ):
        uploaded = st.file_uploader("Archivo PDF", type=["pdf"])
        subject = st.text_input("Materia (opcional)", key="upload_subject")
        if uploaded is not None and st.button("Subir documento", type="primary"):
            try:
                result = client.upload_document(
                    uploaded.getvalue(),
                    filename=uploaded.name,
                    content_type="application/pdf",
                    subject=subject or None,
                )
            except ApiError as exc:
                # Humanizado (UI-REDESIGN-02 §4.2/§4.3): sin códigos ni detalle
                # del backend (antes incluso filtraba str(exc)); queda en log.
                error_box(
                    "No se pudo subir el archivo. Verificá que sea un PDF "
                    "válido e intentá de nuevo.",
                    exc=exc,
                    context="library.upload",
                )
                return
            st.session_state["ada_last_job"] = {
                "document_id": result["document_id"],
                "job_id": result["job_id"],  # interno: solo para el polling
            }
            # Sin (202) ni "Job: <uuid>": el procesamiento corre en segundo
            # plano y su estado se muestra abajo (polling real del job).
            st.success(
                "Documento registrado. La ingesta corre en segundo plano; "
                "el estado se actualiza abajo."
            )


def _status_filter(documents) -> str:
    """Filtro de estado editorial (solo con estados REALMENTE presentes)."""
    counts: dict[str, int] = {}
    for document in documents:
        counts[document.editorial_status] = (
            counts.get(document.editorial_status, 0) + 1
        )
    ordered = [s for s in EDITORIAL_STATUSES if s in counts] + [
        s for s in counts if s not in EDITORIAL_STATUSES
    ]
    options = ["__all__", *ordered]
    choice = st.selectbox(
        "Filtrar por estado",
        options,
        format_func=lambda s: (
            "Todos los estados"
            if s == "__all__"
            else f"{editorial_label(s) or s} ({counts[s]})"
        ),
        key="ada_library_status_filter",
    )
    return choice


def _render_document_list(client) -> None:
    """Listado real de documentos visibles según rol (grilla de tarjetas)."""
    try:
        documents = client.list_documents()
    except ApiError as exc:
        st.error(describe_library_error(exc))
        return

    if not documents:
        empty_state(
            "Tu biblioteca está vacía",
            "Subí tu primer PDF con el botón «Subir PDF»: cuando termine de "
            "procesarse vas a poder estudiarlo.",
        )
        return

    choice = _status_filter(documents)
    filtered = (
        documents
        if choice == "__all__"
        else [d for d in documents if d.editorial_status == choice]
    )
    if not filtered:
        empty_state(
            "Ningún documento con ese estado",
            "Probá con otro estado del filtro: el listado solo muestra lo "
            "que existe en tu biblioteca.",
        )
        return

    for start in range(0, len(filtered), DOCS_PER_ROW):
        row = filtered[start : start + DOCS_PER_ROW]
        cols = st.columns(DOCS_PER_ROW)
        for col, document in zip(cols, row, strict=False):
            with col:
                doc_card(document, on_action=_detail_action(document.document_id))


def _detail_action(document_id: str):
    """Acción «Ver detalle» de la tarjeta (caller crea el botón con key)."""

    def action() -> None:
        if st.button("Ver detalle", key=f"detail_{document_id}"):
            st.session_state["ada_selected_document"] = document_id
            # Vista de detalle propia: recién a partir del próximo render la
            # página entera ES el detalle (no un anexo al final de la lista).
            st.rerun()

    return action


def render_document_detail(client, document_id: str) -> None:
    """Vista de detalle real de un documento con acciones autorizadas.

    La vista se abre SOLO desde «Ver detalle» (``ada_selected_document``):
    incluye «Volver a la biblioteca», pastilla de estado editorial y las
    acciones que el backend autoriza para el espectador actual. Borrar
    requiere confirmación explícita (barrera ``confirm_destructive``).
    """
    try:
        document = client.get_document(document_id)
    except ApiError as exc:
        if exc.error_code == "DEL_NOT_FOUND":
            st.error("El documento no existe o no es visible para tu cuenta.")
        else:
            error_box(
                "No se pudo cargar el documento. Intentá de nuevo en unos minutos.",
                exc=exc,
                context="library.detail",
            )
        return

    header_col, back_col = st.columns([4, 1])
    with header_col:
        st.markdown(
            '<p class="ada-kicker">DETALLE DE DOCUMENTO</p>', unsafe_allow_html=True
        )
        st.subheader(document.filename)
    with back_col:
        if st.button("← Volver a la biblioteca", key="back_to_library"):
            st.session_state.pop("ada_selected_document", None)
            st.rerun()

    if document.subject:
        st.caption(f"Materia: {document.subject}")
    st.markdown('<p class="ada-kicker">Estado editorial</p>', unsafe_allow_html=True)
    status_pill(document.editorial_status)

    # Identidad del espectador desde el backend (GET /api/me): la UI nunca
    # confía en un id/rol local para decidir visibilidad de acciones.
    try:
        viewer_id: str | None = client.me().user_id
    except ApiError:
        viewer_id = None

    # Barrera de confirmación para la acción destructiva: el botón queda
    # deshabilitado hasta marcar la casilla (no hay borrado accidental).
    confirmed = confirm_destructive(
        "Confirmo que quiero borrar este documento",
        key=f"ada_confirm_delete_{document.document_id}",
    )

    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("🗑️ Borrar documento", disabled=not confirmed):
            try:
                client.delete_document(document.document_id)
                # Sin (202), "cortina inmediata" ni "ADR-0004" (G4): el
                # significado real, en lenguaje del usuario.
                st.success(
                    "El documento fue borrado y ya no es visible. La limpieza "
                    "de los índices se completa en los próximos minutos."
                )
                st.session_state.pop("ada_selected_document", None)
                st.rerun()
            except ApiError as exc:
                error_box(
                    "No se pudo borrar el documento. Intentá de nuevo en unos minutos.",
                    exc=exc,
                    context="library.delete",
                )
    with col2:
        if document.active_version_id and st.button("🔁 Reintentar ingesta"):
            try:
                client.retry_version(document.document_id, document.active_version_id)
                st.info("Reintento de ingesta en cola: el procesamiento comienza automáticamente.")
            except ApiError as exc:
                error_box(
                    "No se pudo reintentar la ingesta. Intentá de nuevo en unos minutos.",
                    exc=exc,
                    context="library.retry",
                )
    with col3:
        # PUBLISH-FLOW-02: solo para el autor de un draft propio; el listado
        # se refresca tras la operación (st.rerun) para reflejar el estado.
        if can_submit_for_review(document, viewer_id) and st.button("📤 Enviar a revisión"):
            try:
                client.submit_for_review(document.document_id)
                # Etiqueta del diccionario único (sin enum crudo).
                st.success(
                    "Enviado a revisión: el documento ahora aparece en la "
                    "cola de Moderación."
                )
                st.rerun()
            except ApiError as exc:
                st.error(describe_submit_error(exc))
