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

    # Conversaciones multi-turno (CHECKPOINTER-02/03). False (default) = modo
    # efímero por petición (sin checkpointer ni estado conversacional).
    ada_conversations_enabled: bool = False

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

    # Proveedor LLM del tutor (TASK-11). "none" (default) = modo determinístico
    # sin llamadas ni costos. La clave llega SOLO por entorno (.env); jamás se
    # incluye en código, logs ni mensajes de error (sin secretos en el repo).
    ada_llm_provider: str = "none"  # "none" | "openai" | "groq" (endpoint
    # OpenAI-compatible de Groq; la factory fija base_url y reasoning_format)
    ada_llm_model: str = ""  # obligatorio si provider != "none" (p. ej. gpt-5.4-mini)
    ada_llm_api_key: str = ""  # alternativa: OPENAI_API_KEY (openai) o
    # GROQ_API_KEY (groq); sin default inseguro
    # Límites por llamada (política de costos TASK-11: máx. 1 llamada del tutor
    # por intento; máx. 3 por petición con las 2 reparaciones del validator).
    ada_llm_temperature: float = 0.0
    # 2000 tokens (LLM-CONFIG-01, antes 800): alcanza para explicaciones
    # pedagógicas completas con títulos y listas sin inflar el costo por llamada.
    ada_llm_max_tokens: int = Field(default=2000, ge=1)
    ada_llm_timeout_seconds: float = Field(default=20.0, gt=0)
    ada_llm_max_retries: int = Field(default=2, ge=0)

    # Observabilidad técnica (AGENT-TRACE-01 §6): OpenInference/OTel hacia
    # Phoenix. False (default) = sin instrumentación ni dependencias cargadas.
    # Phoenix corre como servicio (docker-compose, puerto 6006); la app solo
    # exporta spans por OTLP al endpoint configurado.
    ada_phoenix_enabled: bool = False
    ada_phoenix_endpoint: str = "http://localhost:6006/v1/traces"


@lru_cache
def get_settings() -> Settings:
    """Devuelve la configuración singleton (cacheada por proceso)."""
    return Settings()
