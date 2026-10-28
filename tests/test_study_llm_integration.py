"""Prueba OPTATIVA contra el proveedor real de OpenAI (TASK-11).

NO corre en la suite habitual (ni en CI sin autorización): requiere
`ADA_LLM_LIVE=1` y una clave real en `OPENAI_API_KEY` (o `ADA_LLM_API_KEY`).
Realiza UNA llamada real al proveedor con costo acotado (`ada_llm_max_tokens`
pequeño): ejecutarla solo con autorización explícita de quien paga la cuenta.
Sin esas variables se salta automáticamente (patrón de las pruebas de
PostgreSQL real con `DATABASE_URL`).
"""

import os

import pytest

from ada_study.config import Settings

pytestmark = pytest.mark.skipif(
    os.getenv("ADA_LLM_LIVE") != "1"
    or not (os.getenv("OPENAI_API_KEY") or os.getenv("ADA_LLM_API_KEY")),
    reason="Optativa: requiere ADA_LLM_LIVE=1 y clave del proveedor (no corre por defecto).",
)


async def test_live_tutor_redacts_explanation_with_openai():
    """Ruta real: el tutor redacta con OpenAI; referencias del retrieval real."""
    from ada_study.index.retriever import ViewerContext
    from ada_study.llm import build_chat_model
    from ada_study.study import run_study
    from tests.test_study_endpoint import FakeStudyRepository, _seed_published_document

    settings = Settings(
        database_url="postgresql+asyncpg://fake",
        ada_llm_provider="openai",
        ada_llm_model=os.getenv("ADA_LLM_MODEL", "gpt-5.4-mini"),
        ada_llm_api_key=os.getenv("ADA_LLM_API_KEY") or os.getenv("OPENAI_API_KEY", ""),
        ada_llm_max_tokens=400,
        ada_llm_timeout_seconds=30.0,
        ada_llm_max_retries=0,
    )
    model = build_chat_model(settings)
    assert model is not None

    repo = FakeStudyRepository()
    _seed_published_document(
        repo,
        "doc_live",
        "autor",
        "La fotosíntesis convierte la energía luminosa en energía química "
        "dentro de los cloroplastos de las células vegetales.",
    )
    response = await run_study(
        repo,
        "¿Qué es la fotosíntesis?",
        ViewerContext(user_id="usr_stu", role="student"),
        chat_model=model,
    )
    assert response.status == "complete"
    assert response.explanation
    assert response.references
    assert [ref.chunk_id for ref in response.references] == ["chk_doc_live_0"]