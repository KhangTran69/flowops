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
- One partner app (GDAP) serves every tenant; a tenant with its own app
  sets `credential_ref` and the secret lives in
  `FLOWOPS_TENANT_<REF>_GRAPH_CLIENT_SECRET`.
- `POST /tenants/drift-check` compares each tenant with its baseline (required
  SKUs and free seats, security defaults, unlicensed enabled users). A
  tenant that fails (revoked consent) is reported as `error`, and the check
  carries on with the other tenants.

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

## SQL reporting (`src/reports.py`, `sql/reporting_queries.sql`)

Migration `0009` creates the views `v_automation_daily`, `v_action_reliability`,
`v_ticket_backlog` and `v_time_saved`. On PostgreSQL it also creates the function
`flowops_tenant_kpis(p_since timestamptz)`. The views are served at
`GET /reports/summary` and `GET /reports/{name}`. Ad-hoc queries for
stakeholders are in `sql/reporting_queries.sql`.
