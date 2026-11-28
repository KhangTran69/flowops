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


## Release-validation evidence (November 27, 2026)

CI revision 08a61294e1fdce37b2a5d284c12b606a57d8d231 passed all eight
configured jobs (run 45938463991), with 495 offline tests passed, 22 skipped,
and 22 separate PostgreSQL tests passed. Vite 6.4.6 was built and npm audit
reported zero vulnerabilities at that revision.

The disposable PostgreSQL benchmark used 1,000 synthetic tickets and 50
workflow runs, 40 requests per endpoint per concurrency level. TestClient
measurements include the application and database but exclude WAN/TLS and
real integrations.

| Endpoint | Serial p95 ms | Concurrent (4) p95 ms | Concurrent requests/s |
| --- | ---: | ---: | ---: |
| Tickets | 6.97 | 39.37 | 163.09 |
| Workflow runs | 26.42 | 127.15 | 50.11 |
| Dashboard summary | 11.16 | 37.18 | 130.54 |

These are historical synthetic baseline measurements, not a production SLA.
The repeatable scripts run on disposable CI databases only. No new database
index was justified by these results.

Migration 0017 adds a revocation table containing only a hash of a session's
random CSRF token and its retention deadline. Logout records revocation and
the audit event atomically. Existing signed cookies remain accepted unless
revoked or expired. Revocation is retained for the maximum supported cookie
lifetime (24 hours); expired rows are pruned on subsequent logout. A rollback
that drops this table removes revocation history: before rollback, rotate
the session signing secret and require reauthentication. Never downgrade
a production database just to test this procedure.

The console delivery list exposes resource identifiers, status, attempts and
creation timestamps; destinations, raw errors and payloads remain outside
this read-only view. Existing tenant scope is enforced before pagination.
It cannot trigger an external notification.

The focused browser accessibility checks cover labels, names, document
metadata and selected ARIA rules; the monitoring CI uses promtool to check
syntax and synthetic alert behavior. Neither substitutes for a full WCAG
2.2 AA audit or a live monitoring deployment. Production Entra/gateway
configuration, secure logout at the identity provider, workforce browser
sign-in, production backup/restore rehearsals and live connector verification
remain release gates.

Data-safety gate (ROADMAP 2b) evidence: the backup/restore rehearsal runs in
CI as a `docker-postgres` job step ("Backup-restore rehearsal") — dump of the
paused stack, restore into a clean `postgres:16-alpine`, migration 0016
roundtrip replayed on the restored copy, nine-table reconciliation, timings
uploaded as the `backup-restore-timings` artifact (first dated run
2026-11-28; measured window in `docs/runbooks/backup-restore.md`). A
rehearsal at production data volume remains part of the eventual production
deployment.
