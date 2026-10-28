"""Endpoint pedagógico POST /api/study (TASK-10, Fase 3).

- Autenticación: misma frontera que el resto de la API (get_current_user,
  token Bearer validado contra la sesión real en PostgreSQL).
- `ViewerContext` derivado de la identidad autenticada en el backend: nunca
  de un rol o usuario enviado por el cliente (Spec 06 §9).
- Entrada/salida validadas estrictamente con los contratos existentes
  (`StudyRequest` propio del endpoint; salida `StudyResponse` de study.py).
- Sin respuestas ficticias ni fuentes inventadas: la evidencia proviene del
  retrieval real sobre el catálogo; sin evidencia, la respuesta reconoce la
  insuficiencia (status="insufficient_evidence" o "limited") y no se consume
  LLM (TASK-11).
- Errores controlados con códigos estables (StudyRuntimeError); sin trazas
  internas, secretos ni datos de otros usuarios en las respuestas.
- Límites: longitud máxima de consulta (MAX_REQUEST_CHARS) y
  recursion_limit explícito del grafo (STUDY_RECURSION_LIMIT).
"""

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ada_study.api.dependencies import get_current_user
from ada_study.contracts.identity import UserRecord
from ada_study.index.retriever import ViewerContext
from ada_study.study import (
    MAX_REQUEST_CHARS,
    StudyRuntimeError,
    run_study,
)

router = APIRouter(prefix="/api")


class StudyRequest(BaseModel):
    """Cuerpo de la consulta de estudio (límite de entrada del endpoint)."""

    user_request: str = Field(min_length=1, max_length=MAX_REQUEST_CHARS)


@router.post("/study")
async def study(
    request: Request,
    body: StudyRequest,
    user: Annotated[UserRecord, Depends(get_current_user)],
) -> JSONResponse:
    """Responde una consulta de estudio con evidencia verificada del corpus.

    - El `ViewerContext` se deriva SIEMPRE de la identidad autenticada en el
      backend (nunca del cuerpo de la petición).
    - El supervisor mantiene el enrutado determinístico (decisión TASK-11);
      el chat model opcional solo redacta la explicación del tutor, con
      fallback determinístico ante fallo del proveedor.
    - Errores controlados con códigos estables; 503 sin detalles internos.
    """
    repository = request.app.state.ada.repository
    viewer = ViewerContext(user_id=user.user_id, role=user.role)
    # Chat model del tutor construido una vez por proceso (TASK-11); None =
    # modo determinístico (default). El supervisor nunca lo recibe.
    chat_model = getattr(request.app.state.ada, "chat_model", None)
    try:
        response = await run_study(repository, body.user_request, viewer, chat_model)
    except StudyRuntimeError as exc:
        status = 422 if exc.error_code in {
            "STUDY_EMPTY_REQUEST",
            "STUDY_REQUEST_TOO_LONG",
        } else 503
        return JSONResponse(
            status_code=status,
            content={"error": exc.error_code, "detail": exc.detail},
        )
    return JSONResponse(status_code=200, content=response.model_dump(mode="json"))
