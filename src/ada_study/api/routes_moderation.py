"""Rutas de moderación (TASK-07, Fase H2, ADR-0006).

Endpoints:
- POST /api/moderation/documents/{id}/decide → decisión editorial
- GET  /api/moderation/queue                 → cola de revisión pendiente

Reglas aplicadas:
- Autorización en backend en cada endpoint (Spec 06 §9, §28): solo
  moderator/admin deciden y ven la cola; student/trusted_contributor → 403.
- Motivo obligatorio en reject/withdraw (Spec 06 §102): lo valida el
  servicio de dominio `ModerationService`; aquí se mapea a HTTP.
- El autor ve el motivo de la decisión sobre sus aportes (Spec 06 §16):
  el motivo queda en la auditoría y es legible vía detalle del documento.
- 404 (no 403) ante recursos ajenos: no se revela existencia (Spec 06 §41).
"""

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ada_study.api.dependencies import get_current_user
from ada_study.contracts.identity import UserRecord
from ada_study.db.repository import InvalidTransitionError, NotFoundError
from ada_study.domain.identity_service import ForbiddenError
from ada_study.domain.moderation_service import (
    ModerationError,
    ModerationService,
)

router = APIRouter(prefix="/api/moderation")

# HTTP status por código de error de moderación (ADR-0005)
MODERATION_ERROR_STATUS = {
    "MOD_INVALID_ACTION": 422,
    "MOD_REASON_REQUIRED": 422,
    "MOD_NOT_ALLOWED": 403,
    "MOD_INVALID_TRANSITION": 409,
}


class DecideRequest(BaseModel):
    """Cuerpo de la decisión de moderación."""

    action: Literal["approve", "reject", "withdraw", "request_changes"]
    reason: str | None = None


def _error_payload(error_code: str, detail: str) -> dict:
    return {"error": error_code, "detail": detail}


@router.post("/documents/{document_id}/decide")
async def decide(
    document_id: str,
    body: DecideRequest,
    request: Request,
    user: Annotated[UserRecord, Depends(get_current_user)],
):
    """Aplica una decisión editorial (approve/reject/withdraw/request_changes).

    - Delega en `ModerationService.decide`: autorización en backend, transición
      validada con `state_rules`, motivo obligatorio en reject/withdraw,
      desindexación encolada en retiro y auditoría append-only (Spec 06 §102).
    """
    repository = request.app.state.ada.repository
    try:
        document = await ModerationService(repository).decide(
            actor=user,
            document_id=document_id,
            action=body.action,
            reason=body.reason,
        )
    except ForbiddenError:
        raise
    except NotFoundError as exc:
        return JSONResponse(
            status_code=404,
            content=_error_payload("DEL_NOT_FOUND", str(exc)),
        )
    except InvalidTransitionError as exc:
        return JSONResponse(
            status_code=409,
            content=_error_payload("MOD_INVALID_TRANSITION", str(exc)),
        )
    except ModerationError as exc:
        return JSONResponse(
            status_code=MODERATION_ERROR_STATUS.get(exc.error_code, 400),
            content=_error_payload(exc.error_code, str(exc)),
        )
    return JSONResponse(
        status_code=200,
        content={
            "document_id": document.document_id,
            "editorial_status": document.editorial_status,
            "action": body.action,
        },
    )


@router.get("/queue")
async def moderation_queue(
    request: Request,
    user: Annotated[UserRecord, Depends(get_current_user)],
):
    """Cola de revisión: documentos pendientes de decisión editorial.

    - Requiere acceso de moderación: `permissions.can_moderate` →
      moderator/admin (Spec 06 §28). student/trusted_contributor → 403.
    - Incluye `pending_review` (revisión previa) y `published_pending_review`
      (revisión posterior de trusted_contributor, Spec 06 §21-22).
    """
    from ada_study.domain import permissions

    if not permissions.can_moderate(user.role):
        raise ForbiddenError("moderation:queue")

    repository = request.app.state.ada.repository
    documents = await repository.list_documents()
    queue = [
        doc
        for doc in documents
        if doc.editorial_status in ("pending_review", "published_pending_review")
    ]
    return JSONResponse(
        status_code=200,
        content={"queue": [doc.model_dump() for doc in queue]},
    )
