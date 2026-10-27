"""Pruebas de servicios de dominio del catálogo (TASK-02).

Usan un repositorio fake en memoria (unitarias). Las de integración con
PostgreSQL real están en test_catalog_integration.py.
"""

from datetime import UTC, datetime

import pytest

from ada_study.contracts.documents import (
    DocumentRecord,
    DocumentVersion,
    IngestionJob,
    PendingIndexOp,
)
from ada_study.db.repository import DuplicateDocumentError
from ada_study.domain.catalog_service import CatalogService
from ada_study.storage.pdf_storage import PdfStorage


class FakeRepository:
    """Repositorio en memoria que replica el contrato de CatalogRepository."""

    def __init__(self) -> None:
        self.documents: dict[str, DocumentRecord] = {}
        self.versions: dict[str, DocumentVersion] = {}
        self.jobs: dict[str, IngestionJob] = {}
        self.pending_ops: dict[str, PendingIndexOp] = {}

    async def find_active_version_by_sha256(self, sha256: str) -> DocumentVersion | None:
        active_docs = {d.document_id for d in self.documents.values() if d.deleted_at is None}
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

    async def get_document(self, document_id: str) -> DocumentRecord:
        if document_id not in self.documents:
            from ada_study.db.repository import NotFoundError

            raise NotFoundError("Documento", document_id)
        return self.documents[document_id]

    async def get_version(self, version_id: str) -> DocumentVersion:
        if version_id not in self.versions:
            from ada_study.db.repository import NotFoundError

            raise NotFoundError("Versión", version_id)
        return self.versions[version_id]

    async def list_versions(self, document_id: str) -> list[DocumentVersion]:
        return sorted(
            (v for v in self.versions.values() if v.document_id == document_id),
            key=lambda v: v.version_number,
        )

    async def enqueue_pending_op(self, op: PendingIndexOp) -> None:
        self.pending_ops[op.op_id] = op


def _service() -> tuple[CatalogService, FakeRepository, PdfStorage]:
    repo = FakeRepository()
    storage = PdfStorage(root=_tmp_root())
    return CatalogService(repository=repo, storage=storage), repo, storage


def _tmp_root():
    import tempfile
    from pathlib import Path

    return Path(tempfile.mkdtemp(prefix="ada_study_test_"))


PDF_A = b"%PDF-1.4 documento A"
PDF_B = b"%PDF-1.4 documento B"


class TestRegisterDocument:
    async def test_register_creates_document_version_job(self):
        service, repo, _ = _service()
        document, version, job = await service.register_document(
            filename="apuntes.pdf", content=PDF_A
        )
        assert document.document_id in repo.documents
        assert version.version_id in repo.versions
        assert job.job_id in repo.jobs
        assert document.active_version_id is None  # se activa tras ingesta
        assert version.status == "queued"  # no disponible antes de indexar
        assert job.status == "queued"

    async def test_register_enqueues_outbox_ops_for_both_destinations(self):
        service, repo, _ = _service()
        _, version, _ = await service.register_document(filename="a.pdf", content=PDF_A)
        destinations = {
            op.destination
            for op in repo.pending_ops.values()
            if op.version_id == version.version_id
        }
        assert destinations == {"pinecone", "bm25"}
        assert all(op.operation == "index" for op in repo.pending_ops.values())

    async def test_duplicate_sha256_raises_409_ing_duplicate(self):
        service, _, _ = _service()
        await service.register_document(filename="a.pdf", content=PDF_A)
        with pytest.raises(DuplicateDocumentError) as exc_info:
            await service.register_document(filename="copia.pdf", content=PDF_A)
        assert exc_info.value.error_code == "ING_DUPLICATE"

    async def test_duplicate_does_not_create_second_document(self):
        service, repo, _ = _service()
        await service.register_document(filename="a.pdf", content=PDF_A)
        with pytest.raises(DuplicateDocumentError):
            await service.register_document(filename="copia.pdf", content=PDF_A)
        assert len(repo.documents) == 1
        assert len(repo.versions) == 1

    async def test_physical_file_reused_but_catalog_separate(self):
        service, _, storage = _service()
        await service.register_document(filename="a.pdf", content=PDF_A)
        # El archivo físico existe una sola vez
        files = list(storage.root.rglob("*.pdf"))
        assert len(files) == 1
        # Un segundo documento con contenido distinto sí se registra
        document_b, _, _ = await service.register_document(filename="b.pdf", content=PDF_B)
        assert document_b.document_id is not None


class TestRestoreVersion:
    async def test_restore_creates_new_version_without_touching_history(self):
        service, repo, _ = _service()
        document, version_v1, _ = await service.register_document(filename="a.pdf", content=PDF_A)
        # Simular una v2 con contenido distinto
        v2 = DocumentVersion(
            version_id="ver_2",
            document_id=document.document_id,
            version_number=2,
            sha256=service._storage.compute_sha256(PDF_B),
            storage_key=service._storage.storage_key_for(service._storage.compute_sha256(PDF_B)),
            content_type="application/pdf",
            size_bytes=len(PDF_B),
            status="available",
            created_at=datetime.now(UTC).isoformat(),
        )
        repo.versions["ver_2"] = v2
        restored = await service.restore_version(document.document_id, version_v1.version_id)
        # La nueva versión es distinta de la histórica
        assert restored.version_id != version_v1.version_id
        assert restored.version_number == 3  # siguiente número
        assert restored.sha256 == version_v1.sha256  # hereda la huella
        assert restored.storage_key == version_v1.storage_key  # mismo archivo físico
        assert restored.status == "queued"  # debe reindexarse antes de activarse
        # El historial previo no se modifica
        assert repo.versions[version_v1.version_id].status == "queued"
        assert repo.versions["ver_2"].status == "available"

    async def test_restore_enqueues_index_ops(self):
        service, repo, _ = _service()
        document, version_v1, _ = await service.register_document(filename="a.pdf", content=PDF_A)
        restored = await service.restore_version(document.document_id, version_v1.version_id)
        destinations = {
            op.destination
            for op in repo.pending_ops.values()
            if op.version_id == restored.version_id
        }
        assert destinations == {"pinecone", "bm25"}

    async def test_restore_unknown_version_raises_not_found(self):
        service, _, _ = _service()
        document, _, _ = await service.register_document(filename="a.pdf", content=PDF_A)
        from ada_study.db.repository import NotFoundError

        with pytest.raises(NotFoundError):
            await service.restore_version(document.document_id, "ver_inexistente")

    async def test_restore_version_of_other_document_raises_not_found(self):
        service, _, _ = _service()
        doc_a, _, _ = await service.register_document(filename="a.pdf", content=PDF_A)
        doc_b, _, _ = await service.register_document(filename="b.pdf", content=PDF_B)
        from ada_study.db.repository import NotFoundError

        versions_a = await service.list_document_versions(doc_a.document_id)
        with pytest.raises(NotFoundError):
            await service.restore_version(doc_b.document_id, versions_a[0].version_id)
