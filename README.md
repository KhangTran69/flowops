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
| v1.0 UI: operator console | ✅ | Jinja2 templates, responsive dashboard, onboarding/outbox pages, theme toggle, inline actions |
| v1.0 Reliability: durable workflow jobs | ✅ | Leased worker queue with retries, dead-letter, operator retry/cancel |
| v1.0 Security: auth, roles, audit | ✅ | Optional bearer/Entra auth, role checks, webhook API key, actor attribution |
| v1.1 Secure operator console | ✅ | Signed session login, CSRF-protected UI actions, role-aware controls, login/logout audit |
| v1.1 Workflow DAG engine | ✅ | Step dependencies, blocked/cascade states, output piping, signed single-use ChatOps approvals |
| v1.2 Workflow orchestrator | ✅ | DB-backed run/step/event ledger, leased scheduler, parallel ready steps, pause/resume/cancel, DAG metrics |
| v1.3 Versioned definitions | ✅ | Immutable workflow versions, safe predicates, all/any joins, SLA timers, runtime migration |
| v0.6.1 Offboarding completion ledger | ✅ | Admin-only, claim-before-execute, idempotent replay, stale-claim takeover, ledger-backed offboarding |
| v1.0 Operations: readiness, metrics, migrations | ✅ | `/ready`, `/metrics`, correlation IDs, Alembic baseline, container migration entrypoint |
| v1.0 CI + Docker | ✅ | GitHub Actions runs pytest, a Docker Compose API + PostgreSQL smoke test, and PostgreSQL reliability tests |
| v0.6 Microsoft 365 actions | ⬜ | Real account creation via Power Automate / Microsoft Graph / PowerShell (needs credentials) |
| v0.7 AI support agent | ✅ | Simulated AI triage, KB search, suggest-only recommendations, human-approved apply gate |

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
│   ├── onboarding.py         # checklist templates
│   ├── jobs.py               # durable workflow queue: enqueue, lease, retry, cancel
│   ├── worker.py             # leased worker process for onboarding actions
│   ├── outbox.py             # leased outbox claim/finalize helpers
│   ├── outbox_worker.py      # automatic notification dispatcher
│   ├── auth.py               # bearer/Entra auth and role helpers
│   ├── ui_session.py         # signed UI sessions, cookie flags, and CSRF helpers
│   ├── ai_agent.py           # simulated AI agent, KB search, optional LLM boundary
│   ├── web.py                # Jinja2 rendering helpers
│   ├── templates/            # shared layout, landing, dashboard, operator pages
│   └── static/               # local CSS and vanilla JavaScript
├── migrations/               # Alembic baseline and environment
├── tests/                    # offline test suite (test_*.py); PostgreSQL reliability tests opt in via env
├── data/                     # runtime artifacts (git-ignored): flowops.db, ticket.json
├── .github/workflows/ci.yml  # GitHub Actions: SQLite matrix, migrations, PostgreSQL smoke + reliability tests
├── Dockerfile                # Non-root API/worker image with migration entrypoint
├── docker-compose.yml        # API + worker + PostgreSQL demo stack
├── alembic.ini               # Migration configuration
├── src/ai_agent.py           # simulated AI agent, KB, optional LLM boundary
├── docs/portfolio-demo.md    # Guided interview/portfolio walkthrough
├── docs/architecture.md      # Data flow and reliability boundaries
├── scripts/demo_smoke.sh     # Local end-to-end smoke test
├── scripts/load_smoke.py     # Small concurrent intake/replay smoke test
├── scripts/ai_smoke.py       # Opt-in live AI provider smoke check
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

The included compose file runs the API, a durable workflow worker, an
automatic outbox dispatcher, and PostgreSQL. The app entrypoint runs
`alembic upgrade head`; the workers wait for a healthy app. Services use
restart policies and stop grace periods; the image runs as the non-root
`flowops` user. The API is exposed at `http://127.0.0.1:8000`. Docker must be
installed locally for this command. The same build and smoke test also run in
the GitHub Actions `docker-postgres` job, and the `postgres-idempotency` job
runs the PostgreSQL reliability tests against a PostgreSQL service.

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
| `POST .../items/{item}/execute` | Synchronous onboarding action execution (kept for compatibility; optional auth) |
| `POST /offboarding-requests/{checklist_id}/steps/{step_key}/complete` | Admin-only, idempotent offboarding completion via the claim ledger |
| `POST .../items/{item}/jobs` | Enqueue a durable workflow job and return `202` |
| `GET /workflow-jobs?status=&limit=` | List durable jobs; `GET /workflow-jobs/{id}` for one |
| `POST /workflow-jobs/{id}/retry` | Return a failed job to the queue with a fresh attempt budget |
| `POST /workflow-jobs/{id}/cancel` | Cancel a queued or retry-scheduled job |
| `GET /execution-logs?status=&actor=&limit=` | Recent workflow runs, newest first |
| `GET /audit?limit=` | Actor-attributed execution rows |
| `POST /tickets/{id}/ai-triage` | Summarize a ticket, search the KB, and return suggestions (no mutation) |
| `POST /tickets/{id}/ai-recommendations/apply` | Operator-approved apply of queue/priority recommendations |
| `GET /knowledge-base?query=&category=` | List or search the simulated IT knowledge base |
| `GET /ui/tickets[/{id}]` | Operator ticket list/detail with an AI triage panel |
| `GET /ready` | Readiness probe including database connectivity |
| `GET /metrics` | Prometheus-style request, job, and outbox metrics |
| `GET /dashboard` | Server-rendered HTML monitoring dashboard |
| `GET /ui/onboarding-requests[/{id}]` | Operator onboarding list/detail pages |
| `GET /ui/notifications` | Operator pending/dead-letter outbox page |
| `GET /ui/workflow-jobs` | Operator workflow queue page |

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

### AI support agent (v0.7)

The AI boundary is suggest-only by default:

- `POST /tickets/{id}/ai-triage` returns a structured summary, urgency signals,
  ranked knowledge-base matches, and recommendations. It never mutates the
  ticket.
- Recommendations are stable IDs such as `queue-reassign`, `priority-upgrade`,
  `reply-draft`, and `onboarding-checklist`. Only queue/priority are applicable.
- `POST /tickets/{id}/ai-recommendations/apply` requires an operator actor and
  an explicit list of recommendation IDs. It re-runs triage server-side and
  applies only those IDs; status is never changed by AI.
- The simulated adapter is deterministic and credential-free. `FLOWOPS_AI_ADAPTER`
  can select an OpenAI-compatible provider: `gemini`, `groq`, `openai`,
  `openrouter`, `ollama`, or generic `llm`. Set `FLOWOPS_AI_API_KEY` plus
  optional `FLOWOPS_AI_BASE_URL`, `FLOWOPS_AI_MODEL`, and `FLOWOPS_AI_TIMEOUT`.
  The built-in client calls `/chat/completions`; tests inject
  `httpx.MockTransport` and never touch the network. `ollama` runs locally and
  needs no key.
- Every triage and apply action writes `ai.summarize`, `ai.recommend`, or
  `ai.recommendation_applied` execution logs with actor, duration, and
  correlation ID.
- `/ui/tickets/{id}?triage=1` renders the summary, KB guidance, and approval
  checkboxes; the Apply button calls the approval endpoint.

AI safety guardrails:

- Ticket text is redacted (emails, phone numbers, tokens, credentialed URLs)
  and truncated before an LLM prompt. Ticket content is delimited as untrusted
  data with explicit instructions to ignore embedded instructions.
- LLM output is schema-validated: only known recommendation kinds are kept,
  queue/priority suggestions must use known values, confidence is clamped, and
  only queue/priority may be applicable. Status/title/requester changes are
  impossible through the apply endpoint.
- Provider failures return `503` and write an `ai.error` log with actor and
  correlation ID.
- `tests/test_ai_eval.py` runs a deterministic golden evaluation over 12
  synthetic tickets (top-1 KB accuracy threshold, queue/priority recall,
  workflow suggestions never applicable, no false positives on correct
  tickets).
- `/metrics` exposes `flowops_ai_triage_total`, `flowops_ai_apply_total`, and
  `flowops_ai_provider_errors_total`; the dashboard has an AI activity panel.
- `scripts/ai_smoke.py` is an opt-in live provider smoke check. It reads
  `FLOWOPS_AI_*` from the environment, never prints the key, and is not part of
  CI.

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
- **Authentication is optional** — local development defaults to no auth; set `FLOWOPS_AUTH_REQUIRED=true` and configure a dev bearer token or Entra JWT before exposing the service. Offboarding completion is admin-only. Bind to localhost for demos.
- Runtime artifacts (`data/*.json`, `data/tickets/`, `data/*.db`) git-ignored.

### Offboarding completion ledger

Offboarding is destructive, so it uses a dedicated admin-only ledger endpoint:
`POST /offboarding-requests/{checklist_id}/steps/{step_key}/complete`.

- The ledger row is **claimed before the side effect runs**, so concurrent
  callers cannot execute the same step twice.
- The first completion returns `201`; a replay returns `200` with
  `X-Offboarding-Replayed: true`.
- A fresh in-flight claim returns `409`; a claim older than the five-minute
  recovery window can be taken over with a new owner token.
- Unknown step keys return `422`; onboarding checklists, closed/abandoned
  checklists, and unapproved gated steps return `409`; missing checklists `404`.
- Executor failure removes the claim so the step can be retried.
- Every path writes an actor-attributed execution log. The operator UI uses
  the ledger endpoint for offboarding steps, and the dashboard shows
  offboarding progress.

## Workflow DAG and ChatOps approvals (v1.1)

Checklist templates can declare `depends_on` prerequisites. The default
`onboarding-standard`/`offboarding-standard` templates stay flat for backward
compatibility; opt into `onboarding-dag` or `offboarding-dag` with
`"template": "..."` when creating a request.

- Steps with unmet prerequisites start as `blocked`; completing all
  prerequisites promotes them to `pending` (or `approved` for gated steps).
- A failed, rejected, or cancelled prerequisite cascades dependents to
  `cascade_blocked`.
- Structured action outputs are persisted in `output_json` and passed to
  downstream actions as `context["prerequisite_outputs"]` and
  `context["upstream_outputs"]`.
- `POST /webhooks/actions/approve?token=...` and
  `POST /webhooks/actions/reject?token=...` accept HMAC-SHA256 tokens bound to
  request/item/action with a 24-hour TTL. Tokens are single-use
  (`chatops_token_uses`) and log actor `chatops:slack`, `chatops:teams`,
  `chatops:email`, or `chatops:webhook`.
- Set `FLOWOPS_CHATOPS_SECRET` to enable links. The operator detail page shows
  dependency badges, blocker state, step outputs, and ChatOps buttons.
- Tests: `tests/test_workflow_dag.py` (offline).

## Workflow orchestrator (v1.2)

`src/scheduler.py` adds a DB-backed orchestrator on top of the v1.1 DAG:

- `workflow_runs`, `workflow_step_runs`, and `workflow_events` are migration
  `0007_workflow_orchestrator`.
- Starting a run enqueues every ready non-gated step in parallel, bounded by
  `FLOWOPS_DAG_MAX_PARALLEL`.
- Gated steps are never auto-approved; operator/ChatOps approval makes them
  schedulable.
- Worker completion/failure advances the run immediately; polling is only a
  recovery path.
- Run leases (`FLOWOPS_DAG_LEASE_SECONDS`) fence scheduler ticks; stale leases
  can be taken over.
- `POST /onboarding-requests/{id}/run`, `GET .../run`,
  `POST /workflow-runs/{id}/pause|resume|cancel`, `GET /workflow-runs`.
- `python -m src.scheduler --once` or `--poll --interval 2`.
- `/ui/workflow-runs` and the onboarding detail page show run/step state,
  attempts, outputs, and pause/resume/cancel controls.
- `/metrics` exposes `flowops_dag_runs`, `flowops_dag_steps`, and
  `flowops_dag_scheduler_ticks_total`.

## Versioned workflows and SLA timers (v1.3)

Workflow definitions can be stored as immutable versions (migration `0008_workflow_definitions`):

- `POST /workflow-definitions`, `PUT /workflow-definitions/{key}`,
  `POST /workflow-definitions/{key}/publish`, `GET /workflow-definitions`.
- Requests pin `definition_key`/`definition_version` at creation; publishing a
  new version never mutates in-flight work.
- Steps support `join: all|any`, safe `when` predicates evaluated by
  `src/expressions.py`, and `sla_minutes`.
- False predicates mark a step `skipped`; unknown identifiers fail closed.
- `workflow_timers` plus `src/timers.py` fire leased, conditional SLA alarms,
  write `onboarding.step.overdue`, enqueue escalation notices, and can reassign
  the queue. Run `python -m src.timers --once` or `--poll`.
- `POST /onboarding-requests/{id}/definition` previews/applies a safe version
  migration while preserving completed outputs.
- Definition pages: `/ui/workflow-definitions`; request detail shows overdue/SLA
  badges and skip state.

## Operator UI

The HTML UI is server-rendered with Jinja2 templates and local CSS/vanilla JS
(no CDN, no build pipeline). `/` is the landing page; `/dashboard` shows run
health, routing bars, pending deliveries, and dead letters. Operator pages live
under `/ui/...`: onboarding list/detail with approve/complete/execute actions,
the notification outbox with retry/requeue buttons, the durable job queue with
job detail pages, and the actor audit trail. Lists support pagination/filter
controls, the theme toggle stores a preference, and layouts respect
`prefers-color-scheme`.

## Durable workflow jobs

Onboarding actions can run through a durable queue instead of only inside the
HTTP request:

- `POST /onboarding-requests/{id}/items/{item}/jobs` enqueues a job (202).
- A worker claims due jobs with a unique owner token and a 60-second lease.
- The worker heartbeats the lease while a slow action runs; a takeover stops
  the stale owner from finalizing. Actions receive a stable
  `idempotency_key` (`onboarding:{request_id}:{item_id}`), forwarded by the
  Graph/Power Automate adapters where supported.
- `python -m src.worker --once` processes one job; `python -m src.worker`
  polls continuously. The Compose stack runs the worker as a separate service.
- Failures retry with exponential backoff up to `max_attempts` (default 5),
  then become `failed` (dead-letter). `POST /workflow-jobs/{id}/retry` and
  `/cancel` are conditional operator transitions.
- The item update, execution log, audit row, and job completion commit in one
  transaction guarded by the lease token; a stale worker cannot finalize a
  takeover. Automatic external side effects remain at-least-once.

Outbox deliveries are also dispatched automatically: `python -m
src.outbox_worker` claims pending rows with a lease, delivers once, applies
backoff, and dead-letters at `FLOWOPS_NOTIFICATION_MAX_ATTEMPTS`. Manual
retry/requeue endpoints remain available.

## Authentication, roles, and audit

`src/auth.py` supports optional Entra JWT or a local development bearer token.
Set `FLOWOPS_AUTH_REQUIRED=true` to protect mutating routes; the default local
mode is unauthenticated for beginner-friendly development.

The browser console adds a separate session layer. Set
`FLOWOPS_UI_AUTH_REQUIRED=true`, `FLOWOPS_AUTH_REQUIRED=true`, and a random
`FLOWOPS_SESSION_SECRET`; then
`/dashboard` and `/ui/*` redirect to `/login`, where an operator pastes a dev
bearer token or Entra access token. The signed, HttpOnly, SameSite=Lax cookie
carries subject/roles and a per-session CSRF token. UI fetch actions send
`X-CSRF-Token`; cookie-authenticated mutations without it return `403`. Bearer
API clients remain CSRF-exempt. Set `FLOWOPS_SESSION_COOKIE_SECURE=true` behind
HTTPS. This is a prototype token-paste login, not full SSO or user management.

Operator routes
require `automation.operator` or `automation.admin`. `POST /webhooks/intake`
can additionally require `X-API-Key` via `FLOWOPS_WEBHOOK_API_KEY`.
Authenticated actions write `actor_subject` to execution logs; `GET /audit` and
`/execution-logs?actor=...` expose the trail, and `/ui/audit` renders it.
Local development is admin-equivalent; protected deployments can distinguish
operator and admin roles (requeue/cancel are admin-only). Missing auth
configuration fails fast at startup. `FLOWOPS_RATE_LIMIT_PER_MINUTE` enables a
small in-process limit for mutating requests; it is off by default and is not
a multi-process production limiter. `GET /auth/me` returns the resolved actor.
No real secrets or Microsoft tenant are included.

## Readiness, metrics, and migrations

- `GET /health` is liveness; `GET /ready` checks database connectivity.
- `GET /metrics` exposes request counters plus workflow-job and outbox gauges.
- Every response carries `X-Correlation-ID` (or echoes the caller's value).
- Alembic owns the schema: `alembic upgrade head`. The Docker entrypoint runs
  it before starting the app; the worker waits for a healthy app and skips
  migrations. The baseline is safe for existing databases: it creates missing
  tables and adds the additive fencing/scope/actor columns without dropping
  data. Docker runs as the non-root `flowops` user.

## Running the tests

```bash
pytest    # 293 passed, 15 skipped locally; the skips are PostgreSQL-only tests
python -m compileall -q src tests
```

The default suite is fully offline: mocked HTTP and temporary SQLite databases. The PostgreSQL reliability tests run when `FLOWOPS_TEST_POSTGRES_URL` points at a disposable database:

```bash
FLOWOPS_TEST_POSTGRES_URL="postgresql+psycopg://flowops:flowops@127.0.0.1:5432/flowops" \
  pytest -q tests/test_idempotency_postgres.py tests/test_outbox_postgres.py \
           tests/test_jobs_postgres.py tests/test_outbox_worker_postgres.py \
           tests/test_offboarding_postgres.py tests/test_ai_postgres.py
```

The CI `postgres-idempotency` job supplies a `postgres:16` service, runs
`alembic upgrade head`, and exercises the same idempotency, fencing,
dead-letter, requeue, and workflow-job assertions on every push and pull
request. The matrix also checks the SQLite migration path and the full suite.
`scripts/load_smoke.py` runs a small concurrent create/replay smoke test against
a live local API (not a benchmark).

## Roadmap status

- ✅ v0.1–v0.5 and the monitoring dashboard (see table above)
- ⬜ v0.6 — Power Automate / Microsoft Graph / PowerShell behind the onboarding checklists (needs real credentials)
- ✅ v0.7 — Simulated AI triage, KB search, and human-approved recommendations (optional pluggable LLM boundary)
- ✅ v1.0 foundation — Offline CI workflow and Docker Compose PostgreSQL demo
- ✅ v1.0 reliability — Fenced idempotency ownership with stale-takeover protection on SQLite and PostgreSQL
- ✅ v1.0 reliability — Onboarding idempotency and notification dead-letter/requeue workflow
- ✅ v1.0 UI — Server-rendered operator console with onboarding, outbox, and job pages
- ✅ v1.0 reliability — Durable leased workflow jobs with retries, dead-letter, and operator controls
- ✅ v1.0 security — Optional bearer/Entra auth, roles, webhook API key, and actor audit trail
- ✅ v1.0 operations — Readiness, metrics, correlation IDs, Alembic migrations, non-root container
- ✅ v1.0 reliability — Worker heartbeats, adapter idempotency keys, automatic outbox dispatcher
- ✅ v0.6.1 reliability — Admin-only, claim-first offboarding completion ledger
- ✅ v1.0 experience — Audit trail, job detail, pagination/filters, admin/rate-limit hardening
- ✅ v1.0 foundation — Guided portfolio demo and repeatable local smoke test
- ⬜ v1.0 — Public deployment and screenshots
