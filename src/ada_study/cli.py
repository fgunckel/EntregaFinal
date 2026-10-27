"""CLI de administrador inicial (TASK-04, condición aprobada).

- La contraseña se solicita por stdin SIN mostrarla (getpass).
- No acepta contraseñas por argumentos (se rechaza explícitamente).
- No eleva administradores existentes silenciosamente: si ya existe una
  cuenta admin activa, el comando falla con código 1 y mensaje claro.
- Idempotente y seguro: si la cuenta del email ya existe pero NO es admin,
  no se eleva; se informa y se sale con código 1. Si no existe, se crea
  la cuenta admin activa con el hash PBKDF2 de la contraseña.
"""

import asyncio
import getpass
import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy.ext.asyncio import create_async_engine

from ada_study.contracts.identity import UserRecord
from ada_study.db.repository import CatalogRepository
from ada_study.domain.identity_service import hash_password

# ---------------------------------------------------------------------------
# Validación de contraseña (mínimos razonables, Spec 06 §37)
# ---------------------------------------------------------------------------

MIN_PASSWORD_LENGTH = 12


def validate_password_strength(password: str) -> str | None:
    """Devuelve un mensaje de error si la contraseña es débil, o None."""
    if len(password) < MIN_PASSWORD_LENGTH:
        return (
            f"La contraseña debe tener al menos {MIN_PASSWORD_LENGTH} caracteres"
        )
    if not any(c.isdigit() for c in password):
        return "La contraseña debe incluir al menos un dígito"
    if not any(c.isalpha() for c in password):
        return "La contraseña debe incluir al menos una letra"
    return None


def _read_password(prompt: str) -> str:
    """Lee la contraseña por stdin sin eco (getpass; fallback a input oculto)."""
    try:
        return getpass.getpass(prompt)
    except Exception:
        # getpass puede fallar en consolas no interactivas: no se acepta
        # contraseña por argumento; se aborta con error claro.
        print(
            "No se pudo leer la contraseña de forma segura (stdin no interactivo).",
            file=sys.stderr,
        )
        raise SystemExit(1) from None


def _read_non_empty(prompt: str) -> str:
    value = input(prompt).strip()
    if not value:
        print("El valor no puede estar vacío.", file=sys.stderr)
        raise SystemExit(1)
    return value


async def create_initial_admin(
    engine, *, email: str, display_name: str, password: str
) -> int:
    """Crea la cuenta admin inicial; idempotente y sin elevación silenciosa.

    Devuelve código de salida: 0 creado, 1 error.
    """
    engine_obj = create_async_engine(engine)
    try:
        repo = CatalogRepository(engine_obj)
        existing = await repo.get_user_by_email(email)
        if existing is not None:
            if existing.role == "admin" and existing.account_status == "active":
                # Idempotente: la cuenta admin ya existe; no se modifica nada.
                print(f"La cuenta admin {email} ya existe; no se realizaron cambios.")
                return 0
            # Existe pero no es admin activa: NO se eleva silenciosamente.
            print(
                f"ERROR: ya existe una cuenta con el correo {email} "
                f"(rol={existing.role}, estado={existing.account_status}). "
                "No se creó ni elevó ninguna cuenta.",
                file=sys.stderr,
            )
            return 1
        now = datetime.now(UTC).isoformat()
        record = UserRecord(
            user_id=f"usr_{uuid.uuid4().hex[:12]}",
            email=email,
            display_name=display_name,
            role="admin",
            account_status="active",
            password_hash=hash_password(password),
            created_at=now,
            updated_at=now,
        )
        await repo.create_user(record)
        print(f"Cuenta admin creada para {email}.")
        return 0
    finally:
        await engine_obj.dispose()


def main(argv: list[str] | None = None) -> int:
    """Punto de entrada del CLI (python -m ada_study.cli create-admin)."""
    args = list(sys.argv[1:] if argv is None else argv)
    if args and args[0] in ("-h", "--help"):
        print(
            "Uso: python -m ada_study.cli\n"
            "Crea la cuenta de administrador inicial.\n"
            "Solicita email, nombre y contraseña por stdin (sin mostrarla)."
        )
        return 0
    print("== Alta del administrador inicial ==")
    email = _read_non_empty("Email del administrador: ")
    display_name = _read_non_empty("Nombre para mostrar: ")
    password = _read_password("Contraseña (no se mostrará): ")
    confirm = _read_password("Confirmar contraseña: ")
    if password != confirm:
        print("ERROR: las contraseñas no coinciden.", file=sys.stderr)
        return 1
    if message := validate_password_strength(password):
        print(f"ERROR: {message}", file=sys.stderr)
        return 1
    database_url = args[0] if args else None
    if not database_url:
        print(
            "ERROR: falta la URL de PostgreSQL "
            "(ej. postgresql+asyncpg://user:pass@localhost/db).",
            file=sys.stderr,
        )
        return 1
    return asyncio.run(
        create_initial_admin(
            database_url, email=email, display_name=display_name, password=password
        )
    )


if __name__ == "__main__":
    sys.exit(main())
