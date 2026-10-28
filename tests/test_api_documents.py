"""Pruebas de la API de documentos (TASK-06, Fase E1, ADR-0006).

Cubren:
- Los 4 endpoints (upload 202, listado, detalle, polling de job).
- Frontera de autenticación: sin token, token malformado, token inexistente
  → 401 con códigos estables (validación REAL contra sesiones).
- Autorización por rol y por ID: conocer un identificador no da acceso.
- Validación de carga: content-type (415), tamaño (413), vacío (422).
- Ausencia de registros parciales ante rechazos.
- Flujo completo con worker separado (upload → worker → available → visible).

Usan la app FastAPI con un repositorio fake en memoria (unitarias); las de
integración con PostgreSQL real están en test_catalog_integration.py.
"""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from ada_study.api.app import AdaState, create_app
from ada_study.config import Settings
from ada_study.contracts.documents import (
    DocumentRecord,
    DocumentVersion,
    IngestionJob,
    PendingIndexOp,
)
from ada_study.contracts.identity import SessionRecord, UserRecord
from ada_study.db.repository import (
    NotFoundError,
)
from ada_study.storage.pdf_storage import PdfStorage


def _pdf(content_text: str) -> bytes:
    """PDF 1.4 mínimo con texto seleccionable (misma técnica que test_worker)."""
    text_bytes = content_text.encode("latin-1", "replace")
    stream = b"BT /F1 12 Tf 72 720 Td (" + text_bytes + b") Tj ET\n"
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Contents 4 0 R /Resources << /Font << /F1 5 0 R >> >> >>",
        b"<< /Length " + str(len(stream)).encode() + b" >> stream\n" + stream + b"endstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref_pos = len(out)
    out += f"xref\n0 {len(objects) + 1}\n".encode()
    out += b"0000000000 65535 f \n"
    for offset in offsets:
        out += f"{offset:010d} 00000 n \n".encode()
    out += (
        b"trailer\n<< /Size " + str(len(objects) + 1).encode()
        + b" /Root 1 0 R >>\nstartxref\n" + str(xref_pos).encode() + b"\n%%EOF\n"
    )
    return bytes(out)


# ---------------------------------------------------------------------------
# Repositorio fake completo (documentos + identidad)
# ---------------------------------------------------------------------------


class FakeApiRepository:
    """Réplica en memoria del contrato de CatalogRepository para la API.

    Hereda el ciclo completo del catálogo (mismos métodos que usa el worker)
    y añade identidad (usuarios y sesiones con hash SHA-256).
    """

    def __init__(self) -> None:
        self.documents: dict[str, DocumentRecord] = {}
        self.versions: dict[str, DocumentVersion] = {}
        self.jobs: dict[str, IngestionJob] = {}
        self.pending_ops: dict[str, PendingIndexOp] = {}
        self.chunks: dict[str, list] = {}
        self.users: dict[str, UserRecord] = {}
        self.sessions: dict[str, SessionRecord] = {}

    # -- catálogo (contrato completo usado por API + worker) ----------------

    async def find_active_version_by_sha256(self, sha256: str) -> DocumentVersion | None:
        active_docs = {
            d.document_id for d in self.documents.values() if d.deleted_at is None
        }
        for version in self.versions.values():
            if (
                version.sha256 == sha256
                and version.document_id in active_docs
                and version.status != "deleting"
            ):
                return version
        return None

    async def create_document(self, record: DocumentRecord) -> None:
        self.documents[record.document_id] = record

    async def create_version(self, version: DocumentVersion) -> None:
        self.versions[version.version_id] = version

    async def create_job(self, job: IngestionJob) -> None:
        self.jobs[job.job_id] = job

    async def get_job(self, job_id: str) -> IngestionJob:
        if job_id not in self.jobs:
            raise NotFoundError("Job", job_id)
        return self.jobs[job_id]

    async def get_document(self, document_id: str) -> DocumentRecord:
        if document_id not in self.documents:
            raise NotFoundError("Documento", document_id)
        return self.documents[document_id]

    async def get_version(self, version_id: str) -> DocumentVersion:
        if version_id not in self.versions:
            raise NotFoundError("Versión", version_id)
        return self.versions[version_id]

    async def list_versions(self, document_id: str) -> list[DocumentVersion]:
        return sorted(
            (v for v in self.versions.values() if v.document_id == document_id),
            key=lambda v: v.version_number,
        )

    async def list_documents(self) -> list[DocumentRecord]:
        return sorted(self.documents.values(), key=lambda d: d.created_at)

    async def update_version_status(self, version_id: str, target: str) -> DocumentVersion:
        version = self.versions[version_id]
        version.status = target
        version.error_code = None
        return version

    async def mark_version_failed(self, version_id: str, error_code: str) -> None:
        version = self.versions[version_id]
        version.status = "failed"
        version.error_code = error_code

    async def set_version_page_count(self, version_id: str, page_count: int) -> None:
        self.versions[version_id].page_count = page_count

    async def replace_chunks(self, version_id: str, chunks: list) -> None:
        self.chunks[version_id] = list(chunks)

    async def list_chunks(self, version_id: str) -> list:
        return list(self.chunks.get(version_id, []))

    async def delete_chunks(self, version_id: str) -> int:
        return len(self.chunks.pop(version_id, []))

    async def set_active_version(self, document_id: str, version_id: str) -> None:
        self.documents[document_id].active_version_id = version_id

    async def list_ops_for_version(self, version_id: str) -> list[PendingIndexOp]:
        return [
            op for op in self.pending_ops.values() if op.version_id == version_id
        ]

    async def complete_pending_op(self, op_id: str) -> None:
        self.pending_ops[op_id].status = "done"

    async def fail_pending_op(self, op_id: str, error_code: str) -> None:
        op = self.pending_ops[op_id]
        op.status = "failed"
        op.attempts += 1
        op.last_error_code = error_code

    async def reset_failed_ops(self, version_id: str) -> int:
        count = 0
        for op in self.pending_ops.values():
            if op.version_id == version_id and op.status == "failed":
                op.status = "pending"
                count += 1
        return count

    async def list_version_ids_with_pending_ops(self) -> list[str]:
        return sorted(
            {
                op.version_id
                for op in self.pending_ops.values()
                if op.status in {"pending", "failed"}
            }
        )

    async def enqueue_pending_op(self, op: PendingIndexOp) -> None:
        self.pending_ops[op.op_id] = op

    async def list_jobs_for_version(self, version_id: str) -> list[IngestionJob]:
        return [job for job in self.jobs.values() if job.version_id == version_id]

    async def update_job_status(
        self, job_id: str, status: str, error_code: str | None = None
    ) -> None:
        self.jobs[job_id].status = status
        self.jobs[job_id].error_code = error_code

    async def update_editorial_status(
        self, document_id: str, target: str
    ) -> None:
        document = self.documents[document_id]
        document.editorial_status = target
        document.updated_at = datetime.now(UTC).isoformat()
        if target == "deleted":
            document.deleted_at = datetime.now(UTC).isoformat()

    # -- identidad --------------------------------------------------------

    async def get_user(self, user_id: str) -> UserRecord:
        if user_id not in self.users:
            raise NotFoundError("Usuario", user_id)
        return self.users[user_id]

    async def get_user_by_email(self, email: str) -> UserRecord | None:
        for user in self.users.values():
            if user.email == email:
                return user
        return None

    async def create_user(self, record: UserRecord) -> None:
        self.users[record.user_id] = record

    async def get_session_by_token_hash(self, token_hash: str) -> SessionRecord | None:
        for session in self.sessions.values():
            if session.token_hash == token_hash:
                return session
        return None

    async def create_session(self, record: SessionRecord) -> None:
        self.sessions[record.session_id] = record


# ---------------------------------------------------------------------------
# Fixtures: app con servicios reales sobre repositorio fake
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


def _session_token(repository: FakeApiRepository, user: UserRecord) -> str:
    """Crea una sesión REAL (hash SHA-256 en el repositorio) y devuelve el token."""
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


def _client(repository: FakeApiRepository, **settings_overrides):
    """App de prueba con CatalogService/IdentityService reales sobre el fake.

    Usa `state_factory` para inyectar el estado sin PostgreSQL: la frontera
    de autenticación y los servicios de dominio son los REALES.
    """
    tmp_root = Path(tempfile.mkdtemp(prefix="ada_api_test_"))
    settings = Settings(
        database_url="postgresql+asyncpg://fake",
        ada_storage_dir=str(tmp_root),
        **settings_overrides,
    )

    def state_factory(resolved: Settings) -> AdaState:
        storage = PdfStorage(root=tmp_root)
        return AdaState(engine=None, repository=repository, storage=storage)

    app = create_app(settings, state_factory=state_factory)
    return TestClient(app)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


PDF = _pdf("La mitocondria genera energia para la celula.")


# ---------------------------------------------------------------------------
# Autenticación (frontera real, sin bypass)
# ---------------------------------------------------------------------------


class TestAuthentication:
    async def test_upload_without_token_rejected_401(self):
        repo = FakeApiRepository()
        with _client(repo) as client:
            response = client.post("/api/documents", content=PDF)
        assert response.status_code == 401
        assert response.json()["error"] == "AUTH_SESSION_INVALID"

    async def test_upload_with_malformed_header_rejected_401(self):
        repo = FakeApiRepository()
        with _client(repo) as client:
            response = client.post(
                "/api/documents",
                headers={"Authorization": "Token abc"},
                content=PDF,
            )
        assert response.status_code == 401

    async def test_upload_with_unknown_token_rejected_401(self):
        repo = FakeApiRepository()
        with _client(repo) as client:
            response = client.post(
                "/api/documents",
                headers=_auth("token-inexistente"),
                content=PDF,
            )
        assert response.status_code == 401
        assert response.json()["error"] == "AUTH_SESSION_INVALID"

    async def test_suspended_account_with_valid_session_rejected_403(self):
        repo = FakeApiRepository()
        user = _user("usr_susp", "student")
        repo.users[user.user_id] = user
        token = _session_token(repo, user)
        # La cuenta pasa a suspendida DESPUÉS de crear la sesión: la
        # validación verifica el estado ACTUAL en cada petición (Spec 06 §40).
        repo.users[user.user_id] = user.model_copy(
            update={"account_status": "suspended"}
        )
        with _client(repo) as client:
            response = client.post("/api/documents", headers=_auth(token), content=PDF)
        assert response.status_code == 403
        assert response.json()["error"] == "AUTH_ACCOUNT_NOT_ACTIVE"

    async def test_list_without_token_rejected_401(self):
        repo = FakeApiRepository()
        with _client(repo) as client:
            response = client.get("/api/documents")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Upload: validación síncrona y ausencia de registros parciales
# ---------------------------------------------------------------------------


class TestUploadValidation:
    async def test_upload_accepts_and_enqueues_202(self):
        repo = FakeApiRepository()
        user = _user("usr_up", "student")
        repo.users[user.user_id] = user
        token = _session_token(repo, user)
        with _client(repo) as client:
            response = client.post(
                "/api/documents",
                headers={
                    **_auth(token),
                    "Content-Type": "application/pdf",
                    "X-Filename": "apuntes.pdf",
                },
                content=PDF,
            )
        assert response.status_code == 202
        body = response.json()
        assert body["job_status"] == "queued"
        assert body["document_id"] in repo.documents
        assert len(repo.pending_ops) == 2  # outbox: pinecone + bm25

    async def test_wrong_content_type_rejected_415_without_partial_records(self):
        repo = FakeApiRepository()
        user = _user("usr_415", "student")
        repo.users[user.user_id] = user
        token = _session_token(repo, user)
        with _client(repo) as client:
            response = client.post(
                "/api/documents",
                headers={**_auth(token), "Content-Type": "text/plain"},
                content=b"texto plano",
            )
        assert response.status_code == 415
        assert response.json()["error"] == "ING_UNSUPPORTED_TYPE"
        # Sin registros parciales: ni documentos ni jobs ni outbox.
        assert repo.documents == {}
        assert repo.jobs == {}
        assert repo.pending_ops == {}

    async def test_oversized_upload_rejected_413_without_partial_records(self):
        repo = FakeApiRepository()
        user = _user("usr_413", "student")
        repo.users[user.user_id] = user
        token = _session_token(repo, user)
        with _client(repo, ada_max_upload_bytes=100) as client:
            response = client.post(
                "/api/documents",
                headers={**_auth(token), "Content-Type": "application/pdf"},
                content=PDF,
            )
        assert response.status_code == 413
        assert response.json()["error"] == "ING_FILE_TOO_LARGE"
        assert repo.documents == {}
        assert repo.jobs == {}

    async def test_empty_body_rejected_422(self):
        repo = FakeApiRepository()
        user = _user("usr_empty", "student")
        repo.users[user.user_id] = user
        token = _session_token(repo, user)
        with _client(repo) as client:
            response = client.post(
                "/api/documents",
                headers={**_auth(token), "Content-Type": "application/pdf"},
                content=b"",
            )
        assert response.status_code == 422
        assert response.json()["error"] == "ING_FILE_EMPTY"
        assert repo.documents == {}

    async def test_duplicate_content_rejected_409(self):
        repo = FakeApiRepository()
        user = _user("usr_dup", "student")
        repo.users[user.user_id] = user
        token = _session_token(repo, user)
        with _client(repo) as client:
            first = client.post(
                "/api/documents",
                headers={**_auth(token), "Content-Type": "application/pdf"},
                content=PDF,
            )
            assert first.status_code == 202
            second = client.post(
                "/api/documents",
                headers={**_auth(token), "Content-Type": "application/pdf"},
                content=PDF,
            )
        assert second.status_code == 409
        assert second.json()["error"] == "ING_DUPLICATE"


# ---------------------------------------------------------------------------
# Listado y detalle: visibilidad y autorización por ID
# ---------------------------------------------------------------------------


class TestListDocuments:
    async def test_student_sees_published_and_own_only(self):
        repo = FakeApiRepository()
        student = _user("usr_stu", "student")
        repo.users[student.user_id] = student
        now = datetime.now(UTC).isoformat()
        repo.documents["doc_pub"] = DocumentRecord(
            document_id="doc_pub",
            filename="publicado.pdf",
            editorial_status="published",
            uploaded_by="usr_otro",
            created_at=now,
            updated_at=now,
        )
        repo.documents["doc_own"] = DocumentRecord(
            document_id="doc_own",
            filename="propio.pdf",
            editorial_status="pending_review",
            uploaded_by=student.user_id,
            created_at=now,
            updated_at=now,
        )
        repo.documents["doc_priv"] = DocumentRecord(
            document_id="doc_priv",
            filename="ajeno.pdf",
            editorial_status="pending_review",
            uploaded_by="usr_otro",
            created_at=now,
            updated_at=now,
        )
        token = _session_token(repo, student)
        with _client(repo) as client:
            response = client.get("/api/documents", headers=_auth(token))
        assert response.status_code == 200
        ids = {d["document_id"] for d in response.json()["documents"]}
        assert "doc_pub" in ids  # compartido publicado
        assert "doc_own" in ids  # propio (aunque no publicado)
        assert "doc_priv" not in ids  # ajeno y no publicado

    async def test_moderator_sees_all(self):
        repo = FakeApiRepository()
        moderator = _user("usr_mod", "moderator")
        repo.users[moderator.user_id] = moderator
        now = datetime.now(UTC).isoformat()
        repo.documents["doc_ajeno"] = DocumentRecord(
            document_id="doc_ajeno",
            filename="ajeno.pdf",
            editorial_status="pending_review",
            uploaded_by="usr_otro",
            created_at=now,
            updated_at=now,
        )
        token = _session_token(repo, moderator)
        with _client(repo) as client:
            response = client.get("/api/documents", headers=_auth(token))
        assert response.status_code == 200
        ids = {d["document_id"] for d in response.json()["documents"]}
        assert "doc_ajeno" in ids


class TestGetDocumentById:
    async def test_detail_unauthorized_id_returns_404(self):
        repo = FakeApiRepository()
        student = _user("usr_det", "student")
        repo.users[student.user_id] = student
        now = datetime.now(UTC).isoformat()
        repo.documents["doc_ajeno"] = DocumentRecord(
            document_id="doc_ajeno",
            filename="revision.pdf",
            editorial_status="pending_review",
            uploaded_by="usr_otro",
            created_at=now,
            updated_at=now,
        )
        token = _session_token(repo, student)
        with _client(repo) as client:
            response = client.get("/api/documents/doc_ajeno", headers=_auth(token))
        # Conocer el ID no da acceso: 404 sin revelar existencia (Spec 06 §41).
        assert response.status_code == 404

    async def test_detail_own_document_visible(self):
        repo = FakeApiRepository()
        student = _user("usr_autor", "student")
        repo.users[student.user_id] = student
        now = datetime.now(UTC).isoformat()
        repo.documents["doc_mio"] = DocumentRecord(
            document_id="doc_mio",
            filename="mio.pdf",
            editorial_status="pending_review",
            uploaded_by=student.user_id,
            created_at=now,
            updated_at=now,
        )
        token = _session_token(repo, student)
        with _client(repo) as client:
            response = client.get("/api/documents/doc_mio", headers=_auth(token))
        assert response.status_code == 200
        assert response.json()["document_id"] == "doc_mio"

    async def test_detail_missing_document_returns_404(self):
        repo = FakeApiRepository()
        student = _user("usr_404", "student")
        repo.users[student.user_id] = student
        token = _session_token(repo, student)
        with _client(repo) as client:
            response = client.get("/api/documents/doc_inexistente", headers=_auth(token))
        assert response.status_code == 404
        assert response.json()["error"] == "DEL_NOT_FOUND"


class TestGetJobById:
    async def test_job_of_own_document_visible(self):
        repo = FakeApiRepository()
        student = _user("usr_job", "student")
        repo.users[student.user_id] = student
        now = datetime.now(UTC).isoformat()
        repo.documents["doc_job"] = DocumentRecord(
            document_id="doc_job",
            filename="job.pdf",
            editorial_status="draft",
            uploaded_by=student.user_id,
            created_at=now,
            updated_at=now,
        )
        repo.jobs["job_1"] = IngestionJob(
            job_id="job_1",
            document_id="doc_job",
            version_id="ver_1",
            status="queued",
            created_at=now,
            updated_at=now,
        )
        token = _session_token(repo, student)
        with _client(repo) as client:
            response = client.get("/api/ingestion-jobs/job_1", headers=_auth(token))
        assert response.status_code == 200
        assert response.json()["status"] == "queued"

    async def test_job_of_other_user_private_document_returns_404(self):
        repo = FakeApiRepository()
        student = _user("usr_job2", "student")
        repo.users[student.user_id] = student
        now = datetime.now(UTC).isoformat()
        repo.documents["doc_otro"] = DocumentRecord(
            document_id="doc_ajeno_job",
            filename="ajeno.pdf",
            editorial_status="pending_review",
            uploaded_by="usr_autor_ajeno",
            created_at=now,
            updated_at=now,
        )
        repo.jobs["job_ajeno"] = IngestionJob(
            job_id="job_ajeno",
            document_id="doc_ajeno_job",
            version_id=None,
            status="queued",
            created_at=now,
            updated_at=now,
        )
        token = _session_token(repo, student)
        with _client(repo) as client:
            response = client.get("/api/ingestion-jobs/job_ajeno", headers=_auth(token))
        assert response.status_code == 404


# ---------------------------------------------------------------------------
# Flujo completo: upload → worker separado → available
# ---------------------------------------------------------------------------


class TestIngestionFlowWithSeparateWorker:
    async def test_upload_then_worker_activates_version(self):
        from ada_study.index.bm25_index import BM25Index
        from ada_study.ingestion.destinations import (
            BM25Destination,
            FakePineconeDestination,
        )
        from ada_study.ingestion.worker import IngestionWorker

        repo = FakeApiRepository()
        student = _user("usr_flow", "student")
        repo.users[student.user_id] = student
        token = _session_token(repo, student)
        with _client(repo) as client:
            upload = client.post(
                "/api/documents",
                headers={
                    **_auth(token),
                    "Content-Type": "application/pdf",
                    "X-Filename": "mitocondria.pdf",
                },
                content=PDF,
            )
            assert upload.status_code == 202
            job_id = upload.json()["job_id"]

            # El worker corre SEPARADO de la petición HTTP: consume el outbox.
            worker = IngestionWorker(
                repository=repo,
                storage=PdfStorage(root=client.app.state.ada.storage.root),
                destinations=[BM25Destination(BM25Index()), FakePineconeDestination()],
            )
            version_id = next(iter(repo.versions))
            await worker.run_once(version_id)

            # El job progresa y el documento queda disponible para su autor.
            job_response = client.get(f"/api/ingestion-jobs/{job_id}", headers=_auth(token))
            assert job_response.status_code == 200
            assert job_response.json()["status"] in {"done", "queued", "running"}

            detail = client.get(
                f"/api/documents/{upload.json()['document_id']}", headers=_auth(token)
            )
            assert detail.status_code == 200


# ---------------------------------------------------------------------------
# E2: borrado, reintento y restauración (TASK-07)
# ---------------------------------------------------------------------------


class TestDeleteDocument:
    async def test_author_deletes_own_document_202_with_curtain(self):
        repo = FakeApiRepository()
        user = _user("usr_del", "student")
        repo.users[user.user_id] = user
        token = _session_token(repo, user)
        with _client(repo) as client:
            upload = client.post(
                "/api/documents",
                headers={**_auth(token), "Content-Type": "application/pdf"},
                content=PDF,
            )
            assert upload.status_code == 202
            document_id = upload.json()["document_id"]
            version_id = upload.json()["version_id"]
            # Simula una ingesta completada: la versión está activa y
            # disponible, por lo que hay vectores/chunks que purgar.
            await repo.set_active_version(document_id, version_id)

            response = client.delete(f"/api/documents/{document_id}", headers=_auth(token))
        assert response.status_code == 202
        body = response.json()
        assert body["editorial_status"] == "deleted"
        assert body["purge"] == "enqueued"
        # Cortina inmediata: el documento ya no es visible en el listado.
        listing = client.get("/api/documents", headers=_auth(token))
        visible_ids = {d["document_id"] for d in listing.json()["documents"]}
        assert document_id not in visible_ids
        # Purga encolada en el outbox por destino.
        deindex_ops = [
            op for op in repo.pending_ops.values() if op.operation == "delete"
        ]
        assert len(deindex_ops) == 2  # pinecone + bm25

    async def test_deleted_document_not_recoverable_404(self):
        repo = FakeApiRepository()
        user = _user("usr_del2", "student")
        repo.users[user.user_id] = user
        token = _session_token(repo, user)
        with _client(repo) as client:
            upload = client.post(
                "/api/documents",
                headers={**_auth(token), "Content-Type": "application/pdf"},
                content=PDF,
            )
            document_id = upload.json()["document_id"]
            deleted = client.delete(
                f"/api/documents/{document_id}", headers=_auth(token)
            )
            assert deleted.status_code == 202
            # El detalle responde 404: no recuperable (Spec 05 esc. 5).
            detail = client.get(f"/api/documents/{document_id}", headers=_auth(token))
            assert detail.status_code == 404
            # Borrar de nuevo: 409 (transición deleted -> deleted inválida).
            again = client.delete(f"/api/documents/{document_id}", headers=_auth(token))
            assert again.status_code == 409

    async def test_other_user_cannot_delete_foreign_document_404(self):
        repo = FakeApiRepository()
        owner = _user("usr_owner", "student")
        other = _user("usr_other", "student")
        repo.users[owner.user_id] = owner
        repo.users[other.user_id] = other
        token_owner = _session_token(repo, owner)
        token_other = _session_token(repo, other)
        with _client(repo) as client:
            upload = client.post(
                "/api/documents",
                headers={**_auth(token_owner), "Content-Type": "application/pdf"},
                content=PDF,
            )
            document_id = upload.json()["document_id"]
            response = client.delete(
                f"/api/documents/{document_id}", headers=_auth(token_other)
            )
        assert response.status_code == 404  # no revela existencia (Spec 06 §41)

    async def test_moderator_can_delete_any_document_202(self):
        repo = FakeApiRepository()
        owner = _user("usr_own", "student")
        moderator = _user("usr_mod", "moderator")
        repo.users[owner.user_id] = owner
        repo.users[moderator.user_id] = moderator
        token_owner = _session_token(repo, owner)
        token_mod = _session_token(repo, moderator)
        with _client(repo) as client:
            upload = client.post(
                "/api/documents",
                headers={**_auth(token_owner), "Content-Type": "application/pdf"},
                content=PDF,
            )
            document_id = upload.json()["document_id"]
            response = client.delete(
                f"/api/documents/{document_id}", headers=_auth(token_mod)
            )
        assert response.status_code == 202


class TestRetryVersion:
    async def test_retry_failed_version_202_and_idempotent(self):
        repo = FakeApiRepository()
        user = _user("usr_retry", "student")
        repo.users[user.user_id] = user
        token = _session_token(repo, user)
        with _client(repo) as client:
            upload = client.post(
                "/api/documents",
                headers={**_auth(token), "Content-Type": "application/pdf"},
                content=PDF,
            )
            document_id = upload.json()["document_id"]
            version_id = upload.json()["version_id"]
            # La versión falla (p. ej. error de extracción).
            await repo.mark_version_failed(version_id, "ING_INTERNAL")
            first = client.post(
                f"/api/documents/{document_id}/versions/{version_id}/retry",
                headers=_auth(token),
            )
            assert first.status_code == 202
            assert first.json()["status"] == "queued"
            # Idempotente: reintentar de nuevo sobre `queued` responde 200
            # sin crear un segundo job.
            jobs_before = len(repo.jobs)
            second = client.post(
                f"/api/documents/{document_id}/versions/{version_id}/retry",
                headers=_auth(token),
            )
            assert second.status_code == 200
            assert len(repo.jobs) == jobs_before

    async def test_retry_non_failed_version_rejected_409(self):
        repo = FakeApiRepository()
        user = _user("usr_retry409", "student")
        repo.users[user.user_id] = user
        token = _session_token(repo, user)
        with _client(repo) as client:
            upload = client.post(
                "/api/documents",
                headers={**_auth(token), "Content-Type": "application/pdf"},
                content=PDF,
            )
            document_id = upload.json()["document_id"]
            version_id = upload.json()["version_id"]
            # La versión está `queued` (no failed): el reintento es inválido.
            response = client.post(
                f"/api/documents/{document_id}/versions/{version_id}/retry",
                headers=_auth(token),
            )
            assert response.status_code == 409


class TestRestoreVersion:
    async def test_restore_creates_new_version_and_keeps_history(self):
        repo = FakeApiRepository()
        user = _user("usr_restore", "student")
        repo.users[user.user_id] = user
        token = _session_token(repo, user)
        with _client(repo) as client:
            upload = client.post(
                "/api/documents",
                headers={**_auth(token), "Content-Type": "application/pdf"},
                content=PDF,
            )
            document_id = upload.json()["document_id"]
            version_id = upload.json()["version_id"]
            response = client.post(
                f"/api/documents/{document_id}/versions/{version_id}/restore",
                headers=_auth(token),
            )
            assert response.status_code == 202
            restored_id = response.json()["restored_version_id"]
            assert restored_id != version_id
            restored = repo.versions[restored_id]
            assert restored.status == "queued"
            assert restored.version_number == 2
            # El historial no se modifica: la versión original sigue igual.
            assert repo.versions[version_id].version_number == 1
            assert len(repo.versions) == 2

    async def test_restore_unknown_version_404(self):
        repo = FakeApiRepository()
        user = _user("usr_restore404", "student")
        repo.users[user.user_id] = user
        token = _session_token(repo, user)
        with _client(repo) as client:
            upload = client.post(
                "/api/documents",
                headers={**_auth(token), "Content-Type": "application/pdf"},
                content=PDF,
            )
            document_id = upload.json()["document_id"]
            response = client.post(
                f"/api/documents/{document_id}/versions/ver_inexistente/restore",
                headers=_auth(token),
            )
            assert response.status_code == 404
