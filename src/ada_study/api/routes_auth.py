"""Rutas de autenticación HTTP (TASK-08, B1) y perfil (B3).

- POST /api/auth/login: delega en IdentityService.login (errores GENÉRICOS
  sin enumeración de correos, Spec 06 §41). La API no toca hashes ni
  contraseñas: reutiliza el servicio existente.
- POST /api/auth/logout: revoca la sesión del token Bearer actual.
- GET /api/me: perfil del usuario resuelto por la frontera de
  autenticación (get_current_user); nunca se confía en un rol enviado por
  el cliente.
- Sin registro de tokens ni contraseñas en logs: la auditoría del
  servicio registra solo el evento con actor y resultado (Spec 06 §42).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ada_study.api.dependencies import get_current_user
from ada_study.contracts.identity import UserRecord
from ada_study.domain.identity_service import IdentityError

router = APIRouter(prefix="/api")


class LoginRequest(BaseModel):
    """Cuerpo del login (credenciales; nunca se registran)."""

    email: str
    password: str


@router.post("/auth/login")
async def login(request: Request, body: LoginRequest) -> JSONResponse:
    """Autentica credenciales y devuelve el token opaco de la sesión.

    - Errores GENÉRICOS (Spec 06 §41): correo inexistente, contraseña
      incorrecta y cuenta no activa producen códigos estables sin revelar
      si el correo existe.
    - El token opaco viaja en el cuerpo (no en cookies): el cliente lo
      envía como Bearer en cada llamada posterior.
    """
    identity = request.app.state.ada.identity
    try:
        token = await identity.login(email=body.email, password=body.password)
    except IdentityError:
        # El handler global de la app mapea a 401/403 con código estable.
        raise
    return JSONResponse(status_code=200, content={"token": token})


@router.post("/auth/logout", status_code=204)
async def logout(
    request: Request,
    user: Annotated[UserRecord, Depends(get_current_user)],
) -> JSONResponse:
    """Revoca la sesión del token Bearer actual (logout real)."""
    header = request.headers.get("authorization", "")
    token = header[len("Bearer "):].strip()
    await request.app.state.ada.identity.logout(actor=user, token=token)
    return JSONResponse(status_code=204, content=None)


@router.get("/me")
async def get_me(
    user: Annotated[UserRecord, Depends(get_current_user)],
) -> JSONResponse:
    """Perfil del usuario autenticado (B3).

    La identidad proviene EXCLUSIVAMENTE de la sesión validada por el
    backend (dependencies.get_current_user); nunca de campos del cliente.
    El hash de contraseña NUNCA se expone (Spec 06 §35): se excluye del
    payload aunque el contrato lo contenga internamente.
    """
    payload = user.model_dump()
    payload.pop("password_hash", None)
    return JSONResponse(status_code=200, content=payload)
