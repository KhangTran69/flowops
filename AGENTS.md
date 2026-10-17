# FlowOps — Base44 Development Setup

## What this is
FlowOps is a FastAPI IT workflow automation platform (Python 3.13, SQLAlchemy 2.0, Jinja2 templates, PostgreSQL). It receives support tickets, classifies/routes them, runs onboarding/offboarding workflows, and exposes a monitoring dashboard.

## How to run (Base44 sandbox)
```bash
docker compose -f docker-compose.base44.yml up -d --build
```
The app is on **host port 3000** (mapped to container 8000). Health check: `GET /ready`.

## Services in docker-compose.base44.yml
- **postgres** — PostgreSQL 16 (user/pass/db: `flowops`)
- **app** — uvicorn with `--reload` (live reload on `src/` changes); runs Alembic migrations on startup
- **worker** — durable workflow job processor (`src.worker`)
- **scheduler** — DAG orchestrator (`src.scheduler`)
- **timers** — SLA timer service (`src.timers`)
- **outbox** — notification dispatcher (`src.outbox_worker`)
- **webhook-sink** — local webhook capture service (built from `docker/webhook-sink/`)

All Python services use `python:3.13-slim` with the repo bind-mounted at `/app`; dependencies install via `pip install -r requirements.txt` on container startup.

## No external secrets required
The app boots with all defaults: auth is off (`FLOWOPS_AUTH_REQUIRED=false`, `FLOWOPS_UI_AUTH_REQUIRED=false`), AI adapter is `simulated`, action adapter is `simulated`. Optional credentials (Entra ID, Microsoft Graph, AI API key, ChatOps secret) can be added later via the Base44 secrets dashboard.

## Key URLs
- `/` — landing page
- `/dashboard` — monitoring dashboard
- `/tickets` — ticket list
- `/docs` — interactive API docs (Swagger)
- `/ready` — readiness probe
- `/health` — health probe

## Local development (without Docker)
```bash
pip install -r requirements.txt
uvicorn src.api:app --reload   # http://127.0.0.1:8000
```
Without `FLOWOPS_DATABASE_URL`, the app defaults to `sqlite:///data/flowops.db`.

## Tests
```bash
pytest -q
```
