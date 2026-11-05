"""Contratos de trazabilidad estructurada (AGENT-TRACE-01).

Fuente de requisitos: tarea AGENT-TRACE-01 (§4 Contrato de trazabilidad).
Convenciones: Pydantic en los límites, base StrictModel (extra="forbid").

Cada consulta de estudio produce UNA `Trace` con eventos reales de la
ejecución de LangGraph y de las llamadas efectivas al LLM. La traza viaja
dentro de `StudyResponse.trace` (opcional, default None) para que la UI la
muestre sin alterar el contrato público (AGENT-TRACE-02 la consume).

Privacidad (Spec 03 §46, Spec 06): los eventos NUNCA incluyen contenido de
documentos, prompts completos, claves ni texto de excepción; solo
identificadores, decisiones, cantidades, duraciones y tipos de error.
"""

from datetime import datetime
from typing import Any, Literal

from pydantic import Field

from ada_study.strict_model import StrictModel

# Tipos de evento: inicio y finalización de nodo, decisión intermedia,
# resultado, advertencia y error. Solo se registran eventos de lo que
# REALMENTE ocurrió (AGENT-TRACE-01 §3.7): no hay nodos "esperados".
TraceEventType = Literal[
    "node_start",
    "node_end",
    "decision",
    "result",
    "warning",
    "error",
]

# Estado del evento: ok (normal), warning (degradación controlada, p. ej.
# fallback determinístico) y error (fallo real registrado).
TraceEventStatus = Literal["ok", "warning", "error"]


class TraceEvent(StrictModel):
    """Evento puntual de la ejecución (nodo, decisión, resultado, error).

    - `node`: nodo LangGraph que originó el evento ("supervisor",
      "researcher", "tutor", "validator", "synthesizer", "limited_finish").
      Las llamadas al LLM se registran bajo el nodo que las invoca ("tutor")
      con `event_type="result"`/`"error"` y el proveedor/modelo en `details`.
    - Los eventos `node_start`/`node_end` se emiten por CADA ejecución real
      del nodo (un nodo puede ejecutarse varias veces por reintentos):
      `started_at`/`completed_at`/`duration_ms` son tiempos medidos, nunca
      estimados.
    - `details`: datos específicos del evento (decisiones de enrutado,
      evidencias aceptadas/descartadas con motivo, tokens del LLM, etc.).
    - `error`: tipo de excepción cuando aplica (NUNCA su texto completo:
      códigos estables y sin filtrar trazas internas, Spec 06).
    """

    event_id: str = Field(min_length=1)
    node: str = Field(min_length=1)
    event_type: TraceEventType
    status: TraceEventStatus = "ok"
    summary: str = Field(min_length=1)
    details: dict[str, Any] = Field(default_factory=dict)
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: float | None = None
    error: str | None = None


class Trace(StrictModel):
    """Trazabilidad completa de una consulta de estudio (AGENT-TRACE-01 §4).

    - `trace_id`: identificador único de la ejecución; correlaciona todos
      los eventos entre sí y con los spans de Phoenix (se envía como
      metadata de la invocación de LangGraph: `ada_trace_id`).
    - `conversation_id`: thread de conversación usado por el checkpointer
      (None en modo efímero). El UUID de conversación del endpoint se
      entrega aparte en la respuesta.
    - `status`: "completed" (el grafo terminó con respuesta publicada) o
      "failed" (excepción en ejecución; `error` lleva el tipo).
    - `nodes_executed`: nodos efectivamente ejecutados, derivado de los
      eventos reales (no se asume que todos corren en todas las consultas).
    """

    trace_id: str = Field(min_length=1)
    conversation_id: str | None = None
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: float | None = None
    status: Literal["completed", "failed"] = "completed"
    nodes_executed: list[str] = Field(default_factory=list)
    events: list[TraceEvent] = Field(default_factory=list)
    error: str | None = None
