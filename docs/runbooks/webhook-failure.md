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

Manual retry and the automatic dispatcher use the same lease claim and
conditional finalization. Manual retry may bypass a scheduled delay but
cannot take over an unexpired worker lease. A completed send with an expired
lease is an uncertain external effect: reconcile receiver-side evidence
before retrying. This is at-least-once delivery, not exactly-once execution.
The dispatcher backs off after database operational errors; repeated
outages should be escalated rather than bypassing database fencing.

The global-admin metrics endpoint exposes flowops_notification_expired_leases
as a bounded-cardinality gauge; FlowOpsExpiredNotificationLease alerts after
five minutes of sustained expired ownership. The threshold is provisional.
Check dispatcher health, database reachability and receiver-side delivery
evidence before retrying. A zero gauge does not prove all external deliveries
succeeded.

Each claim now durably counts one delivery attempt before the network call.
Abandoned leases consume that budget; once exhausted, a due abandoned row
is dead-lettered without another send. A new explicit requeue grants one
additional attempt, not an erased history. Before upgrading workers, drain
or reconcile all old in-flight leases: pre-upgrade workers counted at
finalization, so mixed versions cannot guarantee exact attempt accounting.
Malformed persisted notification envelopes are dead-lettered with a static
diagnostic, not sent or allowed to crash the dispatcher.
