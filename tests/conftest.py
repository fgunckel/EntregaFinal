"""Configuración global de tests (preflight CHECKPOINTER-03).

Windows: psycopg3 (driver async del checkpointer de LangGraph) no puede
ejecutarse sobre `ProactorEventLoop`. pytest-asyncio usa el loop default,
así que se fija la política de SelectorEventLoop SOLO para la suite.
No modifica producción (uvicorn ya fija Selector en Windows).
"""

import asyncio
import sys

if sys.platform == "win32":
    import asyncio

    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

import pytest


@pytest.fixture(autouse=True)
def _hermetic_conversations_env(monkeypatch):
    """Aísla los tests de la ADA_CONVERSATIONS_ENABLED ambiental de la sesión.

    Los tests que necesitan conversaciones la habilitan EXPLÍCITAMENTE en su
    Settings; el resto debe ver el default (False, modo efímero) sin que la
    sesión/shell lo contamin (preflight CHECKPOINTER-03).
    """
    monkeypatch.delenv("ADA_CONVERSATIONS_ENABLED", raising=False)
