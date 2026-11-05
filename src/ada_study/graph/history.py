"""Historial pedagógico (CHECKPOINTER-03, dimensión A).

Decisión de diseño: `messages` NO se usa para el historial conversacional.
El supervisor con LLM escribe en `messages` decisiones internas de enrutado
(`builder.py`, mecánica M7), así que ese canal mezcla contenido interno y no
cumple el requisito de contener exclusivamente alumno + respuestas finales.
El historial pedagógico vive en el canal propio `conversation_history`
(LastValue, sin reducer): lo gestiona el runtime (intake) y el synthesizer
(consolidación), y persiste vía checkpointer por thread.

Cada entrada: {"role": "student" | "ada", "content": str} (+ "topic" para
ADA). Funciones puras, sin I/O, testeables sin LangGraph.
"""

from typing import Any

# Único punto de verdad del formato del historial.
ROLE_STUDENT = "student"
ROLE_ADA = "ada"


def student_turn(content: str) -> dict[str, str]:
    """Entrada de un mensaje del alumno (exacto, sin procesar)."""
    return {"role": ROLE_STUDENT, "content": content}


def ada_turn(content: str, topic: str | None = None) -> dict[str, str]:
    """Entrada de UNA respuesta final de ADA (solo finales publicadas)."""
    entry = {"role": ROLE_ADA, "content": content}
    if topic:
        entry["topic"] = topic
    return entry


def _is_last(history: list[dict], entry: dict) -> bool:
    """True si la última entrada ya es la indicada (guarda anti-duplicado)."""
    last = history[-1] if history else None
    return (
        last is not None
        and last.get("role") == entry["role"]
        and last.get("content") == entry["content"]
    )


def append_student_message(history: list[dict], content: str) -> list[dict]:
    """Incorpora el mensaje del alumno EXACTAMENTE una vez.

    Guarda anti-duplicado: si el turno anterior falló después del intake, el
    checkpoint ya contiene el mensaje del alumno al final; un reintento con la
    misma consulta NO lo duplica. Con un turno exitoso previo la última
    entrada es de ADA, por lo que una pregunta repetida legítima sí se agrega.
    """
    entry = student_turn(content)
    if _is_last(history, entry):
        return list(history)
    return [*history, entry]


def append_ada_message(history: list[dict], content: str, topic: str | None = None) -> list[dict]:
    """Consolida la respuesta final de ADA exactamente una vez."""
    entry = ada_turn(content, topic)
    if _is_last(history, entry):
        return list(history)
    return [*history, entry]


def last_ada_topic(history: list[dict]) -> str | None:
    """Tema declarado en la última respuesta de ADA (para reformular consultas).

    Solo entradas `ada` con "topic": los mensajes del alumno nunca aportan
    el tema (no reutilizar referencias no publicadas del turno anterior).
    """
    for entry in reversed(history):
        if entry.get("role") == ROLE_ADA and entry.get("topic"):
            return str(entry["topic"])
    return None


def history_is_well_formed(history: list[dict]) -> bool:
    """Validación defensiva del historial recuperado del checkpointer.

    Un historial corrupto NO rompe el turno: se descarta (lista vacía) en el
    runtime antes de incorporarlo al estado.
    """
    if not isinstance(history, list):
        return False
    for entry in history:
        if not isinstance(entry, dict):
            return False
        if entry.get("role") not in {ROLE_STUDENT, ROLE_ADA}:
            return False
        if not isinstance(entry.get("content"), str) or not entry["content"]:
            return False
        if entry["role"] == ROLE_STUDENT and "topic" in entry:
            return False
    return True


def sanitize_history(raw: Any) -> list[dict]:
    """Historial recuperado del checkpointer, validado (o vacío si corrupto)."""
    if not history_is_well_formed(raw):
        return []
    return [dict(entry) for entry in raw]
