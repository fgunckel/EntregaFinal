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
from ada_study.db.repository import CatalogRepository, DuplicateDocumentError
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
    ) -> tuple[DocumentRecord, DocumentVersion, IngestionJob]:
        """Registra un documento nuevo con su versión inicial y job de ingesta.

        - Persiste el PDF en el almacén content-addressed (reutiliza el archivo
          físico si la huella ya existe; la integridad queda garantizada por el
          layout y la verificación de `open_pdf`).
        - Dedup: si la huella ya está registrada en un documento lógico
          distinto, lanza DuplicateDocumentError (409 ING_DUPLICATE) sin
          fusionar autoría, permisos ni historial.
        - La versión nace `queued`: no es recuperable hasta completar la
          ingesta (criterio de aceptación de TASK-02).
        """
        sha256 = self._storage.compute_sha256(content)
        existing = await self._repo.find_active_version_by_sha256(sha256)
        if existing is not None:
            raise DuplicateDocumentError(
                existing.document_id, existing.version_id
            )
        _, storage_key = self._storage.save_pdf(content)
        now = _now_iso()
        document = DocumentRecord(
            document_id=_new_id("doc"),
            filename=filename,
            subject=subject,
            active_version_id=None,
            editorial_status="draft",
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
        await self._repo.create_document(document)
        await self._repo.create_version(version)
        await self._repo.create_job(job)
        # Outbox: indexación pendiente por destino (ADR-0001/0004). El worker
        # de TASK-03 las ejecutará; hasta entonces la versión no es available.
        for destination in ("pinecone", "bm25"):
            await self._repo.enqueue_pending_op(
                PendingIndexOp(
                    op_id=_new_id("op"),
                    version_id=version.version_id,
                    destination=destination,
                    operation="index",
                )
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
        - La activación (active_version_id) queda para el worker de TASK-03,
          cuando la nueva versión esté `available`.
        """
        document = await self._repo.get_document(document_id)
        historical = await self._repo.get_version(historical_version_id)
        if historical.document_id != document.document_id:
            from ada_study.db.repository import NotFoundError

            raise NotFoundError("Versión", historical_version_id)
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
        await self._repo.create_version(restored)
        job = IngestionJob(
            job_id=_new_id("job"),
            document_id=document_id,
            version_id=restored.version_id,
            status="queued",
            created_at=now,
            updated_at=now,
            error_code=None,
        )
        await self._repo.create_job(job)
        for destination in ("pinecone", "bm25"):
            await self._repo.enqueue_pending_op(
                PendingIndexOp(
                    op_id=_new_id("op"),
                    version_id=restored.version_id,
                    destination=destination,
                    operation="index",
                )
            )
        return restored

    async def list_document_versions(self, document_id: str) -> list[DocumentVersion]:
        """Historial inmutable de versiones del documento."""
        return await self._repo.list_versions(document_id)

    async def get_document(self, document_id: str) -> DocumentRecord:
        return await self._repo.get_document(document_id)

    async def get_job(self, job_id: str) -> IngestionJob:
        return await self._repo.get_job(job_id)
