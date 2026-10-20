# Make, Rewst, and Zapier bridges

FlowOps exposes ordinary HTTP contracts that can be connected to common iPaaS
products without a product-specific SDK.

## Inbound automation

Make.com, Rewst, or Zapier can send tickets to `POST /webhooks/intake` from an
HTTP module, webhook step, or Zap action. The endpoint accepts one ticket or a
list of tickets. A successful request returns HTTP 200; a malformed item makes
the whole batch fail with HTTP 422 before any item is processed.

```bash
curl -X POST http://127.0.0.1:8000/webhooks/intake \
  -H 'Content-Type: application/json' \
  -d '{"userId":7,"id":4201,"title":"VPN cannot connect","completed":false}'
```

Bulk payloads use the same item shape:

```json
[
  {"userId": 7, "id": 4201, "title": "VPN cannot connect", "completed": false},
  {"userId": 8, "id": 4202, "title": "Laptop will not start", "completed": false}
]
```

Configure `FLOWOPS_WEBHOOK_API_KEY` when the inbound endpoint must require an
API key. See the API implementation in [`src/api.py`](../../src/api.py).

## Outbound automation

Set `FLOWOPS_ACTION_ADAPTER=webhook` and configure:

```bash
export FLOWOPS_WEBHOOK_URL='https://hook.example/flowops'
export FLOWOPS_WEBHOOK_SECRET='rotate-me'
```

`GenericWebhookAdapter` in [`src/actions.py`](../../src/actions.py) posts the
following shape to a Make custom webhook, Rewst webhook, or Zapier Catch Hook:

```bash
curl -X POST "$FLOWOPS_WEBHOOK_URL" \
  -H 'Content-Type: application/json' \
  -H 'X-Webhook-Secret: rotate-me' \
  -d '{"action":"create_user_account","context":{"request_id":8},"idempotency_key":"onboarding:8:29"}'
```

The `idempotency_key` field is omitted when no key is available. Configure the
receiving scenario/workflow/Zap to deduplicate it before side effects.

## Platform mapping

- **Make.com:** Custom webhook trigger → router → Microsoft 365/PSA modules.
- **Rewst:** Webhook trigger → workflow step/action → tenant-aware subworkflow.
- **Zapier:** Catch Hook trigger → filter/path → downstream app action.

Keep FlowOps as the durable system of record for workflow state, retries,
approvals, and dead letters. Treat iPaaS execution as an external side effect
and return a clear success/failure response.
