"""Rutas de auditoría (TASK-07, Fase H3, ADR-0006).

Endpoints:
- GET /api/audit/events → lectura de eventos de auditoría

Reglas aplicadas:
- Lectura restringida: solo moderator/admin (Spec 06 §88, permiso
  `audit:read`); student/trusted_contributor → 403.
- La tabla es append-only: el repositorio NO expone update/delete
  (Spec 06 §88); este router solo lee.
- Sin secretos ni contenido documental en los eventos (Spec 06 §86).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ada_study.api.dependencies import get_current_user
from ada_study.contracts.identity import UserRecord
from ada_study.domain import permissions
from ada_study.domain.identity_service import ForbiddenError

router = APIRouter(prefix="/api/audit")


@router.get("/events")
async def list_audit_events(
    request: Request,
    user: Annotated[UserRecord, Depends(get_current_user)],
    entity_type: str | None = None,
    entity_id: str | None = None,
    limit: int = 100,
):
    """Eventos de auditoría, más recientes primero (lectura restringida).

    - Requiere `audit:read` (moderator/admin, Spec 06 §88).
    - Filtros opcionales por entidad; `limit` acotado a [1, 500].
    - Los eventos se generan en las acciones sensibles (login, cargas,
      decisiones de moderación, borrados, restauraciones, indexación).
    """
    if not permissions.can_read_audit(user.role):
        raise ForbiddenError("audit:read")

    limit = max(1, min(int(limit), 500))
    repository = request.app.state.ada.repository
    events = await repository.list_audit_events(
        entity_type=entity_type,
        entity_id=entity_id,
        limit=limit,
    )
    return JSONResponse(
        status_code=200,
        content={"events": [event.model_dump() for event in events]},
    )
