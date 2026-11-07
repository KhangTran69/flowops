# MSP integrations

How FlowOps connects to the systems an MSP automation team uses. Each
integration runs offline in tests (mocked HTTP) and needs real credentials
only in deployment. No secret is stored in the database or the repo.

## Microsoft Graph (`src/graph.py`, `src/actions.py`)

- **Auth**: OAuth2 client credentials against each customer's Entra authority
  (`https://login.microsoftonline.com/{tenant}/oauth2/v2.0/token`, scope
  `https://graph.microsoft.com/.default`). Tokens are cached per tenant and
  refreshed 5 minutes before expiry.
- **Queries**: `$filter` built with `odata_eq()`, which escapes quotes
  (`o'brien` becomes `'o''brien'`); `$select` keeps payloads small;
  `@odata.nextLink` is followed until the collection ends.
- **Throttling**: 429/503/504 retried with `Retry-After`, then exponential
  backoff; bounded attempts. Errors raise `GraphError` with Graph's `code`
  and `request-id` so a failure can be traced in Entra sign-in logs.
- **Retry policy**: 4xx errors (`Authorization_RequestDenied`,
  `LicenceExhausted`, `UsageLocationMissing`) set `retryable=False`, so
  the worker dead-letters them on the first attempt instead of retrying 5 times.
- **Idempotent actions**: create user (find by UPN first), assign licence
  (skip if assigned, check free units first), mailbox settings, disable and
  revoke sessions, remove licences.
- **Application permissions**: `User.ReadWrite.All`, `Directory.Read.All`,
  `MailboxSettings.ReadWrite`, `Policy.Read.All` (drift check).

Set `FLOWOPS_ACTION_ADAPTER=graph_simulated` to run the same adapter against
the in-memory Graph in `src/graph_simulator.py` (paging, OData errors and
licence limits included).

## When to drop back to Exchange Online PowerShell

Graph cannot convert a mailbox to shared, hide it from the GAL or grant
FullAccess. `archive_email_mailbox` therefore goes to
`scripts/powershell/Invoke-FlowOpsAction.ps1`:

```bash
echo '{"action":"archive_email_mailbox","context":{"employee_email":"jane@contoso.com","manager_email":"boss@contoso.com"}}' \
  | pwsh -NoProfile -File scripts/powershell/Invoke-FlowOpsAction.ps1 -DryRun
```

The request arrives on stdin (never interpolated into a command line). The
script connects with certificate-based app-only auth, writes one JSON line,
and uses exit code 2 for bad input/config (not retried) and 1 for runtime
failures (retried). It also parses Exchange size strings such as
`1.2 GB (1,288,490,188 bytes)` to bytes.

## Multi-tenant / GDAP (`src/tenants.py`, `/tenants`)

- `PUT /tenants/{key}` registers a client: Entra tenant id, default domain,
  licence SKU, usage location, ConnectWise company id, a `credential_ref`
  and a baseline. Tickets (`tenant`) and onboarding requests (`tenant_key`)
  carry the tenant through the worker into every Graph call, log line and
  error message (`[acme] ...`).
- A tenant-scoped administrator may update its operational settings, but only
  an unscoped global administrator may set or change `entra_tenant_id` and
  `credential_ref`; those fields select the Graph authority and credential.
- One partner app (GDAP) serves every tenant; a tenant with its own app
  sets `credential_ref` and the secret lives in
  `FLOWOPS_TENANT_<REF>_GRAPH_CLIENT_SECRET`.
- `POST /tenants/drift-check` compares each tenant with its baseline (required
  SKUs and free seats, security defaults, unlicensed enabled users). A
  tenant that fails (revoked consent) is reported as `error`, and the check
  carries on with the other tenants.

## Jira, ServiceNow, and webhook connectors (`src/connectors.py`, `/connectors`)

Connector credentials remain in the process environment. A tenant-scoped
connector must use its fixed references, with hyphens in the tenant key
replaced by underscores and the key uppercased:

- Jira: `FLOWOPS_TENANT_<KEY>_CONNECTOR_JIRA_API_TOKEN`
- ServiceNow: `FLOWOPS_TENANT_<KEY>_CONNECTOR_SERVICENOW_USERNAME` and
  `FLOWOPS_TENANT_<KEY>_CONNECTOR_SERVICENOW_PASSWORD`
- Slack: `FLOWOPS_TENANT_<KEY>_CONNECTOR_SLACK_SIGNING_SECRET`

For tenant event ingress, set
`FLOWOPS_TENANT_<CREDENTIAL_REF>_WEBHOOK_API_KEY` and send it with
`X-Tenant-Key: <tenant-key>` to `/events`. Tenant identity comes from the
server's active tenant record and its `credential_ref`; the global
`FLOWOPS_WEBHOOK_API_KEY` is limited to unscoped events. The same identity
must match a connector's tenant at
`/connectors/{connector_id}/webhook`.

Trigger `notify` rules require
`FLOWOPS_TENANT_<CREDENTIAL_REF>_NOTIFY_ALLOWED_HOSTS`, a comma-separated
list of exact hostnames (for example `hooks.example.com`). Missing or empty
policies block delivery. Notify URLs must use HTTPS on port 443, have no user
credentials, query, or fragment, resolve to public addresses, and redirects
are rejected at delivery. Configure only webhook hosts approved for that
tenant.

Unbound, global connectors use `FLOWOPS_CONNECTOR_...` references. Connector
config must name the exact reference for its type and tenant; it cannot select
other application secrets.

For Docker Compose, copy `.connector-secrets.env.example` to the ignored
`.connector-secrets.env` and put the configured connector values there. Both
Compose files pass this optional file only to `app` and `scheduler`. Put
tenant webhook keys and notify host policies in the separate ignored
`.tenant-webhook-secrets.env` (copy
`.tenant-webhook-secrets.env.example`); Compose passes it only to `app` and
`outbox`, which need the values for authentication and delivery validation.
Keep the connector host allowlist in the normal Compose environment as
`FLOWOPS_CONNECTOR_ALLOWED_HOSTS_JSON`.
For non-Compose runs, export the same exact variable names to the process
environment.

Jira and ServiceNow polling requires an HTTPS endpoint on the exact host
allowlist in `FLOWOPS_CONNECTOR_ALLOWED_HOSTS_JSON`. This is a JSON object
mapping tenant keys to host arrays; the `"*"` key is for unbound connectors.
For example, `{"acme":["acme.atlassian.net","acme.service-now.com"]}`.
Hosts are exact (no wildcard), use port 443, and are controlled by the
deployment environment. No list is configured by default, so a polling
connector is rejected until its tenant's trusted hosts are configured.
Redirects are disabled so credentials cannot be forwarded to another host.
Existing connector rows with custom `*_env` references must be updated to the
fixed names above and their tenant host allowlist must be configured before
polling resumes. Event responses return an opaque versioned fingerprint;
clients should treat it as an identifier rather than parse its contents.
Historical events did not record connector IDs, so a still-returning event
from a configured connector may be ingested once after the migration; review
active trigger side effects during rollout.

## ConnectWise Manage (`src/connectwise.py`)

- Basic auth `company+publicKey:privateKey` plus the `clientId` header.
- `conditions` builder: `condition("board/name", "=", "Service Desk")`, dates
  as `[2026-01-02T03:04:05Z]`, `in (1,2)`, grouped `and`/`or`.
- JSON-Patch updates (`replace("status", {"name": "Closed (resolved)"})`),
  notes, and time entries (`chargeToType: ServiceTicket`).
- `POST /tickets/{id}/psa-sync` creates the PSA ticket once (stored in
  `external_links`) and patches it on later syncs, so it never creates a duplicate.

## Claude (`src/ai_agent.py`)

`FLOWOPS_AI_ADAPTER=claude` with `ANTHROPIC_API_KEY` uses the Anthropic
Messages API (`x-api-key`, `anthropic-version: 2023-06-01`, `temperature 0`).
The existing safeguards still apply: ticket text is redacted, marked as
untrusted data, and the model's output is checked against an allowlist.
Suggestions are only applied when an operator approves them. 429/529 errors return 503.

## MCP server (`src/mcp_server.py`)

Lets Claude Desktop, Claude Code or any MCP client work with FlowOps through the REST API (same auth,
roles, idempotency and audit as people):

| Tool | Type |
|---|---|
| `list_tickets`, `get_ticket`, `triage_ticket` | read (triage only suggests) |
| `list_pending_approvals`, `list_failed_jobs` | read |
| `automation_kpis`, `tenant_drift_report` | read |
| `request_onboarding`, `retry_job` | write, only with `FLOWOPS_MCP_ALLOW_WRITES=true` |

No tool can approve a step. A person still approves every step.

Claude Desktop (`claude_desktop_config.json`):

```json
{
  "mcpServers": {
    "flowops": {
      "command": "python",
      "args": ["-m", "src.mcp_server"],
      "cwd": "/path/to/flowops",
      "env": {
        "FLOWOPS_API_URL": "http://127.0.0.1:8000",
        "FLOWOPS_MCP_API_TOKEN": "<operator token>",
        "FLOWOPS_MCP_ALLOW_WRITES": "false"
      }
    }
  }
}
```

## n8n (`flows/n8n/`)

Three exported workflows: email-to-ticket intake, HR new starter to
onboarding, and a nightly drift and dead-letter digest that is also the error
workflow for the other two. See [flows/n8n/README.md](../flows/n8n/README.md).

## RPA webhook adapter (`src/rpa.py`)

Set `FLOWOPS_ACTION_ADAPTER=rpa` to send approved checklist actions to an
external RPA bot endpoint. `FLOWOPS_RPA_BOT` selects the bot/work queue and
`FLOWOPS_RPA_SECRET` is sent as `X-RPA-Secret`. The stable workflow
`idempotency_key` is included in the payload so a bot can deduplicate retries.
The adapter returns the same typed `ActionResult` as Graph, Power Automate,
PowerShell, and simulated adapters, so retry, audit, and dead-letter behavior
does not change when the execution platform changes.

All HTTP-backed action adapters classify failures at the boundary: authentication
and other 4xx responses are permanent, 429 responses remain retryable, and 5xx
or transport failures are retryable. This prevents invalid credentials or
missing resources from consuming the entire retry budget while preserving safe
recovery for throttling and transient outages.

## SQL reporting (`src/reports.py`, `sql/reporting_queries.sql`)

Migration `0009` creates the views `v_automation_daily`, `v_action_reliability`,
`v_ticket_backlog` and `v_time_saved`. On PostgreSQL it also creates the function
`flowops_tenant_kpis(p_since timestamptz)`. The views are served at
`GET /reports/summary` and `GET /reports/{name}`. Ad-hoc queries for
stakeholders are in `sql/reporting_queries.sql`.
