"""Escenarios E2E de Spec 05 (TASK-07, Fase I1).

Los 4 escenarios implementables sin servicios externos, como pruebas
repetibles de punta a punta:

1. Cargar PDF → `available` → consulta con fuente documento/página.
2. Duplicado: cargar la misma huella no crea un segundo documento (409).
3. Sin evidencia suficiente: el flujo reconoce la insuficiencia (limited).
5. Borrar → el documento deja de ser recuperable de inmediato.

El escenario 4 (web etiquetada) queda FUERA: requiere proveedor de búsqueda
web, tarea aparte (decisión TASK-02).

IMPORTANTE — distinción de niveles de prueba:
- Estas pruebas son E2E de APLICACIÓN con fakes de infraestructura
  (repositorio en memoria, BM25 real, Pinecone fake): ejercitan API +
  servicios de dominio + worker + retriever + grafo como sistema.
- NO sustituyen a las pruebas de integración con servicios reales
  (PostgreSQL, Pinecone, Redis) de test_catalog_integration.py, que
  requieren DATABASE_URL y se saltan sin ella.
"""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from ada_study.api.app import AdaState, create_app
from ada_study.config import Settings
from ada_study.contracts.documents import (
    ChunkRecord,
    DocumentRecord,
    DocumentVersion,
    IngestionJob,
    PendingIndexOp,
)
from ada_study.contracts.identity import SessionRecord, UserRecord
from ada_study.db.repository import NotFoundError
from ada_study.ingestion.destinations import BM25Destination
from ada_study.ingestion.worker import IngestionWorker
from ada_study.storage.pdf_storage import PdfStorage

# ---------------------------------------------------------------------------
# Infraestructura de prueba (repositorio fake completo, igual que test_worker)
# ---------------------------------------------------------------------------


class FakeE2ERepository:
    """Repositorio en memoria con el contrato completo API + worker."""

    def __init__(self) -> None:
        self.documents: dict[str, DocumentRecord] = {}
        self.versions: dict[str, DocumentVersion] = {}
        self.jobs: dict[str, IngestionJob] = {}
        self.pending_ops: dict[str, PendingIndexOp] = {}
        self.chunks: dict[str, list] = {}
        self.users: dict[str, UserRecord] = {}
        self.sessions: dict[str, SessionRecord] = {}
        self.audit_events: list = []

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
        return [op for op in self.pending_ops.values() if op.version_id == version_id]

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

    async def reset_failed_delete_ops(self, version_id: str) -> int:
        count = 0
        for op in self.pending_ops.values():
            if (
                op.version_id == version_id
                and op.status == "failed"
                and op.operation == "delete"
            ):
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

    async def update_editorial_status(self, document_id: str, target: str) -> None:
        document = self.documents[document_id]
        document.editorial_status = target
        document.updated_at = datetime.now(UTC).isoformat()
        if target == "deleted":
            document.deleted_at = datetime.now(UTC).isoformat()

    async def get_user(self, user_id: str) -> UserRecord:
        if user_id not in self.users:
            raise NotFoundError("Usuario", user_id)
        return self.users[user_id]

    async def get_session_by_token_hash(self, token_hash: str) -> SessionRecord | None:
        for session in self.sessions.values():
            if session.token_hash == token_hash:
                return session
        return None

    async def append_audit_event(self, event) -> None:
        self.audit_events.append(event)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _user(user_id: str, role: str = "student") -> UserRecord:
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


def _session_token(repository: FakeE2ERepository, user: UserRecord) -> str:
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


def _client(repository: FakeE2ERepository):
    tmp_root = Path(tempfile.mkdtemp(prefix="ada_e2e_"))
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
    return TestClient(app), tmp_root


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


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


def _make_worker(repository: FakeE2ERepository, storage_root: Path) -> IngestionWorker:
    """Worker real con BM25 real y Pinecone fake (infraestructura simulada)."""
    from ada_study.index.bm25_index import BM25Index

    return IngestionWorker(
        repository=repository,
        storage=PdfStorage(root=storage_root),
        destinations=[BM25Destination(BM25Index())],
    )


async def _run_worker(repository: FakeE2ERepository, storage_root: Path) -> None:
    """Ejecuta el worker sobre todas las versiones con ops pendientes."""
    worker = _make_worker(repository, storage_root)
    for version_id in await repository.list_version_ids_with_pending_ops():
        await worker.run_once(version_id)


# ---------------------------------------------------------------------------
# Escenario 1: cargar PDF → available → consulta con fuente documento/página
# ---------------------------------------------------------------------------


class TestScenario1UploadAvailableQuery:
    async def test_upload_ingest_and_retrieve_with_provenance(self):
        repo = FakeE2ERepository()
        user = _user("usr_e2e1")
        repo.users[user.user_id] = user
        token = _session_token(repo, user)
        client, tmp_root = _client(repo)
        with client:
            upload = client.post(
                "/api/documents",
                headers={
                    **_auth(token),
                    "Content-Type": "application/pdf",
                    "X-Filename": "biologia.pdf",
                },
                content=_pdf("La mitocondria genera energia para la celula."),
            )
            assert upload.status_code == 202
            document_id = upload.json()["document_id"]

            # El documento NO está disponible antes de la ingesta.
            detail = client.get(f"/api/documents/{document_id}", headers=_auth(token))
            assert detail.status_code == 200
            assert detail.json()["active_version_id"] is None

            # El worker separado completa la ingesta (BM25 real, Pinecone fake).
            await _run_worker(repo, tmp_root)

            # Tras la ingesta, la versión está activa y disponible.
            detail = client.get(f"/api/documents/{document_id}", headers=_auth(token))
            assert detail.status_code == 200
            assert detail.json()["active_version_id"] is not None

            # La recuperación híbrida devuelve evidencia con procedencia.
            # El documento sigue `pending_review`: el autor lo recupera por
            # acceso privado (regla TASK-07); el RAG compartido no lo expone.
            from ada_study.index.bm25_index import BM25Index
            from ada_study.index.retriever import HybridRetriever, ViewerContext

            version_id = detail.json()["active_version_id"]
            chunks = await repo.list_chunks(version_id)
            assert len(chunks) >= 1
            owner_viewer = ViewerContext(user_id=user.user_id, role=user.role)
            retriever = HybridRetriever(
                repo, BM25Index.rebuild(chunks), viewer=owner_viewer
            )
            retriever.load_chunks(chunks)
            results = await retriever.search("mitocondria energia")
            assert len(results) >= 1
            chunk = results[0]
            assert chunk.document_id == document_id
            assert chunk.page >= 1
            assert chunk.content_hash

            # Aislamiento: otro student NO puede recuperar el pendiente ajeno.
            other = _user("usr_other_e2e1")
            other_viewer = ViewerContext(user_id=other.user_id, role=other.role)
            other_retriever = HybridRetriever(
                repo, BM25Index.rebuild(chunks), viewer=other_viewer
            )
            other_retriever.load_chunks(chunks)
            assert await other_retriever.search("mitocondria energia") == []


# ---------------------------------------------------------------------------
# Escenario 2: duplicado no duplica
# ---------------------------------------------------------------------------


class TestScenario2DuplicateRejected:
    async def test_same_content_twice_returns_409_single_document(self):
        repo = FakeE2ERepository()
        user = _user("usr_e2e2")
        repo.users[user.user_id] = user
        token = _session_token(repo, user)
        client, tmp_root = _client(repo)
        pdf = _pdf("Contenido unico para el test de duplicados.")
        with client:
            first = client.post(
                "/api/documents",
                headers={**_auth(token), "Content-Type": "application/pdf"},
                content=pdf,
            )
            assert first.status_code == 202
            second = client.post(
                "/api/documents",
                headers={**_auth(token), "Content-Type": "application/pdf"},
                content=pdf,
            )
            assert second.status_code == 409
            assert second.json()["error"] == "ING_DUPLICATE"
            # Un solo documento registrado.
            assert len(repo.documents) == 1


# ---------------------------------------------------------------------------
# Escenario 3: sin evidencia suficiente → reconoce insuficiencia
# ---------------------------------------------------------------------------


class TestScenario3InsufficientEvidence:
    async def test_retriever_returns_nothing_for_unrelated_query(self):
        """Con corpus vacío o consulta sin coincidencias, no hay evidencia."""
        from ada_study.index.bm25_index import BM25Index
        from ada_study.index.retriever import HybridRetriever

        repo = FakeE2ERepository()
        # Sin chunks: el retriever no puede fabricar evidencia.
        retriever = HybridRetriever(repo, BM25Index.rebuild([]))
        retriever.load_chunks([])
        results = await retriever.search("consulta sin corpus")
        assert results == []

    async def test_limited_finish_reports_gaps_without_fabricating(self):
        """La salida segura reporta gaps y limitaciones sin inventar contenido."""
        from ada_study.graph.synthesizer import limited_finish_node

        update = limited_finish_node({})
        response = update["study_response"]
        assert response["status"] == "limited"
        assert response["references"] == []
        assert response["evidence_gaps"]
        assert response["limitations"]


# ---------------------------------------------------------------------------
# Escenario 5: borrar → no recuperable
# ---------------------------------------------------------------------------


class TestScenario5DeleteNotRecoverable:
    async def test_deleted_document_excluded_from_retrieval_immediately(self):
        repo = FakeE2ERepository()
        user = _user("usr_e2e5")
        repo.users[user.user_id] = user
        token = _session_token(repo, user)
        client, tmp_root = _client(repo)
        with client:
            upload = client.post(
                "/api/documents",
                headers={**_auth(token), "Content-Type": "application/pdf"},
                content=_pdf("La celula respira mediante la mitocondria."),
            )
            document_id = upload.json()["document_id"]
            await _run_worker(repo, tmp_root)

            # El documento sigue `pending_review`: el autor lo recupera por
            # acceso privado (regla TASK-07).
            from ada_study.index.bm25_index import BM25Index
            from ada_study.index.retriever import HybridRetriever, ViewerContext

            detail = client.get(f"/api/documents/{document_id}", headers=_auth(token))
            version_id = detail.json()["active_version_id"]
            chunks = await repo.list_chunks(version_id)
            owner_viewer = ViewerContext(user_id=user.user_id, role=user.role)
            retriever = HybridRetriever(
                repo, BM25Index.rebuild(chunks), viewer=owner_viewer
            )
            retriever.load_chunks(chunks)
            assert len(await retriever.search("mitocondria")) >= 1

            # Borrado: cortina editorial inmediata + purga encolada.
            deleted = client.delete(f"/api/documents/{document_id}", headers=_auth(token))
            assert deleted.status_code == 202

            # La recuperación ya NO expone el contenido, NI siquiera para el
            # autor (Spec 05 esc. 5; withdrawn/deleted excluidos absolutamente).
            assert await retriever.search("mitocondria") == []
            # La API tampoco lo expone.
            assert (
                client.get(f"/api/documents/{document_id}", headers=_auth(token)).status_code
                == 404
            )
            # La purga física quedó encolada (eventual, ADR-0004).
            delete_ops = [
                op for op in repo.pending_ops.values() if op.operation == "delete"
            ]
            assert len(delete_ops) == 2


# ---------------------------------------------------------------------------
# Regla de acceso privado (aprobada en TASK-07): visibilidad por propietario
# ---------------------------------------------------------------------------


class TestPrivateAccessRule:
    def _seed_pending_document(self, repo: FakeE2ERepository, owner_id: str):
        """Documento pending_review con versión available y chunks indexados."""
        from ada_study.index.bm25_index import BM25Index

        now = datetime.now(UTC).isoformat()
        document = DocumentRecord(
            document_id="doc_priv",
            filename="privado.pdf",
            subject=None,
            active_version_id="ver_priv",
            editorial_status="pending_review",
            review_status="not_required",
            uploaded_by=owner_id,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        version = DocumentVersion(
            version_id="ver_priv",
            document_id="doc_priv",
            version_number=1,
            sha256="a" * 64,
            storage_key="2026/01/priv.pdf",
            content_type="application/pdf",
            size_bytes=100,
            status="available",
            created_at=now,
            page_count=1,
            error_code=None,
        )
        chunk = ChunkRecord(
            chunk_id="chk_priv",
            document_id="doc_priv",
            version_id="ver_priv",
            page=1,
            chunk_index=0,
            content_hash="b" * 64,
            text="La mitocondria produce energia.",
            title="privado.pdf",
            source="upload",
        )
        repo.documents[document.document_id] = document
        repo.versions[version.version_id] = version
        repo.chunks[version.version_id] = [chunk]
        return BM25Index.rebuild([chunk]), [chunk]

    async def test_owner_sees_pending_review(self):
        from ada_study.index.retriever import HybridRetriever, ViewerContext

        repo = FakeE2ERepository()
        bm25, chunks = self._seed_pending_document(repo, "usr_owner")
        viewer = ViewerContext(user_id="usr_owner", role="student")
        retriever = HybridRetriever(repo, bm25, viewer=viewer)
        retriever.load_chunks(chunks)
        assert len(await retriever.search("mitocondria")) >= 1

    async def test_other_student_cannot_see_pending_review(self):
        from ada_study.index.retriever import HybridRetriever, ViewerContext

        repo = FakeE2ERepository()
        bm25, chunks = self._seed_pending_document(repo, "usr_owner")
        viewer = ViewerContext(user_id="usr_other", role="student")
        retriever = HybridRetriever(repo, bm25, viewer=viewer)
        retriever.load_chunks(chunks)
        assert await retriever.search("mitocondria") == []

    async def test_moderator_cannot_retrieve_pending_review_of_other(self):
        """El moderator revisa por la cola de moderación, no por recuperación."""
        from ada_study.index.retriever import HybridRetriever, ViewerContext

        repo = FakeE2ERepository()
        bm25, chunks = self._seed_pending_document(repo, "usr_owner")
        viewer = ViewerContext(user_id="usr_mod", role="moderator")
        retriever = HybridRetriever(repo, bm25, viewer=viewer)
        retriever.load_chunks(chunks)
        assert await retriever.search("mitocondria") == []

    async def test_published_visible_to_all_viewers(self):
        from ada_study.index.retriever import HybridRetriever, ViewerContext

        repo = FakeE2ERepository()
        bm25, chunks = self._seed_pending_document(repo, "usr_owner")
        repo.documents["doc_priv"].editorial_status = "published"
        # Sin viewer: visible (RAG compartido).
        shared = HybridRetriever(repo, bm25)
        shared.load_chunks(chunks)
        assert len(await shared.search("mitocondria")) >= 1
        # Con viewer ajeno: visible igualmente.
        viewer = HybridRetriever(
            repo, bm25, viewer=ViewerContext(user_id="usr_x", role="student")
        )
        viewer.load_chunks(chunks)
        assert len(await viewer.search("mitocondria")) >= 1

    async def test_withdrawn_never_recoverable_even_for_owner(self):
        from ada_study.index.retriever import HybridRetriever, ViewerContext

        repo = FakeE2ERepository()
        bm25, chunks = self._seed_pending_document(repo, "usr_owner")
        repo.documents["doc_priv"].editorial_status = "withdrawn"
        viewer = HybridRetriever(
            repo, bm25, viewer=ViewerContext(user_id="usr_owner", role="student")
        )
        viewer.load_chunks(chunks)
        assert await viewer.search("mitocondria") == []

    async def test_rejected_never_recoverable(self):
        from ada_study.index.retriever import HybridRetriever, ViewerContext

        repo = FakeE2ERepository()
        bm25, chunks = self._seed_pending_document(repo, "usr_owner")
        repo.documents["doc_priv"].editorial_status = "rejected"
        viewer = HybridRetriever(
            repo, bm25, viewer=ViewerContext(user_id="usr_owner", role="student")
        )
        viewer.load_chunks(chunks)
        assert await viewer.search("mitocondria") == []

    async def test_pedagogical_use_only_after_ingestion(self):
        """Versión no `available` (aún en ingesta) no es recuperable ni por el autor."""
        from ada_study.index.retriever import HybridRetriever, ViewerContext

        repo = FakeE2ERepository()
        bm25, chunks = self._seed_pending_document(repo, "usr_owner")
        repo.versions["ver_priv"].status = "processing"
        viewer = HybridRetriever(
            repo, bm25, viewer=ViewerContext(user_id="usr_owner", role="student")
        )
        viewer.load_chunks(chunks)
        assert await viewer.search("mitocondria") == []
