"""Proveedor LLM del grafo pedagógico (TASK-11).

Fábrica del chat model inyectado exclusivamente en el nodo tutor para
redactar `explanation` (decisión aprobada TASK-11: supervisor determinístico;
researcher, validator, synthesizer y limited_finish sin LLM).

Modo determinístico por defecto (`ADA_LLM_PROVIDER=none`): sin dependencias
extras, sin llamadas y sin costos. El paquete de proveedor es un extra
opcional (`pip install -e ".[llm]"`) y se importa de forma perezosa.
"""

from ada_study.llm.factory import (
    LLM_FALLBACK_LIMITATION,
    LLMConfigError,
    build_chat_model,
)

__all__ = ["LLM_FALLBACK_LIMITATION", "LLMConfigError", "build_chat_model"]