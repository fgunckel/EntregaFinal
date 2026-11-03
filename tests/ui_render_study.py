"""Script contenedor para AppTest (AGENT-TRACE-02): monta Estudiar.

Streamlit AppTest ejecuta un archivo como si fuera la app completa; este
wrapper llama al punto de entrada real de la página sin duplicar lógica.
"""

from ada_study.ui.pages.study import render_study

render_study()
