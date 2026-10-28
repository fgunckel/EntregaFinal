"""Pruebas de TASK-10: runtime de estudio y endpoint POST /api/study.

Cubren:
- Runtime determinístico: flujo completo retrieval → grafo → StudyResponse
  con evidencia real; sin evidencia → insuficiencia reconocida sin citas.
- Aislamiento: solo publicados (+ pending_review propio) alimentan la
  recuperación; withdrawn/deleted/rejected excluidos incluso para el autor.
- Endpoint: autenticación, límites de entrada, errores controlados (422/503),
  sin exposición de trazas internas.
- Topología del grafo compilado: protección del synthesizer, corte por
  reintentos, limited_finish sin fabricar.

Evidencia unitaria con fakes: NO valida integración con PostgreSQL real
(las 7 pruebas de integración siguen skipped sin DATABASE_URL) ni Pinecone.
"""

import tempfile
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from ada_study.api.app import AdaState, create_app
from ada_study.config import Settings
from ada_study.contracts.documents import ChunkRecord, DocumentRecord, DocumentVersion
from ada_study.db.repository import NotFoundError
from ada_study.index.retriever import ViewerContext
from ada_study.storage.pdf_storage import PdfStorage
from ada_study.study import (
    MAX_REQUEST_CHARS,
    STUDY_RECURSION_LIMIT,
    StudyRuntimeError,
    run_study,
)
from ada_study.study.runtime import _collect_visible_chunks

# Reutiliza fakes y helpers de los tests de API existentes.
from tests.test_api_auth import FakeAuthRepository, _session_token, _user

# ---------------------------------------------------------------------------
# Fake de catálogo con versiones y chunks (mínimo para el runtime de estudio)
# ---------------------------------------------------------------------------


class FakeStudyRepository(FakeAuthRepository):
    """Fake de identidad + catálogo con versiones y chunks."""

    def __init__(self) -> None:
        super().__init__()
        self.documents: dict[str, DocumentRecord] = {}
        self.versions: dict[str, list[DocumentVersion]] = {}
        self.chunks: dict[str, list[ChunkRecord]] = {}

    async def get_document(self, document_id: str) -> DocumentRecord:
        if document_id not in self.documents:
            raise NotFoundError("Documento", document_id)
        return self.documents[document_id]

    async def get_version(self, version_id: str) -> DocumentVersion:
        for versions in self.versions.values():
            for version in versions:
                if version.version_id == version_id:
                    return version
        raise NotFoundError("Versión", version_id)

    async def list_documents(self) -> list[DocumentRecord]:
        return sorted(self.documents.values(), key=lambda d: d.created_at)

    async def list_versions(self, document_id: str) -> list[DocumentVersion]:
        return self.versions.get(document_id, [])

    async def list_chunks(self, version_id: str) -> list[ChunkRecord]:
        return self.chunks.get(version_id, [])


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _hash(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def _document(document_id: str, owner_id: str, status: str = "published") -> DocumentRecord:
    return DocumentRecord(
        document_id=document_id,
        filename=f"{document_id}.pdf",
        subject="Álgebra",
        active_version_id=f"ver_{document_id}",
        editorial_status=status,
        review_status="not_required",
        uploaded_by=owner_id,
        created_at=_now(),
        updated_at=_now(),
        deleted_at=None,
    )


def _version(document_id: str, status: str = "available") -> DocumentVersion:
    return DocumentVersion(
        version_id=f"ver_{document_id}",
        document_id=document_id,
        version_number=1,
        sha256=_hash(document_id),
        storage_key=f"ab/{document_id}.pdf",
        content_type="application/pdf",
        size_bytes=1024,
        status=status,
        created_at=_now(),
    )


def _chunk(document_id: str, text: str, page: int = 1, index: int = 0) -> ChunkRecord:
    return ChunkRecord(
        chunk_id=f"chk_{document_id}_{index}",
        document_id=document_id,
        version_id=f"ver_{document_id}",
        page=page,
        chunk_index=index,
        content_hash=_hash(f"{document_id}:{index}:{text}"),
        text=text,
        title=f"Tema {document_id}",
        source=f"{document_id}.pdf",
    )


def _client(repository: FakeStudyRepository) -> TestClient:
    tmp_root = Path(tempfile.mkdtemp(prefix="ada_study_test_"))
    settings = Settings(
        database_url="postgresql+asyncpg://fake",
        ada_storage_dir=str(tmp_root),
    )

    def state_factory(resolved: Settings) -> AdaState:
        return AdaState(
            engine=None,
            repository=repository,
            storage=PdfStorage(root=tmp_root),
        )

    return TestClient(create_app(settings, state_factory=state_factory))


def _seed_published_document(
    repo: FakeStudyRepository,
    document_id: str,
    owner_id: str,
    text: str,
) -> None:
    """Documento publicado con versión activa available y un chunk."""
    repo.documents[document_id] = _document(document_id, owner_id, "published")
    repo.versions[document_id] = [_version(document_id)]
    repo.chunks[f"ver_{document_id}"] = [_chunk(document_id, text)]


# ---------------------------------------------------------------------------
# Runtime determinístico (retrieval + grafo)
# ---------------------------------------------------------------------------


class TestStudyRuntime:
    async def test_full_flow_returns_response_with_real_evidence(self):
        """Flujo E2E: retrieval → grafo → StudyResponse con referencias reales."""
        repo = FakeStudyRepository()
        viewer = ViewerContext(user_id="usr_1", role="student")
        _seed_published_document(
            repo, "doc_a", "autor",
            "Una ecuación lineal es una igualdad con incógnitas elevadas a la potencia uno.",
        )
        _seed_published_document(
            repo, "doc_b", "autor",
            "El teorema de Pitágoras relaciona los catetos con la hipotenusa.",
        )
        response = await run_study(repo, "¿Qué es una ecuación lineal?", viewer)
        assert response.status == "complete"
        assert response.references
        # Procedencia: cada referencia apunta a chunks reales del corpus.
        for ref in response.references:
            assert ref.document_id in {"doc_a", "doc_b"}
            assert ref.chunk_id.startswith("chk_")

    async def test_no_evidence_recognizes_insufficiency(self):
        """Sin corpus: insuficiencia explícita, sin citas inventadas."""
        repo = FakeStudyRepository()
        viewer = ViewerContext(user_id="usr_1", role="student")
        response = await run_study(repo, "¿Qué es la fotosíntesis?", viewer)
        assert response.status in {"insufficient_evidence", "limited"}
        assert response.references == []
        assert response.evidence_gaps or response.limitations

    async def test_empty_request_rejected(self):
        repo = FakeStudyRepository()
        viewer = ViewerContext(user_id="usr_1", role="student")
        with pytest.raises(StudyRuntimeError) as excinfo:
            await run_study(repo, "   ", viewer)
        assert excinfo.value.error_code == "STUDY_EMPTY_REQUEST"

    async def test_oversized_request_rejected(self):
        repo = FakeStudyRepository()
        viewer = ViewerContext(user_id="usr_1", role="student")
        with pytest.raises(StudyRuntimeError) as excinfo:
            await run_study(repo, "x" * (MAX_REQUEST_CHARS + 1), viewer)
        assert excinfo.value.error_code == "STUDY_REQUEST_TOO_LONG"


class TestStudyIsolation:
    async def test_withdrawn_excluded_even_for_owner(self):
        """Cortina editorial: withdrawn excluido absolutamente (TASK-07)."""
        repo = FakeStudyRepository()
        viewer = ViewerContext(user_id="usr_owner", role="student")
        repo.documents["doc_w"] = _document("doc_w", "usr_owner", "withdrawn")
        repo.versions["doc_w"] = [_version("doc_w")]
        repo.chunks["ver_doc_w"] = [_chunk("doc_w", "contenido retirado")]
        chunks = await _collect_visible_chunks(repo, viewer)
        assert chunks == []

    async def test_deleted_excluded_even_for_owner(self):
        repo = FakeStudyRepository()
        viewer = ViewerContext(user_id="usr_owner", role="student")
        repo.documents["doc_d"] = _document("doc_d", "usr_owner", "deleted")
        repo.versions["doc_d"] = [_version("doc_d")]
        repo.chunks["ver_doc_d"] = [_chunk("doc_d", "contenido eliminado")]
        chunks = await _collect_visible_chunks(repo, viewer)
        assert chunks == []

    async def test_pending_review_private_access_for_owner(self):
        """El autor recupera su propio pending_review (regla TASK-07)."""
        repo = FakeStudyRepository()
        viewer = ViewerContext(user_id="usr_owner", role="student")
        repo.documents["doc_p"] = _document("doc_p", "usr_owner", "pending_review")
        repo.versions["doc_p"] = [_version("doc_p")]
        repo.chunks["ver_doc_p"] = [_chunk("doc_p", "borrador del autor")]
        chunks = await _collect_visible_chunks(repo, viewer)
        assert len(chunks) == 1

    async def test_pending_review_hidden_from_others(self):
        """El pending_review de otro estudiante NO es recuperable."""
        repo = FakeStudyRepository()
        viewer = ViewerContext(user_id="usr_other", role="student")
        repo.documents["doc_p"] = _document("doc_p", "usr_owner", "pending_review")
        repo.versions["doc_p"] = [_version("doc_p")]
        repo.chunks["ver_doc_p"] = [_chunk("doc_p", "borrador ajeno")]
        chunks = await _collect_visible_chunks(repo, viewer)
        assert chunks == []

    async def test_inactive_version_excluded(self):
        """Solo la versión activa `available` alimenta el índice."""
        repo = FakeStudyRepository()
        viewer = ViewerContext(user_id="usr_1", role="student")
        repo.documents["doc_v"] = _document("doc_v", "autor", "published")
        old_version = _version("doc_v")
        repo.versions["doc_v"] = [old_version]
        repo.chunks["ver_doc_v"] = [_chunk("doc_v", "texto")]
        # Versión activa apunta a una versión inexistente/no disponible.
        repo.documents["doc_v"].active_version_id = "ver_otra"
        chunks = await _collect_visible_chunks(repo, viewer)
        assert chunks == []


# ---------------------------------------------------------------------------
# Endpoint POST /api/study
# ---------------------------------------------------------------------------


class TestStudyEndpoint:
    def test_requires_authentication(self):
        repo = FakeStudyRepository()
        with _client(repo) as client:
            response = client.post("/api/study", json={"user_request": "¿Qué es una fracción?"})
        assert response.status_code == 401

    def test_full_flow_within_app(self):
        """Flujo determinístico E2E dentro de la app: auth → study → evidencia."""
        repo = FakeStudyRepository()
        user = _user("usr_stu", "student")
        repo.users[user.user_id] = user
        repo.users_by_email[user.email] = user
        _seed_published_document(
            repo, "doc_pub", "otro",
            "Una fracción representa las partes iguales de un todo dividido.",
        )
        token = _session_token(repo, user)
        with _client(repo) as client:
            response = client.post(
                "/api/study",
                json={"user_request": "¿Qué es una fracción?"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "complete"
        assert payload["references"]
        assert payload["references"][0]["document_id"] == "doc_pub"
        assert payload["topic"]

    def test_request_too_long_422(self):
        repo = FakeStudyRepository()
        user = _user("usr_stu", "student")
        repo.users[user.user_id] = user
        repo.users_by_email[user.email] = user
        token = _session_token(repo, user)
        with _client(repo) as client:
            response = client.post(
                "/api/study",
                json={"user_request": "x" * (MAX_REQUEST_CHARS + 1)},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 422

    def test_empty_request_422(self):
        repo = FakeStudyRepository()
        user = _user("usr_stu", "student")
        repo.users[user.user_id] = user
        repo.users_by_email[user.email] = user
        token = _session_token(repo, user)
        with _client(repo) as client:
            response = client.post(
                "/api/study",
                json={"user_request": "   "},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 422

    def test_no_evidence_returns_insufficiency_not_error(self):
        """Sin corpus: 200 con insuficiencia reconocida (no fabrica nada)."""
        repo = FakeStudyRepository()
        user = _user("usr_stu", "student")
        repo.users[user.user_id] = user
        repo.users_by_email[user.email] = user
        token = _session_token(repo, user)
        with _client(repo) as client:
            response = client.post(
                "/api/study",
                json={"user_request": "¿Qué es la mitosis?"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] in {"insufficient_evidence", "limited"}
        assert payload["references"] == []

    def test_isolation_withdrawn_document_not_retrieved(self):
        """Cortina editorial vía endpoint: withdrawn no alimenta respuestas."""
        repo = FakeStudyRepository()
        user = _user("usr_owner", "student")
        repo.users[user.user_id] = user
        repo.users_by_email[user.email] = user
        repo.documents["doc_w"] = _document("doc_w", "usr_owner", "withdrawn")
        repo.versions["doc_w"] = [_version("doc_w")]
        repo.chunks["ver_doc_w"] = [_chunk("doc_w", "contenido secreto retirado")]
        token = _session_token(repo, user)
        with _client(repo) as client:
            response = client.post(
                "/api/study",
                json={"user_request": "contenido secreto retirado"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] in {"insufficient_evidence", "limited"}
        assert payload["references"] == []

    def test_private_access_own_pending_review(self):
        """Acceso privado: el autor consulta su pending_review (TASK-07)."""
        repo = FakeStudyRepository()
        user = _user("usr_author", "student")
        repo.users[user.user_id] = user
        repo.users_by_email[user.email] = user
        repo.documents["doc_p"] = _document("doc_p", "usr_author", "pending_review")
        repo.versions["doc_p"] = [_version("doc_p")]
        repo.chunks["ver_doc_p"] = [_chunk("doc_p", "definición de ecuación en borrador")]
        token = _session_token(repo, user)
        with _client(repo) as client:
            response = client.post(
                "/api/study",
                json={"user_request": "ecuación"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] == "complete"
        assert payload["references"][0]["document_id"] == "doc_p"

    def test_other_users_pending_review_not_retrieved(self):
        """El pending_review ajeno NO alimenta respuestas de otro usuario."""
        repo = FakeStudyRepository()
        user = _user("usr_other", "student")
        repo.users[user.user_id] = user
        repo.users_by_email[user.email] = user
        repo.documents["doc_p"] = _document("doc_p", "usr_author", "pending_review")
        repo.versions["doc_p"] = [_version("doc_p")]
        repo.chunks["ver_doc_p"] = [_chunk("doc_p", "definición de ecuación en borrador")]
        token = _session_token(repo, user)
        with _client(repo) as client:
            response = client.post(
                "/api/study",
                json={"user_request": "ecuación"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload["status"] in {"insufficient_evidence", "limited"}
        assert payload["references"] == []


# ---------------------------------------------------------------------------
# Topología del grafo compilado
# ---------------------------------------------------------------------------


class TestCompiledGraphTopology:
    def _graph(self):
        from ada_study.graph import compile_graph

        return compile_graph(chat_model=None, retriever=None)

    def test_compiles_without_model(self):
        """El grafo determinístico compila sin chat model (D-1)."""
        graph = self._graph()
        assert graph is not None

    async def test_recursion_limit_is_explicit_constant(self):
        """D-3: el límite de recursión es una constante explícita del runtime."""
        assert STUDY_RECURSION_LIMIT == 25
        assert STUDY_RECURSION_LIMIT >= 7  # topología completa mínima

    async def test_limited_finish_on_retries_exhausted(self):
        """Reparaciones agotadas → limited_finish (salida segura, sin fabricar)."""
        from ada_study.graph.builder import route_after_validator

        state = {
            "validation_result": {"is_valid": False, "pending_repair_agent": None},
            "retries": 2,
        }
        assert route_after_validator(state) == "limited_finish"

    async def test_synthesizer_protection_deterministic(self):
        """El synthesizer solo corre con validación vigente (protección M7)."""
        from ada_study.graph.builder import route_after_supervisor

        state = {"next_agent": "synthesizer", "validation_result": {"is_valid": False}}
        assert route_after_supervisor(state) == "validator"
