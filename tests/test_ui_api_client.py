"""Pruebas del cliente HTTP de la UI (TASK-08).

Ejercitan `ApiClient` (puro, sin Streamlit) contra la app FastAPI con
repositorio fake vía TestClient de Starlette montado en httpx. Cubren los
flujos de la UI: login → me → biblioteca → detalle → acciones → logout.

Evidencia unitaria con fakes: NO valida integración con PostgreSQL real.
"""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ada_study.api.app import AdaState, create_app
from ada_study.config import Settings
from ada_study.contracts.documents import DocumentRecord
from ada_study.db.repository import NotFoundError
from ada_study.storage.pdf_storage import PdfStorage
from ada_study.ui.api_client import ApiClient, ApiError

# Reutiliza el fake de identidad del test de autenticación.
from tests.test_api_auth import FakeAuthRepository, _session_token, _user  # noqa: F401

# ---------------------------------------------------------------------------
# Fake con catálogo mínimo (biblioteca + detalle)
# ---------------------------------------------------------------------------


class FakeCatalogRepository(FakeAuthRepository):
    """Fake de identidad + catálogo mínimo para los flujos de la UI."""

    def __init__(self) -> None:
        super().__init__()
        self.documents: dict[str, object] = {}

    async def get_document(self, document_id: str):

        if document_id not in self.documents:
            raise NotFoundError("Documento", document_id)
        return self.documents[document_id]

    async def list_documents(self):

        return sorted(self.documents.values(), key=lambda d: d.created_at)

    async def update_editorial_status(self, document_id: str, target: str) -> None:
        from datetime import UTC, datetime

        document = self.documents[document_id]
        document.editorial_status = target
        document.updated_at = datetime.now(UTC).isoformat()


def _document(document_id: str, owner_id: str, status: str = "published"):

    now = datetime.now(UTC).isoformat()
    return DocumentRecord(
        document_id=document_id,
        filename=f"{document_id}.pdf",
        subject="Álgebra",
        active_version_id=None,
        editorial_status=status,
        review_status="not_required",
        uploaded_by=owner_id,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )


def _asgi_client(repository) -> TestClient:
    """TestClient de FastAPI (httpx.Client síncrono, sin red)."""
    return TestClient(_build_app(repository))


class TestUiClientFlows:
    def test_login_me_library_flow(self):
        """Flujo completo de la UI: login → me → biblioteca → detalle."""
        repo = FakeCatalogRepository()
        user = _user("usr_ui", "student")
        repo.users[user.user_id] = user
        repo.users_by_email[user.email] = user
        repo.documents["doc_pub"] = _document("doc_pub", "otro", status="published")

        # TestClient de FastAPI ES un httpx.Client síncrono contra la app
        # ASGI (sin red): se inyecta en el cliente puro de la UI. Con el
        # context manager corre el lifespan (crea app.state.ada).
        with TestClient(_build_app(repo)) as http:
            client = ApiClient("http://testserver", http=http)
            # Login real (B1) contra la app (sin red).
            token = client.login(user.email, "secreto123")
            client = client.with_token(token)

            # Perfil desde la sesión (B3): identidad del backend.
            me = client.me()
            assert me.user_id == user.user_id
            assert me.role == "student"

            # Biblioteca real (publicados compartidos + propios).
            documents = client.list_documents()
            assert [d.document_id for d in documents] == ["doc_pub"]

    def test_me_without_token_raises(self):
        client = ApiClient("http://testserver")
        with pytest.raises(ApiError) as excinfo:
            client.me()
        assert excinfo.value.error_code == "AUTH_SESSION_INVALID"

    def test_document_not_visible_404_without_existence_leak(self):
        """Aislamiento: documento ajeno no publicado → 404 (no 403)."""
        repo = FakeCatalogRepository()
        user = _user("usr_iso")
        repo.users[user.user_id] = user
        repo.documents["doc_private"] = _document("doc_private", "otro", status="pending_review")
        token = _session_token(repo, user)
        with TestClient(_build_app(repo)) as http:
            client = ApiClient("http://testserver", token, http=http)
            with pytest.raises(ApiError) as excinfo:
                client.get_document("doc_private")
        assert excinfo.value.status_code == 404
        assert excinfo.value.error_code == "DEL_NOT_FOUND"

    def test_moderation_queue_requires_role(self):
        """Student no accede a la cola (frontera en el backend)."""
        repo = FakeCatalogRepository()
        student = _user("usr_stu")
        repo.users[student.user_id] = student
        token = _session_token(repo, student)
        with TestClient(_build_app(repo)) as http:
            client = ApiClient("http://testserver", token, http=http)
            with pytest.raises(ApiError) as excinfo:
                client.moderation_queue()
        assert excinfo.value.error_code == "AUTH_FORBIDDEN"

    def test_moderator_decides_with_reason(self):
        """Decisión real de moderación con motivo obligatorio."""
        repo = FakeCatalogRepository()
        moderator = _user("usr_mod", "moderator")
        repo.users[moderator.user_id] = moderator
        repo.documents["doc_p"] = _document("doc_p", "autor", status="pending_review")
        token = _session_token(repo, moderator)
        with TestClient(_build_app(repo)) as http:
            client = ApiClient("http://testserver", token, http=http)
            updated = client.decide("doc_p", "approve")
        assert updated["editorial_status"] == "published"

    def test_admin_parses_moderation_queue_200(self):
        """Flujo 200 real: el cliente UI parsea la cola (`{"queue": [...]}`).

        Cubre admin accediendo a la cola (fix de `can_moderate`) y el
        parseo de la clave `queue` del payload (regresión del crash de
        Streamlit por clave `documents` inexistente).
        """
        repo = FakeCatalogRepository()
        admin = _user("usr_adm_ui", "admin")
        repo.users[admin.user_id] = admin
        repo.documents["doc_p"] = _document("doc_p", "autor", status="pending_review")
        repo.documents["doc_post"] = _document(
            "doc_post", "otro", status="published_pending_review"
        )
        repo.documents["doc_pub"] = _document("doc_pub", "otro", status="published")
        token = _session_token(repo, admin)
        with TestClient(_build_app(repo)) as http:
            client = ApiClient("http://testserver", token, http=http)
            queue = client.moderation_queue()
        assert [d.document_id for d in queue] == ["doc_p", "doc_post"]
        assert all(
            d.editorial_status in ("pending_review", "published_pending_review")
            for d in queue
        )
        assert all(isinstance(d, DocumentRecord) for d in queue)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_app(repository):

    tmp_root = Path(tempfile.mkdtemp(prefix="ada_ui_test_"))
    settings = Settings(
        database_url="postgresql+asyncpg://fake",
        ada_storage_dir=str(tmp_root),
        _env_file=None,
    )

    def state_factory(resolved: Settings) -> AdaState:
        return AdaState(
            engine=None,
            repository=repository,
            storage=PdfStorage(root=tmp_root),
        )

    return create_app(settings, state_factory=state_factory)
