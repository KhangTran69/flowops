# Power Automate implementation guide

This guide turns the FlowOps API into a practical Power Automate workflow for
the job scenarios this project targets. It is intentionally written as a
build guide rather than a fake native export: connection references,
environment IDs, and tenant-specific SharePoint/Teams details belong in the
customer's Power Platform environment.

The accompanying [`solution-manifest.json`](solution-manifest.json),
[`hr-new-starter.json`](hr-new-starter.json), and
[`schemas/hr-new-starter.json`](schemas/hr-new-starter.json) are sanitized,
import-oriented reference artifacts. They are not a native Power Platform zip:
create the solution in a tenant, bind its connection references, test it, and
export the tenant-specific native package from Power Platform.

## Flow: HR new starter to FlowOps

**Trigger:** `When an HTTP request is received`

Use this JSON schema for the trigger:

```json
{
  "type": "object",
  "required": ["employee_name", "employee_email", "department", "requester_id", "request_id"],
  "properties": {
    "request_id": { "type": "string" },
    "employee_name": { "type": "string" },
    "employee_email": { "type": "string" },
    "department": { "type": "string" },
    "requester_id": { "type": "integer" },
    "tenant_key": { "type": "string" },
    "start_date": { "type": "string" }
  }
}
```

Build the actions in this order:

1. **Compose - idempotency key**

   ```
   concat('power-automate-hr-', triggerBody()?['request_id'])
   ```

2. **HTTP - create onboarding request**

   - Method: `POST`
   - URI: `{FLOWOPS_URL}/onboarding-requests`
   - Headers: `Authorization: Bearer ...`, `Idempotency-Key: outputs('Compose_-_idempotency_key')`
   - Body: map the trigger fields and set `request_type` to `onboarding`.

3. **Parse JSON - FlowOps response**

   Validate that `id`, `status`, and `checklist` exist. Do not continue on a
   response that is not a successful 2xx response.

4. **Condition - approval required**

   If the checklist contains approval-gated steps, send an **Approvals -
   Start and wait for an approval** request. The flow must not call a
   completion endpoint before the approval succeeds.

5. **Teams - notify service delivery**

   Post the request ID, employee, tenant, and FlowOps URL to the service
   delivery channel. Do not include access tokens or secrets.

6. **Response**

   Return HTTP `202` with the FlowOps request ID and `status`.

## Failure handling

Configure a parallel **Scope - error handler** with `runAfter` set to
`has failed`, `has timed out`, and `is skipped` for the main workflow scope.
The error handler should:

1. Capture the action name and `workflow()` run ID.
2. Send a Teams alert containing the run ID and FlowOps request ID.
3. POST an operational event to `/events` with `event_type` set to
   `power_automate_failure`.
4. Return a non-success response to the HR system when the trigger is
   synchronous.

Do not add a blanket “succeeded” response after a failed HTTP action. FlowOps
uses idempotency keys, so a retried Power Automate run is safe.

## Flow: ticket notification

Use the FlowOps outbound webhook as the trigger for a second flow:

1. Trigger on `When an HTTP request is received`.
2. Validate the event envelope and reject payloads without `event_id`.
3. Switch on `event_type`.
4. For `ticket.created` or `ticket.escalated`, post a Teams adaptive card.
5. For `workflow.failed` or `outbox.failed`, create a SharePoint list item
   containing the correlation ID, tenant, job ID, and error summary.

Keep the HTTP trigger protected with an API key or a secured API Management
endpoint. Never put customer credentials in a flow body.

## Deployment checklist

- Store `FLOWOPS_URL`, bearer tokens, and webhook keys in environment
  variables or Azure Key Vault-backed connection references.
- Use separate Power Platform solutions for development, test, and production.
- Add retry policies only for transient HTTP failures (408, 429, and 5xx).
- Set a concurrency limit when the downstream system has tenant/API limits.
- Export the solution after each tested change and commit sanitized solution
  metadata to version control.
- Test duplicate delivery, expired credentials, approval rejection, and a
  FlowOps dead-letter job before production release.

See [`docs/runbooks/credentials-and-access.md`](../../docs/runbooks/credentials-and-access.md)
and [`docs/runbooks/failed-jobs.md`](../../docs/runbooks/failed-jobs.md) for
the operator procedures after deployment.
