"""Pruebas de integración FIX-TC06: persistencia conversacional con PostgreSQL real.

Cubren los TEST 1-5 de la SPEC FIX-TC06 contra la MISMA base PostgreSQL que
usa la API (sin fakes de repositorio ni checkpointer en memoria):

- TEST 1 — Creación: `conversation_id` + `thread_id` vinculados en la fila
  de `conversations`.
- TEST 2 — Continuidad: dos turnos con el mismo `conversation_id`, mismo
  `thread_id`, `turn_count` incrementa, historial previo recuperado del
  checkpointer.
- TEST 3 — Persistencia tras reinicio: se cierran y reconstruyen el engine y
  el `CheckpointerPool` (simulando el reinicio de FastAPI); la conversación
  continúa con el MISMO hilo, genera un checkpoint nuevo y NO crea una
  conversación nueva.
- TEST 4 — Aislamiento: un usuario no puede continuar la conversación de
  otro (CONVERSATION_NOT_FOUND, sin filtrar existencia).
- TEST 5 — Conversación nueva: identificadores diferentes y sin estado de
  otra conversación.

Sin LLM real: `chat_model=None` (tutor determinístico del grafo) — ninguna
llamada a Groq u otro proveedor. Phoenix no se inicializa (los spans no son
objeto de estas pruebas).

Marcadas como `integration` (ver pyproject.toml). Se saltan automáticamente
si DATABASE_URL no está disponible (sin PostgreSQL levantado).

Ejecutar:   pytest tests/test_tc06_checkpointer_postgres.py -v
Excluir:    pytest -m "not integration"
"""

import asyncio
import uuid
from types import SimpleNamespace

import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ada_study.config import Settings
from ada_study.db.checkpointer import CheckpointerPool
from ada_study.db.repository import CatalogRepository
from ada_study.domain.conversation_service import (
    CONVERSATION_NOT_FOUND,
    ConversationError,
    ConversationService,
)
from ada_study.index.retriever import ViewerContext
from ada_study.study import run_study

# Consultas del corpus real del proyecto (doc_hist); el estado de la respuesta
# no se aserta: estas pruebas verifican PERSISTENCIA, no calidad RAG.
QUERY_1 = "¿Qué temas se estudian en Historia de quinto grado?"
QUERY_2 = "¿Y en qué documento encontraste esa información?"
QUERY_OTRA = "¿Cuánto vale el cuatro en el número 345?"


def _database_url() -> str:
    """URL real del proyecto (.env o entorno); vacía si no está disponible."""
    try:
        return Settings(_env_file=".env").database_url
    except Exception:  # noqa: BLE001 - frontera: sin configuración → skip
        return ""


def _postgres_available() -> bool:
    """True solo si PostgreSQL responde (con timeout corto)."""

    async def _ping() -> bool:
        engine = create_async_engine(_database_url())
        try:
            async with engine.connect() as conn:
                await conn.execute(text("SELECT 1"))
            return True
        except Exception:  # noqa: BLE001 - frontera: BD ausente → skip
            return False
        finally:
            await engine.dispose()

    if not _database_url():
        return False
    global _PG_ERROR
    try:
        return asyncio.run(asyncio.wait_for(_ping(), timeout=5.0))
    except Exception as exc:  # noqa: BLE001 - frontera: BD colgada → skip
        _PG_ERROR = f"{type(exc).__name__}: {exc}"
        return False


_PG_ERROR = ""
PG_OK = _postgres_available()

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not PG_OK, reason="PostgreSQL real no disponible (DATABASE_URL/servicio caído)"
    ),
]


def _user() -> str:
    """Usuario único por prueba: sin colisiones ni datos ajenos."""
    return f"usr_tc06_{uuid.uuid4().hex[:10]}"


def _viewer(user_id: str) -> ViewerContext:
    return ViewerContext(user_id=user_id, role="student")


@pytest.fixture()
async def stack():
    """Engine + CheckpointerPool REALES; ciclo de vida cerrado al terminar."""
    url = _database_url()
    engine = create_async_engine(url, echo=False)
    pool = CheckpointerPool(url)
    await pool.open()
    yield SimpleNamespace(engine=engine, pool=pool, repository=CatalogRepository(engine=engine))
    await pool.close()
    await engine.dispose()


async def _checkpoint_count(engine, thread_id: str) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT count(*) FROM checkpoints WHERE thread_id = :t"),
                {"t": thread_id},
            )
        ).scalar_one()


async def _conversation_rows(engine, conversation_id: str) -> int:
    async with engine.connect() as conn:
        return (
            await conn.execute(
                text("SELECT count(*) FROM conversations WHERE conversation_id = :c"),
                {"c": conversation_id},
            )
        ).scalar_one()


async def _saved_history(pool: CheckpointerPool, thread_id: str) -> list:
    """Historial pedagógico checkpointeado (lo que recuperaría un reinicio)."""
    checkpoint = await pool.checkpointer.aget_tuple(
        {"configurable": {"thread_id": thread_id}}
    )
    if checkpoint is None:
        return []
    values = checkpoint.checkpoint.get("channel_values", {})
    history = values.get("conversation_history") or []
    return history if isinstance(history, list) else []


# ---------------------------------------------------------------------------
# TEST 1 — Creación
# ---------------------------------------------------------------------------


async def test_tc06_1_creacion_registra_fila(stack):
    user = _user()
    service = ConversationService(stack.repository)

    record = await service.resolve_conversation(user, None)

    assert record.conversation_id  # UUID generado por el servidor
    assert record.thread_id == f"conv-{record.conversation_id}"
    row = await stack.repository.get_conversation(record.conversation_id)
    assert row is not None
    assert row.user_id == user
    assert row.thread_id == record.thread_id
    assert row.turn_count == 0


# ---------------------------------------------------------------------------
# TEST 2 — Continuidad (mismo conversation_id, mismo thread, turn_count, historial)
# ---------------------------------------------------------------------------


async def test_tc06_2_continuidad_mismo_hilo_y_historial(stack):
    user = _user()
    service = ConversationService(stack.repository)
    conv = await service.resolve_conversation(user, None)

    await run_study(
        stack.repository, QUERY_1, _viewer(user),
        chat_model=None,
        checkpointer=stack.pool.checkpointer, thread_id=conv.thread_id,
    )
    await service.record_turn(conv.conversation_id)

    checkpoints_turno_1 = await _checkpoint_count(stack.engine, conv.thread_id)
    assert checkpoints_turno_1 >= 1
    history_turno_1 = await _saved_history(stack.pool, conv.thread_id)
    assert any(QUERY_1[:30] in str(entry) for entry in history_turno_1)

    # Segundo turno con el MISMO conversation_id (como hace Streamlit).
    await run_study(
        stack.repository, QUERY_2, _viewer(user),
        chat_model=None,
        checkpointer=stack.pool.checkpointer, thread_id=conv.thread_id,
    )
    await service.record_turn(conv.conversation_id)

    row = await stack.repository.get_conversation(conv.conversation_id)
    assert row.turn_count == 2
    history_turno_2 = await _saved_history(stack.pool, conv.thread_id)
    joined = str(history_turno_2)
    # Estado anterior recuperado: la pregunta del turno 1 sigue en el hilo.
    assert QUERY_1[:30] in joined
    assert QUERY_2[:30] in joined
    assert await _checkpoint_count(stack.engine, conv.thread_id) > checkpoints_turno_1


# ---------------------------------------------------------------------------
# TEST 3 — Persistencia después de reinicio (PostgreSQL real, obligatorio)
# ---------------------------------------------------------------------------


async def test_tc06_3_recupera_estado_tras_reinicio_del_proceso():
    user = _user()
    url = _database_url()

    # ---- Fase A: proceso "original" (como el primer uvicorn) --------------
    engine_a = create_async_engine(url, echo=False)
    pool_a = CheckpointerPool(url)
    await pool_a.open()
    repo_a = CatalogRepository(engine_a)
    conv = await ConversationService(repo_a).resolve_conversation(user, None)
    await run_study(
        repo_a, QUERY_1, _viewer(user),
        chat_model=None,
        checkpointer=pool_a.checkpointer, thread_id=conv.thread_id,
    )
    await ConversationService(repo_a).record_turn(conv.conversation_id)
    checkpoints_fase_a = await _checkpoint_count(engine_a, conv.thread_id)

    # Reinicio real de los recursos del proceso: el estado vive SOLO en
    # PostgreSQL a partir de acá (nada en memoria cruza la frontera).
    await pool_a.close()
    await engine_a.dispose()

    # ---- Fase B: proceso "reconstruido" (pools NUEVOS, MISMA base) --------
    engine_b = create_async_engine(url, echo=False)
    pool_b = CheckpointerPool(url)
    await pool_b.open()
    repo_b = CatalogRepository(engine_b)
    service_b = ConversationService(repo_b)

    # El endpoint resolvería la conversación existente por su id:
    conv_recovered = await service_b.resolve_conversation(user, conv.conversation_id)
    assert conv_recovered.conversation_id == conv.conversation_id
    assert conv_recovered.thread_id == conv.thread_id  # mismo hilo
    # NO se creó una conversación nueva:
    assert await _conversation_rows(engine_b, conv.conversation_id) == 1

    # Estado previo recuperado desde PostgreSQL:
    history = await _saved_history(pool_b, conv_recovered.thread_id)
    assert any(QUERY_1[:30] in str(entry) for entry in history)

    # La conversación CONTINÚA en el mismo hilo y genera un checkpoint nuevo:
    await run_study(
        repo_b, QUERY_2, _viewer(user),
        chat_model=None,
        checkpointer=pool_b.checkpointer, thread_id=conv_recovered.thread_id,
    )
    await service_b.record_turn(conv_recovered.conversation_id)

    assert await _checkpoint_count(engine_b, conv_recovered.thread_id) > checkpoints_fase_a
    row = await repo_b.get_conversation(conv_recovered.conversation_id)
    assert row.turn_count == 2  # el contador continúa, no reinicia

    await pool_b.close()
    await engine_b.dispose()


# ---------------------------------------------------------------------------
# TEST 4 — Aislamiento entre usuarios
# ---------------------------------------------------------------------------


async def test_tc06_4_aislamiento_entre_usuarios(stack):
    propietario = _user()
    intruso = _user()
    service = ConversationService(stack.repository)
    conv = await service.resolve_conversation(propietario, None)

    with pytest.raises(ConversationError) as excinfo:
        await service.resolve_conversation(intruso, conv.conversation_id)
    # Mismo código para ajena e inexistente (nunca se filtra existencia).
    assert excinfo.value.error_code == CONVERSATION_NOT_FOUND


# ---------------------------------------------------------------------------
# TEST 5 — Conversación nueva: ids distintos, sin estado ajeno
# ---------------------------------------------------------------------------


async def test_tc06_5_conversacion_nueva_sin_estado_ajeno(stack):
    user = _user()
    service = ConversationService(stack.repository)
    conv_a = await service.resolve_conversation(user, None)
    await run_study(
        stack.repository, QUERY_1, _viewer(user),
        chat_model=None,
        checkpointer=stack.pool.checkpointer, thread_id=conv_a.thread_id,
    )
    await service.record_turn(conv_a.conversation_id)

    conv_b = await service.resolve_conversation(user, None)
    assert conv_b.conversation_id != conv_a.conversation_id
    assert conv_b.thread_id != conv_a.thread_id

    # El hilo nuevo NO arrastró el estado de la conversación anterior:
    history_b_before = await _saved_history(stack.pool, conv_b.thread_id)
    assert history_b_before == []

    await run_study(
        stack.repository, QUERY_OTRA, _viewer(user),
        chat_model=None,
        checkpointer=stack.pool.checkpointer, thread_id=conv_b.thread_id,
    )
    history_b = await _saved_history(stack.pool, conv_b.thread_id)
    assert any(QUERY_OTRA[:30] in str(entry) for entry in history_b)
    assert not any(QUERY_1[:30] in str(entry) for entry in history_b)
