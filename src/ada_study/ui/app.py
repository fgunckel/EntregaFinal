"""App principal de la UI (TASK-08): Streamlit sobre la API FastAPI.

- Navegación por rol REAL: la identidad la resuelve el backend con el
  token de sesión (`GET /api/me`); la UI nunca confía en un rol propio.
- Sin sesión → login. Con sesión → Mi centro de estudio, Estudiar,
  Mi biblioteca, Moderación y Auditoría (estas dos solo para
  moderator/admin según la identidad real del backend).
- Ejecución: `streamlit run -m ada_study.ui.app` o
  `python -m streamlit run src/ada_study/ui/app.py`.
"""

import html

import streamlit as st

from ada_study.ui import auth, theme
from ada_study.ui.api_client import ApiError
from ada_study.ui.components import error_box
from ada_study.ui.pages import audit, home, library, login, moderation, study

st.set_page_config(
    page_title="ADA Study",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="expanded",
)

# Etapa 0 (UI-REDESIGN-02): identidad «Tinta y aula». El tema base lo aplica
# .streamlit/config.toml (resuelto desde el CWD del proceso); aquí se inyecta
# el CSS global (clases `ada-*`) una vez por rerun, antes de renderizar.
# layout="wide" se conserva; el ancho de lectura acotado llega con el
# rediseño de cada pantalla (etapas 4-7).
theme.apply_theme()


ROLE_LABELS = {
    "student": "Estudiante",
    "trusted_contributor": "Colaborador",
    "moderator": "Moderador",
    "admin": "Administrador",
}

# Secciones reservadas: visibles SOLO si la identidad real del backend
# (GET /api/me) devuelve un rol con privilegios de moderación.
PRIVILEGED_ROLES = {"moderator", "admin"}

_STUDENT_NAV = ("Mi centro de estudio", "Estudiar", "Mi biblioteca")


def _render_brand() -> None:
    """Marca «Tinta y aula» en el tope de la barra lateral."""
    st.markdown(
        '<div class="ada-brand">'
        '<p class="ada-brand__mark">ADA</p>'
        '<p class="ada-brand__name">ADA Study</p>'
        '<p class="ada-brand__sub">Estudiá con evidencia</p>'
        "</div>",
        unsafe_allow_html=True,
    )


def _render_user_card(user) -> None:
    """Tarjeta compacta de la identidad activa (datos reales del backend)."""
    initial = html.escape((user.display_name or "?")[:1].upper())
    st.markdown(
        '<div class="ada-user-card">'
        f'<span class="ada-user-card__avatar">{initial}</span>'
        "<div>"
        f'<p class="ada-user-card__name">{html.escape(user.display_name)}</p>'
        f'<p class="ada-user-card__role">{ROLE_LABELS.get(user.role, user.role)}</p>'
        "</div></div>",
        unsafe_allow_html=True,
    )


def _sidebar(client) -> str:
    """Navegación lateral: marca, navegación por rol real y usuario."""
    with st.sidebar:
        _render_brand()
        try:
            user = client.me()
        except ApiError as exc:
            # Sesión inválida/expirada o cuenta suspendida: la UI descarta
            # el token y vuelve al login (el backend es la frontera). El
            # código estable queda en el log; al usuario solo la acción clara.
            error_box(
                "Tu sesión no es válida o expiró. Volvé a iniciar sesión.",
                exc=exc,
                context="app.sidebar",
            )
            auth.clear_session()
            st.rerun()
            return "Mi centro de estudio"
        # La identidad se comparte con las páginas (home la usa para el saludo).
        st.session_state[auth.SESSION_USER_KEY] = user
        options = list(_STUDENT_NAV)
        if user.role in PRIVILEGED_ROLES:
            options += ["Moderación", "Auditoría"]
        # Si el rol cambió, la selección guardada puede dejar de ser válida.
        if st.session_state.get("ada_nav") not in options:
            st.session_state["ada_nav"] = options[0]
        st.divider()
        page = st.radio(
            "Navegación",
            options=options,
            key="ada_nav",
            label_visibility="collapsed",
        )
        st.divider()
        _render_user_card(user)
        if st.button("Cerrar sesión", use_container_width=True):
            try:
                client.logout()
            except ApiError:
                pass  # la sesión ya no es válida; se descarta igualmente
            auth.clear_all()
            st.rerun()
    return page


def main() -> None:
    """Punto de entrada de la UI Streamlit."""
    if not auth.is_authenticated():
        login.render_login()
        return

    client = auth.get_client()
    if client is None:
        login.render_login()
        return

    page = _sidebar(client)
    if page == "Mi centro de estudio":
        home.render_home()
    elif page == "Estudiar":
        study.render_study()
    elif page == "Mi biblioteca":
        library.render_library()
    elif page == "Moderación":
        moderation.render_moderation()
    elif page == "Auditoría":
        audit.render_audit()


if __name__ == "__main__":
    main()
