"""Endpoint pedagógico POST /api/study (TASK-10, Fase 3; conversaciones en
CHECKPOINTER-02/03).

- Autenticación: misma frontera que el resto de la API (get_current_user,
  token Bearer validado contra la sesión real en PostgreSQL).
- `ViewerContext` derivado de la identidad autenticada en el backend: nunca
  de un rol o usuario enviado por el cliente (Spec 06 §9).
- Conversaciones multi-turno (CHECKPOINTER-02): `conversation_id` opcional
  (UUID) en la entrada; SIEMPRE presente en la salida. El cliente no elige
  `thread_id` ni propietario: la conversación se resuelve y su titularidad
  se verifica en el servidor ANTES de invocar LangGraph, y el runtime
  persiste en el thread derivado de la conversación autorizada.
- Entrada/salida validadas estrictamente con los contratos existentes
  (StudyRequest propio del endpoint; salida StudyResponse de study.py).
- Errores controlados con códigos estables (StudyRuntimeError,
  ConversationError); sin trazas internas, secretos ni datos de otros
  usuarios en las respuestas.
- Límites: longitud máxima de consulta (MAX_REQUEST_CHARS) y
  recursion_limit explícito del grafo (STUDY_RECURSION_LIMIT).
"""

import asyncio
from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from ada_study.api.dependencies import get_current_user
from ada_study.contracts.identity import UserRecord
from ada_study.domain.conversation_service import (
    ConversationError,
    ConversationService,
)
from ada_study.index.retriever import ViewerContext
from ada_study.study import (
    MAX_REQUEST_CHARS,
    StudyRuntimeError,
    run_study,
)

router = APIRouter(prefix="/api")


class StudyRequest(BaseModel):
    """Cuerpo de la consulta de estudio (límite de entrada del endpoint).

    `conversation_id` (opcional): UUID de una conversación propia; None
    inicia una conversación nueva. El cliente NUNCA envía `thread_id` ni
    propietario: cualquier campo extra se ignora y la titularidad la decide
    el servidor (Spec 06 §9).
    """

    user_request: str = Field(min_length=1, max_length=MAX_REQUEST_CHARS)
    conversation_id: str | None = None


@router.post("/study")
async def study(
    request: Request,
    body: StudyRequest,
    user: Annotated[UserRecord, Depends(get_current_user)],
) -> JSONResponse:
    """Responde una consulta de estudio con evidencia verificada del corpus.

    - El `ViewerContext` se deriva SIEMPRE de la identidad autenticada en el
      backend (nunca del cuerpo de la petición).
    - La conversación se resuelve con verificación de titularidad ANTES de
      invocar LangGraph; el runtime persiste en el `thread_id` derivado de
      la conversación autorizada (nunca de un valor del cliente).
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
    # Checkpointer persistente (CHECKPOINTER-01): None = modo efímero.
    checkpointer = getattr(request.app.state.ada, "checkpointer", None)
    conversations_enabled = getattr(request.app.state.settings, "ada_conversations_enabled", False)

    # Resolución de conversación con verificación de titularidad, SIEMPRE
    # ANTES de invocar LangGraph. Solo se activa si el cliente envía
    # conversation_id o si las conversaciones están habilitadas; sin ambos,
    # el endpoint conserva su comportamiento previo (modo efímero y
    # conversation_id=None en la salida, compatibilidad total).
    conversation = None
    if body.conversation_id or conversations_enabled:
        conversations = ConversationService(repository)
        try:
            conversation = await conversations.resolve_conversation(
                user.user_id, body.conversation_id
            )
        except ConversationError as exc:
            status = 404 if exc.error_code == "CONVERSATION_NOT_FOUND" else 422
            return JSONResponse(
                status_code=status,
                content={"error": exc.error_code, "detail": str(exc)},
            )

    try:
        # Lock por conversación (CHECKPOINTER-03, §8): los turnos de la MISMA
        # conversación se serializan (el historial se lee y escribe por thread);
        # conversaciones distintas corren en paralelo. Solo aplica con
        # conversación resuelta: el modo efímero no comparte estado.
        if conversation is not None:
            locks = getattr(request.app.state.ada, "conversation_locks", None)
            if locks is not None:
                lock = locks.setdefault(conversation.thread_id, asyncio.Lock())
                async with lock:
                    response = await _run_turn(
                        repository, body.user_request, viewer,
                        chat_model, checkpointer, conversation, conversations,
                    )
            else:
                response = await _run_turn(
                    repository, body.user_request, viewer,
                    chat_model, checkpointer, conversation, conversations,
                )
        else:
            response = await run_study(
                repository, body.user_request, viewer,
                chat_model=chat_model, checkpointer=checkpointer, thread_id=None,
            )
    except StudyRuntimeError as exc:
        status = 422 if exc.error_code in {
            "STUDY_EMPTY_REQUEST",
            "STUDY_REQUEST_TOO_LONG",
        } else 503
        content = {"error": exc.error_code, "detail": exc.detail}
        # Traza estructurada de la ejecución fallida (AGENT-TRACE-01 §3.7):
        # eventos reales hasta el fallo, para el panel de AGENT-TRACE-02.
        # Sin texto de excepción ni datos sensibles (códigos estables, Spec 06).
        if exc.trace is not None:
            content["trace"] = exc.trace.model_dump(mode="json")
        return JSONResponse(status_code=status, content=content)

    payload = response.model_dump(mode="json")
    if conversation is not None:
        payload["conversation_id"] = conversation.conversation_id
    return JSONResponse(status_code=200, content=payload)


async def _run_turn(
    repository, user_request, viewer, chat_model, checkpointer, conversation, conversations
):
    """Ejecuta un turno completo bajo el lock de la conversación.

    Agrupa la ejecución del grafo y el registro del turno: con el lock por
    conversación, dos peticiones concurrentes sobre la MISMA conversación se
    serializan (nunca se mezclan historiales ni se duplica el registro).
    """
    response = await run_study(
        repository,
        user_request,
        viewer,
        chat_model,
        checkpointer=checkpointer,
        thread_id=conversation.thread_id if conversation else None,
    )
    if conversation is not None:
        await conversations.record_turn(conversation.conversation_id)
    return response
