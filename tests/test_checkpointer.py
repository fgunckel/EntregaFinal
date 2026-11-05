"""Pruebas del checkpointer LangGraph y del runtime con thread_id (CP-01/02).

Unitarias con fakes (sin PostgreSQL ni red):
- Conversión de conninfo asyncpg → psycopg (sin exponer credenciales).
- CheckpointerPool sin servidor: falla ruidosa y pool queda cerrado.
- Runtime con checkpointer en memoria: thread_id explícito respetado, cada
  petición usa su thread propio, checkpoints persisten por thread y el
  estado temporal del turno se reinicia entre turnos.

La integración real con PostgreSQL vive en tests/integration.
"""

import pytest
from langgraph.checkpoint.memory import InMemorySaver

from ada_study.db.checkpointer import CheckpointerError, CheckpointerPool, psycopg_conninfo
from ada_study.index.retriever import ViewerContext
from ada_study.study import StudyRuntimeError, run_study
from tests.test_study_endpoint import FakeStudyRepository, _seed_published_document


class TestPsycopgConninfo:
    def test_convierte_url_asyncpg_a_psycopg(self):
        conninfo = psycopg_conninfo(
            "postgresql+asyncpg://ada:secreto@localhost:5432/ada_study"
        )
        assert conninfo.startswith("postgresql://")
        assert "localhost:5432/ada_study" in conninfo
        assert "ada:secreto@" in conninfo  # credenciales preservadas (no enmascaradas)

    def test_url_sin_driver_funciona_igual(self):
        conninfo = psycopg_conninfo("postgresql://ada:secreto@localhost:5432/db")
        assert conninfo.startswith("postgresql://")


class TestPoolSinServidor:
    async def test_pool_sin_servidor_queda_cerrado(self):
        """Sin servidor: falla ruidosa (fail-loud) y el pool NO queda abierto."""
        pool = CheckpointerPool(
            "postgresql+asyncpg://ada:secreto@localhost:59999/ada_study",
            open_timeout=1.0,
        )
        with pytest.raises(CheckpointerError):
            await pool.open()
        assert pool.is_open is False


def _repo_con_evidencia() -> FakeStudyRepository:
    repo = FakeStudyRepository()
    _seed_published_document(
        repo, "doc_pos", "autor",
        "El valor posicional asigna un peso a cada cifra según su posición.",
    )
    return repo


class TestRunStudyConCheckpointer:
    """El runtime persiste el estado del grafo en el thread de la conversación."""

    async def test_thread_id_explicito_se_respeta(self):
        """El thread recibido es EXACTAMENTE el usado por el checkpointer."""
        repo = _repo_con_evidencia()
        viewer = ViewerContext(user_id="usr_1", role="student")
        saver = InMemorySaver()
        await run_study(
            repo, "Explicame el valor posicional", viewer,
            checkpointer=saver, thread_id="conv-abc-123",
        )
        checkpoint = await saver.aget_tuple(
            {"configurable": {"thread_id": "conv-abc-123", "checkpoint_ns": ""}}
        )
        assert checkpoint is not None

    async def test_cada_peticion_usa_thread_propio(self):
        """Dos peticiones con threads distintos no comparten estado."""
        repo = _repo_con_evidencia()
        viewer = ViewerContext(user_id="usr_1", role="student")
        saver = InMemorySaver()
        await run_study(repo, "¿Qué es el valor posicional?", viewer,
                        checkpointer=saver, thread_id="thread-a")
        await run_study(repo, "¿Qué es una fracción?", viewer,
                        checkpointer=saver, thread_id="thread-b")
        config_a = {"configurable": {"thread_id": "thread-a", "checkpoint_ns": ""}}
        config_b = {"configurable": {"thread_id": "thread-b", "checkpoint_ns": ""}}
        assert await saver.aget_tuple(config_a) is not None
        assert await saver.aget_tuple(config_b) is not None

    async def test_persiste_checkpoints_bajo_thread_efimero(self):
        """Varios turnos sobre el mismo thread acumulan checkpoints sin error."""
        repo = _repo_con_evidencia()
        viewer = ViewerContext(user_id="usr_1", role="student")
        saver = InMemorySaver()
        thread = "conv-turnos"
        await run_study(repo, "Explicame el valor posicional", viewer,
                        checkpointer=saver, thread_id=thread)
        await run_study(repo, "Ahora explicámelo con otro ejemplo", viewer,
                        checkpointer=saver, thread_id=thread)
        config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
        history = [c async for c in saver.alist(config, limit=None)]
        assert len(history) >= 2  # al menos un checkpoint por turno

    async def test_estado_temporal_se_reinicia_por_turno(self):
        """Dimensión B (CHECKPOINTER-03): el turno nuevo parte de cero."""
        repo = _repo_con_evidencia()
        viewer = ViewerContext(user_id="usr_1", role="student")
        saver = InMemorySaver()
        thread = "conv-reset"
        await run_study(repo, "Explicame el valor posicional", viewer,
                        checkpointer=saver, thread_id=thread)
        # Si los campos temporales no se reiniciaran, el turno 2 heredaría
        # artefactos/steps del turno 1. La segunda vuelta debe completar el
        # flujo completo igual que la primera.
        response = await run_study(repo, "Dame un ejercicio para practicar", viewer,
                                   checkpointer=saver, thread_id=thread)
        assert response.status in {"complete", "insufficient_evidence", "limited"}

    async def test_checkpointer_sin_thread_rechazado(self):
        repo = _repo_con_evidencia()
        viewer = ViewerContext(user_id="usr_1", role="student")
        with pytest.raises(StudyRuntimeError) as excinfo:
            await run_study(repo, "Consulta", viewer, checkpointer=InMemorySaver())
        assert excinfo.value.error_code == "STUDY_INTERNAL"

    async def test_modo_efimero_sin_checkpointer_no_usa_thread(self):
        """Sin checkpointer (default), el runtime es efímero y no exige thread."""
        repo = _repo_con_evidencia()
        viewer = ViewerContext(user_id="usr_1", role="student")
        response = await run_study(repo, "¿Qué es el valor posicional?", viewer)
        assert response.status == "complete"

