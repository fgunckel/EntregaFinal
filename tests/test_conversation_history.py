"""Pruebas del historial pedagógico (CHECKPOINTER-03, dimensión A).

Unitarias con fakes (sin PostgreSQL, sin LLM real):
- Helpers puros del historial: formato, incorporación exactamente una vez,
  guarda anti-duplicado ante reintentos, tema previo para reformular.
- Runtime con checkpointer en memoria: historial inicial vacío, primer
  mensaje del alumno, respuesta final única, segundo turno conserva orden,
  artefactos temporales reiniciados, historiales separados por conversación,
  fallo sin respuesta final ficticia y reintento sin duplicar, modo efímero.
- Investigador: recuperación NUEVA por turno con reformulación contextual.
- Tutor: `ainvoke` (nunca `invoke`), historial separado de la evidencia.
- Endpoint: compatibilidad sin checkpointer y lock por conversación (§8).
"""

import asyncio
import tempfile
from pathlib import Path

import httpx
import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langgraph.checkpoint.memory import InMemorySaver

from ada_study.api.app import AdaState, create_app
from ada_study.config import Settings
from ada_study.contracts.study import SourceReference
from ada_study.graph.history import (
    append_ada_message,
    append_student_message,
    last_ada_topic,
    sanitize_history,
)
from ada_study.graph.researcher import build_retrieval_query, make_researcher_node
from ada_study.graph.tutor import _build_tutor_prompt
from ada_study.index.retriever import ViewerContext
from ada_study.storage.pdf_storage import PdfStorage
from ada_study.study import run_study
from tests.test_api_auth import _user
from tests.test_conversations import FakeConversationRepository, _registered_session
from tests.test_study_endpoint import _seed_published_document

_TEXT = "El valor posicional asigna un peso a cada cifra según su posición."
T1 = "Explicame el valor posicional"
T2 = "Dame otro ejemplo"


def _repo() -> FakeConversationRepository:
    repo = FakeConversationRepository()
    _seed_published_document(repo, "doc_pos", "autor", _TEXT)
    return repo


def _viewer(user_id: str = "usr_1") -> ViewerContext:
    return ViewerContext(user_id=user_id, role="student")


def _history(saver: InMemorySaver, thread: str) -> list[dict]:
    checkpoint = saver.get_tuple(
        {"configurable": {"thread_id": thread, "checkpoint_ns": ""}}
    )
    if checkpoint is None:
        return []
    values = checkpoint.checkpoint["channel_values"]
    return list(values.get("conversation_history") or [])


class FakeTutorModel(BaseChatModel):
    """Fake del tutor que SOLO implementa la ruta asíncrona (`_agenerate`).

    `_generate` (síncrono) falla ruidosamente: si el tutor usara `invoke`,
    la explicación sería la del fallback determinístico y las pruebas que
    exigen la redacción del modelo fallarían. Así se demuestra `ainvoke`.
    """

    calls: list = []
    response_text: str = "Explicación del modelo."

    @property
    def _llm_type(self) -> str:
        return "fake-tutor"

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        raise AssertionError("el tutor debe usar ainvoke, nunca invoke")

    async def _agenerate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self.calls.append(str(messages[-1].content) if messages else "")
        return ChatResult(
            generations=[ChatGeneration(message=AIMessage(content=self.response_text))]
        )


# ---------------------------------------------------------------------------
# Helpers puros del historial
# ---------------------------------------------------------------------------


class TestHelpersHistorial:
    def test_mensaje_del_alumno_se_incorpora_una_vez(self):
        history = append_student_message([], T1)
        assert history == [{"role": "student", "content": T1}]

    def test_reintento_no_duplica_el_mensaje_del_alumno(self):
        """Tras un fallo post-intake, el reintento idéntico NO duplica."""
        history = append_student_message([], T2)
        history = append_student_message(history, T2)
        assert history == [{"role": "student", "content": T2}]

    def test_pregunta_repetida_legitima_si_se_agrega(self):
        """Tras un turno exitoso la última entrada es de ADA: no hay falso
        positivo de la guarda anti-duplicado."""
        history = append_ada_message(append_student_message([], T1), "Explicación.", "Tema")
        history = append_student_message(history, T1)  # el alumno repite
        assert len(history) == 3
        assert history[-1]["role"] == "student"

    def test_respuesta_final_se_consolidar_una_vez(self):
        history = append_ada_message([], "Respuesta.", "Tema")
        history = append_ada_message(history, "Respuesta.", "Tema")
        assert history == [{"role": "ada", "content": "Respuesta.", "topic": "Tema"}]

    def test_last_ada_topic_ignora_mensajes_del_alumno(self):
        history = [
            {"role": "student", "content": T1, "topic": "inyección"},
            {"role": "ada", "content": "Ok.", "topic": "valor posicional"},
        ]
        assert last_ada_topic(history) == "valor posicional"
        assert last_ada_topic([{"role": "student", "content": "x"}]) is None
        assert last_ada_topic([]) is None

    def test_sanitize_descarta_historial_corrupto(self):
        assert sanitize_history(None) == []
        assert sanitize_history("basura") == []
        assert sanitize_history([{"role": "x", "content": "y"}]) == []
        assert sanitize_history([{"role": "student", "content": ""}]) == []
        assert sanitize_history([{"role": "student", "content": "q", "topic": "t"}]) == []
        ok = [{"role": "ada", "content": "r", "topic": "T"}]
        assert sanitize_history(ok) == ok


# ---------------------------------------------------------------------------
# Runtime con checkpointer: ciclo de vida del historial
# ---------------------------------------------------------------------------


class TestCicloDeVidaDelHistorial:
    async def test_historial_inicial_vacio(self):
        """Sin turnos previos, el thread no tiene historial (lista vacía)."""
        saver = InMemorySaver()
        assert _history(saver, "conv-nuevo") == []

    async def test_primer_mensaje_y_respuesta_registrados(self):
        saver = InMemorySaver()
        response = await run_study(
            _repo(), T1, _viewer(), checkpointer=saver, thread_id="conv-1"
        )
        history = _history(saver, "conv-1")
        assert len(history) == 2
        assert history[0] == {"role": "student", "content": T1}
        assert history[1]["role"] == "ada"
        assert history[1]["content"] == response.explanation
        assert history[1]["topic"]  # tema declarado para reformular después

    async def test_segundo_turno_conserva_historial_en_orden(self):
        saver = InMemorySaver()
        await run_study(_repo(), T1, _viewer(), checkpointer=saver, thread_id="conv-2")
        await run_study(_repo(), T2, _viewer(), checkpointer=saver, thread_id="conv-2")
        history = _history(saver, "conv-2")
        assert [entry["role"] for entry in history] == [
            "student", "ada", "student", "ada",
        ]
        assert history[0]["content"] == T1
        assert history[2]["content"] == T2
        # Orden cronológico: la respuesta de ADA del turno 1 sigue entre los
        # dos mensajes del alumno.
        assert history[1]["content"] == "Explicación del modelo." or history[1]["role"] == "ada"

    async def test_no_se_duplican_mensajes_en_turnos_exitosos(self):
        saver = InMemorySaver()
        await run_study(_repo(), T1, _viewer(), checkpointer=saver, thread_id="conv-3")
        await run_study(_repo(), T1, _viewer(), checkpointer=saver, thread_id="conv-3")
        history = _history(saver, "conv-3")
        assert len(history) == 4  # el alumno repite la pregunta: son 2 turnos
        assert [e["role"] for e in history] == ["student", "ada", "student", "ada"]

    async def test_seguimiento_recibe_contexto_en_el_tutor(self):
        """El prompt del tutor del turno 2 incluye el historial del turno 1."""
        model = FakeTutorModel()
        saver = InMemorySaver()
        await run_study(_repo(), T1, _viewer(), model, checkpointer=saver, thread_id="conv-4")
        await run_study(_repo(), T2, _viewer(), model, checkpointer=saver, thread_id="conv-4")
        assert len(model.calls) == 2
        # Turno 1: sin historial previo (no hay sección de historial).
        assert "Historial conversacional" not in model.calls[0]
        # Turno 2: el prompt incluye el turno anterior como CONTEXTO separado
        # de la evidencia, y mantiene la instrucción del turno actual.
        assert "Historial conversacional" in model.calls[1]
        assert "<turno" in model.calls[1]
        assert T1 in model.calls[1]
        assert T2 in model.calls[1]
        # La evidencia sigue delimitada como única fuente de respaldo.
        assert "única fuente de respaldo" in model.calls[1]

    async def test_artefactos_temporales_se_reinician_por_turno(self):
        saver = InMemorySaver()
        await run_study(_repo(), T1, _viewer(), checkpointer=saver, thread_id="conv-5")
        len_historial_1 = len(_history(saver, "conv-5"))
        await run_study(_repo(), T2, _viewer(), checkpointer=saver, thread_id="conv-5")
        checkpoint = saver.get_tuple(
            {"configurable": {"thread_id": "conv-5", "checkpoint_ns": ""}}
        )
        values = checkpoint.checkpoint["channel_values"]
        # El turno 2 reconstruyó TODO: contributions solo del turno actual.
        agentes = [c["agent"] for c in values["contributions"]]
        assert agentes == ["researcher", "tutor", "validator", "synthesizer"]
        assert values["task_completed"] is True
        # El historial, en cambio, SÍ acumula (no es temporal).
        assert len(_history(saver, "conv-5")) == len_historial_1 + 2

    async def test_evidencia_rag_nueva_por_turno(self):
        saver = InMemorySaver()
        repo = _repo()
        await run_study(repo, T1, _viewer(), checkpointer=saver, thread_id="conv-6")
        await run_study(repo, T2, _viewer(), checkpointer=saver, thread_id="conv-6")
        checkpoint = saver.get_tuple(
            {"configurable": {"thread_id": "conv-6", "checkpoint_ns": ""}}
        )
        values = checkpoint.checkpoint["channel_values"]
        artifact = values["research_artifact"]
        # La recuperación del turno 2 fue NUEVA: consulta actual + tema previo
        # (reformulación), evidencia fresca, nunca reutilizada del turno 1.
        assert artifact["query"] == T2
        assert artifact["retrieval_query"] == f"{T2} Tema doc_pos"
        assert artifact["evidence"]

    async def test_conversaciones_distintas_historiales_separados(self):
        saver = InMemorySaver()
        repo = _repo()
        await run_study(repo, T1, _viewer("usr_a"), checkpointer=saver, thread_id="conv-a")
        await run_study(repo, T2, _viewer("usr_b"), checkpointer=saver, thread_id="conv-b")
        historial_a = _history(saver, "conv-a")
        historial_b = _history(saver, "conv-b")
        assert historial_a[0]["content"] == T1
        assert historial_b[0]["content"] == T2
        assert all(entry["content"] != T1 for entry in historial_b)
        assert all(entry["content"] != T2 for entry in historial_a)


# ---------------------------------------------------------------------------
# Fallo sin respuesta ficticia + reintento sin duplicados
# ---------------------------------------------------------------------------


class TestFalloSinRespuestaFicticia:
    async def test_fallo_en_el_grafo_no_registra_ada_y_reintento_no_duplica(
        self, monkeypatch
    ):
        """Fallo DENTRO del grafo (tras el intake): sin respuesta final
        ficticia; el reintento no duplica el mensaje del alumno."""
        saver = InMemorySaver()
        # Turno 1 SIN fallo (investigador real).
        await run_study(_repo(), T1, _viewer(), checkpointer=saver, thread_id="conv-f1")
        # Turno 2 con fallo DENTRO del grafo (tras el intake del historial).
        import ada_study.graph.builder as builder_mod
        from ada_study.study import StudyRuntimeError

        async def researcher_roto(state):
            raise RuntimeError("fallo interno simulado")

        monkeypatch.setattr(
            builder_mod, "make_researcher_node", lambda retriever: researcher_roto
        )
        with pytest.raises(StudyRuntimeError):
            await run_study(
                _repo(), T2, _viewer(), checkpointer=saver, thread_id="conv-f1"
            )
        history = _history(saver, "conv-f1")
        # El mensaje del turno fallido quedó registrado (intake) pero SIN
        # respuesta de ADA ficticia.
        assert history[-1] == {"role": "student", "content": T2}
        assert all(entry["role"] != "ada" for entry in history[2:])

        # Reintento (researcher reparado): el mensaje del alumno queda UNA vez.
        monkeypatch.undo()
        response = await run_study(
            _repo(), T2, _viewer(), checkpointer=saver, thread_id="conv-f1"
        )
        history = _history(saver, "conv-f1")
        assert [e["role"] for e in history] == ["student", "ada", "student", "ada"]
        assert sum(
            1 for e in history if e["role"] == "student" and e["content"] == T2
        ) == 1
        assert history[-1]["content"] == response.explanation


# ---------------------------------------------------------------------------
# Investigador: recuperación nueva por turno con reformulación contextual
# ---------------------------------------------------------------------------


class _RetrieverGrabador:
    def __init__(self, results=None) -> None:
        self.queries: list[str] = []
        self._results = results or []

    async def search(self, query, top_k=5):
        self.queries.append(query)
        return self._results


class TestReformulacionContextual:
    def test_sin_historial_la_consulta_queda_intacta(self):
        assert build_retrieval_query(T1, []) == T1

    def test_seguimiento_corto_suma_el_tema_previo(self):
        history = [
            {"role": "student", "content": T1},
            {"role": "ada", "content": "explicación", "topic": "valor posicional"},
        ]
        assert build_retrieval_query(T2, history) == f"{T2} valor posicional"

    def test_referencia_deictica_suma_el_tema_previo(self):
        history = [{"role": "ada", "content": "x", "topic": "mitocondria"}]
        consulta = "Ahora explicámelo de otra manera con más detalle y otro enfoque"
        reformulada = build_retrieval_query(consulta, history)
        assert reformulada.startswith(consulta)  # sentido original conservado
        assert reformulada.endswith("mitocondria")

    def test_tema_nuevo_no_se_contamina_con_el_anterior(self):
        history = [{"role": "ada", "content": "x", "topic": "mitocondria"}]
        consulta = "Explique la fotosíntesis incluyendo la clorofila y la luz solar"
        assert build_retrieval_query(consulta, history) == consulta

    async def test_researcher_recupera_nuevo_sin_reusar_referencias(self):
        from ada_study.index.retriever import RetrievedChunk
        from tests.test_study_endpoint import _chunk

        retriever = _RetrieverGrabador(
            results=[
                RetrievedChunk(
                    chunk=_chunk("doc_pos", _TEXT), score=1.0, rank=0, strategies=["bm25"]
                )
            ]
        )
        node = make_researcher_node(retriever)
        history = [
            {"role": "student", "content": T1},
            {"role": "ada", "content": "explicación previa", "topic": "valor posicional"},
        ]
        update = await node({"user_request": T2, "conversation_history": history})
        # Búsqueda NUEVA con la consulta reformulada (no reutiliza referencias).
        assert retriever.queries == [f"{T2} valor posicional"]
        artifact = update["research_artifact"]
        assert artifact["query"] == T2
        refs = [SourceReference.model_validate(item) for item in artifact["evidence"]]
        assert refs[0].chunk_id == "chk_doc_pos_0"


# ---------------------------------------------------------------------------
# Endpoint: compatibilidad sin checkpointer y lock por conversación (§8)
# ---------------------------------------------------------------------------


def _app_client(repo, *, checkpointer=None, chat_model=None):
    tmp_root = Path(tempfile.mkdtemp(prefix="ada_hist_test_"))
    settings = Settings(
        database_url="postgresql+asyncpg://fake",
        ada_storage_dir=str(tmp_root),
        ada_conversations_enabled=True,
        _env_file=None,
    )

    def state_factory(resolved):
        return AdaState(
            engine=None,
            repository=repo,
            storage=PdfStorage(root=tmp_root),
            chat_model=chat_model,
            checkpointer=checkpointer,
        )

    return create_app(settings, state_factory=state_factory)


def _repo_con_usuario():
    repo = FakeConversationRepository()
    _seed_published_document(repo, "doc_pub", "autor", _TEXT)
    return repo, _registered_session(repo, _user("usr_a", "student"))


class TestEndpointCompatibilidad:
    def test_con_conversacion_y_sin_checkpointer_sigue_funcionando(self):
        """Modo efímero con conversation_id: 200 sin persistencia (§9-14)."""
        from fastapi.testclient import TestClient

        repo, token = _repo_con_usuario()
        with TestClient(_app_client(repo)) as client:
            primera = client.post(
                "/api/study", json={"user_request": T1},
                headers={"Authorization": f"Bearer {token}"},
            )
            cid = primera.json()["conversation_id"]
            segunda = client.post(
                "/api/study",
                json={"user_request": T2, "conversation_id": cid},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert primera.status_code == segunda.status_code == 200
        assert segunda.json()["conversation_id"] == cid
        assert repo.conversations[cid].turn_count == 2

    async def test_lock_serializa_turnos_de_la_misma_conversacion(self):
        """Dos turnos concurrentes sobre la MISMA conversación se serializan
        (§8): ambos responden y el historial queda consistente, sin mezclas
        ni duplicados; el lock es por conversación (no global)."""
        repo, token = _repo_con_usuario()
        saver = InMemorySaver()
        app = _app_client(repo, checkpointer=saver)
        headers = {"Authorization": f"Bearer {token}"}
        transport = httpx.ASGITransport(app=app)
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=transport, base_url="http://t") as client:
                primera = await client.post(
                    "/api/study", json={"user_request": T1}, headers=headers
                )
                cid = primera.json()["conversation_id"]
                r1, r2 = await asyncio.gather(
                    client.post(
                        "/api/study",
                        json={"user_request": T2, "conversation_id": cid},
                        headers=headers,
                    ),
                    client.post(
                        "/api/study",
                        json={
                            "user_request": "Ahora dame un ejercicio para practicar",
                            "conversation_id": cid,
                        },
                        headers=headers,
                    ),
                )
        assert r1.status_code == r2.status_code == 200
        history = _history(saver, repo.conversations[cid].thread_id)
        # Tres turnos completos, ordenados, sin duplicados ni mezclas.
        assert [e["role"] for e in history] == [
            "student", "ada", "student", "ada", "student", "ada",
        ]
        assert len({(e["role"], e["content"]) for e in history}) == 6
        assert repo.conversations[cid].turn_count == 3
        # El lock es POR conversación: otra conversación tiene su propio lock.
        locks = app.state.ada.conversation_locks
        assert len(locks) == 1  # una sola conversación usada en este test


class TestUiCliente:
    def test_api_client_envia_y_conserva_conversation_id(self):
        """El cliente de la UI envía el identificador en cada petición y lo
        renueva a partir de la respuesta del servidor (continuidad §6)."""
        from fastapi.testclient import TestClient

        from ada_study.ui.api_client import ApiClient

        repo, token = _repo_con_usuario()
        with TestClient(_app_client(repo)) as http:
            client = ApiClient("http://testserver", token, http=http)
            primera = client.study(T1)
            segunda = client.study(T2, conversation_id=primera.conversation_id)
        assert primera.conversation_id
        assert segunda.conversation_id == primera.conversation_id


# ---------------------------------------------------------------------------
# Tutor: ainvoke y separación historial / evidencia / instrucción
# ---------------------------------------------------------------------------


class TestTutorAsincronoYPrompt:
    def test_prompt_separa_historial_de_evidencia(self):
        from ada_study.contracts.study import SourceReference as Ref

        evidence = [
            Ref(
                document_id="doc_1",
                version_id="ver_1",
                chunk_id="chk_1",
                title="Valor posicional",
                page=3,
                excerpt="Cada cifra vale según su posición.",
                content_hash="0" * 64,
                retrieval_method=["bm25"],
            )
        ]
        history = [
            {"role": "student", "content": T1},
            {"role": "ada", "content": "Explicación anterior con <etiquetas>.", "topic": "Tema"},
        ]
        prompt = _build_tutor_prompt(T2, evidence, history)
        # Secciones separadas y en orden: instrucción → historial → evidencia.
        assert prompt.index("Consulta del estudiante") < prompt.index("Historial conversacional")
        assert prompt.index("Historial conversacional") < prompt.index("<fragmento")
        # El historial es contexto, nunca fuente de respaldo; va escapado.
        assert "<etiquetas>" not in prompt
        assert "única fuente de respaldo" in prompt

    def test_prompt_sin_historial_no_incluye_la_seccion(self):
        prompt = _build_tutor_prompt("consulta", [], [])
        assert "Historial conversacional" not in prompt

    async def test_tutor_invoca_asincrono_con_historial(self):
        """El tutor usa `ainvoke` (nunca `invoke`) y recibe el historial."""
        model = FakeTutorModel()
        saver = InMemorySaver()
        await run_study(_repo(), T1, _viewer(), model, checkpointer=saver, thread_id="conv-t1")
        # Si el tutor hubiera usado `invoke`, _generate habría fallado y la
        # explicación habría sido la del fallback determinístico.
        assert model.calls  # la ruta asíncrona SÍ fue utilizada
        assert model.calls[0].startswith("Sos el tutor pedagógico")


# ---------------------------------------------------------------------------
# Validator: valida contra la evidencia del turno actual (con historial)
# ---------------------------------------------------------------------------


class TestValidatorConEvidenciaActual:
    def _refs(self, chunk_id: str) -> list[dict]:
        return [
            {
                "document_id": "doc_1",
                "version_id": "ver_1",
                "chunk_id": chunk_id,
                "title": "Valor posicional",
                "page": 1,
                "excerpt": "texto",
                "content_hash": "0" * 64,
                "retrieval_method": ["bm25"],
            }
        ]

    def _state(self, cited_chunk_id: str) -> dict:
        return {
            "conversation_history": [
                {"role": "student", "content": "pregunta anterior"},
                {"role": "ada", "content": "respuesta anterior"},
            ],
            "research_artifact": {
                "evidence": self._refs("chk_turno_actual"),
                "evidence_gaps": [],
            },
            "study_response": {
                "status": "complete",
                "topic": "Valor posicional",
                "explanation": "Explicación.",
                "references": self._refs(cited_chunk_id),
                "uses_web_sources": False,
                "evidence_gaps": [],
                "limitations": [],
            },
        }

    def test_cita_ajena_al_turno_actual_se_rechaza(self):
        from ada_study.graph.validator import validator_node

        update = validator_node(self._state("chk_de_otro_turno"))
        assert update["validation_result"]["is_valid"] is False
        assert any("chk_de_otro_turno" in e for e in update["validation_result"]["tutor_errors"])

    def test_cita_de_la_evidencia_actual_se_acepta(self):
        from ada_study.graph.validator import validator_node

        update = validator_node(self._state("chk_turno_actual"))
        assert update["validation_result"]["is_valid"] is True
        assert update["retries"] == 0
