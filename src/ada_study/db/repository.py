"""Repositorio del catálogo documental (ADR-0001).

Funciones tipadas de acceso a datos, sin lógica de negocio. Toda transición
de estado pasa por las reglas de `state_rules` antes de persistir.
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import UTC, datetime

from sqlalchemy import delete, insert, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from ada_study.contracts.documents import (
    DocumentRecord,
    DocumentVersion,
    IngestionJob,
    PendingIndexOp,
)
from ada_study.contracts.identity import (
    AuditEvent,
    InvitationRecord,
    SessionRecord,
    UserRecord,
)
from ada_study.db.schema import (
    audit_events,
    document_chunks,
    document_versions,
    documents,
    ingestion_jobs,
    invitations,
    pending_index_ops,
    sessions,
    users,
)
from ada_study.domain.state_rules import (
    is_editorial_transition_valid,
    is_version_transition_valid,
)


class CatalogError(Exception):
    """Error de catálogo con código estable (ADR-0005)."""

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


class DuplicateDocumentError(CatalogError):
    """Huella SHA-256 ya registrada en otro documento lógico (409 ING_DUPLICATE)."""

    def __init__(self, document_id: str, version_id: str | None) -> None:
        self.existing_document_id = document_id
        # `None` cuando el reclamante existe (claim en `documents.sha256`) pero
        # no tiene una versión recuperable con esa huella (p. ej. base legada
        # con claims cruzados tras una migración).
        self.existing_version_id = version_id
        super().__init__(
            "ING_DUPLICATE",
            f"La huella ya está registrada en el documento {document_id}",
        )


class InvalidTransitionError(CatalogError):
    """Transición de estado no permitida (ADR-0005)."""

    def __init__(self, kind: str, current: str, target: str) -> None:
        code = "MOD_INVALID_TRANSITION" if kind == "editorial" else "DEL_CONFLICT"
        super().__init__(code, f"Transición inválida ({kind}): {current} -> {target}")


class NotFoundError(CatalogError):
    """Entidad inexistente (DEL_NOT_FOUND)."""

    def __init__(self, entity: str, entity_id: str) -> None:
        super().__init__("DEL_NOT_FOUND", f"{entity} {entity_id} no existe")


class InvitationAlreadyConsumedError(CatalogError):
    """Invitación ya consumida o revocada (un solo uso, Spec 06 §38)."""

    def __init__(self, invitation_id: str) -> None:
        super().__init__(
            "AUTH_INVITATION_INVALID",
            f"La invitación {invitation_id} ya fue consumida o revocada",
        )


def _iso(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None


# Índice único parcial de deduplicación (ADR-0001 rev. 3)
DEDUP_INDEX_NAME = "ux_documents_active_sha256"


def _is_dedup_violation(exc: IntegrityError) -> bool:
    """True SOLO si la violación corresponde al índice de deduplicación.

    Nunca se enmascaran otras violaciones de integridad (claves primarias,
    `ux_versions_document_number`, checks) como `ING_DUPLICATE`: solo una
    violación del índice parcial `ux_documents_active_sha256` es un
    duplicado de contenido (409 ING_DUPLICATE).
    """
    orig = getattr(exc, "orig", None) or getattr(exc, "__cause__", None)
    constraint = getattr(orig, "constraint_name", None) or getattr(
        orig, "constraint", None
    )
    if constraint is None:
        # Fallback: algunos drivers solo exponen el mensaje del servidor.
        return orig is not None and DEDUP_INDEX_NAME in str(orig)
    return constraint == DEDUP_INDEX_NAME


def _document_from_row(row) -> DocumentRecord:
    return DocumentRecord(
        document_id=row["document_id"],
        filename=row["filename"],
        subject=row["subject"],
        active_version_id=row["active_version_id"],
        editorial_status=row["editorial_status"],
        review_status=row["review_status"],
        uploaded_by=row["uploaded_by"],
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
        deleted_at=_iso(row["deleted_at"]),
    )


def _version_from_row(row) -> DocumentVersion:
    return DocumentVersion(
        version_id=row["version_id"],
        document_id=row["document_id"],
        version_number=row["version_number"],
        sha256=row["sha256"],
        storage_key=row["storage_key"],
        content_type=row["content_type"],
        size_bytes=row["size_bytes"],
        status=row["status"],
        created_at=row["created_at"].isoformat(),
        page_count=row["page_count"],
        error_code=row["error_code"],
    )


def _job_from_row(row) -> IngestionJob:
    return IngestionJob(
        job_id=row["job_id"],
        document_id=row["document_id"],
        version_id=row["version_id"],
        status=row["status"],
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
        error_code=row["error_code"],
    )


def _op_from_row(row) -> PendingIndexOp:
    return PendingIndexOp(
        op_id=row["op_id"],
        version_id=row["version_id"],
        destination=row["destination"],
        operation=row["operation"],
        status=row["status"],
        attempts=row["attempts"],
        last_error_code=row["last_error_code"],
    )


def _user_from_row(row) -> UserRecord:
    return UserRecord(
        user_id=row["user_id"],
        email=row["email"],
        display_name=row["display_name"],
        role=row["role"],
        account_status=row["account_status"],
        password_hash=row["password_hash"],
        created_at=row["created_at"].isoformat(),
        updated_at=row["updated_at"].isoformat(),
    )


def _invitation_from_row(row) -> InvitationRecord:
    return InvitationRecord(
        invitation_id=row["invitation_id"],
        email=row["email"],
        role=row["role"],
        token_hash=row["token_hash"],
        status=row["status"],
        created_by=row["created_by"],
        created_at=row["created_at"].isoformat(),
        expires_at=row["expires_at"].isoformat(),
        consumed_at=_iso(row["consumed_at"]),
        consumed_by=row["consumed_by"],
    )


def _session_from_row(row) -> SessionRecord:
    return SessionRecord(
        session_id=row["session_id"],
        user_id=row["user_id"],
        token_hash=row["token_hash"],
        status=row["status"],
        created_at=row["created_at"].isoformat(),
        expires_at=row["expires_at"].isoformat(),
        revoked_at=_iso(row["revoked_at"]),
    )


def _audit_from_row(row) -> AuditEvent:
    return AuditEvent(
        event_id=row["event_id"],
        actor_id=row["actor_id"],
        action=row["action"],
        entity_type=row["entity_type"],
        entity_id=row["entity_id"],
        version_id=row["version_id"],
        result=row["result"],
        reason=row["reason"],
        created_at=row["created_at"].isoformat(),
    )


class CatalogRepository:
    """Acceso tipado al catálogo en PostgreSQL (SQLAlchemy Core async)."""

    def __init__(self, engine: AsyncEngine) -> None:
        self._engine = engine

    @asynccontextmanager
    async def _connection(self) -> AsyncIterator[AsyncConnection]:
        async with self._engine.begin() as conn:
            yield conn

    # -- documents ---------------------------------------------------------

    async def create_document(self, record: DocumentRecord, *, sha256: str) -> None:
        """Inserta el documento con su claim de huella; falla si ya existe.

        - `sha256` es el claim de dedup (ADR-0001 rev. 3): una huella solo
          puede estar reclamada por UN documento vivo.
        - La violación del índice de dedup se traduce a DuplicateDocumentError
          (409 ING_DUPLICATE); cualquier otra violación de integridad se
          propaga sin traducir.
        """
        now = datetime.now(UTC)
        try:
            async with self._connection() as conn:
                await conn.execute(
                    insert(documents).values(
                        document_id=record.document_id,
                        filename=record.filename,
                        subject=record.subject,
                        active_version_id=record.active_version_id,
                        editorial_status=record.editorial_status,
                        review_status=record.review_status,
                        uploaded_by=record.uploaded_by,
                        sha256=sha256,
                        created_at=now,
                        updated_at=now,
                        deleted_at=None,
                    )
                )
        except IntegrityError as exc:
            if not _is_dedup_violation(exc):
                raise
            await self._raise_duplicate(sha256, exc)

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

        Todo ocurre en UNA única transacción: un fallo en cualquier paso
        produce rollback completo (sin versiones `queued`, jobs ni ops
        huérfanas). El claim (`documents.sha256`) nace con el documento, de
        modo que el dedup es válido desde el registro, no desde la activación.
        La violación de `ux_documents_active_sha256` (otro documento vivo
        reclama la huella, incluso en carrera) se traduce a
        DuplicateDocumentError (409 ING_DUPLICATE).
        """
        now = datetime.now(UTC)
        try:
            async with self._connection() as conn:
                await conn.execute(
                    insert(documents).values(
                        document_id=record.document_id,
                        filename=record.filename,
                        subject=record.subject,
                        active_version_id=record.active_version_id,
                        editorial_status=record.editorial_status,
                        review_status=record.review_status,
                        uploaded_by=record.uploaded_by,
                        sha256=sha256,
                        created_at=now,
                        updated_at=now,
                        deleted_at=None,
                    )
                )
                await self._insert_version(conn, version)
                await self._insert_job(conn, job)
                for op in ops:
                    await self._insert_op(conn, op)
        except IntegrityError as exc:
            if not _is_dedup_violation(exc):
                raise
            await self._raise_duplicate(sha256, exc)

    async def _raise_duplicate(self, sha256: str, exc: IntegrityError) -> None:
        """Re-lanza la violación como ING_DUPLICATE con el reclamante actual.

        La unicidad es del índice parcial: SIEMPRE que la violación sea de
        `ux_documents_active_sha256` se traduce a `DuplicateDocumentError`,
        aunque el reclamante no tenga una versión recuperable con esa huella
        (el claim vive en `documents.sha256`, no en `document_versions`):
        en ese caso `existing_version_id` es `None`. Solo si el reclamante
        desapareció durante la carrera (fue borrado entre la violación y
        esta consulta) se re-lanza la IntegrityError original (la unicidad
        es de PostgreSQL, no del check de aplicación).
        """
        existing = await self.find_active_version_by_sha256(sha256)
        if existing is not None:
            raise DuplicateDocumentError(existing.document_id, existing.version_id) from exc
        claimant = await self._find_active_claimant(sha256)
        if claimant is not None:
            raise DuplicateDocumentError(claimant, None) from exc
        raise exc

    async def _find_active_claimant(self, sha256: str) -> str | None:
        """`document_id` vivo que reclama la huella vía `documents.sha256`.

        A diferencia de `find_active_version_by_sha256` (pre-check de
        conveniencia, exige versión recuperable), este lookup refleja el
        claim puro: es el que garantiza la traducción SIEMPRE que el índice
        de dedup viole en escritura.
        """
        async with self._connection() as conn:
            row = (
                await conn.execute(
                    select(documents.c.document_id)
                    .where(
                        documents.c.sha256 == sha256,
                        documents.c.deleted_at.is_(None),
                    )
                    .limit(1)
                )
            ).first()
        return row[0] if row is not None else None

    async def get_document(self, document_id: str) -> DocumentRecord:
        async with self._connection() as conn:
            row = (
                await conn.execute(
                    select(documents).where(documents.c.document_id == document_id)
                )
            ).mappings().first()
        if row is None:
            raise NotFoundError("Documento", document_id)
        return _document_from_row(row)

    async def list_documents(self) -> list[DocumentRecord]:
        async with self._connection() as conn:
            rows = (
                await conn.execute(select(documents).order_by(documents.c.created_at))
            ).mappings().all()
        return [_document_from_row(row) for row in rows]

    async def update_editorial_status(self, document_id: str, target: str) -> None:
        """Transición editorial validada contra las reglas (ADR-0005).

        La cortina editorial (`withdrawn`/`deleted`) invalida la recuperación
        al instante (Spec 06 §104); la purga física es posterior (ADR-0004).
        """
        current_record = await self.get_document(document_id)
        current = current_record.editorial_status
        if not is_editorial_transition_valid(current, target):
            raise InvalidTransitionError("editorial", current, target)
        now = datetime.now(UTC)
        async with self._connection() as conn:
            await conn.execute(
                update(documents)
                .where(documents.c.document_id == document_id)
                .values(
                    editorial_status=target,
                    updated_at=now,
                    deleted_at=now if target == "deleted" else None,
                )
            )

    async def set_active_version(self, document_id: str, version_id: str) -> None:
        """Apunta active_version_id a una versión existente del documento."""
        async with self._connection() as conn:
            version_row = (
                await conn.execute(
                    select(document_versions.c.version_id).where(
                        document_versions.c.version_id == version_id,
                        document_versions.c.document_id == document_id,
                    )
                )
            ).first()
            if version_row is None:
                raise NotFoundError("Versión", version_id)
            await conn.execute(
                update(documents)
                .where(documents.c.document_id == document_id)
                .values(active_version_id=version_id, updated_at=datetime.now(UTC))
            )

    # -- document_versions ---------------------------------------------------

    async def create_version(self, version: DocumentVersion) -> None:
        """Inserta una versión inmutable (nunca se actualiza su contenido)."""
        async with self._connection() as conn:
            await self._insert_version(conn, version)

    async def _insert_version(self, conn: AsyncConnection, version: DocumentVersion) -> None:
        await conn.execute(
            insert(document_versions).values(
                version_id=version.version_id,
                document_id=version.document_id,
                version_number=version.version_number,
                sha256=version.sha256,
                storage_key=version.storage_key,
                content_type=version.content_type,
                size_bytes=version.size_bytes,
                status=version.status,
                page_count=version.page_count,
                error_code=version.error_code,
                created_at=datetime.now(UTC),
            )
        )

    async def create_version_with_claim(
        self,
        version: DocumentVersion,
        *,
        claim_sha256: str,
        job: IngestionJob,
        ops: list[PendingIndexOp],
    ) -> DocumentVersion:
        """Nueva versión + traslado del claim de huella, en UNA transacción.

        Alta atómica de versión (registro inicial y restauración, ADR-0001
        rev. 3): en la misma transacción se mueve el claim
        (`documents.sha256`), se inserta la versión, el job y las ops del
        outbox. Si OTRO documento vivo ya reclama esa huella, PostgreSQL
        rechaza el UPDATE por `ux_documents_active_sha256` y TODO se revierte
        (sin versión, job ni ops parciales) → DuplicateDocumentError
        (409 ING_DUPLICATE). Actualizar el claim de un documento a su propia
        huella actual no viola el índice (restauración idempotente).
        """
        try:
            async with self._connection() as conn:
                result = await conn.execute(
                    update(documents)
                    .where(
                        documents.c.document_id == version.document_id,
                        documents.c.deleted_at.is_(None),
                    )
                    .values(sha256=claim_sha256, updated_at=datetime.now(UTC))
                )
                if not result.rowcount:
                    raise NotFoundError("Documento", version.document_id)
                await self._insert_version(conn, version)
                await self._insert_job(conn, job)
                for op in ops:
                    await self._insert_op(conn, op)
        except IntegrityError as exc:
            if not _is_dedup_violation(exc):
                raise
            await self._raise_duplicate(claim_sha256, exc)
        return version

    async def get_version(self, version_id: str) -> DocumentVersion:
        async with self._connection() as conn:
            row = (
                await conn.execute(
                    select(document_versions).where(
                        document_versions.c.version_id == version_id
                    )
                )
            ).mappings().first()
        if row is None:
            raise NotFoundError("Versión", version_id)
        return _version_from_row(row)

    async def list_versions(self, document_id: str) -> list[DocumentVersion]:
        """Historial completo del documento, ordenado por número de versión."""
        async with self._connection() as conn:
            rows = (
                await conn.execute(
                    select(document_versions)
                    .where(document_versions.c.document_id == document_id)
                    .order_by(document_versions.c.version_number)
                )
            ).mappings().all()
        return [_version_from_row(row) for row in rows]

    async def find_active_version_by_sha256(self, sha256: str) -> DocumentVersion | None:
        """Busca el reclamante vigente de una huella (dedup, ADR-0001 rev. 3).

        El claim vive en `documents.sha256` y existe mientras el documento
        esté vivo (`deleted_at IS NULL`): desde el registro de la primera
        versión, durante toda la ingesta (incluso con la versión `failed` o
        `queued`) y en estados editoriales intermedios (`withdrawn`,
        `pending_review`). El borrado libera la huella (la fila sale del
        índice parcial).

        Devuelve la versión NO purgada (`status != 'deleting'`) más reciente
        con esa huella del documento reclamante, o None si nadie reclama la
        huella. Pre-check de conveniencia: la garantía final es el índice
        único (ver `create_document_atomic` / `create_version_with_claim`).
        """
        async with self._connection() as conn:
            doc_row = (
                await conn.execute(
                    select(documents.c.document_id)
                    .where(
                        documents.c.sha256 == sha256,
                        documents.c.deleted_at.is_(None),
                    )
                    .limit(1)
                )
            ).first()
            if doc_row is None:
                return None
            version_row = (
                await conn.execute(
                    select(document_versions)
                    .where(
                        document_versions.c.document_id == doc_row[0],
                        document_versions.c.sha256 == sha256,
                        document_versions.c.status != "deleting",
                    )
                    .order_by(document_versions.c.version_number.desc())
                    .limit(1)
                )
            ).mappings().first()
        # Si el documento reclama la huella pero no tiene versión vigente
        # (solo posible en bases heredadas), se devuelve None: la unicidad
        # real la garantiza el índice en la escritura.
        return _version_from_row(version_row) if version_row is not None else None

    async def update_version_status(
        self, version_id: str, target: str
    ) -> DocumentVersion:
        """Transición técnica de versión validada contra las reglas (ADR-0005).

        Devuelve la versión actualizada re-consultada tras el UPDATE: el
        worker propaga el estado por el ciclo (queued -> processing ->
        indexing -> available) y necesita la fila fresca (incluido
        `error_code` reseteado a None por la transición).
        """
        current_version = await self.get_version(version_id)
        current = current_version.status
        if not is_version_transition_valid(current, target):
            raise InvalidTransitionError("version", current, target)
        async with self._connection() as conn:
            await conn.execute(
                update(document_versions)
                .where(document_versions.c.version_id == version_id)
                .values(status=target, error_code=None)
            )
        return await self.get_version(version_id)

    async def mark_version_failed(self, version_id: str, error_code: str) -> None:
        """Marca una versión como fallida con su código estable (ADR-0005)."""
        current_version = await self.get_version(version_id)
        if not is_version_transition_valid(current_version.status, "failed"):
            raise InvalidTransitionError("version", current_version.status, "failed")
        async with self._connection() as conn:
            await conn.execute(
                update(document_versions)
                .where(document_versions.c.version_id == version_id)
                .values(status="failed", error_code=error_code)
            )

    async def set_version_page_count(self, version_id: str, page_count: int) -> None:
        """Registra la cantidad de páginas detectadas en la extracción."""
        async with self._connection() as conn:
            await conn.execute(
                update(document_versions)
                .where(document_versions.c.version_id == version_id)
                .values(page_count=page_count)
            )

    # -- document_chunks -----------------------------------------------------

    async def replace_chunks(self, version_id: str, chunks: list) -> None:
        """Reemplaza los fragmentos de una versión (operación idempotente)."""
        async with self._connection() as conn:
            await conn.execute(
                delete(document_chunks).where(document_chunks.c.version_id == version_id)
            )
            for chunk in chunks:
                await conn.execute(
                    insert(document_chunks).values(
                        chunk_id=chunk.chunk_id,
                        document_id=chunk.document_id,
                        version_id=chunk.version_id,
                        page=chunk.page,
                        chunk_index=chunk.chunk_index,
                        content_hash=chunk.content_hash,
                        text=chunk.text,
                        title=chunk.title,
                        source=chunk.source,
                    )
                )

    async def list_chunks(self, version_id: str) -> list:
        async with self._connection() as conn:
            rows = (
                await conn.execute(
                    select(document_chunks)
                    .where(document_chunks.c.version_id == version_id)
                    .order_by(document_chunks.c.page, document_chunks.c.chunk_index)
                )
            ).mappings().all()
        from ada_study.contracts.documents import ChunkRecord

        return [
            ChunkRecord(
                chunk_id=row["chunk_id"],
                document_id=row["document_id"],
                version_id=row["version_id"],
                page=row["page"],
                chunk_index=row["chunk_index"],
                content_hash=row["content_hash"],
                text=row["text"],
                title=row["title"],
                source=row["source"],
            )
            for row in rows
        ]

    # -- ingestion_jobs ------------------------------------------------------

    async def create_job(self, job: IngestionJob) -> None:
        async with self._connection() as conn:
            await self._insert_job(conn, job)

    async def _insert_job(self, conn: AsyncConnection, job: IngestionJob) -> None:
        now = datetime.now(UTC)
        await conn.execute(
            insert(ingestion_jobs).values(
                job_id=job.job_id,
                document_id=job.document_id,
                version_id=job.version_id,
                status=job.status,
                error_code=job.error_code,
                created_at=now,
                updated_at=now,
            )
        )

    async def get_job(self, job_id: str) -> IngestionJob:
        async with self._connection() as conn:
            row = (
                await conn.execute(
                    select(ingestion_jobs).where(ingestion_jobs.c.job_id == job_id)
                )
            ).mappings().first()
        if row is None:
            raise NotFoundError("Job", job_id)
        return _job_from_row(row)

    async def update_job_status(
        self, job_id: str, status: str, error_code: str | None = None
    ) -> None:
        async with self._connection() as conn:
            await conn.execute(
                update(ingestion_jobs)
                .where(ingestion_jobs.c.job_id == job_id)
                .values(status=status, error_code=error_code, updated_at=datetime.now(UTC))
            )

    # -- pending_index_ops (outbox, ADR-0001/0004) ---------------------------

    async def enqueue_pending_op(self, op: PendingIndexOp) -> None:
        async with self._connection() as conn:
            await self._insert_op(conn, op)

    async def _insert_op(self, conn: AsyncConnection, op: PendingIndexOp) -> None:
        await conn.execute(
            insert(pending_index_ops).values(
                op_id=op.op_id,
                version_id=op.version_id,
                destination=op.destination,
                operation=op.operation,
                status=op.status,
                attempts=op.attempts,
                last_error_code=op.last_error_code,
                created_at=datetime.now(UTC),
            )
        )

    async def claim_pending_ops(self, destination: str, limit: int = 10) -> list[PendingIndexOp]:
        """Devuelve operaciones pendientes de un destino (el worker las procesa)."""
        async with self._connection() as conn:
            rows = (
                await conn.execute(
                    select(pending_index_ops)
                    .where(
                        pending_index_ops.c.destination == destination,
                        pending_index_ops.c.status == "pending",
                    )
                    .order_by(pending_index_ops.c.created_at)
                    .limit(limit)
                )
            ).mappings().all()
        return [_op_from_row(row) for row in rows]

    async def complete_pending_op(self, op_id: str) -> None:
        async with self._connection() as conn:
            await conn.execute(
                update(pending_index_ops)
                .where(pending_index_ops.c.op_id == op_id)
                .values(status="done")
            )

    async def fail_pending_op(self, op_id: str, error_code: str) -> None:
        """Registra el fallo de un intento y deja la op lista para reintento."""
        async with self._connection() as conn:
            await conn.execute(
                update(pending_index_ops)
                .where(pending_index_ops.c.op_id == op_id)
                .values(
                    status="failed",
                    attempts=pending_index_ops.c.attempts + 1,
                    last_error_code=error_code,
                )
            )

    # -- consultas para el worker de ingesta (TASK-03) ------------------------

    async def list_ops_for_version(self, version_id: str) -> list[PendingIndexOp]:
        """Todas las operaciones del outbox de una versión (cualquier estado)."""
        async with self._connection() as conn:
            rows = (
                await conn.execute(
                    select(pending_index_ops)
                    .where(pending_index_ops.c.version_id == version_id)
                    .order_by(pending_index_ops.c.created_at)
                )
            ).mappings().all()
        return [_op_from_row(row) for row in rows]

    async def reset_failed_ops(self, version_id: str) -> int:
        """Reactiva las ops fallidas de una versión para reintento.

        Devuelve la cantidad de ops reactivadas (reintentos seguros: la op
        conserva su contador de intentos, specs/02 §29).
        """
        async with self._connection() as conn:
            result = await conn.execute(
                update(pending_index_ops)
                .where(
                    pending_index_ops.c.version_id == version_id,
                    pending_index_ops.c.status == "failed",
                )
                .values(status="pending")
            )
        return result.rowcount or 0

    async def reset_failed_delete_ops(self, version_id: str) -> int:
        """Reactiva SOLO las ops delete fallidas de una versión.

        Bajo cortina editorial (ADR-0004): las ops index/reconcile fallidas
        permanecen fallidas (recover() no reintroduce contenido tras la
        cortina); las ops delete siguen siendo recuperables (purga
        garantizada). Devuelve la cantidad de ops reactivadas.
        """
        async with self._connection() as conn:
            result = await conn.execute(
                update(pending_index_ops)
                .where(
                    pending_index_ops.c.version_id == version_id,
                    pending_index_ops.c.status == "failed",
                    pending_index_ops.c.operation == "delete",
                )
                .values(status="pending")
            )
        return result.rowcount or 0

    async def list_version_ids_with_pending_ops(self) -> list[str]:
        """Versiones con ops pendientes o fallidas (recuperación del worker).

        Incluye las fallidas: `recover()` las reactiva para reintento
        (specs/02 §29: reintentos seguros y reconciliación).
        """
        async with self._connection() as conn:
            rows = (
                await conn.execute(
                    select(pending_index_ops.c.version_id)
                    .where(pending_index_ops.c.status.in_(["pending", "failed"]))
                    .distinct()
                )
            ).all()
        return [row[0] for row in rows]

    async def list_versions_by_status(self, status: str) -> list[DocumentVersion]:
        """Versiones en un estado técnico dado (p. ej. `indexing` huérfanas)."""
        async with self._connection() as conn:
            rows = (
                await conn.execute(
                    select(document_versions)
                    .where(document_versions.c.status == status)
                    .order_by(document_versions.c.created_at)
                )
            ).mappings().all()
        return [_version_from_row(row) for row in rows]

    async def list_jobs_for_version(self, version_id: str) -> list[IngestionJob]:
        """Jobs de ingesta asociados a una versión (cualquier estado)."""
        async with self._connection() as conn:
            rows = (
                await conn.execute(
                    select(ingestion_jobs)
                    .where(ingestion_jobs.c.version_id == version_id)
                    .order_by(ingestion_jobs.c.created_at)
                )
            ).mappings().all()
        return [_job_from_row(row) for row in rows]

    async def delete_chunks(self, version_id: str) -> int:
        """Elimina los chunks de una versión (retirada tras activación)."""
        async with self._connection() as conn:
            result = await conn.execute(
                delete(document_chunks).where(document_chunks.c.version_id == version_id)
            )
        return result.rowcount or 0

    # -- users (TASK-04, Spec 06 §35-44) ------------------------------------

    async def create_user(self, record: UserRecord) -> None:
        """Inserta la cuenta; falla si el user_id o el email ya existen."""
        now = datetime.now(UTC)
        async with self._connection() as conn:
            await conn.execute(
                insert(users).values(
                    user_id=record.user_id,
                    email=record.email,
                    display_name=record.display_name,
                    role=record.role,
                    account_status=record.account_status,
                    password_hash=record.password_hash,
                    created_at=now,
                    updated_at=now,
                )
            )

    async def get_user(self, user_id: str) -> UserRecord:
        """Devuelve la cuenta o lanza NotFoundError."""
        async with self._connection() as conn:
            row = (
                await conn.execute(select(users).where(users.c.user_id == user_id))
            ).mappings().first()
        if row is None:
            raise NotFoundError("user", user_id)
        return _user_from_row(row)

    async def get_user_by_email(self, email: str) -> UserRecord | None:
        """Cuenta por email exacto, o None (el servicio decide el error genérico)."""
        async with self._connection() as conn:
            row = (
                await conn.execute(select(users).where(users.c.email == email))
            ).mappings().first()
        return _user_from_row(row) if row is not None else None

    async def update_account_status(self, user_id: str, status: str) -> UserRecord:
        """Actualiza el estado de la cuenta (activación, suspensión, baja)."""
        now = datetime.now(UTC)
        async with self._connection() as conn:
            result = await conn.execute(
                update(users)
                .where(users.c.user_id == user_id)
                .values(account_status=status, updated_at=now)
            )
            if not result.rowcount:
                raise NotFoundError("user", user_id)
            row = (
                await conn.execute(select(users).where(users.c.user_id == user_id))
            ).mappings().one()
        return _user_from_row(row)

    async def update_user_role(self, user_id: str, role: str) -> UserRecord:
        """Cambia el rol de la cuenta (solo admin; se audita en el servicio)."""
        now = datetime.now(UTC)
        async with self._connection() as conn:
            result = await conn.execute(
                update(users)
                .where(users.c.user_id == user_id)
                .values(role=role, updated_at=now)
            )
            if not result.rowcount:
                raise NotFoundError("user", user_id)
            row = (
                await conn.execute(select(users).where(users.c.user_id == user_id))
            ).mappings().one()
        return _user_from_row(row)

    async def set_password_hash(self, user_id: str, password_hash: str) -> None:
        """Guarda el hash PBKDF2 de la contraseña (activación de cuenta)."""
        now = datetime.now(UTC)
        async with self._connection() as conn:
            await conn.execute(
                update(users)
                .where(users.c.user_id == user_id)
                .values(password_hash=password_hash, updated_at=now)
            )

    # -- invitations (Spec 06 §38-39) ----------------------------------------

    async def create_invitation(self, record: InvitationRecord) -> None:
        """Inserta la invitación pendiente con su token hasheado."""
        now = datetime.now(UTC)
        async with self._connection() as conn:
            await conn.execute(
                insert(invitations).values(
                    invitation_id=record.invitation_id,
                    email=record.email,
                    role=record.role,
                    token_hash=record.token_hash,
                    status="pending",
                    created_by=record.created_by,
                    created_at=now,
                    expires_at=datetime.fromisoformat(record.expires_at),
                )
            )

    async def get_invitation_by_token_hash(self, token_hash: str) -> InvitationRecord | None:
        """Invitación por hash de token, o None si no existe."""
        async with self._connection() as conn:
            row = (
                await conn.execute(
                    select(invitations).where(invitations.c.token_hash == token_hash)
                )
            ).mappings().first()
        return _invitation_from_row(row) if row is not None else None

    async def consume_invitation(
        self, invitation_id: str, consumed_by: str
    ) -> InvitationRecord:
        """Consume la invitación de forma ATÓMICA (Spec 06 §38: un solo uso).

        El UPDATE condiciona a `status = 'pending'`: si dos transacciones
        intentan consumirla a la vez, solo una afecta filas y gana; la otra
        lanza InvitationAlreadyConsumedError.
        """
        now = datetime.now(UTC)
        async with self._connection() as conn:
            result = await conn.execute(
                update(invitations)
                .where(
                    invitations.c.invitation_id == invitation_id,
                    invitations.c.status == "pending",
                )
                .values(status="consumed", consumed_at=now, consumed_by=consumed_by)
            )
            if not result.rowcount:
                raise InvitationAlreadyConsumedError(invitation_id)
            row = (
                await conn.execute(
                    select(invitations).where(invitations.c.invitation_id == invitation_id)
                )
            ).mappings().one()
        return _invitation_from_row(row)

    async def revoke_invitation(self, invitation_id: str) -> None:
        """Revoca una invitación pendiente (solo admin)."""
        async with self._connection() as conn:
            result = await conn.execute(
                update(invitations)
                .where(
                    invitations.c.invitation_id == invitation_id,
                    invitations.c.status == "pending",
                )
                .values(status="revoked")
            )
            if not result.rowcount:
                raise NotFoundError("invitation", invitation_id)

    # -- sessions (Spec 06 §42) ----------------------------------------------

    async def create_session(self, record: SessionRecord) -> None:
        """Inserta la sesión activa con el hash del token opaco."""
        async with self._connection() as conn:
            await conn.execute(
                insert(sessions).values(
                    session_id=record.session_id,
                    user_id=record.user_id,
                    token_hash=record.token_hash,
                    status="active",
                    created_at=datetime.now(UTC),
                    expires_at=datetime.fromisoformat(record.expires_at),
                )
            )

    async def get_session_by_token_hash(self, token_hash: str) -> SessionRecord | None:
        """Sesión activa por hash de token, o None si no existe."""
        async with self._connection() as conn:
            row = (
                await conn.execute(
                    select(sessions).where(sessions.c.token_hash == token_hash)
                )
            ).mappings().first()
        return _session_from_row(row) if row is not None else None

    async def revoke_session(self, session_id: str) -> None:
        """Revoca la sesión (logout o revocación administrativa)."""
        now = datetime.now(UTC)
        async with self._connection() as conn:
            result = await conn.execute(
                update(sessions)
                .where(
                    sessions.c.session_id == session_id,
                    sessions.c.status == "active",
                )
                .values(status="revoked", revoked_at=now)
            )
            if not result.rowcount:
                raise NotFoundError("session", session_id)

    async def revoke_all_sessions(self, user_id: str) -> int:
        """Revoca todas las sesiones activas de una cuenta (suspensión/baja)."""
        now = datetime.now(UTC)
        async with self._connection() as conn:
            result = await conn.execute(
                update(sessions)
                .where(sessions.c.user_id == user_id, sessions.c.status == "active")
                .values(status="revoked", revoked_at=now)
            )
        return result.rowcount or 0

    # -- audit_events (Spec 06 §81-88, append-only) ---------------------------
    # Deliberadamente NO existen métodos update/delete para esta tabla:
    # la única vía de escritura desde la aplicación es `append_audit_event`.

    async def append_audit_event(self, event: AuditEvent) -> None:
        """Inserta un evento de auditoría (append-only, Spec 06 §88)."""
        async with self._connection() as conn:
            await conn.execute(
                insert(audit_events).values(
                    event_id=event.event_id,
                    actor_id=event.actor_id,
                    action=event.action,
                    entity_type=event.entity_type,
                    entity_id=event.entity_id,
                    version_id=event.version_id,
                    result=event.result,
                    reason=event.reason,
                    created_at=datetime.now(UTC),
                )
            )

    async def list_audit_events(
        self,
        *,
        entity_type: str | None = None,
        entity_id: str | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """Eventos de auditoría, más recientes primero (lectura restringida)."""
        query = select(audit_events).order_by(audit_events.c.created_at.desc()).limit(limit)
        if entity_type is not None and entity_id is not None:
            query = query.where(
                audit_events.c.entity_type == entity_type,
                audit_events.c.entity_id == entity_id,
            )
        async with self._connection() as conn:
            rows = (await conn.execute(query)).mappings().all()
        return [_audit_from_row(row) for row in rows]
