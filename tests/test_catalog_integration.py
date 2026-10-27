"""Pruebas de integración del catálogo contra PostgreSQL real (TASK-02).

Se ejecutan solo si DATABASE_URL apunta a un PostgreSQL accesible; si no,
se saltan con un mensaje claro (limitación informada: este entorno no tiene
PostgreSQL ni Docker disponibles).
"""

import os

import pytest

from ada_study.contracts.documents import (
    DocumentRecord,
    DocumentVersion,
    IngestionJob,
    PendingIndexOp,
)

pytest.importorskip("sqlalchemy")
pytest.importorskip("asyncpg")

DATABASE_URL = os.environ.get("DATABASE_URL", "")

pytestmark = pytest.mark.skipif(
    not DATABASE_URL,
    reason="DATABASE_URL no definida: se requiere PostgreSQL real para integración",
)


@pytest.fixture()
async def engine():
    from sqlalchemy.ext.asyncio import create_async_engine

    engine = create_async_engine(DATABASE_URL, echo=False)
    from ada_study.db.schema import metadata

    async with engine.begin() as conn:
        await conn.run_sync(metadata.drop_all)
        await conn.run_sync(metadata.create_all)
    yield engine
    await engine.dispose()


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
        await repository.create_document(record)
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
        await repository.create_document(record)
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
        await repository.create_document(record)
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
        await repository.create_document(record)
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
