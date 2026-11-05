"""UI-DETAIL-02: flujo de renderizado del detalle en Mi biblioteca.

Usa streamlit.testing.v1.AppTest sobre el wrapper ``ui_render_library.py``
que llama al punto de entrada real (``render_library``). El cliente API se
reemplaza por un fake en memoria: NUNCA se invoca el endpoint real de envío
a revisión (el botón se verifica por presencia, no por clic).
"""

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from streamlit.testing.v1 import AppTest

from ada_study.contracts.documents import DocumentRecord
from ada_study.ui import auth
from ada_study.ui.api_client import ApiError

SCRIPT_PATH = Path(__file__).parent / "ui_render_library.py"


class FakeUiClient:
    """Mínimo superficial de ApiClient para render_library (solo lectura)."""

    def __init__(self, documents: list[DocumentRecord], user_id: str) -> None:
        self._documents = {d.document_id: d for d in documents}
        self.user_id = user_id
        self.get_document_calls: list[str] = []

    def list_documents(self) -> list[DocumentRecord]:
        return list(self._documents.values())

    def get_document(self, document_id: str) -> DocumentRecord:
        self.get_document_calls.append(document_id)
        if document_id not in self._documents:
            raise ApiError("DEL_NOT_FOUND", "documento no encontrado", 404)
        return self._documents[document_id]

    def me(self) -> SimpleNamespace:
        """Identidad del espectador (equivalente a GET /api/me)."""
        return SimpleNamespace(user_id=self.user_id)


def _record(
    doc_id: str, status: str = "draft", owner: str = "usr_author"
) -> DocumentRecord:
    now = datetime.now(UTC).isoformat()
    return DocumentRecord(
        document_id=doc_id,
        filename=f"{doc_id}.pdf",
        active_version_id="ver_1",
        editorial_status=status,  # type: ignore[arg-type]
        uploaded_by=owner,
        created_at=now,
        updated_at=now,
    )


def _run_app(
    monkeypatch: pytest.MonkeyPatch,
    documents: list[DocumentRecord],
    user_id: str = "usr_author",
) -> AppTest:
    client = FakeUiClient(documents, user_id)
    monkeypatch.setattr(auth, "get_client", lambda: client)
    at = AppTest.from_file(str(SCRIPT_PATH), default_timeout=15)
    at.run()
    assert not at.exception, [e.value for e in at.exception]
    return at


def _button_labels(at: AppTest) -> list[str]:
    return [b.label for b in at.button]


class TestLibraryDetailFlow:
    def test_click_ver_detalle_renders_detail(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        at = _run_app(monkeypatch, [_record("doc_1")])
        # Antes del clic: solo el listado, sin subheader de detalle.
        assert at.subheader == []
        at.button(key="detail_doc_1").click().run()
        assert not at.exception
        assert [s.value for s in at.subheader] == ["doc_1.pdf"]

    def test_own_draft_shows_submit_button(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        at = _run_app(monkeypatch, [_record("doc_1", status="draft")])
        at.button(key="detail_doc_1").click().run()
        assert not at.exception
        assert "📤 Enviar a revisión" in _button_labels(at)

    @pytest.mark.parametrize(
        ("status", "owner"),
        [
            ("draft", "usr_other"),  # borrador de un tercero (aunque sea admin)
            ("published", "usr_author"),
            ("pending_review", "usr_author"),
            ("withdrawn", "usr_author"),
        ],
    )
    def test_submit_button_hidden_when_not_allowed(
        self, monkeypatch: pytest.MonkeyPatch, status: str, owner: str
    ) -> None:
        at = _run_app(monkeypatch, [_record("doc_1", status=status, owner=owner)])
        at.button(key="detail_doc_1").click().run()
        assert not at.exception
        assert "📤 Enviar a revisión" not in _button_labels(at)

    def test_back_button_clears_selection_and_returns_to_list(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        at = _run_app(monkeypatch, [_record("doc_1")])
        at.button(key="detail_doc_1").click().run()
        assert at.subheader != []
        at.button(key="back_to_library").click().run()
        assert not at.exception
        # Vuelve al listado: sin subheader de detalle y sin selección guardada.
        assert at.subheader == []
        assert "ada_selected_document" not in at.session_state

    def test_detail_404_does_not_break_page(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Selección directa a un id inexistente: la página muestra el error
        # controlado y no crashea (cortina 404 sin leak de existencia).
        client = FakeUiClient([], "usr_author")
        monkeypatch.setattr(auth, "get_client", lambda: client)
        at = AppTest.from_file(str(SCRIPT_PATH), default_timeout=15)
        at.session_state["ada_selected_document"] = "doc_missing"
        at.run()
        assert not at.exception
        assert client.get_document_calls == ["doc_missing"]
        assert any(
            "no existe o no es visible" in e.value for e in at.error
        ), [e.value for e in at.error]

    def test_detail_uses_me_for_viewer_identity(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """La identidad del espectador viene del backend (client.me)."""
        at = _run_app(monkeypatch, [_record("doc_1", status="draft")])
        at.button(key="detail_doc_1").click().run()
        # draft propio + identidad resuelta → acción visible (no depura aquí).
        assert "📤 Enviar a revisión" in _button_labels(at)
