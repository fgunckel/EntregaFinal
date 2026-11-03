"""Checkpointer LangGraph persistente en PostgreSQL (CHECKPOINTER-01).

`AsyncPostgresSaver` sobre un pool de psycopg3 (el driver async del
checkpointer es psycopg, no asyncpg). Se abre UNA vez por proceso en el
lifespan de la API y se cierra al apagar; `setup()` crea/verifica las tablas
de checkpoint (checkpoints, checkpoint_blobs, checkpoint_writes,
checkpoint_migrations) de forma idempotente.

Con `ADA_CONVERSATIONS_ENABLED=false` (default) no se construye ningún pool:
estado efímero por petición (comportamiento previo). Fallos de conexión
cortan el arranque de forma ruidosa (fail-loud): NUNCA se degrada
silenciosamente a modo efímero.
"""



class CheckpointerError(RuntimeError):
    """Fallo controlado del checkpointer (código estable, sin trazas)."""

    def __init__(self, error_code: str, detail: str) -> None:
        self.error_code = error_code
        super().__init__(detail)


def psycopg_conninfo(database_url: str) -> str:
    """Convierte la URL del proyecto (asyncpg) al formato psycopg3.

    La BD del proyecto usa asyncpg; el checkpointer de LangGraph usa psycopg.
    La URL se renderiza con credenciales (nunca se imprime ni se registra).
    """
    from sqlalchemy.engine import make_url

    return make_url(database_url).set(drivername="postgresql").render_as_string(
        hide_password=False
    )


class CheckpointerPool:
    """Pool de conexiones del checkpointer; una instancia por proceso.

    - `open()`: abre el pool, construye el `AsyncPostgresSaver` y ejecuta
      `setup()` (creación idempotente de las tablas de checkpoint).
    - `close()`: cierra el pool (shutdown limpio del lifespan).
    - `checkpointer`: el saver listo para inyectar en `compile_graph`.
    """

    def __init__(self, database_url: str, *, open_timeout: float = 10.0) -> None:
        self._conninfo = psycopg_conninfo(database_url)
        self._timeout = open_timeout
        self._pool = None
        self._saver = None

    @property
    def is_open(self) -> bool:
        """True si el pool está abierto y el saver disponible."""
        return self._saver is not None

    @property
    def checkpointer(self):
        """El `AsyncPostgresSaver` abierto (falla si el pool no está abierto)."""
        if self._saver is None:
            raise CheckpointerError(
                "CHECKPOINTER_NOT_OPEN",
                "El checkpointer no está abierto.",
            )
        return self._saver

    async def open(self) -> None:
        """Abre el pool y ejecuta `setup()` (idempotente) del saver."""
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
        from psycopg_pool import AsyncConnectionPool

        pool = AsyncConnectionPool(
            conninfo=self._conninfo,
            kwargs={"autocommit": True, "prepare_threshold": 0},
            open=False,
        )
        try:
            await pool.open(wait=True, timeout=self._timeout)
        except Exception as exc:  # noqa: BLE001 - frontera: error controlado
            await pool.close()
            raise CheckpointerError(
                "CHECKPOINTER_UNAVAILABLE",
                "No se pudo conectar el checkpointer a PostgreSQL.",
            ) from exc
        self._pool = pool
        self._saver = AsyncPostgresSaver(pool)
        try:
            await self._saver.setup()
        except Exception as exc:  # noqa: BLE001 - frontera: error controlado
            await self.close()
            raise CheckpointerError(
                "CHECKPOINTER_SETUP_FAILED",
                "El checkpointer no pudo preparar su esquema en PostgreSQL.",
            ) from exc

    async def close(self) -> None:
        """Cierra el pool de conexiones (idempotente)."""
        if self._pool is not None:
            await self._pool.close()
        self._pool = None
        self._saver = None
