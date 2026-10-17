# FlowOps portfolio demo

This is a five-minute walkthrough for showing the project in an interview or
portfolio review. It demonstrates the complete local workflow without real
credentials or external automation accounts.

## 1. Start the API

From the project root:

```bash
source .venv/bin/activate
uvicorn src.api:app --reload
```

Keep that terminal running. Open these pages in a browser:

- <http://127.0.0.1:8000/> — API landing page
- <http://127.0.0.1:8000/docs> — interactive OpenAPI documentation
- <http://127.0.0.1:8000/dashboard> — workflow monitoring dashboard

## 2. Run the guided smoke test

In a second terminal:

```bash
./scripts/demo_smoke.sh
```

The script checks liveness, submits a deliberately urgent network ticket,
replays it with the same idempotency key, and checks the dashboard response.
It only talks to the local FlowOps server.

## 3. Explain the workflow

Use this ticket as the example:

```json
{
  "userId": 7,
  "id": 9001,
  "title": "VPN outage - all users cannot work",
  "completed": false
}
```

FlowOps validates the source payload, normalizes the field names, classifies it
as `network`, assigns `urgent`, routes it to `it-network`, persists it, and
writes an execution-log record. The dashboard reads those logs and displays
successes, failures, duration, queue/priority breakdowns, and estimated staff
time saved.

The second request uses the same `Idempotency-Key`. It returns the original
result without running the workflow again, which is the reliability behavior
to call out when discussing retries or webhook redelivery.

## 4. Show the approval gate

The onboarding flow demonstrates a human-in-the-loop boundary:

1. `POST /onboarding-requests` creates a checklist.
2. Approval-required items reject completion until approved.
3. `POST .../approve` records the approval.
4. `POST .../complete` advances the step.
5. The request becomes `completed` after every checklist item is complete.

This is intentionally simulated. No Microsoft account, mailbox, device, or
permission is changed by the demo.

## Interview talking points

- Boundary validation uses strict Pydantic types and rejects booleans where IDs
  are expected.
- Routing rules are pure functions, so they are easy to test and replace with
  a workflow-platform branch later.
- Every workflow action produces an execution log for observability.
- Webhook delivery retries with exponential backoff and reports failure rather
  than breaking ticket intake.
- SQLite keeps local setup simple; the same SQLAlchemy models support
  PostgreSQL through `FLOWOPS_DATABASE_URL`.
- Authentication and real Microsoft 365 actions are deliberately deferred; the
  current app is a localhost learning prototype, not production software.

## Verification commands

```bash
pytest -q
python -m compileall -q src tests
```

For the optional PostgreSQL deployment:

```bash
docker compose up --build
curl http://127.0.0.1:8000/health
docker compose down
```

If port 8000 is already in use, run the stack on another host port:

```bash
FLOWOPS_APP_PORT=8001 docker compose up --build
curl http://127.0.0.1:8001/health
FLOWOPS_APP_PORT=8001 docker compose down
```

The same build, startup, health check, and PostgreSQL-backed ticket request
also run automatically in the `docker-postgres` GitHub Actions job. This keeps
the deployment path verifiable even when Docker Desktop is not installed on a
developer machine.


## 5. Walk through the operator UI

Open these pages:

- <http://127.0.0.1:8000/> — landing page with live counts and API status
- <http://127.0.0.1:8000/dashboard> — run health, routing bars, notification state
- <http://127.0.0.1:8000/ui/onboarding-requests> — onboarding list and checklist detail
- <http://127.0.0.1:8000/ui/notifications> — pending and dead-letter deliveries
- <http://127.0.0.1:8000/ui/workflow-jobs> — durable queue, job detail, retry/cancel
- <http://127.0.0.1:8000/ui/audit> — actor-attributed action history

The UI is server-rendered with Jinja2 and local CSS/vanilla JS. It works offline;
there are no CDN assets or build steps. Theme, pagination, and action buttons
are progressive enhancements; core content still renders without JavaScript.

## 6. Show the durable worker

Enqueue an onboarding action:

```bash
curl -X POST http://127.0.0.1:8000/onboarding-requests/1/items/2/jobs
```

Run one worker iteration locally:

```bash
python -m src.worker --once
```

Or with Docker Compose, the worker service processes jobs automatically. In the
UI, `/ui/workflow-jobs/{id}` shows the lease, attempts, payload, result, and
retry/cancel controls. A slow action heartbeats its lease; a takeover fences the
old owner. External actions remain at-least-once and receive a stable
`idempotency_key`.

## 7. Show operations and reliability

```bash
curl http://127.0.0.1:8000/ready
curl http://127.0.0.1:8000/metrics
curl -i http://127.0.0.1:8000/health   # includes X-Correlation-ID
alembic upgrade head
python scripts/load_smoke.py --requests 10 --concurrency 3
```

`/ready` checks the database; `/metrics` exposes request, job, and outbox
counters/gauges; Alembic owns the schema; the load smoke script is a correctness
check for concurrent intake/replay, not a benchmark.

## Talking points for reliability

- Idempotency reservations are fenced; stale owners cannot finalize or clean up
  a takeover.
- Workflow jobs and outbox deliveries use leases and conditional transitions.
- Retries use exponential backoff and dead-lettering; operators can requeue.
- Auth is optional locally, role-aware when enabled, and records the actor in
  the audit trail.
- Migrations run in the container entrypoint; the image runs as non-root.


## 8. Show the AI support agent

1. Create a ticket, then open `/ui/tickets` and select it.
2. Click **Run AI triage** or call:

```bash
curl -X POST http://127.0.0.1:8000/tickets/42/ai-triage
```

The response contains a deterministic summary, urgency signals, ranked
knowledge-base matches, and stable recommendation IDs. The ticket is unchanged.

3. Review the suggestions. Apply only the ones you approve:

```bash
curl -X POST http://127.0.0.1:8000/tickets/42/ai-recommendations/apply \
  -H 'Content-Type: application/json' \
  -d '{"recommendation_ids":["queue-reassign","priority-upgrade"]}'
```

Only queue and priority can change; status is never modified by AI. The UI
(`/ui/tickets/42?triage=1`) provides checkboxes and an Apply button.

4. Inspect `/execution-logs?actor=local-development` or `/ui/audit` for
`ai.summarize`, `ai.recommend`, and `ai.recommendation_applied` with actor and
correlation ID.

The simulated adapter is credential-free. The optional `LLMAIAdapter` is a
pluggable boundary and is not called by tests; no paid LLM key is required to
run the demo.


## 9. Explain the AI safety boundary

- Ticket text is redacted and truncated before provider calls; embedded
  instructions are treated as untrusted data.
- The model may propose only queue/priority changes, plus informational
  response/workflow suggestions. Status, title, and requester are immutable.
- Applying a suggestion requires an explicit operator recommendation-ID list.
- `tests/test_ai_eval.py` checks the deterministic simulated agent against a
  12-ticket golden set.
- `/metrics` tracks AI triage/apply/provider errors; `/ui/tickets/{id}?triage=1`
  shows the same summary and KB matches an operator would review.
- `python scripts/ai_smoke.py` optionally exercises a configured live
  Gemini/Groq/Ollama provider; it is not part of CI.
