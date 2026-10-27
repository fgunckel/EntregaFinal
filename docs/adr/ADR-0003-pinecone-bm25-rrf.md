# ADR-0003 — Pinecone + BM25 propio + RRF para recuperación híbrida

- Estado: Propuesto (espera aprobación del usuario). Alineado con `specs/ADR-001` (aprobada) y Specs 01/02.
- Fecha: 2026-10-25 (rev. 2 — integración Spec Kit v6)
- Decisión aprobada previamente: Pinecone para vectores + BM25 propio para búsqueda léxica + RRF para combinar resultados. No incorporar ChromaDB.

## Contexto

La auditoría (BASELINE §1.1) confirmó que el `HybridRetriever` de M7 (`src/ada/retrieval/hybrid.py`) es in-memory sobre un corpus JSON estático: no soporta incorporar documentos nuevos sin reconstruir, ni borrado por documento (exigido por Spec 02). M4 ya validó el patrón Pinecone + BM25 + EnsembleRetriever (`Entrega4/src/rag_system.py`), con IDs deterministas que evitan duplicados al re-ingerir.

## Decisión

1. **Vectores en Pinecone** (serverless, métrica cosine, un namespace por versión de documento — ver ADR-0004).
2. **BM25 propio** (rank-bm25, ya dependencia del extra `retrieval` de M7) reconstruido desde los chunks persistidos en PostgreSQL, no un índice externo.
3. **RRF** (reciprocal rank fusion, ya implementada y probada en `src/ada/retrieval/fusion.py::reciprocal_rank_fusion`) para combinar los dos rankings.
4. **No se incorpora ChromaDB** (Specs 01/02: duplicaría la función vectorial de Pinecone).

## Justificación de cada dependencia

| Dependencia | Justificación |
|---|---|
| Pinecone | Índice vectorial persistente con borrado por ID/namespace; patrón ya validado en M4; evita administrar un vector store local |
| rank-bm25 | Ya presente en el proyecto M7 (extra `retrieval`); BM25 propio permite reconstruir el índice léxico desde PostgreSQL en cualquier momento (el índice es derivable, no fuente de verdad) |
| RRF | Ya implementada y probada en M7; no añade dependencias |
| ~~ChromaDB~~ | Descartada por decisión aprobada; Pinecone cubre el mismo rol con borrado por documento más directo |

## Arquitectura de recuperación

```
Consulta
  ├─ BM25 (rank-bm25 sobre chunks de PostgreSQL) ──┐
  │                                                ├─ RRF ──> top_k con provenance
  └─ Pinecone (vectores, namespace de la versión) ─┘
```

- **Fuente de verdad de chunks:** PostgreSQL (`document_chunks`, ADR-0001). BM25 y Pinecone son **destinos de indexación derivables**: si se corrompen, se reconstruyen desde PostgreSQL sin perder datos.
- **Metadatos mínimos por vector** (Spec 01 §40): `document_id`, `version_id`, `chunk_id`, `page`, `content_hash` (+ `title`, `source`).
- **ID de vector determinista:** `{document_id}::{version_id}::{chunk_id}` — reindexar la misma versión actualiza en lugar de duplicar (patrón M4).
- **Embeddings:** `sentence-transformers/all-MiniLM-L6-v2` (ya configurado en M7, `ada/config.py::embedding_model`); dimensión 384, validada contra el índice al conectar (patrón M4 `get_pinecone_index`).

## Activación y verificación (specs/ADR-001 §18, Spec 02 §47)

- La activación de una versión requiere que **Pinecone y BM25 estén actualizados y verificados** (presencia de fragmentos confirmada en ambos destinos). Mientras tanto, la versión no es recuperable y el documento no se publica.
- El estado de indexación se registra **por destino** (`pinecone`, `bm25`) en `pending_index_ops` (ADR-0001) para identificar fallos parciales (Spec 02 §54).
- Si un destino falla, la operación se reintenta sin duplicados (IDs deterministas) y el documento no se marca `available` antes de reconciliar ambos destinos (Spec 02 §69).

## Consecuencias

- Pinecone requiere `PINECONE_API_KEY` y es un servicio externo: las llamadas de indexación ocurren solo en el worker de ingesta, nunca en el request HTTP (Spec 01 §19).
- El índice BM25 vive en memoria del proceso de consulta y se reconstruye al arrancar desde PostgreSQL; para el MVP local (corpus de aulas) es suficiente y evita un servicio más. Debe ser **actualizable por versión**: eliminar/reemplazar los chunks de una versión sin afectar otras (Spec 01 §41).
- La consulta híbrida reutiliza `reciprocal_rank_fusion` sin cambios.

## Historial de revisión

- **Rev. 2 (2026-10-25):** integración Spec Kit v6. Cambios: verificación de ambos destinos antes de activar (`specs/ADR-001` §18); metadatos mínimos por vector según Spec 01; estado de indexación por destino; BM25 actualizable por versión; ID de vector con `chunk_id` (no `chunk_index`).
- **Rev. 1 (2026-10-25):** versión inicial de TASK-01.
