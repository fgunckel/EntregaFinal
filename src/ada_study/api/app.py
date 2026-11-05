"""Fábrica de la API de ADA Study (TASK-06, Fase E1, ADR-0006).

- La API encola y consulta; NO ejecuta la ingesta dentro de la petición HTTP
  (el worker corre separado, Fase D3).
- Reutiliza servicios existentes (CatalogService, IdentityService): no
  duplica lógica de dominio en los endpoints.
- Frontera de autenticación en servidor (Spec 06 §9, regla 5 del plan):
  todo endpoint valida el token Bearer contra la sesión real en PostgreSQL
  vía `IdentityService.validate_session`. Sin bypass ni identidad fija.
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from ada_study.config import Settings, get_settings
from ada_study.db.repository import CatalogRepository
from ada_study.domain.catalog_service import CatalogService
from ada_study.domain.identity_service import IdentityError, IdentityService
from ada_study.storage.pdf_storage import PdfStorage


class AdaState:
    """Estado de la app: engine y servicios compartidos."""

    def __init__(
        self,
        engine,
        repository: CatalogRepository,
        storage: PdfStorage,
        chat_model=None,
        checkpointer=None,
    ) -> None:
        self.engine = engine
        self.repository = repository
        self.storage = storage
        self.catalog = CatalogService(repository=repository, storage=storage)
        self.identity = IdentityService(repository=repository)
        # Chat model del tutor (TASK-11): construido UNA VEZ por proceso.
        # None = modo determinístico (default seguro, sin llamadas ni costos).
        self.chat_model = chat_model
        # Checkpointer LangGraph (CHECKPOINTER-01): None = modo efímero
        # (conversaciones deshabilitadas); AsyncPostgresSaver si está habilitado.
        self.checkpointer = checkpointer
        # Locks por conversación (CHECKPOINTER-03, §8): serializa turnos de la
        # MISMA conversación dentro del proceso; conversaciones distintas no se
        # bloquean entre sí. Sin infraestructura distribuida: un diccionario
        # por app (la mutación es atómica en el event loop, sin `await` entre
        # la lectura y la asignación).
        self.conversation_locks: dict[str, asyncio.Lock] = {}


def create_app(
    settings: Settings | None = None,
    *,
    state_factory=None,
) -> FastAPI:
    """Crea la app FastAPI con los servicios cableados.

    - `settings` permite inyectar configuración en pruebas; por defecto lee
      el entorno (get_settings).
    - `state_factory` permite inyectar el estado completo (engine,
      repositorio, servicios) en pruebas sin PostgreSQL: recibe la settings
      y devuelve un AdaState. En producción es None y se construye el estado
      real contra PostgreSQL (ADR-0001).
    """
    resolved = settings or get_settings()
    if not resolved.database_url:
        raise RuntimeError(
            "DATABASE_URL no definida: la API requiere PostgreSQL (ADR-0001)."
        )

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if state_factory is not None:
            # Pruebas: estado inyectado (sin conexión real).
            app.state.ada = state_factory(resolved)
            yield
            return
        from sqlalchemy.ext.asyncio import create_async_engine

        from ada_study.llm import build_chat_model

        engine = create_async_engine(resolved.database_url, echo=False)
        repository = CatalogRepository(engine=engine)
        storage = PdfStorage(root=resolved.ada_storage_dir)
        # Checkpointer persistente (CHECKPOINTER-01): solo con
        # ADA_CONVERSATIONS_ENABLED=true. Fallos de conexión cortan el
        # arranque de forma ruidosa (fail-loud, sin degradar a efímero).
        checkpointer_pool = None
        if resolved.ada_conversations_enabled:
            from ada_study.db.checkpointer import CheckpointerPool

            checkpointer_pool = CheckpointerPool(resolved.database_url)
            await checkpointer_pool.open()
        # Observabilidad técnica (AGENT-TRACE-01 §6): OTel/OpenInference hacia
        # Phoenix, con instrumentación LangChain real (spans por nodo y por
        # llamada LLM). Configurada sin el extra instalado corta el arranque
        # (ImportError fail-loud, igual que el checkpointer); fallos de
        # exportación posteriores los reporta OTel en logs sin tumbar la API.
        if resolved.ada_phoenix_enabled:
            from ada_study.observability.phoenix import init_phoenix

            init_phoenix(resolved)
        try:
            # Fail-fast (TASK-11): una configuración LLM inválida (proveedor
            # desconocido, modelo o clave ausentes) corta el arranque, nunca una
            # petición. Con ADA_LLM_PROVIDER=none (default) el modelo es None.
            app.state.ada = AdaState(
                engine=engine,
                repository=repository,
                storage=storage,
                chat_model=build_chat_model(resolved),
                checkpointer=checkpointer_pool.checkpointer if checkpointer_pool else None,
            )
            yield
        finally:
            if checkpointer_pool is not None:
                await checkpointer_pool.close()
            await engine.dispose()

    app = FastAPI(title="ADA Study API", version="0.1.0", lifespan=lifespan)
    app.state.settings = resolved

    from ada_study.api.routes_audit import router as audit_router
    from ada_study.api.routes_auth import router as auth_router
    from ada_study.api.routes_documents import router as documents_router
    from ada_study.api.routes_moderation import router as moderation_router
    from ada_study.api.routes_study import router as study_router

    app.include_router(documents_router)
    app.include_router(moderation_router)
    app.include_router(audit_router)
    app.include_router(auth_router)
    app.include_router(study_router)

    @app.exception_handler(IdentityError)
    async def identity_error_handler(request: Request, exc: IdentityError) -> JSONResponse:
        """Mapea errores de identidad a HTTP con códigos estables (ADR-0005)."""
        status = {
            "AUTH_SESSION_INVALID": 401,
            "AUTH_SESSION_EXPIRED": 401,
            "AUTH_ACCOUNT_NOT_ACTIVE": 403,
            "AUTH_FORBIDDEN": 403,
            "AUTH_INVALID_CREDENTIALS": 401,
        }.get(exc.error_code, 401)
        return JSONResponse(
            status_code=status,
            content={"error": exc.error_code, "detail": str(exc)},
        )

    return app
