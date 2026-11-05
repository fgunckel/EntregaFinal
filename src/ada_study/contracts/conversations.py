"""Contratos de conversaciones multi-turno (CHECKPOINTER-02, ADR-0007).

Una conversación agrupa los turnos de estudio de un alumno bajo un
`conversation_id` (UUID generado SIEMPRE por el servidor) y un `thread_id`
determinístico del checkpointer. El cliente nunca elige `thread_id` ni
propietario: la titularidad se verifica en el backend (Spec 06 §9).
"""

from ada_study.strict_model import StrictModel


class ConversationRecord(StrictModel):
    """Fila de la tabla `conversations` (titularidad y trazabilidad)."""

    conversation_id: str
    user_id: str
    thread_id: str
    created_at: str
    last_turn_at: str
    turn_count: int = 0
