# Rewst implementation blueprint

FlowOps is the durable API and audit boundary; Rewst is the visual
orchestration layer. This blueprint shows how the same automation maps to
Rewst blocks without pretending that a customer-specific Rewst export can be
portable between environments.

## New-starter workflow

| Rewst block | Configuration |
|---|---|
| Trigger: webhook | Receive the HR payload and require `request_id`, email, department, and tenant |
| Jinja transform | Normalize HR field names into the FlowOps onboarding schema |
| HTTP request | `POST /onboarding-requests` with `Idempotency-Key: rewst-hr-{{ request_id }}` |
| Branch | Stop and raise an incident if FlowOps returns a non-2xx response |
| Approval | Route privileged access steps to a human approval block |
| Subworkflow | Call the Graph/Exchange action subworkflow only after approval |
| PSA action | Create or update the ConnectWise ticket using the external link |
| Teams notification | Send the request ID, tenant, and result summary |
| Error handler | Capture the Rewst job ID and FlowOps correlation ID, then notify the on-call queue |

## Required reliability settings

- Make `request_id` the idempotency identity; never use a timestamp.
- Keep Graph, Exchange Online, and ConnectWise credentials in Rewst credential
  objects, not workflow variables or ticket notes.
- Add explicit branches for `401/403`, `404`, `409`, `429`, and `5xx`.
- Retry only transient failures and let FlowOps dead-letter permanent failures.
- Pass `tenant_key` through every subworkflow and validate it against the
  tenant registry before performing a write.
- Log the Rewst job ID alongside FlowOps' correlation ID.

## Migration approach

1. Start with the [`n8n`](../n8n/) exports as the functional reference.
2. Recreate the intake, onboarding, and nightly drift/error digest as Rewst
   subworkflows.
3. Keep business rules and idempotency in FlowOps so n8n, Power Automate, and
   Rewst behave consistently.
4. Run both platforms in shadow mode against simulated adapters.
5. Cut over one tenant at a time and retain the old workflow until the
   reliability and KPI reports match.

This separation is useful in an MSP: the platform-specific workflow can change
without duplicating the safety, audit, and multi-tenant rules in every tool.
The accompanying [`manifest.json`](manifest.json) is a sanitized,
import-oriented reference manifest. Rewst exports are workspace-specific, so
the native package must be exported from the configured Rewst workspace after
credentials and tenant variables are bound.
