"""Observabilidad de ADA Study (AGENT-TRACE-01).

- `trace`: trazabilidad estructurada por consulta (contratos en
  `contracts/trace.py`): eventos reales de los nodos LangGraph y de las
  llamadas efectivas al LLM, entregados en `StudyResponse.trace` para el
  panel de AGENT-TRACE-02.
- `phoenix`: observabilidad técnica con OpenInference/OTel hacia Phoenix
  (spans reales de LangChain/LangGraph), compartiendo el `trace_id` via
  metadata de la invocación (`ada_trace_id`).
"""

from ada_study.observability.trace import (
    TRACE_EVENTS_KEY,
    new_event,
    traced_node,
)

__all__ = ["TRACE_EVENTS_KEY", "new_event", "traced_node"]
