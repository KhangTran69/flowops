# Multi-tenant GDAP runbook

FlowOps models an MSP partner with one application and a tenant registry. Each
tenant has a key, Entra tenant ID, baseline, and `credential_ref`; Graph calls
resolve the tenant context before executing an action.

Run a baseline comparison with:

```bash
curl -X POST 'http://127.0.0.1:8000/tenants/drift-check?tenant=acme'
```

Omit `tenant` to check all active tenants. Review `tenant.drift_check` audit
entries and remediate deviations before provisioning. Keep tenant keys on
requests, events, logs, reports, and outbound contexts so cross-tenant runs
remain attributable.

`LicenceExhausted` and `UsageLocationMissing` are configuration/business
failures, not transient network failures; they should be surfaced to the
operator rather than retried indefinitely. Validate delegated access and
credential rotation per tenant before enabling real Graph actions.
