# FlowOps

[![CI](https://github.com/KhangTran69/flowops/actions/workflows/ci.yml/badge.svg)](https://github.com/KhangTran69/flowops/actions/workflows/ci.yml)

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
| v0.5 Onboarding workflows + idempotency | ✅ | Onboarding/offboarding checklists with approval gates, fenced `Idempotency-Key` support for tickets and onboarding, delivery retries |
| v1.0-preview Monitoring dashboard | ✅ | `/dashboard`: run success/failure, durations, queue/priority breakdowns, est. time saved |
| v1.0 Reliability: fenced idempotency | ✅ | Per-claim owner tokens prevent a stale request from finalizing or deleting a takeover reservation |
| v1.0 Reliability: dead-letter retries | ✅ | Exhausted outbox deliveries become `failed`; operators can inspect and requeue them |
| v1.0 CI + Docker | ✅ | GitHub Actions runs pytest, a Docker Compose API + PostgreSQL smoke test, and PostgreSQL reliability tests |
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
├── tests/                    # offline test suite (test_*.py); PostgreSQL reliability tests opt in via env
├── data/                     # runtime artifacts (git-ignored): flowops.db, ticket.json
├── .github/workflows/ci.yml  # GitHub Actions: SQLite matrix, PostgreSQL smoke + reliability tests
├── Dockerfile                # Small API image
├── docker-compose.yml        # API + PostgreSQL demo stack
├── docs/portfolio-demo.md    # Guided interview/portfolio walkthrough
├── docs/architecture.md      # Data flow and reliability boundaries
├── scripts/demo_smoke.sh     # Local end-to-end smoke test
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

For a guided five-minute walkthrough, see
[`docs/portfolio-demo.md`](docs/portfolio-demo.md). Once the API is running,
the repeatable local smoke test is:

```bash
./scripts/demo_smoke.sh
```

The high-level data flow and design boundaries are documented in
[`docs/architecture.md`](docs/architecture.md).

### PostgreSQL (optional)

```bash
export FLOWOPS_DATABASE_URL="postgresql+psycopg://user:password@localhost/flowops"
uvicorn src.api:app
```

Without this variable the app uses `sqlite:///data/flowops.db`. No dotenv dependency — plain environment variables (see `.env.example`).

### Docker Compose (optional)

The included compose file runs the API against PostgreSQL and exposes it at
`http://127.0.0.1:8000`. Docker must be installed locally for this command.
The same build and smoke test also run in the GitHub Actions
`docker-postgres` job, and the `postgres-idempotency` job runs the idempotency
and outbox tests against a PostgreSQL service.

```bash
docker compose up --build
curl http://127.0.0.1:8000/health
docker compose down
```

The host port can be changed when port 8000 is already occupied:
`FLOWOPS_APP_PORT=8001 docker compose up --build`.

The database uses a named Docker volume so `docker compose down` stops the
services without deleting the stored PostgreSQL data. Use
`docker compose down -v` when you intentionally want to remove that volume.

## API overview

| Method & path | Purpose |
|---|---|
| `GET /health` | Liveness probe |
| `POST /tickets` | Validate, route, store a ticket. `201` new / `200` update. Supports `Idempotency-Key`: same-payload replays return the stored response with `Idempotency-Replayed: true`; changed payloads return `409`; active requests return `409`, while abandoned processing reservations can be reclaimed after a five-minute lease. Each claim carries an owner token, so a stale owner that was taken over cannot finalize or delete the new reservation and receives a retryable `409` |
| `GET /tickets`, `GET /tickets/{id}` | Read tickets (sorted by ID; unknown/non-numeric ID → 404) |
| `POST /webhooks/intake` | n8n-friendly intake: single ticket or bulk list; always `200` on success, `422` rejects the whole batch if any item is invalid |
| `POST /onboarding-requests` | Create onboarding/offboarding request → generates its checklist. Supports the same fenced `Idempotency-Key` protocol as `POST /tickets` |
| `GET /onboarding-requests[/{id}]` | Read requests with checklists |
| `GET /onboarding-requests/{id}/status` | Lightweight status summary (total steps, completed, pending approval) for polling |
| `POST .../items/{item}/approve` | Approve a step that requires sign-off (`409` if not approvable in current state) |
| `POST .../items/{item}/complete` | Complete a step (`409` unless approved-if-required). When all steps complete, the request flips to `completed` |
| `GET /notifications/pending` | List outbox deliveries awaiting a retry |
| `GET /notifications/failed` | List dead-letter deliveries that exhausted their retry budget |
| `POST /notifications/retry?limit=` | Retry pending deliveries once each; failures become `failed` at the configured attempt limit |
| `POST /notifications/{id}/requeue` | Return one `failed` delivery to `pending`; `409` unless still failed |
| `GET /execution-logs?status=&limit=` | Recent workflow runs, newest first |
| `GET /dashboard` | Server-rendered HTML monitoring dashboard |

### Idempotency lease and fencing

`POST /tickets` and `POST /onboarding-requests` with an `Idempotency-Key` follow a small reservation protocol:

1. The key is claimed atomically before any ticket or onboarding work. The claim stores a unique owner token, a `processing` marker, and the lease start time.
2. A completed matching call replays its stored response; a changed payload returns `409`; a live processing reservation returns `409`.
3. If the owner crashes or takes longer than five minutes, a matching retry can take over the row. The takeover atomically rotates the owner token and refreshes the lease timestamp.
4. Completion is a conditional update (`key = ... AND owner_token = ... AND state = processing`) in the same transaction as the ticket/onboarding and execution-log writes. If ownership changed, all of those writes roll back and the request gets a retryable `409`; the new owner's reservation and response are untouched.
5. Error cleanup uses the same conditional delete, so a stale owner can remove only its own reservation.
6. Completed records are never reclaimed, regardless of age.
7. Each key is scoped to its resource type (`ticket` or `onboarding`). Reusing a ticket key for onboarding, or vice versa, returns `409`.

A healthy slow request keeps its token and may still finish after five minutes. The lease only enables recovery: once another request rotates the token, the old request is fenced. The lease is not a hard request timeout.

SQLite and PostgreSQL share the conditional update/delete logic. The claim uses each dialect's upsert; because `psycopg` reports `rowcount = -1` for `INSERT ... ON CONFLICT DO UPDATE`, PostgreSQL confirms ownership with `RETURNING owner_token`, while SQLite uses the affected row count.

Durable ticket, execution-log, and notification-outbox writes are fenced. Outbound webhook *delivery* is not transactional: a stale owner could have already sent a notification before losing its lease, even though its database writes are rolled back.

### Notification delivery lifecycle

A failed in-request webhook delivery is stored in `notification_outbox` as `pending` with its attempt count and last error.

- `pending` → `delivered` when a retry succeeds.
- `pending` → `failed` when a retry fails and the attempt count reaches `FLOWOPS_NOTIFICATION_MAX_ATTEMPTS` (default `5`, bounded `1`–`20`).
- `failed` → `pending` via `POST /notifications/{id}/requeue`, conditional on the row still being `failed`.
- `POST /notifications/retry` never selects `failed` rows; an operator must requeue them first.

Requeue preserves the attempt history and last error, so each requeue grants one more delivery attempt and a still-failing delivery returns to `failed`. The conditional transition prevents two operators from requeueing the same row twice. Delivery remains **at-least-once**: the outbox prevents silent loss, but receivers should deduplicate using the ticket ID or event data.

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
- **Idempotency** — ticket and onboarding requests are atomically reserved before processing with a unique owner token. Concurrent duplicates receive `409` while the first request runs; completed same-payload retries replay the original response, while changed payloads return `409`. A key is scoped to its resource type, so reusing a ticket key for onboarding (or vice versa) also returns `409`. Processing reservations older than five minutes can be taken over atomically, which rotates the token and refreshes the lease. Completion and error cleanup are conditional on still owning that token: a stale owner cannot overwrite or delete the new reservation, its ticket/onboarding and execution-log writes roll back, and it receives a retryable `409`. Completed records are never reclaimed. Keep normal processing comfortably below the five-minute lease. Keys over 128 chars → `422`.
- **Notification outbox** — failed webhook deliveries are stored as `pending`. Retries are recorded on the row; when `attempts` reaches `FLOWOPS_NOTIFICATION_MAX_ATTEMPTS` (default `5`), the row becomes `failed` (dead-letter) and is excluded from automatic retries. `POST /notifications/{id}/requeue` uses a conditional update (`status = failed`) to return it to `pending`; attempt history and the last error are preserved. Delivery is at-least-once, so receivers should deduplicate.
- **Onboarding dates** — `start_date` must be both ISO-formatted and a real calendar date.
- `main()` (CLI) catches specific exception families only — no blanket `except`, no silent failures.

## Security considerations

- No credentials in code; `.env` git-ignored; no dotenv dependency.
- All input untrusted: strict type validation, ticket IDs must be numeric before any lookup (blocks path traversal), dashboard HTML output escaped (`html.escape`).
- **No authentication yet** — bind to localhost for development; do not expose publicly.
- Runtime artifacts (`data/*.json`, `data/tickets/`, `data/*.db`) git-ignored.

## Running the tests

```bash
pytest    # 145 passed, 8 skipped locally; the skips are PostgreSQL-only tests
python -m compileall -q src tests
```

The default suite is fully offline: mocked HTTP and temporary SQLite databases. The PostgreSQL reliability tests run when `FLOWOPS_TEST_POSTGRES_URL` points at a disposable database:

```bash
FLOWOPS_TEST_POSTGRES_URL="postgresql+psycopg://flowops:flowops@127.0.0.1:5432/flowops" \
  pytest -q tests/test_idempotency_postgres.py tests/test_outbox_postgres.py
```

The CI `postgres-idempotency` job supplies a `postgres:16` service, so the same idempotency, fencing, dead-letter, and requeue assertions run on every push and pull request.

## Roadmap status

- ✅ v0.1–v0.5 and the monitoring dashboard (see table above)
- ⬜ v0.6 — Power Automate / Microsoft Graph / PowerShell behind the onboarding checklists (needs real credentials)
- ⬜ v0.7 — Controlled MCP and Claude-powered ticket assistance with human approval gates (needs AI credentials)
- ✅ v1.0 foundation — Offline CI workflow and Docker Compose PostgreSQL demo
- ✅ v1.0 reliability — Fenced idempotency ownership with stale-takeover protection on SQLite and PostgreSQL
- ✅ v1.0 reliability — Onboarding idempotency and notification dead-letter/requeue workflow
- ✅ v1.0 foundation — Guided portfolio demo and repeatable local smoke test
- ⬜ v1.0 — Public deployment and screenshots
