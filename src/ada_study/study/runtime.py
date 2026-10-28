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

from ada_study.contracts.documents import ChunkRecord
from ada_study.contracts.study import StudyResponse
from ada_study.graph import compile_graph
from ada_study.index.bm25_index import BM25Index
from ada_study.index.retriever import HybridRetriever, ViewerContext

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

    def __init__(self, error_code: str, detail: str) -> None:
        super().__init__(detail)
        self.error_code = error_code
        self.detail = detail


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


async def run_study(repository, user_request: str, viewer: ViewerContext, chat_model=None):
    """Ejecuta el flujo de estudio completo (determinístico o con tutor LLM).

    Devuelve la `StudyResponse` publicada por el synthesizer (o la salida
    segura de `limited_finish`). Lanza `StudyRuntimeError` ante consultas
    vacías, fallos de retrieval o de ejecución del grafo.

    `chat_model` (opcional, TASK-11): modelo construido una vez por proceso
    e inyectado SOLO al tutor para redactar la explicación; None mantiene el
    modo determinístico. El supervisor conserva el enrutado determinístico y
    un fallo del proveedor activa el fallback determinístico del tutor (no
    multiplica reintentos).
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

    graph = compile_graph(chat_model=None, retriever=retriever, tutor_model=chat_model)
    initial_state = {
        "user_request": query,
        "study_context": None,
        "steps": 0,
        "retries": 0,
        "task_completed": False,
    }
    try:
        result = await graph.ainvoke(
            initial_state,
            config={"recursion_limit": STUDY_RECURSION_LIMIT},
        )
    except Exception as exc:  # noqa: BLE001 - frontera: error controlado sin trazas
        raise StudyRuntimeError(
            "STUDY_INTERNAL",
            "La ejecución del flujo de estudio no pudo completarse.",
        ) from exc

    response_raw = result.get("study_response")
    if not response_raw:
        raise StudyRuntimeError(
            "STUDY_INTERNAL",
            "El flujo terminó sin respuesta publicada.",
        )
    return StudyResponse.model_validate(response_raw)
