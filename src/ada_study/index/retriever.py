"""Retriever híbrido persistente sobre el catálogo (F1, ADR-0003/0004).

Integra BM25 (índice propio incremental, ADR-0003) + Pinecone (por namespace
de versión) + RRF (reutilizado de M7, k=60). PostgreSQL es la única fuente
de verdad de los chunks (ADR-0003); BM25 se reconstruye desde `document_chunks`
al arrancar.

Filtrado de visibilidad (Spec 06 §72, §104; plan §F1):
- Solo fragmentos de documentos `published`/`published_pending_review` con
  versión activa `available` son recuperables.
- El filtro se aplica en DOS capas:
  1. En la fuente: BM25 solo contiene versiones indexadas (que solo se
     indexan al activarse); Pinecone puede tener residuales, así que la capa
     2 es obligatoria.
  2. En el resultado: se re-verifica contra el catálogo (PostgreSQL) que cada
     chunk pertenezca a un documento publicado con versión activa disponible.
     Esto excluye resultados residuales de Pinecone (eventual consistency,
     ADR-0004) y versiones no activas o no disponibles.
- La procedencia se conserva: cada resultado mantiene `document_id`,
  `version_id`, `chunk_id`, `page`, `content_hash` (Spec 02 §18).
"""

from typing import Protocol

from ada_study.contracts.documents import ChunkRecord, DocumentRecord, DocumentVersion
from ada_study.index.bm25_index import BM25Index

# Estados editoriales recuperables (Spec 06 §72, §104)
_VISIBLE_EDITORIAL = frozenset({"published", "published_pending_review"})


class ViewerContext:
    """Contexto de visibilidad del usuario que consulta.

    Habilita el acceso privado del autor a su propio `pending_review`
    (regla aprobada en TASK-07): el RAG compartido sigue excluyendo
    pending_review (ADR-0005); este contexto solo amplía la recuperación
    para el propietario del documento.
    """

    def __init__(self, user_id: str, role: str) -> None:
        self.user_id = user_id
        self.role = role


class CatalogView(Protocol):
    """Vista de solo lectura del catálogo para verificación de visibilidad.

    Protocolo mínimo que cumple `CatalogRepository` (get_document,
    get_version, list_chunks) y cualquier doble de pruebas.
    """

    async def get_document(self, document_id: str) -> DocumentRecord: ...

    async def get_version(self, version_id: str) -> DocumentVersion: ...

    async def list_chunks(self, version_id: str) -> list[ChunkRecord]: ...


class RetrievedChunk:
    """Fragmento recuperado con procedencia completa (Spec 02 §18)."""

    def __init__(
        self,
        chunk: ChunkRecord,
        score: float,
        rank: int,
        strategies: list[str],
    ) -> None:
        self.chunk = chunk
        self.score = score
        self.rank = rank
        self.retriever = "+".join(strategies) if strategies else "rrf"

    @property
    def document_id(self) -> str:
        return self.chunk.document_id

    @property
    def version_id(self) -> str:
        return self.chunk.version_id

    @property
    def chunk_id(self) -> str:
        return self.chunk.chunk_id

    @property
    def page(self) -> int:
        return self.chunk.page

    @property
    def text(self) -> str:
        return self.chunk.text

    @property
    def title(self) -> str:
        return self.chunk.title

    @property
    def content_hash(self) -> str:
        return self.chunk.content_hash


class HybridRetriever:
    """Retriever híbrido BM25 + Pinecone con RRF y filtro de visibilidad.

    - BM25: índice propio incremental (`BM25Index`), reconstruido desde
      PostgreSQL al arrancar (decisión TASK-03).
    - Pinecone: cualquier callable async `search(query, top_k) ->
      list[(chunk_id, score)]` (el destino real o un doble). Puede contener
      residuales: la verificación contra el catálogo los excluye.
    - RRF: fusión de rankings (k=60, igual que M7).
    """

    def __init__(
        self,
        catalog: CatalogView,
        bm25_index: BM25Index,
        vector_search=None,
        top_k: int = 5,
        viewer: ViewerContext | None = None,
    ) -> None:
        self._catalog = catalog
        self._bm25 = bm25_index
        self._vector_search = vector_search
        self._top_k = top_k
        # Visor opcional: habilita acceso privado del autor a su propio
        # `pending_review` (regla aprobada en TASK-07; el RAG compartido
        # sigue excluyendo pending_review, ADR-0005).
        self._viewer = viewer
        # Índice chunk_id -> ChunkRecord, alimentado por `sync_chunks`.
        self._chunk_by_id: dict[str, ChunkRecord] = {}

    # -- sincronización de chunks --------------------------------------------

    def load_chunks(self, chunks: list[ChunkRecord]) -> None:
        """Carga el mapa chunk_id -> ChunkRecord para resolver procedencia.

        Los chunks provienen de PostgreSQL (fuente de verdad, ADR-0003).
        Llamar tras `BM25Index.rebuild` o tras cada activación de versión.
        """
        self._chunk_by_id = {chunk.chunk_id: chunk for chunk in chunks}

    # -- verificación de visibilidad (capa 2, contra el catálogo) -------------

    async def _is_visible(self, chunk: ChunkRecord) -> bool:
        """Re-verifica contra el catálogo que el chunk sea recuperable.

        Excluye: documentos pendientes de aprobación (salvo acceso privado
        del autor, regla TASK-07), rechazados, retirados o eliminados;
        versiones no activas o no `available`. Se aplica SIEMPRE, también
        ante resultados residuales de Pinecone (Spec 06 §104).
        """
        document = await self._catalog.get_document(chunk.document_id)
        status = document.editorial_status
        if status in _VISIBLE_EDITORIAL:
            pass  # visible para todos los usuarios autorizados
        elif status == "pending_review" and self._viewer is not None:
            # Acceso privado: solo el propietario ve su pendiente de revisión.
            if document.uploaded_by != self._viewer.user_id:
                return False
        else:
            return False
        if document.active_version_id != chunk.version_id:
            return False
        version = await self._catalog.get_version(chunk.version_id)
        return version.status == "available"

    # -- consulta híbrida ------------------------------------------------------

    async def search(self, query: str, top_k: int | None = None) -> list[RetrievedChunk]:
        """Recuperación híbrida con filtros de visibilidad y procedencia.

        Devuelve fragmentos de documentos publicados con versión activa
        `available`, fusionados por RRF y con procedencia completa. Si hay
        `viewer`, incluye además el acceso privado del autor a su propio
        `pending_review` (regla TASK-07).
        """
        if not query.strip():
            raise ValueError("La consulta no puede estar vacía")
        if top_k is None:
            top_k = self._top_k
        if top_k < 1:
            raise ValueError("top_k debe ser al menos 1")

        bm25_ranking = self._bm25.search(query, top_k=top_k)
        vector_ranking: list[tuple[str, float]] = []
        if self._vector_search is not None:
            vector_ranking = list(await self._vector_search(query, top_k))

        # Capa 1: BM25 solo contiene versiones activadas (indexadas al activar).
        # Capa 2: re-verificación contra el catálogo para AMBAS listas, porque
        # Pinecone puede conservar residuales de versiones retiradas.
        bm25_visible: list[tuple[str, float]] = []
        for chunk_id, score in bm25_ranking:
            chunk = self._chunk_by_id.get(chunk_id)
            if chunk is not None and await self._is_visible(chunk):
                bm25_visible.append((chunk_id, score))

        vector_visible: list[tuple[str, float]] = []
        for chunk_id, score in vector_ranking:
            chunk = self._chunk_by_id.get(chunk_id)
            if chunk is not None and await self._is_visible(chunk):
                vector_visible.append((chunk_id, score))

        return self._rrf_fuse(bm25_visible, vector_visible, top_k)

    # -- fusión RRF (k=60, igual que M7) ---------------------------------------

    def _rrf_fuse(
        self,
        bm25_ranking: list[tuple[str, float]],
        vector_ranking: list[tuple[str, float]],
        top_k: int,
    ) -> list[RetrievedChunk]:
        """Fusiona listas de (chunk_id, score) ya filtradas por visibilidad."""
        k = 60
        scores: dict[str, float] = {}
        for ranking in (bm25_ranking, vector_ranking):
            for position, (chunk_id, _score) in enumerate(ranking, start=1):
                scores[chunk_id] = scores.get(chunk_id, 0.0) + 1.0 / (k + position)
        ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))[:top_k]
        results: list[RetrievedChunk] = []
        for rank, (chunk_id, fused_score) in enumerate(ordered, start=1):
            strategies = []
            if any(chunk_id == cid for cid, _ in bm25_ranking):
                strategies.append("bm25")
            if any(chunk_id == cid for cid, _ in vector_ranking):
                strategies.append("vector")
            results.append(
                RetrievedChunk(
                    chunk=self._chunk_by_id[chunk_id],
                    score=scores[chunk_id],
                    rank=rank,
                    strategies=strategies,
                )
            )
        return results
