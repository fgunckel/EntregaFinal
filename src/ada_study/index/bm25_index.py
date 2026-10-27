"""Índice BM25 propio, incremental y por versión (ADR-0003, specs/02 §10).

Por qué no se reutiliza el `BM25Retriever` de M7 (rank_bm25):
- `BM25Okapi` precalcula el IDF sobre el corpus completo en el constructor y
  no admite altas/bajas incrementales: cada carga obligaría a reconstruir el
  índice completo (prohibido por specs/02 §10).
- M7 indexa documentos completos sin número de página ni chunk_id, mientras
  que la procedencia exigida (specs/02 §18) requiere `document_id`,
  `version_id`, `chunk_id`, `page` y `content_hash` por fragmento.

Este módulo implementa Okapi BM25 (k1=1.5, b=0.75) a nivel de chunk con:
- `add_version` / `remove_version`: altas y bajas incrementales por versión,
  sin tocar el resto del índice (actualización por versión, specs/02 §10).
- `rebuild`: reconstrucción completa desde los chunks persistidos en
  PostgreSQL (fuente de verdad, ADR-0003) — solo para arranque en frío.

Decisión aprobada (revisión de TASK-03): PostgreSQL es la ÚNICA fuente de
estado. Al arrancar, el índice BM25 se reconstruye desde `document_chunks`
persistidos; NO se agrega persistencia JSON del índice como fuente adicional
de estado. `to_state` / `from_state` quedan como utilidades de diagnóstico y
respaldo puntual, no como mecanismo de estado entre reinicios.
"""

import json
import math
import re

_TOKEN_PATTERN = re.compile(r"\w+", flags=re.UNICODE)

# Parámetros Okapi BM25 estándar (idénticos a los de rank_bm25, M4).
_K1 = 1.5
_B = 0.75


def tokenize(text: str) -> list[str]:
    """Normaliza el texto en tokens minúsculas (misma convención que M4)."""
    return _TOKEN_PATTERN.findall(text.lower())


class BM25Index:
    """Índice BM25 incremental a nivel de chunk, actualizable por versión.

    A diferencia de `BM25Okapi`, el IDF se deriva de estadísticas agregadas
    (N, df, longitud total) que se mantienen incrementalmente al añadir o
    quitar una versión, sin reconstruir el corpus completo.
    """

    def __init__(self) -> None:
        # chunk_id -> (tokens, doc_len)
        self._postings: dict[str, tuple[list[str], int]] = {}
        # término -> conjunto de chunk_ids que lo contienen (df incremental)
        self._df: dict[str, set[str]] = {}
        # version_id -> conjunto de chunk_ids de esa versión
        self._version_chunks: dict[str, set[str]] = {}
        self._total_length: int = 0

    # -- construcción incremental -------------------------------------------

    def add_version(self, chunks: list) -> None:
        """Añade los chunks de una versión al índice (idempotente).

        Si la versión ya estaba indexada, se retira primero: repetir la
        operación no duplica chunks (reintento seguro, specs/02 §29).
        """
        if not chunks:
            return
        version_id = chunks[0].version_id
        self.remove_version(version_id)
        members: set[str] = set()
        for chunk in chunks:
            chunk_id = chunk.chunk_id
            tokens = tokenize(f"{chunk.title} {chunk.text}")
            self._postings[chunk_id] = (tokens, len(tokens))
            self._total_length += len(tokens)
            for term in set(tokens):
                self._df.setdefault(term, set()).add(chunk_id)
            members.add(chunk_id)
        self._version_chunks[version_id] = members

    def remove_version(self, version_id: str) -> int:
        """Retira del índice todos los chunks de una versión.

        Devuelve la cantidad de chunks eliminados (0 si no estaba indexada).
        """
        members = self._version_chunks.pop(version_id, set())
        removed = 0
        for chunk_id in members:
            entry = self._postings.pop(chunk_id, None)
            if entry is None:
                continue
            tokens, doc_len = entry
            self._total_length -= doc_len
            for term in set(tokens):
                term_members = self._df.get(term)
                if term_members is not None:
                    term_members.discard(chunk_id)
                    if not term_members:
                        del self._df[term]
            removed += 1
        return removed

    def indexed_versions(self) -> set[str]:
        """Versiones actualmente indexadas (verificación de destinos)."""
        return set(self._version_chunks.keys())

    def chunk_count(self) -> int:
        """Cantidad total de chunks indexados."""
        return len(self._postings)

    # -- consulta -------------------------------------------------------------

    def search(self, query: str, top_k: int = 5) -> list[tuple[str, float]]:
        """Devuelve los (chunk_id, score) con mayor relevancia BM25."""
        if not query.strip():
            raise ValueError("La consulta no puede estar vacía")
        if top_k < 1:
            raise ValueError("top_k debe ser al menos 1")
        query_terms = tokenize(query)
        if not query_terms:
            return []
        total_docs = len(self._postings)
        if total_docs == 0:
            return []
        avgdl = self._total_length / total_docs
        scores: dict[str, float] = {}
        for term in query_terms:
            term_members = self._df.get(term)
            if not term_members:
                continue
            df = len(term_members)
            idf = math.log(1 + (total_docs - df + 0.5) / (df + 0.5))
            for chunk_id in term_members:
                tokens, doc_len = self._postings[chunk_id]
                tf = tokens.count(term)
                denom = tf + _K1 * (1 - _B + _B * doc_len / avgdl)
                scores[chunk_id] = (
                    scores.get(chunk_id, 0.0) + idf * (tf * (_K1 + 1)) / denom
                )
        ranked = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
        return ranked[:top_k]

    # -- serialización ----------------------------------------------------------

    def to_state(self) -> str:
        """Serializa el índice a JSON (persistencia entre reinicios)."""
        return json.dumps(
            {
                "postings": {
                    chunk_id: [tokens, doc_len]
                    for chunk_id, (tokens, doc_len) in self._postings.items()
                },
                "df": {term: sorted(members) for term, members in self._df.items()},
                "version_chunks": {
                    version_id: sorted(members)
                    for version_id, members in self._version_chunks.items()
                },
                "total_length": self._total_length,
            },
            ensure_ascii=False,
        )

    def load_state(self, state: str) -> None:
        """Restaura el índice desde un estado serializado con `to_state`."""
        data = json.loads(state)
        self._postings = {
            chunk_id: (tokens, doc_len)
            for chunk_id, (tokens, doc_len) in data["postings"].items()
        }
        self._df = {term: set(members) for term, members in data["df"].items()}
        self._version_chunks = {
            version_id: set(members)
            for version_id, members in data["version_chunks"].items()
        }
        self._total_length = data["total_length"]

    # -- reconstrucción -----------------------------------------------------------

    @classmethod
    def rebuild(cls, chunks: list) -> "BM25Index":
        """Reconstruye el índice completo desde chunks persistidos.

        Solo para arranque en frío o reconciliación: la operación normal es
        `add_version`/`remove_version` (specs/02 §10).
        """
        index = BM25Index()
        by_version: dict[str, list] = {}
        for chunk in chunks:
            by_version.setdefault(chunk.version_id, []).append(chunk)
        for version_chunks in by_version.values():
            index.add_version(version_chunks)
        return index
