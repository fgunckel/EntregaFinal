"""Almacén de PDF originales content-addressed (ADR-0002).

- Layout: {raíz}/{sha256[:2]}/{sha256}.pdf — el nombre ES la huella del contenido.
- La `storage_key` es opaca para la API: nunca se expone como ruta navegable
  ni se construye desde input del usuario.
- El borrado solo elimina archivos huérfanos (sin versiones activas que los
  referencien); la decisión de orfandad es responsabilidad del llamador
  (repositorio/servicio), no del almacén.
"""

import hashlib
from pathlib import Path


class PdfStorage:
    """Almacén de archivos content-addressed para PDF originales."""

    def __init__(self, root: Path) -> None:
        self._root = root

    @property
    def root(self) -> Path:
        """Raíz del almacén (configurable por ADA_STORAGE_DIR)."""
        return self._root

    def compute_sha256(self, content: bytes) -> str:
        """Calcula la huella SHA-256 del contenido."""
        return hashlib.sha256(content).hexdigest()

    def storage_key_for(self, sha256: str) -> str:
        """Construye la clave opaca para una huella dada."""
        return f"{sha256[:2]}/{sha256}.pdf"

    def save_pdf(self, content: bytes) -> tuple[str, str]:
        """Persiste el PDF y devuelve (sha256, storage_key).

        Idempotente: guardar el mismo contenido dos veces produce el mismo
        archivo físico (dedup natural, ADR-0002).
        """
        if not content:
            raise ValueError("El contenido del PDF está vacío")
        sha256 = self.compute_sha256(content)
        storage_key = self.storage_key_for(sha256)
        target = self._root / storage_key
        if target.exists():
            return sha256, storage_key
        target.parent.mkdir(parents=True, exist_ok=True)
        # Escritura atómica: archivo temporal + rename para evitar residuos
        # si el proceso muere a mitad de escritura.
        tmp = target.with_suffix(".tmp")
        tmp.write_bytes(content)
        tmp.replace(target)
        return sha256, storage_key

    def open_pdf(self, storage_key: str) -> bytes:
        """Lee el PDF identificado por una clave opaca.

        Rechaza claves que no correspondan al layout esperado (defensa contra
        path traversal: la clave nunca se construye desde input del usuario,
        pero se valida igualmente).
        """
        sha256 = self._parse_key(storage_key)
        target = self._root / storage_key
        content = target.read_bytes()
        # Verificación de integridad: el nombre del archivo ES su huella.
        actual = self.compute_sha256(content)
        if actual != sha256:
            raise ValueError(
                f"Integridad comprometida en {storage_key}: huella {actual} != {sha256}"
            )
        return content

    def exists(self, storage_key: str) -> bool:
        """Indica si el archivo de una clave existe físicamente."""
        return self._parse_key(storage_key) is not None and (self._root / storage_key).exists()

    def delete_if_orphan(self, storage_key: str) -> bool:
        """Elimina el archivo solo si existe; la orfandad la decide el llamador.

        Devuelve True si eliminó el archivo, False si no existía.
        """
        self._parse_key(storage_key)  # valida el formato antes de tocar disco
        target = self._root / storage_key
        if not target.exists():
            return False
        target.unlink()
        return True

    def _parse_key(self, storage_key: str) -> str:
        """Valida el formato de la clave y devuelve la huella que contiene.

        Formato esperado: {2 hex}/{64 hex}.pdf — cualquier otra cosa se
        rechaza (incluye separadores de ruta alternativos o '..').
        """
        parts = storage_key.split("/")
        if len(parts) != 2:
            raise ValueError("storage_key inválida: formato inesperado")
        prefix, filename = parts
        if len(prefix) != 2 or not all(c in "0123456789abcdef" for c in prefix):
            raise ValueError("storage_key inválida: prefijo no hexadecimal")
        # Nombre esperado: {64 hex}.pdf (68 chars) y debe empezar por el prefijo
        if len(filename) != 68 or not filename.endswith(".pdf"):
            raise ValueError("storage_key inválida: nombre con longitud inesperada")
        if not filename.startswith(prefix):
            raise ValueError("storage_key inválida: nombre no coincide con el prefijo")
        sha256 = filename[:-4]
        if not all(c in "0123456789abcdef" for c in sha256):
            raise ValueError("storage_key inválida: huella no hexadecimal")
        return sha256
