"""Pruebas de servicios de dominio del catálogo (TASK-02).

Usan un repositorio fake en memoria (unitarias). Las de integración con
PostgreSQL real están en test_catalog_integration.py.
"""

from collections.abc import Callable
from datetime import UTC, datetime

import pytest

from ada_study.contracts.documents import (
    DocumentRecord,
    DocumentVersion,
    IngestionJob,
    PendingIndexOp,
)
from ada_study.db.repository import DuplicateDocumentError, NotFoundError
from ada_study.domain.catalog_service import CatalogService
from ada_study.storage.pdf_storage import PdfStorage


class FakeRepository:
    """Repositorio en memoria que replica el contrato de CatalogRepository.

    Réplica de la semántica de ADR-0001 rev. 3 (no un simulacro de éxitos):
    - El claim de huella (`documents.sha256`) nace con el documento y solo
      tiene UN dueño vivo; el borrado lo libera (índice parcial simulado).
    - `create_document_atomic` aplica documento + claim + versión + job + ops
      del outbox como una única transacción: un fallo en cualquier paso
      revierte TODO (sin residuos parciales).
    - `create_version_with_claim` traslada el claim y da de alta versión +
      job + ops en una única transacción; si otro documento vivo reclama la
      huella, lanza DuplicateDocumentError (409 ING_DUPLICATE) sin dejar
      versión, job ni ops parciales.
    - `fail_next_atomic` inyecta un fallo a mitad de la transacción (análogo
      a una violación de integridad posterior al INSERT del documento) para
      verificar el rollback completo.
    """

    def __init__(self) -> None:
        self.documents: dict[str, DocumentRecord] = {}
        self.versions: dict[str, DocumentVersion] = {}
        self.jobs: dict[str, IngestionJob] = {}
        self.pending_ops: dict[str, PendingIndexOp] = {}
        # Claim de dedup (`documents.sha256`): huella -> documento vivo dueño.
        self.document_claims: dict[str, str] = {}
        # Inyección de fallos para probar el rollback atómico.
        self.fail_next_atomic: Exception | None = None

    # -- transacción simulada ------------------------------------------------

    def _fail_midway(self) -> None:
        """Inyecta un fallo a mitad de transacción (si hay uno encolado)."""
        if self.fail_next_atomic is not None:
            failure, self.fail_next_atomic = self.fail_next_atomic, None
            raise failure

    def _apply_atomically(
        self, steps: list[tuple[Callable[[], None], Callable[[], None]]]
    ) -> None:
        """Aplica pares (hacer, deshacer) en orden o revierte lo ya aplicado."""
        applied: list[Callable[[], None]] = []
        try:
            for apply_step, undo_step in steps:
                apply_step()
                applied.append(undo_step)
        except BaseException:
            for undo_step in reversed(applied):
                undo_step()
            raise

    # -- claim de huella (documents.sha256, ADR-0001 rev. 3) -----------------

    def _latest_claimant_version(
        self, sha256: str, document_id: str
    ) -> DocumentVersion | None:
        candidates = [
            v
            for v in self.versions.values()
            if v.document_id == document_id
            and v.sha256 == sha256
            and v.status != "deleting"
        ]
        return max(candidates, key=lambda v: v.version_number) if candidates else None

    async def find_active_version_by_sha256(self, sha256: str) -> DocumentVersion | None:
        claimant_id = self.document_claims.get(sha256)
        if claimant_id is None:
            return None
        claimant = self.documents.get(claimant_id)
        if claimant is None or claimant.deleted_at is not None:
            return None  # el borrado libera la huella (índice parcial)
        return self._latest_claimant_version(sha256, claimant_id)

    def _assert_claimable(self, sha256: str, claimant_id: str) -> None:
        """Índice único `ux_documents_active_sha256` simulado.

        Rechaza ANTES de escribir (como el INSERT que viola el índice) si
        otro documento vivo reclama la huella, traduciendo a la excepción de
        dominio (409 ING_DUPLICATE) con el reclamante vigente.
        """
        current = self.document_claims.get(sha256)
        if current is None or current == claimant_id:
            return
        existing = self._latest_claimant_version(sha256, current)
        if existing is not None:
            raise DuplicateDocumentError(current, existing.version_id)


    # -- catálogo ------------------------------------------------------------

    async def create_document(self, record: DocumentRecord, *, sha256: str) -> None:
        """Inserta el documento con su claim de huella; falla si ya existe."""
        self._assert_claimable(sha256, record.document_id)
        self.documents[record.document_id] = record
        self.document_claims[sha256] = record.document_id

    async def create_document_atomic(
        self,
        record: DocumentRecord,
        *,
        sha256: str,
        version: DocumentVersion,
        job: IngestionJob,
        ops: list[PendingIndexOp],
    ) -> None:
        """Registro ATÓMICO: documento + claim + versión + job + outbox.

        Un fallo en cualquier paso (incluido uno inyectado a mitad de la
        transacción) revierte todo: sin documento, claim, versión, job ni ops.
        """
        self._assert_claimable(sha256, record.document_id)
        self._apply_atomically(
            [
                (
                    lambda: self.documents.__setitem__(record.document_id, record),
                    lambda: self.documents.pop(record.document_id, None),
                ),
                (
                    lambda: self.document_claims.__setitem__(sha256, record.document_id),
                    lambda: self.document_claims.pop(sha256, None),
                ),
                # Fallo a mitad de la transacción (inyección de pruebas):
                # deja aplicados documento + claim para verificar el rollback.
                (self._fail_midway, lambda: None),
                (
                    lambda: self.versions.__setitem__(version.version_id, version),
                    lambda: self.versions.pop(version.version_id, None),
                ),
                (
                    lambda: self.jobs.__setitem__(job.job_id, job),
                    lambda: self.jobs.pop(job.job_id, None),
                ),
                *[
                    (
                        lambda op=op: self.pending_ops.__setitem__(op.op_id, op),
                        lambda op_id=op.op_id: self.pending_ops.pop(op_id, None),
                    )
                    for op in ops
                ],
            ]
        )

    async def create_version_with_claim(
        self,
        version: DocumentVersion,
        *,
        claim_sha256: str,
        job: IngestionJob,
        ops: list[PendingIndexOp],
    ) -> DocumentVersion:
        """Nueva versión + traslado del claim, en UNA transacción (rev. 3).

        Si OTRO documento vivo reclama la huella, lanza DuplicateDocumentError
        y revierte TODO: sin versión, job, ops ni traslado de claim.
        """
        document = self.documents.get(version.document_id)
        if document is None or document.deleted_at is not None:
            raise NotFoundError("Documento", version.document_id)
        self._assert_claimable(claim_sha256, version.document_id)
        claims_snapshot = dict(self.document_claims)
        self._apply_atomically(
            [
                # Traslado del claim del documento hacia la huella reclamada.
                (
                    lambda: self._move_claim(version.document_id, claim_sha256),
                    lambda: self._restore_claims(claims_snapshot),
                ),
                (self._fail_midway, lambda: None),
                (
                    lambda: self.versions.__setitem__(version.version_id, version),
                    lambda: self.versions.pop(version.version_id, None),
                ),
                (
                    lambda: self.jobs.__setitem__(job.job_id, job),
                    lambda: self.jobs.pop(job.job_id, None),
                ),
                *[
                    (
                        lambda op=op: self.pending_ops.__setitem__(op.op_id, op),
                        lambda op_id=op.op_id: self.pending_ops.pop(op_id, None),
                    )
                    for op in ops
                ],
            ]
        )
        return version

    def _move_claim(self, document_id: str, claim_sha256: str) -> None:
        """Traslada el claim vivo del documento hacia la huella reclamada."""
        for sha, owner in list(self.document_claims.items()):
            if owner == document_id and sha != claim_sha256:
                del self.document_claims[sha]
        self.document_claims[claim_sha256] = document_id

    def _restore_claims(self, snapshot: dict[str, str]) -> None:
        """Restaura el estado completo del claim de dedup (rollback)."""
        self.document_claims.clear()
        self.document_claims.update(snapshot)

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


class TestAtomicRegistration:
    """Interfaz atómica de ADR-0001 rev. 3: registro y restore todo-o-nada."""

    async def test_register_moves_through_create_document_atomic(self):
        """El registro persiste documento + claim + versión + job + ops juntos."""
        service, repo, _ = _service()
        document, version, job = await service.register_document(
            filename="a.pdf", content=PDF_A
        )
        assert repo.document_claims[version.sha256] == document.document_id
        assert repo.jobs[job.job_id].document_id == document.document_id
        assert repo.versions[version.version_id].document_id == document.document_id

    async def test_atomic_failure_rolls_back_everything(self):
        """Un fallo a mitad de la transacción no deja ningún residuo."""
        service, repo, _ = _service()
        repo.fail_next_atomic = RuntimeError("fallo simulado a mitad de la transacción")
        with pytest.raises(RuntimeError):
            await service.register_document(filename="a.pdf", content=PDF_A)
        # Rollback completo: sin documento, claim, versión, job ni ops.
        assert repo.documents == {}
        assert repo.versions == {}
        assert repo.jobs == {}
        assert repo.pending_ops == {}
        assert repo.document_claims == {}

    async def test_atomic_duplicate_claim_translates_to_409(self):
        """Índice único: la huella reclamada por otro documento vivo → 409."""
        service, repo, _ = _service()
        document, version, _ = await service.register_document(filename="a.pdf", content=PDF_A)
        now = datetime.now(UTC).isoformat()
        record = DocumentRecord(
            document_id="doc_otro",
            filename="otro.pdf",
            created_at=now,
            updated_at=now,
        )
        race_version = DocumentVersion(
            version_id="ver_otro",
            document_id="doc_otro",
            version_number=1,
            sha256=version.sha256,  # misma huella: índice único la rechaza
            storage_key=version.storage_key,
            content_type="application/pdf",
            size_bytes=1,
            created_at=now,
        )
        job = IngestionJob(
            job_id="job_otro",
            document_id="doc_otro",
            version_id="ver_otro",
            created_at=now,
            updated_at=now,
        )
        ops = [
            PendingIndexOp(
                op_id=f"op_otro_{destination}",
                version_id="ver_otro",
                destination=destination,
                operation="index",
            )
            for destination in ("pinecone", "bm25")
        ]
        with pytest.raises(DuplicateDocumentError) as exc_info:
            await repo.create_document_atomic(
                record, sha256=version.sha256, version=race_version, job=job, ops=ops
            )
        assert exc_info.value.error_code == "ING_DUPLICATE"
        assert exc_info.value.existing_document_id == document.document_id
        # Sin residuos: solo quedó el documento original con sus piezas.
        assert len(repo.documents) == 1
        assert len(repo.versions) == 1
        assert len(repo.jobs) == 1
        assert len(repo.pending_ops) == 2

    async def test_restore_failure_rolls_back_and_keeps_history_and_claim(self):
        """El restore atómico revierte versión/job/ops y preserva el claim."""
        service, repo, _ = _service()
        document, version_v1, _ = await service.register_document(
            filename="a.pdf", content=PDF_A
        )
        repo.fail_next_atomic = RuntimeError("fallo simulado a mitad de la transacción")
        with pytest.raises(RuntimeError):
            await service.restore_version(document.document_id, version_v1.version_id)
        # Sin versión nueva, sin job nuevo y sin ops nuevas.
        assert len(repo.versions) == 1
        assert len(repo.jobs) == 1
        assert len(repo.pending_ops) == 2
        # El claim de la huella no se perdió ni se traslado a medias.
        assert repo.document_claims[version_v1.sha256] == document.document_id

    async def test_restore_conflict_with_other_claimant_raises_409(self):
        """Si otro documento vivo reclama la huella histórica, restore → 409."""
        service, repo, _ = _service()
        doc_a, _, _ = await service.register_document(filename="a.pdf", content=PDF_A)
        # Otro documento vivo reclama la huella de PDF_B.
        doc_b, version_b, _ = await service.register_document(filename="b.pdf", content=PDF_B)
        # doc_a intenta restaurar una versión histórica con la huella de doc_b.
        historical = DocumentVersion(
            version_id="ver_historica",
            document_id=doc_a.document_id,
            version_number=2,
            sha256=version_b.sha256,
            storage_key=version_b.storage_key,
            content_type="application/pdf",
            size_bytes=len(PDF_B),
            status="available",
            created_at=datetime.now(UTC).isoformat(),
        )
        repo.versions["ver_historica"] = historical
        with pytest.raises(DuplicateDocumentError) as exc_info:
            await service.restore_version(doc_a.document_id, "ver_historica")
        assert exc_info.value.existing_document_id == doc_b.document_id
        # Sin residuos del restore abortado: no hay versión, job ni ops nuevos.
        assert len(repo.versions) == 3  # v1 de a + v1 de b + histórica, nada más
        assert repo.document_claims[version_b.sha256] == doc_b.document_id

