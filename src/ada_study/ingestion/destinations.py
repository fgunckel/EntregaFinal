"""Destinos de indexación del outbox (ADR-0001/0003).

- `IndexDestinationProtocol`: contrato que todo destino debe cumplir.
- `BM25Destination`: destino real sobre `BM25Index` (índice propio
  incremental, ADR-0003).
- `FakePineconeDestination`: doble de pruebas que replica el comportamiento
  esperado del destino Pinecone (upsert/delete por versión, verificación).
  El cliente real de Pinecone se integra en la tarea de API/despliegue;
  aquí se define el contrato y se prueba la mecánica del worker.
"""

from typing import Protocol

from ada_study.index.bm25_index import BM25Index


class DestinationError(Exception):
    """Fallo de un destino de indexación (se codifica como ING_INDEX_FAILED)."""

    def __init__(self, message: str = "Fallo de indexación en el destino") -> None:
        super().__init__(message)


class IndexDestinationProtocol(Protocol):
    """Contrato de un destino de indexación (idempotente por versión)."""

    name: str

    async def index_version(self, version_id: str, chunks: list) -> None:
        """Indexa (upsert) los chunks de una versión; repetir no duplica."""
        ...

    async def remove_version(self, version_id: str) -> None:
        """Retira del destino todos los chunks de una versión."""
        ...

    async def has_version(self, version_id: str) -> bool:
        """Indica si la versión está indexada en el destino (verificación)."""
        ...


class BM25Destination:
    """Destino BM25 real sobre el índice propio incremental (ADR-0003)."""

    name = "bm25"

    def __init__(self, index: BM25Index) -> None:
        self._index = index

    async def index_version(self, version_id: str, chunks: list) -> None:
        self._index.add_version(chunks)

    async def remove_version(self, version_id: str) -> None:
        self._index.remove_version(version_id)

    async def has_version(self, version_id: str) -> bool:
        return version_id in self._index.indexed_versions()


class FakePineconeDestination:
    """Doble de pruebas del destino Pinecone (mismo contrato, ADR-0003).

    Simula un almacén de vectores con upsert/delete idempotente por versión.
    Permite inyectar fallos para probar reintentos y fallos parciales.
    """

    name = "pinecone"

    def __init__(self) -> None:
        self._indexed: dict[str, set[str]] = {}
        self.fail_next_index = False
        self.fail_next_remove = False

    async def index_version(self, version_id: str, chunks: list) -> None:
        if self.fail_next_index:
            self.fail_next_index = False
            raise DestinationError("Pinecone no disponible (simulado)")
        # Upsert idempotente: repetir la versión reemplaza sus vectores.
        self._indexed[version_id] = {chunk.chunk_id for chunk in chunks}

    async def remove_version(self, version_id: str) -> None:
        if self.fail_next_remove:
            self.fail_next_remove = False
            raise DestinationError("Pinecone no disponible (simulado)")
        self._indexed.pop(version_id, None)

    async def has_version(self, version_id: str) -> bool:
        return version_id in self._indexed

    def vector_count(self) -> int:
        """Cantidad total de vectores simulados (verificación de dedup)."""
        return sum(len(members) for members in self._indexed.values())
