# FlowOps — Development Setup

## What this is
FlowOps is a FastAPI IT workflow automation platform (Python 3.10–3.13, SQLAlchemy 2.0, Alembic, Jinja2 templates + a React operator console, SQLite locally / PostgreSQL for deployment). It receives support tickets, classifies/routes them, runs onboarding/offboarding workflows with versioned DAG definitions, SLA timers, durable leased jobs, event triggers and source connectors, and exposes monitoring dashboards and analytics.

> **Plan of record for future work:** [`docs/ROADMAP.md`](docs/ROADMAP.md).
> Pick work from there, keep PRs single-concern, and rebase on `main` before
> opening. Tick the roadmap checkbox your PR completes in the PR body.

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
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt   # pinned runtime + test dependencies
uvicorn src.api:app --reload          # http://127.0.0.1:8000
```
Without `FLOWOPS_DATABASE_URL`, the app defaults to `sqlite:///data/flowops.db`.

**After pulling new commits, re-run `pip install -r requirements-dev.txt`.**
Dependabot bumps the pinned files weekly; a stale local venv can fail test
collection with import errors that do not exist in CI. CI installs the pinned
files fresh on every run and is the authoritative signal.

## Tests
```bash
pytest -q
```
The suite is fully offline (mocked HTTP, `tmp_path` SQLite). PostgreSQL-only
tests skip unless `FLOWOPS_TEST_POSTGRES_URL` is set; CI's `postgres-idempotency`
job runs them against a `postgres:16` service. Test counts are not pinned in
docs — run the suite for the current number.

## Conventions for changes
- Schema changes go through a new Alembic revision; never edit existing migrations or the baseline. SQLite also gets the additive column via `_add_missing_columns()` in `src/db.py`.
- API changes are additive: existing response shapes, status codes, and idempotency semantics are contracts. The command-center validates responses against typed contracts (`command-center/src/lib/contracts.ts`) at runtime.
- Every workflow action writes an actor-attributed execution log.
- Tests stay offline: mock HTTP, never depend on internet access, never `sleep`.
- Do not commit or push secrets; connector/tenant credentials live in env-var names only (see `.env.example`, `.connector-secrets.env.example`).
