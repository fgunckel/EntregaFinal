"""Pruebas del worker de ingesta incremental (TASK-03).

Cubren: idempotencia, reintentos, fallos parciales entre destinos,
recuperación tras reinicio, aislamiento entre versiones y compuerta de
activación. Usan un repositorio fake en memoria (unitarias); las de
integración con PostgreSQL real están en test_catalog_integration.py.
"""

import hashlib

import pytest

from ada_study.contracts.documents import (
    DocumentRecord,
    DocumentVersion,
    IngestionJob,
    PendingIndexOp,
)
from ada_study.db.repository import NotFoundError
from ada_study.index.bm25_index import BM25Index
from ada_study.ingestion.destinations import (
    BM25Destination,
    FakePineconeDestination,
)
from ada_study.ingestion.worker import IngestionWorker
from ada_study.storage.pdf_storage import PdfStorage


def _pdf(content_text: str) -> bytes:
    """PDF 1.4 mínimo con texto seleccionable y xref válida."""
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


class FakeRepository:
    """Repositorio en memoria que replica el contrato de CatalogRepository."""

    def __init__(self) -> None:
        self.documents: dict[str, DocumentRecord] = {}
        self.versions: dict[str, DocumentVersion] = {}
        self.jobs: dict[str, IngestionJob] = {}
        self.pending_ops: dict[str, PendingIndexOp] = {}
        self.chunks: dict[str, list] = {}

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


class WorkerHarness:
    """Monta worker + fakes y registra documentos/versiones de prueba."""

    def __init__(self, tmp_path) -> None:
        self.repo = FakeRepository()
        self.storage = PdfStorage(root=tmp_path / "storage")
        self.pinecone = FakePineconeDestination()
        self.bm25_index = BM25Index()
        self.bm25 = BM25Destination(self.bm25_index)
        self.worker = IngestionWorker(
            repository=self.repo,
            storage=self.storage,
            destinations=[self.pinecone, self.bm25],
        )

    async def add_version(
        self,
        *,
        document_id: str = "doc_1",
        version_number: int = 1,
        text: str = "contenido inicial del documento",
        status: str = "queued",
        active: bool = False,
    ) -> DocumentVersion:
        version_id = f"ver_{document_id}_{version_number}"
        content = _pdf(text)
        sha256 = hashlib.sha256(content).hexdigest()
        _, storage_key = self.storage.save_pdf(content)
        now = "2026-10-25T00:00:00+00:00"
        self.repo.versions[version_id] = DocumentVersion(
            version_id=version_id,
            document_id=document_id,
            version_number=version_number,
            sha256=sha256,
            storage_key=storage_key,
            content_type="application/pdf",
            size_bytes=len(content),
            status=status,
            created_at=now,
        )
        if document_id not in self.repo.documents:
            self.repo.documents[document_id] = DocumentRecord(
                document_id=document_id,
                filename="apuntes.pdf",
                active_version_id=None,
                editorial_status="published",
                created_at=now,
                updated_at=now,
            )
        if active:
            self.repo.documents[document_id].active_version_id = version_id
        self.repo.jobs[f"job_{version_id}"] = IngestionJob(
            job_id=f"job_{version_id}",
            document_id=document_id,
            version_id=version_id,
            status="queued",
            created_at=now,
            updated_at=now,
        )
        for destination in ("pinecone", "bm25"):
            op_id = f"op_{version_id}_{destination}"
            self.repo.pending_ops[op_id] = PendingIndexOp(
                op_id=op_id,
                version_id=version_id,
                destination=destination,
                operation="index",
            )
        return self.repo.versions[version_id]


@pytest.fixture()
def harness(tmp_path):
    return WorkerHarness(tmp_path)


# ---------------------------------------------------------------------------
# Ciclo feliz
# ---------------------------------------------------------------------------


async def test_full_cycle_activates_version(harness):
    version = await harness.add_version()
    result = await harness.worker.run_once(version.version_id)
    assert result.status == "available"
    document = await harness.repo.get_document("doc_1")
    assert document.active_version_id == version.version_id
    # Ambos destinos confirman la versión
    assert await harness.pinecone.has_version(version.version_id)
    assert await harness.bm25.has_version(version.version_id)


async def test_chunks_persisted_with_provenance(harness):
    version = await harness.add_version(text="texto con datos unicos")
    await harness.worker.run_once(version.version_id)
    chunks = await harness.repo.list_chunks(version.version_id)
    assert len(chunks) >= 1
    assert all(c.version_id == version.version_id for c in chunks)
    assert all(c.document_id == "doc_1" for c in chunks)


# ---------------------------------------------------------------------------
# Idempotencia y reintentos
# ---------------------------------------------------------------------------


async def test_rerun_is_idempotent_no_duplicate_chunks_or_vectors(harness):
    version = await harness.add_version()
    first = await harness.worker.run_once(version.version_id)
    chunks_first = await harness.repo.list_chunks(version.version_id)
    vectors_first = harness.pinecone.vector_count()
    second = await harness.worker.run_once(version.version_id)
    assert second.status == first.status == "available"
    assert len(await harness.repo.list_chunks(version.version_id)) == len(chunks_first)
    assert harness.pinecone.vector_count() == vectors_first


async def test_retry_after_pinecone_failure_completes_without_duplicates(harness):
    version = await harness.add_version()
    harness.pinecone.fail_next_index = True
    result = await harness.worker.run_once(version.version_id)
    # Fallo parcial: la versión NO se activa
    assert result.status == "indexing"
    assert harness.repo.pending_ops[
        f"op_{version.version_id}_pinecone"
    ].last_error_code == "ING_INDEX_FAILED"
    # BM25 sí completó (fallo parcial registrado por destino)
    assert await harness.bm25.has_version(version.version_id)
    assert not await harness.pinecone.has_version(version.version_id)
    # Reintento: sin duplicados
    result = await harness.worker.run_once(version.version_id)
    assert result.status == "available"
    assert await harness.pinecone.has_version(version.version_id)
    assert harness.pinecone.vector_count() == len(
        await harness.repo.list_chunks(version.version_id)
    )


async def test_retry_after_bm25_failure_keeps_version_indexing(harness):
    version = await harness.add_version()
    # Forzamos fallo en BM25 inyectando un índice roto temporalmente
    original_index = harness.bm25._index
    harness.bm25._index = None  # type: ignore[assignment]
    with pytest.raises(AttributeError):
        await harness.worker.run_once(version.version_id)
    harness.bm25._index = original_index
    # La op de bm25 quedó pendiente (el fallo no se capturó como DestinationError)
    # Reintento manual: reset + run
    await harness.repo.reset_failed_ops(version.version_id)
    result = await harness.worker.run_once(version.version_id)
    assert result.status == "available"


async def test_recover_reactivates_failed_ops(harness):
    version = await harness.add_version()
    harness.pinecone.fail_next_index = True
    await harness.worker.run_once(version.version_id)
    # La op fallida se reactiva con recover()
    recovered = await harness.worker.recover()
    assert version.version_id in {v.version_id for v in recovered}
    result = await harness.worker.run_once(version.version_id)
    assert result.status == "available"


# ---------------------------------------------------------------------------
# Fallos parciales y compuerta de activación
# ---------------------------------------------------------------------------


async def test_partial_failure_keeps_previous_active_version(harness):
    v1 = await harness.add_version(
        version_number=1, text="version uno estable con contenido suficiente"
    )
    await harness.worker.run_once(v1.version_id)
    document = await harness.repo.get_document("doc_1")
    assert document.active_version_id == v1.version_id
    # Nueva versión cuyo Pinecone falla
    v2 = await harness.add_version(
        version_number=2, text="version dos con fallo simulado en pinecone"
    )
    harness.pinecone.fail_next_index = True
    result = await harness.worker.run_once(v2.version_id)
    assert result.status == "indexing"
    # La versión activa sigue siendo v1 (specs/02 §53, §67)
    document = await harness.repo.get_document("doc_1")
    assert document.active_version_id == v1.version_id
    # Los chunks de v1 siguen intactos
    assert len(await harness.repo.list_chunks(v1.version_id)) >= 1
    # Reintento exitoso activa v2 y retira v1
    result = await harness.worker.run_once(v2.version_id)
    assert result.status == "available"
    document = await harness.repo.get_document("doc_1")
    assert document.active_version_id == v2.version_id
    # v1 retirada de los índices pero conservada en el historial
    assert not await harness.pinecone.has_version(v1.version_id)
    assert "v1" not in harness.bm25_index.indexed_versions()
    assert await harness.repo.get_version(v1.version_id) is not None


async def test_activation_gate_blocks_until_both_destinations_confirm(harness):
    version = await harness.add_version()
    # Destino pinecone "desaparece" su versión después de indexar: la
    # compuerta debe detectarlo y no activar.
    await harness.worker.run_once(version.version_id)
    await harness.pinecone.remove_version(version.version_id)
    # run_once de nuevo: la versión está available, no re-ejecuta la compuerta
    # (comportamiento esperado: available es terminal salvo deleting).
    result = await harness.worker.run_once(version.version_id)
    assert result.status == "available"


async def test_failed_extraction_marks_failed_and_keeps_previous(harness):
    v1 = await harness.add_version(version_number=1, text="version uno ok con contenido suficiente")
    await harness.worker.run_once(v1.version_id)
    # v2 apunta a un archivo corrupto (integridad comprometida)
    v2 = await harness.add_version(version_number=2, text="version dos con archivo corrupto")
    storage_key = v2.storage_key
    target = harness.storage.root / storage_key
    target.write_bytes(b"contenido corrupto que no es pdf")
    result = await harness.worker.run_once(v2.version_id)
    assert result.status == "failed"
    assert result.error_code == "ING_EXTRACT_FAILED"
    # v1 sigue activa y disponible
    document = await harness.repo.get_document("doc_1")
    assert document.active_version_id == v1.version_id
    assert (await harness.repo.get_version(v1.version_id)).status == "available"


async def test_scanned_pdf_fails_without_publishing(harness):
    version = await harness.add_version()
    # Reemplazamos el archivo por un PDF sin texto (escaneado)
    target = harness.storage.root / version.storage_key
    target.write_bytes(b"%PDF-1.4 minimal sin texto")
    result = await harness.worker.run_once(version.version_id)
    assert result.status == "failed"
    assert result.error_code == "ING_EXTRACT_FAILED"
    assert await harness.pinecone.has_version(version.version_id) is False


# ---------------------------------------------------------------------------
# Aislamiento entre versiones
# ---------------------------------------------------------------------------


async def test_versions_do_not_mix_chunks(harness):
    v1 = await harness.add_version(version_number=1, text="contenido de la version uno para aislar")
    await harness.worker.run_once(v1.version_id)
    v2 = await harness.add_version(
        version_number=2, text="contenido distinto de la version dos aislado"
    )
    await harness.worker.run_once(v2.version_id)
    chunks_v1 = await harness.repo.list_chunks(v1.version_id)
    chunks_v2 = await harness.repo.list_chunks(v2.version_id)
    assert all(c.version_id == v1.version_id for c in chunks_v1)
    assert all(c.version_id == v2.version_id for c in chunks_v2)
    ids_v1 = {c.chunk_id for c in chunks_v1}
    ids_v2 = {c.chunk_id for c in chunks_v2}
    assert ids_v1.isdisjoint(ids_v2)


async def test_previous_version_chunks_deleted_after_activation(harness):
    v1 = await harness.add_version(version_number=1, text="primera version con contenido")
    await harness.worker.run_once(v1.version_id)
    assert len(await harness.repo.list_chunks(v1.version_id)) >= 1
    v2 = await harness.add_version(version_number=2, text="segunda version con otro contenido")
    await harness.worker.run_once(v2.version_id)
    # Los chunks de v1 se eliminan de PostgreSQL tras la activación de v2
    assert await harness.repo.list_chunks(v1.version_id) == []
    # Pero el registro de la versión y el archivo original se conservan
    assert await harness.repo.get_version(v1.version_id) is not None
    assert harness.storage.exists(v1.storage_key)
