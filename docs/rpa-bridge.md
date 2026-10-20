# RPA bridge

## Pattern

FlowOps can submit a checklist action to an RPA bot through a webhook. This is
useful for applications without an API, legacy desktop software, or a bot
queue managed by UiPath, Automation Anywhere, Blue Prism, or Asio RPA.

[`src/rpa.py`](../src/rpa.py) provides `RPAWebhookAdapter` and
`rpa_from_env()`. Configure:

```bash
export FLOWOPS_ACTION_ADAPTER=rpa
export FLOWOPS_RPA_WEBHOOK_URL='https://rpa.example/jobs'
export FLOWOPS_RPA_BOT='software-licensing-bot'
export FLOWOPS_RPA_SECRET='rotate-me'
```

The adapter sends:

```json
{
  "bot": "software-licensing-bot",
  "action": "grant_software_licenses",
  "context": {"request_id": 8, "employee_email": "alex@example.com"},
  "idempotency_key": "onboarding:8:31"
}
```

The idempotency field is included only when present. The default secret header
is `X-RPA-Secret`; a custom header can be supplied when constructing the
adapter directly.

## Worker and DAG integration

RPA calls occur at the existing actions boundary. The workflow DAG schedules a
ready checklist item, the durable worker claims it with a lease, and the
adapter submits the action. HTTP errors follow the normal retry budget and
eventually become a failed job/dead letter. The workflow event and audit
records retain the action, tenant, job, and correlation context.

RPA systems must make their own side effect idempotent using the supplied key.
An HTTP 2xx should mean the bot accepted the job, not necessarily that the
desktop task has completed; use a callback or polling connector if completion
must be observed.

Asio RPA can be triggered through an outbound webhook/RPA queue using this
generic adapter, or integrated through Rewst or Power Automate as an
intermediary.

## Operational checklist

1. Use a dedicated bot identity with least privilege.
2. Store `FLOWOPS_RPA_SECRET` outside the repository and rotate it.
3. Confirm the bot validates action and tenant allowlists.
4. Test timeout, retry, duplicate-key, and dead-letter behavior in a sandbox.
5. Reconcile accepted RPA jobs with FlowOps workflow state before closing a
   request.

See [`docs/runbooks/rpa-bridge.md`](runbooks/rpa-bridge.md) for incident
handling.
