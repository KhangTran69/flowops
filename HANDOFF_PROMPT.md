# FlowOps — AI Handoff Prompt

Copy everything below this line into the AI assistant of your IDE to continue the project.

---

## Role

Act as a senior Python backend engineer and automation engineer. Continue developing **FlowOps**, an IT workflow automation platform, in the current workspace (project root contains this file, `src/`, `tests/`, `data/`, `README.md`). Create real files, run the test suite, verify with live requests, and only report what you actually verified. This is a learning/portfolio project targeting Automation Engineer roles (Rewst, Power Automate, n8n, REST APIs, webhooks, SQL, workflow reliability) — keep it interview-ready and beginner-readable.

## Current state (verified complete as of 2026-10-17 — do not rebuild, extend it)

Milestones v0.1–v0.5 plus the monitoring dashboard and fenced idempotency are implemented and tested (114 offline pytest tests passing, plus 3 PostgreSQL-only tests that skip unless `FLOWOPS_TEST_POSTGRES_URL` is set):

- **v0.1 CLI** — `src/ticket_processor.py`: fetches a JSONPlaceholder todo (`https://jsonplaceholder.typicode.com/todos/1`), strict validation (`TicketValidationError`, rejects bool-as-int, empty titles, non-object payloads), normalizes to the internal format (`ticket_id`, `requester_id`, `title`, `status` open/closed, `priority`), saves `data/ticket.json`. Run: `python -m src.ticket_processor`.
- **v0.2+ API** — `src/api.py` (FastAPI): `POST /tickets` (strict Pydantic: `StrictInt`/`StrictStr`/`StrictBool`, ge=1, non-blank title), `GET /tickets`, `GET /tickets/{id}` (404 for non-numeric IDs — path-traversal safe), `GET /health`, HTML landing page at `/`, Swagger at `/docs`.
- **v0.3 persistence** — `src/db.py` (SQLAlchemy 2.0): `TicketRecord`, `ExecutionLogRecord`, `IdempotencyRecord`, `OnboardingRequestRecord`, `ChecklistItemRecord`. Default SQLite at `data/flowops.db`; PostgreSQL via `FLOWOPS_DATABASE_URL=postgresql+psycopg://...`. `db.get_session()` is the FastAPI dependency; `db.log_execution()` writes run logs (run_id, action, status, ticket_id, duration_ms, detail).
- **v0.4 routing + notifications** — `src/routing.py`: **pure functions** (keyword → category; urgency keywords + category → priority normal/high/urgent; category → queue like `it-access-management`, fallback `it-service-desk`). `src/notify.py`: `deliver_webhook()` retries 3× with exponential backoff (0.5s, 1s), returns a `DeliveryResult` and never raises; API logs the outcome as `notification.webhook`. Bulk n8n-style intake: `POST /webhooks/intake` accepts one ticket or a list (422 rejects the whole batch).
- **v0.5 onboarding + idempotency** — `src/onboarding.py`: checklist templates (onboarding/offboarding, each step has queue + `requires_approval`). API: `POST /onboarding-requests` → generates checklist; `GET /onboarding-requests/{id}/status` returns lightweight progress summary (total, completed, pending approval, percent); `.../items/{id}/approve` and `.../complete` with state machine (gated steps must be approved before completion → else 409); when all steps complete, the request flips to `completed`. `POST /tickets` honors an `Idempotency-Key` header (≤128 chars): same-payload replay returns the stored response with `Idempotency-Replayed: true`; concurrent matching requests get 409 while the original is processing; reusing the key with a different payload returns 409. Legacy stored keys predating payload binding return 409.
- **Fenced idempotency (v1.0 reliability)** — every successful claim stores a unique `owner_token` (fencing token). A processing reservation older than five minutes can be taken over; the takeover atomically rotates the token and refreshes `created_at` (the lease start). Completion is a conditional update that also requires `owner_token` and the `processing` state, and runs in the same transaction as the ticket and execution-log writes. If ownership changed, those writes roll back and the stale request gets a retryable 409. Error cleanup is a conditional delete with the same token, so a stale owner cannot remove the new reservation. Completed records are never reclaimed. SQLite uses the upsert's affected row count; PostgreSQL uses `RETURNING owner_token` because `psycopg` reports `rowcount = -1` for `INSERT ... ON CONFLICT DO UPDATE`.
- **Dashboard** — `GET /dashboard` (server-rendered HTML, `html.escape`d): ticket counts, queue/priority breakdowns, success/failure run counts, avg duration, estimated staff time saved (15 min per auto-routed ticket).
- **Tests** — `tests/test_ticket_processor.py`, `test_api.py`, `test_routing.py`, `test_notify.py`, `test_onboarding.py`, `test_actions.py`, `test_idempotency_postgres.py`. All HTTP mocked; file/DB tests use `tmp_path` + per-test SQLite. Run with `pytest` (114 passing, 3 PostgreSQL-only tests skipped locally, 1 harmless Starlette TestClient deprecation warning). Coverage includes same-payload replay, changed-payload conflict, concurrent same-key requests, stale reservation recovery without reclaiming completed keys, deterministic lease takeover and fencing (stale owner can neither finalize nor delete the new owner's reservation; its ticket/log writes roll back), owner-only failure cleanup, additive migration of pre-fencing databases, impossible calendar dates, legacy idempotency keys, onboarding status, and simulated action retries. `test_idempotency_postgres.py` runs when `FLOWOPS_TEST_POSTGRES_URL` is set and is exercised by the CI `postgres-idempotency` job.
- **Stack** — Python 3.10+ (3.13 in dev), venv at `.venv`, `requirements.txt`: requests, pytest, fastapi, uvicorn, httpx, sqlalchemy. Run API: `uvicorn src.api:app --reload`. No dotenv dependency — plain env vars (`FLOWOPS_SAMPLE_API_URL`, `FLOWOPS_DATABASE_URL`, `FLOWOPS_TICKET_STORE_DIR`, `FLOWOPS_NOTIFICATION_WEBHOOK_URL`), documented in `.env.example`.
- GitHub repository: `https://github.com/KhangTran69/flowops.git`, default branch `main`; runtime artifacts (`data/*.json`, `data/tickets/`, `data/*.db`) are git-ignored via `.gitkeep`-tracked `data/`.

## Long-term vision (four pillars)

1. **Automated ticket management** — classify, prioritize, route to queues, notify (rule-based core done; notifications are webhook-based).
2. **Employee onboarding/offboarding** — checklists for accounts, equipment, software access, approvals, tracked to completion (checklist engine done).
3. **AI support agent** — summarize ticket history, suggest KB solutions, recommend actions; **human approval required for sensitive actions**.
4. **Automation monitoring dashboard** — run success/failure, processing time, tickets auto-handled, staff time saved (preview done at `/dashboard`).

## Next steps (in order)

1. **v1.0 polish** (done 2026-10-17): GitHub Actions CI (`.github/workflows/ci.yml`, pytest on 3.10–3.13, plus a PostgreSQL deployment job and a `postgres-idempotency` job for the fencing tests) and Docker Compose (`Dockerfile`, `docker-compose.yml` — API + PostgreSQL 16, `FLOWOPS_DATABASE_URL` prewired, `psycopg[binary]` in requirements). **Docker Compose verified end to end on 2026-10-17**: `docker compose up --build` succeeded, all 5 tables created in Postgres, ticket POST/GET and execution logs verified against the containerized database. The repo is pushed to https://github.com/KhangTran69/flowops.git (private) with green CI.
2. **v0.6 Microsoft actions** (needs real credentials from the user — ask before starting): wire onboarding checklist steps to real actions via Microsoft Graph / Power Automate / PowerShell; add per-step retry + failure alerts; keep everything behind approval gates.
3. **v0.7 AI support agent** (needs AI credentials from the user): MCP/Claude-powered ticket assistance — summarize history, suggest knowledge-base solutions, recommend actions; sensitive actions require explicit human approval in the workflow.

## Known reliability limits

- Outbound webhook *delivery* is outside the reservation transaction. A stale owner may already have sent a notification before it loses its lease, even though its ticket, execution-log, and outbox writes roll back. Receivers should deduplicate using the ticket ID or an event key.
- The five-minute lease is a recovery window, not a hard request timeout. A healthy slow request keeps ownership until another request takes over and rotates the token.
- Lease staleness uses wall-clock time (`db.utcnow()`). Clock skew between hosts can cause a premature takeover; the fence keeps that safe because only one token can finalize, but keep host clocks synchronized.
- PostgreSQL fencing coverage requires `FLOWOPS_TEST_POSTGRES_URL`; it skips locally and runs in CI. SQLite remains the default offline development path.
- The fencing column is added by a small additive `ALTER TABLE` compatibility step. There is no general migration framework yet.

## Rules

- Extend the existing architecture (pure routing functions, strict validation at the boundary, execution logging on every workflow action). No new frameworks without asking.
- Tests stay fully offline: mock HTTP, use `tmp_path`, per-test SQLite. Never depend on internet access.
- Never claim a command, test, or API request succeeded unless you ran it and saw the output.
- Do not commit or push to GitHub unless the user explicitly asks.
- Clearly label simulated/learning-prototype status; never call the project production-ready. The API has no auth yet — localhost only.
- Avoid overengineering: small functions with clear responsibilities, no class hierarchies, concise docstrings, type hints where they help.

## First command to run

```bash
source .venv/bin/activate && pytest -q
```

Then `uvicorn src.api:app --reload` and curl `/health`, `POST /tickets`, `/dashboard` to confirm the state matches this prompt. Report any drift between this handoff and the actual code before making changes.
