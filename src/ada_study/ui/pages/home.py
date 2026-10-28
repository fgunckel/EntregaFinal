"""Mi centro de estudio (TASK-08): resumen real del catálogo.

- Muestra lo que la API devuelve: documentos visibles por rol con su
  estado editorial real. Sin métricas inventadas ni progreso simulado.
- La jerarquía Materia→Unidad→Tema no tiene soporte de datos aún (los
  chunks no llevan metadatos de unidad/tema): la organización se presenta
  por documento y materia (subject), sin simular estructura inexistente.
"""

import streamlit as st

from ada_study.ui import auth
from ada_study.ui.api_client import ApiError

# Estados editoriales con etiqueta legible (ADR-0005; solo estados reales).
EDITORIAL_LABELS = {
    "draft": "Borrador",
    "pending_review": "En revisión",
    "published_pending_review": "Publicado (revisión pendiente)",
    "published": "Publicado",
    "rejected": "Rechazado",
    "withdrawn": "Retirado",
    "deleted": "Borrado",
}


def render_home() -> None:
    """Resumen real del centro de estudio del usuario autenticado."""
    st.title("Mi centro de estudio")
    client = auth.get_client()
    if client is None:
        st.error("Sesión no encontrada. Volvé a iniciar sesión.")
        return

    try:
        documents = client.list_documents()
    except ApiError as exc:
        st.error(f"No se pudo cargar la biblioteca ({exc.error_code}).")
        return

    # Resumen por estado editorial REAL (sin inventar progreso pedagógico).
    counts: dict[str, int] = {}
    for document in documents:
        counts[document.editorial_status] = counts.get(document.editorial_status, 0) + 1

    st.subheader("Mi biblioteca")
    if not documents:
        st.info("Todavía no hay documentos visibles. Subí uno desde Mi biblioteca.")
        return

    cols = st.columns(min(len(counts), 4))
    for col, (status, count) in zip(cols, counts.items()):
        label = {"published": "Publicados", "pending_review": "En revisión"}.get(
            status, status
        )
        cols[0].metric(label, counts[status])

    st.subheader("Documentos")
    for document in documents:
        with st.container(border=True):
            if document.subject:
                st.caption(f"Materia: {document.subject}")
            st.markdown(f"- {document.filename} — {document.editorial_status}")
