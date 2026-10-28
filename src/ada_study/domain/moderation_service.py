"""Servicio de moderación (TASK-04, Spec 06 §46-55, §99-104).

- Aprobación, rechazo, solicitud de cambios y retiro, reutilizando
  `state_rules` (transiciones válidas) y `permissions` (autorización).
- Motivos obligatorios en rechazos y retiradas (Spec 06 §102).
- Toda decisión se registra en la auditoría append-only.
- Contenido no publicado NO es recuperable: la regla de visibilidad
  (`is_version_recoverable`) ya excluye pending_review/rejected/withdrawn
  de la recuperación, y el retiro encola ops de desindexación para retirar
  los vectores residuales de los índices (Spec 06 §104).
"""

import uuid
from datetime import UTC, datetime

from ada_study.contracts.documents import DocumentRecord, PendingIndexOp
from ada_study.contracts.identity import AuditEvent, UserRecord
from ada_study.db.repository import CatalogRepository, InvalidTransitionError
from ada_study.domain import permissions
from ada_study.domain.identity_service import ForbiddenError, IdentityService
from ada_study.domain.state_rules import is_editorial_transition_valid


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class ModerationError(Exception):
    """Error de moderación con código estable (ADR-0005)."""

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


class ModerationReasonRequiredError(ModerationError):
    """Rechazo/retiro sin motivo (Spec 06 §102)."""

    def __init__(self) -> None:
        super().__init__("MOD_REASON_REQUIRED", "La acción requiere motivo")


class ModerationService:
    """Decisiones de moderación sobre documentos (Spec 06 §99-104)."""

    def __init__(self, repository: CatalogRepository) -> None:
        self._repo = repository
        self._identity = IdentityService(repository)

    async def _audit(
        self,
        *,
        actor_id: str,
        action: str,
        document: DocumentRecord,
        result: str = "ok",
        reason: str | None = None,
    ) -> None:
        await self._repo.append_audit_event(
            AuditEvent(
                event_id=f"evt_{uuid.uuid4().hex[:12]}",
                actor_id=actor_id,
                action=action,
                entity_type="document",
                entity_id=document.document_id,
                version_id=document.active_version_id,
                result=result,
                reason=reason,
                created_at=_now_iso(),
            )
        )

    async def _authorize(self, actor: UserRecord, action: str) -> None:
        """Autoriza la acción de moderación en backend (Spec 06 §9)."""
        if not permissions.can_moderate(actor.role):
            raise ForbiddenError("moderation:decide")

    async def decide(
        self,
        *,
        actor: UserRecord,
        document_id: str,
        action: str,
        reason: str | None = None,
    ) -> DocumentRecord:
        """Aplica una decisión de moderación (approve/reject/withdraw/request_changes).

        - Autorización en backend: solo moderator/admin (Spec 06 §28).
        - Transición validada con `state_rules` antes de persistir.
        - Motivo obligatorio en rechazo y retiro (Spec 06 §102).
        - Retiro (withdraw) encola ops de desindexación: el contenido sale
          de la recuperación aunque queden vectores residuales (Spec 06 §104).
        - La decisión queda en la auditoría con su motivo (Spec 06 §102).
        """
        await self._authorize(actor, action)
        if action not in permissions.MODERATION_ACTIONS:
            raise ModerationError("MOD_INVALID_ACTION", f"Acción desconocida: {action}")
        if permissions.requires_reason(action) and not (reason and reason.strip()):
            raise ModerationReasonRequiredError()
        document = await self._repo.get_document(document_id)
        target = permissions.MODERATION_ACTIONS[action]
        if not is_editorial_transition_valid(document.editorial_status, target):
            raise InvalidTransitionError(
                "editorial", document.editorial_status, target
            )
        await self._repo.update_editorial_status(document_id, target)
        updated = await self._repo.get_document(document_id)
        if action == "withdraw":
            # Desindexación inmediata (Spec 06 §104): ops por destino para
            # retirar los vectores/chunks del contenido retirado. Igual que
            # en el borrado (ADR-0004): se cubre la versión activa Y toda
            # versión en curso (queued/processing/indexing), que podría
            # indexarse después; la cortina del worker la detiene y estas
            # ops garantizan la purga física.
            version_ids = (
                {document.active_version_id} if document.active_version_id else set()
            )
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
        await self._audit(
            actor_id=actor.user_id,
            action=f"moderation.{action}",
            document=updated,
            reason=reason,
        )
        return updated
