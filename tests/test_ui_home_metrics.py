"""UI-REDESIGN-02 §5: distribución de métricas en Mi centro de estudio.

Bug corregido: el loop de `home.render_home` renderizaba SIEMPRE en
`cols[0]`, así que todas las métricas quedaban apiladas en la primera
columna. Aquí se verifica con AppTest que cada métrica vive en SU columna.

Patrón del proyecto (test_library_detail_ui): AppTest sobre un wrapper que
llama al punto de entrada real; el cliente API se reemplaza por un fake en
memoria (sin red, sin PostgreSQL, sin Streamlit server real).
"""

from pathlib import Path
from types import SimpleNamespace

from streamlit.testing.v1 import AppTest
from streamlit.testing.v1.element_tree import Block

from ada_study.ui import auth
from ada_study.ui.theme import editorial_label

SCRIPT_PATH = Path(__file__).parent / "ui_render_home.py"


class FakeHomeClient:
    """Mínimo superficial de ApiClient para render_home (solo listado)."""

    def __init__(self, documents: list[SimpleNamespace] | Exception) -> None:
        self._documents = documents

    def list_documents(self) -> list[SimpleNamespace]:
        if isinstance(self._documents, Exception):
            raise self._documents
        return list(self._documents)


def _record(doc_id: str, status: str) -> SimpleNamespace:
    """Registro mínimo con la forma que render_home consume.

    SimpleNamespace a propósito: permite simular un estado FUERA del
    contrato (`Literal` de DocumentRecord lo rechazaría) para probar el
    manejo seguro de estados desconocidos sin inventar estados válidos.
    """
    return SimpleNamespace(
        document_id=doc_id,
        filename=f"{doc_id}.pdf",
        subject=None,
        editorial_status=status,
    )


def _run_app(monkeypatch, documents) -> AppTest:
    """Ejecuta el wrapper de Home con un cliente falso (sin Streamlit server)."""
    client = FakeHomeClient(documents)
    monkeypatch.setattr(auth, "get_client", lambda: client)
    at = AppTest.from_file(str(SCRIPT_PATH), default_timeout=15)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _metrics_per_column(at: AppTest) -> list[int]:
    """Métricas por columna, recorriendo el árbol de elementos de AppTest.

    Es la verificación directa del bug: si todas las métricas cayeran en
    `cols[0]`, habría una sola columna con N métricas.
    """
    per_column: list[int] = []

    def walk(block: Block) -> None:
        if block.type == "column":
            per_column.append(sum(1 for child in block.children.values() if child.type == "metric"))
        for child in block.children.values():
            if isinstance(child, Block):
                walk(child)

    for child in at.main.children.values():
        if isinstance(child, Block):
            walk(child)
    return per_column


class TestDistribucionMetricasHome:
    """Bug de layout corregido: UNA métrica por columna (en filas de a 4)."""

    def _documentos(self, estados: list[str]) -> list[SimpleNamespace]:
        return [_record(f"doc_{i}_{status}", status) for i, status in enumerate(estados)]

    def test_metricas_distribuidas_en_columnas_distintas(self, monkeypatch):
        """4 estados → 4 columnas con 1 métrica cada una (antes: todo en cols[0])."""
        documentos = self._documentos(
            ["draft", "draft", "published", "published", "published", "pending_review", "rejected"]
        )
        at = _run_app(monkeypatch, documentos)
        assert len(at.metric) == 4
        etiquetas = {m.label for m in at.metric}
        assert etiquetas == {"Borrador", "Publicado", "En revisión", "Rechazado"}
        por_columna = [c for c in _metrics_per_column(at) if c]
        assert por_columna == [1, 1, 1, 1], "cada métrica en su propia columna"

    def test_valores_reales_por_estado(self, monkeypatch):
        documentos = self._documentos(["draft", "published", "draft"])
        at = _run_app(monkeypatch, documentos)
        valores = {m.label: int(m.value) for m in at.metric}
        assert valores == {"Borrador": 2, "Publicado": 1}
        # Sin métricas inventadas: solo los estados presentes en el catálogo.
        assert len(at.metric) == 2

    def test_mas_de_cuatro_estados_forma_nueva_fila(self, monkeypatch):
        """5 estados → fila de 4 + fila de 1; ninguna métrica se pierde."""
        documentos = self._documentos(
            ["draft", "pending_review", "published", "rejected", "withdrawn"]
        )
        at = _run_app(monkeypatch, documentos)
        assert len(at.metric) == 5
        per_column = [c for c in _metrics_per_column(at) if c]
        assert per_column == [1, 1, 1, 1, 1]

    def test_etiquetas_del_diccionario_unico(self, monkeypatch):
        """Las etiquetas de métrica salen del diccionario de estados (§4.1)."""
        estados = ["draft", "pending_review", "published", "rejected", "withdrawn"]
        documentos = self._documentos(estados)
        at = _run_app(monkeypatch, documentos)
        labels = [m.label for m in at.metric]
        assert labels == [editorial_label(s) for s in estados]

    def test_estado_desconocido_no_rompe_y_va_al_final(self, monkeypatch):
        documentos = [
            _record("doc_ok", "published"),
            _record("doc_raro", "estado_desconocido"),
        ]
        at = _run_app(monkeypatch, documentos)
        etiquetas = [m.label for m in at.metric]
        assert etiquetas == ["Publicado", "estado_desconocido"]

    def test_error_de_api_mensaje_humano_sin_codigo(self, monkeypatch):
        """Fallo del listado: mensaje humanizado; el código estable solo en log."""
        from ada_study.ui.api_client import ApiError

        client = FakeHomeClient(ApiError("ING_INTERNAL", "detalle-interno", 500))

        def _fail():
            return client

        monkeypatch.setattr(auth, "get_client", _fail)
        at = AppTest.from_file(str(SCRIPT_PATH), default_timeout=15)
        at.run()
        assert not at.exception
        assert at.error, "se espera un error humanizado"
        visibles = "\n".join([e.value for e in at.error] + [m.value for m in at.markdown])
        assert "ING_INTERNAL" not in visibles
        assert "detalle-interno" not in visibles
        assert "No se pudieron cargar tus documentos" in at.error[0].value
