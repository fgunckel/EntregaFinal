"""Dependencias de la API (TASK-06): autenticación y acceso a servicios.

Frontera de autenticación (Spec 06 §9, §40-42):
- Extrae `Authorization: Bearer <token>` y valida el token REAL contra la
  sesión en PostgreSQL vía `IdentityService.validate_session` (hash SHA-256
  del token, expiración y estado actual de la cuenta en cada petición).
- Sin token, token malformado o sesión inexistente/revocada/expirada → 401
  con código estable (AUTH_SESSION_INVALID / AUTH_SESSION_EXPIRED).
- Cuenta no activa → 403 (AUTH_ACCOUNT_NOT_ACTIVE).
- Sin bypass, sin identidad fija, sin modo desarrollo.
"""

from typing import Annotated

from fastapi import Depends, Request

from ada_study.config import Settings
from ada_study.contracts.identity import UserRecord
from ada_study.domain.identity_service import (
    IdentityError,
    IdentityService,
)


def get_settings_dep(request: Request) -> Settings:
    """Configuración de la app (inyectada en create_app)."""
    return request.app.state.settings


def get_ada_state(request: Request):
    """Servicios compartidos de la app (engine, repositorio, catálogo)."""
    return request.app.state.ada


async def get_current_user(request: Request) -> UserRecord:
    """Frontera de autenticación: valida el token Bearer contra PostgreSQL.

    - Extrae el token de `Authorization: Bearer <token>` (único esquema
      aceptado; sin query params ni cookies).
    - Delega en `IdentityService.validate_session`: busca el hash SHA-256
      del token en la tabla `sessions`, verifica expiración y estado ACTUAL
      de la cuenta (una cuenta suspendida pierde acceso aunque conserve
      sesiones previas, Spec 06 §40).
    - Errores con códigos estables; el handler de la app los mapea a 401/403.
    """
    identity: IdentityService = request.app.state.ada.identity
    header = request.headers.get("authorization", "")
    if not header.startswith("Bearer "):
        raise IdentityError("AUTH_SESSION_INVALID", "Falta el token Bearer")
    token = header[len("Bearer "):].strip()
    if not token:
        raise IdentityError("AUTH_SESSION_INVALID", "Token vacío")
    # Validación real contra la sesión en PostgreSQL (sin bypass).
    return await identity.validate_session(token)


async def require_permission(permission: str):
    """Fábrica de dependencias: exige un permiso al rol del usuario."""

    async def checker(user: Annotated[UserRecord, Depends(get_current_user)]) -> UserRecord:
        from ada_study.domain import permissions

        if not permissions.has_permission(user.role, permission):
            raise ForbiddenError(permission)
        return user

    return checker


# Import diferido para evitar ciclo (identity_service importa permissions)
from ada_study.domain.identity_service import ForbiddenError  # noqa: E402
