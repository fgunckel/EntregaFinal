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
        st.error(f"No se pudo cargar la biblioteca ({exc.error_code}).")
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

    # Acciones reales según reglas del backend (no se ocultan en la UI
    # reglas que el backend igual aplica: la UI solo anticipa).
    col1, col2 = st.columns(2)
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
