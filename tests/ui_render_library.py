"""Script contenedor para AppTest (UI-DETAIL-02): monta Mi biblioteca.

Streamlit AppTest ejecuta un archivo como si fuera la app completa; este
wrapper llama al punto de entrada real de la página sin duplicar lógica.
"""

from ada_study.ui.pages.library import render_library

render_library()
