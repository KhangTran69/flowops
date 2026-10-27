# Employee onboarding and IT asset management

FlowOps remains a Python 3.13 / FastAPI modular monolith with SQLAlchemy 2, PostgreSQL in Compose, SQLite for local development and Jinja2 with local CSS/JavaScript. The existing ticket, checklist, workflow job, audit logging, signed browser session, and notification-outbox code remains in place. The workforce domain is separate from the older onboarding/offboarding checklist API because that API models IT action steps rather than employee and manager approval records.

## Local setup

For a quick SQLite run:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt
python -m alembic upgrade head
python -m src.workforce_demo
uvicorn src.api:app --reload
```

Open `http://127.0.0.1:8000/workforce/ui`. Swagger is at `/docs`. PostgreSQL setup remains the existing `docker compose up -d --build`; the app migrates on startup and starts `workforce-outbox` with the existing services. Set the integration variables from `.env.example` only when configuring n8n. Local simulated provisioning needs no credentials. `python -m src.workforce_worker` can run the worker by itself for SQLite; Compose runs it continuously.

The seed is safe to run repeatedly and creates synthetic `.test` email records: employee `DEMO-1001`, a draft onboarding request, and available asset `DEMO-LAPTOP-001`. The seeded manager subject is `manager-demo`.

### Role accounts

API role checks accept `HR`, `MANAGER`, and `IT_ADMIN` JWT roles or signed UI sessions. Tenant-scoped identities must carry the `flowops_tenant_key` (or `tenant_key`) claim in Entra, or the `tenant_key` field in a development account. The key must name an active FlowOps tenant. Managers can only access records in that tenant and requests for employees whose `manager_subject` equals their authenticated subject. `automation.admin` and local development actors retain explicit global operator access.

To run role-specific UI sessions, enable both auth flags in a development environment and configure `FLOWOPS_SESSION_SECRET`, `FLOWOPS_DEV_BEARER_TOKEN`, and `FLOWOPS_DEMO_USERS`. Generate password hashes with:

```bash
python -m src.demo_users hash
```

The command prompts for a username, role, tenant key, and password, then prints a PBKDF2 hash snippet. Create the `demo` tenant with `python -m src.workforce_demo`, then combine snippets in `FLOWOPS_DEMO_USERS` for HR, manager, and IT_ADMIN. Demo accounts work only when `FLOWOPS_ENVIRONMENT=development`; never use them in production. Production should use Entra bearer authentication with the matching application roles, tenant claim, and secure session settings.

## Workflow and states

```mermaid
flowchart LR
  HR[HR creates employee + draft] --> SUBMIT[Submit for approval]
  SUBMIT --> MANAGER{Assigned manager}
  MANAGER -->|Reject with reason| REJECTED[REJECTED]
  MANAGER -->|Approve once| TASKS[Five required IT tasks + outbox event]
  TASKS --> IT[IT assigns equipment and completes tasks]
  TASKS --> N8N[n8n or local mock provisioning]
  N8N --> CALLBACK[Authenticated, idempotent callback]
  IT --> READY{All required tasks DONE?}
  CALLBACK --> READY
  READY -->|Yes| COMPLETE[COMPLETED]
  READY -->|No| TASKS
  HR -->|Cancel| CANCELLED[CANCELLED; queued events suppressed]
```

States are `DRAFT`, `PENDING_APPROVAL`, `IN_PROGRESS`, `COMPLETED`, `REJECTED`, and `CANCELLED`. `APPROVED` is transient in the approval transaction; after tasks are created, the request enters `IN_PROGRESS`. Rejected and completed requests are terminal. HR can copy a rejected request into a new draft. Cancellation suppresses queued events and callbacks reject cancelled requests. Cancellation cannot recall n8n work already in progress, but a late callback cannot change FlowOps state.

Approval and task creation occur in one transaction. A unique `(request_id, task_key)` constraint prevents duplicate task sets; one decision per request is enforced by a unique constraint. A partial unique index permits at most one active request per employee, including under concurrent submissions. The required tasks are Prepare laptop, Assign equipment, Simulate account provisioning, Confirm software setup, and Complete IT orientation checklist. Only required tasks gate completion. Terminating an employee cancels their active onboarding requests and queued automation.

Asset claims use a conditional, tenant-scoped database update from `AVAILABLE` with no current employee to `ASSIGNED`. One competing claim can succeed. Returning closes the assignment-history row and makes the asset available. Assigned assets must be returned before entering maintenance or retired status.

### Tenant rollout and existing data

Migration `0012_workforce_tenant_fencing` adds nullable tenant ownership columns without changing existing records. Historical workforce rows remain unassigned (`tenant_key=NULL`) and are visible only to global `automation.admin` or local development access until their owner is known. Map those records to the correct tenant before enabling tenant-scoped HR or IT accounts; do not bulk-assign them to an assumed default tenant. Tenantless legacy outbox events are blocked from n8n delivery and marked failed until their records are mapped. New records created by tenant-scoped users inherit the authenticated tenant. Child records retain tenant ownership so queries, audit views, dashboards, reminders, and worker callbacks can verify the same scope.

## API

All routes live under `/workforce` and appear in FastAPI's OpenAPI document at `/docs`.

| Action | Route | Role / credential |
|---|---|---|
| Employee list/create/edit/status | `GET/POST /employees`, `PATCH /employees/{id}`, `PATCH /employees/{id}/status` | HR; IT_ADMIN can list |
| Create/edit/submit/cancel/copy draft | `/onboarding-requests` and action routes | HR |
| List/detail/approve/reject | `/onboarding-requests`, `/{id}` | Assigned manager can read/decide only their requests; HR/IT_ADMIN can read all |
| Filter/update tasks | `GET /tasks?status=&assignee=&overdue=&offset=&limit=`, `PATCH /tasks/{id}` | HR can list; IT_ADMIN can update |
| Inventory/status/assignment/return/history | `/assets` and `/assets/{id}/...` | IT_ADMIN |
| Dashboard and audit | `/dashboard`, `/audit` | Role scoped; manager data is filtered |
| Automation event history/manual retry | `/automation/events`, `/automation/events/{eventId}/retry` | IT_ADMIN |
| List active tenants / get overdue tasks for one tenant | `GET /integration/tenants`, `GET /integration/overdue-tasks?tenant_key=...` | `X-FlowOps-Automation-Key` |
| Record reminder or error outcome | `POST /integration/reminders`, `/integration/errors` | `X-FlowOps-Automation-Key` |
| Provisioning callback/mock | `POST /automation/callbacks/provisioning`; development-only `/automation/events/{eventId}/simulate` | Dedicated callback key / IT_ADMIN |

Pagination is bounded (lists at most 100; audit at most 200). Duplicate employee identifiers/emails and conflicting asset claims return 409. Rejection requires a nonblank reason. Backend role checks are the access boundary; UI controls only improve usability.

## Automation and event delivery

Approval writes a stable event row in the same transaction as the decision and required tasks. The event envelope carries the tenant key:

```json
{
  "eventId": "stable-uuid",
  "eventType": "workforce.onboarding.approved",
  "tenantKey": "demo",
  "schemaVersion": "1.0",
  "occurredAt": "2026-10-22T00:00:00+00:00",
  "correlationId": "request-correlation-id",
  "data": {"requestId": 1, "employeeId": 1, "employeeNumber": "DEMO-1001", "workEmail": "avery.santos@example.test", "fullName": "Avery Santos", "startDate": "2026-11-21"}
}
```

The outbox worker leases due events with a unique fencing token, signs the exact JSON bytes with HMAC-SHA256 (`X-FlowOps-Signature: sha256=...`), and sends the configured production Webhook URL with the automation API key. A worker whose lease expired cannot overwrite a later claimant's result. It retries temporary failures up to five attempts with capped exponential delay. Exhausted events remain visible and can be retried. Delivery state means n8n accepted the webhook; `workflow_status` stays `WAITING` until the authenticated, tenant-matched callback records the outcome. Stable event IDs make duplicate callback processing safe. If an operator retries an event after the 10-minute callback timeout, n8n may execute it again; provisioning workflows should use `eventId` as their idempotency key before invoking non-idempotent external actions.

`FLOWOPS_AUTOMATION_ADAPTER=simulated` completes the account-provisioning task with an explicit simulated note. It creates no external account. n8n mode requires `FLOWOPS_N8N_WEBHOOK_URL`, `FLOWOPS_N8N_WEBHOOK_SECRET`, `FLOWOPS_N8N_API_KEY`, and `FLOWOPS_AUTOMATION_CALLBACK_SECRET`; use unique random values stored in environment or a secret manager.

The importable workflows in `n8n/workflows/` cover approved onboarding provisioning, daily overdue-task reminders, and n8n execution errors. They are inactive exports with URL and credential placeholders. Configure Header Auth credentials (API key for FlowOps integration endpoints and event ingress; separate callback key for provisioning), set the FlowOps base URL, and set an approved operations-notification webhook URL in the error workflow before activation. The daily reminder flow lists active tenants, then requests overdue work separately for each tenant; outcomes carry the tenant key and use a stable tenant/task/UTC-day event ID. The outcome is `SIMULATED` and does not message employees by default. Authoritative business changes remain behind FlowOps APIs.

The exports target n8n **2.44.5 stable**, checked against the official release list on 2026-10-22. They use built-in Webhook, Schedule Trigger, Code, HTTP Request, and Error Trigger nodes. Refer to [n8n Webhook documentation](https://docs.n8n.io/integrations/builtin/core-nodes/n8n-nodes-base.webhook/) and [official releases](https://github.com/n8n-io/n8n/releases) when importing into a later version. No live n8n cloud instance was available, so live execution is unverified.

In n8n choose **Workflows → Import from File** for each JSON export. Configure credentials and placeholder URLs, test using the Webhook test URL, then publish the workflow and configure FlowOps with the production URL. Avoid using the n8n editor URL as the Webhook URL.

## Five-minute portfolio demonstration

1. Run the app, worker, and `python -m src.workforce_demo`; open `/workforce/ui`.
2. Create a synthetic employee and start onboarding, or use `DEMO-1001`; submit its draft.
3. Sign in as its manager (`manager-demo` for the seed), approve, and open the request detail. Confirm exactly five tasks and an outbox event.
4. As IT_ADMIN, assign `DEMO-LAPTOP-001`, complete the hardware, equipment, software, and orientation tasks, then run mock provisioning.
5. Confirm the request becomes COMPLETED only after all five required tasks finish. Show the asset history and audit page.
6. For reliability, configure a test n8n endpoint to fail once, inspect the retry count, restore it, and confirm one stable event ID. Automated tests cover this offline; that is not a live n8n execution.

## Limitations and next steps

- Seed data, account provisioning, and reminders are synthetic/mock. n8n cloud was not available for live verification.
- Production role issuance must be configured in Entra for HR, MANAGER, and IT_ADMIN. Demo users only operate in development.
- Existing workforce records remain unassigned until manually mapped to a tenant; scoped users cannot see these legacy rows before mapping.
- Bulk HR imports, asset depreciation, and real identity provisioning are possible follow-up work.
