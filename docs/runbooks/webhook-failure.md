# Runbook: notifications or n8n webhooks failing

## Outgoing alerts (FlowOps to Teams/n8n) stopped
1. Open **Notifications**. Failed deliveries stay there; nothing is lost.
2. Check the target is up (open the n8n editor; send a test message to the Teams webhook).
3. When it is back, press **Requeue** on the failed notifications.
4. If every delivery fails with `404` or `410`, the webhook URL was deleted or changed. Escalate so an engineer updates `FLOWOPS_WEBHOOK_URL`.

## Incoming n8n calls (n8n to FlowOps) failing
In n8n, open the failed execution and look at the HTTP node's status code:

| Code | Meaning | Action |
|---|---|---|
| 401 / 403 | API key or token wrong or expired | Escalate: [credentials-and-access.md](credentials-and-access.md) |
| 409 | Same `Idempotency-Key` with different data | The HR system re-sent a changed form. Check which one is right, then re-run with the corrected data. |
| 422 | Data was invalid (missing field, unknown tenant) | Fix the source record (HR form, tenant list) and re-run. |
| 429 | Too many calls | Wait a minute; n8n retries automatically. |
| 5xx | FlowOps is down | Check `/ready`. Escalate if it is not `ready`. |

Re-running an n8n execution is safe: FlowOps de-duplicates by ticket id and `Idempotency-Key`.
