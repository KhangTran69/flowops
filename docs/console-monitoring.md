# FlowOps monitoring and acceptance checks

Metrics are exposed by the protected global-admin /metrics endpoint. Configure
a private scraper with a properly scoped credential; do not publish the endpoint
or embed tokens in source control. HTTP counters/histograms are per API process.
Database-backed job, run, attempt, and outbox gauges are shared across replicas:
aggregate these with max (not sum) for a single database. Scheduler tick counters
are process-local and are not a substitute for worker liveness monitoring.

## Signals and initial alerts

- HTTP error ratio, request latency p95, and successful readiness checks.
- Queued and retry-scheduled jobs, failed workflow runs, and failed outbox deliveries.
- Monitor worker, scheduler, timers and outbox process health separately.
- Use a representative staging workload to establish alert thresholds. Start with
  the original 500 ms p95 API target only for the agreed core read workload;
  do not interpret it as a measured production service level.
- The example alert rules in monitoring/flowops-alerts.yml are not installed or
  activated by this repository. Review and tune them before use.

## Recovery checks

Inspect job status and execution audit before retrying. A failed downstream
delivery does not prove that its external action did not execute. Use the
existing fenced retry mechanism and stable idempotency keys; do not manually
re-run an external integration until its actual state is reconciled.

## Acceptance coverage

The offline suite seeds 257 tenant-scoped tickets plus a foreign ticket,
checks all pages and database-backed dashboard totals, and verifies the
50-run query budget of at most six core SELECT queries. Browser CI exercises
ticket creation, filtering, workflow creation/start/approval, keyboard modal
behavior and mobile navigation using only simulated adapters. The PostgreSQL
CI stack validates migrations and durable retry behavior. These checks do
not establish production latency, full WCAG conformance or live integration
readiness.

Production workforce browser access is blocked until a real identity provider
and browser sign-in flow have been configured and independently tested.
Do not remove this release gate to enable a demo. Use the development-only
workforce setup for synthetic data.
