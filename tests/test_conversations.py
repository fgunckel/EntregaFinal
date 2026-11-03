"""Pruebas de conversaciones multi-turno (CHECKPOINTER-02).

Unitarias con fakes (sin PostgreSQL ni LLM real):
- ConversationService: creación, reutilización, titularidad, thread determinístico.
- Endpoint POST /api/study: compatibilidad sin conversation_id, 404 para
  conversación ajena (sin filtrar existencia), 422 para identificador
  inválido, aislamiento entre usuarios, thread_id estable por conversación
  (el cliente no puede elegir thread ni propietario).
"""

import tempfile
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langgraph.checkpoint.memory import InMemorySaver

from ada_study.api.app import AdaState, create_app
from ada_study.config import Settings
from ada_study.domain.conversation_service import (
    ConversationError,
    ConversationService,
    thread_id_for,
)
from ada_study.storage.pdf_storage import PdfStorage
from tests.test_api_auth import _session_token, _user
from tests.test_study_endpoint import FakeStudyRepository, _seed_published_document

_TEXT = "El valor posicional indica cuánto vale cada cifra según su posición."


class FakeConversationRepository(FakeStudyRepository):
    """Fake de identidad + catálogo + conversaciones (mismo contrato que el real)."""

    def __init__(self) -> None:
        super().__init__()
        self.conversations: dict[str, object] = {}

    async def create_conversation(self, record) -> None:
        self.conversations[record.conversation_id] = record

    async def get_conversation(self, conversation_id: str):
        return self.conversations.get(conversation_id)

    async def record_conversation_turn(self, conversation_id: str) -> None:
        record = self.conversations[conversation_id]
        self.conversations[conversation_id] = record.model_copy(
            update={
                "turn_count": record.turn_count + 1,
                "last_turn_at": datetime.now(UTC).isoformat(),
            }
        )


class TestConversationService:
    async def test_resuelve_nueva_conversacion(self):
        repo = FakeConversationRepository()
        service = ConversationService(repo)
        record = await service.resolve_conversation("usr_1", None)
        assert record.user_id == "usr_1"
        assert record.turn_count == 0
        assert record.thread_id == thread_id_for(record.conversation_id)

    async def test_thread_id_derivado_deterministicamente(self):
        assert thread_id_for("abc-123") == thread_id_for("abc-123")
        assert thread_id_for("abc-123") != thread_id_for("def-456")

    async def test_reutiliza_y_actualiza_last_turn(self):
        repo = FakeConversationRepository()
        service = ConversationService(repo)
        record = await service.resolve_conversation("usr_1", None)
        await service.record_turn(record.conversation_id)
        reutilizada = await repo.get_conversation(record.conversation_id)
        assert reutilizada.turn_count == 1
        assert reutilizada.thread_id == record.thread_id

    async def test_conversacion_ajena_rechazada(self):
        repo = FakeConversationRepository()
        service = ConversationService(repo)
        ajena = await service.resolve_conversation("usr_propietario", None)
        with pytest.raises(ConversationError) as excinfo:
            await service.resolve_conversation("usr_intruso", ajena.conversation_id)
        assert excinfo.value.error_code == "CONVERSATION_NOT_FOUND"

    async def test_identificador_invalido_rechazado(self):
        service = ConversationService(FakeConversationRepository())
        with pytest.raises(ConversationError) as excinfo:
            await service.resolve_conversation("usr_1", "no-es-un-uuid")
        assert excinfo.value.error_code == "CONVERSATION_INVALID_ID"


# ---------------------------------------------------------------------------
# Endpoint POST /api/study con conversaciones
# ---------------------------------------------------------------------------


def _client_with(repo, *, checkpointer=None):
    tmp_root = Path(tempfile.mkdtemp(prefix="ada_conv_test_"))
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
            checkpointer=checkpointer,
        )

    return TestClient(create_app(settings, state_factory=state_factory))


def _registered_session(repo, user) -> str:
    """Registra la cuenta en el fake (get_current_user consulta el usuario) y crea la sesión."""
    repo.users[user.user_id] = user
    repo.users_by_email[user.email] = user
    return _session_token(repo, user)


def _repo_con_usuario() -> tuple[FakeConversationRepository, str]:
    repo = FakeConversationRepository()
    _seed_published_document(repo, "doc_pub", "autor", _TEXT)
    return repo, _registered_session(repo, _user("usr_a", "student"))


class TestCreacionYReutilizacion:
    def test_nueva_conversacion_sin_conversation_id(self):
        repo, token = _repo_con_usuario()
        with _client_with(repo) as client:
            response = client.post(
                "/api/study",
                json={"user_request": "¿Qué es una fracción?"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200
        body = response.json()
        assert body["conversation_id"]
        assert len(body["conversation_id"]) == 36  # UUID

    def test_compatibilidad_solicitudes_sin_conversation_id(self):
        repo, token = _repo_con_usuario()
        with _client_with(repo) as client:
            headers = {"Authorization": f"Bearer {token}"}
            primera = client.post(
                "/api/study", json={"user_request": "pregunta 1"}, headers=headers
            )
            segunda = client.post(
                "/api/study", json={"user_request": "pregunta 2"}, headers=headers
            )
        assert primera.status_code == segunda.status_code == 200
        # Sin conversation_id, cada turno inicia una conversación nueva (no
        # se mezclan): el cliente es libre de no continuar una conversación.
        assert primera.json()["conversation_id"] != segunda.json()["conversation_id"]

    def test_identificador_invalido_rechazado_422(self):
        repo, token = _repo_con_usuario()
        with _client_with(repo) as client:
            response = client.post(
                "/api/study",
                json={"user_request": "hola", "conversation_id": "no-uuid"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 422
        assert response.json()["error"] == "CONVERSATION_INVALID_ID"

    def test_recupera_conversacion_propia(self):
        repo, token = _repo_con_usuario()
        with _client_with(repo) as client:
            headers = {"Authorization": f"Bearer {token}"}
            primera = client.post(
                "/api/study", json={"user_request": "pregunta 1"}, headers=headers
            )
            cid = primera.json()["conversation_id"]
            segunda = client.post(
                "/api/study",
                json={"user_request": "pregunta 2", "conversation_id": cid},
                headers=headers,
            )
        assert segunda.status_code == 200
        assert segunda.json()["conversation_id"] == cid
        # record_turn corre en CADA turno (el primero incluido): 2 peticiones
        # sobre la misma conversación → turn_count == 2.
        assert repo.conversations[cid].turn_count == 2


def _repo_dos_usuarios() -> tuple[FakeConversationRepository, str, str]:
    repo = FakeConversationRepository()
    _seed_published_document(repo, "doc_pub", "autor", _TEXT)
    token_a = _registered_session(repo, _user("usr_a", "student"))
    token_b = _registered_session(repo, _user("usr_b", "student"))
    return repo, token_a, token_b


class TestThreadIdEstable:
    def test_cada_conversacion_tiene_su_thread(self):
        repo, token_a, token_b = _repo_dos_usuarios()
        saver = InMemorySaver()
        with _client_with(repo, checkpointer=saver) as client:
            r1 = client.post(
                "/api/study", json={"user_request": "turno A1"},
                headers={"Authorization": f"Bearer {token_a}"},
            )
            r2 = client.post(
                "/api/study", json={"user_request": "turno B1"},
                headers={"Authorization": f"Bearer {token_b}"},
            )
        cid_a, cid_b = r1.json()["conversation_id"], r2.json()["conversation_id"]
        assert cid_a != cid_b
        assert thread_id_for(cid_a) != thread_id_for(cid_b)

    def test_runtime_persiste_en_el_thread_recibido(self):
        repo, token = _repo_con_usuario()
        saver = InMemorySaver()
        with _client_with(repo, checkpointer=saver) as client:
            primera = client.post(
                "/api/study", json={"user_request": "turno 1"},
                headers={"Authorization": f"Bearer {token}"},
            )
            cid = primera.json()["conversation_id"]
            client.post(
                "/api/study",
                json={"user_request": "turno 2", "conversation_id": cid},
                headers={"Authorization": f"Bearer {token}"},
            )
        # El runtime persistió en el thread derivado de la conversación.
        checkpoint = saver.get_tuple(
            {"configurable": {"thread_id": thread_id_for(cid), "checkpoint_ns": ""}}
        )
        assert checkpoint is not None

    def test_thread_id_corresponde_a_la_conversacion_autorizada(self):
        repo, token = _repo_con_usuario()
        saver = InMemorySaver()
        with _client_with(repo, checkpointer=saver) as client:
            response = client.post(
                "/api/study", json={"user_request": "turno 1"},
                headers={"Authorization": f"Bearer {token}"},
            )
        cid = response.json()["conversation_id"]
        record = repo.conversations[cid]
        assert record.thread_id == thread_id_for(cid)
        assert saver.get_tuple(
            {"configurable": {"thread_id": record.thread_id, "checkpoint_ns": ""}}
        ) is not None


class TestAislamiento:
    def test_aislamiento_entre_dos_usuarios(self):
        repo, token_a, token_b = _repo_dos_usuarios()
        with _client_with(repo) as client:
            headers_a = {"Authorization": f"Bearer {token_a}"}
            r1 = client.post("/api/study", json={"user_request": "turno A1"}, headers=headers_a)
            r2 = client.post(
                "/api/study", json={"user_request": "turno B1"},
                headers={"Authorization": f"Bearer {token_b}"},
            )
        cid_a, cid_b = r1.json()["conversation_id"], r2.json()["conversation_id"]
        assert cid_a != cid_b
        assert repo.conversations[cid_a].user_id == "usr_a"
        assert repo.conversations[cid_b].user_id == "usr_b"

    def test_conversacion_ajena_rechazada_404(self):
        repo, token_a, token_b = _repo_dos_usuarios()
        with _client_with(repo) as client:
            cid_a = client.post(
                "/api/study", json={"user_request": "turno A1"},
                headers={"Authorization": f"Bearer {token_a}"},
            ).json()["conversation_id"]
            response = client.post(
                "/api/study",
                json={"user_request": "intento intruso", "conversation_id": cid_a},
                headers={"Authorization": f"Bearer {token_b}"},
            )
        assert response.status_code == 404
        assert response.json()["error"] == "CONVERSATION_NOT_FOUND"

    def test_cliente_no_puede_elegir_thread_id(self):
        repo, token, _ = _repo_dos_usuarios()
        with _client_with(repo) as client:
            response = client.post(
                "/api/study",
                json={"user_request": "turno", "thread_id": "thread-del-cliente"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200
        cid = response.json()["conversation_id"]
        # El thread del servidor es el derivado del conversation_id; el campo
        # extra `thread_id` del cuerpo se ignora por completo.
        assert repo.conversations[cid].thread_id == thread_id_for(cid)
        assert thread_id_for(cid) != "thread-del-cliente"

    def test_cliente_no_puede_fijar_propietario(self):
        repo, token, _ = _repo_dos_usuarios()
        with _client_with(repo) as client:
            response = client.post(
                "/api/study",
                json={"user_request": "turno", "user_id": "usr_intruso"},
                headers={"Authorization": f"Bearer {token}"},
            )
        assert response.status_code == 200
        cid = response.json()["conversation_id"]
        assert repo.conversations[cid].user_id == "usr_a"  # del token, no del cuerpo


