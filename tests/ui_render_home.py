"""Script contenedor para AppTest (UI-REDESIGN-02 §5): monta Mi centro.

Wrapper mínimo que llama al punto de entrada real (render_home) para probar
la distribución de métricas por columnas con AppTest (el cliente API se
reemplaza en el proceso del test vía monkeypatch, como en test_library_detail_ui).
"""

from ada_study.ui.pages.home import render_home

render_home()
