# FlowOps Command Center

Internal React operator console for the FastAPI backend. All console API calls
use the Express allowlist; the backend remains the authority for authentication,
role enforcement, tenant isolation and audit attribution.

## Local development (simulated integrations)

Use Node.js 22 and a running local FlowOps backend. The .env file is loaded by
Node, and Vite forwards /api to the loopback proxy.

```bash
cd command-center
npm ci
cp .env.example .env
# Set FLOWOPS_API_URL to your local backend.
# Explicit local demo only: FLOWOPS_COMMAND_CENTER_DEV=true
# Set FLOWOPS_API_TOKEN to the configured development token (or a dummy
# value if using the intentionally unprotected local backend).
npm run dev
```

Open the Vite address printed by npm. Do not expose the demo proxy or Vite
development server to untrusted networks. The demo proxy always binds to
127.0.0.1 and never accepts identity or role headers. DEV_USER_ROLE is an exact
local display/allowlist role, not an identity provider. Protected backend
credentials still determine actual backend access.

## Protected production configuration (release gate)

Production mode disables the demo credential and will fail closed without
a verified per-user identity. Set FLOWOPS_API_URL to the authenticated
backend, enable FLOWOPS_AUTH_REQUIRED and the existing Entra configuration,
and configure secure signed-session cookies if browser sessions are used.
Use HTTPS and a private trusted upstream connection.

The trusted gateway must supply each caller's own Authorization bearer
credential, or route existing backend login/session traffic on the same
origin so the signed flowops_ui_session cookie is available to the console.
The console itself does not implement an Entra interactive login flow.
A gateway must strip any client-supplied identity headers; the console ignores
X-User-Role, X-User-Email and X-Actor-Email and obtains the actor from
GET /command-center/session. Do not inject a shared service/admin bearer
token. The API checks the same caller again on every forwarded operation.
No production workforce browser release is authorized by this change.

```bash
npm ci
npm run build
node --test server/*.test.mjs
NODE_ENV=production npm start
```

Default proxy bind: 127.0.0.1:8787. HOST can be explicitly set in protected mode.
PORT overrides the port. SERVE_STATIC=true serves the build in non-production.
FLOWOPS_API_TOKEN is used only in explicitly enabled local demo mode.
Do not store credentials in committed files.

### Browser request boundary

POST /api/flowops requires application/json and X-FlowOps-Request: 1.
The proxy does not enable cross-origin requests. Session CSRF tokens are
obtained from the verified backend session, not caller-supplied headers.
Only the signed FlowOps cookie (or the caller's bearer) is forwarded.
The proxy rejects path traversal, refuses redirects, and applies an explicit
method/path allowlist. Network and identity verification failures are denied.

## API contracts

- Upstream POST /tickets keeps its existing positive ID and requester ID
  payload. POST /tickets/manual accepts a known numeric requester ID, title,
  description and optional contact email, and requires Idempotency-Key.
  Negative numeric ticket IDs are server-assigned and reserved for manual
  tickets; the contact email is descriptive, not an identity mapping.
- GET /tickets supports search, status, priority, queue, category, limit,
  offset and existing tenant enforcement. The console pages through matches.
- GET /tickets/{id} includes stored timestamps, resolution note, manual
  metadata, and up to 50 authorized ticket history events.
- GET /workflow-runs/{id} returns logical checklist items and separate
  execution attempts. Progress counts successful checklist completions.
- GET /workflow-definitions/{key}/diff?from=&to= compares immutable versions;
  definition operations remain global-admin-only.
- POST /workflow-runs/{id}/steps/{item_id}/approve or /reject requires an
  administrator, a pending approval, an active run, and a reason.
- GET /command-center/summary provides tenant-scoped totals independent of
  list pagination. Recorded closures use the stored closed_at value and UTC
  day boundaries, rather than guessing from status.
- Typed frontend contracts live in src/lib/contracts.ts. The FastAPI
  OpenAPI schema is the authoritative contract.

## Validation

CI runs the Python test matrix, SQLite migration check, PostgreSQL integration
and reliability tests, npm ci, the TypeScript/Vite build, proxy syntax check,
and Node proxy security regressions. Browser keyboard/focus tests and the
actual production identity gateway must be validated before a release.
The backend's HTTP latency histogram is process-local; a multi-replica
deployment needs external scraping/aggregation.

## Operator-console parity (plan of record)

The React console is the operator console; the Jinja `/ui/*` pages are frozen
(no new features) and will be retired after parity lands. `/dashboard` and
the landing page stay server-rendered — they are monitoring surfaces, not
operator surfaces.

Coverage today (React pages: Approvals, Audit, Dashboard, Definitions,
DefinitionDetail, Deliveries, Requests, RequestDetail, Runs, RunDetail,
Tickets, TicketDetail). Still Jinja-only, in port order by operator value:

- [ ] **Workflow jobs** (`/ui/workflow-jobs`, job detail) — queue health,
  retry/cancel controls, attempt history
- [ ] **Notifications / outbox** (`/ui/notifications`) — pending + dead-letter
  tables, requeue/retry actions
- [ ] **Analytics** (`/ui/analytics`) — summary cards, step bottlenecks,
  tenant/window filters
- [ ] **Triggers** (`/ui/triggers`, trigger detail) — source config, delivery
  history, test-fire
- [ ] **Knowledge base** (`/ui/knowledge-base`) — read-only search
- [ ] **Workforce module** (`/workforce/ui`) — gated behind the browser
  release gate until production identity ships

Every port must, in the same PR:

1. add the typed contract to `src/lib/contracts.ts` and runtime response
   validation (incompatible payloads are rejected, not rendered);
2. add a browser-smoke journey step covering the page's primary action;
3. keep the backend contract unchanged — the Jinja pages and the React
   console consume the same API, so this is a client swap, not a redesign.

Retirement rule: once the checklist is empty, hide `/ui/*` behind a feature
flag for one release, then delete the Jinja templates and their routes.
Apply the additive Alembic manual-ticket migration in a non-production
representative database first. No production deployment is part of this branch.
