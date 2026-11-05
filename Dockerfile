# Imagen ÚNICA de aplicación ADA Study (FINAL-02): la API, el worker de
# ingesta y la UI (Streamlit) comparten esta imagen con distintos comandos
# de arranque definidos en docker-compose.yml (sin imágenes duplicadas).
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_NO_INPUT=1

WORKDIR /app

# pyproject + README primero: capa de dependencias cacheable.
COPY pyproject.toml README.md ./
COPY src ./src
# Tema «Tinta y aula» (UI-REDESIGN-02 etapa 0): Streamlit resuelve
# .streamlit/config.toml desde el CWD del proceso (WORKDIR /app). Sin esta
# línea el tema no llegaría a la imagen y el look seguiría siendo el default.
COPY .streamlit ./.streamlit

# Extras: ui (Streamlit), llm (LangChain/OpenAI-compatible) y observability
# (OpenInference/OTLP hacia Phoenix). psycopg lo trae
# langgraph-checkpoint-postgres (checkpointer, CHECKPOINTER-01).
# Sin secretos: la configuración llega por entorno en runtime (compose).
RUN pip install --no-cache-dir ".[ui,llm,observability]"

EXPOSE 8000 8501

# Sin CMD: cada servicio del Compose define el suyo (factory de FastAPI,
# worker_cli, streamlit run, create_schema).