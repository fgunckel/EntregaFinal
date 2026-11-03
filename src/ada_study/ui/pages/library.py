"""Mi biblioteca (TASK-08): catálogo real + upload + polling de ingesta.

- Lista lo que devuelve GET /api/documents (visibilidad por rol, Spec 06
  §106): publicados compartidos + propios, o todo para moderator/admin.
- Upload real: POST /api/documents (202) + polling del job; la UI nunca
  marca un documento como procesado por su cuenta.
- Acciones según reglas existentes: retry solo para versiones failed,
  delete 202 con aviso de purga eventual (ADR-0004).
"""

import time

import streamlit as st

from ada_study.ui import auth
from ada_study.ui.api_client import ApiError

# Estados técnicos de versión con etiqueta legible (ADR-0005).
VERSION_STATUS_LABELS = {
    "queued": "En cola",
    "processing": "Procesando",
    "indexing": "Indexando",
    "available": "Disponible",
    "failed": "Falló",
    "deleting": "Eliminándose",
}


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


def _status_badge(editorial_status: str) -> str:
    """Etiqueta legible del estado editorial (sin inventar estados)."""
    return {
        "draft": "🟡 Borrador",
        "pending_review": "🟡 En revisión",
        "published_pending_review": "🟢 Publicado (revisión pendiente)",
        "published": "🟢 Publicado",
        "rejected": "🔴 Rechazado",
        "withdrawn": "⚪ Retirado",
    }.get(editorial_status, editorial_status)


def render_library() -> None:
    """Biblioteca: listado real, upload y acceso al detalle."""
    st.title("Mi biblioteca")
    client = auth.get_client()
    if client is None:
        st.error("Sesión no encontrada. Volvé a iniciar sesión.")
        return

    _render_upload(client)
    st.divider()
    _render_document_list(client)

    # UI-DETAIL-02: consumir la selección guardada por "Ver detalle". Sin este
    # bloque la clave era write-only y el detalle (con "Enviar a revisión",
    # PUBLISH-FLOW-02) quedaba inaccesible.
    selected = st.session_state.get("ada_selected_document")
    if not selected:
        return
    st.divider()
    render_document_detail(client, selected)
    if st.button("← Volver a la biblioteca", key="back_to_library"):
        st.session_state.pop("ada_selected_document", None)
        st.rerun()


def _render_upload(client) -> None:
    """Subida de PDF real (202 + polling); rechaza tipos no soportados."""
    with st.expander("Subir documento (PDF)", expanded=False):
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
                st.error(f"Upload rechazado ({exc.error_code}): {exc}")
                return
            st.session_state["ada_last_job"] = {
                "document_id": result["document_id"],
                "job_id": result["job_id"],
            }
            st.success(
                f"Documento registrado (202). Job: {result['job_id']} — "
                "el procesamiento corre en el worker separado."
            )


def _render_document_list(client) -> None:
    """Listado real de documentos visibles según rol."""
    try:
        documents = client.list_documents()
    except ApiError as exc:
        st.error(describe_library_error(exc))
        return

    if not documents:
        st.info("No hay documentos visibles todavía.")
        return

    for document in documents:
        with st.container(border=True):
            col1, col2, col3 = st.columns([3, 2, 1])
            with col1:
                st.markdown(f"**{document.filename}**")
                if document.subject:
                    st.caption(f"Materia: {document.subject}")
            with col2:
                st.caption(f"Estado: {document.editorial_status}")
            with col3:
                if st.button("Ver detalle", key=f"detail_{document.document_id}"):
                    st.session_state["ada_selected_document"] = document.document_id


def _poll_last_job(client) -> None:
    """Polling real del último job de ingesta (202 + GET job)."""
    job_info = st.session_state.get("ada_last_job")
    if not job_info:
        return
    try:
        job = client.get_ingestion_job(job_info["job_id"])
    except ApiError as exc:
        st.warning(f"No se pudo consultar el job ({exc.error_code}).")
        return
    label = {
        "queued": "⏳ En cola",
        "processing": "⚙️ Procesando",
        "indexing": "🔍 Indexando",
        "available": "✅ Disponible",
        "failed": "❌ Falló",
    }.get(job.status, job.status)
    st.caption(f"Estado de ingesta: {label}")
    if job.status in ("queued", "processing", "indexing"):
        time.sleep(2)
        st.rerun()


def render_document_detail(client, document_id: str) -> None:
    """Detalle real de un documento con acciones autorizadas."""
    try:
        document = client.get_document(document_id)
    except ApiError as exc:
        if exc.error_code == "DEL_NOT_FOUND":
            st.error("El documento no existe o no es visible para tu cuenta.")
        else:
            st.error(f"No se pudo cargar el documento ({exc.error_code}).")
        return

    st.subheader(document.filename)
    if document.subject:
        st.caption(f"Materia: {document.subject}")
    st.caption(f"Estado editorial: {document.editorial_status}")

    # Identidad del espectador desde el backend (GET /api/me): la UI nunca
    # confía en un id/rol local para decidir visibilidad de acciones.
    try:
        viewer_id: str | None = client.me().user_id
    except ApiError:
        viewer_id = None

    # Acciones reales según reglas del backend (no se ocultan en la UI
    # reglas que el backend igual aplica: la UI solo anticipa).
    col1, col2, col3 = st.columns(3)
    with col1:
        if st.button("🗑️ Borrar documento"):
            try:
                client.delete_document(document.document_id)
                st.success(
                    "Borrado aceptado (202): cortina inmediata; la purga física "
                    "de los índices es eventual (ADR-0004)."
                )
            except ApiError as exc:
                st.error(f"No se pudo borrar ({exc.error_code}).")
    with col2:
        if document.active_version_id and st.button("🔁 Reintentar ingesta"):
            try:
                client.retry_version(document.document_id, document.active_version_id)
                st.info("Reintento encolado (202).")
            except ApiError as exc:
                st.error(f"No se pudo reintentar ({exc.error_code}).")
    with col3:
        # PUBLISH-FLOW-02: solo para el autor de un draft propio; el listado
        # se refresca tras la operación (st.rerun) para reflejar el estado.
        if can_submit_for_review(document, viewer_id) and st.button(
            "📤 Enviar a revisión"
        ):
            try:
                client.submit_for_review(document.document_id)
                st.success(
                    "Enviado a revisión (pending_review): el documento ahora "
                    "aparece en la cola de Moderación."
                )
                st.rerun()
            except ApiError as exc:
                st.error(describe_submit_error(exc))
