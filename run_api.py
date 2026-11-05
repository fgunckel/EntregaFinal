"""Arranque de la API de ADA Study (FIX-TC06).

En Windows, psycopg (driver async del checkpointer LangGraph) NO puede
correr en el ProactorEventLoop default: exige SelectorEventLoop. Uvicorn
(0.54) elige el loop con un `loop_factory` explícito y SOLO usa Selector
con `--reload` (uvicorn/loops/asyncio.py); sin reload el pool del
checkpointer no conecta (y con el guard fail-loud de db/checkpointer.py,
corta el arranque). Este launcher fuerza SelectorEventLoop SIEMPRE en
Windows, con o sin reload. Fuera de Windows no cambia nada.

Uso:
    python run_api.py                 # desarrollo, con --reload (puerto 8000)
    python run_api.py --no-reload     # demo/TC-06 sin recarga automática
Puerto alternativo:  set ADA_API_PORT=8000 antes de ejecutar.
"""

import asyncio
import os
import sys

if sys.platform == "win32":
    # Por compatibilidad con herramientas que respetan la política:
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
    # uvicorn 0.54 pasa un loop_factory explícito a asyncio.run (ignora la
    # política): se parchea su factory para que SIEMPRE dé SelectorEventLoop.
    # El módulo `auto` importa la función dentro de la llamada, así que el
    # parche aplica tanto con loop="auto" como loop="asyncio".
    import uvicorn.loops.asyncio as _uv_loop_asyncio

    def _selector_loop_factory(use_subprocess: bool = False):
        return asyncio.SelectorEventLoop

    _uv_loop_asyncio.asyncio_loop_factory = _selector_loop_factory

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "ada_study.api.app:create_app",
        factory=True,
        app_dir="src",
        host="127.0.0.1",
        port=int(os.environ.get("ADA_API_PORT", "8000")),
        reload="--no-reload" not in sys.argv,
    )
