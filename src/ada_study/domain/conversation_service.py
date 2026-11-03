"""Servicio de conversaciones multi-turno (CHECKPOINTER-02).

- El `conversation_id` lo genera SIEMPRE el servidor (UUID4); una petición
  sin `conversation_id` inicia una conversación nueva.
- La titularidad se verifica ANTES de invocar LangGraph: una conversación
  ajena devuelve el MISMO código que una inexistente (404 CONVERSATION_NOT_FOUND,
  sin filtrar existencia).
- El `thread_id` del checkpointer se deriva determinísticamente del
  `conversation_id` autorizado: estable entre procesos y reinicios.
- `record_turn` incrementa `turn_count` y actualiza `last_turn_at`.
"""

import uuid
from datetime import UTC, datetime

from ada_study.contracts.conversations import ConversationRecord

# Códigos de error estables (ADR-0005). CONVERSATION_NOT_FOUND cubre tanto la
# conversación inexistente como la ajena (no se filtra existencia).
CONVERSATION_NOT_FOUND = "CONVERSATION_NOT_FOUND"
CONVERSATION_INVALID_ID = "CONVERSATION_INVALID_ID"


class ConversationError(Exception):
    """Fallo controlado de conversaciones (código estable, sin trazas)."""

    def __init__(self, error_code: str, detail: str) -> None:
        self.error_code = error_code
        super().__init__(detail)


def thread_id_for(conversation_id: str) -> str:
    """`thread_id` estable derivado del `conversation_id` autorizado.

    Determinístico: el mismo conversation_id SIEMPRE mapea al mismo thread
    del checkpointer; conversaciones distintas nunca comparten thread.
    """
    return f"conv-{conversation_id}"


class ConversationService:
    """Resolución de conversaciones con verificación de titularidad."""

    def __init__(self, repository) -> None:
        self._repository = repository

    async def resolve_conversation(
        self, user_id: str, conversation_id: str | None
    ) -> ConversationRecord:
        """Devuelve la conversación propia a reutilizar, o crea una nueva.

        - None → nueva conversación (UUID y thread generados aquí).
        - UUID existente y propio → se reutiliza (mismo thread).
        - UUID inexistente o AJENO → CONVERSATION_NOT_FOUND (404; el código
          es idéntico en ambos casos: nunca se revela la existencia).
        - UUID malformado → CONVERSATION_INVALID_ID (422).
        """
        if conversation_id is None or not conversation_id.strip():
            return await self._create(user_id)
        try:
            cid = str(uuid.UUID(conversation_id.strip()))
        except (ValueError, AttributeError) as exc:
            raise ConversationError(
                "CONVERSATION_INVALID_ID",
                "El conversation_id debe ser un UUID válido.",
            ) from exc
        record = await self._repository.get_conversation(cid)
        if record is None or record.user_id != user_id:
            # Mismo código para inexistente y ajena (aislamiento, Spec 06 §9).
            raise ConversationError(
                "CONVERSATION_NOT_FOUND",
                "La conversación no existe o no pertenece al usuario.",
            )
        return record

    async def record_turn(self, conversation_id: str) -> None:
        """Registra el turno realizado (contador y marca de tiempo)."""
        await self._repository.record_conversation_turn(conversation_id)

    async def _create(self, user_id: str) -> ConversationRecord:
        now = datetime.now(UTC).isoformat()
        cid = str(uuid.uuid4())
        record = ConversationRecord(
            conversation_id=cid,
            user_id=user_id,
            thread_id=thread_id_for(cid),
            created_at=now,
            last_turn_at=now,
            turn_count=0,
        )
        await self._repository.create_conversation(record)
        return record
