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
- Offboarding completions use an admin-only append-only ledger. The ledger
  row is claimed before the simulated side effect, so concurrent callers
  cannot execute a step twice; replays return the stored result.
- Failed webhook deliveries move to a dead-letter state after the configured
  attempt limit. An operator can inspect failed rows and conditionally requeue
  one; delivery remains at-least-once, so receivers must deduplicate.
- Webhook delivery retries transient failures and records the final outcome.
- Onboarding steps use explicit approval and completion state transitions.
- Execution logs make each workflow action visible to the dashboard.

## Runtime topology

```text
Browser / operator
        |
        v
FastAPI app ---- Jinja2 templates + local CSS/JS
        |                    |
        |                    +--> landing, dashboard, /ui/onboarding, /ui/notifications, /ui/workflow-jobs
        |
        +--> JSON API -- idempotency reservations, tickets, onboarding, notifications, workflow jobs
        |
        +--> SQLite / PostgreSQL (Alembic-managed schema)
                     ^
                     |
              durable worker (src/worker.py)
              leased jobs, retries, dead-letter
```

The web UI is read-mostly and uses the same JSON API for actions. Workflow jobs
are claimed with an owner token and lease; the worker commits the item update,
execution log, audit row, and job completion in one fenced transaction. Auth is
configurable: local development is open by default, while protected deployments
require a dev bearer token or Entra JWT and record the actor on execution logs.
`/ready` checks the database; `/metrics` exposes request, job, and outbox
gauges; every response carries a correlation ID.

## AI assistance boundary

The AI layer is intentionally suggest-only. `src/ai_agent.py` produces a
deterministic summary, keyword-ranked KB matches, and stable recommendation
IDs. `POST /tickets/{id}/ai-recommendations/apply` re-runs triage and only
applies the IDs an operator explicitly approves, and only queue/priority
fields. The optional LLM adapter is an OpenAI-compatible client boundary for Gemini,
Groq, OpenRouter, OpenAI, or local Ollama; the default simulated adapter needs
no credentials and is what tests exercise. Every AI
action is written to the execution log with actor and correlation ID.
Before provider calls, ticket text is redacted and bounded, and the prompt labels
ticket content as untrusted data. Provider output is schema-validated and
allowlisted; only queue/priority can become applicable. AI metrics are exposed
at `/metrics`, and the golden evaluation suite runs offline.

## Workflow DAG and ChatOps

Checklist rows persist `depends_on_json` and structured `output_json`.
`src/onboarding.py` owns pure graph rules; `src/workflow.py` applies them to
rows and aggregates prerequisite outputs into downstream action contexts.
Blocked steps require every prerequisite to be `completed`; terminal failures
cascade dependents to `cascade_blocked`. ChatOps approve/reject tokens are
HMAC-SHA256 signed, action-bound, expire after 24 hours, and are recorded once
in `chatops_token_uses` for replay protection.

`onboarding-dag` is the only selectable graph template. Offboarding keeps its
flat template on purpose: that flow completes through the idempotent
`offboarding_completions` ledger, which authorises a step by approval state
rather than by graph edges, so a DAG template there would let a `blocked`
dependent step complete out of order. Teaching the ledger about edges is the
follow-up; `OFFBOARDING_DAG_SHAPE` records the intended graph.

The built-in simulated adapter returns no structured identifiers, so a demo
run stores `{}` outputs. The piping contract is exercised with adapter doubles
in `tests/test_workflow_dag.py` and by the Graph / Power Automate adapters,
which can return real payloads once credentials are configured.

## Secure operator console

The JSON API keeps bearer auth. When `FLOWOPS_UI_AUTH_REQUIRED=true`, the HTML
console uses a signed `itsdangerous` cookie containing subject, roles, and a
per-session CSRF token. `/login` validates a dev bearer/Entra token through the
existing auth path; `/dashboard` and `/ui/*` redirect unauthenticated requests.
Cookie-authenticated mutations require `X-CSRF-Token`; bearer routes remain
CSRF-exempt. Cookies are HttpOnly, SameSite=Lax, and optionally Secure, with a
bounded max-age. This is a prototype token-paste login, not an SSO/user store.

## Deliberate limits

This is a localhost learning prototype. Real Microsoft 365 actions and
AI-assisted recommendations are intentionally outside the current
credential-free scope. Authentication is optional and defaults to local
development; configure a dev bearer token or Entra JWT before exposing the
service. Future sensitive actions must remain behind explicit human approval
gates.
