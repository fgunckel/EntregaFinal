"""Worker de ingesta incremental de ADA Study (TASK-03)."""

from ada_study.ingestion.destinations import (
    BM25Destination,
    DestinationError,
    FakePineconeDestination,
    IndexDestinationProtocol,
)
from ada_study.ingestion.worker import IngestionWorker

__all__ = [
    "BM25Destination",
    "DestinationError",
    "FakePineconeDestination",
    "IndexDestinationProtocol",
    "IngestionWorker",
]
