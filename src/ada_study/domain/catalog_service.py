"""Servicios de dominio del catálogo (TASK-02).

Orquestan repositorio + almacén + reglas de estado. Implementan las
decisiones aprobadas:
- Dedup entre documentos lógicos distintos: 409 ING_DUPLICATE, sin fusionar
  autoría/permisos/historial (el archivo físico puede reutilizarse).
- Restauración: crear una NUEVA versión basada en el contenido de una
  versión histórica, sin modificar el historial previo.
- Ningún documento se expone como disponible antes de completar la ingesta
  (la versión solo llega a `available` tras verificación de destinos).
"""

import uuid
from datetime import UTC, datetime

from ada_study.contracts.documents import (
    DocumentRecord,
    DocumentVersion,
    IngestionJob,
    PendingIndexOp,
)
from ada_study.contracts.identity import AuditEvent
from ada_study.db.repository import (
    CatalogRepository,
    DuplicateDocumentError,
    InvalidTransitionError,
)
from ada_study.domain.state_rules import is_editorial_transition_valid
from ada_study.storage.pdf_storage import PdfStorage


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class CatalogService:
    """Operaciones de dominio sobre el catálogo documental."""

    def __init__(self, repository: CatalogRepository, storage: PdfStorage) -> None:
        self._repo = repository
        self._storage = storage

    async def register_document(
        self,
        *,
        filename: str,
        content: bytes,
        content_type: str = "application/pdf",
        subject: str | None = None,
        uploaded_by: str | None = None,
        uploader_role: str = "student",
    ) -> tuple[DocumentRecord, DocumentVersion, IngestionJob]:
        """Registra un documento nuevo con su versión inicial y job de ingesta.

        - Persiste el PDF en el almacén content-addressed (reutiliza el archivo
          físico si la huella ya existe; la integridad queda garantizada por el
          layout y la verificación de `open_pdf`).
        - Dedup: si la huella ya está registrada en un documento lógico
          distinto, lanza DuplicateDocumentError (409 ING_DUPLICATE) sin
          fusionar autoría, permisos ni historial. La garantía final es el
          índice único `ux_documents_active_sha256` (atómico ante carreras);
          el pre-check solo mejora el mensaje.
        - ATÓMICO (ADR-0001 rev. 3): documento con claim de huella + versión
          + job + outbox en UNA transacción del repositorio; un fallo en
          cualquier paso no deja residuos.
        - La versión nace `queued`: no es recuperable hasta completar la
          ingesta (criterio de aceptación de TASK-02).
        - Estado editorial inicial (Spec 06 §51): student nace
          `pending_review` (no publica); moderator/admin nacen `draft`.
        """
        sha256 = self._storage.compute_sha256(content)
        existing = await self._repo.find_active_version_by_sha256(sha256)
        if existing is not None:
            raise DuplicateDocumentError(
                existing.document_id, existing.version_id
            )
        _, storage_key = self._storage.save_pdf(content)
        now = _now_iso()
        # Estado editorial inicial (Spec 06 §51): student no publica, su
        # aporte nace pendiente de revisión; moderator/admin nacen draft.
        initial_status = "draft" if uploader_role in ("moderator", "admin") else "pending_review"
        document = DocumentRecord(
            document_id=_new_id("doc"),
            filename=filename,
            subject=subject,
            active_version_id=None,
            editorial_status=initial_status,
            review_status="not_required",
            uploaded_by=uploaded_by,
            created_at=now,
            updated_at=now,
            deleted_at=None,
        )
        version = DocumentVersion(
            version_id=_new_id("ver"),
            document_id=document.document_id,
            version_number=1,
            sha256=sha256,
            storage_key=storage_key,
            content_type=content_type,
            size_bytes=len(content),
            status="queued",
            created_at=now,
            page_count=None,
            error_code=None,
        )
        job = IngestionJob(
            job_id=_new_id("job"),
            document_id=document.document_id,
            version_id=version.version_id,
            status="queued",
            created_at=now,
            updated_at=now,
            error_code=None,
        )
        ops = [
            PendingIndexOp(
                op_id=_new_id("op"),
                version_id=version.version_id,
                destination=destination,
                operation="index",
            )
            for destination in ("pinecone", "bm25")
        ]
        # Una única transacción: documento con claim de huella + versión +
        # job + outbox (ADR-0001 rev. 3). Un fallo revierte TODO.
        await self._repo.create_document_atomic(
            document, sha256=sha256, version=version, job=job, ops=ops
        )
        return document, version, job

    async def restore_version(
        self, document_id: str, historical_version_id: str
    ) -> DocumentVersion:
        """Crea una NUEVA versión basada en una histórica (decisión aprobada).

        - El historial previo no se modifica: la versión histórica conserva su
          estado y su número.
        - La nueva versión obtiene el siguiente version_number, hereda la huella
          y la storage_key (mismo contenido físico), y nace `queued`: debe pasar
          de nuevo por ingesta/indexación antes de activarse.
        - Dedup (ADR-0001 rev. 3): si OTRO documento vivo reclama la huella
          histórica, lanza DuplicateDocumentError (409 ING_DUPLICATE). El
          traslado del claim y el alta de la versión son ATÓMICOS (una única
          transacción con job y outbox): un conflicto no deja versión, job ni
          ops parciales.
        - La activación (active_version_id) queda para el worker de TASK-03,
          cuando la nueva versión esté `available`.
        """
        document = await self._repo.get_document(document_id)
        historical = await self._repo.get_version(historical_version_id)
        if historical.document_id != document.document_id:
            from ada_study.db.repository import NotFoundError

            raise NotFoundError("Versión", historical_version_id)
        # Pre-check de dedup (ADR-0001 rev. 3): la huella histórica puede
        # estar reclamada por otro documento vivo (p. ej. alguien cargó un
        # PDF idéntico después de que esta versión quedara purgada). La
        # garantía final es el índice único dentro de la transacción.
        claimant = await self._repo.find_active_version_by_sha256(historical.sha256)
        if claimant is not None and claimant.document_id != document_id:
            raise DuplicateDocumentError(claimant.document_id, claimant.version_id)
        history = await self._repo.list_versions(document_id)
        next_number = max(v.version_number for v in history) + 1
        now = _now_iso()
        restored = DocumentVersion(
            version_id=_new_id("ver"),
            document_id=document_id,
            version_number=next_number,
            sha256=historical.sha256,
            storage_key=historical.storage_key,
            content_type=historical.content_type,
            size_bytes=historical.size_bytes,
            status="queued",
            created_at=now,
            page_count=None,
            error_code=None,
        )
        job = IngestionJob(
            job_id=_new_id("job"),
            document_id=document_id,
            version_id=restored.version_id,
            status="queued",
            created_at=now,
            updated_at=now,
            error_code=None,
        )
        ops = [
            PendingIndexOp(
                op_id=_new_id("op"),
                version_id=restored.version_id,
                destination=destination,
                operation="index",
            )
            for destination in ("pinecone", "bm25")
        ]
        # Restauración atómica (ADR-0001 rev. 3): claim + versión + job +
        # outbox en UNA transacción. Si otro documento vivo reclama la huella,
        # PostgreSQL revierte TODO y el repo lanza DuplicateDocumentError.
        await self._repo.create_version_with_claim(
            restored,
            claim_sha256=historical.sha256,
            job=job,
            ops=ops,
        )
        return restored

    async def delete_document(self, document_id: str) -> DocumentRecord:
        """Cortina editorial `deleted` + purga física vía outbox (ADR-0004).

        - El borrado es INMEDIATO en la capa de aplicación: el documento pasa a
          `deleted` (transición terminal, ADR-0005) y desaparece del listado y
          de la recuperación al instante (Spec 06 §104).
        - La purga física (vectores Pinecone, chunks BM25, archivo huérfano)
          NO es síncrona: se encola como ops `deindex` en el outbox y el
          worker la ejecuta eventualmente. Este método NO verifica destinos:
          distinguir cortina (aquí) de eliminación física verificada (worker).
        - El archivo físico NO se borra aquí: puede estar referenciado por
          otras versiones (content-addressed); la purga de huérfanos es una
          operación aparte del worker.
        """
        document = await self._repo.get_document(document_id)
        if not is_editorial_transition_valid(document.editorial_status, "deleted"):
            raise InvalidTransitionError(
                "editorial", document.editorial_status, "deleted"
            )
        await self._repo.update_editorial_status(document_id, "deleted")
        # Purga eventual (ADR-0004): desindexar en ambos destinos (Spec 06
        # §104) la versión activa Y toda versión en curso (queued/processing/
        # indexing): aunque aún no esté activa, puede estar parcialmente
        # indexada o indexarse después; la cortina del worker la detiene y
        # estas ops garantizan la purga física.
        version_ids = {document.active_version_id} if document.active_version_id else set()
        for version in await self._repo.list_versions(document_id):
            if version.status in {"queued", "processing", "indexing"}:
                version_ids.add(version.version_id)
        for version_id in version_ids:
            for destination in ("pinecone", "bm25"):
                await self._repo.enqueue_pending_op(
                    PendingIndexOp(
                        op_id=_new_id("op"),
                        version_id=version_id,
                        destination=destination,
                        operation="delete",
                    )
                )
        updated = await self._repo.get_document(document_id)
        return updated

    async def retry_version(self, version_id: str) -> DocumentVersion:
        """Reencola una versión `failed` (idempotente, Spec 02 §29).

        - Solo aplica a versiones `failed`: cualquier otro estado lanza
          InvalidTransitionError (la transición failed -> queued ya la
          valida el repositorio contra `state_rules`).
        - Idempotente: si la versión ya está `queued` (reintento previo),
          devuelve la versión existente sin crear un segundo job.
        - Reactiva las ops fallidas del outbox (conservan su contador de
          intentos, specs/02 §29) y crea un job nuevo para polling.
        """
        version = await self._repo.get_version(version_id)
        if version.status == "queued":
            # Idempotente SOLO si ya existe un job de reintento previo (más de
            # un job para la versión); un `queued` inicial (job único, nunca
            # reintentada) no es reintentable.
            prior_jobs = await self._repo.list_jobs_for_version(version_id)
            if len(prior_jobs) > 1:
                return version
            raise InvalidTransitionError("version", version.status, "queued")
        if version.status != "failed":
            raise InvalidTransitionError("version", version.status, "queued")
        await self._repo.update_version_status(version_id, "queued")
        now = _now_iso()
        job = IngestionJob(
            job_id=_new_id("job"),
            document_id=version.document_id,
            version_id=version_id,
            status="queued",
            created_at=now,
            updated_at=now,
            error_code=None,
        )
        await self._repo.create_job(job)
        await self._repo.reset_failed_ops(version_id)
        return await self._repo.get_version(version_id)

    async def list_document_versions(self, document_id: str) -> list[DocumentVersion]:
        """Historial inmutable de versiones del documento."""
        return await self._repo.list_versions(document_id)

    async def get_document(self, document_id: str) -> DocumentRecord:
        return await self._repo.get_document(document_id)

    async def get_version(self, version_id: str) -> DocumentVersion:
        return await self._repo.get_version(version_id)

    async def submit_for_review(
        self, *, document_id: str, actor_id: str
    ) -> DocumentRecord:
        """Envío del AUTOR a revisión: draft -> pending_review (PUBLISH-FLOW-02).

        - Ruta de AUTORÍA: la propiedad la valida el endpoint a partir de la
          sesión autenticada (nunca de identificadores del cliente); este
          servicio valida estado y versión, sin revalidar permisos.
        - Alcance limitado a `draft`: ninguna otra transición editorial pasa
          por aquí. ADR-0005 se mantiene intacto: `draft -> published` sigue
          siendo inválida (la validación real la aplica
          `update_editorial_status` contra `state_rules`, no se duplica).
        - Requiere versión activa existente y `available` (misma exigencia
          que la recuperación: `is_version_recoverable`).
        - Registra UN evento de auditoría append-only (Spec 06 §81-88) con
          actor, documento, versión, estado anterior y nuevo, fecha y acción.
        """
        document = await self._repo.get_document(document_id)
        if document.editorial_status != "draft":
            raise InvalidTransitionError(
                "editorial", document.editorial_status, "pending_review"
            )
        if document.active_version_id is None:
            raise InvalidTransitionError("version", "missing", "available")
        version = await self._repo.get_version(document.active_version_id)
        if version.status != "available":
            raise InvalidTransitionError("version", version.status, "available")

        previous_status = document.editorial_status
        await self._repo.update_editorial_status(document_id, "pending_review")
        updated = await self._repo.get_document(document_id)
        await self._repo.append_audit_event(
            AuditEvent(
                event_id=_new_id("evt"),
                actor_id=actor_id,
                action="document.submit_for_review",
                entity_type="document",
                entity_id=document_id,
                version_id=document.active_version_id,
                result="ok",
                reason=f"editorial: {previous_status} -> pending_review",
                created_at=_now_iso(),
            )
        )
        return updated

    async def get_job(self, job_id: str) -> IngestionJob:
        return await self._repo.get_job(job_id)
