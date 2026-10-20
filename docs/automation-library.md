# Automation library

FlowOps is intentionally assembled from reusable, testable components:

- Routing rules: [`src/routing.py`](../src/routing.py)
- Checklist templates: [`src/onboarding.py`](../src/onboarding.py)
- DAG/workflow orchestration: [`src/workflow.py`](../src/workflow.py),
  [`src/scheduler.py`](../src/scheduler.py)
- Durable jobs, worker, and outbox: [`src/jobs.py`](../src/jobs.py),
  [`src/worker.py`](../src/worker.py), [`src/outbox.py`](../src/outbox.py)
- Jinja2 operator templates: [`src/templates/`](../src/templates/)
- iPaaS exports: [`flows/n8n/`](../flows/n8n/),
  [`flows/power-automate/`](../flows/power-automate/), and
  [`flows/rewst/`](../flows/rewst/)
- KPI reports: [`src/reports.py`](../src/reports.py) and
  [`sql/reporting_queries.sql`](../sql/reporting_queries.sql)
- Operational procedures: [`docs/runbooks/`](runbooks/)

Published workflow definitions are immutable and requests pin a definition
version. Idempotency keys fence duplicate submissions, leases fence workers,
and failed jobs/outbox deliveries become visible dead letters instead of being
silently lost.
