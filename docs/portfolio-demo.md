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

