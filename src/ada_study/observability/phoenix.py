"""Observabilidad técnica: OpenInference/OTel hacia Phoenix (AGENT-TRACE-01 §6).

Reutiliza la instrumentación estándar de LangChain (`openinference-
instrumentation-langchain`): genera spans REALES por cada ejecución de
LangGraph y por cada llamada al proveedor LLM (Groq aparece como operación
diferenciada con sus atributos LLM de OpenInference), sin duplicar spans ni
crear un segundo sistema.

Correlación con la traza estructurada: `study/runtime.py` envía el
`trace_id` como metadata de la invocación (`ada_trace_id`), que el
instrumentor adjunta a los atributos de los spans. Así el panel de
AGENT-TRACE-02 y Phoenix comparten el identificador de la ejecución.

Phoenix corre como SERVICIO (imagen `arizephoenix/phoenix` en
docker-compose; UI en el puerto 6006): la app solo necesita el exportador
OTLP apuntando a `ADA_PHOENIX_ENDPOINT`. Con `ADA_PHOENIX_ENABLED=false`
(default) no se inicializa nada: cero overhead y cero dependencias cargadas.
Al igual que el checkpointer (CHECKPOINTER-01), una Phoenix habilitada pero
no accesible corta el arranque de forma ruidosa (fail-loud): no se degrada
en silencio una configuración pedida explícitamente.
"""

import logging

logger = logging.getLogger(__name__)

# Guarda de proceso: `instrument()` global no debe aplicarse dos veces
# (create_app puede llamarse en pruebas y en producción del mismo proceso).
_instrumented = False


def init_phoenix(settings, *, span_exporter=None) -> None:
    """Inicializa OTel + OpenInference hacia Phoenix (idempotente por proceso).

    - `settings.ada_phoenix_endpoint`: endpoint OTLP/HTTP del colector de
      Phoenix (default: http://localhost:6006/v1/traces).
    - `span_exporter` permite inyectar un exportador alternativo en pruebas
      (p. ej. InMemorySpanExporter de opentelemetry-sdk); en producción es
      None y se usa OTLPSpanExporter.
    - Importes perezosos: con la feature deshabilitada ni siquiera se
      importan las dependencias de observabilidad.
    """
    global _instrumented

    from openinference.instrumentation.langchain import LangChainInstrumentor
    from opentelemetry import trace as otel_trace
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, SimpleSpanProcessor

    current_provider = otel_trace.get_tracer_provider()
    if not isinstance(current_provider, TracerProvider):
        provider = TracerProvider(
            resource=Resource.create({"service.name": "ada-study"})
        )
        exporter = span_exporter or OTLPSpanExporter(
            endpoint=settings.ada_phoenix_endpoint
        )
        # InMemorySpanExporter (pruebas) exige procesamiento síncrono para
        # poder inspeccionar los spans de inmediato.
        processor = (
            SimpleSpanProcessor(exporter)
            if span_exporter is not None
            else BatchSpanProcessor(exporter)
        )
        provider.add_span_processor(processor)
        otel_trace.set_tracer_provider(provider)

    if not _instrumented:
        # Spans reales de LangGraph (nodos) y del LLM (Groq como operación
        # diferenciada, con tokens si el proveedor los reporta).
        LangChainInstrumentor().instrument()
        _instrumented = True
    logger.info(
        "Observabilidad Phoenix activa (endpoint=%s).",
        settings.ada_phoenix_endpoint,
    )
