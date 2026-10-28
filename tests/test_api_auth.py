"""Pruebas de las rutas de autenticación HTTP (TASK-08, B1/B3).

Cubren:
- POST /api/auth/login: credenciales válidas → token real; errores
  GENÉRICOS sin enumeración de correos (Spec 06 §41); cuenta suspendida
  → AUTH_ACCOUNT_NOT_ACTIVE.
- POST /api/auth/logout: revoca la sesión real; el token revocado deja
  de autenticar (AUTH_SESSION_INVALID).
- GET /api/me: identidad EXCLUSIVA de la sesión validada por el backend;
  sin token → 401; sesión expirada → 401; cuenta suspendida → 403.

Usan la app FastAPI con repositorio fake en memoria; IdentityService es
el REAL. Evidencia unitaria con fakes: NO valida integración con
PostgreSQL real (las 7 pruebas de integración de catálogo siguen
skipped sin DATABASE_URL).
"""

import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from fastapi.testclient import TestClient

from ada_study.api.app import AdaState, create_app
from ada_study.config import Settings
from ada_study.contracts.identity import AuditEvent, SessionRecord, UserRecord
from ada_study.db.repository import NotFoundError
from ada_study.domain.identity_service import hash_password, hash_token, new_session_token
from ada_study.storage.pdf_storage import PdfStorage

# ---------------------------------------------------------------------------
# Repositorio fake mínimo (identidad + auditoría)
# ---------------------------------------------------------------------------


class FakeAuthRepository:
    """Réplica en memoria del contrato de identidad usado por las rutas."""

    def __init__(self) -> None:
        self.users: dict[str, UserRecord] = {}
        self.users_by_email: dict[str, UserRecord] = {}
        self.sessions: dict[str, SessionRecord] = {}
        self.audit_events: list = []

    async def get_user(self, user_id: str) -> UserRecord:
        if user_id not in self.users:
            raise NotFoundError("Usuario", user_id)
        return self.users[user_id]

    async def get_user_by_email(self, email: str) -> UserRecord | None:
        return self.users_by_email.get(email)

    async def create_session(self, session) -> None:
        self.sessions[session.session_id] = session

    async def get_session_by_token_hash(self, token_hash: str):
        for session in self.sessions.values():
            if session.token_hash == token_hash:
                return session
        return None

    async def revoke_session(self, session_id: str) -> None:
        session = self.sessions[session_id]
        session.status = "revoked"
        session.revoked_at = datetime.now(UTC).isoformat()

    async def revoke_all_sessions(self, user_id: str) -> None:
        for session in self.sessions.values():
            if session.user_id == user_id and session.status == "active":
                session.status = "revoked"

    async def update_account_status(self, user_id: str, status: str) -> UserRecord:
        user = self.users[user_id]
        user.account_status = status
        return user

    async def append_audit_event(self, event: AuditEvent) -> None:
        self.audit_events.append(event)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _user(user_id: str, role: str = "student", *, account_status: str = "active") -> UserRecord:
    now = datetime.now(UTC).isoformat()
    return UserRecord(
        user_id=user_id,
        email=f"{user_id}@test.local",
        display_name=user_id,
        role=role,
        account_status=account_status,
        password_hash=hash_password("secreto123"),
        created_at=now,
        updated_at=now,
    )


def _client(repository: FakeAuthRepository):


    tmp_root = Path(tempfile.mkdtemp(prefix="ada_auth_test_"))
    settings = Settings(
        database_url="postgresql+asyncpg://fake",
        ada_storage_dir=str(tmp_root),
    )

    def state_factory(resolved: Settings) -> AdaState:
        return AdaState(
            engine=None,
            repository=repository,
            storage=PdfStorage(root=tmp_root),
        )

    app = create_app(settings, state_factory=state_factory)
    return TestClient(app)


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _session_token(
    repository: FakeAuthRepository,
    user: UserRecord,
    *,
    expires_in: timedelta = timedelta(hours=1),
) -> str:
    """Crea una sesión real (hash del token) y devuelve el token opaco."""
    token = new_session_token()
    now = datetime.now(UTC)
    record = SessionRecord(
        session_id=f"ses_{user.user_id}",
        user_id=user.user_id,
        token_hash=hash_token(token),
        created_at=now.isoformat(),
        expires_at=(datetime.now(UTC) + expires_in).isoformat(),
    )
    repository.sessions[record.session_id] = record
    return token


# ---------------------------------------------------------------------------
# Login (B1)
# ---------------------------------------------------------------------------


class TestAuthLogin:
    async def test_login_valid_credentials_returns_token(self):
        repo = FakeAuthRepository()
        user = _user("usr_1")
        repo.users[user.user_id] = user
        repo.users_by_email[user.email] = user
        with _client(repo) as client:
            response = client.post(
                "/api/auth/login",
                json={"email": user.email, "password": "secreto123"},
            )
        assert response.status_code == 200
        assert response.json()["token"]

    async def test_login_wrong_password_generic_401(self):
        repo = FakeAuthRepository()
        user = _user("usr_2")
        repo.users[user.user_id] = user
        repo.users_by_email[user.email] = user
        with _client(repo) as client:
            response = client.post(
                "/api/auth/login",
                json={"email": user.email, "password": "incorrecta"},
            )
        assert response.status_code == 401
        assert response.json()["error"] == "AUTH_INVALID_CREDENTIALS"

    async def test_login_unknown_email_same_error_as_wrong_password(self):
        """Sin enumeración de correos: mismo código y mensaje (Spec 06 §41)."""
        repo = FakeAuthRepository()
        with _client(repo) as client:
            unknown = client.post(
                "/api/auth/login",
                json={"email": "nadie@test.local", "password": "loquesea"},
            )
            repo2 = FakeAuthRepository()
            user = _user("usr_2")
            repo2.users[user.user_id] = user
            repo2.users_by_email[user.email] = user
        with _client(repo2) as client:
            wrong_password = client.post(
                "/api/auth/login",
                json={"email": user.email, "password": "incorrecta"},
            )
        assert unknown.status_code == wrong_password.status_code == 401
        assert unknown.json() == wrong_password.json()

    async def test_login_suspended_account_403(self):
        repo = FakeAuthRepository()
        user = _user("usr_susp", account_status="suspended")
        repo.users[user.user_id] = user
        repo.users_by_email[user.email] = user
        with _client(repo) as client:
            response = client.post(
                "/api/auth/login",
                json={"email": user.email, "password": "secreto123"},
            )
        assert response.status_code == 403
        assert response.json()["error"] == "AUTH_ACCOUNT_NOT_ACTIVE"


# ---------------------------------------------------------------------------
# Logout (revocación real)
# ---------------------------------------------------------------------------


class TestAuthLogout:
    async def test_logout_revokes_session(self):
        repo = FakeAuthRepository()
        user = _user("usr_lo")
        repo.users[user.user_id] = user
        token = _session_token(repo, user)
        with _client(repo) as client:
            response = client.post("/api/auth/logout", headers=_auth(token))
            assert response.status_code == 204
            # El token revocado ya no autentica (frontera real).
            after = client.get("/api/me", headers=_auth(token))
        assert after.status_code == 401
        assert after.json()["error"] == "AUTH_SESSION_INVALID"

    async def test_logout_without_token_401(self):
        repo = FakeAuthRepository()
        with _client(repo) as client:
            response = client.post("/api/auth/logout")
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# GET /api/me (B3)
# ---------------------------------------------------------------------------


class TestGetMe:
    async def test_me_returns_authenticated_user(self):
        repo = FakeAuthRepository()
        user = _user("usr_me", "moderator")
        repo.users[user.user_id] = user
        token = _session_token(repo, user)
        with _client(repo) as client:
            response = client.get("/api/me", headers=_auth(token))
        assert response.status_code == 200
        payload = response.json()
        assert payload["user_id"] == "usr_me"
        assert payload["role"] == "moderator"
        assert "password_hash" not in payload or payload["password_hash"] is None

    async def test_me_without_token_401(self):
        repo = FakeAuthRepository()
        with _client(repo) as client:
            response = client.get("/api/me")
        assert response.status_code == 401
        assert response.json()["error"] == "AUTH_SESSION_INVALID"

    async def test_me_with_expired_session_401(self):
        repo = FakeAuthRepository()
        user = _user("usr_exp")
        repo.users[user.user_id] = user
        token = _session_token(repo, user, expires_in=timedelta(hours=-1))
        with _client(repo) as client:
            response = client.get("/api/me", headers=_auth(token))
        assert response.status_code == 401
        assert response.json()["error"] == "AUTH_SESSION_EXPIRED"

    async def test_me_with_suspended_account_403(self):
        """Cuenta suspendida pierde acceso aunque conserve sesión activa."""
        repo = FakeAuthRepository()
        user = _user("usr_susp2")
        repo.users[user.user_id] = user
        token = _session_token(repo, user)
        # Suspensión posterior al login (Spec 06 §40).
        repo.users[user.user_id].account_status = "suspended"
        with _client(repo) as client:
            response = client.get("/api/me", headers=_auth(token))
        assert response.status_code == 403
        assert response.json()["error"] == "AUTH_ACCOUNT_NOT_ACTIVE"

    async def test_me_ignores_client_sent_identity(self):
        """La identidad proviene de la sesión; campos del cliente se ignoran."""
        repo = FakeAuthRepository()
        user = _user("usr_real", "student")
        repo.users[user.user_id] = user
        token = _session_token(repo, user)
        with _client(repo) as client:
            response = client.get(
                "/api/me",
                headers={**_auth(token), "X-Role": "admin", "X-User-Id": "otro"},
            )
        assert response.status_code == 200
        assert response.json()["role"] == "student"
        assert response.json()["user_id"] == "usr_real"
