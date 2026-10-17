# FlowOps

FlowOps is an IT workflow automation platform. It receives support tickets and onboarding/offboarding requests, validates their data, classifies and routes them according to business rules, persists records, logs every workflow execution, and exposes a monitoring dashboard.

> ⚠️ **This is a learning prototype, not a production ticketing system.** Sample data comes from [JSONPlaceholder](https://jsonplaceholder.typicode.com/), a free fake REST API. These are **simulated** tickets, not real IT support requests.

## Long-term vision

1. **Automated ticket management** — users submit IT issues; the system classifies tickets, assigns priority, routes them to the appropriate support queue, and sends notifications automatically.
2. **Employee onboarding and offboarding** — a manager submits a request; the system generates a checklist for accounts, equipment, software access, and approvals, then tracks completion.
3. **AI support agent** — an AI model summarizes ticket history, suggests solutions from a knowledge base, and recommends actions; sensitive actions always require human approval.
4. **Automation monitoring dashboard** — track successful and failed workflow runs, processing time, tickets handled automatically, and estimated staff time saved.

## What is implemented

| Milestone | Status | What it delivers |
|---|---|---|
| v0.1 Ticket Intake Processor (CLI) | ✅ | Fetch a JSONPlaceholder todo, validate strictly, normalize, save `data/ticket.json` |
| v0.2 Ticket Intake API | ✅ | `POST /tickets` with strict request validation, ticket store, GET endpoints |
| v0.3 Persistence + execution logs | ✅ | SQLAlchemy models (`sqlite` default, PostgreSQL via `FLOWOPS_DATABASE_URL`), every run logged |
| v0.4 Routing + notifications + webhooks | ✅ | Rule-based category/priority/queue routing, outbound webhook with retries, n8n-style bulk intake |
| v0.5 Onboarding workflows + idempotency | ✅ | Onboarding/offboarding checklists with approval gates, `Idempotency-Key` support, delivery retries |
| v1.0-preview Monitoring dashboard | ✅ | `/dashboard`: run success/failure, durations, queue/priority breakdowns, est. time saved |
| v1.0 CI + Docker | ✅* | GitHub Actions (pytest, Python 3.10–3.13) and Docker Compose (API + PostgreSQL) — validated, Docker run pending a Docker-enabled machine |
| v0.6 Microsoft 365 actions | ⬜ | Real account creation via Power Automate / Microsoft Graph / PowerShell (needs credentials) |
| v0.7 AI support agent | ⬜ | MCP/Claude assistance with human approval gates (needs AI credentials) |

## Technology stack

- Python 3.10+
- FastAPI + Uvicorn — API and server
- Pydantic — strict request validation
- SQLAlchemy 2.0 — persistence (SQLite locally, PostgreSQL-ready)
- `requests` — HTTP client (CLI fetch, webhook delivery)
- `pytest` + `httpx` — offline test suite
- `psycopg` — PostgreSQL driver for the optional container deployment

## Project structure

```text
flowops/
├── src/
│   ├── ticket_processor.py   # v0.1 CLI: fetch, validate, normalize, save
│   ├── api.py                # FastAPI app: intake, routing, onboarding, dashboard
│   ├── db.py                 # SQLAlchemy models, session management, logging helper
│   ├── routing.py            # pure classification/priority/queue rules
│   ├── notify.py             # webhook delivery with exponential-backoff retries
│   └── onboarding.py         # checklist templates
├── tests/                    # 82 offline tests (test_*.py)
├── data/                     # runtime artifacts (git-ignored): flowops.db, ticket.json
├── .github/workflows/ci.yml  # GitHub Actions: install dependencies and run pytest
├── Dockerfile                # Small API image
├── docker-compose.yml        # API + PostgreSQL demo stack
├── .gitignore  .env.example  requirements.txt  README.md
```

## Installation and running (macOS)

```bash
cd flowops
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# CLI (v0.1)
python -m src.ticket_processor

# API
uvicorn src.api:app --reload        # http://127.0.0.1:8000
```

Key URLs: `/` landing page · `/docs` interactive API docs · `/dashboard` monitoring.

### PostgreSQL (optional)

```bash
export FLOWOPS_DATABASE_URL="postgresql+psycopg://user:password@localhost/flowops"
uvicorn src.api:app
```

Without this variable the app uses `sqlite:///data/flowops.db`. No dotenv dependency — plain environment variables (see `.env.example`).

### Docker Compose (optional)

The included compose file runs the API against PostgreSQL and exposes it at
`http://127.0.0.1:8000`. Docker must be installed locally.
**Note:** the files are syntax-validated but have not yet been exercised end to
end (no Docker on the development machine) — verify with the `curl` below on
first use.

```bash
docker compose up --build
curl http://127.0.0.1:8000/health
docker compose down
```

The database uses a named Docker volume so `docker compose down` stops the
services without deleting the stored PostgreSQL data. Use
`docker compose down -v` when you intentionally want to remove that volume.

## API overview

| Method & path | Purpose |
|---|---|
| `GET /health` | Liveness probe |
| `POST /tickets` | Validate, route, store a ticket. `201` new / `200` update. Supports `Idempotency-Key` header: replays return the stored response with `Idempotency-Replayed: true` and do **not** re-execute |
| `GET /tickets`, `GET /tickets/{id}` | Read tickets (sorted by ID; unknown/non-numeric ID → 404) |
| `POST /webhooks/intake` | n8n-friendly intake: single ticket or bulk list; always `200` on success, `422` rejects the whole batch if any item is invalid |
| `POST /onboarding-requests` | Create onboarding/offboarding request → generates its checklist |
| `GET /onboarding-requests[/{id}]` | Read requests with checklists |
| `POST .../items/{item}/approve` | Approve a step that requires sign-off (`409` if not approvable in current state) |
| `POST .../items/{item}/complete` | Complete a step (`409` unless approved-if-required). When all steps complete, the request flips to `completed` |
| `GET /execution-logs?status=&limit=` | Recent workflow runs, newest first |
| `GET /dashboard` | Server-rendered HTML monitoring dashboard |

## How routing works

`src/routing.py` is a set of **pure functions** (the same shape as branch logic in Rewst or n8n):

- **Category** — keyword rules on the title (password, email, network, hardware, software, access, else `general`).
- **Priority** — `urgent` for outages/all-users impact, `high` for security terms or password issues, else `normal`.
- **Queue** — category → owning queue (e.g. password/access → `it-access-management`, hardware → `it-hardware`, fallback `it-service-desk`).

Example: `"VPN outage - all users cannot work"` → `network` / `urgent` / `it-network`.

Onboarding checklists: each step has an owning queue and an approval flag; approval-gated steps refuse completion until approved. On every workflow action a row is written to `execution_logs` (run id, action, status, duration, detail) — that log feeds the dashboard.

## Example session

```bash
curl -X POST http://127.0.0.1:8000/tickets -H 'Content-Type: application/json' \
  -d '{"userId":7,"id":42,"title":"VPN outage - all users cannot work","completed":false}'
# 201 → {"ticket_id":"42","requester_id":7,"status":"open","priority":"urgent","category":"network","queue":"it-network", ...}

curl -X POST http://127.0.0.1:8000/onboarding-requests -H 'Content-Type: application/json' \
  -d '{"request_type":"onboarding","employee_name":"Jane Doe","employee_email":"jane@example.com","department":"Sales","requester_id":3,"start_date":"2026-11-02"}'
# 201 → request with 4 checklist steps (2 requiring approval)
```

## Error handling

- **Intake validation** — strict Pydantic types (`StrictInt`/`StrictStr`/`StrictBool`, positive IDs, non-blank titles) → `422` with field details; wrong top-level structure rejected.
- **HTTP/network (CLI & webhooks)** — `raise_for_status()`, 10s CLI timeout, webhook deliveries retried 3× with exponential backoff (0.5s → 1s) and reported to the execution log instead of raising, so a flaky webhook never fails intake.
- **Workflow state** — invalid checklist transitions return `409` with a clear reason (e.g. completing an unapproved gated step).
- **Idempotency** — replayed keys return the original response without re-execution; keys over 128 chars → `422`.
- `main()` (CLI) catches specific exception families only — no blanket `except`, no silent failures.

## Security considerations

- No credentials in code; `.env` git-ignored; no dotenv dependency.
- All input untrusted: strict type validation, ticket IDs must be numeric before any lookup (blocks path traversal), dashboard HTML output escaped (`html.escape`).
- **No authentication yet** — bind to localhost for development; do not expose publicly.
- Runtime artifacts (`data/*.json`, `data/tickets/`, `data/*.db`) git-ignored.

## Running the tests

```bash
pytest    # 82 tests, fully offline: mocked HTTP, SQLite in tmp dirs
```

## Roadmap status

- ✅ v0.1–v0.5 and the monitoring dashboard (see table above)
- ⬜ v0.6 — Power Automate / Microsoft Graph / PowerShell behind the onboarding checklists (needs real credentials)
- ⬜ v0.7 — Controlled MCP and Claude-powered ticket assistance with human approval gates (needs AI credentials)
- ✅ v1.0 foundation — Offline CI workflow and Docker Compose PostgreSQL demo
- ⬜ v1.0 — Deployment, screenshots, and portfolio demonstration
