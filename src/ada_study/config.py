"""Configuración central tipada (Fase A2 del plan, TASK-06).

Lee variables de entorno con pydantic-settings. Defaults seguros para
desarrollo; límites de carga configurables (A2: límite de tamaño de upload
configurable). Nunca contiene secretos por defecto: DATABASE_URL es
obligatoria en runtime (la API y el worker fallan sin ella).
"""

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Límites por defecto (Spec 01: uploads de PDF razonables para la edad 12-15)
DEFAULT_MAX_UPLOAD_BYTES = 25 * 1024 * 1024  # 25 MB


class Settings(BaseSettings):
    """Configuración tipada de ADA Study (env: ADA_* y DATABASE_URL)."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # PostgreSQL (única fuente de estado, ADR-0001). Obligatoria en runtime.
    database_url: str = ""

    # Almacén content-addressed de PDF (ADR-0002)
    ada_storage_dir: str = ".ada_storage"

    # Límite configurable de tamaño de upload (Spec 01; ADR-0005 ING_FILE_TOO_LARGE)
    ada_max_upload_bytes: int = Field(
        default=DEFAULT_MAX_UPLOAD_BYTES,
        ge=1,
    )

    # Tipos de contenido aceptados en upload (ADR-0005 ING_UNSUPPORTED_TYPE)
    ada_allowed_content_types: str = "application/pdf"


@lru_cache
def get_settings() -> Settings:
    """Devuelve la configuración singleton (cacheada por proceso)."""
    return Settings()
