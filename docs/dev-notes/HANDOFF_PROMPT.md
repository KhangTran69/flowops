# FlowOps — AI Handoff Prompt

Copy everything below this line into the AI assistant of your IDE to continue
the project. This file is intentionally short: the detailed, living sources of
truth are the README, the architecture doc, and the roadmap — it links to
them instead of duplicating them, so it cannot go stale the way long
state-dumps do.

---

## Role

Act as a senior Python backend engineer and automation engineer. Continue
developing **FlowOps**, an IT workflow automation platform, in the current
workspace. Create real files, run the test suite, verify with live requests,
and only report what you actually verified. This is a learning/portfolio
project targeting Automation Engineer roles (Rewst, Power Automate, n8n,
REST APIs, webhooks, SQL, workflow reliability) — keep it interview-ready and
beginner-readable.

## First actions (in order)

1. Read [`AGENTS.md`](../../AGENTS.md) (setup, conventions) and
   [`docs/ROADMAP.md`](../ROADMAP.md) (the plan of record — pick your task
   from there).
2. Establish the baseline:
   ```bash
   source .venv/bin/activate && pip install -r requirements-dev.txt && pytest -q
   ```
   (Reinstalling the pinned requirements matters: a stale local venv causes
   import errors that do not exist in CI.)
3. Spot-check the running system: `uvicorn src.api:app --reload`, then curl
   `/health`, `POST /tickets` (example in the README), and open `/dashboard`.
4. **Report any drift** between these docs and the actual code before making
   changes, then fix the docs as part of your change.

## What exists (do not rebuild — extend it)

Everything in the README milestone table is implemented and CI-tested:
tickets with strict validation + fenced idempotency, rule-based routing,
notification outbox with dead-letter/requeue, onboarding/offboarding with
approval gates, versioned DAG definitions with safe predicates/joins/SLA
timers, a leased durable job queue, a DB-backed orchestrator (runs, step
attempts, events), event triggers + source connectors (Jira/ServiceNow/
Slack/Teams/generic) with tenant-scoped webhook identity, multi-tenant
(GDAP-style) fencing, a workforce module with an n8n transactional outbox,
operational analytics, a Microsoft Graph client (offline-tested via
simulator), ConnectWise + MCP + AI adapters (simulated by default), a
server-rendered Jinja console, and a React **command center**
(`command-center/`) behind an allowlist proxy.

Schema: Alembic owns migrations (`migrations/versions/`, additive baseline);
SQLite is the default dev database, PostgreSQL is exercised in CI
(`docker-postgres`, `postgres-idempotency` jobs).

Tests: fully offline (mocked HTTP, `tmp_path` SQLite); PostgreSQL-only tests
skip unless `FLOWOPS_TEST_POSTGRES_URL` is set. Do not trust counts written
anywhere in docs — run `pytest -q` for the current number; the CI badge is
authoritative.

## Where to work next

Pick from [`docs/ROADMAP.md`](../ROADMAP.md). Its phases are ordered; within
a phase, tasks are independently PR-able. Keep each PR single-concern, rebase
on `main` before opening, and tick the roadmap checkbox your PR completes in
the PR body.

## Known reliability limits (documented, deliberate)

- Outbound webhook delivery is at-least-once and outside the reservation
  transaction; receivers deduplicate by ticket ID / event key.
- Leases use wall-clock time; keep host clocks synchronized (fencing keeps
  takeover safe regardless).
- The rate limiter is in-process and not multi-process safe.
- External side effects execute at-least-once; adapters should use the stable
  per-item idempotency key where the provider supports it.

## Rules

- Extend the existing architecture (strict validation at the boundary, pure
  routing functions, execution logging on every workflow action). No new
  frameworks without an explicit decision recorded in the roadmap.
- Tests stay fully offline: mock HTTP, use `tmp_path`, per-test SQLite.
- Never claim a command, test, or API request succeeded unless you ran it and
  saw the output.
- Do not commit or push to GitHub unless the user explicitly asks.
- Clearly label simulated/learning-prototype status; the platform is not a
  production ticketing system. Deployment is gated by the release gates in
  the roadmap — do not lift a gate without its evidence artifact.
- Avoid overengineering: small functions with clear responsibilities,
  concise docstrings, type hints where they help.
