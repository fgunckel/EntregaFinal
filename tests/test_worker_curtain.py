"""Cortina editorial en el worker: consistencia eventual (ADR-0004).

Cubren el defecto detectado en la revisión del ZIP:
- Borrado durante queued/processing/indexing: la versión NO se indexa ni
  se activa después de la cortina; la purga queda garantizada con ops
  delete persistidas para todos los destinos.
- Retiro por moderación antes de la primera activación: mismo tratamiento.
- Ops index/reconcile que intentan ejecutarse tras borrado/retiro: no se
  ejecutan ni se completan; solo ops delete.
- Purga idempotente en ambos destinos: repetir no duplica ni falla; una
  op delete no queda done hasta que el destino confirma.
- recover() no reactiva ops index/reconcile bajo cortina; las delete sí.
- Acceso privado del autor a pending_review preservado (no retirado ni
  eliminado).
"""

import hashlib

import pytest

from ada_study.contracts.documents import (
    DocumentRecord,
    DocumentVersion,
    IngestionJob,
    PendingIndexOp,
)
from ada_study.index.bm25_index import BM25Index
from ada_study.ingestion.destinations import (
    BM25Destination,
    FakePineconeDestination,
)
from ada_study.ingestion.worker import IngestionWorker
from ada_study.storage.pdf_storage import PdfStorage
from tests.test_worker import FakeRepository, _pdf


class CurtainHarness:
    """Monta worker + fakes con control fino del estado editorial."""

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
        editorial_status: str = "published",
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
                editorial_status=editorial_status,
                created_at=now,
                updated_at=now,
            )
        else:
            self.repo.documents[document_id].editorial_status = editorial_status
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

    def set_editorial(self, document_id: str, status: str) -> None:
        """Simula moderación/borrado concurrente (cortina)."""
        self.repo.documents[document_id].editorial_status = status

    def delete_ops(self, version_id: str) -> list:
        return [
            op
            for op in self.repo.pending_ops.values()
            if op.version_id == version_id and op.operation == "delete"
        ]


@pytest.fixture()
def curtain_harness(tmp_path):
    return CurtainHarness(tmp_path)


# ---------------------------------------------------------------------------
# Grupo 1: borrado durante queued / processing / indexing
# ---------------------------------------------------------------------------


class TestDeleteDuringProcessing:
    async def test_delete_during_queued_never_indexes(self, curtain_harness):
        """Borrado con versión queued: no indexa, no activa, purga garantizada."""
        h = curtain_harness
        version = await h.add_version(status="queued")
        h.set_editorial("doc_1", "deleted")  # carrera: borrado antes de procesar
        await h.worker.run_once(version.version_id)
        # No se indexó en ningún destino ni se activó.
        assert not await h.pinecone.has_version(version.version_id)
        assert version.version_id not in h.bm25_index.indexed_versions()
        document = await h.repo.get_document("doc_1")
        assert document.active_version_id is None
        # Purga garantizada: ops delete persistidas para ambos destinos.
        assert {op.destination for op in h.delete_ops(version.version_id)} == {
            "pinecone",
            "bm25",
        }
        # Y ejecutadas: ambos destinos confirman la eliminación.
        assert not await h.pinecone.has_version(version.version_id)
        assert version.version_id not in h.bm25_index.indexed_versions()

    async def test_delete_during_processing_blocks_indexing(self, curtain_harness):
        """Carrera: cortina entre extracción y transición a indexing."""
        h = curtain_harness
        version = await h.add_version(status="processing")
        # Simular la carrera: el documento se borra justo después de iniciar
        # el ciclo (la extracción ya corrió; la cortina se detecta después).
        h.set_editorial("doc_1", "deleted")
        result = await h.worker.run_once(version.version_id)
        # La versión NO avanza a indexing ni se activa.
        assert result.status != "indexing"
        assert result.status != "available"
        assert not await h.pinecone.has_version(version.version_id)
        assert version.version_id not in h.bm25_index.indexed_versions()
        # Purga garantizada y ejecutada.
        assert {op.destination for op in h.delete_ops(version.version_id)} == {
            "pinecone",
            "bm25",
        }

    async def test_delete_during_indexing_purges_and_does_not_activate(
        self, curtain_harness
    ):
        """Carrera: cortina mientras la versión está indexándose."""
        h = curtain_harness
        version = await h.add_version(status="indexing")
        # Indexar parcialmente (solo pinecone) para simular fallo parcial.
        h.pinecone.fail_next_index = True
        await h.worker.run_once(version.version_id)
        result_status = (await h.repo.get_version(version.version_id)).status
        assert result_status == "indexing"
        # Cortina concurrente durante la indexación.
        h.set_editorial("doc_1", "deleted")
        result = await h.worker.run_once(version.version_id)
        # NO se activa: la cortina detiene la activación.
        assert result.status != "available"
        document = await h.repo.get_document("doc_1")
        assert document.active_version_id is None
        # Purga garantizada para ambos destinos.
        assert {op.destination for op in h.delete_ops(version.version_id)} == {
            "pinecone",
            "bm25",
        }
        assert not await h.pinecone.has_version(version.version_id)
        assert version.version_id not in h.bm25_index.indexed_versions()


# ---------------------------------------------------------------------------
# Grupo 2: retiro por moderación antes de la primera activación
# ---------------------------------------------------------------------------


class TestWithdrawBeforeActivation:
    async def test_withdraw_during_queued_never_indexes(self, curtain_harness):
        """Retiro con versión queued: no indexa, no activa, purga garantizada."""
        h = curtain_harness
        version = await h.add_version(status="queued")
        h.set_editorial("doc_1", "withdrawn")  # moderación antes de procesar
        await h.worker.run_once(version.version_id)
        assert not await h.pinecone.has_version(version.version_id)
        assert version.version_id not in h.bm25_index.indexed_versions()
        document = await h.repo.get_document("doc_1")
        assert document.active_version_id is None
        assert {op.destination for op in h.delete_ops(version.version_id)} == {
            "pinecone",
            "bm25",
        }

    async def test_withdraw_during_indexing_blocks_activation(self, curtain_harness):
        """Carrera: retiro mientras la versión se indexa; no se activa."""
        h = curtain_harness
        version = await h.add_version(status="indexing")
        h.set_editorial("doc_1", "withdrawn")
        result = await h.worker.run_once(version.version_id)
        assert result.status != "available"
        document = await h.repo.get_document("doc_1")
        assert document.active_version_id is None
        assert {op.destination for op in h.delete_ops(version.version_id)} == {
            "pinecone",
            "bm25",
        }
        assert not await h.pinecone.has_version(version.version_id)
        assert version.version_id not in h.bm25_index.indexed_versions()


# ---------------------------------------------------------------------------
# Grupo 3: ops index/reconcile que intentan ejecutarse tras la cortina
# ---------------------------------------------------------------------------


class TestOpsAfterCurtain:
    async def test_index_ops_not_executed_nor_completed_after_delete(
        self, curtain_harness
    ):
        """Bajo cortina: ops index no se ejecutan ni se marcan done."""
        h = curtain_harness
        version = await h.add_version(status="indexing")
        h.set_editorial("doc_1", "deleted")
        await h.worker.run_once(version.version_id)
        index_ops = [
            op
            for op in h.repo.pending_ops.values()
            if op.version_id == version.version_id and op.operation == "index"
        ]
        # Las ops index NO quedaron done (quedan pendientes para auditoría
        # del outbox) y NO se ejecutaron (los destinos no tienen la versión).
        assert all(op.status != "done" for op in index_ops)
        assert not await h.pinecone.has_version(version.version_id)
        assert version.version_id not in h.bm25_index.indexed_versions()

    async def test_reconcile_op_blocked_after_withdraw(self, curtain_harness):
        """Bajo cortina withdrawn: una op reconcile no se ejecuta."""
        h = curtain_harness
        version = await h.add_version(status="indexing", active=True)
        # Dejar una op reconcile pendiente (p. ej. tras restauración fallida).
        reconcile_op = PendingIndexOp(
            op_id="op_reconcile_1",
            version_id=version.version_id,
            destination="pinecone",
            operation="reconcile",
        )
        await h.repo.enqueue_pending_op(reconcile_op)
        h.set_editorial("doc_1", "withdrawn")
        await h.worker.run_once(version.version_id)
        # La op reconcile no se completó ni el destino la recibió.
        assert h.repo.pending_ops["op_reconcile_1"].status != "done"
        assert not await h.pinecone.has_version(version.version_id)


# ---------------------------------------------------------------------------
# Grupo 4: purga idempotente en ambos destinos
# ---------------------------------------------------------------------------


class TestPurgeIdempotency:
    async def test_repeated_curtain_does_not_duplicate_delete_ops(self, curtain_harness):
        """Repetir run_once bajo cortina no duplica ops delete."""
        h = curtain_harness
        version = await h.add_version(status="indexing")
        h.set_editorial("doc_1", "deleted")
        await h.worker.run_once(version.version_id)
        first_count = len(h.delete_ops(version.version_id))
        assert first_count == 2  # pinecone + bm25
        await h.worker.run_once(version.version_id)
        await h.worker.run_once(version.version_id)
        assert len(h.delete_ops(version.version_id)) == first_count

    async def test_delete_op_not_done_until_destination_confirms(self, curtain_harness):
        """Una op delete no queda done si el destino falla; reintento la recupera."""
        h = curtain_harness
        version = await h.add_version(status="indexing")
        h.set_editorial("doc_1", "deleted")
        h.pinecone.fail_next_remove = True  # primer intento de purga falla
        await h.worker.run_once(version.version_id)
        pinecone_delete = next(
            op
            for op in h.delete_ops(version.version_id)
            if op.destination == "pinecone"
        )
        # La op falló (no done) pero la del otro destino se completó.
        assert pinecone_delete.status == "failed"
        bm25_op = next(op for op in h.delete_ops(version.version_id) if op.destination == "bm25")
        assert bm25_op.status == "done"
        # Reintento: la op fallida se reactiva y la purga se completa.
        await h.repo.reset_failed_delete_ops(version.version_id)
        await h.worker.run_once(version.version_id)
        assert h.repo.pending_ops[pinecone_delete.op_id].status == "done"
        assert not await h.pinecone.has_version(version.version_id)
        assert version.version_id not in h.bm25_index.indexed_versions()

    async def test_remove_version_idempotent_in_both_destinations(
        self, curtain_harness
    ):
        """remove_version repetido no falla ni duplica (idempotencia)."""
        h = curtain_harness
        version = await h.add_version(status="indexing")
        h.set_editorial("doc_1", "deleted")
        await h.worker.run_once(version.version_id)
        # Repetir la purga manualmente en ambos destinos: sin excepción.
        await h.pinecone.remove_version(version.version_id)
        await h.bm25.remove_version(version.version_id)
        assert not await h.pinecone.has_version(version.version_id)
        assert version.version_id not in h.bm25_index.indexed_versions()


# ---------------------------------------------------------------------------
# Grupo 5: recover() bajo cortina
# ---------------------------------------------------------------------------


class TestRecoverUnderCurtain:
    async def test_recover_does_not_reactivate_index_ops_after_delete(
        self, curtain_harness
    ):
        """recover() bajo cortina deleted: no reactiva ops index; delete sí."""
        h = curtain_harness
        version = await h.add_version(status="indexing")
        h.set_editorial("doc_1", "deleted")
        # Fallar todas las ops (simula worker muerto con ops fallidas).
        for op in h.repo.pending_ops.values():
            if op.version_id == version.version_id:
                await h.repo.fail_pending_op(op.op_id, "ING_INDEX_FAILED")
        recovered = await h.worker.recover()
        index_ops = [
            op
            for op in h.repo.pending_ops.values()
            if op.version_id == version.version_id and op.operation == "index"
        ]
        delete_ops = h.delete_ops(version.version_id)
        # Las ops index permanecen fallidas (no se reintroducen tras cortina).
        assert all(op.status == "failed" for op in index_ops)
        # Las ops delete quedaron reactivadas (purga garantizada).
        assert all(op.status == "pending" for op in delete_ops)
        # La versión indexing huérfana bajo cortina sí se recupera (para purga).
        assert any(v.version_id == version.version_id for v in recovered)

    async def test_recover_reactivates_delete_ops_after_withdraw(self, curtain_harness):
        """recover() bajo cortina withdrawn: reactiva solo ops delete."""
        h = curtain_harness
        version = await h.add_version(status="indexing")
        h.set_editorial("doc_1", "withdrawn")
        await h.worker.run_once(version.version_id)  # cortina: crea ops delete
        # Fallar la op delete de pinecone (destino caído durante la purga).
        pinecone_delete = next(
            op for op in h.delete_ops(version.version_id) if op.destination == "pinecone"
        )
        # Fallar la op delete de pinecone (destino caído durante la purga).
        pinecone_delete = next(
            op for op in h.delete_ops(version.version_id) if op.destination == "pinecone"
        )
        await h.repo.fail_pending_op(pinecone_delete.op_id, "ING_INDEX_FAILED")
        await h.worker.recover()
        # La op delete quedó reactivada (pending) para reintento.
        assert h.repo.pending_ops[pinecone_delete.op_id].status == "pending"
        # Las ops index siguen sin ejecutarse.
        index_ops = [
            op
            for op in h.repo.pending_ops.values()
            if op.version_id == version.version_id and op.operation == "index"
        ]
        assert all(op.status != "done" for op in index_ops)
        assert not await h.pinecone.has_version(version.version_id)


# ---------------------------------------------------------------------------
# Grupo 6: carrera real a mitad del loop + acceso privado preservado
# ---------------------------------------------------------------------------


class TestConcurrencyAndPrivateAccess:
    async def test_curtain_appearing_mid_loop_stops_indexing(self, curtain_harness):
        """Carrera real: la cortina aparece DURANTE la ejecución de una op index.

        El primer destino indexa y, como efecto secundario, activa la cortina
        (simula moderación concurrente). El segundo destino NO debe indexar.
        """
        h = curtain_harness
        version = await h.add_version(status="indexing")

        original_index = h.pinecone.index_version

        async def index_and_delete_concurrently(version_id: str, chunks: list) -> None:
            await original_index(version_id, chunks)
            h.set_editorial("doc_1", "deleted")  # moderación concurrente

        h.pinecone.index_version = index_and_delete_concurrently  # type: ignore[method-assign]
        await h.worker.run_once(version.version_id)
        # Pinecone llegó a indexar (la cortina apareció después de su op),
        # pero la purga posterior lo retiró: el estado final es limpio.
        assert not await h.pinecone.has_version(version.version_id)
        # BM25 nunca indexó: la cortina se detectó antes de su op.
        assert version.version_id not in h.bm25_index.indexed_versions()
        # No se activó.
        document = await h.repo.get_document("doc_1")
        assert document.active_version_id is None
        # Purga garantizada para ambos destinos.
        assert {op.destination for op in h.delete_ops(version.version_id)} == {
            "pinecone",
            "bm25",
        }

    async def test_pending_review_document_processes_normally(self, curtain_harness):
        """Sin cortina: pending_review con versión en curso se activa normal.

        Garantiza que la cortina no sobre-actúa: el acceso pedagógico privado
        del autor (chunks de pending_review con versión available) sigue
        existiendo tras la ingesta.
        """
        h = curtain_harness
        version = await h.add_version(status="queued", editorial_status="pending_review")
        result = await h.worker.run_once(version.version_id)
        # Flujo normal: indexado en ambos destinos y activado.
        assert result.status == "available"
        assert await h.pinecone.has_version(version.version_id)
        assert version.version_id in h.bm25_index.indexed_versions()
        document = await h.repo.get_document("doc_1")
        assert document.active_version_id == version.version_id
        assert document.editorial_status == "pending_review"
