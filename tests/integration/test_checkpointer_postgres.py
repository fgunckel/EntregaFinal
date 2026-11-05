"""Integración del checkpointer LangGraph con PostgreSQL real (CHECKPOINTER-01).

Requieren PostgreSQL real (DATABASE_URL del proyecto, vía conftest de
tests/integration). NUNCA se toca la base configurada: el pool se abre
contra la base EFÍMERA de la sesión de tests.

Cubre:
- Ciclo de vida del pool: apertura/cierre limpios; lifespan habilita y
  cierra el checkpointer; pool sin servidor queda cerrado (fail-loud).
- Persistencia real: los checkpoints sobreviven al reinicio del proceso
  (cerrar el pool, reabrir y leer el mismo thread).
"""

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult

from ada_study.db.checkpointer import CheckpointerError, CheckpointerPool


class TestCicloDeVidaDelPool:
    async def test_apertura_y_cierre_limpios(self, test_database_url: str):
        pool = CheckpointerPool(test_database_url)
        assert pool.is_open is False
        await pool.open()
        try:
            assert pool.is_open is True
            # setup() dejó el saver listo y las tablas creadas (idempotente).
            saver = pool.checkpointer
            assert saver is not None
        finally:
            await pool.close()
        assert pool.is_open is False

    async def test_pool_sin_servidor_queda_cerrado(self):
        pool = CheckpointerPool(
            "postgresql+asyncpg://ada:secreto@localhost:59999/ada_study",
            open_timeout=1.0,
        )
        with pytest.raises(CheckpointerError):
            await pool.open()
        assert pool.is_open is False

    async def test_lifespan_habilita_y_cierra_el_checkpointer(self, test_database_url, tmp_path):
        """Con ADA_CONVERSATIONS_ENABLED=true el lifespan abre y cierra el pool."""
        from ada_study.api.app import create_app
        from ada_study.config import Settings

        settings = Settings(
            database_url=test_database_url,
            ada_storage_dir=str(tmp_path / "storage"),
            ada_conversations_enabled=True,
            _env_file=None,
        )
        app = create_app(settings=settings)
        async with app.router.lifespan_context(app):
            assert app.state.ada.checkpointer is not None
            saver = app.state.ada.checkpointer
        # Tras el lifespan, el pool quedó cerrado (shutdown limpio).
        assert getattr(saver.conn, "closed", False) is True


class TestPersistenciaReal:
    async def test_checkpoints_sobreviven_al_reinicio(self, test_database_url):
        """El checkpoint persiste tras cerrar y reabrir el pool (reinicio)."""
        from ada_study.index.retriever import ViewerContext
        from ada_study.study import run_study
        from tests.test_study_endpoint import FakeStudyRepository

        thread = "conv-reinicio"
        repo = FakeStudyRepository()
        from tests.test_study_endpoint import _seed_published_document as _seed

        _seed(repo, "doc_pos", "autor", "El valor posicional asigna peso a cada cifra.")
        viewer = ViewerContext(user_id="usr_1", role="student")

        # "Proceso" 1: abre pool, ejecuta un turno, cierra (reinicio).
        pool_1 = CheckpointerPool(test_database_url)
        await pool_1.open()
        try:
            await run_study(
                repo, "Explicame el valor posicional", viewer,
                checkpointer=pool_1.checkpointer, thread_id=thread,
            )
        finally:
            await pool_1.close()
        assert pool_1.is_open is False

        # "Proceso" 2: reabre el pool sobre la misma base y LEE el thread.
        config = {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
        pool_2 = CheckpointerPool(test_database_url)
        await pool_2.open()
        try:
            checkpoint = await pool_2.checkpointer.aget_tuple(config)
            assert checkpoint is not None
            values = checkpoint.checkpoint["channel_values"]
            assert values.get("task_completed") is True
        finally:
            await pool_2.close()


# Sin servidor real disponible: el test de pool cerrado corre SIEMPRE
# (puerto cerrado garantizado), incluso en entornos sin DATABASE_URL.
class TestPoolSinConexion:
    async def test_apertura_fallida_queda_cerrada(self):
        pool = CheckpointerPool(
            "postgresql+asyncpg://ada:secreto@localhost:59999/ada_study",
            open_timeout=1.0,
        )
        with pytest.raises(CheckpointerError):
            await pool.open()
        assert pool.is_open is False


# ---------------------------------------------------------------------------
# CHECKPOINTER-03 §7: continuidad conversacional tras reiniciar FastAPI
# ---------------------------------------------------------------------------

_T1 = "Explicame el valor posicional"
_T2 = "Dame otro ejemplo"
_TEXTO = "El valor posicional asigna peso a cada cifra según su posición."


class FakeTutorModel(BaseChatModel):
    """Fake determinístico del tutor (sin proveedor real); registra prompts."""

    calls: list = []

    @property
    def _llm_type(self) -> str:
        return "fake-tutor"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls.append(str(messages[-1].content) if messages else "")
        texto = f"Explicación del modelo #{len(self.calls)}."
        return ChatResult(generations=[ChatGeneration(message=AIMessage(content=texto))])


class TestContinuidadTrasReinicio:
    async def test_historial_pedagogico_sobrevive_reinicio_fastapi(
        self, test_database_url, tmp_path
    ):
        """1) crear conversación; 2) primer turno; 3) cerrar la app; 4) nueva
        instancia de FastAPI; 5) segundo turno con el mismo conversation_id;
        6) el historial anterior se recupera del checkpointer PostgreSQL; 7)
        el Tutor recibe el contexto anterior; 8) el Researcher ejecuta una
        recuperación nueva; 9) la respuesta final se agrega una sola vez."""
        import httpx

        from ada_study.api.app import AdaState, create_app
        from ada_study.config import Settings
        from ada_study.domain.conversation_service import thread_id_for
        from ada_study.storage.pdf_storage import PdfStorage
        from tests.test_api_auth import _user
        from tests.test_conversations import FakeConversationRepository, _registered_session
        from tests.test_study_endpoint import _seed_published_document

        repo = FakeConversationRepository()
        _seed_published_document(repo, "doc_pos", "autor", _TEXTO)
        token = _registered_session(repo, _user("usr_a", "student"))
        headers = {"Authorization": f"Bearer {token}"}

        settings = Settings(
            database_url=test_database_url,
            ada_storage_dir=str(tmp_path / "storage"),
            ada_conversations_enabled=True,
            _env_file=None,
        )

        async def turno(model, pool, body):
            def state_factory(resolved):
                return AdaState(
                    engine=None,
                    repository=repo,
                    storage=PdfStorage(root=tmp_path / "storage"),
                    chat_model=model,
                    checkpointer=pool.checkpointer,
                )

            app = create_app(settings, state_factory=state_factory)
            transport = httpx.ASGITransport(app=app)
            async with app.router.lifespan_context(app):
                async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                    return await client.post("/api/study", json=body, headers=headers)

        # Instancia 1: turno 1, cierre completo de la app y del pool.
        model_1 = FakeTutorModel()
        pool_1 = CheckpointerPool(test_database_url)
        await pool_1.open()
        try:
            primera = await turno(model_1, pool_1, {"user_request": _T1})
        finally:
            await pool_1.close()
        assert primera.status_code == 200
        cid = primera.json()["conversation_id"]
        explicacion_1 = primera.json()["explanation"]

        # Instancia 2 (NUEVA app + NUEVO pool sobre la misma base PostgreSQL):
        # segundo turno con el MISMO conversation_id.
        model_2 = FakeTutorModel()
        pool_2 = CheckpointerPool(test_database_url)
        await pool_2.open()
        try:
            segunda = await turno(
                model_2, pool_2, {"user_request": _T2, "conversation_id": cid}
            )
            assert segunda.status_code == 200
            assert segunda.json()["conversation_id"] == cid

            # El historial completo persiste en PostgreSQL (thread estable).
            config = {
                "configurable": {"thread_id": thread_id_for(cid), "checkpoint_ns": ""}
            }
            checkpoint = await pool_2.checkpointer.aget_tuple(config)
            assert checkpoint is not None
            values = checkpoint.checkpoint["channel_values"]
            history = values["conversation_history"]
            assert [entry["role"] for entry in history] == [
                "student", "ada", "student", "ada",
            ]
            assert history[0]["content"] == _T1
            assert history[1]["content"] == explicacion_1
            assert history[2]["content"] == _T2
            # Respuesta final agregada UNA sola vez (sin duplicados).
            assert history[3]["content"] == segunda.json()["explanation"]
            assert len(history) == 4
            # Cada mensaje del alumno aparece exactamente una vez (sin
            # duplicar el intake ni mezclar turnos).
            estudiantes = [e["content"] for e in history if e["role"] == "student"]
            assert estudiantes == [_T1, _T2]

            # El Tutor de la instancia 2 recibió el contexto del turno anterior.
            assert len(model_2.calls) == 1
            assert "Historial conversacional" in model_2.calls[0]
            assert _T1 in model_2.calls[0]

            # El Researcher del turno 2 ejecutó una recuperación NUEVA (con
            # reformulación contextual), no reutilizó la del turno 1.
            artifact = values["research_artifact"]
            assert artifact["query"] == _T2
            assert artifact["retrieval_query"].startswith(_T2)
            assert artifact["retrieval_query"] != artifact["query"]
        finally:
            await pool_2.close()
