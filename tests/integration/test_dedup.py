"""Deduplicación y atomicidad contra PostgreSQL REAL (ADR-0001 rev. 3).

Valida: unicidad entre documentos vivos, cargas concurrentes, ciclo de vida
del claim (liberación por borrado, retención en withdrawn/failed),
restauración con y sin conflicto con rollback completo, y que otras
violaciones de integridad NO se traducen a ING_DUPLICATE.
"""

import asyncio
import hashlib

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from ada_study.db.repository import DuplicateDocumentError, NotFoundError

CONTENT_X = b"%PDF-1.4 contenido de prueba X"
CONTENT_Y = b"%PDF-1.4 contenido de prueba Y"
SHA_X = hashlib.sha256(CONTENT_X).hexdigest()
SHA_Y = hashlib.sha256(CONTENT_Y).hexdigest()


async def _claim_of(repo, document_id: str) -> str | None:
    async with repo._engine.connect() as conn:
        row = (
            await conn.execute(
                text("SELECT sha256 FROM documents WHERE document_id = :d"),
                {"d": document_id},
            )
        ).first()
    return row[0] if row is not None else None


class TestDedupBetweenLiveDocuments:
    async def test_second_registration_rejected_with_409_contract(self, catalog):
        await catalog.register_document(filename="a.pdf", content=CONTENT_X, uploaded_by="u1")
        with pytest.raises(DuplicateDocumentError) as excinfo:
            await catalog.register_document(filename="b.pdf", content=CONTENT_X, uploaded_by="u2")
        assert excinfo.value.error_code == "ING_DUPLICATE"

    async def test_different_content_is_allowed(self, catalog):
        await catalog.register_document(filename="a.pdf", content=CONTENT_X, uploaded_by="u1")
        document, _, _ = await catalog.register_document(
            filename="b.pdf", content=CONTENT_Y, uploaded_by="u2"
        )
        assert document.document_id

    async def test_concurrent_uploads_same_sha(self, catalog, repo):
        results = await asyncio.gather(
            catalog.register_document(filename="a.pdf", content=CONTENT_X, uploaded_by="u1"),
            catalog.register_document(filename="b.pdf", content=CONTENT_X, uploaded_by="u2"),
            return_exceptions=True,
        )
        errors = [r for r in results if isinstance(r, BaseException)]
        successes = [r for r in results if not isinstance(r, BaseException)]
        assert len(successes) == 1
        assert len(errors) == 1
        assert isinstance(errors[0], DuplicateDocumentError)
        # Exactamente UN documento vivo reclama la huella.
        async with repo._engine.connect() as conn:
            count = (
                await conn.execute(
                    text(
                        "SELECT count(*) FROM documents "
                        "WHERE sha256 = :s AND deleted_at IS NULL"
                    ),
                    {"s": SHA_X},
                )
            ).scalar()
        assert count == 1

    async def test_other_integrity_errors_not_translated(self, repo):
        """Otras violaciones de integridad (p. ej. PK) NO son ING_DUPLICATE."""
        from datetime import UTC, datetime

        from ada_study.contracts.documents import DocumentRecord

        now = datetime.now(UTC).isoformat()
        first = DocumentRecord(
            document_id="doc_dup",
            filename="uno.pdf",
            created_at=now,
            updated_at=now,
        )
        await repo.create_document(first, sha256=SHA_X)
        clash = DocumentRecord(
            document_id="doc_dup",
            filename="otro.pdf",
            created_at=now,
            updated_at=now,
        )
        # Mismo document_id con OTRA huella: viola la PK, no el índice dedup.
        with pytest.raises(IntegrityError) as excinfo:
            await repo.create_document(clash, sha256=SHA_Y)
        assert not isinstance(excinfo.value, DuplicateDocumentError)


class TestClaimLifecycle:
    async def test_delete_releases_claim(self, catalog, repo):
        document, _, _ = await catalog.register_document(
            filename="a.pdf", content=CONTENT_X, uploaded_by="u1"
        )
        await catalog.delete_document(document.document_id)
        # deleted_at libera el claim: nadie reclama la huella.
        assert await repo.find_active_version_by_sha256(SHA_X) is None
        # La huella liberada puede ser reclamada por un documento nuevo.
        new_document, _, _ = await catalog.register_document(
            filename="a2.pdf", content=CONTENT_X, uploaded_by="u2"
        )
        assert new_document.document_id != document.document_id

    async def test_withdrawn_retains_claim(self, catalog):
        document, _, _ = await catalog.register_document(
            filename="a.pdf", content=CONTENT_X, uploaded_by="u1"
        )
        await catalog._repo.update_editorial_status(document.document_id, "withdrawn")
        with pytest.raises(DuplicateDocumentError):
            await catalog.register_document(filename="b.pdf", content=CONTENT_X, uploaded_by="u2")

    async def test_failed_version_retains_claim(self, catalog, repo):
        _, version, _ = await catalog.register_document(
            filename="a.pdf", content=CONTENT_X, uploaded_by="u1"
        )
        await repo.mark_version_failed(version.version_id, "ING_EXTRACT_FAILED")
        with pytest.raises(DuplicateDocumentError):
            await catalog.register_document(
                filename="b.pdf", content=CONTENT_X, uploaded_by="u2"
            )


class TestRestoreAtomicity:
    async def test_restore_without_conflict(self, catalog, repo):
        document, historical, _ = await catalog.register_document(
            filename="a.pdf", content=CONTENT_X, uploaded_by="u1"
        )
        restored = await catalog.restore_version(document.document_id, historical.version_id)
        assert restored.version_number == 2
        assert restored.status == "queued"
        assert restored.sha256 == SHA_X
        # El claim sigue en el mismo documento (misma huella histórica).
        assert await _claim_of(repo, document.document_id) == SHA_X
        # Job + outbox de la nueva versión, todo en la misma transacción.
        jobs = await repo.list_jobs_for_version(restored.version_id)
        assert len(jobs) == 1
        ops = await repo.list_ops_for_version(restored.version_id)
        assert {op.destination for op in ops} == {"pinecone", "bm25"}

    async def test_restore_with_conflict_rolls_back_completely(self, catalog, repo):
        from datetime import UTC, datetime

        from ada_study.contracts.documents import DocumentVersion, IngestionJob, PendingIndexOp

        document_a, historical, _ = await catalog.register_document(
            filename="a.pdf", content=CONTENT_X, uploaded_by="u1"
        )
        await catalog.register_document(filename="b.pdf", content=CONTENT_Y, uploaded_by="u2")
        # Estado heredado simulado: claims cruzados vía SQL directo (docA
        # reclama Y y docB reclama X), como podría quedar una base legada.
        document_b = await repo.find_active_version_by_sha256(SHA_Y)
        assert document_b is not None
        doc_b_id = document_b.document_id
        async with repo._engine.begin() as conn:
            # Paso intermedio (huella libre) para no chocar con el índice.
            await conn.execute(
                text("UPDATE documents SET sha256 = :t WHERE document_id = :a"),
                {"t": "e" * 64, "a": document_a.document_id},
            )
            await conn.execute(
                text("UPDATE documents SET sha256 = :x WHERE document_id = :b"),
                {"x": SHA_X, "b": doc_b_id},
            )
            await conn.execute(
                text("UPDATE documents SET sha256 = :y WHERE document_id = :a"),
                {"y": SHA_Y, "a": document_a.document_id},
            )
        # Precondición: la huella X la reclama docB (vivo) y docA conserva
        # la versión histórica X.
        assert await _claim_of(repo, document_a.document_id) == SHA_Y
        assert await _claim_of(repo, doc_b_id) == SHA_X

        # 1) Servicio: el pre-check detecta el conflicto y NO escribe nada.
        with pytest.raises(DuplicateDocumentError):
            await catalog.restore_version(document_a.document_id, historical.version_id)

        # 2) Repositorio: la garantía de la BD es la que revierte TODO.
        restored = DocumentVersion(
            version_id="ver_roll",
            document_id=document_a.document_id,
            version_number=2,
            sha256=SHA_X,
            storage_key=historical.storage_key,
            content_type="application/pdf",
            size_bytes=len(CONTENT_X),
            status="queued",
            created_at=datetime.now(UTC).isoformat(),
        )
        job = IngestionJob(
            job_id="job_roll",
            document_id=document_a.document_id,
            version_id=restored.version_id,
            status="queued",
            created_at=datetime.now(UTC).isoformat(),
            updated_at=datetime.now(UTC).isoformat(),
        )
        ops = [
            PendingIndexOp(
                op_id=f"op_roll_{destination}",
                version_id=restored.version_id,
                destination=destination,
                operation="index",
            )
            for destination in ("pinecone", "bm25")
        ]
        with pytest.raises(DuplicateDocumentError):
            await repo.create_version_with_claim(
                restored, claim_sha256=SHA_X, job=job, ops=ops
            )
        # Rollback completo: sin versión, job ni ops huérfanas y claim intacto.
        with pytest.raises(NotFoundError):
            await repo.get_version(restored.version_id)
        with pytest.raises(NotFoundError):
            await repo.get_job(job.job_id)
        assert await repo.list_ops_for_version(restored.version_id) == []
        assert await _claim_of(repo, document_a.document_id) == SHA_Y

