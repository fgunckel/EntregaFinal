"""Rutas de documentos (TASK-06, Fase E1, ADR-0006).

Endpoints:
- POST /api/documents          → 202 Accepted (encola; no ingesta en línea)
- GET  /api/documents          → listado con visibilidad según rol
- GET  /api/documents/{id}     → detalle con autorización por ID
- GET  /api/ingestion-jobs/{job_id} → polling del job con autorización

Reglas aplicadas:
- Validación síncrona ANTES de registrar/encolar: content-type y tamaño
  configurable. Un rechazo no deja documentos ni trabajos parciales.
- Autorización en cada recurso: conocer un identificador no da acceso
  (Spec 06 §9; regla 4: visibilidad editorial + técnica).
- Códigos de error estables (ADR-0005); estados HTTP coherentes.
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse

from ada_study.api.dependencies import get_current_user
from ada_study.config import Settings
from ada_study.contracts.identity import UserRecord
from ada_study.db.repository import (
    CatalogError,
    InvalidTransitionError,
    NotFoundError,
)
from ada_study.domain.catalog_service import CatalogService
from ada_study.domain.identity_service import ForbiddenError

router = APIRouter(prefix="/api")


def _catalog(request: Request) -> CatalogService:
    return request.app.state.ada.catalog


def _settings(request: Request) -> Settings:
    return request.app.state.settings


def _error_payload(error_code: str, detail: str) -> dict:
    return {"error": error_code, "detail": detail}


# HTTP status por código de error estable (ADR-0005)
ERROR_STATUS = {
    "ING_FILE_EMPTY": 422,
    "ING_FILE_TOO_LARGE": 413,
    "ING_UNSUPPORTED_TYPE": 415,
    "ING_DUPLICATE": 409,
    "ING_INTERNAL": 500,
    "DEL_NOT_FOUND": 404,
    "DEL_CONFLICT": 409,
    "MOD_INVALID_TRANSITION": 409,
    "MOD_NOT_ALLOWED": 403,
}


@router.post("/documents", status_code=202)
async def upload_document(
    request: Request,
    user: Annotated[UserRecord, Depends(get_current_user)],
):
    """Registra un PDF y encola su ingesta (202 + job para polling).

    - Valida content-type y tamaño ANTES de registrar nada: un rechazo no
      deja documentos ni trabajos parciales (condición 4 de TASK-06).
    - Delega en CatalogService.register_document (dedup 409 ING_DUPLICATE,
      outbox por destino). La API no ejecuta la ingesta.
    """
    from ada_study.domain import permissions

    if not permissions.has_permission(user.role, "documents:upload"):
        raise ForbiddenError("documents:upload")

    settings: Settings = request.app.state.settings
    content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
    allowed = [t.strip().lower() for t in settings.ada_allowed_content_types.split(",")]
    if content_type not in allowed:

        return JSONResponse(
            status_code=415,
            content=_error_payload(
                "ING_UNSUPPORTED_TYPE",
                f"Tipo de contenido no soportado: {content_type or 'vacío'}",
            ),
        )

    content_length = request.headers.get("content-length")
    if content_length is not None and content_length.isdigit():
        if int(content_length) > settings.ada_max_upload_bytes:

            return JSONResponse(
                status_code=413,
                content=_error_payload(
                    "ING_FILE_TOO_LARGE",
                    f"El archivo excede el límite de {settings.ada_max_upload_bytes} bytes",
                ),
            )

    body = await request.body()
    if not body:

        return JSONResponse(
            status_code=422,
            content=_error_payload("ING_FILE_EMPTY", "El cuerpo de la petición está vacío"),
        )
    if len(body) > settings.ada_max_upload_bytes:

        return JSONResponse(
            status_code=413,
            content=_error_payload(
                "ING_FILE_TOO_LARGE",
                f"El archivo excede el límite de {settings.ada_max_upload_bytes} bytes",
            ),
        )

    filename = request.headers.get("x-filename", "documento.pdf")
    subject = request.headers.get("x-subject")

    try:
        document, version, job = await _catalog(request).register_document(
            filename=filename,
            content=body,
            content_type=content_type,
            subject=subject,
            uploaded_by=user.user_id,
            uploader_role=user.role,
        )
    except CatalogError as exc:

        return JSONResponse(
            status_code=ERROR_STATUS.get(exc.error_code, 400),
            content=_error_payload(exc.error_code, str(exc)),
        )

    return JSONResponse(
        status_code=202,
        content={
            "document_id": document.document_id,
            "version_id": version.version_id,
            "job_id": job.job_id,
            "job_status": job.status,
        },
    )


@router.get("/documents")
async def list_documents(
    request: Request,
    user: Annotated[UserRecord, Depends(get_current_user)],
):
    """Lista documentos con visibilidad según rol (Spec 06 §106).

    - student/trusted_contributor: solo publicados (biblioteca compartida)
      más los propios (estado de aportes propios, Spec 06 §16).
    - moderator/admin: todos.
    """
    from ada_study.domain import permissions

    if not permissions.has_permission(user.role, "documents:read_shared"):
        raise ForbiddenError("documents:read_shared")

    documents = await request.app.state.ada.repository.list_documents()
    visible = []
    for document in documents:
        if document.editorial_status == "deleted":
            continue  # cortina editorial: un borrado no se lista (Spec 06 §104)
        is_own = document.uploaded_by == user.user_id
        is_shared = document.editorial_status in ("published", "published_pending_review")
        if user.role in ("moderator", "admin") or is_own or is_shared:
            visible.append(document)
    return JSONResponse(
        status_code=200,
        content={"documents": [doc.model_dump() for doc in visible]},
    )


@router.get("/documents/{document_id}")
async def get_document(
    document_id: str,
    request: Request,
    user: Annotated[UserRecord, Depends(get_current_user)],
):
    """Detalle de un documento con autorización por ID (Spec 06 §9).

    Conocer el identificador no da acceso: un documento no publicado solo es
    visible para su autor o para moderator/admin (Spec 06 §106).
    """
    from ada_study.domain import permissions

    if not permissions.has_permission(user.role, "documents:read_shared"):
        raise ForbiddenError("documents:read_shared")

    try:
        document = await _catalog(request).get_document(document_id)
    except NotFoundError as exc:

        return JSONResponse(
            status_code=404,
            content=_error_payload("DEL_NOT_FOUND", str(exc)),
        )

    is_own = document.uploaded_by == user.user_id
    is_shared = document.editorial_status in ("published", "published_pending_review")
    if user.role not in ("moderator", "admin") and not (is_own or is_shared):

        # 404 (no 403): no se revela la existencia del recurso (Spec 06 §41).
        return JSONResponse(
            status_code=404,
            content=_error_payload("DEL_NOT_FOUND", f"Documento {document_id} no existe"),
        )

    if document.editorial_status == "deleted":
        # Cortina editorial: un documento borrado no es recuperable,
        # ni siquiera para su autor o un moderador (Spec 05 esc. 5).
        return JSONResponse(
            status_code=404,
            content=_error_payload("DEL_NOT_FOUND", f"Documento {document_id} no existe"),
        )

    return JSONResponse(status_code=200, content=document.model_dump())


@router.get("/ingestion-jobs/{job_id}")
async def get_ingestion_job(
    job_id: str,
    request: Request,
    user: Annotated[UserRecord, Depends(get_current_user)],
):
    """Consulta el estado de un job de ingesta (polling, ADR-0006).

    Autorización por ID: solo el autor del documento, moderator/admin, o
    quien pueda ver el documento publicado. 404 si no existe o no es
    visible (sin revelar existencia).
    """
    from ada_study.domain import permissions

    if not permissions.has_permission(user.role, "documents:read_own"):
        raise ForbiddenError("documents:read_own")

    repository = request.app.state.ada.repository
    try:
        job = await repository.get_job(job_id)
        document = await repository.get_document(job.document_id)
    except NotFoundError as exc:

        return JSONResponse(
            status_code=404,
            content=_error_payload("DEL_NOT_FOUND", str(exc)),
        )

    is_own = document.uploaded_by == user.user_id
    is_shared = document.editorial_status in ("published", "published_pending_review")
    if user.role not in ("moderator", "admin") and not (is_own or is_shared):

        return JSONResponse(
            status_code=404,
            content=_error_payload("DEL_NOT_FOUND", f"Job {job_id} no existe"),
        )

    return JSONResponse(status_code=200, content=job.model_dump())


# ---------------------------------------------------------------------------
# E2: borrado, reintento y restauración (TASK-07, ADR-0004/0006)
# ---------------------------------------------------------------------------


def _can_see_document(user: UserRecord, document) -> bool:
    """Visibilidad editorial + técnica (Spec 06 §106)."""
    is_own = document.uploaded_by == user.user_id
    is_shared = document.editorial_status in ("published", "published_pending_review")
    return user.role in ("moderator", "admin") or is_own or is_shared


@router.delete("/documents/{document_id}", status_code=202)
async def delete_document(
    document_id: str,
    request: Request,
    user: Annotated[UserRecord, Depends(get_current_user)],
):
    """Borrado con cortina editorial inmediata + purga eventual (ADR-0004).

    - 202 Accepted: la cortina (`deleted`) se aplica en la petición y la
      recuperación excluye el documento al instante (Spec 06 §104); la purga
      física (vectores, chunks, archivo huérfano) queda encolada en el outbox.
    - Autorización: solo el autor o moderator/admin (Spec 06 §106).
    - Idempotente en la práctica: un documento ya `deleted` responde 404
      (la transición deleted -> deleted no es válida, ADR-0005).
    """
    from ada_study.domain import permissions

    if not permissions.has_permission(user.role, "documents:read_own"):
        raise ForbiddenError("documents:read_own")

    repository = request.app.state.ada.repository
    try:
        document = await repository.get_document(document_id)
    except NotFoundError as exc:
        return JSONResponse(
            status_code=404,
            content=_error_payload("DEL_NOT_FOUND", str(exc)),
        )
    if not _can_see_document(user, document):
        # 404 (no 403): no se revela la existencia del recurso (Spec 06 §41).
        return JSONResponse(
            status_code=404,
            content=_error_payload(
                "DEL_NOT_FOUND", f"Documento {document_id} no existe"
            ),
        )
    if document.uploaded_by != user.user_id and user.role not in ("moderator", "admin"):
        return JSONResponse(
            status_code=403,
            content=_error_payload("DEL_NOT_ALLOWED", "Solo el autor puede borrar"),
        )

    try:
        document = await _catalog(request).delete_document(document_id)
    except InvalidTransitionError as exc:
        return JSONResponse(
            status_code=409,
            content=_error_payload("DEL_CONFLICT", str(exc)),
        )
    return JSONResponse(
        status_code=202,
        content={
            "document_id": document.document_id,
            "editorial_status": document.editorial_status,
            "deleted_at": document.deleted_at,
            "purge": "enqueued",
        },
    )


@router.post("/documents/{document_id}/versions/{version_id}/restore", status_code=202)
async def restore_version(
    document_id: str,
    version_id: str,
    request: Request,
    user: Annotated[UserRecord, Depends(get_current_user)],
):
    """Restaura una versión histórica como versión NUEVA (Spec 02 §67).

    - El historial no se modifica: la versión histórica conserva su estado.
    - La nueva versión nace `queued` y debe pasar de nuevo por ingesta e
      indexación antes de activarse (nunca se marca available aquí).
    - Autorización: autor del documento o moderator/admin.
    """
    from ada_study.domain import permissions

    if not permissions.has_permission(user.role, "documents:upload"):
        raise ForbiddenError("documents:upload")

    try:
        document = await _catalog(request).get_document(document_id)
    except NotFoundError as exc:
        return JSONResponse(
            status_code=404,
            content=_error_payload("DEL_NOT_FOUND", str(exc)),
        )
    if not _can_see_document(user, document):
        return JSONResponse(
            status_code=404,
            content=_error_payload("DEL_NOT_FOUND", f"Documento {document_id} no existe"),
        )
    if document.uploaded_by != user.user_id and user.role not in ("moderator", "admin"):
        return JSONResponse(
            status_code=403,
            content=_error_payload("DEL_NOT_ALLOWED", "Solo el autor puede restaurar"),
        )
    if document.editorial_status == "deleted":
        return JSONResponse(
            status_code=409,
            content=_error_payload(
                "DEL_CONFLICT", "No se puede restaurar un documento eliminado"
            ),
        )

    try:
        restored = await _catalog(request).restore_version(document_id, version_id)
    except NotFoundError as exc:
        return JSONResponse(
            status_code=404,
            content=_error_payload("DEL_NOT_FOUND", str(exc)),
        )
    except InvalidTransitionError as exc:
        return JSONResponse(
            status_code=409,
            content=_error_payload("DEL_CONFLICT", str(exc)),
        )

    return JSONResponse(
        status_code=202,
        content={
            "document_id": document_id,
            "restored_version_id": restored.version_id,
            "status": restored.status,
        },
    )


@router.post("/documents/{document_id}/versions/{version_id}/retry", status_code=202)
async def retry_version(
    document_id: str,
    version_id: str,
    request: Request,
    user: Annotated[UserRecord, Depends(get_current_user)],
):
    """Reintento de ingesta de una versión `failed` (idempotente, Spec 02 §29).

    - Solo aplica a versiones `failed`; sobre otro estado responde 409.
    - Idempotente: si la versión ya fue reencolada, responde 200 con la
      versión existente sin duplicar jobs.
    """
    from ada_study.domain import permissions

    if not permissions.has_permission(user.role, "documents:upload"):
        raise ForbiddenError("documents:upload")

    try:
        document = await _catalog(request).get_document(document_id)
        version = await _catalog(request).get_version(version_id)
    except NotFoundError as exc:
        return JSONResponse(
            status_code=404,
            content=_error_payload("DEL_NOT_FOUND", str(exc)),
        )
    if version.document_id != document.document_id:
        return JSONResponse(
            status_code=404,
            content=_error_payload("DEL_NOT_FOUND", f"Versión {version_id} no existe"),
        )
    if not _can_see_document(user, document):
        return JSONResponse(
            status_code=404,
            content=_error_payload("DEL_NOT_FOUND", f"Documento {document_id} no existe"),
        )
    if document.uploaded_by != user.user_id and user.role not in ("moderator", "admin"):
        return JSONResponse(
            status_code=403,
            content=_error_payload("DEL_NOT_ALLOWED", "Solo el autor puede reintentar"),
        )

    already_queued = version.status == "queued"
    try:
        retried = await _catalog(request).retry_version(version_id)
    except InvalidTransitionError as exc:
        return JSONResponse(
            status_code=409,
            content=_error_payload("DEL_CONFLICT", str(exc)),
        )

    return JSONResponse(
        status_code=200 if already_queued else 202,
        content={
            "document_id": document_id,
            "version_id": retried.version_id,
            "status": retried.status,
        },
    )
