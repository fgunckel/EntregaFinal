"""UI-REDESIGN-02: sistema de estilos «Tinta y aula» y componentes comunes.

Cubre los requisitos de prueba de la tarea:

- Diccionario ÚNICO de estados (editoriales + técnicos) con etiquetas en
  español y estilos semánticos diferenciados (tarea §4.1).
- ``status_pill`` renderizado real vía AppTest (etiqueta en español, color
  semántico, sin emojis, estado desconocido manejado sin romper).
- ``error_box``: mensaje humanizado visible, código estable SOLO en log.
- ``page_header`` y ``short_ref``, disponibles para las etapas 2-7.

Patrón del proyecto: AppTest sobre un wrapper (``ui_render_components.py``)
y funciones puras sin Streamlit para los diccionarios.
"""

import logging
from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from ada_study.ui.api_client import ApiError
from ada_study.ui.components import error_box, short_ref
from ada_study.ui.theme import (
    EDITORIAL_STATUSES,
    TECHNICAL_STATUSES,
    editorial_label,
    status_style,
    technical_label,
)

SCRIPT_PATH = Path(__file__).parent / "ui_render_components.py"

# Estados exigidos por la tarea (UI-REDESIGN-02 §4.1).
EDITORIALES_ESPERADOS = {
    "draft",
    "pending_review",
    "published",
    "published_pending_review",
    "rejected",
    "withdrawn",
    "deleted",
}
TECNICOS_ESPERADOS = {"queued", "processing", "indexing", "available", "failed"}


class TestDiccionarioEstados:
    def test_editorial_statuses_completos(self):
        assert set(EDITORIAL_STATUSES) == EDITORIALES_ESPERADOS

    def test_technical_statuses_completos(self):
        assert set(TECHNICAL_STATUSES) >= TECNICOS_ESPERADOS
        # Estado real del contrato de versiones (specs/02), ya etiquetado antes.
        assert set(TECHNICAL_STATUSES) == TECNICOS_ESPERADOS | {"deleting"}

    def test_etiquetas_en_espanol_sin_enum_crudo(self):
        for status, style in {**EDITORIAL_STATUSES, **TECHNICAL_STATUSES}.items():
            assert style.label, f"etiqueta vacía para {status}"
            assert "_" not in style.label, f"enum crudo en etiqueta: {status}"
            assert style.color.startswith("#")
            assert style.tone in {"ok", "warn", "danger", "neutral", "accent"}

    def test_familias_no_colisionan(self):
        assert not set(EDITORIAL_STATUSES) & set(TECHNICAL_STATUSES)

    def test_estado_desconocido_devuelve_none(self):
        assert status_style("estado_fantasma") is None
        assert editorial_label("estado_fantasma") is None
        assert technical_label("estado_fantasma") is None

    def test_etiquetas_espanol(self):
        assert editorial_label("draft") == "Borrador"
        assert editorial_label("published_pending_review") == "Publicado (revisión pendiente)"
        assert technical_label("queued") == "En cola"
        assert technical_label("failed") == "Falló"

    def test_estilos_semanticos_diferenciados(self):
        # Verde para publicado/disponible; rojo para rechazado/falló.
        assert EDITORIAL_STATUSES["published"].tone == "ok"
        assert EDITORIAL_STATUSES["rejected"].tone == "danger"
        assert TECHNICAL_STATUSES["available"].tone == "ok"
        assert TECHNICAL_STATUSES["failed"].tone == "danger"

    @pytest.mark.parametrize(
        ("status", "technical"),
        [("draft", False), ("queued", True), ("available", False), ("draft", True)],
    )
    def test_status_style_resuelve_entre_familias(self, status: str, technical: bool):
        assert status_style(status, technical=technical) is not None


class TestComponentesRender:
    def _run(self) -> AppTest:
        at = AppTest.from_file(str(SCRIPT_PATH), default_timeout=15)
        at.run()
        assert not at.exception, [e.value for e in at.exception]
        return at

    def test_render_sin_excepciones(self):
        self._run()

    def test_pill_editorial_espanol_sin_enum_crudo(self):
        at = self._run()
        pills = [m.value for m in at.markdown if "ada-pill" in m.value]
        assert any("ada-pill--warn" in p and "Borrador" in p for p in pills)
        assert any("ada-pill--ok" in p and "Publicado (revisión pendiente)" in p for p in pills)
        # Estados CONOCIDOS nunca muestran el enum crudo.
        assert not any("pending_review" in p for p in pills)

    def test_pill_tecnica_diferenciada(self):
        at = self._run()
        pills = [m.value for m in at.markdown if "ada-pill" in m.value]
        tecnica = [p for p in pills if "ada-pill--tech" in p]
        assert tecnica and any("Disponible" in p for p in tecnica)

    def test_estado_desconocido_pill_neutral_con_valor_escapado(self):
        """Desconocido: no se inventa etiqueta ni se oculta (estilo neutro)."""
        at = self._run()
        pills = [m.value for m in at.markdown if "ada-pill" in m.value]
        desconocidas = [p for p in pills if "estado_desconocido" in p]
        assert desconocidas and "ada-pill--neutral" in desconocidas[0]

    def test_error_box_muestra_mensaje_humano_sin_codigo(self):
        at = self._run()
        errores = [e.value for e in at.error]
        assert errores == ["Mensaje humano", "Solo mensaje"]
        visibles = "\n".join(errores + [m.value for m in at.markdown])
        assert "HTTP_ERROR" not in visibles
        assert "detalle-interno" not in visibles

    def test_error_box_registra_codigo_en_log(self, caplog):
        """El código estable queda disponible para diagnóstico en el log."""
        with caplog.at_level(logging.ERROR, logger="ada_study.ui"):
            error_box(
                "Fallo humanizado",
                exc=ApiError("HTTP_ERROR", "detalle-interno", 500),
                context="test",
            )
        assert any("HTTP_ERROR" in r.message for r in caplog.records)

    def test_page_header_kicker_y_titulo(self):
        at = self._run()
        markdown = "\n".join(m.value for m in at.markdown)
        assert "ada-kicker" in markdown and "BIBLIOTECA" in markdown
        assert "ada-page-title" in markdown and "Mi biblioteca" in markdown

    def test_css_del_tema_solo_clases_propias(self):
        at = self._run()
        css = "\n".join(m.value for m in at.markdown if "<style>" in m.value)
        assert "ada-pill--ok" in css and ".ada-kicker" in css
        assert "st-" not in css  # sin selectores internos de Streamlit

    def test_short_ref_abrevia_uuid_y_cubre_vacios(self):
        # Decisión aprobada: autor como UUID abreviado, sin tocar el backend.
        assert short_ref(None) == "—"
        assert short_ref("") == "—"
        assert short_ref("corto") == "corto"
        assert short_ref("12345678-abcd-9012-efgh-3456ijkl7890") == "12345678…"
