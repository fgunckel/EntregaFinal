"""Pruebas del almacén de PDF content-addressed (ADR-0002)."""

from pathlib import Path

import pytest

from ada_study.storage.pdf_storage import PdfStorage


@pytest.fixture()
def storage(tmp_path: Path) -> PdfStorage:
    return PdfStorage(root=tmp_path / "storage")


PDF_A = b"%PDF-1.4 contenido de prueba A"
PDF_B = b"%PDF-1.4 contenido de prueba B"


class TestSavePdf:
    def test_save_returns_sha256_and_key(self, storage):
        sha, key = storage.save_pdf(PDF_A)
        assert len(sha) == 64
        assert key == f"{sha[:2]}/{sha}.pdf"

    def test_save_is_deterministic(self, storage):
        sha1, key1 = storage.save_pdf(PDF_A)
        sha2, key2 = storage.save_pdf(PDF_A)
        assert sha1 == sha2
        assert key1 == key2

    def test_save_creates_file(self, storage):
        _, key = storage.save_pdf(PDF_A)
        assert (storage.root / key).exists()

    def test_save_empty_raises(self, storage):
        with pytest.raises(ValueError, match="vacío"):
            storage.save_pdf(b"")

    def test_different_content_different_key(self, storage):
        _, key_a = storage.save_pdf(PDF_A)
        _, key_b = storage.save_pdf(PDF_B)
        assert key_a != key_b


class TestOpenPdf:
    def test_roundtrip(self, storage):
        _, key = storage.save_pdf(PDF_A)
        assert storage.open_pdf(key) == PDF_A

    def test_integrity_check_detects_corruption(self, storage):
        _, key = storage.save_pdf(PDF_A)
        target = storage.root / key
        target.write_bytes(b"contenido corrompido")
        with pytest.raises(ValueError, match="Integridad"):
            storage.open_pdf(key)

    def test_rejects_path_traversal(self, storage):
        with pytest.raises(ValueError):
            storage.open_pdf("../secrets/x.pdf")

    def test_rejects_malformed_key(self, storage):
        with pytest.raises(ValueError):
            storage.open_pdf("no-a-key")

    def test_rejects_non_hex_prefix(self, storage):
        with pytest.raises(ValueError):
            storage.open_pdf("zz/abc.pdf")


class TestDelete:
    def test_delete_existing(self, storage):
        _, key = storage.save_pdf(PDF_A)
        assert storage.delete_if_orphan(key) is True
        assert not (storage.root / key).exists()

    def test_delete_missing_returns_false(self, storage):
        sha = "ab" + "0" * 62
        key = f"{sha[:2]}/{sha}.pdf"
        assert storage.delete_if_orphan(key) is False

    def test_exists(self, storage):
        _, key = storage.save_pdf(PDF_A)
        assert storage.exists(key) is True
        missing_sha = "ff" + "0" * 62
        assert storage.exists(f"{missing_sha[:2]}/{missing_sha}.pdf") is False
