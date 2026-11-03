"""Pruebas de integración del catálogo contra PostgreSQL real (TASK-02).

Se ejecutan solo si DATABASE_URL apunta a un PostgreSQL accesible; si no,
se saltan con un mensaje claro. Igual que tests/integration: se crea una
base EFÍMERA por sesión (`ada_study_test_<hex>`) y NUNCA se toca la base
configurada en DATABASE_URL (sin drop_all/create_all sobre ella).
"""

import os
import secrets
from datetime import UTC, datetime

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import create_async_engine

from ada_study.contracts.documents import (
    DocumentRecord,
    DocumentVersion,
    IngestionJob,
    PendingIndexOp,
)
from ada_study.db.repository import DuplicateDocumentError, NotFoundError

pytest.importorskip("sqlalchemy")
pytest.importorskip("asyncpg")

def _database_url_from_project_config() -> str:
    """Resuelve la URL desde la configuración del repo (.env), no ambiental.

    Hermético (preflight CHECKPOINTER-03): ignora cualquier `DATABASE_URL`
    ambiental de la sesión que apunte a credenciales ajenas al proyecto.
    """
    from ada_study.config import Settings

    saved = os.environ.pop("DATABASE_URL", None)
    try:
        return Settings().database_url.strip()
    finally:
        if saved is not None:
            os.environ["DATABASE_URL"] = saved


DATABASE_URL = _database_url_from_project_config()

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL no definida: se requiere PostgreSQL real para integración",
)


@pytest.fixture(scope="session")
async def test_database_url() -> str:
    """Base EFÍMERA por sesión; nunca se toca la base configurada."""
    from sqlalchemy.engine import make_url

    url = make_url(DATABASE_URL)
    dbname = f"ada_study_test_{secrets.token_hex(4)}"
    admin_url = url.set(database="postgres")
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    finally:
        await admin.dispose()
    # render_as_string(hide_password=False): str(URL) enmascara la contraseña
    # con `***` y asyncpg fallaría con "password authentication failed".
    yield url.set(database=dbname).render_as_string(hide_password=False)
    drop = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with drop.connect() as conn:
            await conn.execute(text(f'DROP DATABASE "{dbname}" WITH (FORCE)'))
    finally:
        await drop.dispose()


@pytest.fixture()
async def engine(test_database_url):
    """Engine con esquema rev. 3 limpio para un test."""
    from ada_study.db.schema import metadata

    engine = create_async_engine(test_database_url, echo=False)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.drop_all)
        await conn.run_sync(metadata.create_all)
    yield engine
    await engine.dispose()


async def _claim_of(engine, document_id: str) -> str | None:
    """Claim vigente (`documents.sha256`) leído por SQL directo."""
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT sha256 FROM documents WHERE document_id = :d"),
                {"d": document_id},
            )
        ).scalar()


@pytest.fixture()
async def repository(engine):
    from ada_study.db.repository import CatalogRepository

    return CatalogRepository(engine=engine)


PDF_A = b"%PDF-1.4 integracion A"
PDF_B = b"%PDF-1.4 integracion B"


class TestDocumentPersistence:
    async def test_create_and_get_document(self, repository):
        from datetime import UTC, datetime

        record = DocumentRecord(
            document_id="doc_it_1",
            filename="apuntes.pdf",
            subject="historia",
            active_version_id=None,
            editorial_status="draft",
            review_status="not_required",
            uploaded_by=None,
            created_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
            deleted_at=None,
        )
        await repository.create_document(record, sha256="0" * 64)
        fetched = await repository.get_document("doc_it_1")
        assert fetched.filename == "apuntes.pdf"
        assert fetched.editorial_status == "draft"

    async def test_get_missing_document_raises_not_found(self, repository):
        from ada_study.db.repository import NotFoundError

        with pytest.raises(NotFoundError):
            await repository.get_document("doc_inexistente")

    async def test_editorial_transition_validated(self, repository):
        from datetime import UTC, datetime

        from ada_study.db.repository import InvalidTransitionError

        record = DocumentRecord(
            document_id="doc_it_2",
            filename="a.pdf",
            subject=None,
            active_version_id=None,
            editorial_status="draft",
            review_status="not_required",
            uploaded_by=None,
            created_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
            deleted_at=None,
        )
        await repository.create_document(record, sha256="1" * 64)
        # draft -> published es inválida (ADR-0005)
        with pytest.raises(InvalidTransitionError):
            await repository.update_editorial_status("doc_it_2", "published")
        # draft -> pending_review es válida
        await repository.update_editorial_status("doc_it_2", "pending_review")
        fetched = await repository.get_document("doc_it_2")
        assert fetched.editorial_status == "pending_review"

    async def test_version_history_and_dedup_lookup(self, repository):
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        sha_a = "a" * 64
        sha_b = "b" * 64
        record = DocumentRecord(
            document_id="doc_it_3",
            filename="a.pdf",
            subject=None,
            active_version_id=None,
            editorial_status="draft",
            review_status="not_required",
            uploaded_by=None,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        await repository.create_document(record, sha256=sha_a)
        v1 = DocumentVersion(
            version_id="ver_it_1",
            document_id="doc_it_3",
            version_number=1,
            sha256=sha_a,
            storage_key="aa/" + sha_a + ".pdf",
            content_type="application/pdf",
            size_bytes=100,
            status="available",
            created_at=now,
        )
        v2 = DocumentVersion(
            version_id="ver_it_2",
            document_id="doc_it_3",
            version_number=2,
            sha256=sha_b,
            storage_key="bb/" + sha_b + ".pdf",
            content_type="application/pdf",
            size_bytes=200,
            status="queued",
            created_at=now,
        )
        await repository.create_version(v1)
        await repository.create_version(v2)
        history = await repository.list_versions("doc_it_3")
        assert [v.version_number for v in history] == [1, 2]
        found = await repository.find_active_version_by_sha256(sha_a)
        assert found is not None
        assert found.version_id == "ver_it_1"
        assert await repository.find_active_version_by_sha256("c" * 64) is None

    async def test_job_lifecycle(self, repository):
        from datetime import UTC, datetime

        now = datetime.now(UTC).isoformat()
        record = DocumentRecord(
            document_id="doc_it_4",
            filename="a.pdf",
            subject=None,
            active_version_id=None,
            editorial_status="draft",
            review_status="not_required",
            uploaded_by=None,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        await repository.create_document(record, sha256="4" * 64)
        job = IngestionJob(
            job_id="job_it_1",
            document_id="doc_it_4",
            version_id=None,
            status="queued",
            created_at=now,
            updated_at=now,
            error_code=None,
        )
        await repository.create_job(job)
        await repository.update_job_status("job_it_1", "running")
        fetched = await repository.get_job("job_it_1")
        assert fetched.status == "running"
        await repository.update_job_status("job_it_1", "failed", error_code="ING_EXTRACT_FAILED")
        fetched = await repository.get_job("job_it_1")
        assert fetched.status == "failed"
        assert fetched.error_code == "ING_EXTRACT_FAILED"

    async def test_outbox_claim_and_complete(self, repository):

        op = PendingIndexOp(
            op_id="op_it_1",
            version_id="ver_it_x",
            destination="pinecone",
            operation="index",
        )
        await repository.enqueue_pending_op(op)
        claimed = await repository.claim_pending_ops("pinecone")
        assert [o.op_id for o in claimed] == ["op_it_1"]
        await repository.complete_pending_op("op_it_1")
        assert await repository.claim_pending_ops("pinecone") == []

    async def test_outbox_fail_increments_attempts(self, repository):
        op = PendingIndexOp(
            op_id="op_it_2",
            version_id="ver_it_x",
            destination="bm25",
            operation="index",
        )
        await repository.enqueue_pending_op(op)
        await repository.fail_pending_op("op_it_2", "ING_INDEX_FAILED")
        # fail_pending_op marca failed; el reconciliador la reprocesa
        claimed = await repository.claim_pending_ops("bm25")
        assert claimed == []  # status failed no se reclama como pending


class TestAtomicRegistrationContract:
    """Interfaz atómica rev. 3 (ADR-0001) contra PostgreSQL real."""

    SHA_A = "a" * 64
    SHA_B = "b" * 64
    SHA_C = "c" * 64

    def _record(self, document_id: str) -> DocumentRecord:
        now = datetime.now(UTC).isoformat()
        return DocumentRecord(
            document_id=document_id,
            filename="atomico.pdf",
            subject=None,
            active_version_id=None,
            editorial_status="draft",
            review_status="not_required",
            uploaded_by=None,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )

    def _version(
        self, version_id: str, document_id: str, sha256: str, number: int = 1
    ) -> DocumentVersion:
        return DocumentVersion(
            version_id=version_id,
            document_id=document_id,
            version_number=number,
            sha256=sha256,
            storage_key=sha256[:2] + "/" + sha256 + ".pdf",
            content_type="application/pdf",
            size_bytes=100,
            status="queued",
            created_at=datetime.now(UTC).isoformat(),
        )

    def _job(self, job_id: str, document_id: str, version_id: str) -> IngestionJob:
        now = datetime.now(UTC).isoformat()
        return IngestionJob(
            job_id=job_id,
            document_id=document_id,
            version_id=version_id,
            status="queued",
            created_at=now,
            updated_at=now,
            error_code=None,
        )

    def _ops(self, version_id: str, prefix: str) -> list[PendingIndexOp]:
        return [
            PendingIndexOp(
                op_id=f"{prefix}_{destination}",
                version_id=version_id,
                destination=destination,
                operation="index",
            )
            for destination in ("pinecone", "bm25")
        ]

    async def test_atomic_registration_persists_all_pieces(self, repository):
        record = self._record("doc_at_1")
        version = self._version("ver_at_1", "doc_at_1", self.SHA_A)
        ops = self._ops("ver_at_1", "op_at_1")

        await repository.create_document_atomic(
            record,
            sha256=self.SHA_A,
            version=version,
            job=self._job("job_at_1", "doc_at_1", "ver_at_1"),
            ops=ops,
        )

        assert (await repository.get_document("doc_at_1")).document_id == "doc_at_1"
        assert [v.version_id for v in await repository.list_versions("doc_at_1")] == [
            "ver_at_1"
        ]
        assert (await repository.get_job("job_at_1")).status == "queued"
        claimed = await repository.claim_pending_ops("pinecone")
        assert [o.op_id for o in claimed] == ["op_at_1_pinecone"]

    async def test_atomic_duplicate_claim_translates_to_domain_error(self, repository, engine):
        record_a = self._record("doc_at_2")
        version_a = self._version("ver_at_2", "doc_at_2", self.SHA_A)
        await repository.create_document_atomic(
            record_a,
            sha256=self.SHA_A,
            version=version_a,
            job=self._job("job_at_2", "doc_at_2", "ver_at_2"),
            ops=self._ops("ver_at_2", "op2"),
        )

        record_b = self._record("doc_at_3")
        version_b = self._version("ver_at_3", "doc_at_3", self.SHA_B)
        with pytest.raises(DuplicateDocumentError) as exc_info:
            await repository.create_document_atomic(
                record_b,
                sha256=self.SHA_A,  # huella ya reclamada por doc_at_2
                version=version_b,
                job=self._job("job_at_3", "doc_at_3", "ver_at_3"),
                ops=self._ops("ver_at_3", "op3"),
            )
        assert exc_info.value.error_code == "ING_DUPLICATE"
        assert exc_info.value.existing_document_id == "doc_at_2"

        # Rollback completo del perdedor: sin documento, versión, job ni ops.
        with pytest.raises(NotFoundError):
            await repository.get_document("doc_at_3")
        with pytest.raises(NotFoundError):
            await repository.get_version("ver_at_3")
        with pytest.raises(NotFoundError):
            await repository.get_job("job_at_3")
        assert await repository.list_ops_for_version("ver_at_3") == []
        assert await _claim_of(engine, "doc_at_2") == self.SHA_A

    async def test_atomic_non_dedup_integrity_failure_rolls_back(self, repository):
        # Violación de PK en document_versions (NO del índice de dedup):
        # se propaga SIN traducir y TODO se revierte.
        record_seed = self._record("doc_at_4")
        version_seed = self._version("ver_at_seed", "doc_at_4", self.SHA_B)
        await repository.create_document_atomic(
            record_seed,
            sha256=self.SHA_B,
            version=version_seed,
            job=self._job("job_at_4", "doc_at_4", "ver_at_seed"),
            ops=self._ops("ver_at_seed", "op4"),
        )

        record = self._record("doc_at_5")
        version = self._version("ver_at_seed", "doc_at_5", self.SHA_C)  # PK duplicada
        with pytest.raises(IntegrityError):
            await repository.create_document_atomic(
                record,
                sha256=self.SHA_C,
                version=version,
                job=self._job("job_at_5", "doc_at_5", "ver_at_seed"),
                ops=self._ops("ver_at_5", "op5"),
            )
        with pytest.raises(NotFoundError):
            await repository.get_document("doc_at_5")

    async def test_create_version_with_claim_moves_claim_atomically(self, repository, engine):
        record = self._record("doc_at_6")
        await repository.create_document(record, sha256=self.SHA_A)
        version = self._version("ver_at_6", "doc_at_6", self.SHA_B)
        job = self._job("job_at_6", "doc_at_6", "ver_at_6")

        result = await repository.create_version_with_claim(
            version, claim_sha256=self.SHA_B, job=job, ops=self._ops("ver_at_6", "op6")
        )

        assert result.version_id == "ver_at_6"
        assert await _claim_of(engine, "doc_at_6") == self.SHA_B
        found = await repository.find_active_version_by_sha256(self.SHA_B)
        assert found is not None and found.version_id == "ver_at_6"
        # El claim anterior quedó liberado (nadie vivo reclama SHA_A).
        assert await repository.find_active_version_by_sha256(self.SHA_A) is None
        assert (await repository.get_job("job_at_6")).status == "queued"

    async def test_restore_conflict_rolls_back_completely(self, repository, engine):
        record = self._record("doc_at_7")
        await repository.create_document(record, sha256=self.SHA_A)
        # El reclamante rival registró su huella por la vía atómica:
        # claim (documents.sha256) + versión con la misma huella.
        other = self._record("doc_at_8")
        await repository.create_document_atomic(
            other,
            sha256=self.SHA_B,
            version=self._version("ver_at_8", "doc_at_8", self.SHA_B),
            job=self._job("job_at_8", "doc_at_8", "ver_at_8"),
            ops=self._ops("ver_at_8", "op8"),
        )

        restored = self._version("ver_at_7", "doc_at_7", self.SHA_B)
        with pytest.raises(DuplicateDocumentError) as exc_info:
            await repository.create_version_with_claim(
                restored,
                claim_sha256=self.SHA_B,  # reclamada por doc_at_8 (vivo)
                job=self._job("job_at_7", "doc_at_7", "ver_at_7"),
                ops=self._ops("ver_at_7", "op7"),
            )
        assert exc_info.value.existing_document_id == "doc_at_8"

        # Rollback completo y claim del restaurador intacto.
        with pytest.raises(NotFoundError):
            await repository.get_version("ver_at_7")
        with pytest.raises(NotFoundError):
            await repository.get_job("job_at_7")
        assert await repository.list_ops_for_version("ver_at_7") == []
        assert await _claim_of(engine, "doc_at_7") == self.SHA_A
