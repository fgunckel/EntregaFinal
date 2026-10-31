"""Tests unitarios de la traducción de duplicados (ADR-0001 rev. 3).

Sin PostgreSQL: prueban la lógica pura (clasificación de violaciones de
integridad y orquestación del servicio con repositorio de prueba). La
validación del índice único REAL está en tests/integration/ contra
PostgreSQL.
"""

import pytest

from ada_study.db.repository import (
    CatalogRepository,
    DuplicateDocumentError,
    IntegrityError,
    _is_dedup_violation,
)
from ada_study.domain.catalog_service import CatalogService
from ada_study.storage.pdf_storage import PdfStorage


class _FakeViolation(Exception):
    """Doble del error del driver con nombre de constraint (asyncpg)."""

    def __init__(self, constraint_name: str | None = None, message: str = "dup key") -> None:
        if constraint_name is not None:
            self.constraint_name = constraint_name
        super().__init__(message)


def _integrity_error(orig: Exception) -> IntegrityError:
    return IntegrityError("INSERT INTO ...", {}, orig)


class TestIsDedupViolation:
    def test_true_for_dedup_index(self):
        exc = _integrity_error(_FakeViolation(constraint_name="ux_documents_active_sha256"))
        assert _is_dedup_violation(exc) is True

    def test_false_for_other_unique_index(self):
        exc = _integrity_error(_FakeViolation(constraint_name="ux_versions_document_number"))
        assert _is_dedup_violation(exc) is False

    def test_false_for_primary_key_violation(self):
        exc = _integrity_error(_FakeViolation(constraint_name="documents_pkey"))
        assert _is_dedup_violation(exc) is False

    def test_false_without_constraint_attribute(self):
        exc = _integrity_error(Exception("violación genérica sin constraint"))
        assert _is_dedup_violation(exc) is False

    def test_message_fallback_mentions_dedup_index(self):
        exc = _integrity_error(
            Exception(
                'duplicate key value violates unique constraint "ux_documents_active_sha256"'
            )
        )
        assert _is_dedup_violation(exc) is True


class _StubRepo:
    """Repositorio de prueba: registra llamadas, nunca toca una base real."""

    def __init__(self, existing_version=None):
        self._existing = existing_version
        self.atomic_calls: list[dict] = []
        self.claim_calls: list[dict] = []

    async def find_active_version_by_sha256(self, sha256):
        return self._existing

    async def create_document_atomic(self, record, **kwargs):
        self.atomic_calls.append({"record": record, **kwargs})

    async def get_document(self, document_id):
        from ada_study.contracts.documents import DocumentRecord

        return DocumentRecord(
            document_id=document_id,
            filename="hist.pdf",
            subject=None,
            active_version_id=None,
            editorial_status="draft",
            review_status="not_required",
            uploaded_by="u1",
            created_at="2026-01-01T00:00:00+00:00",
            updated_at="2026-01-01T00:00:00+00:00",
            deleted_at=None,
        )

    async def get_version(self, version_id):
        from ada_study.contracts.documents import DocumentVersion

        return DocumentVersion(
            version_id=version_id,
            document_id="doc_mio",
            version_number=1,
            sha256="a" * 64,
            storage_key="aa/" + "a" * 64 + ".pdf",
            content_type="application/pdf",
            size_bytes=10,
            status="available",
            created_at="2026-01-01T00:00:00+00:00",
        )

    async def create_version_with_claim(self, version, **kwargs):
        self.claim_calls.append({"version": version, **kwargs})
        return version

    async def list_versions(self, document_id):
        raise AssertionError("no debería listarse el historial en dedup temprano")


class TestServiceDedupOrchestration:
    async def test_register_uses_atomic_claim(self, tmp_path):
        storage = PdfStorage(root=tmp_path / "storage")
        repo = _StubRepo()
        service = CatalogService(repository=repo, storage=storage)
        document, version, job = await service.register_document(
            filename="a.pdf", content=b"%PDF-fake", uploaded_by="u1"
        )
        # El registro pasa por el método atómico con el claim incluido.
        assert len(repo.atomic_calls) == 1
        call = repo.atomic_calls[0]
        assert call["sha256"] == version.sha256
        assert call["version"].version_id == version.version_id
        assert call["job"].job_id == job.job_id
        assert {op.operation for op in call["ops"]} == {"index"}
        assert len(call["ops"]) == 2  # pinecone + bm25
        assert document.document_id == version.document_id

    async def test_restore_precheck_blocks_foreign_claimant_before_writes(self, tmp_path):
        from ada_study.contracts.documents import DocumentVersion

        foreign = DocumentVersion(
            version_id="ver_otros",
            document_id="doc_otros",
            version_number=1,
            sha256="a" * 64,
            storage_key="aa/" + "a" * 64 + ".pdf",
            content_type="application/pdf",
            size_bytes=10,
            status="available",
            created_at="2026-01-01T00:00:00+00:00",
        )
        repo = _StubRepo(existing_version=foreign)
        service = CatalogService(repository=repo, storage=PdfStorage(root=tmp_path / "s"))

        with pytest.raises(DuplicateDocumentError) as excinfo:
            await service.restore_version("doc_mio", "ver_hist")
        assert excinfo.value.existing_document_id == "doc_otros"
        # Pre-check: nada fue escrito (el conflicto se detecta antes del alta).
        assert not getattr(repo, "claim_calls", [])


class TestRaiseDuplicateTranslation:
    """FIX-2: la violación de `ux_documents_active_sha256` SIEMPRE se traduce.

    Unitarios sin base real: se parchean los lookups del repositorio para
    simular los tres estados posibles del reclamante al momento del fallo.
    La validación del índice único REAL sigue en tests/integration/.
    """

    SHA = "a" * 64

    def _repo_with(self, monkeypatch, version, claimant) -> CatalogRepository:
        from typing import cast

        from sqlalchemy.ext.asyncio import AsyncEngine

        repo = CatalogRepository(engine=cast(AsyncEngine, None))

        async def _find_version(sha256):
            return version

        async def _find_claimant(sha256):
            return claimant

        monkeypatch.setattr(repo, "find_active_version_by_sha256", _find_version)
        monkeypatch.setattr(repo, "_find_active_claimant", _find_claimant)
        return repo

    def _dedup_exc(self) -> IntegrityError:
        return _integrity_error(
            _FakeViolation(constraint_name="ux_documents_active_sha256")
        )

    async def test_translates_with_version_when_version_recoverable(self, monkeypatch):
        from ada_study.contracts.documents import DocumentVersion

        version = DocumentVersion(
            version_id="ver_otros",
            document_id="doc_otros",
            version_number=1,
            sha256=self.SHA,
            storage_key="aa/" + self.SHA + ".pdf",
            content_type="application/pdf",
            size_bytes=10,
            status="available",
            created_at="2026-01-01T00:00:00+00:00",
        )
        repo = self._repo_with(monkeypatch, version=version, claimant=None)
        exc = self._dedup_exc()

        with pytest.raises(DuplicateDocumentError) as excinfo:
            await repo._raise_duplicate(self.SHA, exc)

        assert excinfo.value.error_code == "ING_DUPLICATE"
        assert excinfo.value.existing_document_id == "doc_otros"
        assert excinfo.value.existing_version_id == "ver_otros"
        assert excinfo.value.__cause__ is exc

    async def test_translates_with_claimant_even_without_matching_version(
        self, monkeypatch
    ):
        # Estado legado: el claim existe en documents.sha256 pero no hay
        # versión recuperable con esa huella (claims cruzados post-migración).
        repo = self._repo_with(monkeypatch, version=None, claimant="doc_legacy")
        exc = self._dedup_exc()

        with pytest.raises(DuplicateDocumentError) as excinfo:
            await repo._raise_duplicate(self.SHA, exc)

        assert excinfo.value.error_code == "ING_DUPLICATE"
        assert excinfo.value.existing_document_id == "doc_legacy"
        assert excinfo.value.existing_version_id is None
        assert excinfo.value.__cause__ is exc

    async def test_reraises_when_claimant_vanished(self, monkeypatch):
        # El reclamante fue borrado entre la violación y la consulta: la
        # unicidad es de PostgreSQL, no del check de aplicación.
        repo = self._repo_with(monkeypatch, version=None, claimant=None)
        exc = self._dedup_exc()

        with pytest.raises(IntegrityError):
            await repo._raise_duplicate(self.SHA, exc)
