"""Runtime de estudio determinístico (TASK-10, D-1/D-2/D-3).

Cablea el grafo pedagógico en modo determinístico (sin proveedor LLM real,
pospuesto a TASK-11) sobre el retriever híbrido con aislamiento por
`ViewerContext` (regla TASK-07). El grafo y el índice BM25 se construyen por
petición (D-2: estado efímero, sin persistencia conversacional): el corpus de
la biblioteca es pequeño, así que reconstruir BM25 desde PostgreSQL en cada
consulta es barato y garantiza frescura sin estado global mutable.

Límites (D-3): `recursion_limit` explícito y longitud máxima de consulta.
Los campos HITL heredados de M7 quedan fuera de alcance (D-4): ningún nodo
los usa y este runtime no los expone.
"""

from ada_study.study.runtime import (
    MAX_REQUEST_CHARS,
    STUDY_RECURSION_LIMIT,
    STUDY_TOP_K,
    StudyRuntimeError,
    run_study,
)

__all__ = [
    "MAX_REQUEST_CHARS",
    "STUDY_RECURSION_LIMIT",
    "STUDY_TOP_K",
    "StudyRuntimeError",
    "run_study",
]
