"""Runtime de estudio: retrieval híbrido + grafo determinístico (TASK-10).

Flujo por petición (D-2):
1. Recolecta los chunks recuperables para el `ViewerContext` (documentos
   `published`/`published_pending_review` con versión activa `available`, más
   el acceso privado del autor a su propio `pending_review` — regla TASK-07).
2. Reconstruye BM25 desde esos chunks (`BM25Index.rebuild`, decisión TASK-03)
   y arma el `HybridRetriever` con el catálogo real (capa 2 de visibilidad).
3. Compila el grafo (`compile_graph`): el supervisor usa SIEMPRE el enrutado
   determinístico (decisión aprobada TASK-11); el chat model opcional (si se
   configuró `ADA_LLM_PROVIDER=openai`) se inyecta SOLO al tutor para
   redactar la explicación, con fallback determinístico ante fallo del
   proveedor (sin multiplicar reintentos: máx. 1 llamada del tutor por
   intento).
4. Invoca el grafo con `recursion_limit` explícito (D-3) y devuelve la
   `StudyResponse` validada por el validator del grafo.

Errores: `StudyRuntimeError` con códigos estables para fallos de retrieval o
de ejecución del grafo; nunca expone trazas internas.
"""

import time
from typing import Any
from uuid import uuid4

from ada_study.contracts.documents import ChunkRecord
from ada_study.contracts.study import StudyResponse
from ada_study.contracts.trace import Trace, TraceEvent
from ada_study.graph import compile_graph
from ada_study.graph.history import append_student_message, sanitize_history
from ada_study.index.bm25_index import BM25Index
from ada_study.index.retriever import HybridRetriever, ViewerContext
from ada_study.observability.trace import TRACE_EVENTS_KEY, utcnow

# Límites de ejecución (D-3). La topología determinística completa usa ~7
# pasos (supervisor→researcher→supervisor→tutor→validator→supervisor→
# synthesizer); con 2 reparaciones máximas (MAX_RETRIES del validator) la
# cota superior razonable es 25, igual al default de LangGraph pero explícito.
STUDY_RECURSION_LIMIT = 25

# Límite de entrada: consulta del estudiante (Spec 01: edad 12-15).
MAX_REQUEST_CHARS = 2000

# Fragmentos recuperados por consulta (coincide con el researcher).
STUDY_TOP_K = 5


class StudyRuntimeError(Exception):
    """Fallo controlado del runtime de estudio (código estable, sin trazas)."""

    def __init__(
        self, error_code: str, detail: str, trace: Trace | None = None
    ) -> None:
        super().__init__(detail)
        self.error_code = error_code
        self.detail = detail
        # Traza estructurada de la ejecución fallida (AGENT-TRACE-01 §3.7):
        # eventos reales recolectados hasta el fallo. Sin texto de excepción
        # ni contenido sensible: solo el tipo de error en `trace.error`.
        self.trace = trace


async def _collect_visible_chunks(repository, viewer: ViewerContext) -> list[ChunkRecord]:
    """Chunks de versiones activas `available` visibles para el viewer.

    - Documentos `published`/`published_pending_review`: recuperables para
      todos los usuarios autorizados.
    - Documentos `pending_review`: solo si el viewer es el autor (acceso
      privado TASK-07). La capa 2 del retriever re-verifica contra el
      catálogo en cada resultado (red de seguridad ante residuales).
    - `withdrawn`/`deleted`/`rejected`: excluidos absolutamente, incluso
      para el autor (regla TASK-07).
    """
    chunks: list[ChunkRecord] = []
    documents = await repository.list_documents()
    for document in documents:
        status = document.editorial_status
        if status in {"published", "published_pending_review"}:
            pass
        elif status == "pending_review" and document.uploaded_by == viewer.user_id:
            pass
        else:
            continue
        for version in await repository.list_versions(document.document_id):
            if version.version_id == document.active_version_id and version.status == "available":
                chunks.extend(await repository.list_chunks(version.version_id))
    return chunks


async def run_study(
    repository,
    user_request: str,
    viewer: ViewerContext,
    chat_model=None,
    *,
    checkpointer=None,
    thread_id: str | None = None,
):
    """Ejecuta el flujo de estudio completo (determinístico o con tutor LLM).

    Devuelve la `StudyResponse` publicada por el synthesizer (o la salida
    segura de `limited_finish`). Lanza `StudyRuntimeError` ante consultas
    vacías, fallos de retrieval o de ejecución del grafo.

    `chat_model` (opcional, TASK-11): modelo construido una vez por proceso
    e inyectado SOLO al tutor para redactar la explicación; None mantiene el
    modo determinístico. El supervisor conserva el enrutado determinístico y
    un fallo del proveedor activa el fallback determinístico del tutor (no
    multiplica reintentos).

    `checkpointer` + `thread_id` (CHECKPOINTER-01/02): si se provee un
    checkpointer, `thread_id` es OBLIGATORIO y estable (derivado de la
    conversación autorizada por el endpoint); el estado del grafo persiste
    por thread y sobrevive al reinicio del proceso.
    """
    query = (user_request or "").strip()
    if not query:
        raise StudyRuntimeError("STUDY_EMPTY_REQUEST", "La consulta no puede estar vacía.")
    if len(query) > MAX_REQUEST_CHARS:
        raise StudyRuntimeError(
            "STUDY_REQUEST_TOO_LONG",
            f"La consulta excede {MAX_REQUEST_CHARS} caracteres.",
        )

    try:
        chunks = await _collect_visible_chunks(repository, viewer)
    except Exception as exc:  # noqa: BLE001 - frontera: error controlado sin trazas
        raise StudyRuntimeError(
            "STUDY_RETRIEVAL_UNAVAILABLE",
            "No se pudo acceder al catálogo para recuperar evidencia.",
        ) from exc

    index = BM25Index.rebuild(chunks)
    retriever = HybridRetriever(
        catalog=repository,
        bm25_index=index,
        vector_search=None,  # Pinecone real pendiente (TASK-11+); solo BM25.
        top_k=STUDY_TOP_K,
        viewer=viewer,
    )
    retriever.load_chunks(chunks)

    graph = compile_graph(
        chat_model=None, retriever=retriever, tutor_model=chat_model, checkpointer=checkpointer
    )
    config: dict[str, Any] = {"recursion_limit": STUDY_RECURSION_LIMIT}
    if checkpointer is not None:
        if not thread_id:
            raise StudyRuntimeError(
                "STUDY_INTERNAL",
                "El checkpointer requiere un thread_id de conversación.",
            )
        config["configurable"] = {"thread_id": thread_id}

    # INTAKE del historial pedagógico (CHECKPOINTER-03, dimensión A). El
    # estado previo del thread se recupera del CHECKPOINTER (nunca del
    # cliente): incluye el historial completo del turno anterior. El mensaje
    # nuevo del alumno se incorpora EXACTAMENTE una vez (guarda anti-duplicado
    # en `append_student_message` para reintentos tras fallos), se conserva el
    # historial anterior intacto y los artefactos temporales se reinician.
    # Sin checkpointer (modo efímero) el historial del turno es solo el mensaje
    # actual: compatibilidad total con el comportamiento previo.
    previous_history: list[dict] = []
    if checkpointer is not None:
        snapshot = await graph.aget_state(config)
        previous_history = sanitize_history(
            snapshot.values.get("conversation_history") if snapshot else None
        )
    conversation_history = append_student_message(previous_history, query)

    # Estado inicial de CADA turno (CHECKPOINTER-03, dimensión B): el
    # `user_request` del turno nuevo más el reinicio explícito de todos los
    # campos temporales. Son canales LastValue (sin reducer), así que
    # reenviarlos SÍ sobrescribe el valor checkpointeado del turno anterior.
    # `messages` (reducer acumulativo) NO se reinicia: contiene decisiones
    # internas del supervisor. `conversation_history` tampoco: es el historial
    # pedagógico persistente (intake arriba, consolidación en el synthesizer).
    # Trazabilidad (AGENT-TRACE-01): identificador único de la ejecución y
    # canal `trace_events` vacío; el `trace_id` se comparte con Phoenix vía
    # metadata de la invocación (`ada_trace_id` en los atributos de spans).
    trace_id = uuid4().hex
    config["metadata"] = {"ada_trace_id": trace_id}
    trace_started = utcnow()
    trace_t0 = time.perf_counter()

    initial_state: dict = {
        "user_request": query,
        "study_context": None,
        "conversation_history": conversation_history,
        "research_artifact": None,
        "study_response": None,
        "validation_result": None,
        "validation_errors": [],
        "contributions": [],
        "steps": 0,
        "retries": 0,
        "task_completed": False,
        "trace_events": [],
    }
    # astream(stream_mode="updates") entrega el update REAL de cada nodo al
    # terminar su super-step: semánticamente equivalente a `ainvoke` (mismo
    # estado final acumulado), pero permite recolectar los eventos de los
    # pasos completados incluso si un paso posterior lanza excepción.
    result: dict = {}
    try:
        async for chunk in graph.astream(
            initial_state, config=config, stream_mode="updates"
        ):
            if not isinstance(chunk, dict):
                continue
            for update in chunk.values():
                if isinstance(update, dict):
                    result.update(update)
    except Exception as exc:  # noqa: BLE001 - frontera: error controlado sin trazas
        raise StudyRuntimeError(
            "STUDY_INTERNAL",
            "La ejecución del flujo de estudio no pudo completarse.",
            trace=_build_trace(
                trace_id,
                thread_id,
                trace_started,
                trace_t0,
                result.get(TRACE_EVENTS_KEY) or [],
                status="failed",
                error=type(exc).__name__,
            ),
        ) from exc

    events_raw = result.get(TRACE_EVENTS_KEY) or []
    response_raw = result.get("study_response")
    if not response_raw:
        raise StudyRuntimeError(
            "STUDY_INTERNAL",
            "El flujo terminó sin respuesta publicada.",
            trace=_build_trace(
                trace_id,
                thread_id,
                trace_started,
                trace_t0,
                events_raw,
                status="failed",
                error="StudyRuntimeError",
            ),
        )
    trace = _build_trace(
        trace_id, thread_id, trace_started, trace_t0, events_raw,
        status="completed", error=None,
    )
    response = StudyResponse.model_validate(response_raw)
    # La traza viaja DENTRO de la respuesta (AGENT-TRACE-01 §5): el campo es
    # opcional y aditivo, sin romper el contrato público de `/api/study`.
    return response.model_copy(update={"trace": trace})


def _build_trace(
    trace_id: str,
    conversation_id: str | None,
    started_at,
    t0: float,
    events_raw: list,
    status: str,
    error: str | None,
) -> Trace:
    """Completa la traza del turno con los eventos reales recolectados."""
    events = [TraceEvent.model_validate(item) for item in events_raw]
    nodes_executed = []
    for event in events:
        if event.event_type == "node_start" and event.node not in nodes_executed:
            nodes_executed.append(event.node)
    return Trace(
        trace_id=trace_id,
        conversation_id=conversation_id,
        started_at=started_at,
        completed_at=utcnow(),
        duration_ms=round((time.perf_counter() - t0) * 1000.0, 3),
        status=status,  # type: ignore[arg-type]
        nodes_executed=nodes_executed,
        events=events,
        error=error,
    )
