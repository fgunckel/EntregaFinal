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


def render_login() -> None:
    """Formulario de login; al autenticar guarda el token en memoria."""
    st.title("ADA Study")
    st.subheader("Iniciar sesión")

    base_url = st.text_input(
        "URL de la API",
        value=st.session_state.get("ada_api_base_url", "http://localhost:8000"),
        help="Base URL de la API FastAPI (ej.: http://localhost:8000)",
    )
    email = st.text_input("Correo", key="login_email")
    password = st.text_input("Contraseña", type="password", key="login_password")

    if st.button("Entrar", type="primary", use_container_width=True):
        if not email or not password:
            st.warning("Ingresá correo y contraseña.")
            return
        try:
            # Login real contra el backend: IdentityService.login valida
            # credenciales contra PostgreSQL (sin enumeración de correos).
            probe = ApiClient(base_url)
            token = probe.login(email, password)
        except ApiError as exc:
            if exc.error_code == "AUTH_INVALID_CREDENTIALS":
                st.error("Credenciales inválidas.")
            elif exc.error_code == "AUTH_ACCOUNT_NOT_ACTIVE":
                st.error("La cuenta no está activa. Contactá al moderador.")
            else:
                st.error(f"No se pudo iniciar sesión ({exc.error_code}).")
            return
        auth.save_session(token, base_url)
        st.rerun()
