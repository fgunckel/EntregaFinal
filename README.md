# ADA Study

Biblioteca documental con RAG incremental y centro de estudio para estudiantes de 12–15 años.

## Estado

- **TASK-00** — Auditoría de fuentes (completada, `docs/audit/BASELINE.md`).
- **TASK-01** — Decisiones de arquitectura y contratos (completada, `docs/adr/` + `docs/audit/`).
- **TASK-02** — Catálogo documental y metadatos (completada).
- **TASK-03** — Ingesta incremental (completada).
- **TASK-04** — Identidad mínima y moderación (completada).
- **TASK-05** — Retriever híbrido y grafo pedagógico (completada).
- **TASK-06** — API de documentos, Fase E1 (completada).
- **TASK-07** — Cierre de API: borrado/restauración/reintento, moderación, auditoría, E2E (completada).

## Estructura

- `ADA_Study_Spec_Kit/` — specs, tareas y prompts (fuente de requisitos).
- `docs/adr/` — decisiones de arquitectura (ADR-0001…0007).
- `docs/audit/` — auditoría, matriz de contratos, plan de implementación.
- `src/ada_study/` — código de la aplicación.
- `tests/` — pruebas unitarias e integración.

## Desarrollo

```bash
uv venv --python 3.12 .venv
uv pip install -e ".[dev]" --python .venv
.venv/Scripts/python -m pytest
.venv/Scripts/python -m ruff check .
```

Las pruebas de integración requieren `DATABASE_URL` apuntando a un PostgreSQL real
(p. ej. `postgresql+asyncpg://user:pass@localhost:5432/ada_study`); sin ella se saltan.

## Despliegue local con Docker Compose (FINAL-02)

Requisito previo: **Docker Desktop** (o Docker Engine + Compose v2). No se
necesita Python, venv ni ningún comando manual adicional.

### Arranque (comando único)

```bash
docker compose up --build
```

Levanta todo el sistema desde un clon limpio: PostgreSQL, esquema
(idempotente, se aplica solo), Phoenix, API, worker de ingesta y UI.

### URLs locales

| Servicio | URL |
|---|---|
| UI Streamlit | http://localhost:8501 |
| API FastAPI | http://localhost:8000 |
| Swagger / OpenAPI | http://localhost:8000/docs |
| Phoenix (trazas) | http://localhost:6006 |

### Primer administrador (instalación limpia)

La creación del admin es **interactiva por diseño** (la contraseña se pide por
stdin con `getpass`, nunca por argumentos ni por defecto en el repo). Una vez
levantados los servicios, ejecutar:

```bash
docker compose run --rm api python -m ada_study.cli \
  postgresql+asyncpg://ada:ada_dev_password@postgres:5432/ada_study
```

El comando pide email, nombre y contraseña (mínimo 12 caracteres con dígito y
letra), es idempotente y **no eleva** cuentas existentes silenciosamente.

### Configuración

Toda la configuración vive en variables de entorno (`.env` local; ver
`.env.example`). Con Compose:

- `DATABASE_URL`, `ADA_STORAGE_DIR` y `ADA_PHOENIX_ENDPOINT` los fija
  `docker-compose.yml` por la red interna (host `postgres`/`phoenix`); el
  `.env` se inyecta vía `env_file` y **nunca se copia dentro de la imagen**
  (`.dockerignore`).
- `ADA_CONVERSATIONS_ENABLED=true` (en `.env`) activa el checkpointer
  persistente para la demostración de persistencia (ver sección siguiente).
- `ADA_PHOENIX_ENABLED=true` activa la exportación de spans a Phoenix.
- LLM: `ADA_LLM_PROVIDER` / `ADA_LLM_MODEL` / `ADA_LLM_API_KEY` llegan al
  contenedor vía `.env` (sin secretos en el repo ni en la imagen). Default
  `none`: modo determinístico, sin llamadas ni costos.

### Detener, reiniciar y volúmenes

```bash
docker compose down              # detiene; los datos PERSISTEN (volumen)
docker compose up -d             # reinicia sin reconstruir; datos intactos
docker compose down -v           # ELIMINA volúmenes (datos + PDFs de /data)
docker compose up -d --build     # reconstruir tras cambios de código
```

La persistencia de PostgreSQL vive en el volumen `ada_pgdata` y los PDF en
`ada_storage` (compartido por API y worker). `migrate` es idempotente: puede
re-ejecutarse en cada arranque sin dañar datos.

## Proveedor LLM (TASK-11)

El nodo **tutor** puede redactar la explicación con un LLM real; el resto del
grafo (supervisor, researcher, validator, synthesizer, `limited_finish`)
permanece determinístico (anti-alucinación y procedencia). Por defecto el
sistema corre en **modo determinístico** (`ADA_LLM_PROVIDER=none`), sin
dependencias de proveedor ni costos.

Para activar el modo LLM:

```bash
pip install -e ".[llm]"   # langchain-openai (extra opcional)
```

```env
ADA_LLM_PROVIDER=openai
ADA_LLM_MODEL=gpt-5.4-mini
ADA_LLM_API_KEY=sk-...    # o OPENAI_API_KEY; nunca versionar la clave real
```

Variante **Groq** (endpoint OpenAI-compatible, mismo extra `llm`, sin
dependencias nuevas):

```env
ADA_LLM_PROVIDER=groq
ADA_LLM_MODEL=openai/gpt-oss-120b
ADA_LLM_API_KEY=gsk-...   # o GROQ_API_KEY; nunca versionar la clave real
```

La factory fija el endpoint oficial (`https://api.groq.com/openai/v1`) y
`reasoning_format=parsed`: los modelos gpt-oss razonan y, sin ese parámetro,
su cadena de análisis contaminaría la explicación mostrada al estudiante.

Garantías (decisiones aprobadas TASK-11):

- El modelo se construye **una vez por proceso** (lifespan de la API) y solo
  se inyecta en el tutor; configuración inválida corta el arranque (fail-fast).
- El LLM redacta **únicamente `explanation`**: las referencias se copian
  verbatim de la evidencia recuperada y el `status` se decide determinísticamente.
- Los fragmentos recuperados y la consulta se delimitan como **datos no
  confiables** (escape de etiquetas + reglas anti prompt-injection).
- Límites por llamada: `ADA_LLM_MAX_TOKENS` (800), `ADA_LLM_TIMEOUT_SECONDS`
  (20) y `ADA_LLM_MAX_RETRIES` (2) del proveedor; sin reintentos a nivel de
  grafo (máx. 1 llamada del tutor por intento).
- Fallo del proveedor, salida vacía o excedida → **fallback determinístico**
  con limitación explícita en `StudyResponse.limitations`; sin evidencia no
  se llama al LLM. Errores y uso de tokens se registran sin secretos ni
  contenido sensible.

Prueba optativa contra el proveedor real (realiza una llamada pagada; requiere
autorización explícita y clave):

```bash
ADA_LLM_LIVE=1 OPENAI_API_KEY=sk-... python -m pytest tests/test_study_llm_integration.py -v
```

Las pruebas de integración (PostgreSQL) requieren `DATABASE_URL` apuntando a un
PostgreSQL real (p. ej. `postgresql+asyncpg://user:pass@localhost:5432/ada_study`);
sin ella se saltan.

## API y worker (desarrollo)

```bash
# 1. PostgreSQL 16 (Docker)
docker compose up -d

# 2. Crear el esquema (idempotente)
export DATABASE_URL="postgresql+asyncpg://ada:ada_dev_password@localhost:5432/ada_study"
python -m ada_study.db.create_schema "$DATABASE_URL"


### Base existente: migración a rev. 3 (claim en `documents.sha256`)

`create_all` NO modifica tablas existentes. Para una base creada con el
esquema anterior, la migración idempotente y no destructiva es:

```bash
python -m ada_study.db.migrate "$DATABASE_URL"
```

Pasos: agrega `documents.sha256` si falta; completa el claim de cada
documento con la huella de su versión más reciente no purgada; detecta
conflictos de huella entre documentos vivos (aborta SIN crear el índice y
SIN modificar datos si existen); crea el índice único parcial
`ux_documents_active_sha256` y retira el índice heredado
`ux_versions_active_sha256`. Puede ejecutarse N veces (idempotente);
`python -m ada_study.db.create_schema "$DATABASE_URL"` también sirve: crea el
esquema limpio en rev. 3 y ejecuta la migración como verificación final.
Código de salida 2 = conflicto de huellas pendiente de resolución editorial.

# 3. Crear la cuenta admin inicial (interactivo, contraseña por stdin)
python -m ada_study.cli "$DATABASE_URL"

# 4. Iniciar el worker de ingesta (separado de la API; consume el outbox)
python -m ada_study.worker_cli "$DATABASE_URL" --once   # un ciclo
python -m ada_study.worker_cli "$DATABASE_URL"          # bucle (2 s)

# 5. Iniciar la API
uvicorn ada_study.api.app:create_app --factory --reload

# 6. (Opcional) Iniciar la UI Streamlit (TASK-08)
pip install -e ".[ui]"
streamlit run src/ada_study/ui/app.py
```

Endpoints (ADR-0006): `POST /api/documents` (202 + polling),
`GET /api/documents`, `GET /api/documents/{id}`,
`GET /api/ingestion-jobs/{job_id}`,
`DELETE /api/documents/{id}` (202, cortina + purga eventual),
`POST /api/documents/versions/{id}/retry` (solo failed, idempotente),
`POST /api/documents/{id}/versions/{vid}/restore` (versión nueva, historial intacto),
`POST /api/moderation/documents/{id}/decide` (moderator/admin),
`GET /api/moderation/queue` (moderator/admin),
`GET /api/audit/events` (moderator/admin),
`POST /api/auth/login` / `POST /api/auth/logout` (TASK-08, B1),
`GET /api/me` (perfil de la sesión validada, TASK-08 B3),
`POST /api/study` (TASK-10/11: consulta pedagógica con evidencia verificada
del corpus; el supervisor conserva el enrutado determinístico y el chat
model opcional solo redacta la explicación del tutor, con fallback
determinístico ante fallo del proveedor — ver "Proveedor LLM" abajo).
Autenticación: `Authorization: Bearer <token>`
(token opaco de sesión; se valida contra PostgreSQL en cada petición).

## Arquitectura

```text
┌─────────────┐   202+job   ┌──────────────────┐   outbox   ┌─────────────────┐
│   Cliente   │ ──────────► │  API FastAPI     │ ─────────► │  PostgreSQL     │
│ (UI TASK-08)│ ◄────────── │  + Identity      │ ◄───────── │  (ADR-0001)     │
└─────────────┘   polling   └──────────────────┘            └──────┬──────────┘
                                                                   │ outbox
                     ┌─────────────────────────────────────────────┘
                     ▼
             ┌───────────────┐   index/deindex   ┌──────────────────────┐
             │ Worker ingesta│ ────────────────► │ BM25 (rebuild desde  │
             │ (separado)    │                   │ chunks) + Pinecone   │
             └──────┬────────┘                   └──────────┬───────────┘
                    │ extrae/chunkea (pypdf)                │
                    ▼                                       ▼
             ┌───────────────┐                    ┌──────────────────────┐
             │ Almacén PDF   │                    │ HybridRetriever      │
             │ content-addr. │                    │ (RRF: bm25 + vector) │
             │ (ADR-0002)    │                    └──────────┬───────────┘
             └───────────────┘                               ▼
                                                    ┌──────────────────────┐
                                                    │ Grafo pedagógico     │
                                                    │ (supervisor/…/synth) │
                                                    └──────────────────────┘
```

Flujo editorial: upload de student nace `pending_review` (visible en privado
para su autor; el RAG compartido solo expone `published*`). El moderator decide
en `/api/moderation/queue`; toda acción sensible queda en `audit_events`
(append-only). Borrado/retiro aplican cortina inmediata y encolan la purga
física (eventual, ADR-0004).

## Grafo de agentes (LangGraph)

El grafo de estudio (`src/ada_study/graph/builder.py`) tiene seis nodos:
`supervisor`, `researcher`, `tutor`, `validator`, `synthesizer` y
`limited_finish` (salida segura). Los workers (`researcher`, `tutor`)
siempre devuelven el control al supervisor; las decisiones condicionales
son `route_after_supervisor` y `route_after_validator`.

```mermaid
flowchart TD
    START([START]) --> SUP["supervisor<br/>enrutado determinístico en producción<br/>(decisión aprobada TASK-11)"]

    SUP -->|"delega"| RES["researcher<br/>recuperación RAG híbrido"]
    SUP -->|"delega"| TUT["tutor<br/>único nodo con LLM<br/>(si ADA_LLM_PROVIDER ≠ none)"]
    RES -->|"retorno al supervisor"| SUP
    TUT -->|"retorno al supervisor"| SUP

    SUP -->|"propone synthesizer sin<br/>validación vigente (protección)"| VAL["validator"]
    SUP -->|"propone END sin artefacto<br/>de investigación"| LF["limited_finish<br/>salida segura"]
    SUP -->|"propone END con respuesta completa<br/>y validación aprobada"| FIN([END])
    SUP -->|"delega con validación vigente"| SYN["synthesizer"]

    VAL -->|"is_valid = true"| SYN
    VAL -->|"reparable: pending_repair_agent<br/>o research_errors"| RES
    VAL -->|"reparable: tutor_errors"| TUT
    VAL -->|"reintentos ≥ 2 o nada reparable<br/>presupuesto agotado"| LF
    SYN --> FIN
    LF --> FIN
```

Detalles de enrutamiento (reflejan el código, no inventan transiciones):

- **Supervisor**: en producción compila con `chat_model=None`
  (`study/runtime.py`), así que el enrutado es **determinístico** (decisión
  aprobada TASK-11). Existe una implementación con LLM
  (`graph/supervisor.py`, salida estructurada `RoutingDecision`) que queda
  reservada, y una arista condicional de protección que aplica reglas fijas
  aunque el supervisor propusiera otra cosa: el `synthesizer` solo corre con
  validación vigente aprobada, y `END` solo con `status=complete` y
  validación aprobada.
- **Tutor**: es el único nodo que usa el LLM (cuando se configura
  `ADA_LLM_PROVIDER`), con fallback determinístico ante fallo del proveedor.
- **Validator**: si la validación es correcta deriva al `synthesizer`; si
  detecta errores reparables deriva al especialista responsable
  (`researcher` o `tutor`) directamente, formando el **ciclo de reparación**;
  el presupuesto es de **2 reintentos** (`retries >= 2` en
  `route_after_validator`) y, agotado o sin errores reparables, deriva a
  `limited_finish` (salida segura con respuesta parcial honesta).
- **Cinturón adicional**: el runtime fija `recursion_limit`
  (`STUDY_RECURSION_LIMIT`) en la invocación del grafo.

## Persistencia de conversaciones (checkpointer, CHECKPOINTER-02/03)

La API puede mantener el estado conversacional multi-turno de cada alumno
entre reinicios del proceso:

```env
ADA_CONVERSATIONS_ENABLED=true
```

- **Qué habilita**: el checkpointer de LangGraph por conversación. Cada
  `POST /api/study` persiste el estado del grafo (incluido el historial
  pedagógico) y los turnos siguientes lo recuperan del servidor, nunca del
  cliente. Sin la variable (default `false`) la API corre en modo efímero
  por petición.
- **Mecanismo de persistencia**: `AsyncPostgresSaver` de LangGraph
  (`db/checkpointer.py`) sobre PostgreSQL; el esquema del checkpointer se
  crea al arrancar con `setup()` (idempotente).
- **Dependencia**: requiere `DATABASE_URL` apuntando a un PostgreSQL real y
  accesible; con conversaciones habilitadas y sin base accesible la API
  falla al arrancar (fail-loud).
- **Demostración**: debe estar habilitado para verificar la persistencia de
  conversaciones entre reinicios (los tests de integración de
  `tests/integration/` lo ejercitan contra PostgreSQL real).
- **Cambios de configuración**: requieren reiniciar la API (las settings se
  cachean por proceso y el lifespan monta/verifica el checkpointer al
  arrancar).

## Observabilidad con Phoenix (opcional, AGENT-TRACE-01 §6)

`docker-compose.yml` levanta Phoenix como servicio (UI en
`http://localhost:6006`). La app exporta spans por OTLP HTTP al endpoint:

```env
ADA_PHOENIX_ENABLED=true
ADA_PHOENIX_ENDPOINT=http://localhost:6006/v1/traces
```

- El endpoint debe incluir la ruta **`/v1/traces`**: apuntar solo al host
  (`http://localhost:6006`) hace que el exportador haga `POST /` y reciba
  `405 Method Not Allowed`; verificado contra el servicio local (la ruta
  `/v1/traces` existe y responde).
- Si la imagen de Phoenix se levanta con autenticación habilitada (responde
  `401` sin credenciales), configurar el token/credencial de ingesta o
  ejecutar Phoenix sin autenticación para desarrollo local.
- Requiere el extra `pip install -e ".[observability]"`. Los spans (nodos
  LangGraph + llamada al proveedor LLM) comparten el `trace_id` de la traza
  estructurada vía metadata `ada_trace_id`; la traza funcional viaja además
  en `StudyResponse.trace` (siempre activa).

## Limitaciones conocidas

- Sin Pinecone real, el worker usa `FakePineconeDestination` (doble de
  pruebas); la integración real de Pinecone queda pendiente. La recuperación
  efectiva del corpus es **BM25 puro** (`HybridRetriever` con
  `vector_search=None` en runtime): la fusión RRF bm25+vector solo entra en
  juego cuando exista búsqueda vectorial real.
- El supervisor usa enrutado determinístico en producción (el LLM está en
  el tutor); ver "Grafo de agentes" arriba.
