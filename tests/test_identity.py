"""Pruebas de identidad (TASK-04): invitaciones, sesiones, suspensión, auditoría.

Pruebas UNITARIAS con fake de repositorio (sin PostgreSQL real): verifican
la lógica de dominio. Las de integración con PostgreSQL real quedan como
pendiente explícita (mismo criterio que TASK-02/03).
"""

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from ada_study.contracts.identity import (
    AuditEvent,
    InvitationRecord,
    SessionRecord,
    UserRecord,
)
from ada_study.db.repository import InvitationAlreadyConsumedError
from ada_study.domain.identity_service import (
    AccountNotActiveError,
    ForbiddenError,
    IdentityError,
    IdentityService,
    InvalidCredentialsError,
    InvitationInvalidError,
    hash_password,
)


def _now() -> datetime:
    return datetime.now(UTC)


def _user(
    user_id: str = "usr_1",
    role: str = "student",
    status: str = "active",
    email: str = "student@test.local",
    password: str | None = "Contrasena123",
) -> UserRecord:
    return UserRecord(
        user_id=user_id,
        email=email,
        display_name="Estudiante",
        role=role,
        account_status=status,
        password_hash=hash_password(password) if password else None,
        created_at=datetime.now(UTC).isoformat(),
        updated_at=datetime.now(UTC).isoformat(),
    )


class FakeRepository:
    """Fake del repositorio con las operaciones de identidad del servicio."""

    def __init__(self) -> None:
        self.users: dict[str, UserRecord] = {}
        self.invitations: dict[str, InvitationRecord] = {}
        self.sessions: dict[str, SessionRecord] = {}
        self.audit: list[AuditEvent] = []

    async def create_user(self, record: UserRecord) -> None:
        self.users[record.user_id] = record

    async def get_user(self, user_id: str) -> UserRecord:
        return self.users[user_id]

    async def get_user_by_email(self, email: str) -> UserRecord | None:
        return next((u for u in self.users.values() if u.email == email), None)

    async def update_account_status(self, user_id: str, status: str) -> UserRecord:
        updated = self.users[user_id].model_copy(update={"account_status": status})
        self.users[user_id] = updated
        return updated

    async def update_user_role(self, user_id: str, role: str) -> UserRecord:
        updated = self.users[user_id].model_copy(update={"role": role})
        self.users[user_id] = updated
        return updated

    async def set_password_hash(self, user_id: str, password_hash: str) -> None:
        updated = self.users[user_id].model_copy(
            update={"password_hash": password_hash}
        )
        self.users[user_id] = updated

    async def create_invitation(self, record: InvitationRecord) -> None:
        self.invitations[record.invitation_id] = record

    async def get_invitation_by_token_hash(self, token_hash: str):
        return next(
            (i for i in self.invitations.values() if i.token_hash == token_hash), None
        )

    async def consume_invitation(self, invitation_id: str, consumed_by: str):
        invitation = self.invitations[invitation_id]
        if invitation.status != "pending":
            raise InvitationAlreadyConsumedError(invitation_id)
        updated = invitation.model_copy(
            update={
                "status": "consumed",
                "consumed_at": datetime.now(UTC).isoformat(),
                "consumed_by": consumed_by,
            }
        )
        self.invitations[invitation_id] = updated
        return updated

    async def revoke_invitation(self, invitation_id: str) -> None:
        invitation = self.invitations[invitation_id]
        if invitation.status != "pending":
            raise InvitationAlreadyConsumedError(invitation_id)
        self.invitations[invitation_id] = invitation.model_copy(
            update={"status": "revoked"}
        )

    async def create_session(self, record: SessionRecord) -> None:
        self.sessions[record.session_id] = record

    async def get_session_by_token_hash(self, token_hash: str) -> SessionRecord | None:
        return next(
            (s for s in self.sessions.values() if s.token_hash == token_hash), None
        )

    async def revoke_session(self, session_id: str) -> None:
        session = self.sessions[session_id]
        self.sessions[session_id] = session.model_copy(
            update={"status": "revoked", "revoked_at": datetime.now(UTC).isoformat()}
        )

    async def revoke_all_sessions(self, user_id: str) -> int:
        count = 0
        for session in self.sessions.values():
            if session.user_id == user_id and session.status == "active":
                self.sessions[session.session_id] = session.model_copy(
                    update={
                        "status": "revoked",
                        "revoked_at": datetime.now(UTC).isoformat(),
                    }
                )
                count += 1
        return count

    async def append_audit_event(self, event: AuditEvent) -> None:
        self.audit.append(event)


@pytest.fixture
def service():
    return IdentityService(FakeRepository())


@pytest.mark.asyncio
async def test_admin_creates_invitation_and_returns_token_once(service):
    admin = _user(role="admin")
    token = await service.create_invitation(
        actor=admin, email="nuevo@estudiante.com", role="student"
    )
    assert token  # token en claro devuelto una vez
    stored = next(iter(service._repo.invitations.values()))
    assert stored.token_hash != token  # en DB solo el hash
    assert stored.status == "pending"


@pytest.mark.asyncio
async def test_student_cannot_create_invitation(service):
    student = _user(role="student")
    with pytest.raises(ForbiddenError):
        await service.create_invitation(actor=student, email="x@x.com", role="student")


@pytest.mark.asyncio
async def test_activate_account_single_use_and_status(service):
    admin = _user(role="admin")
    token = await service.create_invitation(
        actor=admin, email="nuevo@estudiante.com", role="student"
    )
    # El flujo real crea la cuenta en estado "invited" al momento de invitar:
    await service._repo.create_user(
        _user(email="nuevo@estudiante.com", status="invited", password=None)
    )
    user = await service.activate_account(
        token=token, password="Contrasena123", display_name="Nuevo"
    )
    assert user.account_status == "active"
    # Segundo uso del mismo token: rechazado (un solo uso)
    with pytest.raises(InvitationInvalidError):
        await service.activate_account(
            token=token, password="Otra12345678", display_name="Otro"
        )


@pytest.mark.asyncio
async def test_activate_with_expired_invitation_fails(service):
    admin = _user(role="admin")
    token = await service.create_invitation(
        actor=admin, email="vencido@test.local", role="student"
    )
    stored = next(iter(service._repo.invitations.values()))
    service._repo.invitations[stored.invitation_id] = stored.model_copy(
        update={"expires_at": (_now() - timedelta(days=1)).isoformat()}
    )
    with pytest.raises(InvitationInvalidError):
        await service.activate_account(
            token=token, password="Contrasena123", display_name="X"
        )


@pytest.mark.asyncio
async def test_activate_with_revoked_invitation_fails(service):
    admin = _user(role="admin")
    token = await service.create_invitation(
        actor=admin, email="revocada@test.local", role="student"
    )
    stored = next(iter(service._repo.invitations.values()))
    await service._repo.revoke_invitation(stored.invitation_id)
    with pytest.raises(InvitationInvalidError):
        await service.activate_account(
            token=token, password="Contrasena123", display_name="X"
        )


@pytest.mark.asyncio
async def test_activate_concurrent_invitation_only_one_wins(service):
    """Consumo concurrente: solo una activación gana (un solo uso, atómico)."""
    admin = _user(role="admin")
    token = await service.create_invitation(
        actor=admin, email="carrera@test.local", role="student"
    )
    invited = _user(
        user_id="usr_inv", status="invited", email="carrera@test.local", password=None
    )
    await service._repo.create_user(invited)

    async def try_activate():
        try:
            await service.activate_account(
                token=token, password="Contrasena123", display_name="X"
            )
            return "ok"
        except InvitationInvalidError:
            return "invalid"

    results = await asyncio.gather(try_activate(), try_activate())
    assert sorted(results) == ["invalid", "ok"]  # exactamente una gana


@pytest.mark.asyncio
async def test_login_success_returns_opaque_token(service):
    user = _user()
    await service._repo.create_user(user)
    token = await service.login(email=user.email, password="Contrasena123")
    assert token
    session = next(iter(service._repo.sessions.values()))
    assert session.token_hash != token  # solo el hash en DB
    assert session.status == "active"


@pytest.mark.asyncio
async def test_login_generic_error_no_email_enumeration(service):
    """Correo inexistente y contraseña incorrecta: MISMO error y mensaje."""
    with pytest.raises(InvalidCredentialsError) as unknown_email:
        await service.login(email="fantasma@test.local", password="Contrasena123")
    with pytest.raises(InvalidCredentialsError) as wrong_password:
        await service.login(email="student@test.local", password="Incorrecta1")
    # Ambas ramas producen el mismo tipo, código y mensaje (sin enumeración):
    assert type(unknown_email.value) is type(wrong_password.value)
    assert unknown_email.value.error_code == wrong_password.value.error_code
    assert str(unknown_email.value) == str(wrong_password.value)


@pytest.mark.asyncio
async def test_validate_session_returns_current_user(service):
    user = _user()
    await service._repo.create_user(user)
    token = await service.login(email=user.email, password="Contrasena123")
    validated = await service.validate_session(token)
    assert validated.user_id == user.user_id


@pytest.mark.asyncio
async def test_expired_session_rejected(service):
    user = _user()
    await service._repo.create_user(user)
    token = await service.login(email=user.email, password="Contrasena123")
    session = next(iter(service._repo.sessions.values()))
    service._repo.sessions[session.session_id] = session.model_copy(
        update={"expires_at": (_now() - timedelta(hours=1)).isoformat()}
    )
    with pytest.raises(IdentityError) as exc:
        await service.validate_session(token)
    assert exc.value.error_code == "AUTH_SESSION_EXPIRED"


@pytest.mark.asyncio
async def test_revoked_session_rejected(service):
    user = _user()
    await service._repo.create_user(user)
    token = await service.login(email=user.email, password="Contrasena123")
    await service.logout(actor=user, token=token)
    with pytest.raises(IdentityError) as exc:
        await service.validate_session(token)
    assert exc.value.error_code == "AUTH_SESSION_INVALID"


@pytest.mark.asyncio
async def test_suspended_account_loses_access_even_with_active_session(service):
    """Suspensión: sin acceso aunque la sesión previa siga activa en DB."""
    user = _user()
    await service._repo.create_user(user)
    token = await service.login(email=user.email, password="Contrasena123")
    admin = _user(user_id="usr_admin", role="admin")
    await service._repo.create_user(admin)
    await service.suspend_account(actor=admin, user_id=user.user_id)
    # La sesión se revocó...
    assert all(s.status == "revoked" for s in service._repo.sessions.values())
    # ...y el token ya no valida (sesión revocada tiene prioridad):
    with pytest.raises(IdentityError) as exc:
        await service.validate_session(token)
    assert exc.value.error_code == "AUTH_SESSION_INVALID"
    # Re-login con cuenta suspendida: rechazado (sin revelar estado)
    with pytest.raises(AccountNotActiveError):
        await service.login(email=user.email, password="Contrasena123")


@pytest.mark.asyncio
async def test_suspended_login_generic_error(service):
    """Login de cuenta suspendida: rechazado sin revelar detalles."""
    suspended = _user(status="suspended")
    await service._repo.create_user(suspended)
    with pytest.raises(AccountNotActiveError):
        await service.login(email=suspended.email, password="Contrasena123")


@pytest.mark.asyncio
async def test_only_admin_suspends_and_assigns_role(service):
    moderator = _user(user_id="usr_mod", role="moderator")
    await service._repo.create_user(moderator)
    await service._repo.create_user(_user(user_id="usr_2"))
    with pytest.raises(ForbiddenError):
        await service.suspend_account(actor=moderator, user_id="usr_2")
    with pytest.raises(ForbiddenError):
        await service.assign_role(actor=moderator, user_id="usr_2", role="admin")
    # Admin sí puede:
    admin = _user(user_id="usr_admin", role="admin")
    await service._repo.create_user(admin)
    suspended = await service.suspend_account(actor=admin, user_id="usr_2")
    assert suspended.account_status == "suspended"
    promoted = await service.assign_role(
        actor=admin, user_id="usr_2", role="trusted_contributor"
    )
    assert promoted.role == "trusted_contributor"


@pytest.mark.asyncio
async def test_audit_records_operations_without_secrets(service):
    """La auditoría registra acciones relevantes SIN contraseñas ni tokens."""
    admin = _user(user_id="usr_admin", role="admin")
    await service._repo.create_user(admin)
    token = await service.create_invitation(
        actor=admin, email="auditado@test.local", role="student"
    )
    await service._repo.create_user(
        _user(email="auditado@test.local", status="invited", password=None)
    )
    await service.activate_account(
        token=token, password="Contrasena123", display_name="X"
    )
    await service.login(email="auditado@test.local", password="Contrasena123")
    events = service._repo.audit
    actions = {e.action for e in events}
    assert "invitation.create" in actions
    assert "account.activated" in actions
    assert "auth.login" in actions
    # Ningún evento contiene la contraseña ni el token en claro:
    serialized = " ".join(
        f"{e.action}|{e.reason or ''}|{e.entity_id}" for e in events
    )
    assert "Contrasena123" not in serialized
    assert token not in serialized
