"""Fábrica del chat model del tutor (TASK-11).

Construye UNA VEZ por proceso el modelo de lenguaje que usa exclusivamente
el nodo tutor (decisión aprobada TASK-11; el supervisor conserva su enrutado
determinístico y los demás nodos no usan LLM).

Política de configuración:
- `ada_llm_provider="none"` (default seguro) devuelve None: el sistema corre
  en modo determinístico sin dependencias de proveedor ni costos.
- `provider="openai"`: importa `langchain_openai` de forma perezosa (extra
  opcional `llm` de pyproject). La configuración se valida ANTES de importar:
  falta de clave o modelo es un error de configuración (fail-fast al arrancar
  en `create_app`), nunca un ImportError a mitad de una petición.
- La clave API nunca se registra en logs ni se incluye en mensajes de error.

Política de límites (costos y latencia): `temperature`, `max_tokens`,
`timeout` y `max_retries` se pasan al constructor del proveedor (parámetros
documentados por LangChain) y provienen de las settings `ADA_LLM_*`.
"""

import logging
import os

from ada_study.config import Settings

logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = {"openai"}

# Limitación declarada en StudyResponse.limitations cuando el proveedor falla
# y el tutor recurre a la composición determinística (política de fallback
# TASK-11: solo si puede producirse una respuesta contractual segura).
LLM_FALLBACK_LIMITATION = (
    "La redacción con modelo de lenguaje no estuvo disponible: la explicación "
    "fue compuesta determinísticamente a partir de la evidencia recuperada."
)


class LLMConfigError(Exception):
    """Configuración LLM inválida o incompleta (fail-fast al arrancar)."""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


def build_chat_model(settings: Settings):
    """Construye el chat model del tutor o None en modo determinístico.

    - None: modo determinístico (`provider="none"`, default).
    - `BaseChatModel` de OpenAI si `provider="openai"` con modelo y clave.
    - `LLMConfigError` ante configuración inválida o extra no instalado; el
      mensaje nunca contiene la clave ni valores sensibles.
    """
    provider = (settings.ada_llm_provider or "none").strip().lower()
    if provider in {"", "none"}:
        return None

    if provider not in SUPPORTED_PROVIDERS:
        raise LLMConfigError(
            f"Proveedor LLM no soportado: {provider!r}. "
            f"Soportados: {sorted(SUPPORTED_PROVIDERS)}."
        )

    model = (settings.ada_llm_model or "").strip()
    if not model:
        raise LLMConfigError("ADA_LLM_PROVIDER=openai requiere ADA_LLM_MODEL.")

    api_key = (settings.ada_llm_api_key or "").strip() or os.environ.get(
        "OPENAI_API_KEY", ""
    ).strip()
    if not api_key:
        raise LLMConfigError(
            "ADA_LLM_PROVIDER=openai requiere ADA_LLM_API_KEY u OPENAI_API_KEY."
        )

    try:
        from langchain_openai import ChatOpenAI  # import perezoso (extra `llm`)
    except ImportError as exc:
        raise LLMConfigError(
            "Proveedor 'openai' sin el extra opcional: instalar con "
            'pip install -e ".[llm]".'
        ) from exc

    # Registro sin secretos: solo parámetros operativos (TASK-11).
    logger.info(
        "Chat model del tutor configurado: model=%s max_tokens=%s "
        "timeout=%ss max_retries=%s (clave no registrada).",
        model,
        settings.ada_llm_max_tokens,
        settings.ada_llm_timeout_seconds,
        settings.ada_llm_max_retries,
    )
    return ChatOpenAI(
        model=model,
        api_key=api_key,
        temperature=settings.ada_llm_temperature,
        max_tokens=settings.ada_llm_max_tokens,
        timeout=settings.ada_llm_timeout_seconds,
        max_retries=settings.ada_llm_max_retries,
    )