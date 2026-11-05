"""Mi centro de estudio (TASK-08): resumen real del catálogo.

- Muestra lo que la API devuelve: documentos visibles por rol con su
  estado editorial real. Sin métricas inventadas ni progreso simulado.
- La jerarquía Materia→Unidad→Tema no tiene soporte de datos aún (los
  chunks no llevan metadatos de unidad/tema): la organización se presenta
  por documento y materia (subject), sin simular estructura inexistente.
- UI-REDESIGN-02 (etapas 0-1): etiquetas de estado del diccionario único
  (`ui/theme.py`) y corrección del bug de layout por el que TODAS las
  métricas caían en `cols[0]`.
"""

import html

import streamlit as st

from ada_study.ui import auth
from ada_study.ui.api_client import ApiError
from ada_study.ui.components import doc_card, empty_state, error_box, page_header
from ada_study.ui.pages.study import CONVERSATION_KEY, TURNS_KEY
from ada_study.ui.theme import EDITORIAL_STATUSES, editorial_label

# Métricas por fila en el resumen (una columna real por estado).
METRICS_PER_ROW = 4

# Documentos por fila en la grilla de tarjetas.
DOCS_PER_ROW = 3


def _navigate_to(page: str) -> None:
    """Callback de navegación (SMOKE-FIX-01).

    ``ada_nav`` es la key del ``st.radio`` del sidebar: escribirla en
    ``st.session_state`` DESPUÉS de que el widget se instanció en el mismo
    rerun lanza ``StreamlitWidgetAlreadyInstantiatedError``. El callback
    corre ANTES del siguiente rerun (antes de instanciar widgets), por eso
    es el mecanismo compatible con el ciclo de Streamlit. El rerun es
    automático al hacer clic; no se fuerza otro.
    """
    st.session_state["ada_nav"] = page


def _quick_actions() -> None:
    """Accesos rápidos a las dos acciones principales del estudiante."""
    study_col, library_col = st.columns(2)
    study_col.button(
        "Ir a Estudiar",
        type="primary",
        use_container_width=True,
        key="home_go_study",
        on_click=_navigate_to,
        args=("Estudiar",),
    )
    library_col.button(
        "Ir a la biblioteca",
        use_container_width=True,
        key="home_go_library",
        on_click=_navigate_to,
        args=("Mi biblioteca",),
    )


def _continue_card() -> None:
    """«Continuar donde dejaste»: SOLO con conversación real de la sesión.

    Se apoya en las claves de sesión de la página Estudiar
    (``ada_study_conversation_id`` + ``ada_study_turns``): si no hay turno
    previo REAL no se muestra nada (sin estados simulados).
    """
    turns = st.session_state.get(TURNS_KEY) or []
    if not st.session_state.get(CONVERSATION_KEY) or not turns:
        return
    last_question = turns[-1]["pregunta"]
    excerpt = last_question if len(last_question) <= 120 else f"{last_question[:117]}…"
    with st.container(border=True):
        st.markdown(
            '<p class="ada-kicker">Continuar donde dejaste</p>', unsafe_allow_html=True
        )
        st.markdown(f"Última pregunta: **{html.escape(excerpt)}**")
        # Mismo mecanismo callback (SMOKE-FIX-01): escribir `ada_nav` aquí
        # lanzaría StreamlitWidgetAlreadyInstantiatedError (el radio del
        # sidebar ya existe en este rerun).
        st.button(
            "Continuar en Estudiar",
            key="home_continue_study",
            on_click=_navigate_to,
            args=("Estudiar",),
        )


def _render_document_grid(documents) -> None:
    """Grilla de tarjetas de documentos (presupuesto visual: 3 por fila)."""
    for start in range(0, len(documents), DOCS_PER_ROW):
        row = documents[start : start + DOCS_PER_ROW]
        cols = st.columns(DOCS_PER_ROW)
        for col, document in zip(cols, row, strict=False):
            with col:
                doc_card(document)


def render_home() -> None:
    """Resumen real del centro de estudio del usuario autenticado."""
    client = auth.get_client()
    if client is None:
        st.error("Sesión no encontrada. Volvé a iniciar sesión.")
        return
    user = st.session_state.get(auth.SESSION_USER_KEY)
    first_name = (user.display_name or "").split(" ")[0] if user else ""
    title = f"Hola, {first_name}" if first_name else "Mi centro de estudio"
    page_header("TU CENTRO DE ESTUDIO", title)
    st.markdown(
        '<p class="ada-page-sub">Estudiá con evidencia de tu biblioteca.</p>',
        unsafe_allow_html=True,
    )
    _quick_actions()
    _continue_card()

    try:
        documents = client.list_documents()
    except ApiError as exc:
        # Humanizado (UI-REDESIGN-02 §4.2): el código estable queda en el log.
        error_box(
            "No se pudieron cargar tus documentos. Intentá de nuevo en unos minutos.",
            exc=exc,
            context="home.list_documents",
        )
        return

    # Resumen por estado editorial REAL (sin inventar progreso pedagógico).
    counts: dict[str, int] = {}
    for document in documents:
        counts[document.editorial_status] = counts.get(document.editorial_status, 0) + 1

    st.subheader("Mi biblioteca")
    if not documents:
        empty_state(
            "Todavía no hay documentos",
            "Cuando subas material a Mi biblioteca, acá vas a ver el resumen "
            "y vas a poder estudiarlo.",
            action=None,
        )
        return

    # BUG corregido (UI-REDESIGN-02 §5): antes el loop renderizaba siempre en
    # `cols[0]` y todas las métricas quedaban apiladas en la primera columna.
    # Ahora cada estado ocupa su propia columna, en filas de a 4 (con orden
    # canónico del diccionario de estados; estados desconocidos van al final,
    # sin inventar contadores ni estados).
    ordered = [s for s in EDITORIAL_STATUSES if s in counts] + [
        s for s in counts if s not in EDITORIAL_STATUSES
    ]
    for start in range(0, len(ordered), METRICS_PER_ROW):
        row = ordered[start : start + METRICS_PER_ROW]
        cols = st.columns(len(row))
        for col, status in zip(cols, row, strict=True):
            col.metric(editorial_label(status) or status, counts[status])

    st.subheader("Documentos")
    _render_document_grid(documents)
