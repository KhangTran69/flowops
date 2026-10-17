# Runbook: an onboarding or offboarding workflow failed

**Who:** service-desk coordinator. **Time:** about 10 minutes.

## 1. Find the request
1. Open FlowOps > **Onboarding** and click the employee.
2. Note the **tenant** (client) and which step is red (`failed`) or grey (`blocked`).
   Blocked steps are waiting for an earlier step; fix the first red one.

## 2. Read the error on the failed step
Open **Jobs**, find the job for that step and read *Last error*.

| Error contains | Meaning | What you do |
|---|---|---|
| `LicenceExhausted` | The client has no free licences of that type. | Ask the account manager to buy one or free one up. Then press **Retry**. |
| `UsageLocationMissing` | The user has no country set and the client has no default. | Ask an engineer to set the tenant's *usage location* (Tenants page). Retry. |
| `already exists` | The account was already created (maybe by hand). | Nothing. FlowOps treats this as done. |
| `MailboxNotEnabledForRESTAPI` | The mailbox is not ready yet (new licences can take up to an hour). | Wait 30 to 60 minutes, then Retry. |
| `Exchange Online PowerShell` / `FLOWOPS_EXO_` | The shared-mailbox step can't reach Exchange. | Escalate to an engineer ([credentials-and-access.md](credentials-and-access.md)). |
| `HTTP 429` / `TooManyRequests` | Microsoft is throttling. FlowOps already waited and retried. | Wait 15 minutes, then Retry once. |
| `401`, `403`, `consent`, `CredentialsMissing` | FlowOps cannot sign in to that client. | Escalate: [credentials-and-access.md](credentials-and-access.md). |

## 3. Retry safely
Press **Retry** on the job. It is safe to run twice: every step checks
whether the work is already done before doing it.

## 4. Escalate when
- The same step fails twice after your fix, or
- the error is not in the table above, or
- an offboarding (leaver) has been stuck more than 1 hour. Leavers are a security risk.

Escalate on the PSA ticket with: employee, tenant, step name, the *Last error* text, and the job number.
