"""Pantalla de login (TASK-08, B1).

- Credenciales → POST /api/auth/login → token opaco en session_state.
- Errores genéricos del backend (AUTH_INVALID_CREDENTIALS,
  AUTH_ACCOUNT_NOT_ACTIVE) se muestran tal cual, sin inventar causas.
- La base URL de la API es editable para permitir despliegues distintos
  del trabajo final (local, LAN, contenedor).
"""

import streamlit as st

from ada_study.ui import auth
from ada_study.ui.api_client import ApiClient, ApiError
from ada_study.ui.components import error_box


def render_login() -> None:
    """Formulario de login; al autenticar guarda el token en memoria.

    UI-REDESIGN-03: composición centrada con marca y tagline aprobado
    («Estudiá con evidencia de tu biblioteca»), formulario en tarjeta y las
    opciones avanzadas (URL de la API) colapsadas para no estorbar el camino
    principal. Se oculta la barra lateral vacía que Streamlit reserva.
    """
    # Sin sesión no hay barra lateral que mostrar: se oculta la reserva vacía.
    st.markdown(
        "<style>[data-testid='collapseSidebar']{display: none;}</style>",
        unsafe_allow_html=True,
    )
    _left, center, _right = st.columns([1, 2.2, 1])
    with center:
        st.markdown(
            '<div class="ada-login-hero">'
            '<p class="ada-kicker">ADA STUDY</p>'
            '<h1 class="ada-login-title">'
            "Estudiá con evidencia de tu biblioteca</h1>"
            '<p class="ada-login-tag">Iniciá sesión con tu cuenta de ADA.</p>'
            "</div>",
            unsafe_allow_html=True,
        )
        with st.container(border=True):
            st.subheader("Iniciar sesión")
            with st.expander("Opciones avanzadas"):
                base_url = st.text_input(
                    "URL de la API",
                    value=st.session_state.get(
                        auth.SESSION_BASE_URL_KEY, auth.DEFAULT_BASE_URL
                    ),
                    help="Base URL de la API FastAPI (ej.: http://localhost:8000)",
                )
            email = st.text_input("Correo", key="login_email")
            password = st.text_input("Contraseña", type="password", key="login_password")

            if st.button("Entrar", type="primary", use_container_width=True):
                if not email or not password:
                    st.warning("Ingresá correo y contraseña.")
                    return
                try:
                    probe = ApiClient(base_url)
                    token = probe.login(email, password)
                except ApiError as exc:
                    if exc.error_code == "AUTH_INVALID_CREDENTIALS":
                        st.error("Credenciales inválidas.")
                    elif exc.error_code == "AUTH_ACCOUNT_NOT_ACTIVE":
                        st.error("La cuenta no está activa. Contactá al moderador.")
                    else:
                        # Humanizado (UI-REDESIGN-02 §4.2): el código queda
                        # en el log; el usuario recibe una acción clara.
                        error_box(
                            "No se pudo iniciar sesión en este momento. "
                            "Verificá tu conexión e intentá de nuevo.",
                            exc=exc,
                            context="login",
                        )
                    return
                auth.save_session(token, base_url)
                st.rerun()
