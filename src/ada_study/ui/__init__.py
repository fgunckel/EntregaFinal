"""UI de ADA Study (TASK-08): cliente Streamlit sobre la API FastAPI.

- El cliente HTTP (`api_client`) es puro y testeable sin browser.
- El token de sesión vive solo en `st.session_state` (nunca en cookies ni
  en disco); la frontera de autenticación sigue siendo el backend.
- La UI no duplica lógica de dominio: muestra estados y errores reales
  de la API, sin simular procesamiento ni resultados pedagógicos.
"""
