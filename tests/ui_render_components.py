"""Script contenedor para AppTest (UI-REDESIGN-02): monta los componentes.

Streamlit AppTest ejecuta un archivo como si fuera la app completa; este
wrapper ejercita el CSS del tema y los componentes de etapa 1 sin duplicar
lógica. Cualquier excepción se verifica en los tests (at.exception).
"""

from ada_study.ui import theme
from ada_study.ui.api_client import ApiError
from ada_study.ui.components import error_box, page_header, status_pill

theme.apply_theme()
page_header("BIBLIOTECA", "Mi biblioteca")
status_pill("draft")
status_pill("published_pending_review")
status_pill("available", technical=True)
status_pill("estado_desconocido")
error_box("Mensaje humano", exc=ApiError("HTTP_ERROR", "detalle-interno", 500))
error_box("Solo mensaje")
