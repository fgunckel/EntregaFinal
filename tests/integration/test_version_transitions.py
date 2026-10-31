"""Transiciones técnicas de versión contra PostgreSQL REAL (ADR-0005).

Cubre el contrato de `CatalogRepository.update_version_status` (Opción A):
devuelve el `DocumentVersion` actualizado (el worker propaga el estado con
ese retorno); los fakes de tests/test_worker.py, test_api_documents.py y
test_e2e_scenarios.py ya implementan este contrato.
"""

import pytest

from ada_study.contracts.documents import DocumentVersion
from ada_study.db.repository import InvalidTransitionError
from ada_study.domain.catalog_service import CatalogService

PDF = b"%PDF-1.4 contenido para transiciones de estado"


async def _seed_version(catalog: CatalogService):
    """Documento + versión real en `queued` (flujo de registro estándar)."""
    _, version, _ = await catalog.register_document(
        filename="a.pdf", content=PDF, uploaded_by="u1"
    )
    return version


class TestUpdateVersionStatus:
    async def test_returns_updated_document_version(self, repo, catalog):
        version = await _seed_version(catalog)

        updated = await repo.update_version_status(version.version_id, "processing")

        # 1. Retorno de tipo DocumentVersion (contrato Opción A).
        assert isinstance(updated, DocumentVersion)
        assert updated.version_id == version.version_id
        # 2. Estado actualizado.
        assert updated.status == "processing"
        # Persistencia real: la fila en PostgreSQL refleja la transición.
        fresh = await repo.get_version(version.version_id)
        assert fresh.status == "processing"

    async def test_error_code_cleared_on_transition(self, repo, catalog):
        version = await _seed_version(catalog)
        # Un fallo previo deja error_code poblado en la fila.
        await repo.mark_version_failed(version.version_id, "ING_EXTRACT_FAILED")
        assert (await repo.get_version(version.version_id)).error_code == (
            "ING_EXTRACT_FAILED"
        )

        # failed -> queued es la transición del retry (vía del servicio).
        updated = await repo.update_version_status(version.version_id, "queued")

        # 3. error_code limpiado a None tras la transición.
        assert updated.error_code is None
        assert (await repo.get_version(version.version_id)).error_code is None

    async def test_invalid_transition_rejected_without_update(self, repo, catalog):
        version = await _seed_version(catalog)

        # queued -> available NO es transición válida (ADR-0005): requiere
        # pasar por processing -> indexing y la compuerta de activación.
        with pytest.raises(InvalidTransitionError):
            await repo.update_version_status(version.version_id, "available")

        # La fila no se tocó.
        fresh = await repo.get_version(version.version_id)
        assert fresh.status == "queued"
        assert fresh.error_code is None