"""Punto de entrada del worker de ingesta (TASK-06, Fase D3/E1).

El worker corre SEPARADO de la API: consume el outbox (`pending_index_ops`)
y ejecuta el ciclo de ingesta (extracción → chunks → indexación →
activación). Uso en desarrollo:

    python -m ada_study.worker_cli "postgresql+asyncpg://ada:pass@localhost:5432/ada_study" --once

Sin Pinecone real usa el doble FakePineconeDestination (limitación informada;
la integración real de Pinecone queda fuera del alcance de TASK-06).
"""

import asyncio
import sys

from ada_study.ingestion.worker import IngestionWorker


async def run_worker(database_url: str, storage_dir: str, once: bool) -> int:
    """Ejecuta el ciclo del worker: recover() + procesamiento de versiones pendientes."""
    from sqlalchemy.ext.asyncio import create_async_engine

    from ada_study.db.repository import CatalogRepository
    from ada_study.index.bm25_index import BM25Index
    from ada_study.ingestion.destinations import BM25Destination, FakePineconeDestination
    from ada_study.storage.pdf_storage import PdfStorage

    engine = create_async_engine(database_url, echo=False)
    repository = CatalogRepository(engine=engine)
    storage = PdfStorage(root=storage_dir)
    bm25 = BM25Index()
    destinations = [BM25Destination(bm25), FakePineconeDestination()]
    worker = IngestionWorker(repository=repository, storage=storage, destinations=destinations)
    try:
        await worker.recover()
        while True:
            processed = 0
            for version_id in await repository.list_version_ids_with_pending_ops():
                version = await worker.run_once(version_id)
                if version.status in {"available", "failed"}:
                    processed += 1
            if processed == 0:
                if once:
                    print("Worker: no hay ops pendientes; saliendo.")
                    return 0
                await asyncio.sleep(2)
    finally:
        await engine.dispose()


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada: python -m ada_study.worker_cli <DATABASE_URL> [--once]."""
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        print(
            "Uso: python -m ada_study.worker_cli <DATABASE_URL> [--once]\n"
            "Consume el outbox de indexación (destinos configurados) hasta\n"
            "agotarlo (--once) o en bucle (default, intervalo 2 s)."
        )
        return 0
    once = "--once" in args
    positional = [a for a in args if not a.startswith("--")]
    if not positional:
        print("ERROR: falta DATABASE_URL.", file=sys.stderr)
        return 1
    from ada_study.config import get_settings

    return asyncio.run(run_worker(positional[0], get_settings().ada_storage_dir, once))


if __name__ == "__main__":
    sys.exit(main())
