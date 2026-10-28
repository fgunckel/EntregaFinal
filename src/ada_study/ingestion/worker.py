"""Worker de ingesta incremental (TASK-03, specs/02).

Consume el outbox (`pending_index_ops`) y ejecuta el ciclo por versión:
1. `queued -> processing`: extrae texto con pypdf, detecta escaneados,
   genera chunks deterministas y los persiste en PostgreSQL.
2. `processing -> indexing`: ejecuta las ops del outbox por destino
   (Pinecone y BM25), con reintentos idempotentes.
3. Compuerta de activación: la versión pasa a `available` SOLO cuando
   ambos destinos confirman tener la versión indexada (specs/02 §47) y
   se cumplen las reglas de publicación (specs/06 §72/§104).
4. Al activar: apunta `active_version_id` a la nueva versión, retira de
   los índices los chunks de la versión anterior y los elimina de
   PostgreSQL (specs/02 §48). El original y los metadatos se conservan
   para historial/restauración.
5. Cortina editorial (ADR-0004, Spec 06 §104): si el documento está
   `deleted` o `withdrawn`, la versión NO se indexa ni se activa. La
   cortina se revalida al inicio del ciclo, antes de cada op
   index/reconcile y antes de activar (cubre cambios concurrentes de
   moderación/borrado durante el procesamiento). Bajo cortina solo se
   ejecutan ops `delete` (idempotentes y reintentables); la purga queda
   garantizada con ops `delete` persistidas para todos los destinos.

Fallos parciales: cada destino registra su op con estado y contador de
intentos; si uno falla, la versión queda `indexing` (no `available`) y
`recover()` reactiva las ops fallidas sin duplicar chunks (los destinos
hacen upsert idempotente). La versión activa anterior nunca se toca hasta
que la nueva esté `available`.
"""

import uuid

from ada_study.contracts.documents import (
    DocumentVersion,
    PendingIndexOp,
)
from ada_study.db.repository import CatalogRepository
from ada_study.ingestion.destinations import (
    DestinationError,
    IndexDestinationProtocol,
)
from ada_study.ingestion.extraction import ScannedPdfError, chunk_pages, extract_pages
from ada_study.storage.pdf_storage import PdfStorage


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# Estados editoriales bajo cortina (ADR-0004, Spec 06 §104): ninguna
# indexación ni activación procede; solo purga (ops delete idempotentes).
CURTAIN_EDITORIAL: frozenset[str] = frozenset({"deleted", "withdrawn"})


class IngestionWorker:
    """Worker asíncrono del outbox de indexación (specs/02 §27, §52)."""

    def __init__(
        self,
        repository: CatalogRepository,
        storage: PdfStorage,
        destinations: list[IndexDestinationProtocol],
    ) -> None:
        self._repo = repository
        self._storage = storage
        self._destinations = {destination.name: destination for destination in destinations}

    # -- cortina editorial (ADR-0004, Spec 06 §104) -----------------------------

    async def _editorial_curtain(self, document_id: str) -> str | None:
        """Devuelve el estado editorial bajo cortina del documento, o None.

        Revalida contra PostgreSQL (fuente de verdad) en cada llamada: la
        cortina puede aparecer en cualquier momento por moderación o borrado
        concurrentes, por lo que una comprobación inicial aislada no basta.
        """
        document = await self._repo.get_document(document_id)
        if document.editorial_status in CURTAIN_EDITORIAL:
            return document.editorial_status
        return None

    async def _ensure_delete_ops(self, version_id: str) -> None:
        """Garantiza ops `delete` persistidas para todos los destinos.

        Idempotente: solo encola una op delete por destino si no existe ya
        una (en cualquier estado) para ese par versión+destino. Así la purga
        queda garantizada aunque la cortina se detecte varias veces.
        """
        existing = await self._repo.list_ops_for_version(version_id)
        existing_deletes = {
            op.destination
            for op in existing
            if op.operation == "delete"
        }
        for destination_name in self._destinations:
            if destination_name in existing_deletes:
                continue
            await self._repo.enqueue_pending_op(
                PendingIndexOp(
                    op_id=_new_id("op"),
                    version_id=version_id,
                    destination=destination_name,
                    operation="delete",
                )
            )

    async def _curtain_version(self, version: DocumentVersion) -> DocumentVersion:
        """Aplica la cortina a una versión: purga sí, indexación/activación no.

        - Garantiza ops `delete` persistidas para todos los destinos (la
          purga es eventual pero garantizada, ADR-0004).
        - Ejecuta las ops `delete` pendientes/fallidas (idempotentes en los
          destinos; no se marca done hasta que el destino confirma).
        - Marca la versión `deleting` solo si la transición técnica lo
          permite (indexing/available/failed -> deleting, ADR-0005); desde
          queued/processing la versión queda congelada: el worker nunca la
          indexa ni la activa, y la purga de sus chunks procede igualmente.
        """
        await self._ensure_delete_ops(version.version_id)
        await self._run_delete_ops_only(version)
        if version.status in {"indexing", "available", "failed"}:
            try:
                await self._repo.update_version_status(version.version_id, "deleting")
            except Exception:
                # La transición puede haber cambiado concurrentemente; la
                # purga ya quedó garantizada con las ops delete.
                pass
        return await self._repo.get_version(version.version_id)

    async def _run_delete_ops_only(self, version: DocumentVersion) -> None:
        """Ejecuta SOLO las ops delete de la versión (idempotentes).

        Las ops index/reconcile se ignoran bajo cortina (no se ejecutan ni
        se completan: quedan para auditoría del outbox). Una op delete no
        se marca done hasta que el destino confirma la eliminación; si
        falla queda `failed` y `recover()` la reactiva (reintentable).
        """
        ops = await self._repo.list_ops_for_version(version.version_id)
        for op in ops:
            if op.status == "done" or op.operation != "delete":
                continue
            destination = self._destinations.get(op.destination)
            if destination is None:
                await self._repo.fail_pending_op(op.op_id, "ING_INDEX_FAILED")
                continue
            try:
                await destination.remove_version(version.version_id)
            except DestinationError:
                await self._repo.fail_pending_op(op.op_id, "ING_INDEX_FAILED")
                continue
            await self._repo.complete_pending_op(op.op_id)

    # -- ciclo principal -------------------------------------------------------

    async def run_once(self, version_id: str) -> DocumentVersion:
        """Procesa una versión completa: extracción, indexación y activación.

        Idempotente: puede invocarse repetidamente (reintentos) sin duplicar
        chunks ni activar la versión antes de tiempo.

        Cortina editorial: revalidada al inicio y antes de activar; si el
        documento está `deleted`/`withdrawn`, la versión se purga en lugar
        de indexarse (ADR-0004).
        """
        version = await self._repo.get_version(version_id)
        if await self._editorial_curtain(version.document_id) is not None:
            return await self._curtain_version(version)
        if version.status == "queued":
            version = await self._extract_and_persist_chunks(version)
        if version.status == "processing":
            version = await self._repo.update_version_status(version.version_id, "indexing")
        if version.status == "indexing":
            version = await self._run_outbox_ops(version)
        if version.status == "indexing" and await self._activation_gate(version):
            # Revalidar cortina antes de activar (cambio concurrente durante
            # la indexación: moderación o borrado entre ops y compuerta).
            if await self._editorial_curtain(version.document_id) is not None:
                return await self._curtain_version(version)
            version = await self._activate(version)
        return version

    async def recover(self) -> list[DocumentVersion]:
        """Recupera operaciones pendientes tras un reinicio o fallo.

        - Reactiva las ops fallidas de las versiones con ops pendientes
          (reintentos seguros, specs/02 §29).
        - Reencola versiones `indexing` huérfanas (worker muerto a mitad de
          indexación) reactivando sus ops fallidas o pendientes.
        - Cortina editorial: para documentos `deleted`/`withdrawn` NO
          reactiva ops index/reconcile (incompatibles con la cortina);
          solo reactiva ops `delete`, que siguen siendo recuperables
          (la purga no se abandona, ADR-0004).
        """
        recovered: list[DocumentVersion] = []
        version_ids = await self._repo.list_version_ids_with_pending_ops()
        for version_id in version_ids:
            version = await self._repo.get_version(version_id)
            curtain = await self._editorial_curtain(version.document_id)
            if curtain is not None:
                # Bajo cortina: reactivar solo ops delete (purga garantizada).
                await self._reset_failed_delete_ops(version_id)
                version = await self._repo.get_version(version_id)
                if version.status in {"indexing", "available", "failed"}:
                    recovered.append(version)
                continue
            await self._repo.reset_failed_ops(version_id)
            version = await self._repo.get_version(version_id)
            if version.status in {"queued", "processing", "indexing"}:
                recovered.append(version)
        return recovered

    async def _reset_failed_delete_ops(self, version_id: str) -> None:
        """Reactiva SOLO las ops delete fallidas de la versión.

        Las ops index/reconcile fallidas permanecen fallidas bajo cortina:
        recover() no debe reintroducir contenido tras la cortina editorial.
        """
        await self._repo.reset_failed_delete_ops(version_id)

    # -- extracción y chunks -----------------------------------------------------

    async def _extract_and_persist_chunks(self, version: DocumentVersion) -> DocumentVersion:
        """Extrae texto, genera chunks y los persiste (queued -> processing)."""
        version = await self._repo.update_version_status(version.version_id, "processing")
        try:
            content = self._storage.open_pdf(version.storage_key)
            pages = extract_pages(content)
        except ScannedPdfError:
            return await self._fail(version, "ING_EXTRACT_FAILED")
        except ValueError:
            return await self._fail(version, "ING_EXTRACT_FAILED")
        await self._repo.set_version_page_count(version.version_id, len(pages))
        document = await self._repo.get_document(version.document_id)
        chunks = chunk_pages(
            pages,
            document_id=version.document_id,
            version_id=version.version_id,
            title=document.filename,
            source=document.filename,
        )
        # replace_chunks es idempotente: repetir la extracción reemplaza los
        # chunks de ESTA versión sin tocar otras (aislamiento por version_id).
        await self._repo.replace_chunks(version.version_id, chunks)
        return version

    # -- outbox por destino --------------------------------------------------------

    async def _run_outbox_ops(self, version: DocumentVersion) -> DocumentVersion:
        """Ejecuta las ops del outbox de la versión, destino por destino.

        Fallos parciales: si un destino falla, su op queda `failed` con el
        código y el contador de intentos; la otra puede completarse. La
        versión permanece `indexing` hasta que ambos confirmen.

        Cortina editorial: revalidada antes de CADA op index/reconcile
        (cubre moderación/borrado concurrentes durante el loop). Si la
        cortina aparece a mitad del ciclo, las ops index/reconcile
        restantes no se ejecutan: se garantizan y ejecutan solo las ops
        delete (purga, ADR-0004).
        """
        ops = await self._repo.list_ops_for_version(version.version_id)
        for op in ops:
            if op.status == "done":
                continue
            # Revalidar cortina antes de cada op de indexación: la cortina
            # puede aparecer durante el procesamiento (concurrencia).
            if op.operation in {"index", "reconcile"}:
                if await self._editorial_curtain(version.document_id) is not None:
                    await self._ensure_delete_ops(version.version_id)
                    await self._run_delete_ops_only(version)
                    return version
            destination = self._destinations.get(op.destination)
            if destination is None:
                await self._repo.fail_pending_op(op.op_id, "ING_INDEX_FAILED")
                continue
            try:
                if op.operation == "index":
                    chunks = await self._repo.list_chunks(version.version_id)
                    await destination.index_version(version.version_id, chunks)
                elif op.operation == "delete":
                    await destination.remove_version(version.version_id)
                elif op.operation == "reconcile":
                    chunks = await self._repo.list_chunks(version.version_id)
                    await destination.index_version(version.version_id, chunks)
            except DestinationError:
                await self._repo.fail_pending_op(op.op_id, "ING_INDEX_FAILED")
                continue
            await self._repo.complete_pending_op(op.op_id)
        return version

    # -- compuerta de activación ------------------------------------------------------

    async def _activation_gate(self, version: DocumentVersion) -> bool:
        """Verifica que ambos destinos tengan la versión indexada.

        specs/02 §47: no activar hasta que extracción, chunks, Pinecone y
        BM25 hayan terminado y se haya verificado la presencia de sus
        fragmentos. La verificación es contra el destino real, no contra el
        estado del outbox.
        """
        for destination in self._destinations.values():
            if not await destination.has_version(version.version_id):
                return False
        return True

    async def _activate(self, version: DocumentVersion) -> DocumentVersion:
        """Activa la versión y retira la anterior (specs/02 §48).

        Defensa final: revalida la cortina justo antes de persistir la
        activación (la compuerta ya la revalidó, pero la cortina puede
        aparecer entre la compuerta y la activación por concurrencia).
        """
        if await self._editorial_curtain(version.document_id) is not None:
            return await self._curtain_version(version)
        document = await self._repo.get_document(version.document_id)
        previous_version_id = document.active_version_id
        # 1. Activar la nueva versión (compuerta ya verificada).
        await self._repo.set_active_version(version.document_id, version.version_id)
        version = await self._repo.update_version_status(version.version_id, "available")
        # 2. Retirar la versión anterior de los índices y de PostgreSQL.
        if previous_version_id is not None and previous_version_id != version.version_id:
            await self._retire_previous_version(previous_version_id)
        return version

    async def _retire_previous_version(self, previous_version_id: str) -> None:
        """Retira una versión anterior de los índices y borra sus chunks.

        Conserva el registro de la versión (historial inmutable) y el
        archivo original (restauración, specs/02 §16).
        """
        for destination in self._destinations.values():
            try:
                await destination.remove_version(previous_version_id)
            except DestinationError:
                # La retirada fallida se registra como op pendiente para
                # reconciliación posterior (specs/02 §50).
                await self._repo.enqueue_pending_op(
                    PendingIndexOp(
                        op_id=_new_id("op"),
                        version_id=previous_version_id,
                        destination=destination.name,
                        operation="delete",
                    )
                )
        await self._repo.delete_chunks(previous_version_id)
        # La versión anterior queda `available` en el historial (inmutable);
        # su retirada de índices es lo que la excluye de consultas normales.
        # No se marca `deleting`: conserva estado para restauración.
        return

    # -- fallo ----------------------------------------------------------------------

    async def _fail(self, version: DocumentVersion, error_code: str) -> DocumentVersion:
        """Marca la versión como fallida con código estable (specs/02 §53).

        La versión activa anterior NO se modifica: sigue siendo la activa y
        sus fragmentos siguen consultables (specs/02 §53, §67).
        """
        await self._repo.mark_version_failed(version.version_id, error_code)
        job = await self._repo.list_jobs_for_version(version.version_id)
        for current_job in job:
            if current_job.status in {"queued", "running"}:
                await self._repo.update_job_status(current_job.job_id, "failed", error_code)
        return await self._repo.get_version(version.version_id)
