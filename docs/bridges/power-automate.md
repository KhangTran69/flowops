# Power Automate bridge

## Overview

FlowOps can hand a checklist action to a Power Automate flow through an HTTP
trigger. The action boundary keeps the workflow engine, retries, leases, and
dead-letter behavior inside FlowOps while Power Automate handles downstream
Microsoft 365 or business-process steps.

The implementation is [`PowerAutomateAdapter`](../../src/actions.py), selected
with `FLOWOPS_ACTION_ADAPTER=power_automate`.

## Prerequisites

- A FlowOps worker with network access to the Power Automate trigger URL.
- A Power Automate flow with the **When an HTTP request is received** trigger.
- `FLOWOPS_POWER_AUTOMATE_URL` stored as an environment secret; never store it
  in a database row or commit it to the repository.

## Create the HTTP-triggered flow

1. Create an instant cloud flow and add the HTTP request trigger.
2. Define a JSON schema accepting `action`, `context`, and an optional
   `idempotency_key`.
3. Add the required Microsoft 365 actions and return a 2xx response after the
   flow has accepted the request.
4. Copy the generated trigger URL into `FLOWOPS_POWER_AUTOMATE_URL`.

The FlowOps worker calls the adapter from the normal durable job path, so an
HTTP failure is retried according to the job policy and eventually appears in
the failed-job queue.

## Map action, context, and idempotency

Map `action` to a flow branch such as `create_user_account` or
`grant_software_licenses`. The `context` object contains the request, employee,
tenant, job, and workflow-output fields needed by the branch. Pass
`idempotency_key` to downstream actions so a retry cannot create a duplicate
side effect.

## Test from FlowOps

```bash
export FLOWOPS_ACTION_ADAPTER=power_automate
export FLOWOPS_POWER_AUTOMATE_URL='https://example.invalid/trigger/...'
python -m src.worker --once
```

Use the simulated adapter for local development. Do not point a demo database
at a production flow without a test tenant and a deliberately scoped flow.

## Security

Keep the trigger URL in a secret manager or process environment. Restrict the
flow trigger, validate the action allowlist in the flow, and do not pass raw
credentials in `context`. Use tenant context and audit logs to identify who
caused an action.

## Idempotency

FlowOps only includes `idempotency_key` when one exists. The receiving flow
should persist or otherwise fence that key before performing a non-idempotent
operation. FlowOps job leases, retries, and the outbox provide at-least-once
delivery; idempotency must therefore be enforced at the side-effect boundary.

## Example payload

```json
{
  "action": "grant_software_licenses",
  "context": {
    "request_id": 8,
    "employee_email": "alex@example.com",
    "tenant_key": "acme",
    "job_id": 42
  },
  "idempotency_key": "onboarding:8:31"
}
```
