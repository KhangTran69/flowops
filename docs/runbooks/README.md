# FlowOps runbooks

Plain-English guides for service-desk coordinators and on-call engineers.
You do not need to read code to follow them.

| When this happens | Use |
|---|---|
| A new starter or leaver workflow is stuck or failed | [onboarding-failure.md](onboarding-failure.md) |
| A job shows **failed** / dead-letter on the Jobs page | [failed-jobs.md](failed-jobs.md) |
| Teams/n8n alerts stopped arriving, or a webhook says "failed" | [webhook-failure.md](webhook-failure.md) |
| The morning digest reports tenant drift | [tenant-drift.md](tenant-drift.md) |
| Errors mention 401, 403, "consent", "token" or "credentials" | [credentials-and-access.md](credentials-and-access.md) |

**Golden rules**
1. Never press *Retry* more than once without reading the error. Retrying a "licence exhausted" error does nothing.
2. Never paste passwords, secrets or tokens into tickets, Teams or these docs.
3. If a fix needs a change in a customer tenant (buying licences, consent), log it on the customer's PSA ticket first.
