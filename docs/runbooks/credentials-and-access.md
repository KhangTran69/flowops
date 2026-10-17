# Runbook: credentials, consent and access errors

**Who:** engineer with access to the secret store. Coordinators: escalate with the error text.

FlowOps never stores secrets in its database or code. They live in the
deployment's secret store as environment variables:

| System | Variables | Typical error when wrong |
|---|---|---|
| Microsoft Graph (partner app, all tenants) | `FLOWOPS_GRAPH_CLIENT_ID`, `FLOWOPS_GRAPH_CLIENT_SECRET` | `AADSTS7000215` invalid secret, `AADSTS700016` app not found |
| One tenant with its own app | `FLOWOPS_TENANT_<REF>_GRAPH_CLIENT_ID/_SECRET` (REF = tenant's `credential_ref`) | `CredentialsMissing` |
| Exchange Online PowerShell | `FLOWOPS_EXO_APP_ID`, `FLOWOPS_EXO_CERT_THUMBPRINT`, `FLOWOPS_EXO_ORGANIZATION` | `FLOWOPS_EXO_APP_ID is not set`, certificate errors |
| ConnectWise Manage | `FLOWOPS_CW_SITE`, `FLOWOPS_CW_COMPANY_ID`, `FLOWOPS_CW_PUBLIC_KEY`, `FLOWOPS_CW_PRIVATE_KEY`, `FLOWOPS_CW_CLIENT_ID` | `HTTP 401`, `ConnectWise is not configured` |
| Claude | `ANTHROPIC_API_KEY` (or `FLOWOPS_AI_API_KEY`) | AI triage returns 503 |

## Fixes
- **Expired client secret**: create a new secret on the app registration, update the store, restart the app and worker, then retry failed jobs.
- **`AADSTS65001` / consent**: the customer tenant hasn't consented, or GDAP relationship/roles expired. Re-run admin consent for that tenant, or renew the GDAP relationship in Partner Center.
- **`Authorization_RequestDenied` (403)**: the app is missing a Graph application permission. Needed: `User.ReadWrite.All`, `Directory.Read.All`, `MailboxSettings.ReadWrite`, `Policy.Read.All`. Use least privilege; don't grant `Directory.ReadWrite.All` "just in case".
- **Rotate after any leak**: revoke first, then investigate.
