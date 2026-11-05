"""Helpers de trazabilidad estructurada (AGENT-TRACE-01).

Mecánica: cada nodo emite eventos con datos REALES de su ejecución
(tiempos medidos con `perf_counter`, decisiones que efectivamente tomó,
resultados que efectivamente produjo). Los eventos viajan en el canal
`trace_events` del estado de LangGraph; el runtime (`study/runtime.py`)
los colecta con `astream(stream_mode="updates")`, de modo que sobreviven
incluso si un paso posterior falla, y los adjunta a `StudyResponse.trace`.

Reglas:
- Solo se registra lo que REALMENTE sucedió (nada simulado ni decorativo).
- `details` nunca contiene contenido de documentos, prompts completos,
  claves ni texto de excepción (solo identificadores, cantidades,
  duraciones y tipos de error: Spec 03 §46, Spec 06).
- Los nodos emiten sus decisiones vía `new_event` y las devuelven en el
  update; el wrapper `traced_node` agrega `node_start`/`node_end` con
  duración real a cada ejecución (incluyendo reintentos).
"""

import asyncio
import functools
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from ada_study.contracts.trace import TraceEvent

logger = logging.getLogger(__name__)

# Canal del estado de LangGraph que acumula los eventos del turno.
TRACE_EVENTS_KEY = "trace_events"


def utcnow() -> datetime:
    """Timestamp UTC con zona explícita (serializable a ISO por Pydantic)."""
    return datetime.now(UTC)


def new_event(
    node: str,
    event_type: str,
    summary: str,
    *,
    status: str = "ok",
    details: dict[str, Any] | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    duration_ms: float | None = None,
    error: str | None = None,
) -> dict:
    """Construye un evento trazable como dict (validado por contrato).

    Se devuelve como dict para viajar por el canal del estado de LangGraph
    (updates serializables); el runtime lo valida con `TraceEvent`.
    """
    event = TraceEvent(
        event_id=uuid4().hex,
        node=node,
        event_type=event_type,  # type: ignore[arg-type]
        status=status,  # type: ignore[arg-type]
        summary=summary,
        details=details or {},
        started_at=started_at or utcnow(),
        completed_at=completed_at,
        duration_ms=duration_ms,
        error=error,
    )
    return event.model_dump()


def _events_from_state(state: dict) -> list[dict]:
    """Eventos acumulados del turno (lista nueva, nunca muta el estado)."""
    return list(state.get(TRACE_EVENTS_KEY) or [])


def _node_end_event(
    name: str, started_at: datetime, duration_ms: float
) -> dict:
    return new_event(
        name,
        "node_end",
        f"{name} completado.",
        started_at=started_at,
        completed_at=utcnow(),
        duration_ms=round(duration_ms, 3),
    )


def traced_node(name: str, fn: Callable) -> Callable:
    """Envuelve un nodo para registrar su ejecución real.

    Agrega a cada update un evento `node_start` (con el instante real de
    inicio), los eventos de decisión/resultado que el nodo haya emitido y
    un evento `node_end` con `completed_at`/`duration_ms` medidos. Si el
    nodo lanza, la excepción se propaga intacta (fail-loud) y queda el
    `node_start` como último registro del nodo; el runtime marca la traza
    como "failed" con el tipo de excepción.
    """

    @functools.wraps(fn)
    async def async_wrapper(state: dict) -> dict:
        started = utcnow()
        t0 = time.perf_counter()
        node_events: list[dict] = []
        try:
            update = await fn(state)
        finally:
            # La duración se mida o falle el nodo: el finally conserva el
            # valor para el log de frontera (los eventos solo persisten en
            # éxito; en fallo el runtime marca la traza como "failed").
            duration_ms = (time.perf_counter() - t0) * 1000.0
            node_events.append(_node_end_event(name, started, duration_ms))
            logger.debug("Nodo %s ejecutado en %.1f ms.", name, duration_ms)
        emitted = list(update.pop(TRACE_EVENTS_KEY, []))
        update[TRACE_EVENTS_KEY] = [
            *_events_from_state(state),
            new_event(
                name,
                "node_start",
                f"Inicio de {name}.",
                started_at=started,
            ),
            *emitted,
            *node_events,
        ]
        return update

    @functools.wraps(fn)
    def sync_wrapper(state: dict) -> dict:
        started = utcnow()
        t0 = time.perf_counter()
        update = fn(state)
        emitted = list(update.pop(TRACE_EVENTS_KEY, []))
        update[TRACE_EVENTS_KEY] = [
            *_events_from_state(state),
            new_event(
                name,
                "node_start",
                f"Inicio de {name}.",
                started_at=started,
            ),
            *emitted,
            _node_end_event(name, started, (time.perf_counter() - t0) * 1000.0),
        ]
        return update

    return async_wrapper if asyncio.iscoroutinefunction(fn) else sync_wrapper
