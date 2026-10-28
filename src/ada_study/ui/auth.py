"""Estado de sesión de la UI (TASK-08).

- El token opaco vive SOLO en `st.session_state` (memoria del proceso de
  Streamlit): nunca en cookies, query params ni disco.
- La identidad SIEMPRE la resuelve el backend validando el token contra
  PostgreSQL (`GET /api/me`); la UI nunca confía en un rol enviado por el
  cliente ni mantiene identidad propia.
"""

from ada_study.ui.api_client import ApiClient

SESSION_TOKEN_KEY = "ada_session_token"
SESSION_USER_KEY = "ada_session_user"
SESSION_BASE_URL_KEY = "ada_api_base_url"

# Base URL por defecto de la API (despliegue local, README §instalación).
DEFAULT_BASE_URL = "http://localhost:8000"


def get_base_url() -> str:
    """Base URL de la API desde session_state (editable en login)."""
    import streamlit as st

    return st.session_state.get(SESSION_BASE_URL_KEY, DEFAULT_BASE_URL)


def get_client() -> ApiClient | None:
    """Cliente autenticado si hay sesión; None en otro caso."""
    import streamlit as st

    token = st.session_state.get(SESSION_TOKEN_KEY)
    if not token:
        return None
    base_url = st.session_state.get(SESSION_BASE_URL_KEY, DEFAULT_BASE_URL)
    return ApiClient(base_url, token)


def is_authenticated() -> bool:
    """Indica si hay token de sesión en memoria."""
    import streamlit as st

    return bool(st.session_state.get(SESSION_TOKEN_KEY))


def save_session(token: str, base_url: str) -> None:
    """Guarda el token y la base URL en session_state (solo memoria)."""
    import streamlit as st

    st.session_state[SESSION_TOKEN_KEY] = token
    st.session_state[SESSION_BASE_URL_KEY] = base_url


def clear_session() -> None:
    """Descarta token e identidad de memoria (logout o sesión inválida)."""
    import streamlit as st

    st.session_state.pop(SESSION_TOKEN_KEY, None)
    st.session_state.pop(SESSION_USER_KEY, None)


def clear_all() -> None:
    """Limpieza total (logout): token, usuario y base URL."""
    clear_session()
    import streamlit as st

    st.session_state.pop(SESSION_BASE_URL_KEY, None)
