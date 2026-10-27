"""Servicio de identidad (TASK-04, Spec 06 §37-44).

- Contraseñas: PBKDF2-HMAC-SHA256 con salt aleatorio por usuario.
  Parámetros documentados: 600_000 iteraciones (OWASP 2023), salt de 16
  bytes aleatorio por usuario, hash de 32 bytes. Formato almacenado:
  "pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>".
- Sesiones: tokens opacos aleatorios (secrets.token_urlsafe, 32 bytes);
  en PostgreSQL solo se guarda SHA-256 del token. Revocables y con
  expiración. Toda validación verifica el estado ACTUAL de la cuenta.
- Errores de autenticación genéricos: nunca se expone si un correo existe
  (Spec 06 §41).
"""

import hashlib
import hmac
import secrets
import uuid
from datetime import UTC, datetime, timedelta

from ada_study.contracts.identity import (
    AuditEvent,
    InvitationRecord,
    Role,
    SessionRecord,
    UserRecord,
)
from ada_study.db.repository import (
    CatalogRepository,
    InvitationAlreadyConsumedError,
)
from ada_study.domain import permissions

# Parámetros PBKDF2 documentados (condición obligatoria de TASK-04).
PBKDF2_ITERATIONS = 600_000
PBKDF2_ALGORITHM = "sha256"
_SALT_BYTES = 16
_HASH_BYTES = 32

# Sesiones e invitaciones
SESSION_TTL_HOURS = 24
INVITATION_TTL_DAYS = 7


class IdentityError(Exception):
    """Error de identidad con código estable (ADR-0005)."""

    def __init__(self, error_code: str, message: str) -> None:
        self.error_code = error_code
        super().__init__(message)


class InvalidCredentialsError(IdentityError):
    """Credenciales inválidas: error GENÉRICO sin enumeración de correos."""

    def __init__(self) -> None:
        super().__init__("AUTH_INVALID_CREDENTIALS", "Credenciales inválidas")


class AccountNotActiveError(IdentityError):
    """Cuenta invitada, suspendida o desactivada (Spec 06 §40)."""

    def __init__(self) -> None:
        super().__init__("AUTH_ACCOUNT_NOT_ACTIVE", "La cuenta no está activa")


class ForbiddenError(IdentityError):
    """Operación no permitida para el rol (autorización en backend)."""

    def __init__(self, permission: str) -> None:
        super().__init__("AUTH_FORBIDDEN", f"Permiso requerido: {permission}")


class InvitationInvalidError(IdentityError):
    """Invitación inexistente, vencida, revocada o ya consumida."""

    def __init__(self) -> None:
        super().__init__("AUTH_INVITATION_INVALID", "Invitación inválida o vencida")


# ---------------------------------------------------------------------------
# Primitivas de hash (PBKDF2) y tokens
# ---------------------------------------------------------------------------


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """Genera el hash PBKDF2 con salt aleatorio por usuario.

    Formato: "pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>".
    El salt es aleatorio por usuario (condición obligatoria de TASK-04).
    """
    salt_bytes = secrets.token_bytes(_SALT_BYTES)
    digest = hashlib.pbkdf2_hmac(
        PBKDF2_ALGORITHM,
        password.encode("utf-8"),
        salt_bytes,
        iterations,
        dklen=_HASH_BYTES,
    )
    return f"pbkdf2_{PBKDF2_ALGORITHM}${iterations}${salt_bytes.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """Verifica la contraseña contra el hash almacenado (comparación constante)."""
    try:
        algorithm, iterations_raw, salt_hex, hash_hex = stored.split("$")
    except ValueError:
        return False
    if algorithm != f"pbkdf2_{PBKDF2_ALGORITHM}":
        return False
    try:
        iterations = int(iterations_raw)
        expected = bytes.fromhex(hash_hex)
        actual = hashlib.pbkdf2_hmac(
            PBKDF2_ALGORITHM,
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            iterations,
            dklen=_HASH_BYTES,
        )
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(expected, actual)


def new_session_token() -> str:
    """Token opaco aleatorio (solo vive con el cliente; en DB va el hash)."""
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    """SHA-256 hex del token (lo único que se persiste)."""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _now() -> datetime:
    return datetime.now(UTC)


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


class IdentityService:
    """Identidad, sesiones y auditoría (Spec 06 §37-44).

    Autorización en backend vía `permissions`; errores de login genéricos;
    en PostgreSQL solo hashes (PBKDF2 de contraseñas, SHA-256 de tokens).
    """

    def __init__(self, repository: CatalogRepository) -> None:
        self._repo = repository

    async def _audit(
        self,
        *,
        actor_id: str | None,
        action: str,
        entity_type: str,
        entity_id: str,
        result: str = "ok",
        reason: str | None = None,
    ) -> None:
        """Registra un evento de auditoría SIN secretos (Spec 06 §88).

        Nunca se registran contraseñas, tokens ni hashes: solo identificadores,
        acción, resultado y motivo funcional.
        """
        await self._repo.append_audit_event(
            AuditEvent(
                event_id=_new_id("evt"),
                actor_id=actor_id,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                result=result,
                reason=reason,
                created_at=_now().isoformat(),
            )
        )

    # -- invitaciones (Spec 06 §38-39) ----------------------------------------

    async def create_invitation(
        self, *, actor: UserRecord, email: str, role: Role
    ) -> str:
        """Crea una invitación de un solo uso y devuelve el token en claro.

        El token en claro se devuelve UNA vez al admin (canal de entrega);
        en PostgreSQL solo queda su SHA-256. Requiere users:invite.
        """
        if not permissions.can_manage_users(actor.role):
            await self._audit(
                actor_id=actor.user_id,
                action="invitation.create",
                entity_type="invitation",
                entity_id="n/a",
                result="denied",
                reason="rol sin permiso users:invite",
            )
            raise ForbiddenError("users:invite")
        token = new_session_token()
        expires_at = (_now() + timedelta(days=INVITATION_TTL_DAYS)).isoformat()
        record = InvitationRecord(
            invitation_id=_new_id("inv"),
            email=email,
            role=role,
            token_hash=hash_token(token),
            created_by=actor.user_id,
            created_at=_now().isoformat(),
            expires_at=expires_at,
        )
        await self._repo.create_invitation(record)
        await self._audit(
            actor_id=actor.user_id,
            action="invitation.create",
            entity_type="invitation",
            entity_id=record.invitation_id,
        )
        return token

    async def activate_account(
        self, *, token: str, password: str, display_name: str
    ) -> UserRecord:
        """Activa la cuenta desde la invitación (un solo uso, atómico)."""
        invitation = await self._repo.get_invitation_by_token_hash(hash_token(token))
        if invitation is None or invitation.status != "pending":
            raise InvitationInvalidError()
        if datetime.fromisoformat(invitation.expires_at) < _now():
            raise InvitationInvalidError()
        user = await self._repo.get_user_by_email(invitation.email)
        if user is None:
            raise InvitationInvalidError()
        password_hash = hash_password(password)
        # Consumo atómico ANTES de activar: si pierde la carrera, no se activa.
        try:
            await self._repo.consume_invitation(invitation.invitation_id, user.user_id)
        except InvitationAlreadyConsumedError:
            raise InvitationInvalidError() from None
        await self._repo.set_password_hash(user.user_id, password_hash)
        await self._repo.update_account_status(user.user_id, "active")
        await self._audit(
            actor_id=user.user_id,
            action="account.activated",
            entity_type="user",
            entity_id=user.user_id,
        )
        return await self._repo.get_user(user.user_id)

    # -- login y sesiones (Spec 06 §40-42) -------------------------------------

    async def login(self, *, email: str, password: str) -> str:
        """Autentica y devuelve el token opaco de la nueva sesión.

        Errores GENÉRICOS (Spec 06 §41): tanto el correo inexistente como la
        contraseña incorrecta producen el mismo AUTH_INVALID_CREDENTIALS, con
        el mismo mensaje y sin medición de tiempos diferenciables por rama.
        Cuentas no activas también fallan de forma genérica (no se revela
        si el correo existe).
        """
        user = await self._repo.get_user_by_email(email)
        if (
            user is None
            or user.password_hash is None
            or not verify_password(password, user.password_hash)
        ):
            await self._audit(
                actor_id=None,
                action="auth.login",
                entity_type="user",
                entity_id="desconocido",
                result="denied",
                reason="credenciales inválidas",
            )
            raise InvalidCredentialsError()
        if user.account_status != "active":
            await self._audit(
                actor_id=user.user_id,
                action="auth.login",
                entity_type="user",
                entity_id=user.user_id,
                result="denied",
                reason="cuenta no activa",
            )
            raise AccountNotActiveError()
        token = new_session_token()
        expires_at = (_now() + timedelta(hours=SESSION_TTL_HOURS)).isoformat()
        await self._repo.create_session(
            SessionRecord(
                session_id=_new_id("ses"),
                user_id=user.user_id,
                token_hash=hash_token(token),
                created_at=_now().isoformat(),
                expires_at=expires_at,
            )
        )
        await self._audit(
            actor_id=user.user_id,
            action="auth.login",
            entity_type="user",
            entity_id=user.user_id,
        )
        return token

    async def validate_session(self, token: str) -> UserRecord:
        """Valida el token y devuelve la cuenta ACTUAL (Spec 06 §40).

        Verifica el estado de la cuenta en cada validación: una cuenta
        suspendida o desactivada pierde el acceso aunque conserve sesiones
        activas previas. Errores diferenciados: expirada vs. inválida.
        """
        session = await self._repo.get_session_by_token_hash(hash_token(token))
        if session is None or session.status != "active":
            raise IdentityError("AUTH_SESSION_INVALID", "Sesión inválida")
        if datetime.fromisoformat(session.expires_at) < _now():
            raise IdentityError("AUTH_SESSION_EXPIRED", "Sesión expirada")
        user = await self._repo.get_user(session.user_id)
        if user.account_status != "active":
            raise AccountNotActiveError()
        return user

    async def logout(self, *, actor: UserRecord, token: str) -> None:
        """Revoca la sesión del token (logout)."""
        session = await self._repo.get_session_by_token_hash(hash_token(token))
        if session is not None:
            await self._repo.revoke_session(session.session_id)
        await self._audit(
            actor_id=actor.user_id,
            action="auth.logout",
            entity_type="session",
            entity_id=session.session_id if session else "desconocida",
        )

    # -- administración de cuentas (Spec 06 §30-34) ----------------------------

    async def suspend_account(self, *, actor: UserRecord, user_id: str) -> UserRecord:
        """Suspende la cuenta y revoca todas sus sesiones activas."""
        if not permissions.can_manage_users(actor.role):
            await self._audit(
                actor_id=actor.user_id,
                action="account.suspend",
                entity_type="user",
                entity_id=user_id,
                result="denied",
                reason="rol sin permiso users:manage",
            )
            raise ForbiddenError("users:manage")
        suspended = await self._repo.update_account_status(user_id, "suspended")
        await self._repo.revoke_all_sessions(user_id)
        await self._audit(
            actor_id=actor.user_id,
            action="account.suspended",
            entity_type="user",
            entity_id=user_id,
        )
        return suspended

    async def deactivate_account(
        self, *, actor: UserRecord, user_id: str
    ) -> UserRecord:
        """Desactiva (baja) la cuenta y revoca sus sesiones."""
        if not permissions.can_manage_users(actor.role):
            raise ForbiddenError("users:manage")
        deactivated = await self._repo.update_account_status(user_id, "deactivated")
        await self._repo.revoke_all_sessions(user_id)
        await self._audit(
            actor_id=actor.user_id,
            action="account.deactivated",
            entity_type="user",
            entity_id=user_id,
        )
        return deactivated

    async def assign_role(
        self, *, actor: UserRecord, user_id: str, role: Role
    ) -> UserRecord:
        """Cambia el rol de una cuenta (solo admin; se audita)."""
        if not permissions.can_manage_users(actor.role):
            raise ForbiddenError("users:assign_role")
        updated = await self._repo.update_user_role(user_id, role)
        await self._audit(
            actor_id=actor.user_id,
            action="user.role_changed",
            entity_type="user",
            entity_id=user_id,
            reason=f"nuevo rol: {role}",
        )
        return updated
