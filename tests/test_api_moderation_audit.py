"""Pruebas de las rutas de moderación y auditoría (TASK-07, Fases H2/H3).

Cubren:
- POST /api/moderation/documents/{id}/decide: permisos positivos y negativos,
  motivo obligatorio en reject/withdraw, transiciones inválidas (409),
  aislamiento (404 sin revelar existencia) y auditoría de la decisión.
- GET /api/moderation/queue: solo moderator/admin; cola con pendientes.
- GET /api/audit/events: lectura restringida (moderator/admin), filtros,
  y verificación de append-only (sin update/delete en el repositorio).

Usan la app FastAPI con repositorio fake en memoria; los servicios de dominio
(ModerationService, IdentityService) son los REALES.
"""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from ada_study.api.app import AdaState, create_app
from ada_study.config import Settings
from ada_study.contracts.documents import DocumentRecord, DocumentVersion
from ada_study.contracts.identity import AuditEvent, SessionRecord, UserRecord
from ada_study.db.repository import NotFoundError
from ada_study.storage.pdf_storage import PdfStorage

# ---------------------------------------------------------------------------
# Repositorio fake mínimo (catálogo + auditoría + identidad)
# ---------------------------------------------------------------------------


class FakeModerationRepository:
    """Réplica en memoria del contrato usado por moderación y auditoría."""

    def __init__(self) -> None:
        self.documents: dict[str, DocumentRecord] = {}
        self.versions: dict[str, DocumentVersion] = {}
        self.audit_events: list[AuditEvent] = []
        self.users: dict[str, UserRecord] = {}
        self.sessions: dict[str, SessionRecord] = {}
        self.pending_ops: dict[str, object] = {}

    async def get_document(self, document_id: str) -> DocumentRecord:
        if document_id not in self.documents:
            raise NotFoundError("Documento", document_id)
        return self.documents[document_id]

    async def list_documents(self) -> list[DocumentRecord]:
        return sorted(self.documents.values(), key=lambda d: d.created_at)

    async def get_version(self, version_id: str) -> DocumentVersion:
        if version_id not in self.versions:
            raise NotFoundError("Versión", version_id)
        return self.versions[version_id]

    async def list_versions(self, document_id: str) -> list[DocumentVersion]:
        return sorted(
            (v for v in self.versions.values() if v.document_id == document_id),
            key=lambda v: v.version_number,
        )

    async def update_editorial_status(self, document_id: str, target: str) -> None:
        document = self.documents[document_id]
        document.editorial_status = target
        document.updated_at = datetime.now(UTC).isoformat()

    async def enqueue_pending_op(self, op) -> None:
        self.pending_ops[op.op_id] = op

    # -- auditoría (append-only: sin update/delete) --------------------------

    async def append_audit_event(self, event: AuditEvent) -> None:
        self.audit_events.append(event)

    async def list_audit_events(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        events = list(self.audit_events)
        if entity_type is not None and entity_id is not None:
            events = [
                e
                for e in events
                if e.entity_type == entity_type and e.entity_id == entity_id
            ]
        return list(reversed(events))[:limit]

    # -- identidad ------------------------------------------------------------

    async def get_user(self, user_id: str) -> UserRecord:
        if user_id not in self.users:
            raise NotFoundError("Usuario", user_id)
        return self.users[user_id]

    async def get_session_by_token_hash(self, token_hash: str) -> SessionRecord | None:
        for session in self.sessions.values():
            if session.token_hash == token_hash:
                return session
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _user(user_id: str, role: str) -> UserRecord:
    now = datetime.now(UTC).isoformat()
    return UserRecord(
        user_id=user_id,
        email=f"{user_id}@test.local",
        display_name=user_id,
        role=role,
        account_status="active",
        created_at=now,
        updated_at=now,
    )


def _session_token(repository: FakeModerationRepository, user: UserRecord) -> str:
    from ada_study.domain.identity_service import hash_token, new_session_token

    token = new_session_token()
    now = datetime.now(UTC)
    record = SessionRecord(
        session_id=f"ses_{user.user_id}",
        user_id=user.user_id,
        token_hash=hash_token(token),
        created_at=now.isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(),
    )
    repository.sessions[record.session_id] = record
    return token


def _client(repository: FakeModerationRepository) -> TestClient:
    tmp_root = Path(tempfile.mkdtemp(prefix="ada_mod_test_"))
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

    app = create_app(settings, state_factory=state_factory)
    return TestClient(app)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _document(document_id: str, owner_id: str, status: str = "pending_review") -> DocumentRecord:
    now = datetime.now(UTC).isoformat()
    return DocumentRecord(
        document_id=document_id,
        filename=f"{document_id}.pdf",
        subject=None,
        active_version_id=None,
        editorial_status=status,
        review_status="not_required",
        uploaded_by=owner_id,
        created_at=now,
        updated_at=now,
        deleted_at=None,
    )


# ---------------------------------------------------------------------------
# Moderación: decide
# ---------------------------------------------------------------------------


class TestModerationDecide:
    async def test_moderator_approves_pending_review(self):
        repo = FakeModerationRepository()
        moderator = _user("usr_mod", "moderator")
        repo.users[moderator.user_id] = moderator
        repo.documents["doc_1"] = _document("doc_1", "usr_author")
        token = _session_token(repo, moderator)
        with _client(repo) as client:
            response = client.post(
                "/api/moderation/documents/doc_1/decide",
                headers=_auth(token),
                json={"action": "approve"},
            )
        assert response.status_code == 200
        assert response.json()["editorial_status"] == "published"
        # La decisión queda auditada con actor y acción.
        assert any(
            event.action == "moderation.approve" for event in repo.audit_events
        )

    async def test_student_cannot_decide_403(self):
        repo = FakeModerationRepository()
        student = _user("usr_stu", "student")
        repo.users[student.user_id] = student
        repo.documents["doc_1"] = _document("doc_1", "usr_author")
        token = _session_token(repo, student)
        with _client(repo) as client:
            response = client.post(
                "/api/moderation/documents/doc_1/decide",
                headers=_auth(token),
                json={"action": "approve"},
            )
        assert response.status_code == 403
        assert response.json()["error"] == "AUTH_FORBIDDEN"

    async def test_reject_without_reason_rejected_422(self):
        repo = FakeModerationRepository()
        moderator = _user("usr_mod2", "moderator")
        repo.users[moderator.user_id] = moderator
        repo.documents["doc_1"] = _document("doc_1", "usr_author")
        token = _session_token(repo, moderator)
        with _client(repo) as client:
            response = client.post(
                "/api/moderation/documents/doc_1/decide",
                headers=_auth(token),
                json={"action": "reject"},
            )
        assert response.status_code == 422
        assert response.json()["error"] == "MOD_REASON_REQUIRED"

    async def test_reject_with_reason_records_it(self):
        repo = FakeModerationRepository()
        moderator = _user("usr_mod3", "moderator")
        repo.users[moderator.user_id] = moderator
        repo.documents["doc_1"] = _document("doc_1", "usr_author")
        token = _session_token(repo, moderator)
        with _client(repo) as client:
            response = client.post(
                "/api/moderation/documents/doc_1/decide",
                headers=_auth(token),
                json={"action": "reject", "reason": "Contenido fuera de tema"},
            )
        assert response.status_code == 200
        assert response.json()["editorial_status"] == "rejected"
        event = next(
            e for e in repo.audit_events if e.action == "moderation.reject"
        )
        assert event.reason == "Contenido fuera de tema"

    async def test_withdraw_enqueues_deindex_ops(self):
        repo = FakeModerationRepository()
        moderator = _user("usr_mod3", "moderator")
        repo.users[moderator.user_id] = moderator
        doc = _document("doc_1", "usr_author", status="published")
        doc.active_version_id = "ver_1"
        repo.documents["doc_1"] = doc
        token = _session_token(repo, moderator)
        with _client(repo) as client:
            response = client.post(
                "/api/moderation/documents/doc_1/decide",
                headers=_auth(token),
                json={"action": "withdraw", "reason": "Duplicado de otro aporte"},
            )
        assert response.status_code == 200
        assert response.json()["editorial_status"] == "withdrawn"
        # Desindexación encolada por destino (Spec 06 §104).
        ops = list(repo.pending_ops.values())
        assert len(ops) == 2

    async def test_invalid_transition_rejected_409(self):
        repo = FakeModerationRepository()
        moderator = _user("usr_mod4", "moderator")
        repo.users[moderator.user_id] = moderator
        # Un documento `draft` no puede aprobarse directamente (ADR-0005).
        repo.documents["doc_1"] = _document("doc_1", "usr_author", status="draft")
        token = _session_token(repo, moderator)
        with _client(repo) as client:
            response = client.post(
                "/api/moderation/documents/doc_1/decide",
                headers=_auth(token),
                json={"action": "approve"},
            )
        assert response.status_code == 409

    async def test_unknown_document_404(self):
        repo = FakeModerationRepository()
        moderator = _user("usr_mod5", "moderator")
        repo.users[moderator.user_id] = moderator
        token = _session_token(repo, moderator)
        with _client(repo) as client:
            response = client.post(
                "/api/moderation/documents/doc_missing/decide",
                headers=_auth(token),
                json={"action": "approve"},
            )
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Moderación: cola
# ---------------------------------------------------------------------------


class TestModerationQueue:
    async def test_moderator_sees_pending_queue(self):
        repo = FakeModerationRepository()
        moderator = _user("usr_modq", "moderator")
        repo.users[moderator.user_id] = moderator
        repo.documents["doc_p"] = _document("doc_p", "usr_a", status="pending_review")
        repo.documents["doc_post"] = _document(
            "doc_post", "usr_b", status="published_pending_review"
        )
        repo.documents["doc_pub"] = _document(
            "doc_pub", "usr_c", status="published"
        )
        token = _session_token(repo, moderator)
        with _client(repo) as client:
            response = client.get("/api/moderation/queue", headers=_auth(token))
        assert response.status_code == 200
        ids = {d["document_id"] for d in response.json()["queue"]}
        assert ids == {"doc_p", "doc_post"}

    async def test_student_cannot_see_queue_403(self):
        repo = FakeModerationRepository()
        student = _user("usr_stu_q", "student")
        repo.users[student.user_id] = student
        token = _session_token(repo, student)
        with _client(repo) as client:
            response = client.get("/api/moderation/queue", headers=_auth(token))
        assert response.status_code == 403


# ---------------------------------------------------------------------------
# Auditoría: lectura restringida y append-only
# ---------------------------------------------------------------------------


class TestAuditEvents:
    async def test_moderator_reads_audit_events(self):
        repo = FakeModerationRepository()
        moderator = _user("usr_mod_a", "moderator")
        repo.users[moderator.user_id] = moderator
        repo.audit_events.append(
            AuditEvent(
                event_id="evt_1",
                actor_id="usr_mod",
                action="moderation.approve",
                entity_type="document",
                entity_id="doc_1",
                result="ok",
                reason=None,
                created_at=datetime.now(UTC).isoformat(),
            )
        )
        token = _session_token(repo, moderator)
        with _client(repo) as client:
            response = client.get("/api/audit/events", headers=_auth(token))
        assert response.status_code == 200
        events = response.json()["events"]
        assert len(events) == 1
        assert events[0]["action"] == "moderation.approve"

    async def test_student_cannot_read_audit_403(self):
        repo = FakeModerationRepository()
        student = _user("usr_stu_a", "student")
        repo.users[student.user_id] = student
        token = _session_token(repo, student)
        with _client(repo) as client:
            response = client.get("/api/audit/events", headers=_auth(token))
        assert response.status_code == 403
        assert response.json()["error"] == "AUTH_FORBIDDEN"

    async def test_audit_is_append_only(self):
        """El repositorio NO expone update/delete para audit_events (Spec 06 §88)."""
        from ada_study.db.repository import CatalogRepository

        for method in ("update_audit_event", "delete_audit_event"):
            assert not hasattr(CatalogRepository, method), method
        # La única vía de escritura es append.
        assert hasattr(CatalogRepository, "append_audit_event")

    async def test_no_secrets_in_audit_events(self):
        """Los eventos no contienen tokens ni hashes (Spec 06 §86)."""
        repo = FakeModerationRepository()
        moderator = _user("usr_mod_s", "moderator")
        repo.users[moderator.user_id] = moderator
        repo.documents["doc_1"] = _document("doc_1", "usr_author")
        token = _session_token(repo, moderator)
        with _client(repo) as client:
            client.post(
                "/api/moderation/documents/doc_1/decide",
                headers=_auth(token),
                json={"action": "approve"},
            )
        for event in repo.audit_events:
            serialized = event.model_dump_json()
            assert token not in serialized
            assert "token_hash" not in serialized
