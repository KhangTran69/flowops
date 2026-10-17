# FlowOps Command Center

Internal operator console for the FlowOps FastAPI backend. The browser never
receives the FlowOps API token: all calls go through a small Express proxy.

## Run

```bash
cd command-center
npm install
cp .env.example .env    # edit values
npm run dev             # proxy :8787 + Vite :5173
```

Production:

```bash
npm run build
NODE_ENV=production npm start   # serves dist + /api on :8787
```

## Environment

| Variable | Required | Description |
|---|---|---|
| `FLOWOPS_API_URL` | yes | Base URL of the FlowOps FastAPI service, e.g. `https://flowops.example.com` |
| `FLOWOPS_API_TOKEN` | yes | Bearer token used by the server-side proxy |
| `PORT` | no | Proxy port (default `8787`) |
| `DEV_USER_EMAIL` | no | Fallback identity when no auth proxy header is present |
| `DEV_USER_ROLE` | no | Fallback role: `admin`, `operator`, or `viewer` |
| `SERVE_STATIC` | no | Set `true` to serve `dist` from the proxy in non-production mode |

### User identity and roles

The proxy reads the signed-in user from:

- `X-User-Email` or `X-Auth-Request-Email`
- `X-User-Role` or `X-Auth-Request-Role`

These headers are expected from an auth proxy or SSO gateway in front of the
Command Center. If absent, `DEV_USER_EMAIL` / `DEV_USER_ROLE` are used.
Role matching is substring-based: `admin`, `operator`, `viewer`; unknown roles
fall back to `operator`.

`GET /api/session` returns `{ role, email, configured }`.

## API token permissions

The token should be allowed to call:

**Read**
- `/health`, `/ready`
- `/tickets`, `/tickets/{id}`
- `/onboarding-requests`, `/onboarding-requests/{id}`
- `/onboarding-requests/{id}/run`
- `/workflow-runs`, `/workflow-runs/{id}`
- `/workflow-definitions`, `/workflow-definitions/{key}`
- `/workflow-definitions/{key}/diff`
- `/execution-logs`, `/audit`

**Operator writes**
- `POST /tickets`
- `POST /tickets/{id}/ai-triage`
- `POST /tickets/{id}/ai-recommendations/apply`
- `POST /onboarding-requests`
- `POST /onboarding-requests/{id}/run`
- `POST /workflow-runs/{id}/pause|resume|cancel`

**Admin writes**
- `POST /workflow-runs/{id}/steps/{stepKey}/approve|reject`
- `POST /workflow-definitions`
- `POST /workflow-definitions/{key}/publish`
- `PUT /workflow-definitions/{key}`

The proxy enforces method/path allowlists and role checks, but the FlowOps API
remains authoritative. Its 401/403 responses are surfaced to the user.

## Assumed API endpoints

The Command Center assumes these endpoints exist:

- `GET /workflow-definitions/{key}/diff?from=&to=`
- `POST /workflow-runs/{id}/steps/{stepKey}/approve`
- `POST /workflow-runs/{id}/steps/{stepKey}/reject`
- `GET /workflow-runs/{id}` returns steps and events
- `POST /workflow-runs/{id}/pause|resume|cancel`

If an endpoint differs, adjust the path allowlist in `server/index.mjs` and the
corresponding page.

## Security

- `FLOWOPS_API_TOKEN` is read only on the server.
- Requests include `Authorization: Bearer <token>`, `Accept: application/json`,
  and `X-Actor-Email`.
- The proxy logs only method, path and upstream status.
- Allowed paths are regex-allowlisted; arbitrary methods/paths are rejected.
- All rendered text is plain text; no raw HTML injection.
- 20-second upstream timeout; network failures become a friendly 503.

## Structure

```text
server/index.mjs        secure proxy and static server
src/pages/              dashboard, tickets, requests, runs, approvals, definitions, audit
src/components/         layout, tables, status badges, confirmations, data states
src/lib/api.ts          proxy client and defensive field helpers
src/lib/session.tsx     role/email context
src/hooks/usePoll.ts    visibility-aware polling with last-good data
```
