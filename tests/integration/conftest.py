"""Fixtures de integración: PostgreSQL REAL (tests/integration).

- Requieren `DATABASE_URL` (p. ej. `docker compose up -d` del proyecto);
  sin ella los tests se marcan SKIP: nada se presenta como validación de
  PostgreSQL sin una instancia real.
- Se crea una base EFÍMERA por sesión (`ada_study_test_<hex>`); nunca se
  tocan volúmenes, tablas ni datos de la base configurada. La base de
  prueba se elimina al terminar la sesión.
- Cada test parte de un esquema rev. 3 limpio (drop_all + create_all).

"""


import hashlib
import os
import secrets
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import pytest_asyncio
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine

from ada_study.contracts.identity import SessionRecord, UserRecord
from ada_study.db.repository import CatalogRepository
from ada_study.db.schema import metadata
from ada_study.domain.catalog_service import CatalogService
from ada_study.storage.pdf_storage import PdfStorage


@pytest.fixture(scope="session")
def database_url() -> str:
    """URL de PostgreSQL real; SKIP sin DATABASE_URL (nada de fakes)."""
    url = os.environ.get("DATABASE_URL", "").strip()
    if not url:
        pytest.skip("DATABASE_URL no definida: se requiere PostgreSQL real (docker compose up -d)")
    return url


@pytest_asyncio.fixture(scope="session")
async def test_database_url(database_url: str) -> str:
    """Base EFÍMERA por sesión; nunca se toca la base configurada."""
    from sqlalchemy.engine import make_url

    url = make_url(database_url)
    dbname = f"ada_study_test_{secrets.token_hex(4)}"
    admin_url = url.set(database="postgres")
    admin = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with admin.connect() as conn:
            await conn.execute(text(f'CREATE DATABASE "{dbname}"'))
    finally:
        await admin.dispose()
    # NOTA: `str(URL)` de SQLAlchemy ENMASCARA la contraseña con `***`; con
    # ella asyncpg fallaría con "password authentication failed". Se renderiza
    # con hide_password=False (infraestructura de test, rev. 3).
    yield url.set(database=dbname).render_as_string(hide_password=False)

    drop = create_async_engine(admin_url, isolation_level="AUTOCOMMIT")
    try:
        async with drop.connect() as conn:
            await conn.execute(text(f'DROP DATABASE "{dbname}" WITH (FORCE)'))
    finally:
        await drop.dispose()


@pytest_asyncio.fixture
async def db_engine(test_database_url: str):
    """Engine con esquema rev. 3 limpio para un test."""
    engine = create_async_engine(test_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.drop_all)
        await conn.run_sync(metadata.create_all)
    yield engine
    await engine.dispose()


@pytest_asyncio.fixture
async def repo(db_engine):
    return CatalogRepository(engine=db_engine)


@pytest.fixture
def storage(tmp_path: Path) -> PdfStorage:
    return PdfStorage(root=tmp_path / "storage")


@pytest_asyncio.fixture
async def catalog(repo, storage) -> CatalogService:
    return CatalogService(repository=repo, storage=storage)


async def make_user_session(repo: CatalogRepository, role: str = "admin") -> str:
    """Crea cuenta activa + sesión real en PostgreSQL; devuelve el token."""
    now = datetime.now(UTC)
    token = secrets.token_urlsafe(32)
    user = UserRecord(
        user_id=f"user_{role}",
        email=f"{role}@test.local",
        display_name=f"{role} de prueba",
        role=role,
        account_status="active",
        password_hash=None,
        created_at=now.isoformat(),
        updated_at=now.isoformat(),
    )
    await repo.create_user(user)
    session = SessionRecord(
        session_id=f"sess_{role}",
        user_id=user.user_id,
        token_hash=hashlib.sha256(token.encode()).hexdigest(),
        status="active",
        created_at=now.isoformat(),
        expires_at=(now + timedelta(hours=1)).isoformat(),
    )
    await repo.create_session(session)
    return token


@pytest_asyncio.fixture
async def api(test_database_url: str, tmp_path: Path):
    """App FastAPI completa (lifespan real) + sesión admin en PostgreSQL."""
    import httpx

    from ada_study.api.app import create_app
    from ada_study.config import Settings

    engine = create_async_engine(test_database_url)
    async with engine.begin() as conn:
        await conn.run_sync(metadata.drop_all)
        await conn.run_sync(metadata.create_all)
    repo = CatalogRepository(engine=engine)
    token = await make_user_session(repo, role="admin")
    settings = Settings(
        database_url=test_database_url,
        ada_storage_dir=str(tmp_path / "api_storage"),
    )
    app = create_app(settings=settings)
    transport = httpx.ASGITransport(app=app)
    client = httpx.AsyncClient(transport=transport, base_url="http://test")
    async with app.router.lifespan_context(app):
        yield client, repo, token
    await client.aclose()
    await engine.dispose()

