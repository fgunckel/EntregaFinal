"""App principal de la UI (TASK-08): Streamlit sobre la API FastAPI.

- Navegación por rol REAL: la identidad la resuelve el backend con el
  token de sesión (`GET /api/me`); la UI nunca confía en un rol propio.
- Sin sesión → login. Con sesión → Mi centro de estudio, Estudiar,
  Mi biblioteca, Moderación y Auditoría (estas dos solo para
  moderator/admin según la identidad real del backend).
- Ejecución: `streamlit run -m ada_study.ui.app` o
  `python -m streamlit run src/ada_study/ui/app.py`.
"""

import streamlit as st

from ada_study.ui import auth
from ada_study.ui.api_client import ApiError
from ada_study.ui.pages import audit, home, library, login, moderation, study

st.set_page_config(
    page_title="ADA Study",
    page_icon="📚",
    layout="wide",
    initial_sidebar_state="expanded",
)


def _sidebar(client) -> None:
    """Navegación lateral con identidad real y logout."""
    with st.sidebar:
        st.header("ADA Study")
        try:
            user = client.me()
        except ApiError as exc:
            # Sesión inválida/expirada o cuenta suspendida: la UI descarta
            # el token y vuelve al login (el backend es la frontera).
            st.error(f"Sesión no válida ({exc.error_code}).")
            auth.clear_session()
            st.rerun()
            return
        st.caption(f"{user.display_name} · {user.role}")
        st.divider()
        page = st.radio(
            "Navegación",
            options=[
                "Mi centro de estudio",
                "Estudiar",
                "Mi biblioteca",
                "Moderación",
                "Auditoría",
            ],
            key="ada_nav",
        )
        if st.button("Cerrar sesión"):
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
