# CrianzaApp — Guía para Claude

App **FastAPI** (Python, español) de crianza de esturión: estanques, peces (PIT),
muestreos, reproducción, **sexado offline** y **calidad de agua**. Puerto **8002**.

## Base de datos — LEER ANTES DE CUALQUIER SCRIPT ⚠️

Esta app usa **`fastapp_etapa1`** (usuario `fastapp_user`):
`postgresql://fastapp_user:fastapp_pass@localhost/fastapp_etapa1`

**Trampa conocida (causa confusión recurrente):** `app/db/session.py` hace
`load_dotenv()` —que **NO** pisa las variables ya definidas en el SO— y luego
`os.getenv("DATABASE_URL")`. El **SO tiene `DATABASE_URL=produccion_faena`** (la
base de PlantaApp), que **gana sobre el `.env`** de CrianzaApp. Por eso, al correr
scripts con el venv de CrianzaApp **sin forzar** la URL, terminas en
`produccion_faena` (donde las tablas de crianza ni existen).

- **Scripts / diagnóstico** → forzar la URL correcta:
  `DATABASE_URL=postgresql://fastapp_user:fastapp_pass@localhost/fastapp_etapa1 .venv/Scripts/python.exe ...`
- **Alembic** → `alembic.ini` ya fija `sqlalchemy.url = ...fastapp_etapa1` (no usa
  el env), así que `alembic upgrade head` apunta bien.
- **Servidor en producción** → `start_server.cmd` hace `set DATABASE_URL=%CRIANZA_DB_URL%`
  (fastapp_etapa1) antes de lanzar uvicorn; corre bien.

Bases del entorno que **NO** son de CrianzaApp: PlantaApp → `produccion_faena`;
AdminDashboard → lee las tres (produccion_faena + fastapp_etapa1 + dashboard_db);
CasAmaliApp → `casamaliapp`.

## Cómo correr y verificar

- Arranque: `uvicorn app.main:app --host 0.0.0.0 --port 8002` desde la raíz.
- Migraciones: Alembic (`alembic upgrade head`). Ojo con **múltiples heads** si dos
  ramas parten del mismo `down_revision`.
- El `.venv` **no** tiene pytest/httpx (TestClient no usable): verificar fabricando
  un Starlette `Request` y llamando a los handlers directo, con `PYTHONIOENCODING=utf-8`.
