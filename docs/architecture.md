# FlowOps architecture

FlowOps is intentionally small: the API boundary validates input, pure
routing functions make the workflow decision, SQLAlchemy persists the result,
and execution logs provide the monitoring data.

```text
Client / n8n webhook
        |
        v
FastAPI boundary
  strict Pydantic validation
        |
        v
Ticket workflow
  normalize -> classify -> prioritize -> queue
        |                  |
        v                  v
  SQLAlchemy store     webhook notifier
  tickets + logs       retry/backoff
        |
        v
Monitoring dashboard
  success/failure, duration, routing, staff-time estimate
```

## Persistence boundary

SQLite is the default for local development. Setting
`FLOWOPS_DATABASE_URL=postgresql+psycopg://...` switches the same SQLAlchemy
models to PostgreSQL. The Docker Compose stack exercises that path in both
local verification and GitHub Actions.

## Reliability boundaries

- Strict validation rejects malformed or ambiguous source data at the API edge.
- Idempotency keys reserve ticket and onboarding operations with a fencing
  token. A stale owner that loses its lease to a takeover cannot finalize or
  delete the new reservation, and its resource/log writes roll back with a
  retryable 409. Keys are scoped to their resource type.
- Failed webhook deliveries move to a dead-letter state after the configured
  attempt limit. An operator can inspect failed rows and conditionally requeue
  one; delivery remains at-least-once, so receivers must deduplicate.
- Webhook delivery retries transient failures and records the final outcome.
- Onboarding steps use explicit approval and completion state transitions.
- Execution logs make each workflow action visible to the dashboard.

## Deliberate limits

This is a localhost learning prototype. Authentication, real Microsoft 365
actions, and AI-assisted recommendations are intentionally outside the current
credential-free scope. Future sensitive actions must remain behind explicit
human approval gates.
