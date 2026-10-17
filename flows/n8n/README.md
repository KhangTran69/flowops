# n8n workflows for FlowOps

Exported n8n workflows that drive FlowOps through its public API. Import them
in n8n with **Workflows > Import from File**.

| File | Trigger | What it does |
|---|---|---|
| `01-email-intake-to-flowops.json` | New email in the support mailbox (Outlook) | Reshapes each message to the intake schema (stable id from the Graph message id, tenant from sender domain), batches it to `POST /webhooks/intake`, and alerts Teams for critical tickets. |
| `02-hr-onboarding-to-flowops.json` | HR system webhook | Validates the new-starter payload (throws on missing fields), creates `POST /onboarding-requests` with an `Idempotency-Key` derived from the HR submission, starts the run, and replies 202 to the HR system. |
| `03-nightly-drift-and-error-alerts.json` | Weekdays 06:00 + n8n Error Trigger | Runs `POST /tenants/drift-check`, pulls failed jobs, posts a plain-English digest to Teams only when something needs attention. Also the `errorWorkflow` for 01 and 02, so no n8n failure is silent. |

## Setup

1. Environment variables on the n8n host: `FLOWOPS_URL` (e.g. `https://flowops.example.com`) and `TEAMS_INCOMING_WEBHOOK_URL`.
2. Credentials (all marked `REPLACE_ME` in the exports, none are included):
   - **FlowOps X-API-Key** (Header Auth, name `X-API-Key`): one of `FLOWOPS_WEBHOOK_API_KEYS`.
   - **FlowOps bearer token** (Header Auth, name `Authorization`, value `Bearer <token>`): an Entra token with the `automation.operator` role.
   - **Support mailbox**: Microsoft Outlook OAuth2 for the shared mailbox.
3. Import 03 first, then set its workflow id as `errorWorkflow` in 01 and 02.

## Why these patterns

- **Idempotency**: flow 01 derives a stable ticket id, and flow 02 sends an `Idempotency-Key`, so re-running an execution updates instead of duplicating.
- **No silent failures**: HTTP nodes retry 3 times, validation throws, and every workflow routes errors to flow 03.
- **Multi-tenant**: the client/tenant key travels with every request (`tenant` / `tenant_key`).
